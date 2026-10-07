"""Adapters for established open-source OSINT tools, run as separate processes.

Why subprocesses: it keeps licences apart (theHarvester is GPL-2.0; running it as a separate program
does not make Sextant a derivative work) and isolates heavy dependencies. Safety rules:

* argv lists only — no shell, ever; inputs are validated (no leading "-", strict character sets)
* a minimal environment (PATH, HOME=temp dir, LANG); proxy variables are stripped
* output goes to a fresh temporary directory, is size-capped and parsed as data
* hard timeout; the process group is killed on timeout or cancellation
* evasion features are never enabled: Maigret's ``--cloudflare-bypass``, ``--use-disabled-sites``, cookies and proxies;
  theHarvester's ``--dns-brute``, ``--take-over``, ``--screenshot``, ``--api-scan``; leak-data sources are refused

These tools make their own network requests — they do NOT pass through Sextant's SSRF guard, rate
limiter or robots.txt policy. The catalog says so, and they are off until installed and enabled.
"""

from __future__ import annotations

import asyncio
import csv
import json
import os
import re
import shutil
import signal
import sys
import tempfile
from pathlib import Path

from ...registry.platforms import parse_profile_url
from ...sdk import Connector, Credential, Disclosure, Manifest, RateLimit, register, transform
from ...sdk.errors import NotFound, Skipped, SourceUnavailable
from ...sdk.http import FetchError
from ...util.normalize import canonical_url, normalize_domain, normalize_email, normalize_ip, normalize_name
from .._common import add_account, add_link_target
from .._policy import adult, forbidden_reason

SAFE_USERNAME = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$")
MAX_OUTPUT = 20 * 1024 * 1024


def find_tool(env_key: str, names: tuple[str, ...], cfg=None) -> str | None:
    configured = (cfg.credential(env_key) if cfg is not None else None) or os.environ.get(env_key)
    if configured:
        p = Path(configured).expanduser()
        return str(p) if p.exists() else None
    for n in names:
        found = shutil.which(n)
        if found:
            return found
    return None


async def run_tool(argv: list[str], *, timeout: float, workdir: Path) -> tuple[int, str, str]:
    env = {"PATH": os.environ.get("PATH", "/usr/bin:/bin"), "HOME": str(workdir), "LANG": "C.UTF-8",
           "LC_ALL": "C.UTF-8", "PYTHONIOENCODING": "utf-8", "NO_COLOR": "1"}
    kwargs = {"start_new_session": True} if sys.platform != "win32" else {}
    proc = await asyncio.create_subprocess_exec(*argv, cwd=str(workdir), env=env, stdin=asyncio.subprocess.DEVNULL,
                                                stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE, **kwargs)
    try:
        out, err = await asyncio.wait_for(proc.communicate(), timeout)
    except (asyncio.TimeoutError, asyncio.CancelledError):
        try:
            if sys.platform != "win32":
                os.killpg(proc.pid, signal.SIGKILL)
            else:  # pragma: no cover
                proc.kill()
        except ProcessLookupError:
            pass
        await proc.wait()
        raise
    return proc.returncode or 0, out[:MAX_OUTPUT].decode("utf-8", "replace"), err[-20000:].decode("utf-8", "replace")


def tool_python(exe: str) -> str | None:
    """The interpreter of a pip/pipx-installed tool, from the shebang of its launcher."""
    try:
        first = Path(exe).read_bytes()[:300].split(b"\n", 1)[0].decode("utf-8", "replace")
    except OSError:
        return None
    return first[2:].strip().split()[0] if first.startswith("#!") else None


def tool_eval(exe: str, code: str, *args: str, timeout: float = 60) -> str | None:
    """Runs a short snippet in the tool's own interpreter (isolated mode, no shell); stdout or None."""
    import subprocess
    python = tool_python(exe)
    if not python:
        return None
    try:
        res = subprocess.run([python, "-I", "-c", code, *args], capture_output=True, text=True, timeout=timeout,
                             env={"PATH": os.environ.get("PATH", "")})
    except (OSError, subprocess.SubprocessError):
        return None
    return res.stdout.strip() if res.returncode == 0 else None


