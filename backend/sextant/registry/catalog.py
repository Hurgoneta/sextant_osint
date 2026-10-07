"""The catalog's user layer — sources, links, categories and platforms the investigator adds in the UI.

Two layers, one catalog:

* **system** — everything shipped with this version: connectors (``registry/connectors.py``), official sources per
  jurisdiction (``data/jurisdictions/*.toml``), platforms (``data/platforms``), the category vocabularies. Read-only,
  never copied into the database, so an update that adds defaults shows them at once and nothing can overwrite them.
* **user** — records the investigator created, stored in the ``catalog_*`` tables (``storage/db.py``). One record
  per source; its jurisdictions and platforms are relationships, not copies.

Every screen (Data Sources, Coverage matrix, Jurisdictions, country pages, Global coverage, Platforms, the audit) reads
both layers through this module, and every item carries ``origin: "system" | "user"``. Counts are computed, never
stored, so all screens agree.

A user source is **metadata, never code**. It cannot carry commands, scripts, queries or request templates, and
Sextant never contacts it on its own. It may *reference* an existing connector ("this register is automated by
connector X"); then the connector, with its manifest, disclosure and rate limits, does the work. Its status says what
it really is: Manual only, Disabled, Excluded, or the linked connector's status. Health stays "Unknown (not yet
verified)" until the investigator runs a link check.

The catalog reset (``reset``) deletes the user layer and the investigator's on/off overrides of built-in sources.
"""

from __future__ import annotations

import hashlib
import json
import re
import secrets
import time
import unicodedata
import weakref
from dataclasses import dataclass, field
from functools import lru_cache
from urllib.parse import urlsplit

from ..ontology import ENTITY_TYPES
from ..sdk.manifest import CATEGORIES
from . import countries as C
from . import jurisdictions as J
from . import access_class as ACX
from . import platforms as P

# --------------------------------------------------------------------------- vocabularies

# What a user source technically is. None of them is an automated connector: automation only comes from linking a
# connector that exists in this installation.
SOURCE_TYPES: dict[str, dict] = {
    "reference": {"title": "Reference link", "automation": "none",
                  "help": "A useful website to know about. Nothing is automated; it is a link you open."},
    "manual": {"title": "Manual source", "automation": "manual",
               "help": "A website or register you search yourself. Sextant does not query it."},
    "official_api": {"title": "Official API (no connector)", "automation": "none",
                     "help": "The provider documents an API, but no connector for it is installed, so Sextant cannot query "
                             "it. Saving the API address does not make it automated."},
    "public_api": {"title": "Public API (no connector)", "automation": "none",
                   "help": "A documented public endpoint exists, but no connector for it is installed, so Sextant cannot "
                           "query it."},
    "dataset": {"title": "Dataset / bulk download", "automation": "manual",
                "help": "Data you download and search yourself."},
    "local": {"title": "Local / offline source", "automation": "manual",
              "help": "A file, archive or tool on your own machine or network. Sextant does not read it."},
}
AUTOMATION_TITLES = {
    "reference": "None: reference link",
    "manual": "Manual: you search it yourself",
    "official_api": "Not automated: no connector for this API is installed",
    "public_api": "Not automated: no connector for this API is installed",
    "dataset": "Manual: download and search it yourself",
    "local": "Manual: local/offline, not read by Sextant",
}
# Who provides the data and on what terms. A commercial provider is never queried because it is listed: only a
# connector configured with the investigator's own licence can run, and until then it shows "Contact provider".
PROVIDER_CLASSES = {
    "public": "Public / free source",
    "commercial": "Licensed commercial provider",
    "private": "Your own private source (internal, subscription you manage)",
    "manual": "Manual-only source",
}
REVIEW = {
    "not_verified": "Not verified",
    "verified": "Verified by you",
    "unavailable": "Marked unavailable",
    "excluded": "Excluded (your policy decision)",
}
ACCESS = J.ACCESS                                      # free_web, free_api, free_download, registration, paid, …
FREE_ACCESS = ("free_web", "free_api", "free_download")

# Data Sources category → group of the jurisdiction views (so a user source sits next to official sources of the
# same kind). Categories without an official-source equivalent go to "other".
CATEGORY_GROUP = {
    "search": "web", "social": "social", "usernames": "social", "forums": "social", "developer_platforms": "social",
    "gaming": "social", "music": "social", "video": "social", "blogs": "social", "marketplaces": "social",
    "professional_networks": "professional", "email": "other", "phone": "other", "breach_metadata": "other",
    "domains": "domains", "dns": "domains", "rdap": "domains", "certificates": "domains",
    "infrastructure": "infrastructure", "technology_detection": "web", "reputation": "infrastructure",
    "websites": "web", "archives": "web", "documents": "documents", "companies": "corporate",
    "organizations": "corporate", "public_records": "public_records", "legal_records": "judicial",
    "sanctions": "regulatory", "procurement": "procurement", "publications": "documents", "news": "news",
    "images": "other", "geolocation": "other", "people_search": "commercial", "adapters": "other",
    "b2b_sales_intelligence": "commercial", "business_contact_data": "commercial", "consumer_data": "commercial",
    "identity_resolution": "commercial", "professional_intelligence": "commercial", "company_intelligence": "commercial",
    "public_records_aggregation": "commercial", "contact_enrichment": "commercial", "marketing_data": "commercial",
    "historical_data": "commercial",
}

# Official-source kind → Data Sources category (migration of country-page sources, audit suggestions)
KIND_CATEGORY = {
    "company_registry": "companies", "beneficial_ownership": "companies", "stock_exchange": "companies",
    "chamber_of_commerce": "companies", "business_directory": "companies", "charity_register": "organizations",
    "intellectual_property": "organizations", "court_portal": "legal_records", "judgments": "legal_records",
    "court": "legal_records", "bar_association": "legal_records", "insolvency": "public_records",
    "property": "public_records", "offender_registry": "public_records", "own_record": "public_records",
    "wanted": "sanctions", "people_directory": "people_search", "aggregator": "public_records",
    "professional_register": "organizations", "licensing": "organizations", "academic_directory": "publications",
    "regulatory": "organizations", "financial_regulator": "organizations", "central_bank": "organizations",
    "data_protection": "organizations", "sanctions": "sanctions", "pep": "sanctions", "debarment": "sanctions",
    "procurement": "procurement", "news_agency": "news", "news_media": "news", "public_broadcaster": "news",
    "gazette": "public_records", "legislation": "documents", "library": "publications", "archives": "documents",
    "government_portal": "public_records", "parliament": "public_records", "statistics": "public_records",
    "open_data": "public_records", "tax_authority": "public_records", "ombudsman": "public_records",
    "anti_corruption": "public_records", "elections": "public_records", "election_authority": "public_records",
    "public_officials": "public_records", "lobbying": "public_records", "search_engine": "search", "forum": "forums",
    "domain_registry": "domains", "internet_registry": "infrastructure", "commercial_provider": "people_search",
}

SOURCE_ID = re.compile(r"^(user|custom)-[a-z0-9-]{1,64}$")
CATEGORY_ID = re.compile(r"^ucat-[a-z0-9-]{1,64}$")
PLATFORM_ID = re.compile(r"^uplat-[a-z0-9-]{1,64}$")
_URL = re.compile(r"^https?://[^\s/$.?#][^\s]*$", re.I)


