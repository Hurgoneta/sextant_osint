"""Identity resolution and intelligence ranking.

Model: a transparent log-likelihood sum, in the spirit of Fellegi–Sunter record linkage.

    confidence = sigmoid(prior[type] + Σ feature weights)

* Every weight is shown next to the evidence that produced it ("+4.0 profile shows the target's
  email", "−1.2 different name on profile"). The weights are judgement calls collected in one table
  (``Weights``) so they can be reviewed and tuned. They are not calibrated probabilities, and the UI
  says so.
* **Contradictions** (negative weights) are kept and displayed apart from supporting evidence.
* **Conservative by construction.** A name alone never makes a "likely" match. Public records start
  from a low prior and a name-only match stays a *possible name match*. Nothing is ever confirmed
  automatically; only the investigator confirms.
* **Iterative.** Accounts that reach high confidence through strong evidence become *anchors*, so
  "links to the target's GitHub" can count. Investigator decisions pin values and become anchors too.

Ranking (section 17 of the brief): every entity also gets a **tier** (high/medium/low) and an
**investigative value** that drives the order in which the engine expands the graph.
"""

from __future__ import annotations

import math
from dataclasses import dataclass

from ..model import Entity, Graph, Reason, RelEvidence, Source
from ..ontology import ENTITY_TYPES
from ..registry.platforms import parse_profile_url
from ..util.imagehash import MATCH_SIMILARITY, photo_similarity
from ..util.normalize import (
    canonical_url, fold, location_tokens, name_similarity, name_tokens, org_name_key, registered_domain, url_host,
    username_uniqueness,
)
from .profile import ANCHOR_CONFIDENCE, TargetProfile, build_profile

MODEL_VERSION = "identity-v1"


@dataclass(frozen=True)
class Weights:
    # strong identifiers
    email: float = 4.0
    phone: float = 3.5
    verified_link_to_anchor: float = 3.0
    link_to_anchor: float = 2.5
    linked_from_anchor: float = 2.5
    link_to_target_domain: float = 2.5
    link_to_email_domain: float = 1.0
    user_photo: float = 3.0
    anchor_avatar: float = 2.0
    registration_id: float = 5.0
    # medium
    username_base: float = 0.3
    username_unique: float = 1.7
    name_full: float = 1.5
    name_contained: float = 1.2
    name_fuzzy: float = 0.8
    name_surname: float = 0.5
    name_first: float = 0.2
    name_mismatch: float = -1.2
    city: float = 0.8
    country: float = 0.3
    location_mismatch: float = -0.6
    employer_same: float = 0.8
    employer_diff: float = -0.3
    occupation_same: float = 0.4
    occupation_diff: float = -0.3
    birth_year_same: float = 2.0
    birth_year_diff: float = -3.0
    nationality_same: float = 0.3
    nationality_diff: float = -0.5
    anchor_display_name: float = 0.8
    bio_identifier: float = 1.0
    search_rank: float = 0.3                 # a top result for a platform search of the name: ranking, not proof
    corroborated_link: float = 1.2           # cross-link with another account using the same target handle
    corroborated_verified: float = 2.0       # … through a verified link (rel=me, cryptographic proof)
    # pages / documents (text mentions)
    m_email: float = 4.0
    m_phone: float = 3.5
    m_domain: float = 2.0
    m_name: float = 0.8
    m_name_partial: float = 0.2
    m_location_with_name: float = 1.0
    m_nothing: float = -0.5
    # public records: a name is the weakest identifier there is
    r_name_exact: float = 0.8
    r_name_contained: float = 0.4


W = Weights()

PRIORS = {"account": -1.5, "website": -1.5, "document": -1.5, "publication": -2.0, "public_record": -2.0,
          "person": -2.0, "organization": -1.0}
SCORED = frozenset(PRIORS)