def tool_package_file(exe: str, module: str, relpath: str) -> Path | None:
    """The installed tool's bundled file (e.g. its site database)."""
    origin = tool_eval(exe, "import importlib.util,sys;print(importlib.util.find_spec(sys.argv[1]).origin)", module, timeout=30)
    f = Path(origin).parent / relpath if origin else None
    return f if f and f.exists() else None


MAIGRET_META_URL = "https://raw.githubusercontent.com/soxoj/maigret/main/maigret/resources/db_meta.json"
MAIGRET_DB_REFRESH = 86_400


def _version(v: str) -> tuple:
    try:
        return tuple(int(x) for x in str(v).strip().split("."))
    except ValueError:
        return (0,)


async def current_maigret_db(ctx, exe: str) -> tuple[Path | None, str]:
    """Maigret's current site database through Maigret's own update channel (db_meta.json): the installed version
    must satisfy ``min_maigret_version``, the file must match the published SHA-256 and have the expected structure.
    Cached in the data folder for a day. Returns (path, description) or (None, reason) to fall back to the bundled one."""
    import hashlib
    import time
    cache = ctx.data_dir / "maigret" if ctx.data_dir else None
    cached = cache / "data.json" if cache else None
    meta_path = cache / "db_meta.json" if cache else None
    if cached and cached.exists() and meta_path.exists() and time.time() - cached.stat().st_mtime < MAIGRET_DB_REFRESH:
        meta = json.loads(meta_path.read_text(encoding="utf-8"))
        return cached, f"current database of {str(meta.get('updated_at', ''))[:10]}"
    installed = tool_eval(exe, "from maigret.__version__ import __version__ as v; print(v)", timeout=30) or "0"
    try:
        resp = await ctx.get(MAIGRET_META_URL, cache_ttl=None, retries=1, evidence=False)
        meta = resp.json() if resp.status == 200 else None
        if not isinstance(meta, dict) or not meta.get("data_url") or not meta.get("data_sha256"):
            raise ValueError("no usable db_meta.json")
        if _version(installed) < _version(meta.get("min_maigret_version", "0")):
            return None, f"current database needs Maigret ≥ {meta['min_maigret_version']} (installed {installed})"
        data = await ctx.get(str(meta["data_url"]), cache_ttl=None, retries=1, max_bytes=30_000_000, evidence=False)
        if data.status != 200 or hashlib.sha256(data.content).hexdigest() != meta["data_sha256"]:
            raise ValueError("download failed or SHA-256 mismatch")
        doc = json.loads(data.content)
        if not all(k in doc for k in ("sites", "engines", "tags")):
            raise ValueError("unexpected database structure")
    except (FetchError, ValueError) as exc:
        if cached and cached.exists():
            return cached, f"cached current database (refresh failed: {exc})"
        return None, f"current database unavailable ({exc})"
    if cache:
        cache.mkdir(parents=True, exist_ok=True)
        tmp = cached.with_suffix(".tmp")
        tmp.write_bytes(data.content)
        os.replace(tmp, cached)
        meta_path.write_text(json.dumps(meta), encoding="utf-8")
        return cached, f"current database of {str(meta.get('updated_at', ''))[:10]}"
    scratch = Path(tempfile.mkdtemp(prefix="sextant-maigret-db-")) / "data.json"
    scratch.write_bytes(data.content)
    return scratch, f"current database of {str(meta.get('updated_at', ''))[:10]}"


def write_policy_db(src: Path, dest: Path, kind: str, include_adult: bool) -> tuple[int, int]:
    """Copy of the tool's site database without the sites Sextant's policy excludes. Returns (kept, excluded)."""
    doc = json.loads(src.read_text(encoding="utf-8"))
    sites = doc.get("sites", {}) if kind == "maigret" else {k: v for k, v in doc.items() if not k.startswith("$")}
    keep, dropped = {}, 0
    for name, site in sites.items():
        site = site or {}
        urls = (site.get("urlMain"), site.get("url"), site.get("urlProbe"))
        tags = site.get("tags") or (["nsfw"] if site.get("isNSFW") else [])
        if forbidden_reason(name, *urls, source=site.get("source")) or (not include_adult and adult(tags)):
            dropped += 1
            continue
        keep[name] = site
    if kind == "maigret":
        out = {**doc, "sites": keep}
    else:
        out = {**{k: v for k, v in doc.items() if k.startswith("$")}, **keep}
    dest.write_text(json.dumps(out), encoding="utf-8")
    return len(keep), dropped


