#!/usr/bin/env sh
# Generates a random secret key for SearXNG (once) and starts the container.
set -eu
cd "$(dirname "$0")"

if grep -q "CHANGE_ME_RUN_SETUP_SH" settings.yml; then
  KEY=$(python3 -c "import secrets; print(secrets.token_hex(32))" 2>/dev/null || openssl rand -hex 32)
  sed -i.bak "s/CHANGE_ME_RUN_SETUP_SH/${KEY}/" settings.yml && rm -f settings.yml.bak
  echo "Generated a new SearXNG secret key."
fi

if command -v docker >/dev/null 2>&1; then
  docker compose up -d
elif command -v podman >/dev/null 2>&1; then
  podman compose up -d 2>/dev/null || podman-compose up -d
else
  echo "Docker or Podman is required: https://docs.docker.com/engine/install/" >&2
  exit 1
fi

echo
echo "SearXNG is starting on http://127.0.0.1:8888"
echo "In Sextant: Data Sources -> SearXNG (self-hosted) -> Configure -> http://127.0.0.1:8888"