# how much an identifier is worth expanding, relative to how likely it is to belong to the target
EXPAND_FACTOR = {"email": 1.0, "phone": 1.0, "username": 0.95, "domain": 0.95, "name": 0.8, "image": 0.8, "ip": 0.6,
                 "organization": 0.85, "location": 0.3, "asn": 0.25, "breach": 0.15, "source": 0.0}
TIER_FLOOR = {"high": 0.8, "medium": 0.55, "low": 0.0}
STRONG_FEATURES = frozenset({"email", "phone", "link_to_anchor", "verified_link_to_anchor", "linked_from_anchor",
                             "link_to_target_domain", "user_photo", "registration_id", "m_email", "m_phone"})
MEDIUM_FEATURES = frozenset({"username", "name_full", "name_contained", "city", "birth_year_same", "anchor_avatar",
                             "m_domain", "employer_same", "corroborated_link", "corroborated_verified"})
GENERIC_DOMAINS = frozenset({
    "gmail.com", "googlemail.com", "outlook.com", "hotmail.com", "yahoo.com", "icloud.com", "proton.me", "protonmail.com",
    "github.com", "gitlab.com", "github.io", "google.com", "youtube.com", "facebook.com", "instagram.com", "x.com",
    "twitter.com", "linkedin.com", "medium.com", "wordpress.com", "blogspot.com", "linktr.ee", "bit.ly", "t.co",
    "wikipedia.org", "reddit.com", "tiktok.com", "apple.com", "microsoft.com", "amazon.com", "cloudflare.com",
    "netlify.app", "vercel.app", "herokuapp.com", "pages.dev", "substack.com", "bsky.social", "keybase.io",
})

RESOLUTION_SOURCE = Source(id="resolution", connector="sextant", kind="local",
                           title="Entity resolution (local analysis on this computer)", reliability="medium")


def sigmoid(x: float) -> float:
    return 1.0 / (1.0 + math.exp(-x))


# --------------------------------------------------------------------------- candidate features

def _neighbor_values(g: Graph, e: Entity, type_id: str,
                     rels: tuple[str, ...] = ("USES", "OWNS", "REGISTERED_TO", "LINKED_TO")) -> list[tuple[str, float]]:
    out = []
    for r in g.edges(e.key):
        if r.type not in rels:
            continue
        other = r.target if r.source == e.key else r.source
        if other.startswith(type_id + ":"):
            out.append((other.split(":", 1)[1], r.confidence))
    return out


def _names(e: Entity) -> list[str]:
    if e.type == "account":
        return [str(x) for x in e.values("display_name") if x]
    if e.type == "person":
        return [str(x) for x in [*e.values("full_name"), *e.all_list("aliases")] if x] or [e.label]
    if e.type == "public_record":
        return [str(x) for x in e.values("matched_name") if x]
    if e.type == "organization":
        return [str(x) for x in e.values("name") if x] or [e.label]
    return []


def _name_reason(cands: list[str], p: TargetProfile, record: bool) -> Reason | None:
    if not cands or not p.name_tokens:
        return None
    mine = set(p.name_tokens)
    best: Reason | None = None
    for cand in cands:
        toks = name_tokens(cand)
        if not toks:
            continue
        ct = set(toks)
        if len(mine) >= 2 and ct == mine:
            r = Reason(f"Same full name (“{cand}”)" + (" — namesakes are common" if record else ""),
                       W.r_name_exact if record else W.name_full, "name_full")
        elif len(mine) >= 2 and (mine <= ct or (len(ct) >= 2 and ct <= mine)):
            r = Reason(f"Name contains the target's name or vice versa (“{cand}”)",
                       W.r_name_contained if record else W.name_contained, "name_contained")
        elif len(mine) >= 2 and len(ct) >= 2 and name_similarity(" ".join(p.name_tokens), cand) >= 0.92:
            r = Reason(f"Very similar name — possible transliteration (“{cand}”)",
                       W.r_name_contained if record else W.name_fuzzy, "name_fuzzy")
        elif not record and len(mine) >= 2 and p.name_tokens[-1] in ct:
            r = Reason(f"Same surname (“{cand}”)", W.name_surname, "name_surname")
        elif not record and p.name_tokens[0] in ct:
            r = Reason(f"Same first name only (“{cand}”)", W.name_first, "name_first")
        elif len(ct) >= 2 and not (ct & mine):
            r = Reason(f"Different name (“{cand}”)", W.name_mismatch, "name_mismatch")
        else:
            continue
        if best is None or r.weight > best.weight:
            best = r
    return best