class ToolAdapter(Connector):
    tool_env: str = ""
    tool_names: tuple[str, ...] = ()

    def installed(self, cfg=None) -> tuple[bool, str]:
        path = find_tool(self.tool_env, self.tool_names, cfg)
        if path:
            return True, path
        return False, f"Not found — install it and make sure “{self.tool_names[0]}” is on PATH, or set {self.tool_env}"

    def _exe(self, ctx) -> str:
        path = find_tool(self.tool_env, self.tool_names, None) or (ctx.credential(self.tool_env) or "")
        if not path:
            raise SourceUnavailable(f"{self.manifest.title} is not installed")
        return path


def parse_maigret_report(doc: dict) -> list[dict]:
    """Maigret ``-J simple`` report → [{site, url, username, tags, ids}] for CLAIMED results only."""
    out = []
    for site, data in (doc or {}).items():
        st = (data or {}).get("status") or {}
        if str(st.get("status", "")).lower() != "claimed":
            continue
        out.append({"site": site, "url": st.get("url") or data.get("url_user"), "username": st.get("username") or data.get("username"),
                    "tags": st.get("tags") or [], "ids": st.get("ids") or {}})
    return out


@register
class MaigretAdapter(ToolAdapter):
    tool_env, tool_names = "MAIGRET_PATH", ("maigret",)
    manifest = Manifest(
        name="maigret", title="Maigret (adapter)", category="adapters", kind="adapter",
        description="Runs Maigret (MIT, ~7,000 sites) for a username and imports the claimed accounts with the profile "
                    "data Maigret parsed: its top sites (setting maigret_top_sites, default 500) normally, every site with "
                    "SEARCH EVERYTHING. Policy-excluded platforms are removed from its site list first; bypass features "
                    "are never enabled.", access="tool",
        credentials=(Credential("MAIGRET_PATH", "Path to the maigret executable", "Optional if “maigret” is on PATH "
                                "(pip install maigret).", required=False, secret=False,
                                obtain_url="https://github.com/soxoj/maigret"),),
        rate_limit=RateLimit(1, 1), website="https://github.com/soxoj/maigret", documentation="https://maigret.readthedocs.io/",
        reliability="medium", official_api=False, access_method="tool", license="MIT (tool)", timeout=1800,
        max_concurrency=1, default_enabled=True,
        legal="Maigret sends requests that look like a browser to each site and does not pass through Sextant's SSRF "
              "guard, rate limiter or robots.txt policy. Sextant gives it a filtered copy of its site list: platforms "
              "whose terms forbid automation (and their third-party mirrors) and, unless enabled in Settings, adult and "
              "dating sites are removed. You are responsible for using it within the sites' terms.",
        capabilities=("accounts on ~7,000 sites", "profile fields parsed by Maigret (names, links, ids)"),
        disclosure=Disclosure(("each site Maigret checks (directly, not via Sextant)",), "the username", "tool"),
    )

    @transform(inputs=("username",), outputs=("account", "domain", "name"), title="Maigret: username on ~7,000 sites",
               priority=0.5, estimated_requests=0)
    async def search(self, entity, ctx):
        u = entity.display
        if not SAFE_USERNAME.fullmatch(u):
            raise Skipped("Username not safe to pass to an external tool")
        exe = self._exe(ctx)
        top = int(ctx.setting("maigret_top_sites", 500))
        source, which = None, "bundled database"
        if str(ctx.setting("maigret_database", "current")) == "current":
            source, which = await current_maigret_db(ctx, exe)
            if source is None:
                which = f"bundled database — {which}"
        source = source or tool_package_file(exe, "maigret", "resources/data.json")
        if source is None:
            raise SourceUnavailable("Could not find Maigret's site database to apply Sextant's site policy — not run")
        with tempfile.TemporaryDirectory(prefix="sextant-maigret-") as tmp:
            db = Path(tmp) / "sites-policy.json"
            kept, excluded = write_policy_db(source, db, "maigret", bool(ctx.info.option("include_adult")))
            loaded = tool_eval(exe, "import sys;from maigret.sites import MaigretDatabase as D;print(len(D().load_from_file(sys.argv[1]).sites))",
                               str(db), timeout=120)
            if not loaded or not loaded.isdigit():
                raise SourceUnavailable("Maigret could not load the policy-filtered site list — not run (it would fall back "
                                        "to its unfiltered bundled list)")
            scope = ["-a"] if ctx.info.option("search_everything") else ["--top-sites", str(max(50, min(top, 8000)))]
            argv = [exe, u, "--db", str(db), "--no-autoupdate", "--json", "simple", "--folderoutput", tmp,
                    "--no-progressbar", "--no-color", "--no-recursion", "--timeout", "15", "--retries", "1", "-n", "20", *scope]
            code, out, err = await run_tool(argv, timeout=self.manifest.timeout - 30, workdir=Path(tmp))
            if "Falling back to bundled database" in out + err or f"Using sites database: {db}" not in out + err:
                raise SourceUnavailable("Maigret did not use the policy-filtered site list — its results were discarded")
            reports = list(Path(tmp).rglob("*_simple.json"))
            if not reports:
                raise SourceUnavailable(f"Maigret produced no report (exit {code}): {err.strip()[-300:]}")
            doc = json.loads(reports[0].read_text(encoding="utf-8", errors="replace")[:MAX_OUTPUT])
        found = parse_maigret_report(doc)
        scope_note = (f"{'all' if ctx.info.option('search_everything') else f'top {top}'} of {kept} sites from the "
                      f"{which}; {excluded} excluded by Sextant's site policy")
        if not found:
            raise NotFound(f"Maigret found no accounts ({scope_note})")
        ctx.out.set_status("found", f"{len(found)} accounts reported ({scope_note})")
        for f in found:
            url = canonical_url(str(f["url"] or ""))
            src = ctx.out.source(url, title=f"Maigret: {f['site']}", kind="tool", request=f"username “{u}”")
            known = parse_profile_url(url) if url else None
            ids = f["ids"] or {}
            links = [x for x in (ids.get("links") if isinstance(ids.get("links"), list) else
                                 re.findall(r"https?://[^\s'\",\]]+", str(ids.get("links") or "")))]
            acc = add_account(ctx.out, src, platform=known[0].id if known else f"maigret:{f['site'].lower()}",
                              username=str(f["username"] or u), link_from=entity.key, found_by="adapter",
                              platform_title=known[0].name if known else f["site"],
                              profile={"profile_url": url, "display_name": ids.get("fullname") or ids.get("name"),
                                       "location": ids.get("location") or ids.get("country"), "bio": ids.get("bio"),
                                       "created_at": ids.get("created_at"), "links": links,
                                       "extra": {"maigret_tags": f["tags"], "maigret_ids": {k: str(v)[:200] for k, v in ids.items()}}})
            ctx.out.check(known[0].id if known else f"maigret:{f['site'].lower()}", "found", url=url, account=acc,
                          platform_title=f["site"], detail="Reported by Maigret")


