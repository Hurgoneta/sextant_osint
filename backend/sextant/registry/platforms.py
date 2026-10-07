"""Platform registry — every kind of public online presence, not only "social media".

Three layers, all data:

1. **Curated platforms** (``data/platforms/*.toml``): hand-written entries with capability metadata —
   category, country/region, URL patterns (to recognise a profile link anywhere), username format, public
   profile and public search support, official API and its requirements, authentication, rate limits,
   whether private accounts exist (they are never accessed), service status, and *how Sextant checks it*.
2. **Imported datasets** (``data/platforms/imported/*.json``, refreshed by
   ``python -m sextant.tools.import_datasets platforms``): the site lists of WhatsMyName (CC BY-SA 4.0),
   Sherlock (MIT) and Maigret (MIT) — thousands of sites with detection rules maintained by those
   communities. Metadata only; detection rules stay with the tools.
3. **Runtime capability**: what this installation can actually search right now, derived from the check
   method, connector status (keys, subscriptions) and installed adapters. *Registered ≠ searchable.*

Check methods (``check``):

    <connector name>   a dedicated connector using the platform's official/public API
    probe              a declarative public JSON endpoint ([platform.probe]) run by the profile_probes connector
    whatsmyname        the WhatsMyName rule for the site (public profile page, robots.txt respected)
    maigret / sherlock only through the optional adapter (install the tool)
    manual             the platform forbids automated access, or needs login — a link for the investigator
    none               no public profile lookup exists (private messaging apps, most dating apps)
"""

from __future__ import annotations

import json
import re
import tomllib
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import asdict, dataclass, field
from functools import lru_cache
from pathlib import Path
from urllib.parse import parse_qs, parse_qsl, quote, urlsplit

DATA = Path(__file__).resolve().parents[1] / "data" / "platforms"

PLATFORM_CATEGORIES = {
    "social": "Social networks",
    "microblogging": "Microblogging",
    "messaging": "Messaging & public channels",
    "community": "Public community servers & chat directories",
    "video": "Video & live streaming",
    "music": "Music & audio",
    "podcasts": "Podcasts",
    "photography": "Photography",
    "art": "Art & design",
    "writing": "Blogs & writing",
    "newsletters": "Newsletters",
    "professional": "Professional networks",
    "jobs": "Job platforms",
    "freelance": "Freelance & portfolios",
    "gaming": "Gaming",
    "forums": "Forums & discussion boards",
    "qa": "Question & answer",
    "developer": "Developer & code",
    "packages": "Package registries",
    "academic": "Academic & research repositories",
    "education": "Education & learning",
    "dating": "Dating",
    "fitness": "Fitness & sport",
    "travel": "Travel & places",
    "books": "Books & reading",
    "film": "Film, TV & anime",
    "reviews": "Reviews & reputation",
    "marketplaces": "Marketplaces & auctions",
    "creator": "Creator & donation platforms",
    "crowdfunding": "Crowdfunding",
    "events": "Events & meetups",
    "identity": "Identity & link pages",
    "directories": "Public profile & business directories",
    "reference": "Wikis & reference",
    "genealogy": "Genealogy & memorials",
    "adult": "Adult",
    "other": "Other",
}
LEGACY_CATEGORIES = {"developer_platforms": "developer", "blogs": "writing", "professional_networks": "professional",
                     "chat": "messaging"}
CHECK_SPECIAL = ("probe", "whatsmyname", "maigret", "sherlock", "manual", "none")
SERVICE_STATUSES = ("active", "defunct", "restricted")
SEARCH_STATUS_TITLES = {
    "searchable": "Searchable (automated)",
    "api_key_required": "API key / approval required",
    "subscription_required": "Subscription required",
    "adapter_not_installed": "Only via an optional adapter (not installed)",
    "adapter_off": "Via an installed adapter that is switched off (runs with SEARCH EVERYTHING or when switched on)",
    "search_everything_only": "Checked with SEARCH EVERYTHING (beyond Maigret's top sites in normal runs)",
    "adult_off": "Adult/dating site — checked only when enabled in Settings",
    "no_rule": "Registered — no working detection rule",
    "manual": "Manual check (link for the investigator)",
    "not_publicly_searchable": "Not publicly searchable",
    "defunct": "Service shut down",
    "excluded": "Excluded by policy",
    "disabled": "Connector disabled",
    "catalog_only": "Documented connector only (approval-restricted API, not implemented)",
}
SEARCHABLE = ("searchable",)
OPT_IN = ("adapter_off", "search_everything_only", "adult_off")
API_ONLY = ("api_key_required", "subscription_required")
UNAVAILABLE = ("adapter_not_installed", "no_rule", "manual", "not_publicly_searchable", "defunct", "excluded", "disabled",
               "catalog_only")         # adapter_off is counted on its own (OPT_IN), neither searchable nor unavailable

