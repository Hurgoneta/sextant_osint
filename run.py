#!/usr/bin/env python3
"""Convenience launcher: `python run.py` from the project root (no installation needed)."""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent / "backend"))

from sextant.__main__ import main  # noqa: E402

if __name__ == "__main__":
    sys.exit(main())
