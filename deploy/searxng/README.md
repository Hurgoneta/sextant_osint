# Self-hosted SearXNG for Sextant

[SearXNG](https://docs.searxng.org/) is an open-source metasearch engine. Running your own instance is the most
private way to give Sextant web and document search:

* Your queries go **from your machine** to the search engines SearXNG is configured to use. No search API provider
  sits in between, and there is no account or key.
* Sextant talks to the instance's JSON API on `127.0.0.1` only. The container is never reachable from your network.
* Sextant's SSRF guard normally blocks private addresses. It makes an exception **only** for the exact host you
  enter as `SEARXNG_URL`.

## Setup (Docker or Podman)

```bash
cd deploy/searxng
./setup.sh                      # generates a secret key and runs `docker compose up -d`
```

Then in Sextant open **Data Sources → Search engines → SearXNG (self-hosted) → Configure**, enter
`http://127.0.0.1:8888` and save (or put `SEARXNG_URL=http://127.0.0.1:8888` in `.env`). The source turns
*Available*, and the person/identifier/document search transforms start using it.

Without Docker, follow SearXNG's [installation guide](https://docs.searxng.org/admin/installation.html) and copy the
`search.formats` block from `settings.yml`. JSON output is required.

## Honest caveats

* SearXNG queries Google, Bing, DuckDuckGo, Brave and others **from your own IP address**. Those engines may
  rate-limit or show CAPTCHAs to it. When every engine fails, Sextant records the transform as *source unavailable*,
  naming the engines and their errors, never as *nothing found*.
* The engines still see your queries, which contain your identifiers. They just aren't linked to a search-API
  account. Routing SearXNG through Tor or a VPN is possible (`outgoing.proxies` in `settings.yml`) but slows it
  down and triggers more CAPTCHAs.
* Sextant caps search queries per provider and investigation (default 25, raised by the depth preset), and every
  query counts against the investigation's request budget.

## Files

| File | Purpose |
|---|---|
| `docker-compose.yml` | Container bound to `127.0.0.1:8888`, all capabilities dropped except those SearXNG needs, `no-new-privileges` |
| `settings.yml` | JSON output on, metrics/autocomplete off, limiter off (single local user), image proxy on |
| `setup.sh` | One-time random `secret_key`, then starts the container |
