"""Local HTTP API + static UI.

Security model of a *local* web app:
* binds to 127.0.0.1 only (the launcher enforces it);
* ``Host`` header allow-list → defeats DNS rebinding from malicious web pages;
* every /api call carries ``X-Sextant-Token`` (random per launch, handed to the browser in the URL fragment).
  A custom header forces a CORS preflight that is never granted, so other websites cannot drive the API;
* strict CSP (no inline scripts, no remote resources), no caching of API responses.
"""

from __future__ import annotations

import hmac
import json
import logging
import os
import re
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Literal

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import FileResponse, JSONResponse, Response
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, ConfigDict, Field

from .. import APP_NAME, __version__
from ..config import SETTINGS_BY_KEY, Settings
from ..engine.coverage import DISCLAIMER
from ..engine.input import parse_input
from ..engine.limits import presets_payload
from ..engine.orchestrator import Engine
from ..model import utcnow
from ..ontology import ENTITY_TYPES, RELATION_TYPES, ontology_payload
from ..engine.countries import infer_countries
from ..registry import catalog as CAT
from ..registry import catalog_audit as AUDIT
from ..registry import connectors as REG
from ..registry import countries as COUNTRIES
from ..registry import coverage_model as CM
from ..registry import jurisdictions as JUR
from ..registry import platforms as PLAT
from ..registry.health import connector_health, snapshot as health_snapshot
from ..reporting.report import build_report
from ..storage.crypto import Vault
from ..storage.db import Database
from ..assistant import analysis as AA
from ..assistant import copilot as AC
from ..assistant import planner as AP
from ..assistant import providers as AIP
from . import views

log = logging.getLogger("sextant.api")

INV_ID = re.compile(r"^[a-f0-9]{16}$")
CC_RE = re.compile(r"^[A-Z]{2,3}$")
OBJ_ID = re.compile(r"^[A-Za-z0-9_-]{6,64}$")
CSP = ("default-src 'self'; script-src 'self'; style-src 'self'; img-src 'self' data:; connect-src 'self'; "
       "font-src 'self'; object-src 'none'; base-uri 'none'; form-action 'none'; frame-ancestors 'none'")
PRIVACY = ("Investigations are stored only on this computer, encrypted, in the data folder shown here. There is no "
           "Sextant server. External sources receive only what each connector discloses, and only when it runs.")


@dataclass
class AppState:
    data_dir: Path
    frontend_dir: Path
    token: str
    allowed_hosts: set[str]
    passphrase: str | None = None
    http_factory: Any = None
    db: Database = field(init=False)
    settings: Settings = field(init=False)
    engine: Engine = field(init=False)
    catalog: CAT.CatalogStore = field(init=False)

    def __post_init__(self) -> None:
        self.open()

    def open(self) -> None:
        self.db = Database(self.data_dir, Vault.open(self.data_dir, self.passphrase))
        self.settings = Settings(self.db)
        self.engine = Engine(self.db, self.settings, self.data_dir, self.http_factory)
        self.catalog = CAT.store_for(self.db)
        try:
            self.db.seed_jurisdictions([{**j.to_dict(), "code": j.code} for j in JUR.jurisdictions().values()])
        except Exception:  # the catalog tables are a convenience copy; never block startup
            log.exception("Could not seed the jurisdiction tables")


# --------------------------------------------------------------------------- request models

class ParseInput(BaseModel):
    text: str = Field(default="", max_length=5000)
    structured: dict[str, Any] | None = None
    default_region: str | None = Field(default=None, max_length=2)
    countries: list[str] = Field(default_factory=list, max_length=20)
    search_everything: bool = False


class CreateInput(ParseInput):
    budget_usd: float | None = Field(default=None, ge=0, le=100_000)
    confirm_above_usd: float | None = Field(default=None, ge=0, le=100_000)
    unlimited_budget: bool = False
    depth: str = Field(default="2", pattern=r"^[1-4]|custom$")
    overrides: dict[str, Any] | None = None
    options: dict[str, Any] | None = None
    purpose: str = Field(default="", max_length=2000)
    mode: Literal["auto", "manual"] = "auto"


class BudgetFields(BaseModel):
    budget_usd: float | None = Field(default=None, ge=0, le=100_000)
    confirm_above_usd: float | None = Field(default=None, ge=0, le=100_000)


class ContinueInput(BaseModel):
    overrides: dict[str, Any] | None = None


class TransformsInput(BaseModel):
    transforms: list[str] | Literal["all", "auto"] = "auto"
    expand_levels: int = Field(default=0, ge=0, le=4)


class DecisionInput(BaseModel):
    action: Literal["confirm", "separate", "ignore", "reset"]
    note: str = Field(default="", max_length=500)


class MergeInput(BaseModel):
    keep: str
    merge: str
    note: str = Field(default="", max_length=500)


class UnmergeInput(BaseModel):
    merged_key: str = Field(max_length=500)


class AddEntityInput(BaseModel):
    type: str
    value: str = Field(max_length=500)
    label: str = Field(default="", max_length=300)
    link_from: str | None = None
    rel_type: str = "ASSOCIATED_WITH"


class SourcePrefsInput(BaseModel):
    enabled: bool | None = None
    auto_run: bool | None = None
    manual_only: bool | None = None        # "Mark source manual-only" (after a verification challenge)
    reset: bool = False


class CredentialsInput(BaseModel):
    values: dict[str, str | None]


class SettingsInput(BaseModel):
    values: dict[str, str | int | float | bool | None]


class WipeInput(BaseModel):
    confirm: str


class CustomSourceInput(BaseModel):
    """Former country-page form (kept for compatibility; the source is stored in the catalog's user layer)."""
    country: str = Field(min_length=2, max_length=3)
    title: str = Field(min_length=2, max_length=300)
    url: str = Field(min_length=8, max_length=1000)
    kind: str = Field(default="government_portal", max_length=40)
    access: str = Field(default="unknown", max_length=20)
    name_search: bool = False
    notes: str = Field(default="", max_length=1000)


class _Strict(BaseModel):
    """Catalog inputs accept only declared metadata fields: anything else (code, commands, queries…) is refused."""
    model_config = ConfigDict(extra="forbid")


class CatalogSourceInput(_Strict):
    title: str = Field(max_length=300)
    url: str = Field(max_length=1000)
    category: str = Field(max_length=80)
    source_type: str = Field(default="reference", max_length=20)
    access: str = Field(default="unknown", max_length=20)
    kind: str = Field(default="", max_length=40)
    jurisdictions: list[str] = Field(default_factory=list, max_length=60)
    platforms: list[str] = Field(default_factory=list, max_length=20)
    connector: str = Field(default="", max_length=80)
    inputs: list[str] = Field(default_factory=list, max_length=20)
    official: bool = False
    name_search: bool = False
    description: str = Field(default="", max_length=2000)
    person_data: str = Field(default="", max_length=300)
    notes: str = Field(default="", max_length=2000)
    review: str = Field(default="not_verified", max_length=20)
    provider_class: str = Field(default="public", max_length=20)
    pricing: str = Field(default="", max_length=200)
    auth: str = Field(default="", max_length=120)
    terms_url: str = Field(default="", max_length=1000)
    data_types: list[str] = Field(default_factory=list, max_length=20)
    access_models: list[str] = Field(default_factory=list, max_length=8)
    api_available: str = Field(default="unknown", max_length=10)
    web_app: bool = False
    documentation_url: str = Field(default="", max_length=1000)
    coverage: str = Field(default="", max_length=300)
    limitations: str = Field(default="", max_length=1000)
    last_verified: str = Field(default="", max_length=10)
    enabled: bool | None = None
    allow_duplicate: bool = False


class CatalogSourcePatch(_Strict):
    enabled: bool | None = None
    review: str | None = Field(default=None, max_length=20)


class DuplicateQuery(_Strict):
    title: str = Field(default="", max_length=300)
    url: str = Field(default="", max_length=1000)
    jurisdictions: list[str] = Field(default_factory=list, max_length=60)
    platforms: list[str] = Field(default_factory=list, max_length=20)
    connector: str = Field(default="", max_length=80)
    exclude_id: str | None = Field(default=None, max_length=80)


class CategoryInput(_Strict):
    title: str = Field(max_length=120)
    group: str = Field(default="other", max_length=40)
    description: str = Field(default="", max_length=500)


class PlatformInput(_Strict):
    name: str = Field(max_length=200)
    url: str = Field(max_length=1000)
    category: str = Field(default="other", max_length=40)
    profile_url: str = Field(default="", max_length=1000)
    countries: list[str] = Field(default_factory=list, max_length=60)
    region: str = Field(default="", max_length=120)
    notes: str = Field(default="", max_length=1000)
    status: str = Field(default="active", max_length=20)
    public_profile: str = Field(default="yes", max_length=20)
    allow_duplicate: bool = False


class IgnoreInput(_Strict):
    item: str = Field(min_length=1, max_length=300)
    ignored: bool = True


class ResetInput(_Strict):
    confirm: str = Field(max_length=40)


class PlanInput(ParseInput):
    mode: Literal["quick", "balanced", "deep"] = "balanced"