# First path segments that are site pages, never usernames (used for auto-derived patterns)
RESERVED = ("about", "account", "accounts", "api", "apps", "blog", "contact", "dashboard", "developers", "docs",
            "download", "explore", "faq", "features", "help", "home", "jobs", "join", "legal", "login", "logout",
            "new", "news", "notifications", "pricing", "privacy", "search", "security", "settings", "share",
            "signin", "signup", "site", "static", "status", "store", "support", "terms", "tos", "trending", "watch")

# community dataset categories / tags -> registry categories
WMN_CATEGORIES = {"social": "social", "gaming": "gaming", "tech": "developer", "hobby": "other", "coding": "developer",
                  "xx NSFW xx": "adult", "misc": "other", "finance": "other", "business": "directories", "music": "music",
                  "shopping": "marketplaces", "images": "photography", "blog": "writing", "art": "art", "dating": "dating",
                  "political": "social", "video": "video", "news": "other", "archived": "reference", "health": "fitness"}
MAIGRET_TAGS = (("porn", "adult"), ("erotic", "adult"), ("webcam", "adult"), ("dating", "dating"), ("q&a", "qa"),
                ("freelance", "freelance"), ("career", "jobs"), ("professional", "professional"), ("coding", "developer"),
                ("hacking", "developer"), ("tech", "developer"), ("mastodon", "microblogging"), ("lemmy", "forums"),
                ("forum", "forums"), ("discussion", "forums"), ("messaging", "messaging"), ("streaming", "video"),
                ("video", "video"), ("music", "music"), ("photo", "photography"), ("art", "art"), ("design", "art"),
                ("blog", "writing"), ("writing", "writing"), ("books", "books"), ("reading", "books"),
                ("movies", "film"), ("anime", "film"), ("travel", "travel"), ("maps", "travel"), ("geosocial", "travel"),
                ("shopping", "marketplaces"), ("classified", "marketplaces"), ("review", "reviews"),
                ("education", "education"), ("science", "academic"), ("research", "academic"), ("sport", "fitness"),
                ("gaming", "gaming"), ("links", "identity"), ("wiki", "reference"), ("social", "social"),
                ("networking", "social"))


@dataclass
class Platform:
    id: str
    name: str
    category: str
    url: str
    profile_url: str = ""                     # template with {username}
    hosts: tuple[str, ...] = ()
    url_patterns: tuple[str, ...] = ()        # regexes on "host/path" capturing the username
    username_pattern: str = r"^[A-Za-z0-9._-]{1,64}$"
    case_sensitive: bool = False
    api: str = "none"                         # none | public | key | paid | restricted
    api_requirements: str = ""                # what the official API needs (developer account, app review…)
    public_profile: str = "yes"               # yes | partial | login_wall | none
    public_search: str = "no"                 # can anyone search people on the platform without logging in? yes | limited | no
    private_accounts: bool = False            # the platform has private accounts (never accessed)
    search_methods: tuple[str, ...] = ("username",)
    extraction: tuple[str, ...] = ()
    auth: str = "none"
    rate_limit: str = ""
    countries: tuple[str, ...] = ("*",)
    region: str = ""                          # e.g. "China", "Russia & CIS" (free text, for display)
    check: str = "whatsmyname"
    status: str = "active"                    # active | defunct | restricted
    status_note: str = ""
    notes: str = ""
    tags: tuple[str, ...] = ()
    probe: dict = field(default_factory=dict)  # declarative public JSON endpoint (check = "probe")
    source: str = "curated"                   # curated | whatsmyname | maigret | sherlock | user
    datasets: tuple[str, ...] = ()            # community datasets that also list it
    dataset_ids: tuple[str, ...] = ()         # their ids (wmn:x, maigret:x, sherlock:x) — check results use them
    dataset_valid: bool = True                # the dataset's rule is marked valid / not disabled
    mirror_of: str = ""                       # third-party viewer/mirror of another platform (Maigret "source")
    _compiled: list = field(default_factory=list, repr=False)
    _query_rules: list = field(default_factory=list, repr=False)   # (host, path, parameter) for ?user={username} templates

    def profile(self, username: str) -> str | None:
        return self.profile_url.replace("{username}", quote(username, safe="")) if self.profile_url else None

    def valid_username(self, username: str) -> bool:
        try:
            return re.fullmatch(self.username_pattern, username) is not None
        except re.error:
            return True

    @property
    def automated(self) -> bool:
        return self.check not in ("manual", "none") and self.status != "defunct"

    @property
    def regional(self) -> bool:
        return tuple(self.countries) != ("*",)

    def to_dict(self) -> dict:
        d = asdict(self)
        d.pop("_compiled", None)
        d.pop("_query_rules", None)
        d["category_title"] = PLATFORM_CATEGORIES.get(self.category, self.category)
        d["automated"] = self.automated
        d["regional"] = self.regional
        return d


