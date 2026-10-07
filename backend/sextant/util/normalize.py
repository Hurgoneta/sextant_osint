"""Normalization of identifiers into canonical forms.

Canonical forms are what make deduplication safe: two findings merge automatically
only when their *normalized* values are identical.
"""

from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass, field
from functools import lru_cache
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

import phonenumbers
from phonenumbers import carrier, geocoder, timezone as pn_timezone

EMAIL_RE = re.compile(r"^[A-Za-z0-9.!#$%&'*+/=?^_`{|}~-]{1,64}@[A-Za-z0-9](?:[A-Za-z0-9-]{0,61}[A-Za-z0-9])?(?:\.[A-Za-z0-9](?:[A-Za-z0-9-]{0,61}[A-Za-z0-9])?)+$")
USERNAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$")
DOMAIN_RE = re.compile(r"^(?=.{1,253}$)(?:[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?\.)+[a-z][a-z0-9-]{0,62}$")

FREE_EMAIL_PROVIDERS = frozenset({
    "gmail.com", "googlemail.com", "yahoo.com", "yahoo.co.uk", "yahoo.ro", "ymail.com",
    "outlook.com", "hotmail.com", "hotmail.co.uk", "live.com", "msn.com", "icloud.com",
    "me.com", "mac.com", "aol.com", "proton.me", "protonmail.com", "pm.me", "gmx.com",
    "gmx.net", "gmx.de", "web.de", "mail.com", "yandex.com", "yandex.ru", "mail.ru",
    "zoho.com", "tutanota.com", "tuta.io", "fastmail.com", "hey.com", "qq.com", "163.com",
    "126.com", "orange.fr", "free.fr", "laposte.net", "libero.it", "t-online.de",
})

TRACKING_PARAMS = re.compile(r"^(utm_.*|fbclid|gclid|dclid|msclkid|mc_cid|mc_eid|ref|ref_src|igshid|si|_hsenc|_hsmi|spm)$", re.I)


# --------------------------------------------------------------------------- email

def normalize_email(raw: str) -> str | None:
    s = (raw or "").strip().strip("<>").strip()
    if s.lower().startswith("mailto:"):
        s = s[7:]
    if not EMAIL_RE.match(s):
        return None
    local, domain = s.rsplit("@", 1)
    domain = _idna(domain.lower().rstrip("."))
    if domain is None:
        return None
    return f"{local.lower()}@{domain}"


def email_variants(email: str) -> dict[str, str]:
    """Provider-specific canonical variants (gmail ignores dots and +tags)."""
    local, domain = email.split("@", 1)
    out = {"email": email}
    base_local = local.split("+", 1)[0]
    if domain in ("gmail.com", "googlemail.com"):
        out["provider_canonical"] = f"{base_local.replace('.', '')}@gmail.com"
    elif base_local != local:
        out["provider_canonical"] = f"{base_local}@{domain}"
    return out


def email_domain(email: str) -> str:
    return email.rsplit("@", 1)[1]


def is_free_email_provider(domain: str) -> bool:
    return domain.lower() in FREE_EMAIL_PROVIDERS


# --------------------------------------------------------------------------- phone

@dataclass
class PhoneInfo:
    e164: str
    international: str
    national: str
    country_code: int
    region: str | None
    region_name: str
    carrier: str
    number_type: str
    timezones: list[str] = field(default_factory=list)
    valid: bool = True

    @property
    def search_variants(self) -> list[str]:
        """Formats people actually write, used for search queries and text matching."""
        digits = self.e164.lstrip("+")
        national_digits = re.sub(r"\D", "", self.national)
        cc = str(self.country_code)
        nsn = digits[len(cc):]
        variants = [
            self.e164,
            self.international,
            f"00{digits}",
            digits,
            self.national,
            national_digits,
            f"+{cc} {nsn}",
        ]
        seen: list[str] = []
        for v in variants:
            if v and v not in seen:
                seen.append(v)
        return seen

    def to_dict(self) -> dict:
        return {
            "e164": self.e164, "international": self.international, "national": self.national,
            "country_code": self.country_code, "region": self.region, "region_name": self.region_name,
            "carrier": self.carrier, "number_type": self.number_type, "timezones": self.timezones,
            "valid": self.valid, "search_variants": self.search_variants,
        }