def category_for_kind(kind: str | None) -> str:
    return KIND_CATEGORY.get(kind or "", "public_records")


# --------------------------------------------------------------------------- normalisation

def url_key(url: str) -> str:
    """Comparable form of a URL: host without www., path without trailing slash or index page, no scheme/query."""
    try:
        p = urlsplit((url or "").strip())
    except ValueError:
        return (url or "").strip().lower()
    host = (p.hostname or "").lower().rstrip(".")
    if host.startswith("www."):
        host = host[4:]
    path = re.sub(r"/+", "/", p.path or "")
    path = re.sub(r"/(index|default)\.(html?|php|aspx?)$", "", path, flags=re.I).rstrip("/")
    return host + path


def host_of(url: str) -> str:
    return url_key(url).split("/", 1)[0]


def title_key(title: str) -> str:
    t = unicodedata.normalize("NFKD", title or "").encode("ascii", "ignore").decode().lower()
    t = re.sub(r"\([^)]*\)", " ", t)
    return " ".join(re.findall(r"[a-z0-9]+", t))


def _slug(text: str) -> str:
    s = unicodedata.normalize("NFKD", text or "").encode("ascii", "ignore").decode().lower()
    return re.sub(r"[^a-z0-9]+", "-", s).strip("-")[:32].strip("-")


def clean_url(url: str, what: str = "URL") -> str:
    url = (url or "").strip()
    if len(url) > 1000 or not _URL.match(url):
        raise ValueError(f"The {what} must be a web address starting with http:// or https://")
    p = urlsplit(url)
    if p.username or p.password:
        raise ValueError(f"The {what} must not contain a user name or password")
    if not p.hostname:
        raise ValueError(f"The {what} has no host name")
    return url


def _text(value, limit: int, what: str, required: bool = False) -> str:
    v = " ".join(str(value or "").split()) if limit <= 300 else str(value or "").strip()
    if required and len(v) < 2:
        raise ValueError(f"{what} is required")
    if len(v) > limit:
        raise ValueError(f"{what} is too long (max {limit} characters)")
    return v


# --------------------------------------------------------------------------- snapshot (cached per catalog version)

@dataclass
class Snapshot:
    version: int
    sources: list[dict]
    categories: list[dict]
    platforms: list[dict]
    ignores: set[tuple[str, str]]
    by_id: dict[str, dict] = field(init=False)
    by_jurisdiction: dict[str, list[dict]] = field(init=False)
    category_by_id: dict[str, dict] = field(init=False)
    platform_by_id: dict[str, dict] = field(init=False)
    platform_objects: tuple = field(init=False)
    fingerprint: str = field(init=False)

    def __post_init__(self) -> None:
        self.by_id = {s["id"]: s for s in self.sources}
        self.by_jurisdiction = {}
        for s in self.sources:
            for code in s["jurisdictions"]:
                self.by_jurisdiction.setdefault(code, []).append(s)
        self.category_by_id = {c["id"]: c for c in self.categories}
        self.platform_by_id = {p["id"]: p for p in self.platforms}
        self.platform_objects = tuple(P.from_user(p) for p in self.platforms)
        # content fingerprint: what the per-country coverage rows depend on (cache key, safe across databases)
        relevant = [[s["id"], s["enabled"], s["review"], s["jurisdictions"], s["category"], s.get("kind"), s["connector"],
                     s["source_type"]] for s in self.sources]
        relevant += [[p["id"], p.get("countries"), p.get("profile_url"), p.get("status")] for p in self.platforms]
        relevant += [[c["id"], c["group"]] for c in self.categories]
        self.fingerprint = hashlib.sha1(json.dumps(relevant, sort_keys=True).encode()).hexdigest()[:16]

    def for_jurisdiction(self, code: str) -> list[dict]:
        return self.by_jurisdiction.get(code, [])

    def sources_for_platform(self, platform_id: str) -> list[dict]:
        return [s for s in self.sources if platform_id in s["platforms"]]


def active(s: dict) -> bool:
    """Counts towards coverage: switched on and not excluded or marked unavailable by the investigator."""
    return bool(s["enabled"]) and s["review"] not in ("excluded", "unavailable")


class CatalogStore:
    """The user layer of one database. Reads are cached until the catalog version changes (every write bumps it)."""

    def __init__(self, db) -> None:
        self._db = weakref.ref(db)        # the database owns its store; no reference cycle keeps either alive
        self._snap: Snapshot | None = None
        self._checked = 0.0

    @property
    def db(self):
        db = self._db()
        if db is None:
            raise RuntimeError("The catalog's database is closed")
        return db

    def snapshot(self) -> Snapshot:
        version = self.db.catalog_version()
        self._checked = time.monotonic()
        if self._snap is None or self._snap.version != version:
            raw = self.db.catalog_load()
            self._snap = Snapshot(raw["version"], raw["sources"], raw["categories"], raw["platforms"],
                                  set(raw["ignores"]))
        return self._snap

    def recent(self, max_age: float = 5.0) -> Snapshot:
        """The snapshot, compared with the database at most every ``max_age`` seconds — for hot paths such as every
        transform run of an investigation."""
        if self._snap is not None and time.monotonic() - self._checked < max_age:
            return self._snap
        return self.snapshot()

    # ------------------------------------------------------------------ sources
    def create_source(self, data: dict, *, allow_duplicate: bool = False) -> dict:
        snap = self.snapshot()
        src = validate_source(data, snap)
        base = f"user-{_slug(src['title']) or 'source'}"
        sid = f"{base}-{secrets.token_hex(3)}"
        while sid in snap.by_id:
            sid = f"{base}-{secrets.token_hex(3)}"
        src["id"] = sid
        self._refuse_duplicates(src, snap, allow_duplicate)
        src.update(enabled=data.get("enabled", True) is not False, link_check=None)
        self.db.catalog_put_source(src)
        return self.snapshot().by_id[sid]

    def update_source(self, sid: str, data: dict, *, allow_duplicate: bool = False) -> dict:
        snap = self.snapshot()
        cur = snap.by_id.get(sid)
        if cur is None:
            raise KeyError(sid)
        src = validate_source(data, snap)
        src["id"] = sid
        self._refuse_duplicates(src, snap, allow_duplicate, previous=cur)
        src["enabled"] = cur["enabled"] if data.get("enabled") is None else bool(data["enabled"])
        src["link_check"] = cur.get("link_check") if url_key(cur["url"]) == src["url_key"] else None
        self.db.catalog_put_source(src)
        return self.snapshot().by_id[sid]

    def patch_source(self, sid: str, *, enabled: bool | None = None, review: str | None = None,
                     link_check: dict | None = None) -> dict:
        snap = self.snapshot()
        cur = snap.by_id.get(sid)
        if cur is None:
            raise KeyError(sid)
        if review is not None and review not in REVIEW:
            raise ValueError("Unknown review status")
        src = {**cur, "url_key": url_key(cur["url"])}
        if enabled is not None:
            src["enabled"] = bool(enabled)
        if review is not None:
            src["review"] = review
        if link_check is not None:
            src["link_check"] = link_check
        self.db.catalog_put_source(src)
        return self.snapshot().by_id[sid]

    def delete_source(self, sid: str) -> bool:
        return self.db.catalog_delete_source(sid)

    def _refuse_duplicates(self, src: dict, snap: Snapshot, allow: bool, previous: dict | None = None) -> None:
        """Refuses likely duplicates unless confirmed. When editing, only duplicates the edit introduces count: one the
        investigator already accepted ("Continue anyway") does not block later edits."""
        if allow:
            return
        found = [d for d in find_duplicates(src, snap, exclude_id=src.get("id")) if d["strength"] == "likely"]
        if previous is not None and found:
            before = {(d["type"], d["id"]) for d in find_duplicates(previous, snap, exclude_id=src.get("id"))
                      if d["strength"] == "likely"}
            found = [d for d in found if (d["type"], d["id"]) not in before]
        if found:
            raise DuplicateError(found)

    # ------------------------------------------------------------------ categories
    def put_category(self, data: dict, cat_id: str | None = None) -> dict:
        snap = self.snapshot()
        title, group = _category_fields(data)
        clash = category_by_title(title, snap, exclude_id=cat_id)
        if clash:
            raise ValueError(f"A category named “{clash['title']}” already exists")
        if cat_id is None:
            cat_id = f"ucat-{_slug(title) or 'category'}"
            if cat_id in snap.category_by_id:
                cat_id = f"{cat_id}-{secrets.token_hex(2)}"
        elif cat_id not in snap.category_by_id:
            raise KeyError(cat_id)
        self.db.catalog_put_category({"id": cat_id, "title": title, "group": group,
                                      "description": _text(data.get("description"), 500, "Description")})
        return self.snapshot().category_by_id[cat_id]

    def delete_category(self, cat_id: str) -> list[str]:
        if cat_id not in self.snapshot().category_by_id:
            raise KeyError(cat_id)
        return self.db.catalog_delete_category(cat_id)

    # ------------------------------------------------------------------ platforms
    def put_platform(self, data: dict, pid: str | None = None, *, allow_duplicate: bool = False) -> dict:
        snap = self.snapshot()
        p = validate_platform(data)
        if pid is None:
            pid = f"uplat-{_slug(p['name']) or 'platform'}-{secrets.token_hex(3)}"
        elif pid not in snap.platform_by_id:
            raise KeyError(pid)
        if not allow_duplicate:
            clash = platform_duplicates(p["host"], snap, exclude_id=pid)
            if clash:
                raise DuplicateError(clash)
        p["id"] = pid
        self.db.catalog_put_platform(p)
        return self.snapshot().platform_by_id[pid]

    def delete_platform(self, pid: str) -> bool:
        return self.db.catalog_delete_platform(pid)

    # ------------------------------------------------------------------ audit suggestions the investigator ignored
    def set_ignored(self, code: str, item: str, ignored: bool) -> None:
        self.db.catalog_set_ignore(code, item, ignored)


