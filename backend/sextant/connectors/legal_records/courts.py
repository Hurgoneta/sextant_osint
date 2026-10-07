"""Legal and judicial records from official public sources.

* Romania — portal.just.ro via the Ministry of Justice's public web service (CautareDosare, party-name filter).
  Case files are public and list every party by name and procedural role. The service has no working
  HTTPS, so the name crosses the network unencrypted (disclosed in the plan).
* United States — CourtListener (Free Law Project): federal dockets mirrored from PACER, with party names.
* United Kingdom — Find Case Law (The National Archives): published judgments, Open Justice Licence.

Every record states its jurisdiction, authority, official URL, record type, date and the exact name that
matched. A record links only to the *name string* it carries. The resolver keeps name-only matches at
"possible name match", and the UI never presents a record as being about the target unless the
investigator confirms it.
"""

from __future__ import annotations

import io
import re
from xml.etree.ElementTree import ParseError
from xml.sax.saxutils import escape

from defusedxml.ElementTree import fromstring, iterparse

from ...sdk import Connector, Credential, Disclosure, Manifest, RateLimit, register, transform
from ...sdk.errors import NotFound, SourceUnavailable
from ...util.normalize import compare_names, fold, location_tokens
from .._common import add_record, person_names

ENDPOINT = "http://portalquery.just.ro/query.asmx"
NS = "portalquery.just.ro"
MAX_BYTES = 6 * 1024 * 1024
MAX_CASES = 200

ROLE_HINTS = {
    "inculpat": "defendant (criminal)", "suspect": "suspect (criminal)", "condamnat": "convicted person",
    "parte vatamata": "injured party", "parte civila": "civil party", "reclamant": "claimant", "parat": "defendant (civil)",
    "petent": "petitioner", "contestator": "challenger", "intimat": "respondent", "apelant": "appellant",
    "recurent": "appellant (recourse)", "martor": "witness", "debitor": "debtor", "creditor": "creditor",
}


def _local(tag: str) -> str:
    return tag.rsplit("}", 1)[-1]


def _child_text(el, name: str) -> str:
    for c in el:
        if _local(c.tag) == name:
            return (c.text or "").strip()
    return ""


def court_title(code: str) -> str:
    """'TribunalulBUCURESTI' -> 'Tribunalul BUCURESTI', 'CurteadeApelBUCURESTI' -> 'Curtea de Apel BUCURESTI'."""
    t = code.replace("CurteadeApel", "Curtea de Apel ").replace("InaltaCurtedeCasatiesiJustitie", "Înalta Curte de Casație și Justiție")
    t = re.sub(r"(?<=[a-z])(?=[A-Z])", " ", t)
    return re.sub(r"\s+", " ", t).strip()


def role_hint(role: str) -> str:
    folded = fold(role)
    return next((v for k, v in ROLE_HINTS.items() if k in folded), "")


def parse_cases(xml: bytes, seed_name: str) -> tuple[list[dict], bool]:
    """Incrementally parse <Dosar> elements; tolerate truncation. Returns (cases, complete)."""
    cases: list[dict] = []
    complete = True
    try:
        for _, el in iterparse(io.BytesIO(xml), events=("end",), forbid_dtd=True):
            if _local(el.tag) != "Dosar":
                continue
            parties = []
            sessions = []
            for child in el:
                name = _local(child.tag)
                if name == "parti":
                    parties = [{"name": _child_text(p, "nume"), "role": _child_text(p, "calitateParte")} for p in child]
                elif name == "sedinte":
                    sessions = [{"date": _child_text(s, "data")[:10], "summary": _child_text(s, "solutieSumar"),
                                 "solution": _child_text(s, "solutie")} for s in child]
            matched = [(p, compare_names(seed_name, p["name"])) for p in parties]
            matched = [(p, m) for p, m in matched if m]
            if matched:
                party, how = matched[0]
                sessions.sort(key=lambda s: s["date"])
                last = sessions[-1] if sessions else {}
                cases.append({
                    "number": _child_text(el, "numar"), "court_code": _child_text(el, "institutie"),
                    "category": _child_text(el, "categorieCazNume"), "stage": _child_text(el, "stadiuProcesualNume"),
                    "subject": _child_text(el, "obiect"), "date": _child_text(el, "data")[:10],
                    "modified": _child_text(el, "dataModificare")[:10], "party": party["name"], "role": party["role"],
                    "name_match": how, "hearings": len(sessions),
                    "last_hearing": {k: (v or "")[:300] for k, v in last.items()} if last else None,
                })
            el.clear()
            if len(cases) >= MAX_CASES:
                complete = False
                break
    except ParseError:
        complete = False          # truncated response: keep every case parsed so far
    return cases, complete