@register
class SherlockAdapter(ToolAdapter):
    tool_env, tool_names = "SHERLOCK_PATH", ("sherlock",)
    manifest = Manifest(
        name="sherlock", title="Sherlock (adapter)", category="adapters", kind="adapter",
        description="Runs Sherlock (MIT, ~400 sites) for a username and imports the claimed profile URLs. Policy-excluded "
                    "platforms are removed from its site list first.", access="tool",
        credentials=(Credential("SHERLOCK_PATH", "Path to the sherlock executable", "Optional if “sherlock” is on PATH "
                                "(pipx install sherlock-project).", required=False, secret=False,
                                obtain_url="https://github.com/sherlock-project/sherlock"),),
        rate_limit=RateLimit(1, 1), website="https://github.com/sherlock-project/sherlock",
        documentation="https://sherlockproject.xyz/", reliability="medium", official_api=False, access_method="tool",
        license="MIT (tool)", timeout=600, max_concurrency=1, default_enabled=True,
        legal="Sherlock's requests do not pass through Sextant's SSRF guard, rate limiter or robots.txt policy. Sextant "
              "gives it a filtered copy of its site list without platforms whose terms forbid automation; Sherlock skips "
              "NSFW sites by default.",
        capabilities=("profile URLs on ~400 sites",),
        disclosure=Disclosure(("each site Sherlock checks (directly, not via Sextant)",), "the username", "tool"),
    )

    @transform(inputs=("username",), outputs=("account",), title="Sherlock: username on ~400 sites", priority=0.4,
               estimated_requests=0)
    async def search(self, entity, ctx):
        u = entity.display
        if not SAFE_USERNAME.fullmatch(u):
            raise Skipped("Username not safe to pass to an external tool")
        exe = self._exe(ctx)
        bundled = tool_package_file(exe, "sherlock_project", "resources/data.json")
        if bundled is None:
            raise SourceUnavailable("Could not find Sherlock's site database to apply Sextant's site policy — not run")
        with tempfile.TemporaryDirectory(prefix="sextant-sherlock-") as tmp:
            db = Path(tmp) / "sites-policy.json"
            kept, excluded = write_policy_db(bundled, db, "sherlock", bool(ctx.info.option("include_adult")))
            argv = [exe, u, "--json", str(db), "--csv", "--folderoutput", tmp, "--no-color", "--print-found",
                    "--timeout", "15"]
            code, out, err = await run_tool(argv, timeout=self.manifest.timeout - 30, workdir=Path(tmp))
            path = Path(tmp) / f"{u}.csv"
            if not path.exists():
                raise SourceUnavailable(f"Sherlock produced no CSV (exit {code}): {err.strip()[-300:]}")
            rows = list(csv.DictReader(path.read_text(encoding="utf-8", errors="replace").splitlines()))
        found = [r for r in rows if str(r.get("exists", "")).lower() in ("claimed", "true")]
        note = f"{kept} sites; {excluded} excluded by Sextant's site policy"
        if not found:
            raise NotFound(f"Sherlock found no accounts ({note})")
        ctx.out.set_status("found", f"{len(found)} accounts reported ({note})")
        for r in found:
            url = canonical_url(str(r.get("url_user") or ""))
            if not url:
                continue
            src = ctx.out.source(url, title=f"Sherlock: {r.get('name')}", kind="tool", request=f"username “{u}”")
            known = parse_profile_url(url)
            acc = add_account(ctx.out, src, platform=known[0].id if known else f"sherlock:{str(r.get('name')).lower()}",
                              username=u, link_from=entity.key, found_by="adapter",
                              platform_title=known[0].name if known else r.get("name"), profile={"profile_url": url})
            ctx.out.check(known[0].id if known else f"sherlock:{str(r.get('name')).lower()}", "found", url=url, account=acc,
                          platform_title=r.get("name") or "", detail="Reported by Sherlock")


