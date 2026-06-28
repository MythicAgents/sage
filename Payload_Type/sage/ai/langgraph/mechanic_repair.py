"""Bounded repair helpers for unresolved payload mechanics.

The capability layer owns *what* operation is required. This module only helps
map one unresolved generic operation onto a live payload command surface. It
does not choose a different capability, alter verifier contracts, or execute
anything itself.
"""

from __future__ import annotations

import json
import re
from typing import Any


_INTERNAL_COMMANDS = {"wait_for_seconds"}


def compact_command_surface(commands: list[dict[str, Any]] | None) -> list[dict[str, Any]]:
    """Return a small, model-facing view of Mythic command schemas."""
    compact: list[dict[str, Any]] = []
    for command in list(commands or []):
        if not isinstance(command, dict):
            continue
        name = str(command.get("cmd") or "").strip()
        if not name:
            continue
        groups: dict[str, list[dict[str, Any]]] = {}
        for param in list(command.get("commandparameters") or []):
            if not isinstance(param, dict):
                continue
            group = str(param.get("parameter_group_name") or "Default")
            groups.setdefault(group, []).append({
                "name": str(param.get("name") or ""),
                "cli_name": str(param.get("cli_name") or ""),
                "type": str(param.get("type") or "String"),
                "required": bool(param.get("required")),
                "choices": list(param.get("choices") or [])[:12],
                "default_value": param.get("default_value"),
            })
        compact.append({
            "command": name,
            "description": str(command.get("description") or "")[:240],
            "groups": groups,
        })
    return sorted(compact, key=lambda item: item["command"].casefold())


def command_names(commands: list[dict[str, Any]] | None) -> set[str]:
    return {
        str(command.get("cmd") or "").strip().casefold()
        for command in list(commands or [])
        if isinstance(command, dict) and str(command.get("cmd") or "").strip()
    }


def canonical_command_name(commands: list[dict[str, Any]] | None, requested: Any) -> str:
    wanted = str(requested or "").strip().casefold()
    if not wanted:
        return ""
    for command in list(commands or []):
        if not isinstance(command, dict):
            continue
        name = str(command.get("cmd") or "").strip()
        if name.casefold() == wanted:
            return name
    return ""


def command_schema(commands: list[dict[str, Any]] | None, requested: Any) -> list[dict[str, Any]] | None:
    wanted = str(requested or "").strip().casefold()
    if not wanted:
        return None
    for command in list(commands or []):
        if not isinstance(command, dict):
            continue
        if str(command.get("cmd") or "").strip().casefold() != wanted:
            continue
        params = command.get("commandparameters")
        return list(params) if isinstance(params, list) else []
    return None


def is_internal_command(command: Any) -> bool:
    return str(command or "").strip().casefold() in _INTERNAL_COMMANDS


def build_request(
    *,
    payload_type: str,
    callback_id: str | int,
    command_obj: dict[str, Any],
    command_surface: list[dict[str, Any]] | None,
    reason: str,
) -> dict[str, Any]:
    """Build a redaction-ready request for one unresolved operation."""
    return {
        "kind": "payload_mechanic_repair",
        "payload_type": str(payload_type or ""),
        "callback_id": str(callback_id or ""),
        "reason": str(reason or ""),
        "operation": str(command_obj.get("operation") or ""),
        "capability": str(command_obj.get("capability") or ""),
        "purpose": str(command_obj.get("purpose") or ""),
        "expected_probe": str(command_obj.get("expected_probe") or ""),
        "contract": {
            "consumes": list(command_obj.get("consumes") or []),
            "produces": list(command_obj.get("produces") or []),
            "prerequisites": list(command_obj.get("prerequisites") or []),
            "deferred": bool(command_obj.get("deferred")),
        },
        "original": {
            "command": str(command_obj.get("command") or ""),
            "parameters": command_obj.get("parameters", ""),
        },
        "available_commands": compact_command_surface(command_surface),
        "constraints": [
            "Keep the same generic operation and capability; only replace the payload command mechanic.",
            "Return exactly one command and its parameters as JSON.",
            "Do not change consumes, produces, prerequisites, or verifier semantics.",
            "Prefer native commands or run over shell when either can satisfy the operation.",
            "Do not invent commands that are absent from available_commands.",
            "Do not add unrelated discovery, collection, exploitation, or proof steps.",
        ],
    }


def build_prompt(request: dict[str, Any]) -> str:
    """Return the bounded model prompt for one mechanic substitution."""
    payload = json.dumps(request, sort_keys=True, default=str)
    return (
        "You are resolving one payload command binding for an already-selected capability operation.\n"
        "The capability, operation, artifact contract, and verifier are fixed. Choose at most one substitute "
        "command from the live command surface that performs the same mechanic. Do not choose a new capability "
        "or add steps. Prefer lower-footprint native commands or run; use shell only when no lower-footprint "
        "command can satisfy the same mechanic.\n"
        "Return JSON only in this shape:\n"
        '{"command":"<live command name>","parameters":{...},"rationale":"<short reason>"}\n'
        "If no valid substitute exists, return:\n"
        '{"command":"","parameters":{},"rationale":"no valid substitute"}\n'
        f"REQUEST={payload}"
    )


def parse_candidate(value: Any) -> dict[str, Any] | None:
    """Parse one JSON repair candidate from a model response."""
    if isinstance(value, dict):
        command = str(value.get("command") or "").strip()
        parameters = value.get("parameters", {})
        if parameters is None:
            parameters = {}
        if isinstance(parameters, (dict, str)):
            return {
                "command": command,
                "parameters": parameters,
                "rationale": str(value.get("rationale") or "").strip()[:400],
            }
        return None
    text = _response_text(value).strip()
    if not text:
        return None
    candidates = [text]
    fenced = re.search(r"```(?:json)?\s*(\{.*?\})\s*```", text, flags=re.I | re.S)
    if fenced:
        candidates.insert(0, fenced.group(1))
    first = text.find("{")
    last = text.rfind("}")
    if first >= 0 and last > first:
        candidates.append(text[first : last + 1])
    for candidate in candidates:
        try:
            parsed = json.loads(candidate)
        except Exception:
            continue
        if not isinstance(parsed, dict):
            continue
        command = str(parsed.get("command") or "").strip()
        parameters = parsed.get("parameters", {})
        if parameters is None:
            parameters = {}
        if not isinstance(parameters, (dict, str)):
            continue
        return {
            "command": command,
            "parameters": parameters,
            "rationale": str(parsed.get("rationale") or "").strip()[:400],
        }
    return None


def _response_text(value: Any) -> str:
    content = getattr(value, "content", value)
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts: list[str] = []
        for item in content:
            if isinstance(item, dict):
                text = item.get("text")
                if isinstance(text, str):
                    parts.append(text)
            elif isinstance(item, str):
                parts.append(item)
        return "\n".join(parts)
    return str(content or "")
