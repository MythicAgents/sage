"""Load Sage's own `.env` into the process environment at startup.

Deliberately dependency-light: `main.py` calls this BEFORE importing `mythic_container`, which
builds its Dynaconf settings object at import time and would not see anything loaded afterwards.
Importing this module must therefore not drag in the rest of Sage.

Two rules, both load-bearing:

* **A variable already in the environment wins.** Mythic injects `RABBITMQ_HOST`,
  `MYTHIC_SERVER_HOST` and 19 others into the container. A stale file must never shadow them —
  that failure presents as a Mythic outage, not a configuration error, and would be miserable to
  diagnose. (python-dotenv calls this `override=False`; we assign explicitly so the empty rule
  below is ours rather than the library's.)

* **An empty value is skipped.** `KEY=` must set nothing. Downstream code reads configuration with
  `os.environ.get(KEY)` and treats presence as "configured", so an empty string would silently
  disable a fallback rather than leave it alone — the operator uncomments a line, leaves it blank,
  and the setting they were trying to enable stops resolving from anywhere else.
"""

from __future__ import annotations

import os
from typing import Iterable

from dotenv import dotenv_values

__all__ = ["apply_dotenv", "load_sage_dotenv"]


def apply_dotenv(values: dict[str, str | None], environ: dict[str, str]) -> list[str]:
    """Apply parsed `.env` values to ``environ``. Returns the names actually set.

    Split from file reading so the precedence and empty-value rules are testable without a
    filesystem, and so callers can log what was applied without logging any value.
    """
    applied: list[str] = []
    for key, value in values.items():
        if not value:
            continue
        if key in environ:
            continue
        environ[key] = value
        applied.append(key)
    return applied


def load_sage_dotenv(directory: str | None = None) -> list[str]:
    """Load `<directory>/.env` (default: this file's directory) into ``os.environ``.

    A missing file is a no-op, so a deployment that configures everything through Mythic never
    needs one. Returns the variable names set, for logging — never the values.
    """
    base = directory or os.path.dirname(os.path.abspath(__file__))
    path = os.path.join(base, ".env")
    if not os.path.isfile(path):
        return []
    return apply_dotenv(dotenv_values(path), os.environ)
