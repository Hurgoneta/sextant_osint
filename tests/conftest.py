import asyncio
import os
import sys
import tempfile
from pathlib import Path

# The offline suite must never launch the real external tools (Maigret, Sherlock, SpiderFoot, theHarvester), even on a
# machine where they are installed and switched on: point every adapter at a path that does not exist.
for _tool in ("MAIGRET_PATH", "SHERLOCK_PATH", "SPIDERFOOT_PATH", "THEHARVESTER_PATH"):
    os.environ[_tool] = "/nonexistent/" + _tool.lower()

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "backend"))

from sextant.config import Settings  # noqa: E402
from sextant.registry import connectors as REG  # noqa: E402
from sextant.sdk.testing import mock_http  # noqa: E402
from sextant.storage.crypto import Vault  # noqa: E402
from sextant.storage.db import Database  # noqa: E402

REG.load_all()


def run(coro):
    return asyncio.run(coro)


@pytest.fixture
def db(tmp_path):
    d = Database(tmp_path, Vault.open(tmp_path))
    yield d
    try:
        d.close()
    except Exception:
        pass


@pytest.fixture
def settings(db):
    return Settings(db)


def engine_with(db, settings, routes, calls=None):
    from sextant.engine.orchestrator import Engine
    return Engine(db, settings, db.data_dir, http_factory=lambda **kw: mock_http(routes, calls, **kw))


__all__ = ["run", "mock_http", "engine_with", "tempfile"]