_PHONE_TYPES = {
    0: "fixed_line", 1: "mobile", 2: "fixed_line_or_mobile", 3: "toll_free", 4: "premium_rate",
    5: "shared_cost", 6: "voip", 7: "personal_number", 8: "pager", 9: "uan", 10: "voicemail",
}


def parse_phone(raw: str, default_region: str | None = None) -> PhoneInfo | None:
    """Parse with libphonenumber's *offline* metadata. No network involved."""
    s = (raw or "").strip()
    if not s or len(s) > 40:
        return None
    if s.startswith("00"):
        s = "+" + s[2:]
    try:
        num = phonenumbers.parse(s, (default_region or "").upper() or None)
    except phonenumbers.NumberParseException:
        return None
    if not phonenumbers.is_possible_number(num):
        return None
    region = phonenumbers.region_code_for_number(num)
    return PhoneInfo(
        e164=phonenumbers.format_number(num, phonenumbers.PhoneNumberFormat.E164),
        international=phonenumbers.format_number(num, phonenumbers.PhoneNumberFormat.INTERNATIONAL),
        national=phonenumbers.format_number(num, phonenumbers.PhoneNumberFormat.NATIONAL),
        country_code=num.country_code,
        region=region,
        region_name=geocoder.description_for_number(num, "en") or (region or ""),
        carrier=carrier.name_for_number(num, "en") or "",
        number_type=_PHONE_TYPES.get(phonenumbers.number_type(num), "unknown"),
        timezones=list(pn_timezone.time_zones_for_number(num)),
        valid=phonenumbers.is_valid_number(num),
    )


def normalize_phone(raw: str, default_region: str | None = None) -> str | None:
    info = parse_phone(raw, default_region)
    return info.e164 if info else None


# --------------------------------------------------------------------------- username

def normalize_username(raw: str) -> str | None:
    s = (raw or "").strip()
    if s.startswith("@"):
        s = s[1:]
    s = s.strip()
    if not USERNAME_RE.match(s):
        return None
    return s


def username_key(username: str) -> str:
    """Most platforms treat usernames case-insensitively."""
    return username.casefold()


COMMON_USERNAMES = frozenset({
    "admin", "administrator", "root", "test", "user", "guest", "info", "support", "john",
    "mike", "alex", "david", "chris", "james", "anna", "maria", "michael", "daniel", "andrei",
    "alexandru", "ana", "ion", "mihai", "elena", "me", "hello", "contact", "dev", "demo",
    "jsmith", "johnsmith", "smith", "john_smith", "bob", "alice", "max", "sam", "tom", "ben",
})


def username_uniqueness(username: str, name_tokens: list[str] | None = None) -> float:
    """Heuristic 0..1: how likely is it that only one person uses this handle?

    Short, dictionary-like or name-derived handles collide a lot across platforms.
    This is a transparent rule of thumb, not a statistic.
    """
    u = username.casefold()
    if u in COMMON_USERNAMES:
        return 0.08
    n = len(u)
    score = 0.05 if n <= 4 else 0.15 if n == 5 else 0.25 if n == 6 else 0.35 if n == 7 else 0.45 if n == 8 else 0.55 if n <= 10 else 0.65
    if re.search(r"\d", u) and re.search(r"[a-z]", u):
        score += 0.15
    if re.search(r"[._-]", u):
        score += 0.08
    if name_tokens:
        stripped = re.sub(r"[._\-\d]", "", u)
        joined = "".join(name_tokens)
        if stripped and (stripped == joined or stripped in name_tokens or stripped == "".join(reversed(name_tokens))):
            score -= 0.15  # "johnsmith" style handles are shared by many John Smiths
    return max(0.05, min(0.9, score))