class DuplicateError(ValueError):
    def __init__(self, matches: list[dict]) -> None:
        super().__init__("This may already exist")
        self.matches = matches


def store_for(db) -> CatalogStore:
    """The store of one database (shared by the API and the engine; caches survive between requests)."""
    store = getattr(db, "_catalog_store", None)
    if store is None:
        store = CatalogStore(db)
        db._catalog_store = store
    return store


# --------------------------------------------------------------------------- validation

def _category_fields(data: dict) -> tuple[str, str]:
    title = _text(data.get("title"), 80, "Category name", required=True)
    group = data.get("group") or "other"
    if group not in J.SOURCE_GROUPS:
        raise ValueError("Unknown jurisdiction group")
    return title, group


def category_by_title(title: str, snap: Snapshot, exclude_id: str | None = None) -> dict | None:
    """The built-in or user category with the same name (case, accents and punctuation ignored)."""
    key = title_key(title)
    for cid, c in category_vocab(snap).items():
        if cid != exclude_id and title_key(c["title"]) == key:
            return c
    return None


def category_vocab(snap: Snapshot) -> dict[str, dict]:
    """System categories (connector manifests) + the investigator's own, with their jurisdiction group (computed once
    per snapshot: a snapshot never changes, a write makes a new one)."""
    cached = getattr(snap, "_category_vocab", None)
    if cached is not None:
        return cached
    out = {cid: {"id": cid, "title": title, "group": CATEGORY_GROUP.get(cid, "other"), "origin": "system",
                 "progress_group": pg} for cid, (title, pg) in CATEGORIES.items()}
    for c in snap.categories:
        out[c["id"]] = {"id": c["id"], "title": c["title"], "group": c["group"], "origin": "user",
                        "description": c.get("description") or "", "progress_group": "Your categories"}
    snap._category_vocab = out
    return out


def jurisdiction_codes() -> dict[str, str]:
    out = {code: j.name for code, j in J.jurisdictions().items()}
    out.update(J.PSEUDO)
    return out


def validate_source(data: dict, snap: Snapshot) -> dict:
    """Checks a source definition and returns it normalised. Unknown references are refused; nothing executable is
    accepted (the API model forbids unknown fields)."""
    from .connectors import REGISTRY, load_all
    load_all()
    title = _text(data.get("title"), 200, "Name", required=True)
    url = clean_url(data.get("url") or "")
    cats = category_vocab(snap)
    category = data.get("category") or ""
    if category not in cats:
        raise ValueError("Choose a category (or create one first)")
    stype = data.get("source_type") or "reference"
    if stype not in SOURCE_TYPES:
        raise ValueError("Unknown source type")
    access = data.get("access") or "unknown"
    if access not in ACCESS:
        raise ValueError("Unknown access model")
    kind = data.get("kind") or ""
    if kind and kind not in J.SOURCE_KINDS:
        raise ValueError("Unknown official source type")
    codes = jurisdiction_codes()
    juris: list[str] = []
    for raw in data.get("jurisdictions") or []:
        code = str(raw).strip().upper()
        if code in ("GLOBAL", "*", ""):
            continue
        if code not in codes:
            raise ValueError(f"Unknown jurisdiction {code!r}")
        if code not in juris:
            juris.append(code)
    if len(juris) > 60:
        raise ValueError("Too many jurisdictions")
    connector = data.get("connector") or ""
    if connector and connector not in REGISTRY:
        raise ValueError(f"No connector named {connector!r} in this installation")
    plats: list[str] = []
    for pid in data.get("platforms") or []:
        pid = str(pid)
        if pid not in snap.platform_by_id and P.get(pid) is None:
            raise ValueError(f"Unknown platform {pid!r}")
        if pid not in plats:
            plats.append(pid)
    if len(plats) > 20:
        raise ValueError("Too many platforms")
    inputs = []
    for t in data.get("inputs") or []:
        if t not in ENTITY_TYPES:
            raise ValueError(f"Unknown input type {t!r}")
        if t not in inputs:
            inputs.append(t)
    review = data.get("review") or "not_verified"
    if review not in REVIEW:
        raise ValueError("Unknown review status")
    pclass = data.get("provider_class") or "public"
    if pclass not in PROVIDER_CLASSES:
        raise ValueError("Unknown provider type")
    terms = (data.get("terms_url") or "").strip()
    if terms:
        terms = clean_url(terms, "terms URL")
    dtypes = []
    for t in data.get("data_types") or []:
        t = _text(t, 60, "Data type")
        if t and t not in dtypes:
            dtypes.append(t)
    from .access_class import ACCESS_MODELS
    models = []
    for mdl in data.get("access_models") or []:
        if mdl not in ACCESS_MODELS:
            raise ValueError(f"Unknown access model {mdl!r}")
        if mdl not in models:
            models.append(mdl)
    docs = (data.get("documentation_url") or "").strip()
    if docs:
        docs = clean_url(docs, "documentation URL")
    api = data.get("api_available") or "unknown"
    if api not in ("yes", "no", "unknown"):
        raise ValueError("API available: yes, no or unknown")
    verified = (data.get("last_verified") or "").strip()
    if verified and not re.fullmatch(r"\d{4}-\d{2}-\d{2}", verified):
        raise ValueError("Last verification must be a date (YYYY-MM-DD)")
    return {"access_models": models, "api_available": api, "web_app": bool(data.get("web_app")),
            "documentation_url": docs, "coverage": _text(data.get("coverage"), 300, "Coverage"),
            "limitations": _text(data.get("limitations"), 1000, "Known limitations"), "last_verified": verified,
            "provider_class": pclass, "terms_url": terms, "data_types": dtypes[:20],
            "pricing": _text(data.get("pricing"), 200, "Pricing"), "auth": _text(data.get("auth"), 120, "Authentication"),
            "title": title, "url": url, "url_key": url_key(url), "category": category, "source_type": stype,
            "access": access, "kind": kind, "jurisdictions": juris, "platforms": plats, "connector": connector,
            "inputs": inputs[:20], "official": bool(data.get("official")), "name_search": bool(data.get("name_search")),
            "description": _text(data.get("description"), 2000, "Description"),
            "person_data": _text(data.get("person_data"), 300, "Personal data"),
            "notes": _text(data.get("notes"), 2000, "Notes"), "review": review}