def _derive_pattern(template: str) -> str | None:
    """'https://www.example.com/@{username}' -> '^example\\.com/@([^/?#]{1,64})/?$' (with reserved words excluded)."""
    if "{username}" not in template:
        return None
    parts = urlsplit(template.replace("{username}", "USERNAMEPLACEHOLDER"))
    host = (parts.hostname or "").lower()
    if host.startswith("www."):
        host = host[4:]
    if host.startswith("usernameplaceholder."):
        rest = re.escape(host[len("usernameplaceholder."):])
        return rf"^([a-z0-9-]{{1,63}})\.{rest}/?$"
    path = parts.path.rstrip("/")
    if "USERNAMEPLACEHOLDER" not in path or parts.query:
        return None
    before, after = path.split("USERNAMEPLACEHOLDER", 1)
    guard = rf"(?!(?:{'|'.join(RESERVED)})(?:/|$))" if before == "/" else ""
    return rf"^{re.escape(host)}{re.escape(before)}{guard}([A-Za-z0-9._%-]{{1,64}}){re.escape(after)}/?$"


def _derive_query_rule(template: str) -> tuple[str, str, str] | None:
    """'https://example.com/profile.php?user={username}' -> ('example.com', '/profile.php', 'user'): profile templates whose
    username sits in a query parameter are recognised by host, path and that parameter (path patterns cannot express
    them)."""
    if "{username}" not in template:
        return None
    parts = urlsplit(template.replace("{username}", "USERNAMEPLACEHOLDER"))
    if "USERNAMEPLACEHOLDER" in (parts.path or "") or "USERNAMEPLACEHOLDER" not in (parts.query or ""):
        return None
    for key, value in parse_qsl(parts.query, keep_blank_values=True):
        if value == "USERNAMEPLACEHOLDER" and key:
            host = (parts.hostname or "").lower()
            return (host[4:] if host.startswith("www.") else host), (parts.path or "/").rstrip("/") or "/", key
    return None


def _host(url: str) -> str:
    h = (urlsplit(url.replace("{username}", "x")).hostname or "").lower()
    return h[4:] if h.startswith("www.") else h


# The investigator's own platforms (registry/catalog.py) for the code running now. An API request or an investigation
# sets them, so recognising a profile URL (pasted input, links found on pages, identity resolution) knows them without
# every caller passing them along. Context-local: concurrent requests and two databases in one process never mix.
_USER_PLATFORMS: ContextVar[tuple] = ContextVar("sextant_user_platforms", default=())


@contextmanager
def user_platforms_in_scope(platforms):
    token = _USER_PLATFORMS.set(tuple(platforms))
    try:
        yield
    finally:
        _USER_PLATFORMS.reset(token)


def user_platforms() -> tuple:
    return _USER_PLATFORMS.get()


