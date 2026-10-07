"""Understanding the investigator's input.

One free-text box accepts anything: ``John Michael Smith``, ``john@example.com``, ``+358 40 123 4567``,
``johnsmith123``, ``@handle``, ``https://github.com/jsmith``, ``example.com``, ``8.8.8.8`` — one per line,
or several separated by ``;`` (and by ``,`` when every part is an identifier). Explicit prefixes win
over guessing: ``name:``, ``email:``, ``phone:``, ``user:``, ``domain:``, ``url:``, ``ip:``, ``org:``,
``location:``, ``born:`` (year of birth, used only locally to rule namesakes out).

The parser never invents identifiers. Anything ambiguous is reported back to the investigator,
e.g. a bare word that could be a first name or a handle.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import datetime, timezone

from ..registry.platforms import parse_profile_url, user_platforms_in_scope
from ..util.normalize import (
    EMAIL_RE, canonical_url, name_tokens, normalize_asn, normalize_domain, normalize_email, normalize_ip,
    normalize_name, normalize_username, parse_phone, registered_domain,
)

ORG_HINTS = re.compile(r"\b(inc|llc|ltd|limited|plc|corp|corporation|gmbh|ag|s\.?a\.?|s\.?r\.?l\.?|b\.?v\.?|n\.?v\.?|"
                       r"oy|ab|as|asa|spa|s\.?a\.?s\.?|pty|llp|foundation|university|ministry|association|company)\.?$", re.I)
PREFIXES = {
    "name": "name", "person": "name", "email": "email", "mail": "email", "phone": "phone", "tel": "phone",
    "user": "username", "username": "username", "handle": "username", "domain": "domain", "url": "url",
    "website": "url", "ip": "ip", "asn": "asn", "org": "organization", "company": "organization",
    "organization": "organization", "location": "location", "city": "location", "place": "location",
    "born": "birth_year", "birthyear": "birth_year", "birth_year": "birth_year",
}
MAX_SEEDS = 25


@dataclass
class Seed:
    type: str               # name | email | phone | username | domain | website | account | ip | asn | organization | location
    value: str              # normalized
    label: str
    note: str = ""
    attrs: dict = field(default_factory=dict)


@dataclass
class ParsedInput:
    target_type: str = "person"            # person | organization | none
    target_name: str | None = None
    aliases: list[str] = field(default_factory=list)
    seeds: list[Seed] = field(default_factory=list)
    birth_year: int | None = None
    notes: list[str] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)

    def add(self, seed: Seed) -> None:
        if any(s.type == seed.type and s.value == seed.value for s in self.seeds):
            return
        if len(self.seeds) >= MAX_SEEDS:
            self.errors.append(f"At most {MAX_SEEDS} identifiers per investigation.")
            return
        self.seeds.append(seed)

    def to_dict(self) -> dict:
        return {"target_type": self.target_type, "target_name": self.target_name, "aliases": self.aliases,
                "birth_year": self.birth_year, "notes": self.notes, "errors": self.errors,
                "seeds": [{"type": s.type, "value": s.value, "label": s.label, "note": s.note} for s in self.seeds]}

    @property
    def title(self) -> str:
        if self.target_name:
            return self.target_name
        for t in ("username", "email", "domain", "phone", "account", "website", "ip"):
            for s in self.seeds:
                if s.type == t:
                    return s.label
        return "Investigation"


PREFIX_RE = re.compile(r"^([a-z_]{2,12})\s*:\s*\S", re.I)
COMMA_VALUES = ("location", "name")      # prefixed values that may themselves contain commas ("Stockholm, Sweden")


def _split(text: str) -> list[str]:
    out: list[str] = []
    for line in re.split(r"[\n;]+", text or ""):
        line = line.strip()
        if not line:
            continue
        parts = [p.strip() for p in line.split(",") if p.strip()]
        if len(parts) <= 1:
            out.append(line)
            continue
        separate: list[list[str]] = []        # identifiers and prefixed values, each its own token
        rest: list[str] = []                  # unprefixed free text (a name, possibly "SMITH, John")
        current: list[str] | None = None      # prefixed value still accepting comma-separated continuation
        for p in parts:
            m = PREFIX_RE.match(p)
            if m and m.group(1).lower() in PREFIXES and not p.lower().startswith(("http:", "https:")):
                current = [p]
                separate.append(current)
                if PREFIXES[m.group(1).lower()] not in COMMA_VALUES:
                    current = None
            elif _looks_like_identifier(p) or _looks_like_handle(p):
                current = None
                separate.append([p])
            elif current is not None:
                current.append(p)
            else:
                rest.append(p)
        if not separate:
            out.append(line)
            continue
        # "John Smith, john@example.com, location: Oslo, Norway" — identifiers and prefixed values split off
        out.extend(", ".join(tok) for tok in separate)
        if rest:
            out.append(", ".join(rest))
    return out


def _looks_like_handle(t: str) -> bool:
    """A single token with a digit or underscore ("jdoe92", "j_doe") is a username, not part of a name."""
    t = t.strip()
    return bool(re.fullmatch(r"[A-Za-z0-9._-]{3,64}", t) and re.search(r"[\d_]", t) and re.search(r"[A-Za-z]", t))


def _looks_like_identifier(t: str) -> bool:
    t = t.strip()
    return bool(EMAIL_RE.match(t) or t.startswith(("@", "http://", "https://", "+")) or normalize_ip(t)
                or (" " not in t and "." in t and normalize_domain(t) and registered_domain(t))
                or re.fullmatch(r"[\d\s().-]{7,}", t))


def _classify(token: str, region: str | None, out: ParsedInput) -> None:
    raw = token.strip().strip("\"'")
    if not raw:
        return
    m = re.match(r"^([a-z_]{2,12})\s*:\s*(.+)$", raw, re.I)
    if m and m.group(1).lower() in PREFIXES and not raw.lower().startswith(("http:", "https:")):
        kind, value = PREFIXES[m.group(1).lower()], m.group(2).strip()
        _typed(kind, value, region, out)
        return
    # URL (profile URLs become account + username)
    if re.match(r"^https?://", raw, re.I):
        _typed("url", raw, region, out)
        return
    if EMAIL_RE.match(raw):
        _typed("email", raw, region, out)
        return
    if raw.startswith("@"):
        handle = raw[1:]
        if "@" in handle:                      # fediverse handle user@server
            user, _, server = handle.partition("@")
            u = normalize_username(user)
            if u and normalize_domain(server):
                out.add(Seed("username", u.casefold(), u, f"Fediverse handle @{handle}", {"username": u, "handle": f"{u}@{server}"}))
                return
        _typed("username", handle, region, out)
        return
    if normalize_ip(raw):
        _typed("ip", raw, region, out)
        return
    if re.fullmatch(r"(?i)as\d{1,10}", raw):
        _typed("asn", raw, region, out)
        return
    digits = re.sub(r"\D", "", raw)
    if raw.startswith(("+", "00")) or (re.fullmatch(r"[\d\s().-]+", raw) and len(digits) >= 7):
        _typed("phone", raw, region, out)
        return
    if " " not in raw and "." in raw and normalize_domain(raw) and registered_domain(raw):
        _typed("domain", raw, region, out)
        return
    if re.fullmatch(r"(19|20)\d{2}", raw):
        out.notes.append(f"“{raw}” looks like a year — write born:{raw} to use it as the target's year of birth.")
        return
    toks = name_tokens(raw)
    words = raw.split()
    if len(words) >= 2 and len(toks) >= 2 and all(re.search(r"[^\W\d_]", w) for w in words[:2]):
        _typed("organization" if ORG_HINTS.search(raw) else "name", raw, region, out)
        return
    if normalize_username(raw):
        u = normalize_username(raw)
        note = ""
        if raw.isalpha():
            note = "Single word: treated as a username (it could also be a first name — use name: to override)."
            out.notes.append(f"“{raw}”: {note}")
        out.add(Seed("username", u.casefold(), u, note, {"username": u}))
        return
    out.errors.append(f"Could not understand “{raw[:60]}”. Use a prefix such as name:, email:, user:, domain:.")


def _typed(kind: str, value: str, region: str | None, out: ParsedInput) -> None:
    if kind == "name":
        n = normalize_name(value)
        if not n or len(name_tokens(n)) < 1:
            out.errors.append(f"“{value}” does not look like a name.")
            return
        if out.target_name is None:
            out.target_name = n
        elif n not in out.aliases and n != out.target_name:
            out.aliases.append(n)
        return
    if kind == "organization":
        n = normalize_name(value)
        if not n:
            out.errors.append(f"“{value}” does not look like an organization name.")
            return
        if out.target_name is None and not any(s.type in ("email", "phone", "username") for s in out.seeds):
            out.target_type = "organization"
            out.target_name = n
        else:
            out.add(Seed("organization", n, n))
        return
    if kind == "email":
        e = normalize_email(value)
        if not e:
            out.errors.append(f"“{value}” is not a valid email address.")
            return
        out.add(Seed("email", e, e))
        return
    if kind == "phone":
        info = parse_phone(value, region)
        if info is None:
            if not value.strip().startswith(("+", "00")) and not region:
                out.errors.append(f"Phone “{value}”: write it in international format (+country…) or set a default country.")
            else:
                out.errors.append(f"“{value}” is not a valid phone number.")
            return
        out.add(Seed("phone", info.e164, info.international, "" if info.valid else "Possible but not valid number",
                     info.to_dict()))
        return
    if kind == "username":
        u = normalize_username(value)
        if not u:
            out.errors.append(f"Username “{value}” contains unsupported characters.")
            return
        out.add(Seed("username", u.casefold(), u, "", {"username": u}))
        return
    if kind == "domain":
        d = normalize_domain(value)
        if not d:
            out.errors.append(f"“{value}” is not a valid domain.")
            return
        out.add(Seed("domain", d, d))
        return
    if kind == "url":
        c = canonical_url(value if "://" in value else f"https://{value}")
        if not c:
            out.errors.append(f"“{value}” is not a valid URL.")
            return
        prof = parse_profile_url(c)
        if prof:
            platform, uname = prof
            out.add(Seed("account", f"{platform.id}:{uname.casefold()}", f"{platform.name}: {uname}", "",
                         {"platform": platform.id, "platform_title": platform.name, "username": uname, "profile_url": c}))
            if "/" not in uname and normalize_username(uname):
                out.add(Seed("username", uname.casefold(), uname, f"From {platform.name} profile URL", {"username": uname}))
            return
        out.add(Seed("website", c, c, "", {"url": c}))
        d = normalize_domain(c)
        if d:
            out.add(Seed("domain", registered_domain(d) or d, registered_domain(d) or d))
        return
    if kind == "ip":
        ip = normalize_ip(value)
        if not ip:
            out.errors.append(f"“{value}” is not a valid IP address.")
            return
        out.add(Seed("ip", ip, ip))
        return
    if kind == "asn":
        a = normalize_asn(value)
        if not a:
            out.errors.append(f"“{value}” is not a valid AS number.")
            return
        out.add(Seed("asn", a, a))
        return
    if kind == "location":
        v = value.strip()[:120]
        if name_tokens(v):
            out.add(Seed("location", " ".join(name_tokens(v)), v, "", {"text": v}))
        return
    if kind == "birth_year":
        v = value.strip()
        if v.isdigit() and 1900 <= int(v) <= datetime.now(timezone.utc).year:
            out.birth_year = int(v)
        else:
            out.errors.append("Year of birth must be a four-digit year such as 1990.")
        return


def parse_input(text: str = "", structured: dict | None = None, default_region: str | None = None,
                user_platforms: tuple | None = None) -> ParsedInput:
    """Parses free text and/or structured fields ({names, emails, phones, usernames, domains, urls,
    locations, organizations, birth_year, target_type}). ``user_platforms``: the investigator's own platforms, so a
    pasted profile URL of one of them becomes an account (and its username) like a built-in platform's."""
    if user_platforms is not None:
        with user_platforms_in_scope(user_platforms):
            return parse_input(text, structured, default_region)
    region = (default_region or "").strip().upper() or None
    out = ParsedInput()
    for token in _split(text):
        _classify(token, region, out)
    st = structured or {}
    for kind, key in (("name", "names"), ("email", "emails"), ("phone", "phones"), ("username", "usernames"),
                      ("domain", "domains"), ("url", "urls"), ("location", "locations"), ("organization", "organizations"),
                      ("ip", "ips")):
        vals = st.get(key) or []
        if isinstance(vals, str):
            vals = [v for v in re.split(r"[\n;]+", vals) if v.strip()]
        for v in vals[:MAX_SEEDS]:
            if isinstance(v, str) and v.strip():
                _typed(kind, v.strip(), region, out)
    if st.get("birth_year"):
        _typed("birth_year", str(st["birth_year"]), region, out)
    if st.get("target_type") in ("person", "organization"):
        out.target_type = st["target_type"]
    if out.target_name is None and out.target_type == "person" and not any(
            s.type in ("email", "phone", "username", "account") for s in out.seeds):
        # only infrastructure/org selectors: there is no person to resolve against
        out.target_type = "none" if out.seeds else out.target_type
    if not out.seeds and not out.target_name:
        out.errors.append("Enter at least one identifier: a name, email, phone number, username, URL or domain.")
    return out