# --------------------------------------------------------------------------- names / locations

def fold(text: str) -> str:
    """Casefold and strip diacritics: 'Jyväskylä' -> 'jyvaskyla'."""
    nfkd = unicodedata.normalize("NFKD", text or "")
    return "".join(c for c in nfkd if not unicodedata.combining(c)).casefold()


def name_tokens(name: str) -> list[str]:
    return [t for t in re.split(r"[^a-z0-9]+", fold(name)) if len(t) >= 2]


def normalize_name(raw: str) -> str | None:
    s = re.sub(r"\s+", " ", (raw or "").strip())
    if not s or len(s) > 120 or not re.search(r"[^\W\d_]", s):
        return None
    return s


def location_tokens(loc: str) -> list[str]:
    stop = {"the", "and", "of", "city", "county", "jud", "judet", "judetul", "area", "metro"}
    return [t for t in name_tokens(loc) if t not in stop and not t.isdigit()]


# --------------------------------------------------------------------------- domains / urls

def _idna(host: str) -> str | None:
    try:
        return host.encode("idna").decode("ascii").lower()
    except UnicodeError:
        return None


def normalize_domain(raw: str) -> str | None:
    s = (raw or "").strip().lower()
    if "://" in s:
        s = urlsplit(s).hostname or ""
    s = s.split("/", 1)[0].split(":", 1)[0].rstrip(".")
    if s.startswith("www."):
        s = s[4:]
    host = _idna(s)
    if not host or not DOMAIN_RE.match(host):
        return None
    return host


@lru_cache(maxsize=1)
def _tld_extractor():
    import tldextract
    # Offline: bundled public-suffix snapshot, no network fetch, no cache directory.
    return tldextract.TLDExtract(suffix_list_urls=(), cache_dir=None, include_psl_private_domains=True)


def registered_domain(host: str) -> str | None:
    """'blog.example.co.uk' -> 'example.co.uk' (public-suffix aware, offline)."""
    host = normalize_domain(host)
    if not host:
        return None
    try:
        ext = _tld_extractor()(host)
        # tldextract ≥ 5.3 renamed registered_domain; read the old name only on versions without the new one
        rd = ext.top_domain_under_public_suffix if hasattr(ext, "top_domain_under_public_suffix") else ext.registered_domain
        return rd or None
    except Exception:  # pragma: no cover - defensive, tldextract edge cases
        return host


def canonical_url(raw: str) -> str | None:
    """Canonical URL used as the identity of web pages (dedupe key)."""
    try:
        parts = urlsplit((raw or "").strip())
    except ValueError:
        return None
    if parts.scheme.lower() not in ("http", "https") or not parts.hostname:
        return None
    host = _idna(parts.hostname.rstrip("."))
    if not host:
        return None
    if host.startswith("www."):
        host = host[4:]
    try:
        port = parts.port
    except ValueError:
        return None
    netloc = host if port in (None, 80, 443) else f"{host}:{port}"
    path = re.sub(r"/{2,}", "/", parts.path or "/")
    if len(path) > 1:
        path = path.rstrip("/")
    query = urlencode([(k, v) for k, v in parse_qsl(parts.query, keep_blank_values=True)
                       if not TRACKING_PARAMS.match(k)])
    return urlunsplit(("https", netloc, path, query, ""))


def url_host(url: str) -> str:
    try:
        h = urlsplit(url).hostname or ""
    except ValueError:
        return ""
    return h[4:] if h.startswith("www.") else h


def is_http_url(url: str) -> bool:
    try:
        p = urlsplit(url)
    except ValueError:
        return False
    return p.scheme in ("http", "https") and bool(p.hostname)


def compare_names(seed_name: str, candidate: str) -> str | None:
    """'exact' when the token sets are equal (any order: 'DOE Jane' == 'Jane Doe'),
    'contains' when every token of the seed name appears in the candidate (extra middle names),
    None otherwise. Needs at least two tokens — a lone first name is never a match."""
    seed = set(name_tokens(seed_name))
    cand = set(name_tokens(candidate))
    if len(seed) < 2 or not cand:
        return None
    if seed == cand:
        return "exact"
    if seed <= cand:
        return "contains"
    return None