class ActionInput(_Strict):
    action_id: str = Field(max_length=200)
    confirm_paid: bool = False


class ChatInput(_Strict):
    text: str = Field(min_length=1, max_length=4000)
    confirm_budget: bool = False


class ProposalDecision(_Strict):
    decision: Literal["apply", "cancel"]


class NoteInput(_Strict):
    title: str = Field(min_length=1, max_length=200)
    body: str = Field(default="", max_length=4000)
    identifiers: list[str] = Field(default_factory=list, max_length=30)
    status: str = Field(default="info", max_length=20)


class BudgetDecision(_Strict):
    decision: Literal["continue", "stop"]
    budget_usd: float | None = Field(default=None, ge=0, le=100_000)


class BudgetUpdate(_Strict):
    budget_usd: float | None = Field(default=None, ge=0, le=100_000)
    confirm_above_usd: float | None = Field(default=None, ge=0, le=100_000)


class DiscoverProfilesInput(_Strict):
    entity_id: str | None = Field(default=None, max_length=80)
    platforms: list[str] = Field(default_factory=list, max_length=30)


class AIProfileInput(_Strict):
    enabled: bool | None = None
    model: str | None = Field(default=None, max_length=200)
    base_url: str | None = Field(default=None, max_length=300)
    temperature: float | None = None
    max_output: int | None = None
    reasoning: str | None = Field(default=None, max_length=20)
    timeout: float | None = None
    key: str | None = Field(default=None, max_length=4000)
    clear_key: bool = False
    make_primary: bool = False


class FailoverInput(_Strict):
    order: list[str] = Field(default_factory=list, max_length=10)


class PriceInput(_Strict):
    provider: str = Field(max_length=80)
    service: str = Field(default="api", max_length=80)
    connector: str = Field(default="", max_length=80)
    model: str = Field(default="", max_length=200)
    per_request: float | None = None
    input_per_mtok: float | None = None
    output_per_mtok: float | None = None
    cached_input_per_mtok: float | None = None
    reasoning_per_mtok: float | None = None
    effective_date: str = Field(default="", max_length=10)
    source_url: str = Field(default="", max_length=500)
    verified_date: str = Field(default="", max_length=10)
    note: str = Field(default="", max_length=500)


class InspectInput(_Strict):
    url: str = Field(max_length=1000)
    jurisdiction: str = Field(default="", max_length=3)
    category: str = Field(default="", max_length=80)


class SourceDiscoveryInput(_Strict):
    request: str = Field(default="", max_length=300)
    jurisdiction: str = Field(default="", max_length=3)
    category: str = Field(default="", max_length=80)


class ScopedResetInput(_Strict):
    scopes: list[Literal["catalog", "history", "ai_settings", "credentials"]] = Field(min_length=1, max_length=4)
    confirm: str = Field(max_length=40)


class AIKeyInput(_Strict):
    value: str | None = Field(default=None, max_length=4000)


class FallbackInput(_Strict):
    connector: str = Field(max_length=80)
    entity_id: str = Field(max_length=80)


class ImportInput(_Strict):
    document: dict[str, Any]
    dry_run: bool = True                   # preview by default: nothing is written unless dry_run is false
    overwrite: bool = False                # replace records with the same id instead of skipping them
    restore_preferences: bool = False      # also apply the file's on/off switches of built-in sources


def _id(value: str, rx: re.Pattern = OBJ_ID) -> str:
    if not rx.match(value or ""):
        raise HTTPException(400, "Invalid id")
    return value


# --------------------------------------------------------------------------- app