def validate_platform(data: dict) -> dict:
    name = _text(data.get("name"), 120, "Name", required=True)
    url = clean_url(data.get("url") or "")
    category = data.get("category") or "other"
    if category not in P.PLATFORM_CATEGORIES:
        raise ValueError("Unknown platform category")
    profile = (data.get("profile_url") or "").strip()
    if profile:
        clean_url(profile.replace("{username}", "user"), "profile URL")
        if "{username}" not in profile:
            raise ValueError("The profile URL must contain {username}, e.g. https://example.com/u/{username}")
    codes = set(jurisdiction_codes())
    countries = []
    for raw in data.get("countries") or []:
        code = str(raw).strip().upper()
        if code in ("GLOBAL", "*", ""):
            continue
        if code not in codes or code in J.PSEUDO:
            raise ValueError(f"Unknown country {code!r}")
        if code not in countries:
            countries.append(code)
    status = data.get("status") or "active"
    if status not in P.SERVICE_STATUSES:
        raise ValueError("Unknown service status")
    public_profile = data.get("public_profile") or "yes"
    if public_profile not in ("yes", "partial", "login_wall", "none"):
        raise ValueError("Unknown public-profile value")
    return {"name": name, "url": url, "host": host_of(url), "category": category, "profile_url": profile,
            "countries": countries[:60], "region": _text(data.get("region"), 80, "Region"),
            "notes": _text(data.get("notes"), 1000, "Notes"), "status": status, "public_profile": public_profile}


# --------------------------------------------------------------------------- read models

def automation(s: dict, states: dict[str, str]) -> dict:
    """How the source is used — never claims automation that does not exist."""
    from .connectors import REGISTRY, STATUS_TITLES
    name = s.get("connector") or ""
    if name:
        if name not in REGISTRY:
            return {"mode": "missing_connector", "title": f"Linked connector “{name}” is not in this installation",
                    "connector": name}
        st = states.get(name, "")
        return {"mode": "connector", "connector": name, "connector_title": REGISTRY[name].manifest.title,
                "connector_status": st, "title": f"Automated by connector {REGISTRY[name].manifest.title} "
                                                 f"({STATUS_TITLES.get(st, st)})"}
    t = s.get("source_type") or "reference"
    return {"mode": SOURCE_TYPES.get(t, SOURCE_TYPES["reference"])["automation"],
            "title": AUTOMATION_TITLES.get(t, AUTOMATION_TITLES["reference"])}


def status(s: dict, states: dict[str, str]) -> tuple[str, str]:
    """Same vocabulary as connectors (registry/connectors.STATUS_TITLES)."""
    if not s["enabled"]:
        return "disabled", "Switched off by you"
    if s["review"] == "excluded":
        return "excluded", "Excluded by your policy decision"
    a = automation(s, states)
    if a["mode"] == "connector":
        return a["connector_status"] or "manual", a["title"]
    if s.get("provider_class") == "commercial" and s.get("source_type") != "manual":
        return "catalog_only", "Licensed commercial provider: not queryable until a connector with your licence exists"
    return "manual", a["title"]


def health(s: dict, cfg, records: dict, snap_health: dict) -> dict:
    """The linked connector's health, or the result of the investigator's link check — never assumed."""
    from .connectors import REGISTRY
    from .health import HEALTH_TITLES, connector_health, link_health
    name = s.get("connector") or ""
    if name and name in REGISTRY:
        h = connector_health(name, cfg, records.get(name), snap_health)
        return {**h, "note": f"Health of the linked connector. {h.get('note') or ''}".strip()}
    lc = s.get("link_check")
    h = link_health(lc)
    if s["review"] == "unavailable":
        h = {"status": "temporarily_unavailable", "title": HEALTH_TITLES["temporarily_unavailable"],
             "last_verified": (lc or {}).get("checked_at")}
    note = (f"Link check: {lc.get('result', '').replace('_', ' ')}" + (f" (HTTP {lc['status']})" if lc.get("status") else "")
            if lc else "Never verified: use “Check link”")
    return {"status": h["status"], "title": h["title"], "note": note, "last_verified": h.get("last_verified"),
            "last_success": (lc or {}).get("checked_at") if h["status"] == "operational" else None,
            "last_failure": (lc or {}).get("checked_at") if lc and h["status"] not in ("operational", "unknown") else None,
            "runs": 0, "evidence": "link check" if lc else ""}


def transparency(s: dict) -> dict:
    a = s["access"]
    requirement = ("Free" if a in FREE_ACCESS else "Subscription required" if a == "paid"
                   else "Account required (registration)" if a == "registration"
                   else "Account required (approval)" if a == "restricted"
                   else "On request only" if a == "on_request" else "Not yet reviewed")
    search = "Searchable by name" if s.get("name_search") else \
        "Not publicly searchable" if a in ("on_request", "restricted") else "Manual search"
    return {"requirement": requirement, "search": search}


def matrix_flags(s: dict) -> dict:
    return {"free": s["access"] in FREE_ACCESS,
            "api": s["source_type"] in ("official_api", "public_api") or s["access"] in ("free_api", "registration"),
            "paid": s["access"] == "paid", "public": bool(s.get("official"))}


def group_of(s: dict, snap: Snapshot) -> str:
    """Jurisdiction-view group: from the official source type when given, else from the category."""
    if s.get("kind") in J.KIND_GROUP:
        return J.KIND_GROUP[s["kind"]]
    cat = snap.category_by_id.get(s["category"])
    if cat:
        return cat["group"] if cat["group"] in J.SOURCE_GROUPS else "other"
    return CATEGORY_GROUP.get(s["category"], "other")