def _location_reason(loc: str | None, p: TargetProfile) -> Reason | None:
    if not loc or not p.has_location:
        return None
    toks = set(location_tokens(loc))
    if not toks:
        return None
    if p.city_tokens and set(p.city_tokens) & toks:
        return Reason(f"Same city (“{loc}”)", W.city, "city")
    if p.country_tokens and set(p.country_tokens) & toks:
        return Reason(f"Same country (“{loc}”)", W.country, "country")
    return Reason(f"Different location (“{loc}”)", W.location_mismatch, "location_mismatch")


def _links(g: Graph, e: Entity) -> list[tuple[str, bool]]:
    verified = {str(x) for x in e.all_list("verified_links")}
    raw = [*e.values("website"), *e.all_list("links"), *e.all_list("verified_links")]
    out: list[tuple[str, bool]] = []
    for link in raw:
        s = str(link)
        out.append((s if "://" in s else f"https://{s}", s in verified))
    for r in g.out_edges(e.key):
        if r.type == "LINKED_TO":
            verified_edge = any("verified" in ev.description.lower() or "rel=me" in ev.description.lower() for ev in r.evidence)
            if r.target.startswith("account:"):
                tgt = g.entities.get(r.target)
                if tgt is not None and tgt.get("profile_url"):
                    out.append((str(tgt.get("profile_url")), verified_edge))
                out.append(("account-key:" + r.target, verified_edge))
            elif r.target.startswith(("domain:", "website:")):
                out.append(("https://" + r.target.split(":", 1)[1] if r.target.startswith("domain:") else r.target.split(":", 1)[1],
                            verified_edge))
    return out