# --------------------------------------------------------------------------- infrastructure

def normalize_ip(raw: str) -> str | None:
    import ipaddress
    try:
        return str(ipaddress.ip_address((raw or "").strip().strip("[]")))
    except ValueError:
        return None


def is_public_ip(raw: str) -> bool:
    import ipaddress
    try:
        ip = ipaddress.ip_address((raw or "").strip())
    except ValueError:
        return False
    return ip.is_global and not ip.is_multicast and not ip.is_reserved


def normalize_asn(raw: str) -> str | None:
    s = (raw or "").strip().upper()
    if s.startswith("AS"):
        s = s[2:]
    return f"AS{int(s)}" if s.isdigit() and 0 < int(s) < 2 ** 32 else None


# --------------------------------------------------------------------------- names as identifiers

def name_key(raw: str) -> str | None:
    """Order-insensitive key for a personal or organisational *name string*: 'SMITH, John' == 'John Smith'.
    A name is a weak selector: equal keys mean equal spelling, never the same person."""
    toks = name_tokens(raw or "")
    if not toks or len(" ".join(toks)) > 160:
        return None
    return " ".join(sorted(toks))


def location_key(raw: str) -> str | None:
    toks = location_tokens(raw or "")
    return " ".join(toks) if toks else None


ORG_SUFFIXES = frozenset({
    "inc", "llc", "ltd", "limited", "plc", "corp", "corporation", "co", "company", "gmbh", "ag", "sa", "sas", "sarl",
    "srl", "bv", "nv", "ab", "as", "asa", "oy", "oyj", "spa", "sl", "kft", "sro", "spolka", "sp", "zoo", "pty", "lp",
    "llp", "the", "and", "of",
})


def org_name_key(raw: str) -> str | None:
    """'ACME Software S.R.L.' -> 'acme software' (legal-form suffixes dropped)."""
    toks = [t for t in name_tokens(raw or "") if t not in ORG_SUFFIXES]
    return " ".join(toks) if toks else None


# --------------------------------------------------------------------------- string similarity

def jaro_winkler(a: str, b: str, prefix_scale: float = 0.1) -> float:
    """Jaro–Winkler similarity in [0, 1] (catches transliteration variants: 'Jurij Exempel' ~ 'Yuriy Exempel')."""
    if a == b:
        return 1.0 if a else 0.0
    la, lb = len(a), len(b)
    if not la or not lb:
        return 0.0
    window = max(la, lb) // 2 - 1
    a_match, b_match = [False] * la, [False] * lb
    matches = 0
    for i, ch in enumerate(a):
        lo, hi = max(0, i - window), min(lb, i + window + 1)
        for j in range(lo, hi):
            if not b_match[j] and b[j] == ch:
                a_match[i] = b_match[j] = True
                matches += 1
                break
    if not matches:
        return 0.0
    transpositions, k = 0, 0
    for i in range(la):
        if a_match[i]:
            while not b_match[k]:
                k += 1
            if a[i] != b[k]:
                transpositions += 1
            k += 1
    m = float(matches)
    jaro = (m / la + m / lb + (m - transpositions / 2) / m) / 3
    prefix = 0
    for ca, cb in zip(a[:4], b[:4]):
        if ca != cb:
            break
        prefix += 1
    return jaro + prefix * prefix_scale * (1 - jaro)


def name_similarity(a: str, b: str) -> float:
    """Token-aware similarity: best Jaro–Winkler pairing of tokens, averaged over the shorter name."""
    ta, tb = name_tokens(a), name_tokens(b)
    if not ta or not tb:
        return 0.0
    if len(ta) > len(tb):
        ta, tb = tb, ta
    total = 0.0
    for t in ta:
        total += max(jaro_winkler(t, u) for u in tb)
    return total / len(ta)
