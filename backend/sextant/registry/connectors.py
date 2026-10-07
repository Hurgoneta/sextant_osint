"""Connector registry.

* Native connectors and adapters register themselves on import (``@register``).
* Catalog-only sources (enterprise providers with no public API documentation) are declared as
  data in ``data/catalog/*.toml`` and appear in the catalog with status *catalog only*.
* Every manifest is validated at registration, so a connector with a misleading or incomplete
  manifest cannot enter the catalog.

Status of a connector for a given configuration (``connector_status``):

    available          ready to run
    needs_credentials  free/approval-gated API — add your key
    needs_subscription paid/metered/enterprise — add the credentials of your subscription
    disabled           switched off (by default for opt-in sources, or by you)
    not_installed      adapter whose external tool is not installed
    catalog_only       documented, no implementation (contact provider)
    manual             platform forbids automation — links for you to open
    retired            the provider shut the service down
"""

from __future__ import annotations

import importlib
import logging
import pkgutil
import tomllib
from pathlib import Path
from typing import Any, Protocol

from ..ontology import ENTITY_TYPES
from ..sdk.manifest import manifest_from_mapping
from ..sdk.transform import CatalogConnector, Connector, Transform

log = logging.getLogger("sextant.registry")

REGISTRY: dict[str, Connector] = {}
TRANSFORMS: dict[str, Transform] = {}
_PENDING_TRANSFORMS: list[type[Transform]] = []
_LOADED = False

DATA_DIR = Path(__file__).resolve().parents[1] / "data"

STATUS_TITLES = {
    "available": "Available",
    "needs_credentials": "API key required",
    "needs_subscription": "Subscription required",
    "disabled": "Disabled",
    "not_installed": "Not installed",
    "catalog_only": "Connector interface only — contact provider",
    "manual": "Manual only",
    "retired": "Retired",
    "excluded": "Excluded by policy",
}


class ConfigView(Protocol):
    def credential(self, key: str) -> str | None: ...
    def connector_prefs(self, name: str) -> dict: ...


class ManifestError(ValueError):
    pass


def register(obj: Any) -> Any:
    """Class decorator (or call with an instance) registering a connector and its transforms."""
    inst: Connector = obj() if isinstance(obj, type) else obj
    m = inst.manifest
    problems = m.validate()
    if problems:
        raise ManifestError(f"Connector {m.name!r}: " + "; ".join(problems))
    if m.name in REGISTRY and not isinstance(REGISTRY[m.name], CatalogConnector):
        raise ManifestError(f"Duplicate connector name {m.name!r}")
    REGISTRY[m.name] = inst
    for t in inst.transforms():
        _add_transform(t)
    for tcls in list(_PENDING_TRANSFORMS):
        if tcls.connector == m.name:
            _PENDING_TRANSFORMS.remove(tcls)
            _bind_standalone(tcls)
    return obj


def register_transform(tcls: type[Transform]) -> type[Transform]:
    """Registers a standalone Transform subclass for an existing (or later-registered) connector."""
    if tcls.connector in REGISTRY:
        _bind_standalone(tcls)
    else:
        _PENDING_TRANSFORMS.append(tcls)
    return tcls


def _bind_standalone(tcls: type[Transform]) -> None:
    t = tcls()
    conn = REGISTRY[tcls.connector]
    t.manifest = conn.manifest
    conn.transforms().append(t)
    _add_transform(t)


def _add_transform(t: Transform) -> None:
    if not t.name or "." not in t.name:
        raise ManifestError(f"Transform name {t.name!r} must look like 'connector.operation'")
    if t.name in TRANSFORMS:
        raise ManifestError(f"Duplicate transform {t.name!r}")
    for typ in (*t.input_types, *t.output_types):
        if typ not in ENTITY_TYPES:
            raise ManifestError(f"Transform {t.name!r} uses unknown entity type {typ!r}")
    if not t.input_types:
        raise ManifestError(f"Transform {t.name!r} declares no input types")
    TRANSFORMS[t.name] = t


def load_all(data_dir: Path | None = None) -> dict[str, Connector]:
    """Imports every connector module under ``sextant.connectors`` and loads catalog-only sources."""
    global _LOADED
    if _LOADED:
        return REGISTRY
    from .. import connectors as pkg
    for mod in pkgutil.walk_packages(pkg.__path__, pkg.__name__ + "."):
        if mod.name.rsplit(".", 1)[-1].startswith("_"):
            continue
        importlib.import_module(mod.name)
    load_catalog(data_dir or DATA_DIR / "catalog")
    _LOADED = True
    return REGISTRY


# Commercial-provider metadata of catalog entries (access models, API, web app, data categories as the provider describes
# them, known limitations, last verification), keyed by source name. Loaded from data/catalog/*.toml.
COMMERCIAL: dict[str, dict] = {}