def public(s: dict, snap: Snapshot) -> dict:
    """The stored record as the API returns it (for edit forms)."""
    cats = category_vocab(snap)
    cat = cats.get(s["category"])
    return {**s, "origin": "user", "category_title": cat["title"] if cat else s["category"],
            "source_type_title": SOURCE_TYPES.get(s["source_type"], {}).get("title", s["source_type"]),
            "access_title": ACCESS.get(s["access"], s["access"]), "review_title": REVIEW.get(s["review"], s["review"]),
            "provider_class": s.get("provider_class") or "public",
            "provider_class_title": PROVIDER_CLASSES.get(s.get("provider_class") or "public", ""),
            "kind_title": J.SOURCE_KINDS.get(s.get("kind") or "", ""),
            "group": group_of(s, snap), "group_title": J.SOURCE_GROUPS[group_of(s, snap)],
            "platform_titles": {p: platform_title(p, snap) for p in s["platforms"]},
            **ACX.payload(ACX.for_user_source(s))}


def platform_title(pid: str, snap: Snapshot) -> str:
    up = snap.platform_by_id.get(pid)
    return up["name"] if up else P.title(pid)


def source_item(s: dict, snap: Snapshot, states: dict[str, str], cfg=None, records: dict | None = None,
                snap_health: dict | None = None) -> dict:
    """A user source in the shape of a Data Sources entry (same fields as a connector's ``describe``)."""
    from .health import snapshot as health_snapshot
    st, note = status(s, states)
    cats = category_vocab(snap)
    cat = cats.get(s["category"]) or {"title": s["category"], "progress_group": "Other"}
    auto = automation(s, states)
    juris = s["jurisdictions"] or ["GLOBAL"]
    return {
        "name": s["id"], "origin": "user", "record": public(s, snap), "title": s["title"],
        "description": s.get("description") or s.get("notes") or "", "category": s["category"], "access": s["access"],
        "kind": "user", "implemented": False, "installed": True, "install_note": "",
        "category_title": cat["title"], "progress_group": cat["progress_group"], "status": st,
        "status_title": _status_title(st), "status_note": note, "automation": auto,
        "access_title": ACCESS.get(s["access"], s["access"]), "cost": ACCESS.get(s["access"], s["access"]),
        "rate_limit": {"per_second": 0, "burst": 0, "note": "Not queried by Sextant"},
        "max_calls_per_investigation": None, "jurisdictions": juris,
        "supported_countries": s["jurisdictions"] or ["*"], "legal": "", "public_record": bool(s.get("official")),
        "official_api": s["source_type"] == "official_api",
        "access_method": {"official_api": "api", "public_api": "api", "dataset": "dataset", "local": "local"}.get(
            s["source_type"], "web_page"),
        "website": s["url"], "documentation": "", "terms_url": "", "license": "", "reliability": "not assessed",
        "coverage": ", ".join(juris), "capabilities": [], "input_entities": s.get("inputs") or [], "output_entities": [],
        "disclosure": {"destinations": [], "mode": "none",
                       "sends": "Nothing: Sextant never contacts this source on its own" if auto["mode"] != "connector"
                       else f"Queries go through connector {auto.get('connector_title')} (see its disclosure)",
                       "note": ""},
        "default_enabled": True, "auto_allowed": False, "requires_api_key": False,
        "requires_subscription": s["access"] == "paid", "credentials": [], "transforms": [],
        "matrix": matrix_flags(s), "transparency": transparency(s), **ACX.payload(ACX.for_user_source(s)),
        "commercial": {k: s.get(k) for k in ("access_models", "api_available", "web_app", "documentation_url",
                                              "coverage", "limitations", "last_verified", "pricing", "auth",
                                              "data_types", "terms_url")} if s.get("provider_class") == "commercial" else None,
        "health": health(s, cfg, records or {}, snap_health if snap_health is not None else health_snapshot()),
    }


def _status_title(st: str) -> str:
    from .connectors import STATUS_TITLES
    return STATUS_TITLES.get(st, st)


def jurisdiction_entry(s: dict, snap: Snapshot, states: dict[str, str], code: str) -> dict:
    """A user source in the shape of an official source of a jurisdiction (``JurisdictionSource.to_dict``)."""
    st, _ = status(s, states)
    lc = s.get("link_check")
    return {**public(s, snap), "country": code, "provenance": "user", "regional": "",
            "kind_title": J.SOURCE_KINDS.get(s.get("kind") or "", "") or category_vocab(snap).get(
                s["category"], {}).get("title", s["category"]),
            "api": "", "anonymized": False, "status": st, "status_title": _status_title(st),
            "automation": automation(s, states), "active": active(s),
            "verification": {"result": lc.get("result"), "checked_at": lc.get("checked_at")} if lc else None}


def jurisdictions_payload(snap: Snapshot, states: dict[str, str], countries: list[str] | None = None) -> dict:
    """The Jurisdictions screen: built-in official sources and the investigator's own, in one list per jurisdiction
    (each row says its origin), with counts computed from both."""
    d = J.payload(countries)
    known = set()
    for region in d["regions"]:
        for j in region["jurisdictions"]:
            known.add(j["code"])
            for s in j["sources"]:
                s["origin"] = "system"
                s["active"] = True
            mine = [jurisdiction_entry(s, snap, states, j["code"]) for s in snap.for_jurisdiction(j["code"])]
            j["sources"].extend(mine)
            j["user_sources"] = len(mine)
            j["automated"] += sum(1 for s in mine if s["active"] and s["automation"]["mode"] == "connector")
            for s in mine:
                j["counts"][s["kind"] or s["category"]] = j["counts"].get(s["kind"] or s["category"], 0) + 1
    mine_all = [s for s in snap.sources if set(s["jurisdictions"]) & known]
    t = d["totals"]
    t["user_sources"] = len(mine_all)
    t["sources"] += sum(len(s["jurisdictions"]) for s in mine_all)
    t["automated"] += sum(len(s["jurisdictions"]) for s in mine_all if active(s) and s["connector"])
    t["name_searchable"] += sum(len(s["jurisdictions"]) for s in mine_all if s.get("name_search"))
    d["groups"] = J.SOURCE_GROUPS
    return d


# --------------------------------------------------------------------------- duplicate detection

@lru_cache(maxsize=1)
def _system_index() -> dict:
    """URL, host and name indexes of the built-in catalog (connectors, official sources, curated platforms)."""
    from .connectors import REGISTRY, load_all
    load_all()
    by_url: dict[str, list[dict]] = {}
    by_host: dict[str, list[dict]] = {}
    by_title: dict[str, list[dict]] = {}

    def add(ref: dict, url: str) -> None:
        if url:
            by_url.setdefault(url_key(url), []).append(ref)
            by_host.setdefault(host_of(url), []).append(ref)
        if title_key(ref["title"]):
            by_title.setdefault(title_key(ref["title"]), []).append(ref)

    for name, conn in REGISTRY.items():
        m = conn.manifest
        add({"type": "connector", "id": name, "title": m.title, "url": m.website, "origin": "system",
             "jurisdictions": [c for c in m.supported_countries if c != "*"], "href": f"#/sources/{name}"}, m.website)
    for code, j in J.jurisdictions().items():
        for s in j.sources:
            if s.provenance == "curated":
                add({"type": "official_source", "id": s.id, "title": s.title, "url": s.url, "origin": "system",
                     "jurisdictions": [code], "href": f"#/coverage/{code}" if len(code) == 2 else "#/jurisdictions"}, s.url)
    for p in P.curated():
        ref = {"type": "platform", "id": p.id, "title": p.name, "url": p.url, "origin": "system", "jurisdictions": [],
               "href": "#/platforms"}
        for h in p.hosts:
            by_host.setdefault(h, []).append(ref)
    return {"url": by_url, "host": by_host, "title": by_title}


