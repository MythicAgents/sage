#!/usr/bin/env python3
"""Shared helpers for the Sage architecture governor."""

from __future__ import annotations

import fnmatch
import hashlib
import json
import os
from pathlib import Path
import re
import shlex
import subprocess
import time
from typing import Any, Iterable

HIGH_RISK_PATTERNS = (
    "Payload_Type/sage/prompts/**",
    "Payload_Type/sage/ai/langgraph/model.py",
    "Payload_Type/sage/ai/langgraph/mythic_tools.py",
    "Payload_Type/sage/ai/langgraph/objective_contract.py",
    "Payload_Type/sage/ai/langgraph/turn_authority.py",
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
    "Payload_Type/sage/sage_chat/**",
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

# Cross-skill references are architecture wiring, not benchmark strategy. Remove only these exact canonical
# package names before scanning added lines; all other GOAD identities and free-standing mentions remain gated.
GOAD_LITERAL_REFERENCE_EXEMPTIONS = (
    "sage-goad-reset",
)

TOKEN_VERSION = 1
TOKEN_DIR = Path(os.environ.get("SAGE_ARCH_GATE_DIR", "/tmp/sage_arch_gate"))
REVIEW_LEASE_VERSION = 1


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


def review_lease_path(root: str | Path) -> Path:
    lease_dir = Path(
        os.environ.get("SAGE_ARCH_REVIEW_DIR", "/tmp/sage_arch_review")
    )
    return lease_dir / f"{repo_hash(root)}.json"


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
    normalized = candidate.as_posix()
    while normalized.startswith("./"):
        normalized = normalized[2:]
    return normalized


def matches_any(path: str, patterns: Iterable[str]) -> bool:
    clean = path.replace("\\", "/")
    while clean.startswith("./"):
        clean = clean[2:]
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
    """Extract path operands that common shell commands can mutate."""

    text = str(command or "")
    if not text.strip():
        return []
    found = [
        match.group("target").strip("'\"")
        for match in re.finditer(
            r"(?<![<>])(?:\d*>>?|&>>?)\s*"
            r"(?P<target>(?:\"[^\"]*\"|'[^']*'|[^\s;&|]+))",
            text,
        )
        if match.group("target").strip("'\"")
    ]

    try:
        lexer = shlex.shlex(text, posix=True, punctuation_chars="|;&<>")
        lexer.whitespace_split = True
        lexer.commenters = ""
        tokens = list(lexer)
    except ValueError:
        return sorted(dict.fromkeys(found))

    separators = {"|", "||", "|&", ";", ";;", "&&", "&"}
    redirectors = {">", ">>", ">|", "&>", "&>>"}
    segments: list[list[str]] = []
    segment: list[str] = []
    for token in tokens:
        if token in separators:
            if segment:
                segments.append(segment)
                segment = []
            continue
        segment.append(token)
    if segment:
        segments.append(segment)

    for raw_segment in segments:
        argv: list[str] = []
        index = 0
        while index < len(raw_segment):
            token = raw_segment[index]
            if token in redirectors and index + 1 < len(raw_segment):
                found.append(raw_segment[index + 1])
                index += 2
                continue
            argv.append(token)
            index += 1
        found.extend(_writer_paths(argv))
    return sorted(dict.fromkeys(path for path in found if path))


def _path_operands(
    args: list[str], *, options_with_values: Iterable[str] = ()
) -> list[str]:
    """Return non-option operands while excluding values consumed by options."""

    takes_value = set(options_with_values)
    operands: list[str] = []
    literal = False
    index = 0
    while index < len(args):
        token = args[index]
        if not literal and token == "--":
            literal = True
        elif not literal and token in takes_value:
            index += 1
        elif not literal and token.startswith("-"):
            pass
        else:
            operands.append(token)
        index += 1
    return operands


def _writer_paths(argv: list[str]) -> list[str]:
    """Return the actual destination/removal operands for one shell command."""

    while argv and re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*=.*", argv[0]):
        argv = argv[1:]
    if not argv:
        return []
    command = Path(argv[0]).name
    args = argv[1:]

    if command == "tee":
        return _path_operands(args)
    if command == "truncate":
        return _path_operands(
            args,
            options_with_values=(
                "-o",
                "--io-blocks",
                "-r",
                "--reference",
                "-s",
                "--size",
            ),
        )
    if command == "dd":
        return [
            token.split("=", 1)[1]
            for token in args
            if token.startswith("of=") and token.split("=", 1)[1]
        ]
    if command == "rm":
        return _path_operands(args)
    if command == "touch":
        return _path_operands(
            args,
            options_with_values=(
                "-d",
                "--date",
                "-r",
                "--reference",
                "-t",
                "--time",
            ),
        )
    if command in {"cp", "mv"}:
        operands = _path_operands(
            args,
            options_with_values=(
                "-S",
                "--suffix",
                "-t",
                "--target-directory",
            ),
        )
        target = next(
            (
                args[index + 1]
                for index, token in enumerate(args[:-1])
                if token in {"-t", "--target-directory"}
            ),
            None,
        )
        target = next(
            (
                token.split("=", 1)[1]
                for token in args
                if token.startswith("--target-directory=")
            ),
            target,
        )
        if command == "mv":
            return operands + ([target] if target else [])
        if target:
            return [target]
        return operands[-1:] if len(operands) >= 2 else []
    if command == "git" and args:
        subcommand = args[0]
        subargs = args[1:]
        if subcommand in {"add", "rm"}:
            if any(
                token in {".", "-A", "--all"} or re.fullmatch(r"-[A-Za-z]*A[A-Za-z]*", token)
                for token in subargs
            ):
                return ["*"]
            return _path_operands(subargs)
        if subcommand == "reset":
            if any(
                token in {".", "--hard", "--merge", "--keep"}
                for token in subargs
            ):
                return ["*"]
            return _path_operands(subargs)
    return []


def load_token(root: str | Path) -> dict[str, Any] | None:
    path = token_path(root)
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return None
    return data if isinstance(data, dict) else None


def load_review_lease(root: str | Path) -> dict[str, Any] | None:
    path = review_lease_path(root)
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return None
    if not isinstance(data, dict):
        return None
    if data.get("version") != REVIEW_LEASE_VERSION:
        return None
    if data.get("repo_hash") != repo_hash(root):
        return None
    if data.get("status") != "active":
        return None
    return data


def frozen_path_conflicts(
    root: str | Path, paths: Iterable[str]
) -> tuple[list[str], dict[str, Any] | None]:
    lease = load_review_lease(root)
    if not lease:
        return [], None
    frozen = [
        normalize_repo_path(item, root)
        for key in ("candidate_paths", "protected_paths")
        for item in lease.get(key) or []
    ]
    conflicts: list[str] = []
    for raw_path in paths:
        clean = normalize_repo_path(raw_path, root)
        if not clean:
            continue
        if clean == "*":
            return sorted(dict.fromkeys(frozen)), lease
        for frozen_path in frozen:
            if (
                clean == frozen_path
                or clean.startswith(frozen_path.rstrip("/") + "/")
                or frozen_path.startswith(clean.rstrip("/") + "/")
            ):
                conflicts.append(clean)
                break
    return sorted(dict.fromkeys(conflicts)), lease


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