def load_catalog(path: Path) -> int:
    count = 0
    for file in sorted(path.glob("*.toml")):
        with open(file, "rb") as fh:
            doc = tomllib.load(fh)
        for entry in doc.get("source", []):
            m = manifest_from_mapping(entry)
            if m.name in REGISTRY:
                continue                       # an implementation exists; it wins
            if isinstance(entry.get("commercial"), dict):
                COMMERCIAL[m.name] = dict(entry["commercial"])
            problems = [p for p in m.validate() if "credential" not in p and "disclose" not in p]
            if problems:
                raise ManifestError(f"{file.name}: {m.name}: " + "; ".join(problems))
            REGISTRY[m.name] = CatalogConnector(m)
            count += 1
    return count


# --------------------------------------------------------------------------- queries

def get(name: str) -> Connector:
    return REGISTRY[name]


def transforms_for(entity_type: str) -> list[Transform]:
    return [t for t in TRANSFORMS.values() if entity_type in t.input_types]


def connector_status(conn: Connector, cfg: ConfigView | None) -> tuple[str, str]:
    m = conn.manifest
    if m.status == "excluded":
        return "excluded", m.status_note or m.legal
    if m.access == "manual":
        return "manual", m.status_note or m.legal
    if not m.implemented or isinstance(conn, CatalogConnector):
        return ("retired", m.status_note) if m.status == "retired" else ("catalog_only", m.status_note or "No public API — contact the provider")
    if m.status == "retired":
        return "retired", m.status_note
    if m.access == "manual":
        return "manual", m.legal or m.status_note
    ok, why = conn.installed(cfg)
    if not ok:
        return "not_installed", why
    missing = [c for c in m.credentials if c.required and not (cfg and cfg.credential(c.key))]
    if missing:
        status = "needs_subscription" if m.requires_subscription else "needs_credentials"
        return status, "Needs " + ", ".join(c.label for c in missing)
    prefs = cfg.connector_prefs(m.name) if cfg else {}
    if prefs.get("manual_only"):
        return "manual", prefs.get("manual_note") or "Marked manual-only by you (it asked for verification)"
    enabled = prefs.get("enabled")
    if enabled is None:
        enabled = m.default_enabled
    if not enabled:
        return "disabled", m.opt_in_reason or "Disabled"
    return "available", ""


def auto_allowed(conn: Connector, cfg: ConfigView | None) -> bool:
    prefs = cfg.connector_prefs(conn.manifest.name) if cfg else {}
    auto = prefs.get("auto_run")
    return conn.manifest.auto_default if auto is None else bool(auto)


def describe(conn: Connector, cfg: ConfigView | None) -> dict:
    m = conn.manifest
    status, note = connector_status(conn, cfg)
    d = m.to_dict()
    d["input_entities"] = conn.input_types()
    d["output_entities"] = conn.output_types()
    d["transforms"] = [t.spec() for t in conn.transforms()]
    d["status"] = status
    d["status_title"] = STATUS_TITLES[status]
    d["status_note"] = note or m.status_note
    d["auto_allowed"] = auto_allowed(conn, cfg)
    d["manual_only"] = bool(cfg and cfg.connector_prefs(m.name).get("manual_only"))
    d["credentials"] = [{"key": c.key, "label": c.label, "help": c.help, "secret": c.secret, "required": c.required,
                         "obtain_url": c.obtain_url, "is_set": bool(cfg and cfg.credential(c.key))}
                        for c in m.credentials]
    installed = conn.installed(cfg)
    d["installed"] = installed[0]
    d["install_note"] = installed[1]
    d["transparency"] = transparency(conn, status)
    from .access_class import for_connector, payload
    d["commercial"] = COMMERCIAL.get(m.name)
    d.update(payload(for_connector(m, status, d["commercial"])))
    return d


def transparency(conn: Connector, status: str) -> dict:
    """The two plain-language labels every source shows: what it takes to use it, and how searchable it is."""
    m = conn.manifest
    if status in ("catalog_only", "retired", "excluded"):
        requirement = "Unavailable"
    elif m.access in ("paid", "metered", "enterprise"):
        requirement = "Subscription required"
    elif m.access == "restricted":
        requirement = "Account required (approval)"
    elif m.access == "free_key" or any(c.required for c in m.credentials):
        requirement = "API key required"
    else:
        requirement = "Free"
    if status == "manual" or m.access == "manual":
        search = "Not publicly searchable"
    elif m.access == "restricted" or "limited" in m.tags:
        search = "Limited"
    else:
        search = "Publicly searchable"
    return {"requirement": requirement, "search": search}