def soap_body(name: str) -> bytes:
    return (
        '<?xml version="1.0" encoding="utf-8"?>'
        '<soap:Envelope xmlns:soap="http://schemas.xmlsoap.org/soap/envelope/">'
        f'<soap:Body><CautareDosare xmlns="{NS}"><numeParte>{escape(name)}</numeParte></CautareDosare></soap:Body>'
        "</soap:Envelope>"
    ).encode()


@register
class RomanianCourts(Connector):
    manifest = Manifest(
        name="ro_courts", title="Romania — court portal (portal.just.ro)", category="legal_records",
        description="Public Romanian court case files with a party of this name: court, case number, category "
                    "(criminal, civil…), stage, the party's procedural role and the last hearing.",
        access="free", rate_limit=RateLimit(0.5, 1), website="https://portal.just.ro/",
        documentation="http://portalquery.just.ro/query.asmx", reliability="official", public_record=True,
        jurisdictions=("RO",), timeout=300, official_api=True,
        legal="Romanian case files are public (Law 544/2001; CSM rules). A matching name is not an identity match.",
        capabilities=("court cases with a party of this name", "party role (defendant, claimant, witness…)",
                      "case category, stage, last hearing"),
        disclosure=Disclosure(("portalquery.just.ro",), "the full name (both name orders)",
                              note="The official service has no working HTTPS — the name is sent unencrypted."),
        default_enabled=False, tags=("country_preselect",), supported_countries=("RO",),
        opt_in_reason="Sends the name over unencrypted HTTP; pre-selected when the investigation points to Romania.",
    )

    @transform(inputs=("person",), outputs=("public_record", "name"), title="Romanian court cases with a party of this name",
               priority=0.6, accepts=lambda self, e, info: e.is_seed and bool(person_names(e, info)),
               sends="full name “{value}” over unencrypted HTTP")
    async def cases(self, entity, ctx):
        name = person_names(entity, ctx.info)[0]
        tokens = name.split()
        variants = [name] + ([" ".join(tokens[1:] + tokens[:1])] if len(tokens) >= 2 else [])
        found: dict[str, dict] = {}
        complete = True
        for v in variants:
            resp = await ctx.post(ENDPOINT, content=soap_body(v), max_bytes=MAX_BYTES, retries=1,
                                  headers={"Content-Type": "text/xml; charset=utf-8", "SOAPAction": f"{NS}/CautareDosare"})
            if resp.status != 200:
                raise SourceUnavailable(f"Court portal answered HTTP {resp.status}")
            cases, ok = parse_cases(resp.content, name)
            complete = complete and ok and not resp.truncated
            for c in cases:
                found.setdefault(f"{c['court_code']}:{c['number']}", c)
        city = location_tokens(ctx.info.locations[0].split(",")[0]) if ctx.info.locations else []
        for c in found.values():
            court = court_title(c["court_code"])
            src = ctx.out.source("https://portal.just.ro/", title=f"{court} — dosar {c['number']}", kind="api",
                                 request=f"party name “{name}”")
            add_record(ctx.out, src, key=f"ro-court:{c['court_code']}:{c['number']}", title=f"{court} — dosar {c['number']}",
                       record_type="court_case", jurisdiction="RO", authority=court, official_url="https://portal.just.ro/",
                       matched_name=c["party"], name_match=c["name_match"], query_name=name,
                       props={"reference": c["number"], "category": c["category"], "criminal": "penal" in fold(c["category"]),
                              "status": c["stage"], "summary": c["subject"], "date": c["date"], "role": c["role"],
                              "role_meaning": role_hint(c["role"]), "hearings": c["hearings"], "last_hearing": c["last_hearing"],
                              "court_in_target_city": bool(city) and any(len(t) >= 4 and t[:5] in fold(c["court_code"]) for t in city),
                              "how_to_verify": "Search the case number on portal.just.ro to read the file."})
        msg = f"{len(found)} case(s) with a party named like the target"
        ctx.out.set_status("found" if found else "empty",
                           msg + ("" if complete else " — very common name: results truncated, check the portal manually"))


