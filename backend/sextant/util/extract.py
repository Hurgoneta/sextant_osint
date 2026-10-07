"""Identifier extraction from untrusted text / HTML.

HTML is parsed with the standard-library tokenizer only: no scripts are executed,
no external resources are loaded, nothing is rendered.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from html.parser import HTMLParser
from urllib.parse import urljoin

import phonenumbers

from .normalize import canonical_url, fold, is_http_url, name_tokens, normalize_email

EMAIL_IN_TEXT = re.compile(r"(?<![A-Za-z0-9._%+-])[A-Za-z0-9._%+-]{1,64}@[A-Za-z0-9.-]{1,253}\.[A-Za-z]{2,24}(?![A-Za-z0-9-])")
URL_IN_TEXT = re.compile(r"https?://[^\s<>\"'`)\]}]{3,2000}", re.I)
_ASSET_SUFFIX = re.compile(r"\.(png|jpe?g|gif|svg|webp|css|js)$", re.I)
_OBFUSCATED_AT = re.compile(r"\s*[\[\(\{]\s*(?:at|@)\s*[\]\)\}]\s*", re.I)
_OBFUSCATED_DOT = re.compile(r"\s*[\[\(\{]\s*dot\s*[\]\)\}]\s*", re.I)

MAX_TEXT = 500_000


def extract_emails(text: str) -> list[str]:
    text = (text or "")[:MAX_TEXT]
    text = _OBFUSCATED_DOT.sub(".", _OBFUSCATED_AT.sub("@", text))
    out: list[str] = []
    for m in EMAIL_IN_TEXT.finditer(text):
        cand = m.group(0).strip(".")
        if _ASSET_SUFFIX.search(cand):        # logo@2x.png and friends
            continue
        norm = normalize_email(cand)
        if norm and norm not in out:
            out.append(norm)
    return out


def extract_phones(text: str, region: str | None = None, limit: int = 50) -> list[str]:
    text = (text or "")[:MAX_TEXT]
    out: list[str] = []
    try:
        matcher = phonenumbers.PhoneNumberMatcher(text, (region or "").upper() or None,
                                                  leniency=phonenumbers.Leniency.VALID, max_tries=200)
        for match in matcher:
            e164 = phonenumbers.format_number(match.number, phonenumbers.PhoneNumberFormat.E164)
            if e164 not in out:
                out.append(e164)
            if len(out) >= limit:
                break
    except Exception:  # libphonenumber can choke on pathological input; extraction is best-effort
        pass
    return out


def extract_urls(text: str) -> list[str]:
    out: list[str] = []
    for m in URL_IN_TEXT.finditer((text or "")[:MAX_TEXT]):
        u = m.group(0).rstrip(".,;:!?")
        if is_http_url(u) and u not in out:
            out.append(u)
    return out


@dataclass
class ParsedPage:
    title: str = ""
    text: str = ""
    links: list[str] = field(default_factory=list)       # absolute http(s) links
    me_links: list[str] = field(default_factory=list)    # rel="me" — strong ownership signal
    mailto: list[str] = field(default_factory=list)
    tel: list[str] = field(default_factory=list)
    meta: dict[str, str] = field(default_factory=dict)   # generator, description, og:*, author


class _PageParser(HTMLParser):
    _SKIP = {"script", "style", "noscript", "template", "svg"}

    def __init__(self, base_url: str) -> None:
        super().__init__(convert_charrefs=True)
        self.base = base_url
        self.page = ParsedPage()
        self._skip_depth = 0
        self._in_title = False
        self._text: list[str] = []
        self._text_len = 0

    def handle_starttag(self, tag, attrs):
        a = {k.lower(): (v or "") for k, v in attrs}
        if tag in self._SKIP:
            self._skip_depth += 1
        elif tag == "title":
            self._in_title = True
        elif tag == "meta":
            name = (a.get("name") or a.get("property") or "").lower()
            if name in ("generator", "description", "author", "og:title", "og:description", "og:site_name", "og:image",
                        "twitter:creator", "twitter:title", "profile:username"):
                self.page.meta[name] = a.get("content", "")[:500]
        elif tag in ("a", "link") and a.get("href"):
            href = a["href"].strip()
            low = href.lower()
            if low.startswith("mailto:"):
                self.page.mailto.append(href[7:].split("?", 1)[0])
            elif low.startswith("tel:"):
                self.page.tel.append(href[4:])
            else:
                absolute = urljoin(self.base, href)
                if is_http_url(absolute) and len(self.page.links) < 2000:
                    self.page.links.append(absolute)
                    if "me" in a.get("rel", "").lower().split():
                        self.page.me_links.append(absolute)

    def handle_endtag(self, tag):
        if tag in self._SKIP and self._skip_depth:
            self._skip_depth -= 1
        elif tag == "title":
            self._in_title = False

    def handle_data(self, data):
        if self._skip_depth:
            return
        if self._in_title:
            self.page.title = (self.page.title + data).strip()[:300]
        elif self._text_len < MAX_TEXT:
            self._text.append(data)
            self._text_len += len(data)

    def close(self):
        super().close()
        self.page.text = re.sub(r"\s+", " ", " ".join(self._text)).strip()


def parse_html(html: str, base_url: str) -> ParsedPage:
    parser = _PageParser(base_url)
    try:
        parser.feed((html or "")[: MAX_TEXT * 2])
        parser.close()
    except Exception:  # malformed markup should never crash an investigation
        parser.page.text = parser.page.text or re.sub(r"<[^>]+>", " ", html or "")[:MAX_TEXT]
    return parser.page


def html_to_text(html: str) -> str:
    return parse_html(html, "https://invalid.local/").text


# --------------------------------------------------------------------------- matching seeds in text

@dataclass
class SeedMatcher:
    """Finds which of the user's identifiers occur in a piece of text."""

    names: list[str] = field(default_factory=list)
    emails: list[str] = field(default_factory=list)
    phones: dict[str, list[str]] = field(default_factory=dict)   # e164 -> variants
    usernames: list[str] = field(default_factory=list)
    domains: list[str] = field(default_factory=list)
    locations: list[str] = field(default_factory=list)

    def match(self, text: str, url: str = "") -> dict[str, list[str]]:
        hay = fold(text)
        url_l = (url or "").lower()
        found: dict[str, list[str]] = {}
        for e in self.emails:
            if e in hay or e in url_l:
                found.setdefault("email", []).append(e)
        digits_hay = re.sub(r"[\s().-]", "", hay)
        for e164, variants in self.phones.items():
            for v in variants:
                compact = re.sub(r"[\s().-]", "", v)
                if len(compact) >= 8 and compact in digits_hay:
                    found.setdefault("phone", []).append(e164)
                    break
        for u in self.usernames:
            if re.search(rf"(?<![a-z0-9_.-]){re.escape(u.casefold())}(?![a-z0-9_-])", hay) or \
               re.search(rf"[/@=]{re.escape(u.casefold())}(?![a-z0-9_-])", url_l):
                found.setdefault("username", []).append(u)
        for n in self.names:
            toks = name_tokens(n)
            if len(toks) >= 2:
                phrase = r"\W+".join(re.escape(t) for t in toks)
                rev = r"\W+".join(re.escape(t) for t in reversed(toks))
                if re.search(phrase, hay) or re.search(rf"{re.escape(toks[-1])},?\s+{re.escape(toks[0])}", hay) or re.search(rev, hay):
                    found.setdefault("name", []).append(n)
                elif all(re.search(rf"\b{re.escape(t)}\b", hay) for t in (toks[0], toks[-1])):
                    found.setdefault("name_partial", []).append(n)
        for d in self.domains:
            if d in hay or d in url_l:
                found.setdefault("domain", []).append(d)
        for loc in self.locations:
            toks = [t for t in name_tokens(loc) if len(t) >= 3]
            if toks and re.search(rf"\b{re.escape(toks[0])}\b", hay):
                found.setdefault("location", []).append(loc)
        return found


def canonical_links(links: list[str]) -> list[str]:
    out = []
    for link in links:
        c = canonical_url(link)
        if c and c not in out:
            out.append(c)
    return out