PASSIVE_HARVESTER = "crtsh,certspotter,hackertarget,rapiddns,otx,urlscan,waybackarchive,duckduckgo,subdomaincenter,thc"
REFUSED_HARVESTER = {"dehashed", "intelx", "leaklookup", "hudsonrock", "haveibeenpwned", "hibpverified", "takeover"}


def parse_harvester(doc: dict) -> dict[str, list[str]]:
    hosts = []
    for h in doc.get("hosts") or []:
        hosts.append(str(h).split(":", 1)[0])
    return {"emails": [str(e) for e in doc.get("emails") or []], "hosts": hosts,
            "ips": [str(i) for i in doc.get("ips") or []], "urls": [str(u) for u in doc.get("interesting_urls") or []],
            "people": [str(p) for p in doc.get("people") or doc.get("linkedin_people") or []]}


@register
class TheHarvesterAdapter(ToolAdapter):
    tool_env, tool_names = "THEHARVESTER_PATH", ("theHarvester", "theharvester")
    manifest = Manifest(
        name="theharvester", title="theHarvester (adapter)", category="adapters", kind="adapter",
        description="Runs theHarvester (GPL-2.0, separate process) with passive sources for a domain: emails, hosts, IPs.",
        access="tool",
        credentials=(Credential("THEHARVESTER_PATH", "Path to theHarvester", "Optional if on PATH. Requires Python ≥ 3.14.",
                                required=False, secret=False, obtain_url="https://github.com/laramies/theHarvester"),),
        rate_limit=RateLimit(1, 1), website="https://github.com/laramies/theHarvester",
        documentation="https://github.com/laramies/theHarvester/wiki", reliability="medium", official_api=False,
        access_method="tool", license="GPL-2.0 (tool — run as a separate program)", timeout=900, max_concurrency=1,
        default_enabled=False,
        opt_in_reason="External tool: its requests bypass Sextant's guards. Only passive sources are used.",
        capabilities=("emails at a domain", "subdomains/hosts", "IP addresses"),
        disclosure=Disclosure(("passive sources chosen in Settings (crt.sh, OTX, urlscan…)",), "the domain", "tool"),
    )

    @transform(inputs=("domain",), outputs=("email", "domain", "ip"), title="theHarvester: passive emails and hosts",
               priority=0.4, estimated_requests=0, accepts=lambda self, e, info: not e.get("subdomain"))
    async def harvest(self, entity, ctx):
        d = normalize_domain(entity.value)
        if not d or d.startswith("-"):
            raise Skipped("Invalid domain")
        sources = [s.strip() for s in str(ctx.setting("theharvester_sources", PASSIVE_HARVESTER)).split(",") if s.strip()]
        sources = [s for s in sources if re.fullmatch(r"[a-z0-9_-]{2,30}", s) and s.lower() not in REFUSED_HARVESTER]
        exe = self._exe(ctx)
        with tempfile.TemporaryDirectory(prefix="sextant-harvester-") as tmp:
            base = Path(tmp) / "result"
            argv = [exe, "-d", d, "-b", ",".join(sources), "-l", "200", "-f", str(base)]
            code, out, err = await run_tool(argv, timeout=self.manifest.timeout - 30, workdir=Path(tmp))
            path = base.with_suffix(".json")
            if not path.exists():
                raise SourceUnavailable(f"theHarvester produced no JSON (exit {code}): {err.strip()[-300:]}")
            res = parse_harvester(json.loads(path.read_text(encoding="utf-8", errors="replace")[:MAX_OUTPUT]))
        src = ctx.out.source(f"theharvester:{d}", title="theHarvester (passive sources)", kind="tool",
                             request=f"domain “{d}”, sources {','.join(sources)}")
        for e in res["emails"][:100]:
            ek = ctx.out.entity("email", normalize_email(e) or "", source=src)
            ctx.out.link(ek, entity.key, "REGISTERED_TO", source=src, confidence=0.7, evidence="Address at the domain (theHarvester)")
        for h in res["hosts"][:100]:
            hk = ctx.out.entity("domain", h, source=src, props={"subdomain": True})
            if hk and hk != entity.key:
                ctx.out.link(entity.key, hk, "HAS_SUBDOMAIN", source=src, confidence=0.8, evidence="Host found by theHarvester")
        for ip in res["ips"][:50]:
            if normalize_ip(ip):
                ik = ctx.out.entity("ip", ip, source=src)
                ctx.out.link(entity.key, ik, "RESOLVES_TO", source=src, confidence=0.5, evidence="IP reported by theHarvester")