@register
class CourtListener(Connector):
    manifest = Manifest(
        name="courtlistener", title="CourtListener (US federal dockets)", category="legal_records",
        description="US federal court dockets (RECAP mirror of PACER) whose parties include this name.", access="free",
        credentials=(Credential("COURTLISTENER_TOKEN", "CourtListener API token", "Optional; raises rate limits.",
                                required=False, obtain_url="https://www.courtlistener.com/help/api/rest/"),),
        rate_limit=RateLimit(0.5, 2, "5,000 requests/hour with a token"), website="https://www.courtlistener.com/",
        documentation="https://www.courtlistener.com/help/api/rest/search/", reliability="official", public_record=True,
        jurisdictions=("US",), supported_countries=("US",), license="Public domain (court records) / CC BY-ND (some data)",
        capabilities=("federal dockets with the name among the parties", "court, docket number, nature of suit, filing date"),
        disclosure=Disclosure(("www.courtlistener.com",), "the full name"),
    )

    @transform(inputs=("person", "organization"), outputs=("public_record", "name"),
               title="CourtListener: US federal dockets naming this party", priority=0.5,
               accepts=lambda self, e, info: (e.type == "person" and e.is_seed and bool(person_names(e, info)))
               or (e.type == "organization" and str(e.get("jurisdiction") or "US").upper().startswith("US")))
    async def dockets(self, entity, ctx):
        name = person_names(entity, ctx.info)[0] if entity.type == "person" else str(entity.get("name") or entity.label)
        headers = {"Accept": "application/json"}
        token = ctx.credential("COURTLISTENER_TOKEN")
        if token:
            headers["Authorization"] = f"Token {token}"
        resp = await ctx.get("https://www.courtlistener.com/api/rest/v4/search/", params={"q": f'"{name}"', "type": "r"},
                             headers=headers)
        hits = 0
        for r in (ctx.json(ctx.expect(resp)) or {}).get("results") or []:
            names = [str(p) for p in r.get("party") or []] + [str(r.get("caseName") or "")]
            m = next(((n, how) for n in names for how in [compare_names(name, n)] if how), None)
            if entity.type == "organization" and not m:
                m = (str(r.get("caseName")), "contains") if fold(name) in fold(str(r.get("caseName"))) else None
            if not m:
                continue
            hits += 1
            docket = str(r.get("docketNumber") or "")
            url = "https://www.courtlistener.com" + str(r.get("docket_absolute_url") or "/")
            src = ctx.out.source(url, title=f"{r.get('court')} — {r.get('caseName')}", request=f"party name “{name}”")
            add_record(ctx.out, src, key=f"courtlistener:{r.get('docket_id')}", title=f"{r.get('court')} — {r.get('caseName')}",
                       record_type="court_case", jurisdiction="US", authority=str(r.get("court") or ""), official_url=url,
                       matched_name=m[0] if entity.type == "person" else None, name_match=m[1], query_name=name,
                       link_from=entity.key if entity.type == "organization" else None,
                       props={"reference": docket, "category": r.get("suitNature") or r.get("cause") or "",
                              "criminal": "-cr-" in docket.lower(), "date": r.get("dateFiled"), "summary": r.get("caseName")})
        if not hits:
            raise NotFound("No federal docket names this party")


