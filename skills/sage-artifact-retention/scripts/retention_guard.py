#!/usr/bin/env python3
"""Warn when a Codex turn leaves high-value Sage artifacts only in /tmp."""

from __future__ import annotations

import json
from pathlib import Path
import re
import sys
from typing import Any

from artifact_retention import source_is_recorded


MAX_TRANSCRIPT_BYTES = 16 * 1024 * 1024
TMP_PATH = re.compile(r"/tmp/[A-Za-z0-9_./+@:=,-]+")
HIGH_VALUE_TERMS = (
    "acceptance",
    "audit",
    "canary",
    "contract",
    "decision",
    "evidence",
    "flight-recorder",
    "handoff",
    "manifest",
    "panel",
    "proof",
    "review",
    "result",
    "transcript",
    "transition",
    "validation",
)
SCRATCH_PREFIXES = (
    "/tmp/claude-",
    "/tmp/pip-",
    "/tmp/pytest-",
    "/tmp/sage-frozen-review-",
    "/tmp/sage-isc49-primary-bootstrap.",
    "/tmp/sage-isc49-review-lease-probe-",
    "/tmp/sage_arch_gate/",
    "/tmp/sage_payloads/",
    "/tmp/sessions/",
    "/tmp/tmp",
)


def _transcript_tail(path: Path) -> str:
    size = path.stat().st_size
    with path.open("rb") as handle:
        if size > MAX_TRANSCRIPT_BYTES:
            handle.seek(size - MAX_TRANSCRIPT_BYTES)
        return handle.read().decode("utf-8", errors="replace")


def referenced_tmp_paths(text: str) -> list[Path]:
    paths: dict[str, Path] = {}
    for match in TMP_PATH.finditer(text):
        raw = match.group(0).rstrip(".,;:)]}'\"")
        if not raw or any(raw.startswith(prefix) for prefix in SCRATCH_PREFIXES):
            continue
        candidate = Path(raw)
        if (
            raw.startswith("/tmp/sage_arch_review/")
            and not candidate.name.endswith(".closed.json")
        ):
            continue
        if not candidate.exists():
            continue
        lowered = raw.casefold()
        if not any(term in lowered for term in HIGH_VALUE_TERMS):
            continue
        paths[str(candidate.resolve())] = candidate.resolve()
    return sorted(paths.values(), key=str)


def unrecorded_paths(text: str) -> list[Path]:
    return [
        path
        for path in referenced_tmp_paths(text)
        if not source_is_recorded(path)
    ]


def run_hook(payload: dict[str, Any]) -> dict[str, Any]:
    transcript_value = payload.get("transcript_path")
    if not isinstance(transcript_value, str) or not transcript_value:
        return {}
    transcript = Path(transcript_value)
    if not transcript.is_file():
        return {}
    candidates = unrecorded_paths(_transcript_tail(transcript))
    if not candidates:
        return {}
    displayed = [str(path) for path in candidates[:8]]
    remainder = len(candidates) - len(displayed)
    lines = "\n".join(f"- {path}" for path in displayed)
    if remainder:
        lines += f"\n- … and {remainder} more"
    return {
        "continue": True,
        "systemMessage": (
            "Sage retention warning: high-value temporary artifacts remain "
            "unrecorded. Review them before completion; promote only durable "
            "decision/evidence material, never secrets or payloads:\n"
            f"{lines}"
        ),
        "suppressOutput": False,
    }


def main() -> int:
    try:
        payload = json.load(sys.stdin)
        output = run_hook(payload if isinstance(payload, dict) else {})
    except Exception as exc:
        output = {
            "continue": True,
            "systemMessage": f"Sage retention guard could not inspect this turn: {exc}",
            "suppressOutput": False,
        }
    if output:
        print(json.dumps(output))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