SPIDERFOOT_MAP = {"EMAILADDR": "email", "PHONE_NUMBER": "phone", "USERNAME": "username", "HUMAN_NAME": "name",
                  "INTERNET_NAME": "domain", "DOMAIN_NAME": "domain", "AFFILIATE_DOMAIN_NAME": "domain", "IP_ADDRESS": "ip",
                  "COMPANY_NAME": "organization", "AFFILIATE_COMPANY_NAME": "organization", "GEOINFO": "location",
                  "SOCIAL_MEDIA": "account", "ACCOUNT_EXTERNAL_OWNED": "account", "LINKED_URL_EXTERNAL": "website"}


def parse_spiderfoot(events: list[dict]) -> list[tuple[str, str, str]]:
    """SpiderFoot JSON events → (entity type, value, module) — only types with a clear ontology mapping."""
    out = []
    for ev in events or []:
        t = SPIDERFOOT_MAP.get(str(ev.get("type")))
        data = str(ev.get("data") or "").strip()
        if not t or not data:
            continue
        if t == "account":
            m = re.search(r"https?://[^\s<]+", data)
            if not m:
                continue
            data = m.group(0)
        out.append((t, data[:500], str(ev.get("module") or "")))
    return out


@register
class SpiderFootAdapter(ToolAdapter):
    tool_env, tool_names = "SPIDERFOOT_PATH", ("sf.py", "spiderfoot")
    manifest = Manifest(
        name="spiderfoot", title="SpiderFoot (adapter)", category="adapters", kind="adapter",
        description="Runs SpiderFoot's passive modules (MIT) against a domain, email, username or name and maps its events "
                    "to entities.", access="tool",
        credentials=(Credential("SPIDERFOOT_PATH", "Path to SpiderFoot's sf.py", "SpiderFoot v4.0 (2022) — no longer "
                                "actively maintained.", required=False, secret=False,
                                obtain_url="https://github.com/smicallef/spiderfoot"),),
        rate_limit=RateLimit(1, 1), website="https://github.com/smicallef/spiderfoot", documentation="https://intel471.com/solutions/spiderfoot",
        reliability="medium", official_api=False, access_method="tool", license="MIT (tool)", timeout=1500,
        max_concurrency=1, default_enabled=False, status="beta",
        status_note="Last release 2022, last code commit 2023-11 — effectively unmaintained.",
        opt_in_reason="Slow, unmaintained, and its requests bypass Sextant's guards.",
        capabilities=("events from SpiderFoot's passive modules mapped to entities",),
        disclosure=Disclosure(("whatever SpiderFoot's passive modules contact",), "the target value", "tool"),
    )

    @transform(inputs=("domain", "email", "username"), outputs=("email", "phone", "domain", "ip", "account", "organization"),
               title="SpiderFoot: passive scan", priority=0.3, estimated_requests=0, auto=False)
    async def scan(self, entity, ctx):
        target = entity.display if entity.type == "username" else entity.value
        if target.startswith("-") or not re.fullmatch(r"[A-Za-z0-9@._+-]{2,253}", target):
            raise Skipped("Target not safe to pass to an external tool")
        exe = self._exe(ctx)
        python = ctx.credential("SPIDERFOOT_PYTHON") or shutil.which("python3") or sys.executable
        argv = ([python, exe] if exe.endswith(".py") else [exe]) + ["-s", target, "-u", "passive", "-o", "json", "-q"]
        with tempfile.TemporaryDirectory(prefix="sextant-spiderfoot-") as tmp:
            code, out, err = await run_tool(argv, timeout=self.manifest.timeout - 30, workdir=Path(tmp))
        try:
            events = json.loads(out[out.index("["):]) if "[" in out else []
        except ValueError:
            raise SourceUnavailable(f"Unreadable SpiderFoot output (exit {code})") from None
        mapped = parse_spiderfoot(events)
        if not mapped:
            raise NotFound("SpiderFoot reported nothing mappable")
        src = ctx.out.source(f"spiderfoot:{target}", title="SpiderFoot passive scan", kind="tool", request=f"target “{target}”")
        for t, value, module in mapped[:300]:
            if t == "account":
                add_link_target(ctx.out, entity.key, value, src)
                continue
            if t == "name" and not normalize_name(value):
                continue
            key = ctx.out.entity(t, f"name:{value.casefold()}" if t == "organization" else value, label=value, source=src,
                                 evidence=f"SpiderFoot module {module}", confidence=0.6)
            if key and key != entity.key:
                ctx.out.link(entity.key, key, "ASSOCIATED_WITH", source=src, confidence=0.5, evidence=f"SpiderFoot ({module})")