def from_user(d: dict) -> Platform:
    """A platform the investigator added (registry/catalog.py). It is never automated: with a profile-URL template it
    becomes a manual-check link for found usernames, without one it is listed only."""
    p = Platform(id=d["id"], name=d["name"], category=d["category"], url=d["url"], profile_url=d.get("profile_url") or "",
                 hosts=(d.get("host") or _host(d["url"]),), countries=tuple(d.get("countries") or ()) or ("*",),
                 region=d.get("region") or "", check="manual" if d.get("profile_url") else "none",
                 status=d.get("status") or "active", notes=d.get("notes") or "",
                 public_profile=d.get("public_profile") or "yes", source="user")
    derived = _derive_pattern(p.profile_url) if p.profile_url else None
    p._compiled = [re.compile(derived, re.I)] if derived else []
    rule = _derive_query_rule(p.profile_url) if p.profile_url else None
    p._query_rules = [rule] if rule else []
    return p


def _load_file(path: Path) -> list[Platform]:
    with open(path, "rb") as fh:
        doc = tomllib.load(fh)
    out = []
    for raw in doc.get("platform", []):
        kw = {k: v for k, v in raw.items() if k in Platform.__dataclass_fields__ and not k.startswith("_")}
        for k in ("hosts", "url_patterns", "search_methods", "extraction", "countries", "tags"):
            if k in kw:
                kw[k] = tuple(kw[k])
        kw["category"] = LEGACY_CATEGORIES.get(kw.get("category", ""), kw.get("category", "other"))
        p = Platform(**kw)
        if p.category not in PLATFORM_CATEGORIES:
            raise ValueError(f"{path.name}: {p.id}: unknown category {p.category!r}")
        if p.status not in SERVICE_STATUSES:
            raise ValueError(f"{path.name}: {p.id}: unknown status {p.status!r}")
        if p.check == "probe" and not p.probe.get("url"):
            raise ValueError(f"{path.name}: {p.id}: check = 'probe' needs [platform.probe] url")
        if not p.hosts:
            p.hosts = (_host(p.url),)
        pats = list(p.url_patterns)
        if not pats and p.profile_url:
            derived = _derive_pattern(p.profile_url)
            if derived:
                pats.append(derived)
        p._compiled = [re.compile(rx, re.I) for rx in pats]
        rule = _derive_query_rule(p.profile_url) if p.profile_url and not pats else None
        p._query_rules = [rule] if rule else []
        out.append(p)
    return out


@lru_cache(maxsize=1)
def _imported_raw() -> dict[str, dict]:
    out = {}
    for name in ("whatsmyname", "sherlock", "maigret"):
        try:
            out[name] = json.loads((DATA / "imported" / f"{name}.json").read_text(encoding="utf-8"))
        except (OSError, ValueError):
            out[name] = {}
    return out


@lru_cache(maxsize=1)
def curated() -> tuple[Platform, ...]:
    out: list[Platform] = []
    seen: set[str] = set()
    for file in sorted(DATA.glob("*.toml")):
        for p in _load_file(file):
            if p.id in seen:
                raise ValueError(f"Duplicate platform id {p.id!r} in {file.name}")
            seen.add(p.id)
            out.append(p)
    # annotate with the community datasets that cover the same host
    host_index = {h: p for p in out for h in p.hosts}
    for ds, doc in _imported_raw().items():
        for item in doc.get("items", []):
            p = _match_host(item.get("host", ""), host_index)
            if p is None:
                continue
            p.datasets = tuple(dict.fromkeys([*p.datasets, ds]))
            p.dataset_ids = tuple(dict.fromkeys([*p.dataset_ids, item["id"]]))
            if ds == "whatsmyname" and p.check == "whatsmyname":
                p.dataset_valid = bool(item.get("valid", True))
    return tuple(out)


def _match_host(host: str, index: dict) -> Platform | None:
    host = host[4:] if host.startswith("www.") else host
    while host:
        if host in index:
            return index[host]
        if "." not in host:
            return None
        host = host.split(".", 1)[1]
        if host.count(".") < 1:
            return index.get(host)
    return None


def _category_for(source: str, item: dict) -> str:
    if source == "whatsmyname":
        return WMN_CATEGORIES.get(item.get("category", ""), "other")
    tags = set(item.get("tags") or ([item.get("category")] if item.get("category") else []))
    for tag, cat in MAIGRET_TAGS:
        if tag in tags:
            return cat
    return "other"


