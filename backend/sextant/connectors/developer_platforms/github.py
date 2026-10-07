"""GitHub — official REST API (works unauthenticated; GITHUB_TOKEN raises the rate limit)."""

from __future__ import annotations

from urllib.parse import quote

from ...sdk import Connector, Credential, Disclosure, Manifest, RateLimit, register, transform
from ...sdk.errors import Blocked, NotFound, RateLimited, SourceUnavailable
from .._common import accepts_named_person, add_account, avatar_fingerprint, person_names

API = "https://api.github.com"
MAX_CANDIDATES = 4


@register
class GitHub(Connector):
    manifest = Manifest(
        name="github", title="GitHub", category="developer_platforms",
        description="Public GitHub profiles by username, by exact public email, by commit-author email and by full name "
                    "(narrowed by location when known).",
        access="free", credentials=(Credential("GITHUB_TOKEN", "GitHub token",
                                               "Fine-grained token with no scopes; raises the limit from 60 to 5,000 requests/hour.",
                                               required=False, obtain_url="https://github.com/settings/tokens"),),
        cost="Free", rate_limit=RateLimit(0.5, 3, "60 requests/hour unauthenticated, 5,000/hour with a token"),
        website="https://github.com", documentation="https://docs.github.com/rest", reliability="high",
        coverage="All public GitHub user profiles and public commits",
        capabilities=("account by username", "accounts whose public email matches", "public commits authored with an email",
                      "accounts by full name", "name, bio, location, company, website, linked social accounts, avatar"),
        disclosure=Disclosure(("api.github.com",), "usernames, email addresses and names as search terms"),
        terms_url="https://docs.github.com/site-policy/github-terms/github-terms-of-service",
    )

    def _headers(self, ctx) -> dict:
        h = {"Accept": "application/vnd.github+json", "X-GitHub-Api-Version": "2022-11-28"}
        token = ctx.credential("GITHUB_TOKEN")
        if token:
            h["Authorization"] = f"Bearer {token}"
        return h

    async def _get(self, ctx, path: str, **kw):
        resp = await ctx.get(API + path, headers=self._headers(ctx), **kw)
        if resp.status in (403, 429) and resp.headers.get("x-ratelimit-remaining") == "0":
            raise RateLimited("GitHub API rate limit reached — add a GITHUB_TOKEN or retry later")
        if resp.status in (401, 403):
            raise Blocked(f"GitHub refused the request (HTTP {resp.status})")
        return resp

    async def _user(self, ctx, login: str, *, link_from: str | None, found_by: str, link_confidence: float = 1.0):
        resp = await self._get(ctx, f"/users/{quote(login, safe='')}")
        if resp.status == 404:
            return None
        if resp.status != 200:
            raise SourceUnavailable(f"GitHub answered HTTP {resp.status}")
        u = ctx.json(resp)
        if not isinstance(u, dict) or not u.get("login"):
            raise SourceUnavailable("GitHub returned an unexpected user record")
        social = []
        sresp = await self._get(ctx, f"/users/{quote(login, safe='')}/social_accounts")
        if sresp.status == 200:
            social = [s.get("url") for s in ctx.json(sresp) or [] if isinstance(s, dict) and s.get("url")]
        if u.get("twitter_username"):
            social.append(f"https://x.com/{u['twitter_username']}")
        src = ctx.out.source(f"{API}/users/{u.get('login')}", title="GitHub REST API — user", request=f"login “{login}”")
        profile = {
            "username": u.get("login"), "display_name": u.get("name"), "bio": u.get("bio"), "location": u.get("location"),
            "website": u.get("blog") or None, "company": u.get("company"), "created_at": u.get("created_at"),
            "avatar_url": u.get("avatar_url"), "profile_url": u.get("html_url"), "followers": u.get("followers"),
            "emails": [u["email"]] if u.get("email") else [], "links": social, "user_id": str(u.get("id") or ""),
            "avatar_fp": await avatar_fingerprint(ctx, u.get("avatar_url")),
            "extra": {"public_repos": u.get("public_repos"), "type": u.get("type"), "hireable": u.get("hireable")},
        }
        acc = add_account(ctx.out, src, platform="github", username=u["login"], profile=profile, link_from=link_from,
                          found_by=found_by, link_confidence=link_confidence)
        return acc, u

    async def _search_logins(self, ctx, q: str) -> list[str]:
        resp = await self._get(ctx, "/search/users", params={"q": q, "per_page": MAX_CANDIDATES})
        if resp.status == 422:
            return []
        if resp.status != 200:
            raise SourceUnavailable(f"GitHub search answered HTTP {resp.status}")
        return [i["login"] for i in (ctx.json(resp) or {}).get("items", [])[:MAX_CANDIDATES] if i.get("login")]

    # ------------------------------------------------------------------ transforms
    @transform(inputs=("username",), outputs=("account", "email", "domain", "location", "organization", "image", "name"),
               title="GitHub: find account by username", priority=0.8)
    async def username_lookup(self, entity, ctx):
        res = await self._user(ctx, entity.display, link_from=entity.key, found_by="username")
        if res is None:
            ctx.out.check("github", "not_found", platform_title="GitHub")
            raise NotFound(f"No GitHub user “{entity.display}”")
        ctx.out.check("github", "found", url=res[1].get("html_url"), account=res[0], platform_title="GitHub")

    @transform(inputs=("account",), outputs=("account", "email", "domain", "location", "organization", "image"),
               title="GitHub: refresh full profile", priority=0.6,
               accepts=lambda self, e, info: e.get("platform") == "github" and e.get("found_by") in ("link", "search_result"))
    async def account_profile(self, entity, ctx):
        res = await self._user(ctx, entity.display, link_from=None, found_by="profile")
        if res is None:
            raise NotFound("Linked GitHub profile no longer exists")

    @transform(inputs=("email",), outputs=("account", "website"), title="GitHub: accounts and commits for this email",
               priority=0.8, estimated_requests=4, sends="email address “{value}” (user search + commit-author search)")
    async def email_lookup(self, entity, ctx):
        email = entity.value
        for login in await self._search_logins(ctx, f'"{email}" in:email'):
            await self._user(ctx, login, link_from=entity.key, found_by="email search", link_confidence=0.6)
        resp = await self._get(ctx, "/search/commits", params={"q": f"author-email:{email}", "sort": "author-date",
                                                              "order": "asc", "per_page": 10})
        if resp.status == 200:
            data = ctx.json(resp) or {}
            total = int(data.get("total_count") or 0)
            if total:
                url = f"https://github.com/search?q={quote('author-email:' + email)}&type=commits"
                src = ctx.out.source(url, title="GitHub commit search", request=f"author-email “{email}”")
                ctx.out.prop(entity.key, "public_commits", total, source=src,
                             evidence=f"Author email of {total} public commits on GitHub")
                authors = set()
                for item in data.get("items") or []:
                    a = (item.get("author") or {}).get("login")
                    if a and a not in authors and len(authors) < 3:
                        authors.add(a)
                        await self._user(ctx, a, link_from=entity.key, found_by="commit author email", link_confidence=0.9)
                    repo = (item.get("repository") or {}).get("html_url")
                    if repo:
                        w = ctx.out.entity("website", repo, label=(item.get("repository") or {}).get("full_name") or repo,
                                           source=src, props={"url": repo, "category": "code"})
                        ctx.out.link(entity.key, w, "MENTIONED_IN", source=src, evidence="Commit author email in this repository")

    @transform(inputs=("person",), outputs=("account",), title="GitHub: accounts with this full name", priority=0.5,
               accepts=accepts_named_person, sends="full name “{value}” (+ city when known)")
    async def name_search(self, entity, ctx):
        names = person_names(entity, ctx.info)
        q = f'fullname:"{names[0]}"'
        if ctx.info.locations and entity.is_seed:
            q += f' location:"{ctx.info.locations[0].split(",")[0].strip()}"'
        for login in await self._search_logins(ctx, q):
            await self._user(ctx, login, link_from=None, found_by="name search")
