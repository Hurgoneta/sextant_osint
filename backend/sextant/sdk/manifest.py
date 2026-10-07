"""Connector manifests — the declarative half of every connector.

A manifest says what a source is, what it can provide, what it costs, what it needs, which
jurisdictions it belongs to, what it is legally allowed for, and exactly what it receives.
The catalog, coverage matrix, plan/disclosure view and report are all generated from manifests.
Connector #1000 declares exactly the same fields as connector #1.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass

# --------------------------------------------------------------------------- vocabularies

ACCESS_LEVELS: dict[str, dict] = {
    "local":      {"title": "Runs locally", "free": True, "needs_key": False, "paid": False},
    "free":       {"title": "Free — no account needed", "free": True, "needs_key": False, "paid": False},
    "free_key":   {"title": "Free API key / registration required", "free": True, "needs_key": True, "paid": False},
    "restricted": {"title": "Approval required (access is reviewed by the provider)", "free": True, "needs_key": True, "paid": False},
    "metered":    {"title": "Metered API — small allowance, then paid", "free": False, "needs_key": True, "paid": True},
    "paid":       {"title": "Paid subscription required", "free": False, "needs_key": True, "paid": True},
    "enterprise": {"title": "Enterprise contract — contact the provider", "free": False, "needs_key": True, "paid": True},
    "tool":       {"title": "External open-source tool (install separately)", "free": True, "needs_key": False, "paid": False},
    "manual":     {"title": "Manual only — the platform forbids automated access", "free": True, "needs_key": False, "paid": False},
}

# category id -> (title, progress group shown in the live view)
CATEGORIES: dict[str, tuple[str, str]] = {
    "search": ("Search engines", "Search Engines"),
    "social": ("Social media", "Social Platforms"),
    "usernames": ("Username enumeration", "Social Platforms"),
    "forums": ("Forums & communities", "Social Platforms"),
    "developer_platforms": ("Developer platforms", "Social Platforms"),
    "gaming": ("Gaming", "Social Platforms"),
    "music": ("Music", "Social Platforms"),
    "video": ("Video & streaming", "Social Platforms"),
    "blogs": ("Blogs & publishing", "Social Platforms"),
    "marketplaces": ("Marketplaces", "Social Platforms"),
    "professional_networks": ("Professional networks", "Social Platforms"),
    "email": ("Email intelligence", "Email & Phone"),
    "phone": ("Phone intelligence", "Email & Phone"),
    "breach_metadata": ("Breach exposure (metadata)", "Email & Phone"),
    "domains": ("Domain intelligence", "Domains"),
    "dns": ("DNS", "Domains"),
    "rdap": ("RDAP / WHOIS", "Domains"),
    "certificates": ("Certificate transparency", "Domains"),
    "infrastructure": ("Infrastructure (IP / ASN)", "Domains"),
    "technology_detection": ("Technology detection", "Public Websites"),
    "reputation": ("Reputation & threat intel", "Domains"),
    "websites": ("Public websites", "Public Websites"),
    "archives": ("Web archives", "Public Websites"),
    "documents": ("Documents", "Documents"),
    "companies": ("Company registries", "Public Registries"),
    "organizations": ("Organizations & identifiers", "Public Registries"),
    "public_records": ("Public records", "Public Registries"),
    "legal_records": ("Legal & judicial records", "Public Registries"),
    "sanctions": ("Sanctions & watchlists", "Public Registries"),
    "procurement": ("Public procurement", "Public Registries"),
    "publications": ("Publications & research", "Publications"),
    "news": ("News", "Publications"),
    "images": ("Images", "Images & Places"),
    "geolocation": ("Geolocation", "Images & Places"),
    "people_search": ("People search (commercial)", "Commercial Intelligence"),
    "b2b_sales_intelligence": ("B2B sales intelligence", "Commercial Intelligence"),
    "business_contact_data": ("Business contact data", "Commercial Intelligence"),
    "consumer_data": ("Consumer data", "Commercial Intelligence"),
    "identity_resolution": ("Identity resolution", "Commercial Intelligence"),
    "professional_intelligence": ("Professional intelligence", "Commercial Intelligence"),
    "company_intelligence": ("Company intelligence", "Commercial Intelligence"),
    "public_records_aggregation": ("Public records aggregation", "Commercial Intelligence"),
    "contact_enrichment": ("Contact enrichment", "Commercial Intelligence"),
    "marketing_data": ("Marketing data", "Commercial Intelligence"),
    "historical_data": ("Historical data", "Commercial Intelligence"),
    "adapters": ("OSINT framework adapters", "Social Platforms"),
}

RELIABILITY = {
    "official": "Official/authoritative source (government register, platform's own API)",
    "high": "Structured data from the platform or a curated dataset",
    "medium": "Parsed from public pages or community-maintained rules",
    "low": "Search snippets, third-party aggregators, heuristics",
}

KINDS = ("native", "adapter", "catalog")
STATUSES = ("stable", "beta", "sunsetting", "retired", "excluded")


@dataclass(frozen=True)
class Credential:
    key: str                         # settings key, e.g. "GITHUB_TOKEN"
    label: str
    help: str = ""
    secret: bool = True
    required: bool = True            # False = improves results / raises limits but is optional
    obtain_url: str = ""


@dataclass(frozen=True)
class RateLimit:
    per_second: float = 1.0
    burst: int = 2
    note: str = ""                   # the provider's published limit, e.g. "60 requests/hour unauthenticated"


@dataclass(frozen=True)
class Disclosure:
    destinations: tuple[str, ...]
    sends: str                       # human readable, shown before anything is sent
    mode: str = "plaintext"          # plaintext | hashed | none | public | download | tool
    note: str = ""


@dataclass(frozen=True)
class Manifest:
    name: str
    title: str
    category: str
    description: str
    # access & cost
    access: str = "free"
    credentials: tuple[Credential, ...] = ()
    cost: str = "Free"
    cost_per_call_usd: float = 0.0
    rate_limit: RateLimit = RateLimit()
    max_calls_per_investigation: int | None = None
    # jurisdiction & legal
    jurisdictions: tuple[str, ...] = ("GLOBAL",)       # legal home of the source: ISO-2 codes, "EU", "GLOBAL"
    supported_countries: tuple[str, ...] = ("*",)      # whose data it covers ("*" = worldwide)
    legal: str = ""                                    # access requirements / terms that matter
    public_record: bool = False                        # official public-record or government source
    # technical
    official_api: bool = True
    access_method: str = "api"                         # api | web_page | dataset | local | tool
    website: str = ""
    documentation: str = ""
    terms_url: str = ""
    license: str = ""                                  # licence of the data returned
    reliability: str = "medium"
    coverage: str = ""
    capabilities: tuple[str, ...] = ()                 # "This data source can provide: …"
    input_entities: tuple[str, ...] = ()               # filled from the transforms when empty
    output_entities: tuple[str, ...] = ()
    disclosure: Disclosure = Disclosure((), "nothing", "none")
    # behaviour
    default_enabled: bool = True
    auto_run: bool | None = None                       # may auto-investigation call it? None = derive from access
    opt_in_reason: str = ""
    max_concurrency: int = 2
    timeout: float = 120.0
    status: str = "stable"
    status_note: str = ""
    kind: str = "native"
    implemented: bool = True
    tags: tuple[str, ...] = ()
    version: str = "1"

    # ------------------------------------------------------------------ derived
    @property
    def access_info(self) -> dict:
        return ACCESS_LEVELS.get(self.access, ACCESS_LEVELS["free"])

    @property
    def requires_api_key(self) -> bool:
        return any(c.required for c in self.credentials) or (self.access_info["needs_key"] and bool(self.credentials))

    @property
    def requires_subscription(self) -> bool:
        return self.access in ("paid", "enterprise", "metered")

    @property
    def auto_default(self) -> bool:
        if self.auto_run is not None:
            return self.auto_run
        return self.access not in ("paid", "metered", "enterprise", "manual") and self.default_enabled

    @property
    def category_title(self) -> str:
        return CATEGORIES.get(self.category, (self.category, "Other"))[0]

    @property
    def progress_group(self) -> str:
        return CATEGORIES.get(self.category, (self.category, "Other"))[1]

    def to_dict(self) -> dict:
        d = asdict(self)
        d.update({
            "access_title": self.access_info["title"], "requires_api_key": self.requires_api_key,
            "requires_subscription": self.requires_subscription, "category_title": self.category_title,
            "progress_group": self.progress_group, "auto_default": self.auto_default,
            "matrix": {"free": self.access_info["free"] and not self.requires_api_key,
                       "api": self.requires_api_key or self.access in ("free_key", "restricted"),
                       "paid": self.access_info["paid"], "public": self.public_record},
        })
        return d

    def validate(self) -> list[str]:
        """Problems that would make the catalog misleading. Enforced by the test suite."""
        problems = []
        if not self.name or not self.name.replace("_", "").replace("-", "").isalnum():
            problems.append("name must be an identifier")
        if self.category not in CATEGORIES:
            problems.append(f"unknown category {self.category!r}")
        if self.access not in ACCESS_LEVELS:
            problems.append(f"unknown access level {self.access!r}")
        if self.reliability not in RELIABILITY:
            problems.append(f"unknown reliability {self.reliability!r}")
        if self.kind not in KINDS:
            problems.append(f"unknown kind {self.kind!r}")
        if self.status not in STATUSES:
            problems.append(f"unknown status {self.status!r}")
        if not self.description:
            problems.append("description is required")
        if not self.website and "meta" not in self.tags:
            problems.append("website is required (only meta-connectors that query many sites may omit it)")
        if self.access in ("free_key", "metered", "paid", "restricted") and self.implemented and not self.credentials:
            problems.append("credential-gated access must declare credentials")
        if self.implemented and self.access not in ("local", "manual") and not self.disclosure.destinations:
            problems.append("networked connectors must disclose their destinations")
        return problems


def manifest_from_mapping(d: dict) -> Manifest:
    """Builds a manifest from a catalog TOML table (catalog-only sources)."""
    creds = tuple(Credential(**c) for c in d.get("credentials", []) or [])
    rl = RateLimit(**d["rate_limit"]) if isinstance(d.get("rate_limit"), dict) else RateLimit()
    disc = Disclosure(**{**d["disclosure"], "destinations": tuple(d["disclosure"].get("destinations", ()))}) \
        if isinstance(d.get("disclosure"), dict) else Disclosure((), "nothing", "none")
    tuple_fields = ("jurisdictions", "supported_countries", "capabilities", "input_entities", "output_entities", "tags")
    plain = {k: v for k, v in d.items() if k in Manifest.__dataclass_fields__
             and k not in ("credentials", "rate_limit", "disclosure")}
    for k in tuple_fields:
        if k in plain and isinstance(plain[k], list):
            plain[k] = tuple(plain[k])
    plain.setdefault("kind", "catalog")
    plain.setdefault("implemented", False)
    return Manifest(credentials=creds, rate_limit=rl, disclosure=disc, **plain)


__all__ = ["ACCESS_LEVELS", "CATEGORIES", "RELIABILITY", "Credential", "RateLimit", "Disclosure", "Manifest",
           "manifest_from_mapping"]