@lru_cache(maxsize=1)
def imported() -> tuple[Platform, ...]:
    """Platforms known only from the community datasets, deduplicated by host (one entry per site)."""
    curated_index = {h: p for p in curated() for h in p.hosts}
    by_host: dict[str, Platform] = {}
    for ds in ("whatsmyname", "maigret", "sherlock"):
        for item in _imported_raw()[ds].get("items", []):
            host = item.get("host") or ""
            if not host or _match_host(host, curated_index) is not None:
                continue
            cur = by_host.get(host)
            if cur is not None:
                cur.datasets = tuple(dict.fromkeys([*cur.datasets, ds]))
                cur.dataset_ids = tuple(dict.fromkeys([*cur.dataset_ids, item["id"]]))
                if item.get("countries") and cur.countries == ("*",):
                    cur.countries = tuple(item["countries"])
                continue
            by_host[host] = Platform(
                id=item["id"], name=item.get("name") or host, category=_category_for(ds, item), url=f"https://{host}",
                profile_url=item.get("profile_url", ""), hosts=(host,), check=ds, source=ds, datasets=(ds,),
                dataset_ids=(item["id"],), dataset_valid=bool(item.get("valid", True)),
                countries=tuple(item.get("countries") or ()) or ("*",), tags=tuple(item.get("tags") or ()),
                username_pattern=item.get("username_pattern") or r"^[A-Za-z0-9._-]{1,64}$",
                mirror_of=item.get("mirror_of") or "")
    return tuple(by_host.values())


def imported_provenance() -> dict:
    return {ds: {k: doc.get(k) for k in ("source_url", "license", "retrieved_at")} | {"items": len(doc.get("items", []))}
            for ds, doc in _imported_raw().items() if doc}


@lru_cache(maxsize=1)
def by_id() -> dict[str, Platform]:
    out = {p.id: p for p in imported()}
    for p in imported():
        for i in p.dataset_ids:
            out.setdefault(i, p)
    for p in curated():
        out[p.id] = p
        for i in p.dataset_ids:
            out.setdefault(i, p)
    return out


def get(platform_id: str) -> Platform | None:
    return by_id().get(platform_id) or next((p for p in _USER_PLATFORMS.get() if p.id == platform_id), None)


def title(platform_id: str) -> str:
    p = get(platform_id)
    return p.name if p else platform_id


def platform_for_host(host: str) -> Platform | None:
    host = (host or "").lower()
    if host.startswith("www."):
        host = host[4:]
    for p in (*curated(), *_USER_PLATFORMS.get()):          # built-in first: a user platform never shadows one
        if any(host == h or host.endswith("." + h) for h in p.hosts):
            return p
    return None


def parse_profile_url(url: str) -> tuple[Platform, str] | None:
    """Recognises a profile URL anywhere: 'https://www.github.com/jdoe' -> (GitHub, 'jdoe'). Built-in platforms are
    tried first, then the investigator's own (those with a profile-URL template) when they are in scope."""
    try:
        parts = urlsplit(url.strip())
    except (ValueError, AttributeError):
        return None
    host = (parts.hostname or "").lower()
    if host.startswith("www."):
        host = host[4:]
    path = parts.path or "/"
    if len(path) > 1:
        path = path.rstrip("/")
    target = f"{host}{path}" + (f"?{parts.query}" if parts.query and host == "news.ycombinator.com" else "")
    for p in (*curated(), *_USER_PLATFORMS.get()):
        if not any(host == h or host.endswith("." + h) for h in p.hosts):
            continue
        for rx in p._compiled:
            m = rx.match(target)
            if m:
                uname = m.group(1)
                if uname.lower() in RESERVED:
                    continue
                return p, uname
        for rhost, rpath, param in p._query_rules:
            if (host == rhost or host.endswith("." + rhost)) and (path.rstrip("/") or "/") == rpath:
                value = (parse_qs(parts.query).get(param) or [""])[0].strip()
                if value and len(value) <= 100 and p.valid_username(value):
                    return p, value
    return None


def manual_links(username: str, extra: tuple[Platform, ...] = ()) -> list[dict]:
    """Profile links for platforms that must not be automated — for the investigator to open. ``extra``: the
    investigator's own platforms (always manual)."""
    out = []
    for p in (*curated(), *extra):
        if p.check == "manual" and p.status != "defunct" and p.profile_url and p.valid_username(username):
            out.append({"platform": p.id, "title": p.name, "category": p.category, "url": p.profile(username),
                        "reason": p.notes})
    return out


