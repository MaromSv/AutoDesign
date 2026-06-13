"""Tiny `.env` loader shared across entry points.

A minimal KEY=VALUE reader (no quoting/expansion magic, no extra dependency) that
populates `os.environ` without overwriting anything already set — so a real env var
always wins over the file. Used so `ANTHROPIC_API_KEY` / `NEBIUS_API_KEY` in
`AutoDesign/.env` flow to the signals automatically, no `export` needed.

`rank.py` and `batch.py` call `load_dotenv` explicitly at CLI startup. The Nemotron
client calls `ensure_loaded()` lazily, so the key is picked up even from entry points
that don't load .env themselves (e.g. `python -m pipeline.benchmark`).
"""

from __future__ import annotations

import os
from pathlib import Path

_DEFAULT_PATH = ".env"
_loaded = False


def load_dotenv(path: str | Path = _DEFAULT_PATH) -> None:
    """Read `path` (KEY=VALUE per line) into os.environ, not overwriting set keys."""
    p = Path(path)
    if not p.exists():
        return
    for line in p.read_text(encoding="utf-8").splitlines():
        s = line.strip()
        if not s or s.startswith("#") or "=" not in s:
            continue
        key, _, val = s.partition("=")
        key, val = key.strip(), val.strip().strip('"').strip("'")
        if key and key not in os.environ:
            os.environ[key] = val


def ensure_loaded(path: str | Path = _DEFAULT_PATH) -> None:
    """Load the default `.env` once per process. Idempotent and safe to over-call."""
    global _loaded
    if _loaded:
        return
    _loaded = True
    load_dotenv(path)