def create_app(state: AppState) -> FastAPI:
    @asynccontextmanager
    async def lifespan(_app: FastAPI):
        yield
        for inv_id in list(state.engine.active):
            await state.engine.stop(inv_id)

    app = FastAPI(title=APP_NAME, version=__version__, docs_url=None, redoc_url=None, openapi_url=None, lifespan=lifespan)
    eng = lambda: state.engine  # noqa: E731 - the engine is replaced on wipe

    @app.middleware("http")
    async def guard(request: Request, call_next):
        host = request.headers.get("host", "")
        if host not in state.allowed_hosts:
            return JSONResponse({"detail": "Host not allowed"}, status_code=421)
        if request.url.path.startswith("/api/"):
            token = request.headers.get("x-sextant-token", "")
            if not hmac.compare_digest(token.encode(), state.token.encode()):
                return JSONResponse({"detail": "Missing or invalid local session token"}, status_code=401)
            origin = request.headers.get("origin")
            if origin and origin.split("://", 1)[-1] not in state.allowed_hosts:
                return JSONResponse({"detail": "Cross-origin request rejected"}, status_code=403)
        try:
            response = await call_next(request)
        except KeyError:
            response = JSONResponse({"detail": "Not found"}, status_code=404)
        except ValueError as exc:
            response = JSONResponse({"detail": str(exc)[:300]}, status_code=400)
        response.headers["Content-Security-Policy"] = CSP
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["Referrer-Policy"] = "no-referrer"
        response.headers["X-Frame-Options"] = "DENY"
        response.headers["Cross-Origin-Opener-Policy"] = "same-origin"
        response.headers["Cross-Origin-Resource-Policy"] = "same-origin"
        response.headers["Permissions-Policy"] = "camera=(), microphone=(), geolocation=()"
        if request.url.path.startswith("/api/"):
            response.headers["Cache-Control"] = "no-store"
        return response

    # ------------------------------------------------------------------ meta
    @app.get("/api/meta")
    def meta() -> dict:
        avail = [REG.connector_status(c, state.settings)[0] for c in REG.REGISTRY.values()]
        sn = state.catalog.snapshot()
        return {"app": APP_NAME, "version": __version__, "data_dir": str(state.data_dir), "database": str(state.db.path),
                "encryption": state.db.vault.mode, "privacy": PRIVACY, "coverage_disclaimer": DISCLAIMER,
                "connectors": {"total": len(avail), "available": avail.count("available")},
                "platforms": len(PLAT.curated()) + len(PLAT.imported()) + len(sn.platforms),
                "platforms_curated": len(PLAT.curated()), "user_sources": len(sn.sources),
                "user_platforms": len(sn.platforms), "catalog_version": sn.version,
                "countries": len(COUNTRIES.sovereign()), "jurisdictions": len(JUR.jurisdictions())}

    @app.get("/api/ontology")
    def ontology() -> dict:
        return ontology_payload()

    @app.get("/api/depth-presets")
    def depth_presets() -> list[dict]:
        return presets_payload()

    # ------------------------------------------------------------------ input & plan
    @app.post("/api/parse")
    def parse(body: ParseInput) -> dict:
        parsed = parse_input(body.text, body.structured, body.default_region or state.settings.get("default_region"),
                             state.catalog.snapshot().platform_objects)
        types = {s.type for s in parsed.seeds} | ({"person"} if parsed.target_name else set()) | \
                ({"organization"} if parsed.target_type == "organization" else set())
        plan: dict[str, dict] = {}
        for name, conn in REG.REGISTRY.items():
            ins = set(conn.input_types())
            if not ins & types:
                continue
            status, note = REG.connector_status(conn, state.settings)
            m = conn.manifest
            plan[name] = {"name": name, "title": m.title, "category": m.category, "category_title": m.category_title,
                          "status": status, "status_title": REG.STATUS_TITLES[status], "note": note,
                          "auto": REG.auto_allowed(conn, state.settings), "access": m.access, "cost": m.cost,
                          "sends": m.disclosure.sends, "destinations": list(m.disclosure.destinations),
                          "mode": m.disclosure.mode, "inputs": sorted(ins & types)}
        countries = infer_countries(parsed, [c.upper() for c in body.countries])
        codes = [c["code"] for c in countries]
        prefs = state.settings.connector_prefs
        for name, item in plan.items():
            m = REG.REGISTRY[name].manifest
            if item["status"] == "disabled" and prefs(name).get("enabled") is None:
                hit = [c for c in m.supported_countries if c != "*" and c in codes]
                if hit:
                    item.update(status="available", status_title="Pre-selected", note=f"The investigation points to {', '.join(hit)}",
                                preselected=True)
                elif body.search_everything and not m.requires_subscription and tuple(m.supported_countries) == ("*",):
                    item.update(status="available", status_title="Enabled by SEARCH EVERYTHING",
                                note=m.opt_in_reason or "Off by default", opt_in=True)
            if body.search_everything and item["status"] == "available":
                item["auto"] = True
        ctx = _cov_ctx()
        rows = {r["code"]: r for r in CM.cached_rows(ctx)}
        country_plan = []
        for c in countries:
            r = rows.get(c["code"])
            if r is None:
                continue
            country_plan.append({**c, "level": r["level"], "level_title": r["level_title"],
                                 "connectors": [CM._connector_brief(ctx, n) for n in ctx.country_connectors(c["code"])],
                                 "official_links": r["curated_sources"], "your_sources": r["custom_active"],
                                 "potential_sources": r["discovered_sources"],
                                 "regional_platforms": r["regional_platforms"]})
        items = list(plan.values())
        counts = {"available": sum(1 for p in items if p["status"] == "available"),
                  "needs_credentials": sum(1 for p in items if p["status"] == "needs_credentials"),
                  "needs_subscription": sum(1 for p in items if p["status"] == "needs_subscription"),
                  "unavailable": sum(1 for p in items if p["status"] in ("disabled", "not_installed", "catalog_only",
                                                                         "retired", "manual", "excluded")),
                  "paid_configured": sum(1 for p in items if p["status"] == "available" and p["access"] in CM.COMMERCIAL_ACCESS)}
        return {"input": parsed.to_dict(), "title": parsed.title, "countries": country_plan, "plan_counts": counts,
                "search_everything": body.search_everything, "disclaimer": CM.SEARCH_DISCLAIMER,
                "plan": sorted(items, key=lambda p: (p["status"] != "available", p["category"], p["title"]))}

    # ------------------------------------------------------------------ investigations
    @app.post("/api/investigations", status_code=201)
    async def create(body: CreateInput) -> dict:
        parsed = parse_input(body.text, body.structured, body.default_region or state.settings.get("default_region"),
                             state.catalog.snapshot().platform_objects)
        if not parsed.seeds and not parsed.target_name:
            raise HTTPException(400, "; ".join(parsed.errors) or "Nothing to investigate")
        try:
            options = dict(body.options or {})
            if body.countries:
                options["countries"] = [c.upper() for c in body.countries if COUNTRIES.get(c.upper())]
            if body.unlimited_budget:
                options["budget_usd"] = None
            elif body.budget_usd is not None:
                options["budget_usd"] = body.budget_usd
            if body.confirm_above_usd is not None:
                options["confirm_above_usd"] = body.confirm_above_usd
            if body.search_everything:
                options["search_everything"] = True
                options.pop("categories", None)
                options.pop("platforms", None)
            inv_id = eng().create(parsed, depth=body.depth, overrides=body.overrides, options=options,
                                  purpose=body.purpose)
        except ValueError as exc:
            raise HTTPException(400, str(exc)) from None
        if body.mode == "auto":
            await eng().start(inv_id)
        return {"id": inv_id, "notes": parsed.notes, "errors": parsed.errors}

    @app.get("/api/investigations")
    def list_investigations() -> list[dict]:
        out = state.db.list_investigations()
        for inv in out:
            rt = eng().active.get(inv["id"])
            if rt is not None:
                inv["status"] = rt.status
        return out

    @app.get("/api/investigations/{inv_id}")
    def get_investigation(inv_id: str) -> dict:
        rt = eng().runtime(_id(inv_id, INV_ID))
        d = rt.data
        return {"id": rt.id, "title": rt.title, "purpose": d.get("purpose"), "input": d.get("input"),
                "options": rt.options, "info": d.get("info"), "started_at": rt.started_at, "finished_at": rt.finished_at,
                **eng().status_payload(rt), "target": rt.graph.target.id if rt.graph.target else None}

    @app.delete("/api/investigations/{inv_id}")
    async def delete_investigation(inv_id: str) -> dict:
        await eng().delete(_id(inv_id, INV_ID))
        return {"ok": True}

    @app.get("/api/investigations/{inv_id}/graph")
    def graph(inv_id: str, min_confidence: float = 0.0) -> dict:
        rt = eng().runtime(_id(inv_id, INV_ID))
        return {**views.graph_payload(rt.graph, min_confidence), "seq": rt.events.seq}

    @app.get("/api/investigations/{inv_id}/events")
    async def events(inv_id: str, after: int = 0, wait: float = 0.0) -> dict:
        rt = eng().runtime(_id(inv_id, INV_ID))
        res = await rt.events.wait(after, min(max(wait, 0.0), 25.0)) if wait else rt.events.since(after)
        return {**res, "status": eng().status_payload(rt)}

    @app.get("/api/investigations/{inv_id}/entities/{entity_id}")
    def entity(inv_id: str, entity_id: str) -> dict:
        rt = eng().runtime(_id(inv_id, INV_ID))
        e = rt.graph.by_id(_id(entity_id))
        if e is None:
            raise HTTPException(404, "Entity not found")
        return views.entity_detail(rt.graph, e)

    @app.get("/api/investigations/{inv_id}/entities/{entity_id}/transforms")
    def entity_transforms(inv_id: str, entity_id: str) -> list[dict]:
        return eng().applicable_transforms(_id(inv_id, INV_ID), _id(entity_id))

    @app.post("/api/investigations/{inv_id}/entities/{entity_id}/transforms")
    async def run_transforms(inv_id: str, entity_id: str, body: TransformsInput) -> dict:
        return await eng().run_transforms(_id(inv_id, INV_ID), _id(entity_id), body.transforms,
                                          expand_levels=body.expand_levels)

    @app.post("/api/investigations/{inv_id}/entities/{entity_id}/decision")
    async def decision(inv_id: str, entity_id: str, body: DecisionInput) -> dict:
        try:
            return await eng().decide(_id(inv_id, INV_ID), _id(entity_id), body.action, body.note)
        except ValueError as exc:
            raise HTTPException(400, str(exc)) from None

    @app.post("/api/investigations/{inv_id}/entities")
    async def add_entity(inv_id: str, body: AddEntityInput) -> dict:
        if body.type not in ENTITY_TYPES or body.rel_type not in RELATION_TYPES:
            raise HTTPException(400, "Unknown entity or relationship type")
        try:
            return await eng().add_entity(_id(inv_id, INV_ID), body.type, body.value, body.label,
                                          _id(body.link_from) if body.link_from else None, body.rel_type)
        except ValueError as exc:
            raise HTTPException(400, str(exc)) from None

    @app.post("/api/investigations/{inv_id}/merge")
    async def merge(inv_id: str, body: MergeInput) -> dict:
        try:
            return await eng().merge(_id(inv_id, INV_ID), _id(body.keep), _id(body.merge), body.note)
        except ValueError as exc:
            raise HTTPException(400, str(exc)) from None

    @app.post("/api/investigations/{inv_id}/entities/{entity_id}/unmerge")
    async def unmerge(inv_id: str, entity_id: str, body: UnmergeInput) -> dict:
        return await eng().unmerge(_id(inv_id, INV_ID), _id(entity_id), body.merged_key)

    @app.post("/api/investigations/{inv_id}/relationships/{rel_id}/reject")
    async def reject(inv_id: str, rel_id: str) -> dict:
        return await eng().reject_relationship(_id(inv_id, INV_ID), _id(rel_id))

    @app.post("/api/investigations/{inv_id}/{action}")  # declared after the specific POST routes
    async def lifecycle(inv_id: str, action: Literal["start", "pause", "resume", "stop", "continue"],
                        body: ContinueInput | None = None) -> dict:
        inv_id = _id(inv_id, INV_ID)
        e = eng()
        if action == "start":
            await e.start(inv_id)
        elif action == "pause":
            await e.pause(inv_id)
        elif action == "resume":
            await e.resume(inv_id)
        elif action == "stop":
            await e.stop(inv_id)
        else:
            await e.continue_(inv_id, (body.overrides if body else None))
        return e.status_payload(e.runtime(inv_id))

    @app.get("/api/investigations/{inv_id}/runs")
    def runs(inv_id: str) -> dict:
        rt = eng().runtime(_id(inv_id, INV_ID))
        g = rt.graph
        tasks = sorted(rt.tasks.values(), key=lambda t: (t.status != "running", t.status != "queued", -t.priority))
        return {"runs": [{**r.to_dict(), "input": g.get(r.input_key).summary() if g.get(r.input_key) else None}
                         for r in sorted(rt.runs.values(), key=lambda r: r.started_at, reverse=True)],
                "tasks": [{**t.to_dict(), "entity": g.get(t.entity_key).summary() if g.get(t.entity_key) else None}
                          for t in tasks if t.status in ("queued", "running")][:500]}

    @app.get("/api/investigations/{inv_id}/relationships/{rel_id}")
    def relationship(inv_id: str, rel_id: str) -> dict:
        rt = eng().runtime(_id(inv_id, INV_ID))
        r = rt.graph.rel_by_id(_id(rel_id))
        if r is None:
            raise HTTPException(404, "Relationship not found")
        return views.relationship_detail(rt.graph, r)

    # ------------------------------------------------------------------ assistant
    @app.post("/api/assistant/plan")
    def assistant_plan(body: PlanInput) -> dict:
        """The plan for an input before anything runs: identifiers, name-derived candidates, sources with reasons and
        cost, jurisdictions (inferred or chosen), platforms, and what will not be used and why."""
        parsed = parse_input(body.text, body.structured, body.default_region or state.settings.get("default_region"),
                             state.catalog.snapshot().platform_objects)
        countries = infer_countries(parsed, [c.upper() for c in body.countries])
        plan = AP.plan(parsed, countries, body.mode, state.settings, state.catalog.snapshot(), state.db.health_records())
        from ..costs import ledger as LEDGER
        from ..discovery import router as ROUTER
        plan["forecast"] = LEDGER.forecast(plan, state.settings, AIP.config(state.settings))
        plan["search_providers"] = ROUTER.ordered(state.settings)
        plan["web_discovery_unavailable"] = ROUTER.unavailable_message(state.settings)
        plan["default_budget_usd"] = state.settings.get_float("default_budget_usd", 0.0, 1_000_000.0) or None
        return {"input": parsed.to_dict(), **plan}

    @app.get("/api/investigations/{inv_id}/assistant")
    def assistant(inv_id: str) -> dict:
        rt = eng().runtime(_id(inv_id, INV_ID))
        d = AA.analyze(rt, eng(), state.settings, state.catalog.snapshot().platform_objects)
        d["provider"] = AIP.config(state.settings)
        d["log"] = AC.history(state.db, rt.id)
        d["decisions"] = state.db.decisions(rt.id)
        d["proposals"] = AC.proposals(state.db, rt.id)
        d["notes"] = state.db.notes(rt.id)
        d["budget_hold"] = rt.data.get("budget_hold")
        return d

    @app.post("/api/investigations/{inv_id}/assistant/actions")
    async def assistant_action(inv_id: str, body: ActionInput) -> dict:
        try:
            return await AC.run_action(eng(), state.settings, state.db, _id(inv_id, INV_ID), body.action_id,
                                       confirm_paid=body.confirm_paid,
                                       user_platforms=state.catalog.snapshot().platform_objects)
        except KeyError as exc:
            raise HTTPException(404, str(exc).strip("'\"")) from None
        except ValueError as exc:
            raise HTTPException(400, str(exc)) from None

    @app.post("/api/investigations/{inv_id}/assistant/chat")
    async def assistant_chat(inv_id: str, body: ChatInput) -> dict:
        try:
            return await AC.chat(eng(), state.settings, state.db, _id(inv_id, INV_ID), body.text,
                                 state.catalog.snapshot().platform_objects, confirm_budget=body.confirm_budget)
        except AIP.ProviderError as exc:
            raise HTTPException(400, str(exc)) from None

    @app.post("/api/investigations/{inv_id}/assistant/proposals/{pid}")
    async def assistant_proposal(inv_id: str, pid: str, body: ProposalDecision) -> dict:
        """Apply or cancel something the assistant proposed (a catalog change, a large action, a deletion)."""
        try:
            out = await AC.decide(eng(), state.settings, state.db, _id(inv_id, INV_ID), _id(pid, re.compile(r"^[a-f0-9]{10}$")),
                                  body.decision, state.catalog.snapshot().platform_objects)
        except KeyError as exc:
            raise HTTPException(404, str(exc).strip("'\"")) from None
        except ValueError as exc:
            raise HTTPException(409, str(exc)) from None
        CM._ROWS_CACHE.clear()
        return out

    # ------------------------------------------------------------------ notes, identifiers, discovery, costs, budget
    @app.get("/api/investigations/{inv_id}/notes")
    def notes(inv_id: str) -> list[dict]:
        eng().runtime(_id(inv_id, INV_ID))
        return state.db.notes(inv_id)

    @app.post("/api/investigations/{inv_id}/notes", status_code=201)
    def add_note(inv_id: str, body: NoteInput) -> dict:
        eng().runtime(_id(inv_id, INV_ID))
        import uuid as _uuid
        return state.db.note_put(inv_id, _uuid.uuid4().hex[:10], {**body.model_dump(), "by": "investigator"})

    @app.put("/api/investigations/{inv_id}/notes/{note_id}")
    def update_note(inv_id: str, note_id: str, body: NoteInput) -> dict:
        eng().runtime(_id(inv_id, INV_ID))
        if not any(n["id"] == note_id for n in state.db.notes(inv_id)):
            raise HTTPException(404, "Unknown note")
        return state.db.note_put(inv_id, note_id, {**body.model_dump(), "by": "investigator"})

    @app.delete("/api/investigations/{inv_id}/notes/{note_id}")
    def delete_note(inv_id: str, note_id: str) -> dict:
        eng().runtime(_id(inv_id, INV_ID))
        if not state.db.note_delete(inv_id, note_id):
            raise HTTPException(404, "Unknown note")
        return {"ok": True}

    @app.get("/api/investigations/{inv_id}/identifiers")
    def identifiers(inv_id: str) -> dict:
        from ..assistant import identifiers as IDS
        rt = eng().runtime(_id(inv_id, INV_ID))
        items = IDS.rows(rt)
        return {"items": items, "counts": IDS.counts(items), "states": IDS.STATES}

    @app.get("/api/investigations/{inv_id}/discovery")
    def discovery(inv_id: str) -> dict:
        from ..discovery import stages as STAGES
        rt = eng().runtime(_id(inv_id, INV_ID))
        return STAGES.report(rt, state.settings, AA.identities(rt.graph))

    @app.post("/api/investigations/{inv_id}/discover-profiles")
    async def discover_profiles(inv_id: str, body: DiscoverProfilesInput) -> dict:
        """Web profile discovery for the target or a handle, optionally focused on some platforms."""
        rt = eng().runtime(_id(inv_id, INV_ID))
        primary = eng().search_primary(rt)
        if not primary:
            from ..discovery import router as ROUTER
            raise HTTPException(400, ROUTER.unavailable_message(state.settings) or "No web-search provider is available")
        e = rt.graph.by_id(_id(body.entity_id)) if body.entity_id else rt.graph.target
        if e is None or e.type not in ("person", "username"):
            raise HTTPException(400, "Choose the target person or a username")
        if body.platforms:
            from ..assistant.copilot import _platform_ids
            rt.info.options.setdefault("discovery_focus", {})[e.key] = _platform_ids(
                body.platforms, state.catalog.snapshot().platform_objects)
        return await eng().run_transforms(inv_id, e.id, [f"{primary}.profile_discovery"],
                                          expand_levels=0 if body.platforms else 1)

    @app.get("/api/investigations/{inv_id}/costs")
    def costs(inv_id: str) -> dict:
        from ..costs import ledger as LEDGER
        rt = eng().runtime(_id(inv_id, INV_ID))
        return LEDGER.investigation_costs(rt, state.db, state.settings, eng())

    @app.put("/api/investigations/{inv_id}/budget")
    async def set_budget(inv_id: str, body: BudgetUpdate) -> dict:
        rt = eng().runtime(_id(inv_id, INV_ID))
        rt.options["budget_usd"] = body.budget_usd
        rt.options["confirm_above_usd"] = body.confirm_above_usd
        rt.options.pop("confirmed_above", None)
        state.db.log_decision(inv_id, "budget", {"budget_usd": body.budget_usd, "confirm_above_usd": body.confirm_above_usd})
        state.db.update_investigation(inv_id, data=rt.data)
        return {"budget_usd": rt.options["budget_usd"], "confirm_above_usd": rt.options["confirm_above_usd"]}

    @app.post("/api/investigations/{inv_id}/budget")
    async def budget_decision(inv_id: str, body: BudgetDecision) -> dict:
        """Continue (raise the limit and run the held work) or Stop (keep the limit) after a budget hold."""
        out = await eng().resolve_budget_hold(_id(inv_id, INV_ID), proceed=body.decision == "continue",
                                              new_budget=body.budget_usd)
        state.db.log_decision(inv_id, f"budget_{body.decision}", {"budget_usd": body.budget_usd})
        return out

    @app.post("/api/investigations/{inv_id}/fallback")
    async def try_other_source(inv_id: str, body: FallbackInput) -> dict:
        """"Try another configured source" for a lookup a source refused or asked to verify."""
        try:
            return await eng().run_fallback(_id(inv_id, INV_ID), body.connector, _id(body.entity_id))
        except KeyError:
            raise HTTPException(404, "Unknown source or entity") from None

    @app.get("/api/assistant/provider")
    def assistant_provider() -> dict:
        return AIP.config(state.settings)

    @app.put("/api/assistant/key")
    def assistant_key(body: AIKeyInput) -> dict:
        try:
            state.settings.set_credential(AIP.AI_KEY, body.value)
        except ValueError as exc:
            raise HTTPException(400, str(exc)) from None
        return AIP.config(state.settings)

    @app.post("/api/assistant/test")
    async def assistant_test() -> dict:
        """One small request to the configured provider (no investigation data)."""
        try:
            provider = AIP.make(state.settings)
            reply = await provider.complete("Reply with the single word: ready.",
                                            [provider.user_message("Connection test.")], [])
        except AIP.ProviderError as exc:
            return {"ok": False, "message": str(exc)}
        return {"ok": reply.stop != "refusal", "message": (reply.text or reply.stop)[:200], "model": provider.model,
                "destination": provider.destination}

    # ------------------------------------------------------------------ AI providers (one abstraction, several accounts)
    @app.get("/api/ai/providers")
    def ai_providers() -> dict:
        return {"providers": AIP.providers(state.settings), "primary": AIP.primary_id(state.settings),
                "failover": AIP.failover_order(state.settings), "config": AIP.config(state.settings)}

    @app.put("/api/ai/providers/{pid}")
    def ai_provider_update(pid: str, body: AIProfileInput) -> dict:
        if pid not in AIP.PROVIDERS:
            raise HTTPException(404, "Unknown provider")
        data = body.model_dump(exclude={"key", "clear_key", "make_primary"}, exclude_none=True)
        cur = {k: v for k, v in AIP._stored_profiles(state.settings).get(pid, {}).items()}
        try:
            AIP.save_profile(state.settings, pid, {**cur, **data})
            if body.key:
                state.settings.set_credential(AIP.PROVIDERS[pid]["key"], body.key)
            elif body.clear_key:
                state.settings.set_credential(AIP.PROVIDERS[pid]["key"], None)
            if body.make_primary:
                state.settings.set("ai_provider", pid)
        except ValueError as exc:
            raise HTTPException(400, str(exc)) from None
        return ai_providers()

    @app.put("/api/ai/failover")
    def ai_failover(body: FailoverInput) -> dict:
        order = [p for p in body.order if p in AIP.PROVIDERS]
        state.db.set_setting(AIP.FAILOVER_KEY, order or None)
        return ai_providers()

    @app.post("/api/ai/providers/{pid}/test")
    async def ai_provider_test(pid: str) -> dict:
        """One tiny request (no investigation data) to check the provider, model and key; its usage is recorded."""
        if pid not in AIP.PROVIDERS:
            raise HTTPException(404, "Unknown provider")
        try:
            provider = AIP.build(state.settings, pid)
            reply = await provider.complete("Reply with the single word: ready.", [provider.user_message("Connection test.")], [])
        except AIP.ProviderError as exc:
            return {"ok": False, "message": str(exc)}
        from ..assistant.copilot import _record_usage
        usage = _record_usage(state.db, state.settings, None, provider, reply)
        return {"ok": reply.stop != "refusal", "message": (reply.text or reply.stop)[:200], "model": reply.model or provider.model,
                "destination": provider.destination, "usage": usage}

    @app.post("/api/ai/providers/{pid}/models")
    async def ai_provider_models(pid: str) -> dict:
        """The models the provider offers now; for OpenRouter their published prices are stored in the pricing table
        (origin: provider API, dated)."""
        if pid not in AIP.PROVIDERS or AIP.PROVIDERS[pid]["kind"] != "openai":
            raise HTTPException(400, "Model listing is available for OpenAI-compatible providers")
        p = AIP.profile(state.settings, pid)
        key = state.settings.credential(AIP.PROVIDERS[pid]["key"]) or (state.settings.credential(AIP.AI_KEY) if p["primary"] else None)
        provider = AIP.OpenAICompatibleProvider(key, p["model"] or "-", p["base_url"], provider_id=pid)
        try:
            models = await provider.list_models()
        except AIP.ProviderError as exc:
            raise HTTPException(502, str(exc)) from None
        stored = 0
        if pid == "openrouter":
            from ..costs import pricing as PR
            stored = PR.store_provider_prices(state.settings, "openrouter", models, utcnow(), f"{p['base_url']}/models")
        state.db.set_setting(f"ai_models:{pid}", {"at": utcnow(), "models": models[:2000]})
        return {"models": models[:2000], "count": len(models), "prices_stored": stored, "at": utcnow()}

    @app.get("/api/ai/providers/{pid}/models")
    def ai_provider_models_cached(pid: str) -> dict:
        return state.db.get_setting(f"ai_models:{pid}") or {"at": None, "models": []}

    # ------------------------------------------------------------------ pricing, usage, search providers
    @app.get("/api/pricing")
    def pricing_table() -> dict:
        from ..costs import ledger as LEDGER
        from ..costs import pricing as PR
        return {"prices": PR.table(state.settings), "money": LEDGER.money(state.settings),
                "note": "Prices are used for estimates only; your provider's invoice is authoritative. Unknown prices stay "
                        "unknown (costs show as Unknown)."}

    @app.put("/api/pricing")
    def pricing_put(body: PriceInput) -> dict:
        from ..costs import pricing as PR
        try:
            return PR.put_override(state.settings, body.model_dump())
        except ValueError as exc:
            raise HTTPException(400, str(exc)) from None

    @app.delete("/api/pricing")
    def pricing_delete(key: str) -> dict:
        from ..costs import pricing as PR
        if not PR.delete_override(state.settings, key):
            raise HTTPException(404, "No override with this key (shipped prices cannot be deleted, only overridden)")
        return {"ok": True}

    @app.get("/api/usage")
    def usage_summary() -> dict:
        """All recorded billable usage, per provider (AI requests, catalog discovery searches, tests)."""
        rows = state.db.usage_list(all_records=True, limit=5000)
        by: dict[str, dict] = {}
        for u in rows:
            d = by.setdefault(u["provider"], {"provider": u["provider"], "requests": 0, "tokens": 0, "estimated": 0.0,
                                               "actual": None, "unknown": 0, "services": [], "models": []})
            d["requests"] += u["requests"]
            d["tokens"] += u["total_tokens"]
            d["estimated"] += u["estimated_cost"] or 0.0
            if u["actual_cost"] is not None:
                d["actual"] = (d["actual"] or 0.0) + u["actual_cost"]
            if u["estimated_cost"] is None and u["actual_cost"] is None:
                d["unknown"] += 1
            for k, v in (("services", u["service"]), ("models", u["model"])):
                if v and v not in d[k]:
                    d[k].append(v)
        return {"providers": sorted(by.values(), key=lambda d: -d["estimated"]), "records": rows[:200]}

    @app.get("/api/search-providers")
    def search_providers() -> dict:
        from ..discovery import router as ROUTER
        return {"providers": ROUTER.ordered(state.settings), "mode": state.settings.get("search_provider_mode") or "primary",
                "unavailable_message": ROUTER.unavailable_message(state.settings)}

    @app.post("/api/sources/{name}/test")
    async def source_test(name: str) -> dict:
        """"Test connection": the connector's health probe (a public test input) with your configured credentials."""
        import tomllib
        from ..registry.health import DATA as HEALTH_DIR
        from ..sdk.testing import arun_transform, view
        from ..sdk.transform import InvestigationInfo
        if name not in REG.REGISTRY:
            raise HTTPException(404, "Unknown source")
        with open(HEALTH_DIR / "probes.toml", "rb") as fh:
            spec = tomllib.load(fh).get("probe", {}).get(name)
        if not spec:
            return {"ok": None, "status": "no_probe", "message": "No test input is defined for this source."}
        status, _note = REG.connector_status(REG.REGISTRY[name], state.settings)
        if status in ("needs_credentials", "needs_subscription"):
            return {"ok": False, "status": status, "message": "Add the API key / subscription credentials first."}
        typ, value, *label = spec["entity"]
        http = eng().http_factory()
        try:
            out, run_status, message = await arun_transform(
                spec["transform"], view(typ, value, label=label[0] if label else None, **(spec.get("props") or {})),
                http=http, info=InvestigationInfo(), credentials=state.settings.credential, data_dir=state.data_dir)
        finally:
            await http.aclose()
        ok = run_status in ("found", "empty", "not_found")
        state.db.record_health([(name, run_status, utcnow())])
        return {"ok": ok, "status": run_status, "message": message[:300] or ("The source answered correctly" if ok else ""),
                "test_input": f"{typ} {value}", "entities": len(out.entities)}

    @app.get("/api/investigations/{inv_id}/coverage")
    def coverage(inv_id: str) -> dict:
        return eng().coverage(_id(inv_id, INV_ID))

    @app.get("/api/investigations/{inv_id}/accounts")
    def accounts(inv_id: str) -> dict:
        return views.accounts_view(eng().runtime(_id(inv_id, INV_ID)).graph, state.catalog.snapshot().platform_objects)

    @app.get("/api/investigations/{inv_id}/records")
    def records(inv_id: str) -> dict:
        rt = eng().runtime(_id(inv_id, INV_ID))
        return {"records": views.records_view(rt.graph), "states": views.RECORD_STATES,
                "jurisdictions": JUR.payload(list(rt.info.countries))["relevant"]}

    @app.get("/api/investigations/{inv_id}/raw/{digest}")
    def raw_evidence(inv_id: str, digest: str) -> dict:
        inv_id = _id(inv_id, INV_ID)
        if not re.fullmatch(r"[a-f0-9]{64}", digest):
            raise HTTPException(400, "Invalid digest")
        raw = state.db.get_raw(inv_id, digest)
        if raw is None:
            raise HTTPException(404, "No raw evidence stored with this digest")
        import base64
        body = base64.b64decode(raw.pop("body") or "")
        ctype = str(raw.get("content_type") or "")
        textual = any(t in ctype for t in ("json", "text", "xml", "html", "csv")) or not ctype
        return {**raw, "sha256": digest, "text": body[:1_000_000].decode("utf-8", "replace") if textual else None,
                "binary_size": None if textual else len(body)}

    @app.get("/api/investigations/{inv_id}/timeline")
    def timeline(inv_id: str, min_confidence: float = 0.4) -> list[dict]:
        return views.timeline_view(eng().runtime(_id(inv_id, INV_ID)).graph, min_confidence)

    @app.get("/api/investigations/{inv_id}/decisions")
    def decisions(inv_id: str) -> list[dict]:
        return state.db.decisions(_id(inv_id, INV_ID))

    @app.get("/api/investigations/{inv_id}/export/{fmt}")
    def export(inv_id: str, fmt: Literal["json", "html", "csv"]) -> Response:
        inv_id = _id(inv_id, INV_ID)
        rt = eng().runtime(inv_id)
        from ..assistant import identifiers as IDS
        from ..costs import ledger as LEDGER
        from ..discovery import stages as STAGES
        disc = STAGES.report(rt, state.settings, AA.identities(rt.graph))
        extras = {"identity_discovery": {**disc, "journal": disc["journal"][-200:]}, "discovery": disc,
                  "identifiers": IDS.rows(rt), "costs": LEDGER.investigation_costs(rt, state.db, state.settings, eng()),
                  "notes": state.db.notes(inv_id)}
        if fmt == "json":
            extras.pop("discovery")
        content, media = build_report(rt, eng().coverage(inv_id), state.db.decisions(inv_id), fmt, extras)
        name = f"sextant-{inv_id}.{fmt}"
        return Response(content, media_type=media, headers={"Content-Disposition": f'attachment; filename="{name}"'})

    # ------------------------------------------------------------------ catalog, platforms, jurisdictions
    @app.get("/api/sources")
    def sources(access_class: str = "") -> dict:
        """Data Sources: the built-in connectors and the investigator's own sources, grouped by category.
        ``access_class`` filters (FREE_PUBLIC, FREE_API, FREE_TIER, PAID_API, SUBSCRIPTION, ENTERPRISE, MANUAL,
        REFERENCE_ONLY, UNAVAILABLE, NOT_VERIFIED)."""
        from ..registry.access_class import ACCESS_CLASSES
        records, snap = state.db.health_records(), health_snapshot()
        items = [{**REG.describe(c, state.settings), "origin": "system",
                  "health": connector_health(n, state.settings, records.get(n), snap)} for n, c in REG.REGISTRY.items()]
        states = {d["name"]: d["status"] for d in items}
        sn = state.catalog.snapshot()
        items += [CAT.source_item(s, sn, states, state.settings, records, snap) for s in sn.sources]
        class_counts: dict[str, int] = {}
        for d in items:
            for c in d.get("access_classes") or []:
                class_counts[c] = class_counts.get(c, 0) + 1
        if access_class:
            items = [d for d in items if access_class.upper() in (d.get("access_classes") or [])]
        groups: dict[str, list[dict]] = {}
        for d in sorted(items, key=lambda d: (d["category_title"].lower(), d["title"].lower())):
            groups.setdefault(d["category"], []).append(d)
        counts: dict[str, int] = {}
        for d in items:
            counts[d["status"]] = counts.get(d["status"], 0) + 1
        return {"groups": [{"category": k, "title": v[0]["category_title"], "group": v[0]["progress_group"], "sources": v}
                           for k, v in groups.items()], "counts": counts, "total": len(items),
                "origins": {"system": len(REG.REGISTRY), "user": len(sn.sources)}, "catalog_version": sn.version,
                "access_classes": ACCESS_CLASSES, "access_class_counts": class_counts}

    @app.put("/api/sources/{name}")
    def source_prefs(name: str, body: SourcePrefsInput) -> dict:
        if name not in REG.REGISTRY:
            raise HTTPException(404, "Unknown source (your own sources are changed under /api/catalog/sources)")
        if body.reset:
            state.settings.set_connector_pref(name, clear=True)
        else:
            extra = None if body.manual_only is None else {"manual_only": body.manual_only}
            state.settings.set_connector_pref(name, enabled=body.enabled, auto_run=body.auto_run, extra=extra)
        return REG.describe(REG.REGISTRY[name], state.settings)

    @app.put("/api/sources/{name}/credentials")
    def source_credentials(name: str, body: CredentialsInput) -> dict:
        conn = REG.REGISTRY.get(name)
        if conn is None:
            raise HTTPException(404, "Unknown source")
        allowed = {c.key for c in conn.manifest.credentials}
        for key, value in body.values.items():
            if key not in allowed:
                raise HTTPException(400, f"{key} is not a credential of this source")
            try:
                state.settings.set_credential(key, value)
            except ValueError as exc:
                raise HTTPException(400, str(exc)) from None
        return REG.describe(conn, state.settings)

    @app.get("/api/coverage-matrix")
    def coverage_matrix() -> dict:
        """Derived, never stored: one row per connector and per source of the investigator, from the same records
        as Data Sources."""
        rows = []
        for c in REG.REGISTRY.values():
            d = REG.describe(c, state.settings)
            rows.append({"name": d["name"], "title": d["title"], "category": d["category"], "category_title": d["category_title"],
                         **d["matrix"], "status": d["status"], "status_title": d["status_title"], "access": d["access"],
                         "inputs": d["input_entities"], "jurisdictions": d["jurisdictions"], "origin": "system",
                         "automation": "connector" if d["status"] not in ("manual", "catalog_only", "retired", "excluded")
                         else "manual" if d["status"] == "manual" else "none"})
        states = {r["name"]: r["status"] for r in rows}
        sn = state.catalog.snapshot()
        cats = CAT.category_vocab(sn)
        for s in sn.sources:
            st, _ = CAT.status(s, states)
            rows.append({"name": s["id"], "title": s["title"], "category": s["category"],
                         "category_title": cats.get(s["category"], {}).get("title", s["category"]), **CAT.matrix_flags(s),
                         "status": st, "status_title": REG.STATUS_TITLES.get(st, st), "access": s["access"],
                         "inputs": s.get("inputs") or [], "jurisdictions": s["jurisdictions"] or ["GLOBAL"],
                         "origin": "user", "automation": CAT.automation(s, states)["mode"]})
        rows.sort(key=lambda r: (r["category_title"].lower(), r["title"].lower()))
        return {"rows": rows, "columns": ["free", "api", "paid", "public"], "disclaimer": DISCLAIMER,
                "origins": {"system": len(REG.REGISTRY), "user": len(sn.sources)}}

    @app.get("/api/platforms")
    def platforms(q: str = "", category: str = "", source: str = "", status: str = "", country: str = "",
                  offset: int = 0, limit: int = 200) -> dict:
        sn = state.catalog.snapshot()
        ctx = CM.CoverageContext(state.settings, catalog=sn)
        cat = PLAT.catalog(ctx.states, ctx.adapters, extra=ctx.user_platforms)
        linked: dict[str, list[dict]] = {}
        for s in sn.sources:
            for pid in s["platforms"]:
                linked.setdefault(pid, []).append({"id": s["id"], "title": s["title"]})
        recognizable = {p.id: bool(p._compiled or p._query_rules) for p in ctx.user_platforms}
        for row in cat["curated"]:
            row["origin"] = "user" if row["source"] == "user" else "system"
            if row["origin"] == "user":
                row["record"] = sn.platform_by_id.get(row["id"])
                row["recognizable"] = recognizable.get(row["id"], False)
            if row["id"] in linked:
                row["user_sources"] = linked[row["id"]]
        ql, cc = q.strip().lower(), country.strip().upper()

        def keep(p: dict) -> bool:
            return ((not ql or ql in f"{p['name']} {p['url']}".lower()) and (not category or p["category"] == category)
                    and (not source or p.get("source") == source) and (not status or p["search_status"] == status)
                    and (not cc or cc in (p.get("countries") or [])))
        rows = [p for p in cat["curated"] + cat["imported"] if keep(p)]
        rows.sort(key=lambda p: ({"user": 0, "curated": 1}.get(p.get("source"), 2), p["name"].lower()))
        limit = max(1, min(limit, 1000))
        return {"rows": rows[offset:offset + limit], "total": len(rows), "offset": offset, "limit": limit,
                "counts": cat["counts"], "categories": cat["categories"], "search_status_titles": cat["search_status_titles"],
                "provenance": cat["provenance"]}

    # ------------------------------------------------------------------ global coverage
    def _cov_ctx() -> CM.CoverageContext:
        return CM.CoverageContext(state.settings, state.db.health_records(), state.catalog.snapshot())

    @app.get("/api/global-coverage")
    def global_coverage(territories: bool = True) -> dict:
        return CM.matrix(_cov_ctx(), include_territories=territories)

    @app.get("/api/countries")
    def countries_list() -> list[dict]:
        return [{"code": c.code, "name": c.name, "region": c.region, "sovereign": c.sovereign}
                for c in sorted(COUNTRIES.countries().values(), key=lambda c: c.name)]

    @app.get("/api/coverage-summary")
    def coverage_summary() -> dict:
        return CM.summary(_cov_ctx())

    @app.get("/api/global-coverage/{code}")
    def country_coverage(code: str) -> dict:
        code = _id(code.upper(), CC_RE)
        if COUNTRIES.get(code) is None and code not in JUR.jurisdictions():
            raise HTTPException(404, "Unknown country")
        d = CM.country_detail(code, _cov_ctx())
        d["live_discovery"] = state.db.get_setting(f"discovery:{code}")
        d["kinds"], d["access"] = JUR.SOURCE_KINDS, JUR.ACCESS
        return d

    @app.post("/api/global-coverage/{code}/discover")
    async def discover_sources(code: str) -> dict:
        from ..registry.discovery_live import discover
        code = _id(code.upper(), CC_RE)
        if COUNTRIES.get(code) is None:
            raise HTTPException(404, "Unknown country")
        http = state.engine.http_factory()
        try:
            result = await discover(code, http)
        finally:
            await http.aclose()
        state.db.set_setting(f"discovery:{code}", result)
        return result

    # ------------------------------------------------------------------ catalog: the investigator's own records
    def _snap() -> CAT.Snapshot:
        return state.catalog.snapshot()

    def _states() -> dict[str, str]:
        return {n: REG.connector_status(c, state.settings)[0] for n, c in REG.REGISTRY.items()}

    def _duplicate(exc: CAT.DuplicateError) -> HTTPException:
        return HTTPException(409, {"message": "This may already exist", "duplicates": exc.matches})

    def _src_id(value: str) -> str:
        return _id(value, CAT.SOURCE_ID)

    @app.get("/api/catalog/vocab")
    def catalog_vocab() -> dict:
        return CAT.vocab(_snap())

    @app.get("/api/catalog/sources")
    def catalog_sources(jurisdiction: str = "") -> list[dict]:
        sn = _snap()
        items = sn.for_jurisdiction(jurisdiction.upper()) if jurisdiction else sn.sources
        return [CAT.public(s, sn) for s in items]

    @app.get("/api/catalog/sources/{sid}")
    def catalog_source(sid: str) -> dict:
        sn = _snap()
        s = sn.by_id.get(_src_id(sid))
        if s is None:
            raise HTTPException(404, "Unknown source")
        return CAT.source_item(s, sn, _states(), state.settings, state.db.health_records())

    @app.post("/api/catalog/sources", status_code=201)
    def catalog_create_source(body: CatalogSourceInput) -> dict:
        data = body.model_dump()
        try:
            s = state.catalog.create_source(data, allow_duplicate=data.pop("allow_duplicate"))
        except CAT.DuplicateError as exc:
            raise _duplicate(exc) from None
        except ValueError as exc:
            raise HTTPException(400, str(exc)) from None
        return CAT.public(s, _snap())

    @app.put("/api/catalog/sources/{sid}")
    def catalog_update_source(sid: str, body: CatalogSourceInput) -> dict:
        data = body.model_dump()
        try:
            s = state.catalog.update_source(_src_id(sid), data, allow_duplicate=data.pop("allow_duplicate"))
        except CAT.DuplicateError as exc:
            raise _duplicate(exc) from None
        except KeyError:
            raise HTTPException(404, "Unknown source (built-in sources cannot be edited)") from None
        except ValueError as exc:
            raise HTTPException(400, str(exc)) from None
        return CAT.public(s, _snap())

    @app.patch("/api/catalog/sources/{sid}")
    def catalog_patch_source(sid: str, body: CatalogSourcePatch) -> dict:
        try:
            s = state.catalog.patch_source(_src_id(sid), enabled=body.enabled, review=body.review)
        except KeyError:
            raise HTTPException(404, "Unknown source") from None
        except ValueError as exc:
            raise HTTPException(400, str(exc)) from None
        return CAT.public(s, _snap())

    @app.delete("/api/catalog/sources/{sid}")
    def catalog_delete_source(sid: str) -> dict:
        if not state.catalog.delete_source(_src_id(sid)):
            raise HTTPException(404, "Unknown source (built-in sources cannot be deleted; switch them off instead)")
        return {"ok": True}

    @app.post("/api/catalog/sources/{sid}/check-link")
    async def catalog_check_link(sid: str) -> dict:
        """One GET to the source's own address, on the investigator's request, through the guarded client (no private
        addresses, timeouts, size cap). Sends no personal data."""
        from ..registry.discovery_live import check_url
        s = _snap().by_id.get(_src_id(sid))
        if s is None:
            raise HTTPException(404, "Unknown source")
        http = state.engine.http_factory()
        try:
            result = await check_url(http, s["url"])
        finally:
            await http.aclose()
        return CAT.public(state.catalog.patch_source(s["id"], link_check=result), _snap())

    @app.post("/api/catalog/duplicates")
    def catalog_duplicates(body: DuplicateQuery) -> dict:
        return {"duplicates": CAT.find_duplicates(body.model_dump(), _snap(), exclude_id=body.exclude_id)}

    @app.get("/api/catalog/categories")
    def catalog_categories() -> list[dict]:
        return CAT.vocab(_snap())["categories"]

    @app.post("/api/catalog/categories", status_code=201)
    def catalog_create_category(body: CategoryInput) -> dict:
        try:
            return state.catalog.put_category(body.model_dump())
        except ValueError as exc:
            raise HTTPException(400, str(exc)) from None

    @app.put("/api/catalog/categories/{cid}")
    def catalog_update_category(cid: str, body: CategoryInput) -> dict:
        try:
            return state.catalog.put_category(body.model_dump(), _id(cid, CAT.CATEGORY_ID))
        except KeyError:
            raise HTTPException(404, "Unknown category (built-in categories cannot be edited)") from None
        except ValueError as exc:
            raise HTTPException(400, str(exc)) from None

    @app.delete("/api/catalog/categories/{cid}")
    def catalog_delete_category(cid: str) -> dict:
        try:
            used = state.catalog.delete_category(_id(cid, CAT.CATEGORY_ID))
        except KeyError:
            raise HTTPException(404, "Unknown category (built-in categories cannot be deleted)") from None
        if used:
            raise HTTPException(409, {"message": f"{len(used)} of your sources still use this category; move them first",
                                      "sources": used})
        return {"ok": True}

    @app.get("/api/catalog/platforms")
    def catalog_platforms() -> list[dict]:
        return _snap().platforms

    @app.post("/api/catalog/platforms", status_code=201)
    def catalog_create_platform(body: PlatformInput) -> dict:
        data = body.model_dump()
        try:
            return state.catalog.put_platform(data, allow_duplicate=data.pop("allow_duplicate"))
        except CAT.DuplicateError as exc:
            raise _duplicate(exc) from None
        except ValueError as exc:
            raise HTTPException(400, str(exc)) from None

    @app.put("/api/catalog/platforms/{pid}")
    def catalog_update_platform(pid: str, body: PlatformInput) -> dict:
        data = body.model_dump()
        try:
            return state.catalog.put_platform(data, _id(pid, CAT.PLATFORM_ID), allow_duplicate=data.pop("allow_duplicate"))
        except CAT.DuplicateError as exc:
            raise _duplicate(exc) from None
        except KeyError:
            raise HTTPException(404, "Unknown platform (built-in platforms cannot be edited)") from None
        except ValueError as exc:
            raise HTTPException(400, str(exc)) from None

    @app.delete("/api/catalog/platforms/{pid}")
    def catalog_delete_platform(pid: str) -> dict:
        if not state.catalog.delete_platform(_id(pid, CAT.PLATFORM_ID)):
            raise HTTPException(404, "Unknown platform (built-in platforms cannot be deleted)")
        return {"ok": True}

    @app.get("/api/catalog/audit/{code}")
    def catalog_audit(code: str) -> dict:
        """Read-only jurisdiction audit of the current catalog (see registry/catalog_audit.py)."""
        code = _id(code.upper(), CC_RE)
        if code not in JUR.jurisdictions():
            raise HTTPException(404, "Unknown jurisdiction")
        ctx = _cov_ctx()
        return AUDIT.audit(code, ctx, ctx.catalog, state.db.get_setting(f"discovery:{code}"))

    @app.post("/api/catalog/audit/{code}/ignore")
    def catalog_audit_ignore(code: str, body: IgnoreInput) -> dict:
        code = _id(code.upper(), CC_RE)
        if code not in JUR.jurisdictions():
            raise HTTPException(404, "Unknown jurisdiction")
        state.catalog.set_ignored(code, body.item, body.ignored)
        return {"ok": True}

    @app.post("/api/catalog/inspect")
    async def catalog_inspect(body: InspectInput) -> dict:
        """Reads one address and drafts a source definition (name, category, jurisdiction, nature, duplicates)."""
        from ..registry import source_discovery as SD
        http = state.engine.http_factory()
        try:
            return await SD.inspect_url(body.url, http, _snap(), jurisdiction=body.jurisdiction.upper(),
                                        category=body.category)
        except ValueError as exc:
            raise HTTPException(400, str(exc)) from None
        finally:
            await http.aclose()

    @app.post("/api/catalog/discover")
    async def catalog_discover(body: SourceDiscoveryInput) -> dict:
        """Data Sources → Discover sources: candidates from the configured web-search provider, nothing added."""
        from ..registry import source_discovery as SD
        http = state.engine.http_factory()
        try:
            return await SD.discover(request=body.request, jurisdiction=body.jurisdiction.upper(), category=body.category,
                                     cfg=state.settings, db=state.db, http=http, snap=_snap())
        finally:
            await http.aclose()

    @app.get("/api/catalog/discover")
    def catalog_discover_last() -> dict:
        from ..registry import source_discovery as SD
        return state.db.get_setting(SD.LAST_KEY) or {"candidates": []}

    @app.get("/api/catalog/reset")
    def catalog_reset_summary() -> dict:
        return CAT.reset_summary(state.db, _snap())

    @app.post("/api/catalog/reset")
    def catalog_reset(body: ResetInput) -> dict:
        """The one catalog reset: removes everything the investigator added or changed in the catalog."""
        if body.confirm != "RESET":
            raise HTTPException(400, "Type RESET to confirm")
        removed = state.db.catalog_reset()
        state.settings.invalidate_prefs()
        CM._ROWS_CACHE.clear()
        return {"ok": True, "removed": removed, "summary": CAT.reset_summary(state.db, _snap())}

    @app.get("/api/catalog/export")
    def catalog_export() -> Response:
        """Backup of the user layer (what the reset removes), as a JSON download. Never contains API keys."""
        doc = CAT.export_document(state.db, _snap())
        name = f"sextant-catalog-{doc['exported_at'][:10]}.json"
        return Response(json.dumps(doc, ensure_ascii=False, indent=2), media_type="application/json",
                        headers={"Content-Disposition": f'attachment; filename="{name}"'})

    @app.post("/api/catalog/import")
    def catalog_import(body: ImportInput) -> dict:
        """Previews (dry run, the default) or applies an exported catalog, in one transaction."""
        try:
            plan, report = CAT.plan_import(body.document, _snap(), state.db.connector_prefs(), overwrite=body.overwrite,
                                           restore_preferences=body.restore_preferences)
        except ValueError as exc:
            raise HTTPException(400, str(exc)) from None
        applied = not body.dry_run and report["changes"] > 0
        if applied:
            CAT.apply_import(state.db, plan)
            state.settings.invalidate_prefs()
            state.catalog.snapshot()              # refresh at once (investigations read a cached copy)
        return {**report, "dry_run": body.dry_run, "applied": applied}

    # former country-page endpoints, kept for compatibility: same records as /api/catalog/sources
    @app.get("/api/custom-sources")
    def custom_sources() -> list[dict]:
        sn = _snap()
        return [CAT.public(s, sn) for s in sn.sources]

    @app.post("/api/custom-sources", status_code=201)
    def add_custom_source(body: CustomSourceInput) -> dict:
        code = body.country.upper()
        if COUNTRIES.get(code) is None and code not in JUR.PSEUDO:
            raise HTTPException(400, "Unknown country")
        if body.kind not in JUR.SOURCE_KINDS or body.access not in JUR.ACCESS:
            raise HTTPException(400, "Unknown kind or access")
        try:
            s = state.catalog.create_source({"title": body.title, "url": body.url, "kind": body.kind, "access": body.access,
                                             "category": CAT.category_for_kind(body.kind), "name_search": body.name_search,
                                             "notes": body.notes, "jurisdictions": [code], "source_type": "reference"})
        except CAT.DuplicateError as exc:
            raise _duplicate(exc) from None
        except ValueError as exc:
            raise HTTPException(400, str(exc)) from None
        return {**CAT.public(s, _snap()), "country": code}

    @app.delete("/api/custom-sources/{source_id}")
    def delete_custom_source(source_id: str) -> dict:
        if not state.catalog.delete_source(_src_id(source_id)):
            raise HTTPException(404, "Unknown custom source")
        return {"ok": True}

    @app.get("/api/jurisdictions")
    def jurisdictions(countries: str = "") -> dict:
        states = {n: REG.connector_status(c, state.settings)[0] for n, c in REG.REGISTRY.items()}
        return CAT.jurisdictions_payload(state.catalog.snapshot(), states,
                                         [c.strip().upper() for c in countries.split(",") if c.strip()][:10])

    # ------------------------------------------------------------------ settings & data
    @app.get("/api/settings")
    def get_settings() -> dict:
        return {"settings": state.settings.describe(), "data_dir": str(state.data_dir), "encryption": state.db.vault.mode}

    @app.put("/api/settings")
    def put_settings(body: SettingsInput) -> dict:
        for key, value in body.values.items():
            if key not in SETTINGS_BY_KEY:
                raise HTTPException(400, f"Unknown setting {key}")
            try:
                state.settings.set(key, value)
            except ValueError as exc:
                raise HTTPException(400, str(exc)) from None
        return get_settings()

    AI_SETTING_KEYS = ["ai_provider", "ai_model", "ai_base_url", "ai_effort", "ai_auto_apply_catalog",
                       "ai_action_preview_usd", AIP.PROFILES_KEY, AIP.FAILOVER_KEY, "pricing_overrides", "provider_prices"]

    def _reset_summary() -> dict:
        creds = sorted(state.db.credential_keys())
        return {"scopes": {
            "catalog": {"title": "Reset catalog", "removes": CAT.reset_summary(state.db, _snap())["remove"],
                        "also": "Saved source-discovery results.",
                        "keeps": "Investigations, API keys, AI settings, prices."},
            "history": {"title": "Reset investigation history", "removes": {"investigations": len(state.db.list_investigations())},
                        "also": "Their graphs, assistant conversations, proposals, notes, caches, raw evidence and usage "
                                "records.", "keeps": "Catalog, API keys, settings."},
            "ai_settings": {"title": "Reset AI settings", "removes": {"settings": sum(1 for k in AI_SETTING_KEYS if
                                                                                      state.db.get_setting(k) is not None)},
                            "also": "AI providers, failover order, models, auto-apply and preview settings, your price "
                                    "overrides and prices read from providers.",
                            "keeps": "API keys (separate), investigations, catalog."},
            "credentials": {"title": "Reset API credentials", "removes": {"keys": len(creds)}, "names": creds,
                            "also": "Keys set in the environment / .env file are not stored here and stay.",
                            "keeps": "Everything else."}},
            "confirm": "RESET"}

    @app.get("/api/reset")
    def reset_summary() -> dict:
        return _reset_summary()

    @app.post("/api/reset")
    async def scoped_reset(body: ScopedResetInput) -> dict:
        """The Hard Reset, scoped: each scope is a separate, explicit choice. Nothing outside the chosen scopes changes."""
        if body.confirm != "RESET":
            raise HTTPException(400, "Type RESET to confirm")
        removed: dict = {}
        if "history" in body.scopes:
            for inv_id in list(state.engine.active):
                await state.engine.stop(inv_id)
            state.engine.active.clear()
            removed["history"] = {"investigations": state.db.reset_history()}
        if "catalog" in body.scopes:
            removed["catalog"] = state.db.catalog_reset()
            removed["catalog"]["discovery"] = state.db.reset_settings([], ("catalog_discovery:",))
            state.settings.invalidate_prefs()
            CM._ROWS_CACHE.clear()
        if "ai_settings" in body.scopes:
            removed["ai_settings"] = {"settings": state.db.reset_settings(AI_SETTING_KEYS, ("ai_models:",))}
        if "credentials" in body.scopes:
            removed["credentials"] = {"keys": state.db.reset_credentials()}
        for scope in body.scopes:
            log.info("Reset scope %s", scope)
        return {"ok": True, "removed": removed, "summary": _reset_summary()}

    @app.post("/api/data/wipe")
    async def wipe(body: WipeInput) -> dict:
        if body.confirm != "DELETE EVERYTHING":
            raise HTTPException(400, "Type DELETE EVERYTHING to confirm")
        for inv_id in list(state.engine.active):
            await state.engine.stop(inv_id)
        state.db.wipe_all()
        state.open()
        return {"ok": True}

    # ------------------------------------------------------------------ static UI
    index = state.frontend_dir / "index.html"

    @app.get("/", include_in_schema=False)
    def root():
        if not index.exists():
            return JSONResponse({"detail": "UI not built — run `npm install --no-bin-links && npm run build` in frontend/"},
                                status_code=503)
        return FileResponse(index, headers={"Cache-Control": "no-store"})

    @app.get("/cytoscape.css", include_in_schema=False)
    def cytoscape_css():
        return FileResponse(state.frontend_dir / "cytoscape.css", media_type="text/css")

    # the generic lifecycle route (POST /api/investigations/{id}/{action}) must be matched after every specific POST
    # route under an investigation (budget, notes, discover-profiles…), wherever they were declared
    generic = [r for r in app.router.routes if getattr(r, "path", "") == "/api/investigations/{inv_id}/{action}"]
    for r in generic:
        app.router.routes.remove(r)
        app.router.routes.append(r)

    assets = state.frontend_dir / "assets"
    if assets.is_dir():
        app.mount("/assets", StaticFiles(directory=assets), name="assets")

    return app


def build_state(data_dir: Path, frontend_dir: Path, token: str, port: int, http_factory=None) -> AppState:
    hosts = {f"127.0.0.1:{port}", f"localhost:{port}"}
    return AppState(data_dir=data_dir, frontend_dir=frontend_dir, token=token, allowed_hosts=hosts,
                    passphrase=os.environ.get("SEXTANT_PASSPHRASE") or None, http_factory=http_factory)


__all__ = ["AppState", "build_state", "create_app"]