# --------------------------------------------------------------------------- runtime capability

def _adapter(adapters: dict, tool: str, p: Platform | None = None) -> str:
    """What an adapter does for this platform: runs by default, only with SEARCH EVERYTHING (Maigret sites beyond the
    configured top N), is switched off, or is not installed. ``adapters`` holds each tool's connector status plus
    ``maigret_top`` (ids of the sites a normal Maigret run checks)."""
    state = adapters.get(tool)
    covered = adapters.get(f"{tool}_ids")
    if p is not None and covered is not None and not any(i in covered for i in p.dataset_ids):
        return "no_rule"                       # the installed version of the tool has no (allowed) rule for it
    if state is True or state == "available":
        top = adapters.get("maigret_top")
        if tool == "maigret" and p is not None and top is not None and not any(i in top for i in p.dataset_ids):
            return "search_everything_only"
        return "searchable"
    if state == "disabled":
        return "adapter_off"
    return "adapter_not_installed"


def policy_status(p: Platform, adapters: dict) -> str | None:
    """Site policy for platforms checked by community rules or adapters (same rules as connectors/_policy.py)."""
    from ..connectors._policy import adult, forbidden_reason
    if p.source != "curated" and forbidden_reason(p.name, p.url, p.profile_url, source=p.mirror_of):
        return "excluded"
    if (p.category in ("adult", "dating") or adult(p.tags)) and not adapters.get("include_adult"):
        return "adult_off"
    return None


@lru_cache(maxsize=8)
def tool_sites(tool: str, db_path: str | None, include_adult: bool = False) -> tuple[tuple[str, int], ...]:
    """(dataset id, popularity rank) of the sites an adapter would check: read from the *installed* tool's own
    database when ``db_path`` is given (otherwise from the imported snapshot), minus disabled sites and the sites
    Sextant's policy removes. Counts therefore match what the installed version can really do."""
    from ..connectors._policy import adult, forbidden_reason
    out = []
    if db_path:
        doc = json.loads(Path(db_path).read_text(encoding="utf-8"))
        sites = doc.get("sites", {}) if tool == "maigret" else {k: v for k, v in doc.items() if not k.startswith("$")}
        for name, sd in sites.items():
            sd = sd or {}
            if sd.get("disabled"):
                continue
            tags = sd.get("tags") or (["nsfw"] if sd.get("isNSFW") else [])
            if forbidden_reason(name, sd.get("urlMain"), sd.get("url"), source=sd.get("source")) or \
                    (not include_adult and adult(tags)):
                continue
            out.append((f"{tool}:{name.lower()}", int(sd.get("alexaRank") or 10 ** 12)))
    else:
        for i in _imported_raw()[tool].get("items", []):
            if not i.get("valid") or forbidden_reason(i["name"], i.get("profile_url"), source=i.get("mirror_of")) or \
                    (not include_adult and adult(i.get("tags"))):
                continue
            out.append((i["id"], int(i.get("alexa_rank") or 10 ** 12)))
    return tuple(out)


def maigret_top_ids(n: int, include_adult: bool = False, db_path: str | None = None) -> frozenset:
    """The Maigret sites a normal run checks: allowed sites ranked by Maigret's own popularity rank, first ``n``
    (what Maigret's --top-sites does)."""
    ranked = sorted(tool_sites("maigret", db_path, include_adult), key=lambda x: x[1])
    return frozenset(i for i, _ in ranked[:n])