def score_candidate(g: Graph, e: Entity, p: TargetProfile) -> list[Reason]:
    reasons: list[Reason] = []
    record = e.type == "public_record"
    seen: set[str] = set()

    def add(r: Reason | None) -> None:
        if r is not None and r.feature not in seen:
            seen.add(r.feature)
            reasons.append(r)

    # ---- hard identifiers (properties and identifier neighbours)
    emails = {str(x).lower(): 1.0 for x in e.all_list("emails")}
    for v, conf in _neighbor_values(g, e, "email"):
        emails[v] = max(emails.get(v, 0.0), conf)
    hit = max(((v, c) for v, c in emails.items() if v in p.emails), key=lambda t: t[1], default=None)
    if hit:
        add(Reason(f"Linked to the target's email {hit[0]}" + ("" if hit[1] >= 0.9 else " (fuzzy platform search)"),
                   W.email * hit[1], "email"))
    phones = {str(x): 1.0 for x in e.all_list("phones")}
    for v, conf in _neighbor_values(g, e, "phone"):
        phones[v] = max(phones.get(v, 0.0), conf)
    hit = max(((v, c) for v, c in phones.items() if v in p.phones), key=lambda t: t[1], default=None)
    if hit:
        add(Reason(f"Linked to the target's phone {hit[0]}", W.phone * hit[1], "phone"))
    ids = {str(x).casefold() for x in [*e.values("registration_number"), *e.values("lei")] if x}
    if ids & p.org_ids:
        add(Reason("Same registration number / LEI as a confirmed organization", W.registration_id, "registration_id"))

    # ---- username
    unames = {str(x).casefold() for x in e.values("username") if x} | {v for v, _ in _neighbor_values(g, e, "username")}
    common = unames & p.usernames
    if common:
        u = max(common, key=lambda x: username_uniqueness(x, p.name_tokens))
        uq = username_uniqueness(u, p.name_tokens)
        add(Reason(f"Same username “{u}” (estimated uniqueness {uq:.0%})", W.username_base + W.username_unique * uq,
                   "username" if uq >= 0.3 else "username_weak"))
        if uq < 0.3:
            reasons.append(Reason("Common username — many different people use it", 0.0, "username_common"))

    # ---- names, location, employment, dates
    if p.target_type == "organization":
        mine = {k for k in (org_name_key(n) for n in p.names) if k}
        theirs = {k for k in (org_name_key(n) for n in _names(e)) if k}
        if mine & theirs:
            add(Reason("Same organization name", W.name_full, "name_full"))
    else:
        add(_name_reason(_names(e), p, record))
    loc = e.get("location") or (e.get("country") if e.type == "account" else None)
    add(_location_reason(str(loc) if loc else None, p))
    orgs = [str(x) for x in [*e.values("company"), *e.values("employer")] if x]
    for r in g.out_edges(e.key):
        if r.type in ("WORKS_FOR", "MEMBER_OF"):
            o = g.entities.get(r.target)
            if o is not None:
                orgs.append(str(o.get("name") or o.label))
    org_keys = {k for k in (org_name_key(o.lstrip("@")) for o in orgs) if k}
    if org_keys and p.employers:
        if org_keys & p.employers:
            add(Reason(f"Same employer/organization ({', '.join(sorted(org_keys & p.employers))})", W.employer_same,
                       "employer_same"))
        else:
            add(Reason(f"Different stated employer ({', '.join(sorted(org_keys))[:80]})", W.employer_diff, "employer_diff"))
    occ = {str(x).casefold() for x in [*e.values("occupation"), *e.values("role")] if x}
    if occ and p.occupations:
        if occ & p.occupations:
            add(Reason("Same stated occupation", W.occupation_same, "occupation_same"))
        elif e.type == "person":
            add(Reason(f"Different stated occupation ({', '.join(sorted(occ))[:60]})", W.occupation_diff, "occupation_diff"))
    years = sorted({int(str(y)[:4]) for y in [*e.all_list("birth_years"), *e.values("birth_year"), *e.values("birth_date")]
                    if str(y)[:4].isdigit()})
    if years and p.birth_year:
        if p.birth_year in years:
            add(Reason(f"Same year of birth ({p.birth_year})", W.birth_year_same, "birth_year_same"))
        else:
            add(Reason(f"Different year of birth ({', '.join(map(str, years))})", W.birth_year_diff, "birth_year_diff"))
    elif years and (record or e.type == "person"):
        reasons.append(Reason("The record has a year of birth — add the target's to rule this namesake in or out "
                              "(used only on this computer)", 0.0, "birth_year_unknown"))
    nat = {fold(str(x)) for x in e.values("nationality") if x}
    if nat and p.nationality:
        if nat & {fold(n) for n in p.nationality}:
            add(Reason("Same nationality", W.nationality_same, "nationality_same"))
        else:
            add(Reason(f"Different nationality ({', '.join(sorted(nat))[:60]})", W.nationality_diff, "nationality_diff"))

    # ---- links: to the target's domains / to anchors; incoming from anchors
    for url, verified in _links(g, e):
        if url.startswith("account-key:"):
            tgt = g.resolve(url.split(":", 1)[1])
            if tgt in p.anchors and tgt != e.key:
                add(Reason(f"{'Verified link' if verified else 'Links'} to the target's {p.anchors[tgt].title} account",
                           W.verified_link_to_anchor if verified else W.link_to_anchor,
                           "verified_link_to_anchor" if verified else "link_to_anchor"))
            continue
        c = canonical_url(url)
        if not c:
            continue
        rd = registered_domain(url_host(c)) or ""
        if rd and rd in p.domains and rd not in GENERIC_DOMAINS:
            add(Reason(f"Links to the target's website ({rd})", W.link_to_target_domain, "link_to_target_domain"))
        elif rd and rd in p.email_domains:
            add(Reason(f"Links to the target's email domain ({rd})", W.link_to_email_domain, "link_to_email_domain"))
        prof = parse_profile_url(c)
        if prof:
            tgt = g.resolve(f"account:{prof[0].id}:{prof[1].casefold()}")
            if tgt in p.anchors and tgt != e.key:
                add(Reason(f"{'Verified link' if verified else 'Links'} to the target's {p.anchors[tgt].title} account",
                           W.verified_link_to_anchor if verified else W.link_to_anchor,
                           "verified_link_to_anchor" if verified else "link_to_anchor"))
    for r in g.in_edges(e.key):
        if r.type == "LINKED_TO" and r.source in p.anchors and r.source != e.key:
            add(Reason(f"Linked from the target's {p.anchors[r.source].title} account", W.linked_from_anchor,
                       "linked_from_anchor"))

    # ---- mutual corroboration: accounts that share the target's handle and link to each other
    if e.type == "account" and common and not ({"link_to_anchor", "linked_from_anchor", "verified_link_to_anchor"} & seen):
        for r in g.edges(e.key):
            if r.type != "LINKED_TO":
                continue
            other = g.entities.get(r.target if r.source == e.key else r.source)
            if other is None or other.type != "account" or not other.active:
                continue
            if str(other.get("username") or "").casefold() in p.usernames:
                verified = any("verified" in ev.description.lower() or "proof" in ev.description.lower() for ev in r.evidence)
                title = other.get("platform_title") or other.get("platform") or "account"
                add(Reason(f"{'Verified cross-link' if verified else 'Cross-linked'} with the {title} account using the same "
                           f"handle", W.corroborated_verified if verified else W.corroborated_link,
                           "corroborated_verified" if verified else "corroborated_link"))
                break

    # ---- photo
    fp = e.get("avatar_fp")
    if isinstance(fp, dict) and not fp.get("flat"):
        mine = max(((photo_similarity(attrs, fp), label) for label, attrs in p.photos), default=(0.0, ""))
        if mine[0] >= MATCH_SIMILARITY:
            add(Reason(f"Profile photo matches a photo of the target (possible image match {mine[0]:.0%})", W.user_photo,
                       "user_photo"))
        else:
            best = max(((photo_similarity(fp, a.avatar_fp), a) for a in p.anchors.values()
                        if a.key != e.key and isinstance(a.avatar_fp, dict)), key=lambda t: t[0], default=(0.0, None))
            if best[1] is not None and best[0] >= MATCH_SIMILARITY:
                add(Reason(f"Profile photo looks like the one on the target's {best[1].title} account "
                           f"(possible image match {best[0]:.0%})", W.anchor_avatar, "anchor_avatar"))

    # ---- display name equal to an anchor's when no name is known
    if e.type == "account" and not p.name_tokens:
        dn = e.get("display_name")
        if dn and len(name_tokens(str(dn))) >= 2:
            for a in p.anchors.values():
                if a.key != e.key and a.display_name and name_tokens(str(a.display_name)) == name_tokens(str(dn)):
                    add(Reason(f"Same display name as the target's {a.title} account", W.anchor_display_name,
                               "anchor_display_name"))
                    break

    # ---- search ranking: a weak signal, never enough on its own (analysis.WEAK_ALONE)
    if e.type == "account" and e.get("found_by") == "search_result" and p.name_tokens:
        try:
            rank = min(int(x) for x in e.values("search_rank") if x is not None)
        except ValueError:
            rank = 0
        if 1 <= rank <= 3:
            add(Reason(f"Rank {rank} result of a platform search for the name (a ranking signal, not proof)",
                       W.search_rank, "search_rank"))

    # ---- free text: bios, page titles and snippets
    if e.type == "account":
        bio = str(e.get("bio") or "")
        if bio:
            hits = p.matcher.match(bio)
            for k in [k for k in ("email", "phone", "domain") if k in hits][:2]:
                add(Reason(f"Bio mentions the target's {k}", W.bio_identifier, f"bio_{k}"))
    elif e.type in ("website", "document", "publication"):
        text = " ".join(str(x) for x in [e.get("title"), e.get("snippet"), *e.all_list("authors")] if x)
        url = str(e.get("url") or e.value)
        m = p.matcher.match(text, url)
        if "email" in m:
            add(Reason("Contains the target's email address", W.m_email, "m_email"))
        if "phone" in m:
            add(Reason("Contains the target's phone number", W.m_phone, "m_phone"))
        if "domain" in m:
            add(Reason("Mentions the target's domain", W.m_domain, "m_domain"))
        if "username" in m:
            u = max(m["username"], key=lambda x: username_uniqueness(x, p.name_tokens))
            add(Reason(f"Contains the target's username “{u}”", W.username_base + 1.5 * username_uniqueness(u, p.name_tokens),
                       "username"))
        if "name" in m:
            add(Reason("Contains the target's full name (namesakes are possible)", W.m_name, "m_name"))
        elif "name_partial" in m:
            add(Reason("Contains the target's first and last name separately", W.m_name_partial, "m_name_partial"))
        if p.has_location and ("name" in m or "username" in m):
            folded = set(name_tokens(text))
            if set(p.city_tokens or p.country_tokens) & folded:
                add(Reason("Also mentions the target's location", W.m_location_with_name, "m_location_with_name"))
        if not reasons:
            reasons.append(Reason("Returned for the target's identifiers, but none of them appear in the visible "
                                  "title/snippet", W.m_nothing, "m_nothing"))
    if not reasons:
        reasons.append(Reason("No identifying detail could be compared with the target", 0.0, "no_evidence"))
    return reasons


