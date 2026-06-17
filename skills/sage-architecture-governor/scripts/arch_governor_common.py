#!/usr/bin/env python3
"""Shared helpers for the Sage architecture governor."""

from __future__ import annotations

import fnmatch
import hashlib
import json
import os
from pathlib import Path
import re
import subprocess
import time
from typing import Any, Iterable

HIGH_RISK_PATTERNS = (
    "Payload_Type/sage/prompts/**",
    "Payload_Type/sage/ai/langgraph/model.py",
    "Payload_Type/sage/ai/langgraph/mythic_tools.py",
    "Payload_Type/sage/ai/langgraph/engagement_state.py",
    "Payload_Type/sage/ai/langgraph/graph_reconciler.py",
    "Payload_Type/sage/ai/langgraph/access_reconciler.py",
    "Payload_Type/sage/ai/langgraph/task_reconciler.py",
    "Payload_Type/sage/ai/langgraph/intent_classifier.py",
    "Payload_Type/sage/ai/langgraph/capabilities.py",
    "Payload_Type/sage/ai/langgraph/mythic_capability_adapter.py",
    "Payload_Type/sage/ai/langgraph/command_builder.py",
    "Payload_Type/sage/ai/trajectory/**",
    "Payload_Type/sage/evals/**",
    "Payload_Type/sage/container/agent_functions/chat.py",
    "Payload_Type/sage/container/agent_functions/query.py",
    "Payload_Type/sage/container/agent_functions/state.py",
    "skills/sage-live-runner/scripts/**",
)

GOAD_LITERALS = (
    "GOAD",
    "Trust Walker",
    "CASTELBLACK",
    "WINTERFELL",
    "BRAAVOS",
    "STARKWALLPAPER",
    "samwell.tarly",
    "cersei.lannister",
    "sevenkingdoms.local",
    "north.sevenkingdoms.local",
    "essos.local",
)

GOAD_ALLOWED_PATTERNS = (
    "Payload_Type/sage/evals/reference_solutions/**",
    "Payload_Type/sage/ttps/goadabuse-reference.md",
    "Payload_Type/sage/tests/**",
    "skills/sage-goad-reset/**",
    "skills/sage-live-runner/scripts/run_essos_da.py",
)

TOKEN_VERSION = 1
TOKEN_DIR = Path(os.environ.get("SAGE_ARCH_GATE_DIR", "/tmp/sage_arch_gate"))


def repo_root(start: str | Path | None = None) -> Path:
    """Return the git root for start or cwd, falling back to cwd."""

    cwd = Path(start or os.getcwd()).resolve()
    try:
        out = subprocess.check_output(
            ["git", "rev-parse", "--show-toplevel"],
            cwd=str(cwd),
            text=True,
            stderr=subprocess.DEVNULL,
        ).strip()
        if out:
            return Path(out).resolve()
    except Exception:
        return cwd
    return cwd


def repo_hash(root: str | Path) -> str:
    return hashlib.sha256(str(Path(root).resolve()).encode("utf-8")).hexdigest()[:16]


def token_path(root: str | Path) -> Path:
    return TOKEN_DIR / f"{repo_hash(root)}.json"


def normalize_repo_path(path: str | Path, root: str | Path) -> str:
    root_path = Path(root).resolve()
    path_text = str(path).strip()
    if not path_text:
        return ""
    candidate = Path(path_text)
    if candidate.is_absolute():
        try:
            return candidate.resolve().relative_to(root_path).as_posix()
        except Exception:
            return candidate.as_posix().lstrip("/")
    return candidate.as_posix().lstrip("./")


def matches_any(path: str, patterns: Iterable[str]) -> bool:
    clean = path.replace("\\", "/").lstrip("./")
    return any(fnmatch.fnmatch(clean, pattern) for pattern in patterns)


def high_risk_paths(paths: Iterable[str], root: str | Path) -> list[str]:
    out: list[str] = []
    for path in paths:
        clean = normalize_repo_path(path, root)
        if clean and matches_any(clean, HIGH_RISK_PATTERNS):
            out.append(clean)
    return sorted(dict.fromkeys(out))


def parse_apply_patch_paths(patch_text: str) -> list[str]:
    paths: list[str] = []
    for line in str(patch_text or "").splitlines():
        match = re.match(r"^\*\*\* (?:Add|Update|Delete) File: (.+)$", line)
        if match:
            paths.append(match.group(1).strip())
            continue
        move = re.match(r"^\*\*\* Move to: (.+)$", line)
        if move:
            paths.append(move.group(1).strip())
    return paths


def shell_write_paths(command: str) -> list[str]:
    """Best-effort path extraction for obvious shell writes.

    This intentionally catches common Codex write routes. It is not a shell parser
    and should be treated as a guardrail, not a full sandbox.
    """

    text = str(command or "")
    if not text.strip():
        return []
    write_markers = (
        "apply_patch",
        " tee ",
        " tee -",
        " >",
        ">>",
        "sed -i",
        "perl -pi",
        "python -c",
        "python3 -c",
        "rm ",
        "mv ",
        "cp ",
        "touch ",
    )
    if not any(marker in f" {text} " for marker in write_markers):
        return []
    found: list[str] = []
    for pattern in HIGH_RISK_PATTERNS:
        literal_prefix = pattern.split("*", 1)[0].rstrip("/")
        if literal_prefix and literal_prefix in text:
            found.append(literal_prefix)
    for match in re.finditer(r"(?P<path>Payload_Type/sage/[A-Za-z0-9_./-]+|skills/sage-live-runner/[A-Za-z0-9_./-]+)", text):
        found.append(match.group("path"))
    return found


def load_token(root: str | Path) -> dict[str, Any] | None:
    path = token_path(root)
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return None
    return data if isinstance(data, dict) else None


def approval_status(root: str | Path, paths: Iterable[str]) -> tuple[bool, str]:
    high = high_risk_paths(paths, root)
    if not high:
        return True, "no high-risk paths"
    token = load_token(root)
    if not token:
        return False, f"no architecture gate token for high-risk paths: {', '.join(high)}"
    if token.get("version") != TOKEN_VERSION:
        return False, "architecture gate token has unsupported version"
    if str(token.get("repo_hash") or "") != repo_hash(root):
        return False, "architecture gate token belongs to a different repo"
    try:
        expires_at = float(token.get("expires_at") or 0)
    except Exception:
        expires_at = 0
    if expires_at < time.time():
        return False, "architecture gate token expired"
    scope = [str(item) for item in token.get("scope") or [] if str(item).strip()]
    if not scope:
        return False, "architecture gate token has empty scope"
    missing = [path for path in high if not matches_any(path, scope)]
    if missing:
        return False, f"architecture gate token does not cover: {', '.join(missing)}"
    return True, f"architecture gate token valid for: {', '.join(high)}"


def write_json(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    tmp.replace(path)


def added_lines_from_diff(diff_text: str) -> list[tuple[str, str]]:
    """Return (path, added_line) pairs from unified git diff text."""

    current = ""
    out: list[tuple[str, str]] = []
    for line in str(diff_text or "").splitlines():
        if line.startswith("+++ b/"):
            current = line[len("+++ b/") :].strip()
            continue
        if line.startswith("+++ /dev/null"):
            current = ""
            continue
        if line.startswith("+") and not line.startswith("+++"):
            out.append((current, line[1:]))
    return out


def is_goad_allowed(path: str) -> bool:
    return matches_any(path, GOAD_ALLOWED_PATTERNS)