ATOM = "{http://www.w3.org/2005/Atom}"
TNA = "{https://caselaw.nationalarchives.gov.uk}"


def parse_find_case_law(xml: bytes) -> list[dict]:
    root = fromstring(xml)
    out = []
    for e in root.findall(f"{ATOM}entry"):
        link = next((ln.get("href") for ln in e.findall(f"{ATOM}link") if ln.get("rel") == "alternate" and not ln.get("type")), None)
        ident = next((i.text for i in e.findall(f"{TNA}identifier") if i.get("type") == "ukncn"), None)
        out.append({"title": (e.findtext(f"{ATOM}title") or "").strip(), "url": link, "published": (e.findtext(f"{ATOM}published") or "")[:10],
                    "court": (e.findtext(f"{ATOM}author/{ATOM}name") or "").strip(), "citation": ident,
                    "id": (e.findtext(f"{ATOM}id") or link or "").rsplit("/", 1)[-1]})
    return out


@register
class UkFindCaseLaw(Connector):
    manifest = Manifest(
        name="uk_find_case_law", title="UK — Find Case Law (The National Archives)", category="legal_records",
        description="Published judgments of UK courts and tribunals that mention a person or organization.", access="free",
        rate_limit=RateLimit(0.5, 1), website="https://caselaw.nationalarchives.gov.uk/",
        documentation="https://nationalarchives.github.io/ds-find-caselaw-docs/public", reliability="official",
        public_record=True, jurisdictions=("GB",), supported_countries=("GB",), license="Open Justice Licence",
        legal="Open Justice Licence; computational analysis across the corpus needs a separate licence (not done here — "
              "one search per name).",
        capabilities=("judgments mentioning the name", "court, neutral citation, date"),
        disclosure=Disclosure(("caselaw.nationalarchives.gov.uk",), "the name as an exact search"),
    )

    @transform(inputs=("person", "organization"), outputs=("public_record", "name"),
               title="Find Case Law: UK judgments mentioning this name", priority=0.5,
               accepts=lambda self, e, info: (e.type == "person" and e.is_seed and bool(person_names(e, info)))
               or (e.type == "organization" and str(e.get("jurisdiction") or "GB").upper().startswith("GB")))
    async def judgments(self, entity, ctx):
        name = person_names(entity, ctx.info)[0] if entity.type == "person" else str(entity.get("name") or entity.label)
        resp = await ctx.get("https://caselaw.nationalarchives.gov.uk/atom.xml", params={"query": f'"{name}"', "per_page": 20})
        body = ctx.expect(resp).content
        try:
            entries = parse_find_case_law(body)
        except (ParseError, ValueError):          # not XML, or XML defusedxml refuses (DTDs, entities)
            raise SourceUnavailable("Find Case Law returned a response that is not a valid Atom feed") from None
        if not entries:
            raise NotFound("No published judgment mentions this name")
        for j in entries:
            src = ctx.out.source(j["url"], title=j["title"], request=f"“{name}”")
            add_record(ctx.out, src, key=f"uk-fcl:{j['id']}", title=j["title"], record_type="judgment", jurisdiction="GB",
                       authority=j["court"], official_url=j["url"],
                       matched_name=name if entity.type == "person" else None, name_match="mentioned in text",
                       link_from=entity.key if entity.type == "organization" else None,
                       props={"reference": j["citation"], "date": j["published"],
                              "summary": "The name appears in the judgment text — the person may be a party, witness, "
                                         "lawyer, judge or someone else entirely."})