# --------------------------------------------------------------------------- labels, tiers, value

def tier_of(e: Entity, reasons: list[Reason], conf: float) -> str:
    feats = {r.feature for r in reasons if r.weight > 0}
    if e.status in ("seed", "confirmed"):
        return "high"
    if (feats & STRONG_FEATURES and conf >= 0.5) or conf >= 0.8:
        return "high"
    if conf >= 0.45 or feats & MEDIUM_FEATURES:
        return "medium"
    return "low"


def match_label(e: Entity, conf: float, reasons: list[Reason]) -> str:
    if e.status == "seed":
        return "Investigation input"
    if e.status == "confirmed":
        return "Confirmed identity match" if e.type in ("public_record", "person") else "Confirmed by investigator"
    if e.status == "separated":
        return "Different person (separated)"
    if e.status == "ignored":
        return "Ignored"
    feats = {r.feature for r in reasons}
    if e.type == "public_record":
        if feats & {"email", "phone", "registration_id"}:
            return "Public record found — linked by identifier"
        if feats & {"name_full", "name_contained", "name_fuzzy"}:
            return "Possible name match"
        return "Unable to verify"
    if e.type not in SCORED:
        return ""
    if conf >= 0.75:
        return "Likely match"
    if conf >= 0.4:
        return "Possible match"
    return "Unlikely match"


