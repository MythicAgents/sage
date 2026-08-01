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

Two files are read, `.env.local` before `.env`, and **load order alone is the precedence rule** —
because "already set wins", whatever `.env.local` sets first is what `.env` cannot overwrite. No
override flag, no merge logic. The split exists because the two files answer to different owners:
`.env` is tracked so a Mythic operator can edit it in the container from the web UI without a shell,
which means it must ship inert and free of credentials; `.env.local` is gitignored and belongs to
whoever is running Sage outside a container. Before the split, configuring local development meant
editing the tracked file, which put a real API key in a public repository's working tree and turned
three guard tests red.
"""

from __future__ import annotations

import os
from typing import Iterable

from dotenv import dotenv_values

__all__ = ["apply_dotenv", "dotenv_paths", "load_sage_dotenv"]

#: Filenames in precedence order, highest first. Order IS the precedence — see module docstring.
DOTENV_FILENAMES = (".env.local", ".env")


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


def dotenv_paths(directory: str | None = None) -> list[str]:
    """Existing dotenv files under ``directory``, highest precedence first.

    Shared with the relaunch helper's identity recorder so the search order has exactly one
    definition. When that recorder knew only about `.env`, it reported a locally-configured Sage as
    having no provider or model while the process was correctly configured — a gate blind to the
    thing it gates. A second copy of this list would reintroduce that by drift.
    """
    base = directory or os.path.dirname(os.path.abspath(__file__))
    return [
        path
        for path in (os.path.join(base, name) for name in DOTENV_FILENAMES)
        if os.path.isfile(path)
    ]


def load_sage_dotenv(directory: str | None = None) -> list[str]:
    """Load `<directory>/.env.local` then `<directory>/.env` into ``os.environ``.

    Both files are optional, so a deployment configured entirely through Mythic needs neither.
    Returns the variable names set, for logging — never the values.
    """
    applied: list[str] = []
    for path in dotenv_paths(directory):
        applied.extend(apply_dotenv(dotenv_values(path), os.environ))
    return applied
