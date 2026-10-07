"""Ontology — the vocabulary every connector, transform, resolver and view shares.

Three families of entity types:

* **Selectors** (name, email, phone, username, domain, ip, asn): plain identifiers. They are
  deduplicated globally by their normalized value. Two observations of ``john@example.com``
  are the same node, whoever reported them.
* **Objects** (account, website, document, publication, public_record, breach, image, organization,
  location): things in the world. Each has a stable identity key built from what identifies it
  (platform + handle, canonical URL, DOI, registry + number, …).
* **Identities** (person): deliberately *never* keyed by name. A name is a selector that many people
  share. A person node is either the investigation's target or a person described by one specific
  record (``person:<connector>:<record id>``). Deciding that two of them are the same human is the
  job of entity resolution and of the investigator. Normalization never decides it.

The registry is data, so connector packages can register new types (``register_entity_type``).
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Callable

from .util.normalize import (
    canonical_url, fold, location_key, name_key, normalize_asn, normalize_domain, normalize_email, normalize_ip,
    normalize_phone, normalize_username,
)


@dataclass(frozen=True)
class Prop:
    name: str
    title: str
    kind: str = "text"            # text | url | email | phone | date | int | float | bool | list | json | image
    identifying: bool = False     # used as evidence by entity resolution
    local_only: bool = False      # never sent to any external source (e.g. year of birth)


@dataclass(frozen=True)
class EntityType:
    id: str
    title: str
    plural: str
    family: str                   # selector | object | identity | meta
    group: str                    # UI grouping: identity | contact | online | web | infrastructure | records | content | place
    shape: str                    # Cytoscape node shape
    color: str
    description: str
    properties: tuple[Prop, ...] = ()
    resolvable: bool = False      # resolution assesses "does this pertain to the target?"
    expandable: bool = True       # may be the input of transforms
    normalize: Callable[[str], str | None] | None = None

    def prop(self, name: str) -> Prop | None:
        return next((p for p in self.properties if p.name == name), None)

    def to_dict(self) -> dict:
        return {
            "id": self.id, "title": self.title, "plural": self.plural, "family": self.family, "group": self.group,
            "shape": self.shape, "color": self.color, "description": self.description, "resolvable": self.resolvable,
            "expandable": self.expandable,
            "properties": [{"name": p.name, "title": p.title, "kind": p.kind, "identifying": p.identifying,
                            "local_only": p.local_only} for p in self.properties],
        }


@dataclass(frozen=True)
class RelationType:
    id: str
    title: str                    # "uses"
    inverse: str                  # "used by"
    description: str
    family: str = "structural"    # structural | identity | association
    source_types: tuple[str, ...] = ()   # informational: typical endpoints
    target_types: tuple[str, ...] = ()

    def to_dict(self) -> dict:
        return {"id": self.id, "title": self.title, "inverse": self.inverse, "description": self.description,
                "family": self.family, "source_types": list(self.source_types), "target_types": list(self.target_types)}


def _free_key(raw: str) -> str | None:
    """Keys built by connectors themselves ('github:jdoe', 'gb-coh:01234567'): trimmed, length-capped."""
    s = (raw or "").strip()
    return s[:400] if s else None


def _account_key(raw: str) -> str | None:
    platform, _, handle = (raw or "").partition(":")
    platform, handle = platform.strip().lower(), handle.strip()
    if not platform or not handle:
        return None
    return f"{platform}:{handle.casefold()}"[:300]


def _username_key(raw: str) -> str | None:
    u = normalize_username(raw)
    return u.casefold() if u else None


def _publication_key(raw: str) -> str | None:
    s = (raw or "").strip()
    low = s.lower()
    for prefix in ("https://doi.org/", "http://doi.org/", "https://dx.doi.org/", "doi:"):
        if low.startswith(prefix):
            return "doi:" + low[len(prefix):]
    if low.startswith("10.") and "/" in low:
        return "doi:" + low
    if "://" in s:
        c = canonical_url(s)
        return f"url:{c}" if c else None
    return _free_key(s)


def _image_key(raw: str) -> str | None:
    s = (raw or "").strip().lower()
    if len(s) == 16 and all(c in "0123456789abcdef" for c in s):
        return s
    return _free_key(s)


P = Prop
ENTITY_TYPES: dict[str, EntityType] = {}


def register_entity_type(t: EntityType) -> EntityType:
    if t.id in ENTITY_TYPES:
        raise ValueError(f"Entity type {t.id!r} already registered")
    ENTITY_TYPES[t.id] = t
    return t


for _t in (
    # ------------------------------------------------------------------ identities
    EntityType(
        "person", "Person", "People", "identity", "identity", "star", "#f4b740",
        "A human being. Never deduplicated by name: either the target of the investigation or the person "
        "described by one specific record.",
        (P("full_name", "Full name", identifying=True), P("aliases", "Aliases", "list", identifying=True),
         P("birth_year", "Year of birth", "int", identifying=True, local_only=True),
         P("birth_date", "Date of birth (as published)", "date", identifying=True),
         P("nationality", "Nationality", identifying=True), P("occupation", "Occupation", identifying=True),
         P("employer", "Employer", identifying=True), P("location", "Location", identifying=True),
         P("role", "Role in record"), P("description", "Description")),
        resolvable=True, normalize=_free_key),
    EntityType(
        "organization", "Organization", "Organizations", "object", "identity", "hexagon", "#c084fc",
        "A company, institution or group. Keyed by registry identifier when known, otherwise by normalized "
        "name + jurisdiction.",
        (P("name", "Name", identifying=True), P("legal_form", "Legal form"), P("jurisdiction", "Jurisdiction"),
         P("registry", "Registry"), P("registration_number", "Registration number", identifying=True),
         P("lei", "LEI", identifying=True), P("status", "Status"), P("incorporated", "Incorporated", "date"),
         P("dissolved", "Dissolved", "date"), P("address", "Registered address"), P("industry", "Industry"),
         P("website", "Website", "url", identifying=True), P("employees", "Employees", "int")),
        resolvable=True, normalize=_free_key),
    # ------------------------------------------------------------------ selectors
    EntityType("name", "Name", "Names", "selector", "identity", "tag", "#fcd34d",
               "A personal-name string as written. Equal names are not equal people.",
               (P("variants", "Spellings seen", "list"),), normalize=name_key),
    EntityType("email", "Email address", "Email addresses", "selector", "contact", "round-rectangle", "#38bdf8",
               "An email address (lower-cased; provider canonical form kept as a property).",
               (P("provider", "Provider"), P("free_provider", "Free provider", "bool"),
                P("provider_canonical", "Provider canonical form"), P("mx", "Mail servers", "list"),
                P("public_commits", "Public commits", "int")), normalize=normalize_email),
    EntityType("phone", "Phone number", "Phone numbers", "selector", "contact", "round-rectangle", "#22d3ee",
               "A phone number in E.164 form.",
               (P("international", "International format"), P("national", "National format"),
                P("region", "Country (ISO)"), P("region_name", "Allocated region"), P("carrier", "Original carrier"),
                P("number_type", "Line type"), P("valid", "Valid", "bool"), P("timezones", "Time zones", "list")),
               normalize=lambda v: normalize_phone(v)),
    EntityType("username", "Username", "Usernames", "selector", "online", "round-rectangle", "#a3e635",
               "A handle as used on online platforms (case-insensitive key).",
               (P("username", "As written"), P("uniqueness", "Estimated uniqueness", "float"),
                P("derived_from", "Derived from"), P("hypothesis", "Generated guess (not evidence)", "bool")),
               normalize=_username_key),
    EntityType("domain", "Domain", "Domains", "selector", "infrastructure", "diamond", "#34d399",
               "A DNS name (registered domain or subdomain).",
               (P("registrar", "Registrar"), P("registered", "Registered", "date"), P("expires", "Expires", "date"),
                P("nameservers", "Name servers", "list"), P("dns", "DNS records", "json"),
                P("registrant", "Public registrant data", "json"), P("site_title", "Site title"),
                P("technologies", "Technologies", "list"), P("subdomain", "Is subdomain", "bool"),
                P("certificate_count", "Certificates", "int"), P("first_certificate", "First certificate", "date")),
               normalize=normalize_domain),
    EntityType("ip", "IP address", "IP addresses", "selector", "infrastructure", "diamond", "#2dd4bf",
               "An IPv4/IPv6 address.",
               (P("asn", "ASN"), P("network", "Network"), P("org", "Network owner"), P("country", "Country"),
                P("ports", "Open ports (passive)", "list"), P("hostnames", "Hostnames", "list"),
                P("cpes", "Software (CPE)", "list"), P("tags", "Tags", "list")), normalize=normalize_ip),
    EntityType("asn", "Autonomous system", "Autonomous systems", "selector", "infrastructure", "diamond", "#14b8a6",
               "An autonomous system (network operator).",
               (P("name", "Name"), P("country", "Country"), P("registry", "Registry")), normalize=normalize_asn),
    # ------------------------------------------------------------------ online objects
    EntityType("account", "Online account", "Online accounts", "object", "online", "round-rectangle", "#60a5fa",
               "An account on a platform (social network, forum, code host, game…). Existing does not mean "
               "it belongs to the target.",
               (P("platform", "Platform"), P("platform_title", "Platform name"), P("category", "Platform category"),
                P("username", "Username", identifying=True), P("user_id", "Platform user id", identifying=True),
                P("display_name", "Display name", identifying=True), P("bio", "Bio"),
                P("location", "Location", identifying=True), P("website", "Website", "url", identifying=True),
                P("links", "Links", "list", identifying=True), P("emails", "Public emails", "list", identifying=True),
                P("phones", "Public phones", "list", identifying=True), P("company", "Company", identifying=True),
                P("created_at", "Created", "date"), P("followers", "Followers", "int"),
                P("profile_url", "Profile URL", "url"), P("avatar_url", "Avatar URL", "url"),
                P("avatar_fp", "Avatar fingerprint", "json", identifying=True), P("avatar_thumb", "Avatar", "image"),
                P("verified_links", "Verified links", "list", identifying=True), P("extra", "Extra", "json")),
               resolvable=True, normalize=_account_key),
    EntityType("website", "Web page", "Web pages", "object", "web", "round-tag", "#94a3b8",
               "A web page or site URL: search results, linked pages, news articles, archived snapshots.",
               (P("url", "URL", "url"), P("title", "Title"), P("snippet", "Snippet"), P("host", "Host"),
                P("category", "Category"), P("published", "Published", "date"), P("matched", "Matched identifiers", "list"),
                P("queries", "Search queries", "list"), P("language", "Language")),
               resolvable=True, normalize=canonical_url),
    EntityType("document", "Document", "Documents", "object", "content", "round-tag", "#cbd5e1",
               "A public document (PDF, office file, filing). Never downloaded automatically.",
               (P("url", "URL", "url"), P("title", "Title"), P("file_type", "File type"), P("snippet", "Snippet"),
                P("host", "Host"), P("published", "Published", "date"), P("matched", "Matched identifiers", "list")),
               resolvable=True, normalize=canonical_url),
    EntityType("publication", "Publication", "Publications", "object", "content", "rectangle", "#f9a8d4",
               "A scholarly, official or press publication (DOI, ORCID work, filing).",
               (P("title", "Title"), P("doi", "DOI"), P("year", "Year", "int"), P("venue", "Venue"),
                P("authors", "Authors", "list"), P("url", "URL", "url"), P("kind", "Kind")),
               resolvable=True, normalize=_publication_key),
    EntityType("image", "Image", "Images", "object", "content", "ellipse", "#fb7185",
               "An image identified by its perceptual hash (a profile photo or a photo supplied by the investigator).",
               (P("phash", "pHash"), P("dhash", "dHash"), P("thumb", "Thumbnail", "image"),
                P("example_url", "Seen at", "url"), P("flat", "Blank/default image", "bool")), normalize=_image_key),
    # ------------------------------------------------------------------ records
    EntityType("public_record", "Public record", "Public records", "object", "records", "vee", "#f87171",
               "An entry in an official public source: court case, judgment, sanctions or wanted list, company "
               "filing, procurement award, gazette notice, licence. A name on a record is not an identity.",
               (P("record_type", "Record type"), P("jurisdiction", "Jurisdiction"), P("authority", "Authority / court"),
                P("official_url", "Official URL", "url"), P("reference", "Reference / case number"),
                P("date", "Date", "date"), P("title", "Title"), P("category", "Category"),
                P("matched_name", "Name on record", identifying=True), P("name_match", "Name match"),
                P("role", "Role of the named party"), P("birth_years", "Years of birth on record", "list", identifying=True),
                P("nationality", "Nationality on record", identifying=True), P("criminal", "Criminal matter", "bool"),
                P("status", "Status"), P("summary", "Summary"), P("list_name", "List")),
               resolvable=True, normalize=_free_key),
    EntityType("breach", "Breach exposure", "Breach exposures", "object", "records", "octagon", "#ef4444",
               "A data breach or info-stealer exposure (metadata only — never credentials).",
               (P("title", "Title"), P("breach_date", "Breach date", "date"), P("data_classes", "Data exposed", "list"),
                P("records", "Records", "int"), P("verified", "Verified", "bool"), P("domain", "Breached site"),
                P("description", "Description")), normalize=lambda v: fold(v).strip()[:200] or None),
    EntityType("location", "Location", "Locations", "object", "place", "triangle", "#fdba74",
               "A place as written in a source, optionally geocoded.",
               (P("text", "As written"), P("city", "City"), P("region", "Region"), P("country", "Country"),
                P("country_code", "Country code"), P("lat", "Latitude", "float"), P("lon", "Longitude", "float"),
                P("kind", "Kind")), normalize=location_key),
    EntityType("source", "Source", "Sources", "meta", "meta", "rectangle", "#64748b",
               "Where an observation came from (shown in the graph only on request).",
               (P("connector", "Connector"), P("url", "URL", "url"), P("retrieved_at", "Retrieved", "date")),
               expandable=False, normalize=_free_key),
):
    register_entity_type(_t)


R = RelationType
RELATION_TYPES: dict[str, RelationType] = {r.id: r for r in (
    R("USES", "uses", "used by", "An identity or account uses an identifier (email, phone, username, domain).",
      "structural", ("person", "account", "organization"), ("email", "phone", "username", "domain")),
    R("OWNS", "owns", "owned by", "Ownership/control: an identity owns an account, domain or asset. Only asserted "
      "from explicit evidence or an investigator's confirmation.", "identity",
      ("person", "organization"), ("account", "domain", "website", "phone", "email")),
    R("AUTHORED", "authored", "authored by", "Authorship of a document, publication, post or commit.",
      "structural", ("person", "account", "organization"), ("document", "publication", "website")),
    R("MENTIONED_IN", "mentioned in", "mentions", "An identifier or name appears in a page or document.",
      "structural", (), ("website", "document", "publication")),
    R("LINKED_TO", "links to", "linked from", "A hyperlink, rel=me link or profile field pointing at another "
      "account, page or domain.", "structural", ("account", "website", "domain"), ("account", "website", "domain")),
    R("LOCATED_IN", "located in", "location of", "Stated or registered location.", "structural", (), ("location",)),
    R("WORKS_FOR", "works for", "employs", "Employment or professional affiliation as stated in a source.",
      "structural", ("person", "account"), ("organization",)),
    R("MEMBER_OF", "member of", "has member", "Officer, director, partner, board or group membership "
      "(the role is recorded on the relationship).", "structural", ("person",), ("organization",)),
    R("APPEARS_ON", "appears on", "lists", "A name, identifier or image appears on a record, list or page.",
      "structural", ("name", "username", "email", "image", "person"), ("public_record", "website", "breach")),
    R("ASSOCIATED_WITH", "associated with", "associated with", "A weaker association reported by a source "
      "(co-occurrence, related entity).", "association"),
    R("REGISTERED_TO", "registered to", "registrant of", "Registration data: a domain or network registered to an "
      "organization, person, email or AS.", "structural", ("domain", "ip", "asn"), ("organization", "person", "email", "asn")),
    R("REFERENCED_BY", "referenced by", "references", "Referenced by an archive snapshot, citation or index.",
      "structural"),
    R("RESOLVES_TO", "resolves to", "resolved from", "DNS resolution.", "structural", ("domain",), ("ip",)),
    R("HAS_SUBDOMAIN", "has subdomain", "subdomain of", "DNS hierarchy.", "structural", ("domain",), ("domain",)),
    R("EXPOSED_IN", "exposed in", "exposed", "An identifier is included in a breach or info-stealer dataset "
      "(metadata only).", "structural", ("email", "username", "domain", "phone"), ("breach",)),
    R("HAS_IMAGE", "has image", "image of", "Profile photo or image shown by an account or page.", "structural",
      ("account", "website", "person"), ("image",)),
    R("HAS_NAME", "has name", "name of", "A person, account or record carries this name string.", "structural",
      ("person", "account", "public_record", "organization"), ("name",)),
    R("SAME_AS", "possibly same as", "possibly same as", "Candidate equivalence produced by entity resolution, "
      "with confidence and evidence. Never merges automatically.", "identity"),
    R("ANNOUNCED_IN", "announced in", "announces", "Official notice concerning an organization or person "
      "(gazette, procurement award, filing).", "structural", ("organization", "person"), ("public_record", "publication")),
)}


def entity_type(type_id: str) -> EntityType:
    try:
        return ENTITY_TYPES[type_id]
    except KeyError:
        raise ValueError(f"Unknown entity type {type_id!r}") from None


def normalize_value(type_id: str, raw: str) -> str | None:
    """Canonical identity value for an entity of ``type_id`` (None = invalid for that type)."""
    t = entity_type(type_id)
    if raw is None:
        return None
    raw = str(raw)
    return t.normalize(raw) if t.normalize else _free_key(raw)


def make_key(type_id: str, value: str) -> str:
    return f"{type_id}:{value}"


def split_key(key: str) -> tuple[str, str]:
    t, _, v = key.partition(":")
    return t, v


def ontology_payload() -> dict:
    return {"entity_types": [t.to_dict() for t in ENTITY_TYPES.values()],
            "relation_types": [r.to_dict() for r in RELATION_TYPES.values()]}


__all__ = ["ENTITY_TYPES", "RELATION_TYPES", "EntityType", "RelationType", "Prop", "entity_type", "normalize_value",
           "make_key", "split_key", "register_entity_type", "ontology_payload"]