def search_status(p: Platform, connector_states: dict[str, str], adapters: dict) -> str:
    """What this installation can do with the platform now. ``connector_states``: connector -> status
    (registry.connectors.connector_status); ``adapters``: maigret/sherlock -> their connector status
    ("available" runs by default, "disabled" = installed but off, anything else = not installed)."""
    if p.status == "defunct":
        return "defunct"
    if p.check == "none" or p.public_profile == "none":
        return "not_publicly_searchable"
    if p.check == "manual":
        return "manual"
    if p.check in ("whatsmyname", "maigret", "sherlock"):
        blocked = policy_status(p, adapters)
        if blocked:
            return blocked
    if p.check == "whatsmyname":
        if "whatsmyname" not in p.datasets or not p.dataset_valid:
            return _fallback(p, adapters)
        return "searchable" if connector_states.get("whatsmyname") == "available" else "excluded" \
            if connector_states.get("whatsmyname") == "excluded" else "adapter_not_installed"
    if p.check in ("maigret", "sherlock"):
        if not p.dataset_valid and p.check == "maigret":
            return "no_rule"
        return _adapter(adapters, p.check, p)
    if p.check == "probe":
        return "searchable" if connector_states.get("profile_probes") == "available" else "adapter_not_installed"
    state = connector_states.get(p.check)
    if state == "available":
        return "searchable"
    if state == "needs_credentials":
        return "api_key_required"
    if state == "needs_subscription":
        return "subscription_required"
    if state == "excluded":
        return "excluded"
    if state == "manual":
        return "manual"
    if state == "disabled":
        return "disabled"
    if state == "catalog_only":
        return "catalog_only"
    return _fallback(p, adapters)


def _fallback(p: Platform, adapters: dict) -> str:
    best = "no_rule"
    rank = {"searchable": 4, "search_everything_only": 3, "adapter_off": 2, "adapter_not_installed": 1, "no_rule": 0}
    for tool in ("maigret", "sherlock"):
        if tool in p.datasets:
            st = _adapter(adapters, tool, p)
            if rank[st] > rank[best]:
                best = st
    return best


def catalog(connector_states: dict[str, str], adapters: dict, include_imported: bool = True,
            extra: tuple[Platform, ...] = ()) -> dict:
    """Curated + imported platforms (+ ``extra``, the investigator's own) with their runtime capability, and the counts
    the coverage screens show."""
    rows = []
    for p in (*curated(), *extra):
        d = p.to_dict()
        d["search_status"] = search_status(p, connector_states, adapters)
        rows.append(d)
    imp = imported()
    counts_imported: dict[str, int] = {}
    imported_rows = []
    for p in imp:
        st = search_status(p, connector_states, adapters)
        counts_imported[st] = counts_imported.get(st, 0) + 1
        if include_imported:
            imported_rows.append({"id": p.id, "name": p.name, "category": p.category,
                                  "category_title": PLATFORM_CATEGORIES.get(p.category, p.category), "url": p.url,
                                  "profile_url": p.profile_url, "check": p.check, "source": p.source,
                                  "datasets": list(p.datasets), "countries": list(p.countries), "regional": p.regional,
                                  "search_status": st, "dataset_valid": p.dataset_valid})
    counts_curated: dict[str, int] = {}
    by_cat: dict[str, int] = {}
    for d in rows:
        counts_curated[d["search_status"]] = counts_curated.get(d["search_status"], 0) + 1
    for d in rows + imported_rows:
        by_cat[d["category"]] = by_cat.get(d["category"], 0) + 1

    def bucket(counts: dict[str, int], keys) -> int:
        return sum(v for k, v in counts.items() if k in keys)

    total = {k: counts_curated.get(k, 0) + counts_imported.get(k, 0) for k in SEARCH_STATUS_TITLES}
    return {
        "curated": rows, "imported": imported_rows, "categories": PLATFORM_CATEGORIES,
        "search_status_titles": SEARCH_STATUS_TITLES, "provenance": imported_provenance(),
        "counts": {
            "curated": len(rows) - len(extra), "user": len(extra), "imported": len(imp), "registered": len(rows) + len(imp),
            "searchable": bucket(total, SEARCHABLE), "opt_in": bucket(total, OPT_IN), "api_only": bucket(total, API_ONLY),
            "unavailable": bucket(total, UNAVAILABLE), "by_status": total, "curated_by_status": counts_curated,
            "regional_curated": sum(1 for d in rows if d["regional"]),
            "regional_total": sum(1 for d in rows if d["regional"]) + sum(1 for p in imp if p.regional),
            "by_category": by_cat,
        },
    }


def for_country(code: str, extra: tuple[Platform, ...] = ()) -> list[Platform]:
    """Platforms specific to a country (curated, the investigator's own, imported) — global platforms are not repeated
    per country."""
    code = code.upper()
    return [p for p in (*curated(), *extra, *imported()) if code in p.countries]