def _bare(key: str) -> bool:
    return "/" not in key


def find_duplicates(cand: dict, snap: Snapshot, exclude_id: str | None = None) -> list[dict]:
    """Likely and possible duplicates of a source being added or edited, with the reasons."""
    idx = _system_index()
    key = url_key(cand.get("url") or "") if cand.get("url") else ""
    host = key.split("/", 1)[0] if key else ""
    tkey = title_key(cand.get("title") or "")
    juris = set(cand.get("jurisdictions") or [])
    hits: dict[tuple[str, str], dict] = {}

    def hit(ref: dict, reason: str, strength: str) -> None:
        k = (ref["type"], ref["id"])
        if ref["id"] == exclude_id:
            return
        h = hits.setdefault(k, {**ref, "reasons": [], "strength": "possible",
                                "editable": ref["origin"] == "user" and ref["type"] == "user_source"})
        if reason not in h["reasons"]:
            h["reasons"].append(reason)
        if strength == "likely":
            h["strength"] = "likely"

    def overlap(other: list[str]) -> bool:
        return not juris or not other or bool(juris & set(other))

    if key:
        for ref in idx["url"].get(key, []):
            hit(ref, "Same address", "likely")
        for ref in idx["host"].get(host, []):
            if ref["type"] == "platform":
                hit(ref, f"This website is the platform {ref['title']}", "possible")
            elif _bare(key) or _bare(url_key(ref["url"])):
                hit(ref, "Same website", "possible")
    if len(tkey) >= 3:
        for ref in idx["title"].get(tkey, []):
            hit(ref, "Same name", "likely" if overlap(ref["jurisdictions"]) else "possible")
    for s in snap.sources:
        ref = {"type": "user_source", "id": s["id"], "title": s["title"], "url": s["url"], "origin": "user",
               "jurisdictions": s["jurisdictions"], "href": f"#/sources/{s['id']}"}
        okey = url_key(s["url"])
        if key and okey == key:
            hit(ref, "Same address", "likely")
        elif key and okey.split("/", 1)[0] == host and (_bare(key) or _bare(okey)):
            hit(ref, "Same website", "possible")
        if len(tkey) >= 3 and title_key(s["title"]) == tkey:
            hit(ref, "Same name", "likely" if overlap(s["jurisdictions"]) else "possible")
        if cand.get("connector") and s["connector"] == cand["connector"] and overlap(s["jurisdictions"]):
            hit(ref, "Same connector and jurisdiction", "possible")
        if set(cand.get("platforms") or []) & set(s["platforms"]) and overlap(s["jurisdictions"]):
            hit(ref, "Same platform and jurisdiction", "possible")
    for p in snap.platforms:
        if host and p["host"] == host:
            hit({"type": "user_platform", "id": p["id"], "title": p["name"], "url": p["url"], "origin": "user",
                 "jurisdictions": p.get("countries") or [], "href": "#/platforms"},
                f"This website is your platform {p['name']}", "possible")
    return sorted(hits.values(), key=lambda h: (h["strength"] != "likely", h["origin"] != "user", h["title"].lower()))


def platform_duplicates(host: str, snap: Snapshot, exclude_id: str | None = None) -> list[dict]:
    out = []
    p = P.platform_for_host(host)
    if p is not None:
        out.append({"type": "platform", "id": p.id, "title": p.name, "url": p.url, "origin": "system",
                    "reasons": ["Same website as a built-in platform"], "strength": "likely", "editable": False,
                    "href": "#/platforms"})
    else:
        imp = P._match_host(host, {h: x for x in P.imported() for h in x.hosts})
        if imp is not None:
            out.append({"type": "platform", "id": imp.id, "title": imp.name, "url": imp.url, "origin": "system",
                        "reasons": [f"Already listed from the {imp.source} dataset"], "strength": "likely",
                        "editable": False, "href": "#/platforms"})
    for u in snap.platforms:
        if u["host"] == host and u["id"] != exclude_id:
            out.append({"type": "user_platform", "id": u["id"], "title": u["name"], "url": u["url"], "origin": "user",
                        "reasons": ["Same website as one of your platforms"], "strength": "likely", "editable": True,
                        "href": "#/platforms"})
    return out


# --------------------------------------------------------------------------- vocabulary for the forms

def vocab(snap: Snapshot) -> dict:
    from .connectors import REGISTRY, load_all
    load_all()
    cats = category_vocab(snap)
    sys_counts: dict[str, int] = {}
    for conn in REGISTRY.values():
        sys_counts[conn.manifest.category] = sys_counts.get(conn.manifest.category, 0) + 1
    user_counts: dict[str, int] = {}
    for s in snap.sources:
        user_counts[s["category"]] = user_counts.get(s["category"], 0) + 1
    return {
        "categories": sorted(({**c, "group_title": J.SOURCE_GROUPS.get(c["group"], c["group"]),
                               "system_sources": sys_counts.get(cid, 0), "user_sources": user_counts.get(cid, 0)}
                              for cid, c in cats.items()), key=lambda c: c["title"].lower()),
        "groups": J.SOURCE_GROUPS, "source_types": SOURCE_TYPES, "access": ACCESS, "kinds": J.SOURCE_KINDS,
        "provider_classes": PROVIDER_CLASSES,
        "review": REVIEW,
        "jurisdictions": [{"code": k, "name": v} for k, v in sorted(jurisdiction_codes().items(), key=lambda kv: kv[1])],
        "connectors": sorted(({"name": n, "title": c.manifest.title, "category_title": c.manifest.category_title}
                              for n, c in REGISTRY.items()), key=lambda c: c["title"].lower()),
        "platforms": sorted([{"id": p.id, "name": p.name, "origin": "system"} for p in P.curated()] +
                            [{"id": p["id"], "name": p["name"], "origin": "user"} for p in snap.platforms],
                            key=lambda p: p["name"].lower()),
        "platform_categories": P.PLATFORM_CATEGORIES, "service_statuses": list(P.SERVICE_STATUSES),
        "entity_types": [{"id": k, "title": t.title} for k, t in ENTITY_TYPES.items()],
        "version": snap.version,
    }


# --------------------------------------------------------------------------- reset

def reset_summary(db, snap: Snapshot) -> dict:
    """What the catalog reset would remove and what it keeps — shown before the investigator confirms."""
    from .connectors import REGISTRY, load_all
    load_all()
    counts = db.catalog_custom_counts()
    prefs = db.connector_prefs()
    return {
        "remove": counts,
        "total": sum(v for k, v in counts.items() if k not in ("jurisdiction_links", "platform_links", "connector_links")),
        "items": {
            "sources": [s["title"] for s in snap.sources if s["source_type"] != "reference"][:100],
            "links": [s["title"] for s in snap.sources if s["source_type"] == "reference"][:100],
            "categories": [c["title"] for c in snap.categories][:100],
            "platforms": [p["name"] for p in snap.platforms][:100],
            "source_preferences": sorted(REGISTRY[n].manifest.title if n in REGISTRY else n for n in prefs)[:100],
        },
        "keeps": {"connectors": len(REGISTRY), "official_sources": len(J.all_sources()),
                  "platforms": len(P.curated()) + len(P.imported()), "categories": len(CATEGORIES),
                  "note": "Investigations, API keys and settings are kept. Built-in sources, categories and platforms "
                          "come from this version of Sextant and are never stored as your data, so they are back "
                          "exactly as shipped."},
    }


