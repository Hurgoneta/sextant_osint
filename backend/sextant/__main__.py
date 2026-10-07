"""Launcher: ``python run.py`` (project root) or ``python -m sextant`` (from ``backend``).

Starts the local engine on 127.0.0.1 with a random per-launch session token and opens the UI.
"""

from __future__ import annotations

import argparse
import logging
import secrets
import socket
import sys
import webbrowser
from pathlib import Path

from . import APP_NAME, __version__
from .config import default_data_dir, default_frontend_dir, load_dotenv


def _free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="sextant", description=f"{APP_NAME} — local OSINT investigation platform")
    parser.add_argument("--port", type=int, default=0, help="port on 127.0.0.1 (default: a random free port)")
    parser.add_argument("--data-dir", type=Path, default=None, help="where the encrypted local database lives")
    parser.add_argument("--no-browser", action="store_true", help="don't open the browser")
    parser.add_argument("--env-file", type=Path, default=Path(".env"), help=".env file with optional API keys")
    parser.add_argument("--debug", action="store_true")
    args = parser.parse_args(argv)

    logging.basicConfig(level=logging.DEBUG if args.debug else logging.WARNING,
                        format="%(levelname)s %(name)s: %(message)s")
    loaded = load_dotenv(args.env_file)
    loaded += load_dotenv(Path(__file__).resolve().parents[2] / ".env")

    import uvicorn

    from .api.app import build_state, create_app

    port = args.port or _free_port()
    token = secrets.token_urlsafe(32)
    data_dir = (args.data_dir or default_data_dir()).expanduser().resolve()
    state = build_state(data_dir, default_frontend_dir(), token, port)
    app = create_app(state)

    url = f"http://127.0.0.1:{port}/#token={token}"
    print(f"\n  {APP_NAME} {__version__} — local OSINT investigation platform")
    print(f"  Local data:  {data_dir}  (encrypted, {state.db.vault.mode} mode)")
    if loaded:
        print(f"  Loaded from .env: {', '.join(sorted(set(loaded)))}")
    if not (default_frontend_dir() / "index.html").exists():
        print("  UI not built yet: cd frontend && npm install --no-bin-links && npm run build")
    print(f"  Open:        {url}")
    print("  The token in this link is your session key — don't share it. Ctrl+C to stop.\n")
    if not args.no_browser:
        webbrowser.open(url)
    uvicorn.run(app, host="127.0.0.1", port=port, log_level="debug" if args.debug else "warning",
                access_log=False, server_header=False, proxy_headers=False)
    return 0


if __name__ == "__main__":
    sys.exit(main())