TIER_SCORE = {"high": 0.95, "medium": 0.65, "low": 0.3}


def value_of(e: Entity) -> float:
    """Investigative value of a scored candidate (accounts, records, pages…): how worth expanding it is.
    HIGH and MEDIUM leads (per the ranking table: same email/phone/cross-links vs. same username/name/city)
    get floors so depth 2 follows them; identity confidence stays a separate, conservative number."""
    if e.status in ("seed", "confirmed"):
        return 1.0
    if not e.active:
        return 0.0
    tier = e.tier or "low"
    return max(TIER_FLOOR.get(tier, 0.0), 0.5 * e.confidence + 0.5 * TIER_SCORE.get(tier, 0.3))


def _propagate_value(g: Graph, scored: set[str]) -> None:
    """Identifiers, organizations, places… inherit investigative value from what revealed them:
    value = max(neighbour value × link confidence) × type factor. Seeds and confirmed entities are 1.0."""
    inherit = [e for e in g.entities.values() if e.key not in scored and not e.locked]
    for e in inherit:
        e.value_score = 0.0
    for _ in range(4):
        changed = False
        for e in inherit:
            factor = EXPAND_FACTOR.get(e.type, 0.5)
            if e.type == "domain" and (registered_domain(e.value) or e.value) in GENERIC_DOMAINS:
                factor = 0.0
            if e.type == "username" and e.get("hypothesis"):
                factor = min(factor, 0.32)
            best = 0.0
            for r in g.edges(e.key):
                if r.type == "SAME_AS":
                    continue
                other = g.entities.get(r.target if r.source == e.key else r.source)
                if other is None or not other.active:
                    continue
                best = max(best, other.value_score * r.confidence)
            v = round(best * factor, 4)
            if abs(v - e.value_score) > 1e-4:
                e.value_score = v
                changed = True
        if not changed:
            break