# --------------------------------------------------------------------------- export / import (backup and restore)

FORMAT = "sextant-catalog"
FORMAT_VERSION = 1
NOT_EXPORTED = "API keys, investigations, settings and saved connector searches are never exported."
CATEGORY_FIELDS = ("id", "title", "group", "description", "created_at", "updated_at")
PLATFORM_FIELDS = ("id", "name", "url", "category", "profile_url", "countries", "region", "notes", "status",
                   "public_profile", "created_at", "updated_at")
SOURCE_FIELDS = ("id", "title", "url", "category", "source_type", "access", "kind", "jurisdictions", "platforms",
                 "connector", "inputs", "official", "name_search", "description", "person_data", "notes", "enabled",
                 "review", "link_check", "created_at", "updated_at", "provider_class", "pricing", "auth", "terms_url",
                 "data_types", "access_models", "api_available", "web_app", "documentation_url", "coverage",
                 "limitations", "last_verified")
IMPORT_LIMITS = {"sources": 20000, "categories": 1000, "platforms": 5000, "audit_ignores": 20000}
_ISO = re.compile(r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(\.\d{1,6})?([+-]\d{2}:\d{2}|Z)?$")
LIST_CAP = 300                                     # entries listed per report section


def export_document(db, snap: Snapshot) -> dict:
    """Everything the catalog reset would remove, as a portable JSON document (``import`` restores it)."""
    from datetime import datetime, timezone
    from .. import __version__
    prefs = db.connector_prefs()
    pref_out = {name: {"enabled": p.get("enabled"), "auto_run": p.get("auto_run")} for name, p in sorted(prefs.items())
                if p.get("enabled") is not None or p.get("auto_run") is not None}
    sources = [{k: s.get(k) for k in SOURCE_FIELDS} for s in snap.sources]
    return {
        "format": FORMAT, "format_version": FORMAT_VERSION, "app_version": __version__,
        "exported_at": datetime.now(timezone.utc).replace(microsecond=0).isoformat(), "catalog_version": snap.version,
        "not_included": NOT_EXPORTED,
        "counts": {"sources": sum(1 for s in sources if s["source_type"] != "reference"),
                   "links": sum(1 for s in sources if s["source_type"] == "reference"),
                   "categories": len(snap.categories), "platforms": len(snap.platforms),
                   "audit_ignores": len(snap.ignores), "source_preferences": len(pref_out)},
        "categories": [{k: c.get(k) for k in CATEGORY_FIELDS} for c in snap.categories],
        "platforms": [{k: p.get(k) for k in PLATFORM_FIELDS} for p in snap.platforms],
        "sources": sources,
        "audit_ignores": [{"jurisdiction": code, "item": item} for code, item in sorted(snap.ignores)],
        "connector_preferences": pref_out,
    }


def _iso(value) -> str | None:
    return value if isinstance(value, str) and _ISO.match(value) else None


def _str_list(value) -> list[str]:
    return [str(v) for v in value if isinstance(v, (str, int))] if isinstance(value, list) else []


def _clean_link_check(value) -> dict | None:
    if not isinstance(value, dict) or not isinstance(value.get("result"), str):
        return None
    status = value.get("status")
    return {"url": str(value.get("url") or "")[:1000], "result": value["result"][:40],
            "status": status if isinstance(status, int) else None, "error": str(value.get("error") or "")[:200] or None,
            "checked_at": _iso(value.get("checked_at"))}


def _records(doc: dict, key: str) -> list:
    items = doc.get(key) or []
    if not isinstance(items, list):
        raise ValueError(f"“{key}” must be a list")
    if len(items) > IMPORT_LIMITS[key]:
        raise ValueError(f"Too many {key} ({len(items)}; at most {IMPORT_LIMITS[key]})")
    return items


def plan_import(doc: dict, snap: Snapshot, prefs_now: dict, *, overwrite: bool = False,
                restore_preferences: bool = False) -> tuple[dict, dict]:
    """Checks an exported document against the current catalog and returns (plan, report) without writing anything.

    The file is untrusted input: every record goes through the same validation as the UI form; unknown fields are
    ignored. Records already in the catalog (same id, or a source with the same address) are skipped unless
    ``overwrite`` (same id only). References that no longer exist in this installation (a connector, platform or
    jurisdiction) are dropped from the record with a warning, so one stale reference does not block a restore.
    Categories with the name of an existing category are merged into it."""
    from .connectors import REGISTRY, load_all
    load_all()
    if not isinstance(doc, dict) or doc.get("format") != FORMAT:
        raise ValueError("This is not a Sextant catalog export")
    if doc.get("format_version") != FORMAT_VERSION:
        raise ValueError(f"Unsupported catalog export version {doc.get('format_version')!r}")
    report = {k: {"add": 0, "update": 0, "skip": 0} for k in ("categories", "platforms", "sources")}
    report.update(audit_ignores={"add": 0, "skip": 0}, source_preferences={"apply": 0, "unchanged": 0, "skip": 0},
                  skipped=[], warnings=[], dropped_fields=0,
                  file={k: doc.get(k) for k in ("app_version", "exported_at", "counts")})
    plan: dict = {"categories": [], "platforms": [], "sources": [], "ignores": [], "preferences": {}}

    def skip(kind: str, title: str, reason: str) -> None:
        report[kind]["skip"] += 1
        if len(report["skipped"]) < LIST_CAP:
            report["skipped"].append({"type": kind[:-1] if kind != "categories" else "category", "title": title,
                                      "reason": reason})

    def warn(text: str) -> None:
        if len(report["warnings"]) < LIST_CAP:
            report["warnings"].append(text)

    def fields(raw: dict, allowed: tuple) -> dict:
        report["dropped_fields"] += len(set(raw) - set(allowed))
        return {k: raw.get(k) for k in allowed}

    # ------------------------------------------------------------------ categories (merged by name)
    cat_map: dict[str, str] = {}
    new_cats: list[dict] = []
    by_title: dict[str, dict] = {}                 # name → built-in, existing or newly planned category
    for c in category_vocab(snap).values():
        by_title.setdefault(title_key(c["title"]), c)
    for raw in _records(doc, "categories"):
        if not isinstance(raw, dict):
            skip("categories", "?", "not a category record")
            continue
        c = fields(raw, CATEGORY_FIELDS)
        name = str(c.get("title") or c.get("id") or "?")[:120]
        try:
            title, group = _category_fields(c)
        except ValueError as exc:
            skip("categories", name, str(exc))
            continue
        file_id = c.get("id") if isinstance(c.get("id"), str) else None
        cid = file_id if file_id and CATEGORY_ID.match(file_id) else None
        rec = {"title": title, "group": group, "description": _text(c.get("description"), 500, "Description"),
               "created_at": _iso(c.get("created_at")), "updated_at": _iso(c.get("updated_at"))}
        same = by_title.get(title_key(title))
        if cid and cid in snap.category_by_id:
            cat_map[cid] = cid
            if not overwrite:
                skip("categories", title, "already in your catalog")
            elif same and same["id"] != cid:
                skip("categories", title, f"another category is already named “{same['title']}”")
            else:
                plan["categories"].append({**rec, "id": cid})
                report["categories"]["update"] += 1
            continue
        if same:
            if file_id:
                cat_map[file_id] = same["id"]
            skip("categories", title, f"merged into the existing category “{same['title']}”")
            continue
        if not cid or cid in {x["id"] for x in new_cats}:
            cid = f"ucat-{_slug(title) or 'category'}-{secrets.token_hex(2)}"
        new = {**rec, "id": cid}
        new_cats.append(new)
        plan["categories"].append(new)
        by_title[title_key(title)] = new
        cat_map[file_id or cid] = cid
        report["categories"]["add"] += 1

    # ------------------------------------------------------------------ platforms
    plat_map: dict[str, str] = {}
    new_plats: list[dict] = []
    for raw in _records(doc, "platforms"):
        if not isinstance(raw, dict):
            skip("platforms", "?", "not a platform record")
            continue
        p = fields(raw, PLATFORM_FIELDS)
        p["countries"] = _str_list(p.get("countries"))
        name = str(p.get("name") or p.get("id") or "?")[:120]
        try:
            data = validate_platform(p)
        except ValueError as exc:
            skip("platforms", name, str(exc))
            continue
        pid = p.get("id") if isinstance(p.get("id"), str) and PLATFORM_ID.match(p["id"]) else None
        data.update(created_at=_iso(p.get("created_at")), updated_at=_iso(p.get("updated_at")))
        if pid and pid in snap.platform_by_id:
            plat_map[pid] = pid
            if overwrite:
                plan["platforms"].append({**data, "id": pid})
                report["platforms"]["update"] += 1
            else:
                skip("platforms", data["name"], "already in your catalog")
            continue
        mine = next((u for u in snap.platforms + new_plats if u["host"] == data["host"]), None)
        if mine:
            if pid:
                plat_map[pid] = mine["id"]
            skip("platforms", data["name"], f"same website as your platform “{mine['name']}”")
            continue
        if not pid or pid in {x["id"] for x in new_plats}:
            pid = f"uplat-{_slug(data['name']) or 'platform'}-{secrets.token_hex(3)}"
        if P.platform_for_host(data["host"]) is not None:
            warn(f"Platform “{data['name']}” has the same website as a built-in platform (kept, as in the export)")
        new = {**data, "id": pid}
        new_plats.append(new)
        plan["platforms"].append(new)
        plat_map[p.get("id") if isinstance(p.get("id"), str) else pid] = pid
        report["platforms"]["add"] += 1

    # ------------------------------------------------------------------ sources and links
    temp = Snapshot(snap.version, list(snap.sources), snap.categories + new_cats, snap.platforms + new_plats, set())
    codes = jurisdiction_codes()
    by_url = {url_key(s["url"]): s for s in snap.sources}
    planned_ids: set[str] = set()
    for raw in _records(doc, "sources"):
        if not isinstance(raw, dict):
            skip("sources", "?", "not a source record")
            continue
        s = fields(raw, SOURCE_FIELDS)
        name = str(s.get("title") or s.get("id") or "?")[:120]
        data = dict(s)
        data["category"] = cat_map.get(s.get("category"), s.get("category"))
        conn = s.get("connector") or ""
        if conn and (not isinstance(conn, str) or conn not in REGISTRY):
            warn(f"“{name}”: connector {conn!r} is not in this installation; imported without it")
            data["connector"] = ""
        plats = []
        for pid in _str_list(s.get("platforms")):
            pid = plat_map.get(pid, pid)
            if pid in temp.platform_by_id or P.get(pid) is not None:
                plats.append(pid)
            else:
                warn(f"“{name}”: platform {pid!r} is unknown here; link dropped")
        data["platforms"] = plats
        juris = []
        for code in _str_list(s.get("jurisdictions")):
            if code.upper() in codes:
                juris.append(code.upper())
            else:
                warn(f"“{name}”: jurisdiction {code!r} is unknown here; dropped")
        data["jurisdictions"] = juris
        data["inputs"] = [t for t in _str_list(s.get("inputs")) if t in ENTITY_TYPES]
        data["data_types"] = _str_list(s.get("data_types"))
        try:
            src = validate_source(data, temp)
        except ValueError as exc:
            skip("sources", name, str(exc))
            continue
        sid = s.get("id") if isinstance(s.get("id"), str) and SOURCE_ID.match(s["id"]) else None
        src.update(enabled=s.get("enabled") is not False, link_check=_clean_link_check(s.get("link_check")),
                   created_at=_iso(s.get("created_at")), updated_at=_iso(s.get("updated_at")))
        if sid and sid in snap.by_id:
            if overwrite:
                plan["sources"].append({**src, "id": sid})
                planned_ids.add(sid)
                report["sources"]["update"] += 1
            else:
                skip("sources", src["title"], "already in your catalog")
            continue
        same = by_url.get(src["url_key"])
        if same:
            skip("sources", src["title"], f"same address as your source “{same['title']}”")
            continue
        if not sid or sid in planned_ids:
            sid = f"user-{_slug(src['title']) or 'source'}-{secrets.token_hex(3)}"
        src["id"] = sid
        planned_ids.add(sid)
        by_url[src["url_key"]] = src
        plan["sources"].append(src)
        report["sources"]["add"] += 1

    # ------------------------------------------------------------------ ignored audit suggestions
    for raw in _records(doc, "audit_ignores"):
        code = str(raw.get("jurisdiction") or "").upper() if isinstance(raw, dict) else ""
        item = raw.get("item") if isinstance(raw, dict) else None
        if code not in J.jurisdictions() or not isinstance(item, str) or not 0 < len(item) <= 300:
            report["audit_ignores"]["skip"] += 1
        elif (code, item) in snap.ignores:
            report["audit_ignores"]["skip"] += 1
        else:
            plan["ignores"].append((code, item))
            report["audit_ignores"]["add"] += 1

    # ------------------------------------------------------------------ on/off switches of built-in sources
    prefs = doc.get("connector_preferences") or {}
    if not isinstance(prefs, dict):
        raise ValueError("“connector_preferences” must be an object")
    report["source_preferences"]["in_file"] = len(prefs)
    if restore_preferences:
        for name, pref in list(prefs.items())[:2000]:
            ok = isinstance(pref, dict) and all(pref.get(k) in (None, True, False) for k in ("enabled", "auto_run"))
            if name not in REGISTRY or not ok:
                report["source_preferences"]["skip"] += 1
                warn(f"Switch for {name!r} skipped: {'unknown source' if name not in REGISTRY else 'invalid value'}")
                continue
            want = {"enabled": pref.get("enabled"), "auto_run": pref.get("auto_run")}
            cur = prefs_now.get(name, {})
            if all(cur.get(k) == v for k, v in want.items()):
                report["source_preferences"]["unchanged"] += 1
            else:
                plan["preferences"][name] = want
                report["source_preferences"]["apply"] += 1
    report["changes"] = sum(report[k]["add"] + report[k]["update"] for k in ("categories", "platforms", "sources")) + \
        report["audit_ignores"]["add"] + report["source_preferences"]["apply"]
    return plan, report


def apply_import(db, plan: dict) -> None:
    db.catalog_import(categories=plan["categories"], platforms=plan["platforms"], sources=plan["sources"],
                      ignores=plan["ignores"], preferences=plan["preferences"])
