#!/usr/bin/env python3
"""Shared, side-effect-free run-as credential resolution for Sage operator tools."""
from __future__ import annotations

import os
from collections.abc import Mapping
from pathlib import Path


REPO = Path(__file__).resolve().parents[3]
SKILL_ENV = Path(__file__).resolve().parents[1] / ".env"
USER_RUN_AS_ENV = Path.home() / ".config" / "sage" / "runas.env"
RUNTIME_ENV = REPO / "Payload_Type" / "sage" / ".env"
CANONICAL_SKILL_ENV_DISPLAY = "skills/sage-foothold-launch/.env"
DEFAULT_PASSWORD_ENV = "SAGE_RUN_AS_PASSWORD"


def _read_nonempty_env_value(path: Path, key: str) -> str | None:
    if not path.is_file():
        return None
    prefix = f"{key}="
    for line in path.read_text(encoding="utf-8", errors="replace").splitlines():
        candidate = line.strip()
        if not candidate.startswith(prefix):
            continue
        value = candidate.split("=", 1)[1].strip().strip("'\"")
        return value or None
    return None


def resolve_run_as_password(
    password_env: str = DEFAULT_PASSWORD_ENV,
    *,
    environ: Mapping[str, str] | None = None,
    include_interactive_compatibility: bool = False,
) -> str | None:
    """Resolve one run-as password without printing, copying, or mutating its source.

    Process environment and the canonical skill-local file always have first and second
    precedence. The tracked runtime environment is the headless launcher's compatibility
    fallback. The eval orchestrator may retain the interactive launcher's older explicit
    file sources around that fallback without maintaining another parser or source list.
    """
    source_env = os.environ if environ is None else environ
    if source_env.get(password_env):
        return source_env[password_env]

    candidates: list[Path | str | None] = [SKILL_ENV]
    if include_interactive_compatibility:
        candidates.extend((source_env.get("SAGE_RUNAS_FILE"), USER_RUN_AS_ENV))
    candidates.append(RUNTIME_ENV)
    if include_interactive_compatibility:
        candidates.append(source_env.get("MYTHIC_ENV_PATH"))

    for candidate in candidates:
        if not candidate:
            continue
        value = _read_nonempty_env_value(Path(candidate).expanduser(), password_env)
        if value:
            return value
    return None