# --------------------------------------------------------------------------- driver

PASSES = 3


def _pin(e: Entity) -> bool:
    if e.status in ("seed", "confirmed"):
        e.confidence = 1.0
        e.reasons = [Reason("Investigation input" if e.status == "seed" else "Confirmed by the investigator", 0.0, "user")]
        return True
    if e.status in ("ignored", "separated"):
        e.confidence = 0.0
        e.reasons = [Reason("Ignored by the investigator" if e.status == "ignored"
                            else "Separated by the investigator: a different person/entity", 0.0, "user")]
        return True
    return False


def _propagate(g: Graph) -> None:
    """Identifiers and other unscored entities inherit relevance along their strongest link."""
    inherit = [e for e in g.entities.values() if (e.type not in SCORED or (e.type == "organization" and g.target is not None
                                                                         and g.target.type != "organization"))
               and not e.locked]
    for e in inherit:
        e.confidence, e.reasons = 0.0, []
    for _ in range(4):
        changed = False
        for e in inherit:
            best, via = 0.0, None
            for r in g.edges(e.key):
                if r.type == "SAME_AS":
                    continue
                other = g.entities.get(r.target if r.source == e.key else r.source)
                if other is None or not other.active:
                    continue
                c = other.confidence * r.confidence
                if c > best:
                    best, via = c, other
            if abs(best - e.confidence) > 1e-6:
                e.confidence = best
                e.reasons = [Reason(f"Inherited from {ENTITY_TYPES[via.type].title.lower()} “{via.label[:60]}” "
                                    f"({via.confidence:.0%})", 0.0, "inherited")] if via else []
                changed = True
        if not changed:
            break


def resolve(g: Graph) -> set[str]:
    """Recomputes confidence, tier, label and investigative value of every entity. Returns changed keys."""
    before = {k: (round(e.confidence, 3), e.tier, e.match_label, round(e.value_score, 3)) for k, e in g.entities.items()}
    for e in g.entities.values():
        _pin(e)
    anchors = {e.key for e in g.entities.values() if e.type == "account" and e.status == "confirmed"}
    candidates = [e for e in g.entities.values() if e.type in SCORED and not e.locked and e.key != g.target_key
                  and not (e.type == "organization" and (g.target is None or g.target.type != "organization"))]
    for _ in range(PASSES):
        profile = build_profile(g, anchors)
        for e in candidates:
            reasons = score_candidate(g, e, profile)
            conf = sigmoid(PRIORS.get(e.type, -1.5) + sum(r.weight for r in reasons))
            e.reasons = sorted(reasons, key=lambda r: -abs(r.weight))
            e.confidence = conf
            e.tier = tier_of(e, reasons, conf)
        # an anchor needs independent strong evidence; mutual corroboration alone must not bootstrap one,
        # or the same cross-link would be counted twice ("corroborated" and then "linked from the target's account")
        new_anchors = {e.key for e in g.entities.values() if e.type == "account" and (
            e.status == "confirmed" or (e.active and e.confidence >= ANCHOR_CONFIDENCE and e.tier == "high"
                                        and any(r.feature in STRONG_FEATURES for r in e.reasons)))}
        if new_anchors == anchors:
            break
        anchors = new_anchors
    _propagate(g)
    scored = {e.key for e in candidates}
    for e in g.entities.values():
        if e.locked:
            e.tier = "high" if e.status in ("seed", "confirmed") else "low"
        elif e.key not in scored:
            e.tier = "high" if e.confidence >= 0.8 else "medium" if e.confidence >= 0.45 else "low"
        e.match_label = match_label(e, e.confidence, e.reasons)
        if e.locked or e.key in scored:
            e.value_score = value_of(e)
    _propagate_value(g, {k for k in scored})
    changed = set()
    for k, e in g.entities.items():
        if before.get(k) != (round(e.confidence, 3), e.tier, e.match_label, round(e.value_score, 3)):
            changed.add(k)
            g.dirty.entities.add(k)
    return changed


def link_same_owner(g: Graph, max_accounts: int = 400) -> int:
    """Pairwise candidate equivalences between accounts (``SAME_AS``), never merges.

    Blocking: same username on different platforms, or near-identical profile photos. A pair needs
    at least two agreeing signals (or one photo match) to be linked."""
    accounts = [e for e in g.of_type("account") if e.active][:max_accounts]
    g.add_source(RESOLUTION_SOURCE)
    added = 0
    by_user: dict[str, list[Entity]] = {}
    for a in accounts:
        u = str(a.get("username") or "").casefold()
        if u:
            by_user.setdefault(u, []).append(a)
    pairs: dict[tuple[str, str], list[str]] = {}
    for u, accs in by_user.items():
        if len(accs) < 2 or username_uniqueness(u) < 0.3:
            continue
        for i in range(len(accs)):
            for j in range(i + 1, len(accs)):
                pairs.setdefault(tuple(sorted((accs[i].key, accs[j].key))), []).append(f"same username “{u}”")
    with_fp = [a for a in accounts if isinstance(a.get("avatar_fp"), dict) and not a.get("avatar_fp").get("flat")]
    for i in range(len(with_fp)):
        for j in range(i + 1, len(with_fp)):
            sim = photo_similarity(with_fp[i].get("avatar_fp"), with_fp[j].get("avatar_fp"))
            if sim >= MATCH_SIMILARITY:
                pairs.setdefault(tuple(sorted((with_fp[i].key, with_fp[j].key))), []).append(
                    f"near-identical profile photo ({sim:.0%})")
    for (a_key, b_key), why in pairs.items():
        a, b = g.entities[a_key], g.entities[b_key]
        dn_a, dn_b = a.get("display_name"), b.get("display_name")
        if dn_a and dn_b and len(name_tokens(str(dn_a))) >= 2 and name_tokens(str(dn_a)) == name_tokens(str(dn_b)):
            why.append(f"same display name “{dn_a}”")
        if any("photo" in w for w in why) or len(why) >= 2:
            conf = min(0.9, 0.45 + 0.2 * len(why))
            rel = g.add_relationship(a_key, b_key, "SAME_AS", RelEvidence(
                RESOLUTION_SOURCE.id, "Possibly the same owner: " + "; ".join(why), confidence=conf))
            if rel is not None:
                added += 1
    return added


__all__ = ["resolve", "link_same_owner", "score_candidate", "build_profile", "Weights", "W", "PRIORS", "MODEL_VERSION",
           "GENERIC_DOMAINS", "value_of", "RESOLUTION_SOURCE"]
