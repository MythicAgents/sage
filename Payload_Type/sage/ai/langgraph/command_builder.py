import re
from dataclasses import dataclass
from enum import Enum
from typing import Any


@dataclass
class ResolveResult:
    ok: bool
    params: dict
    group: str | None
    repair: str | None
    notes: list[str]


class ResultClass(str, Enum):
    SUCCESS = "success"
    CONSTRUCTION = "construction"
    GENUINE = "genuine"
    TRANSIENT = "transient"


_CONSTRUCTION_SIGNATURES = (
    "don't match any parameter group",
    "invalid parameters",
    "unknown key",
    "valid parameters are group",
    "required parameter",
    "takes no command line arguments",
    "operating_system",
    "not registered",
    "ensure_tool_uploaded",
    "null uuid",
)

_GENUINE_SIGNATURES = (
    "access denied",
    "access is denied",
    "0x80070005",
    "status_access_denied",
    "system error 5",
    "object not found",
    "cannot find the file",
    "no such object",
    "0x20f7",
    "getncchanges",
    "constraint violation",
    "structurally impossible",
)

_TRANSIENT_SIGNATURES = (
    "failed to create task",
    "error issuing command",
    "timed out",
    "timeout waiting",
    "no output",
)

_GENERIC_ERROR_HINTS = (
    "error",
    "failed",
    "failure",
    "exception",
    "traceback",
)


def classify_result(command: str, output: str, exception: str | None = None) -> str:
    try:
        _text(command)
        pieces = [_text(output)]
        if exception is not None:
            pieces.append(_text(exception))
        low = "\n".join(pieces).lower()

        if any(signature in low for signature in _CONSTRUCTION_SIGNATURES):
            return ResultClass.CONSTRUCTION.value
        if any(signature in low for signature in _GENUINE_SIGNATURES):
            return ResultClass.GENUINE.value
        if any(signature in low for signature in _TRANSIENT_SIGNATURES):
            return ResultClass.TRANSIENT.value
        if exception is not None and _text(exception).strip():
            return ResultClass.TRANSIENT.value
        if any(signature in low for signature in _GENERIC_ERROR_HINTS):
            return ResultClass.TRANSIENT.value
        return ResultClass.SUCCESS.value
    except Exception:
        return ResultClass.TRANSIENT.value


def breaker_decision(result_class: str, attempts: int) -> str:
    try:
        # Accept either a raw class string ("genuine") or a ResultClass member; a
        # str-Enum's str() yields "ResultClass.GENUINE", so normalize via .value first.
        raw = result_class.value if isinstance(result_class, ResultClass) else result_class
        normalized = _text(raw).lower()
        attempt_count = int(attempts)
        if normalized == ResultClass.SUCCESS.value:
            return "reset"
        if normalized == ResultClass.GENUINE.value:
            return "stop"
        if normalized == ResultClass.CONSTRUCTION.value:
            return "retry" if attempt_count < 1 else "stop"
        if normalized == ResultClass.TRANSIENT.value:
            return "retry" if attempt_count < 2 else "stop"
        return "retry" if attempt_count < 2 else "stop"
    except Exception:
        return "retry"


# training-prior -> schema
_STATIC_ALIASES = {
    "assembly_name": "filename",
    "assembly_file": "file",
    "assembly_arguments": "arguments",
    "assemblyargs": "arguments",
    "commands": "arguments",
    "cmd": "arguments",
    "command": "arguments",
    "args": "arguments",
}


def resolve_params(command_parameters: list[dict], supplied: dict, *, command: str = "") -> ResolveResult:
    try:
        notes: list[str] = []
        schema = _clean_schema(command_parameters)
        supplied_items = _clean_supplied(supplied)
        if not schema:
            for key, _value in supplied_items:
                notes.append(f"dropped '{key}' (no schema parameter)")
            return ResolveResult(True, {}, None, None, notes)

        mapped, sources, alias_notes = _alias_supplied(schema, supplied_items, command)
        notes.extend(alias_notes)
        _reroute_registered_file_refs(schema, mapped, sources, notes)
        candidates = _candidate_groups(schema)
        if not candidates:
            for key, _value in supplied_items:
                notes.append(f"dropped '{key}' (no schema parameter)")
            return ResolveResult(True, {}, None, None, notes)

        group_name, group_params = _select_group(schema, candidates, mapped)
        if group_name is None and not group_params:
            for key, _value in supplied_items:
                notes.append(f"dropped '{key}' (no schema parameter)")
            return ResolveResult(True, {}, None, None, notes)

        if _all_candidates_have_no_required(schema, candidates) and not mapped:
            return ResolveResult(True, {}, group_name, None, notes)

        valid_cli = {param["cli_name"] for param in group_params}
        params, defaulted_cli_names = _build_params(group_params, mapped)
        for cli_name in defaulted_cli_names:
            notes.append(f"defaulted {cli_name}={params[cli_name]}")
        for source_key, cli_name in sources.items():
            if cli_name not in valid_cli:
                notes.append(f"dropped '{source_key}' (not in group '{_display_group(group_name)}')")

        params, invalid_choices = _normalize_and_validate_choices(group_params, params)
        missing = _missing_required(group_params, params)
        if missing or invalid_choices:
            repair = _repair_message(missing, invalid_choices, group_name, group_params)
            return ResolveResult(False, params, group_name, repair, notes)

        return ResolveResult(True, params, group_name, None, notes)
    except Exception as error:
        return ResolveResult(False, {}, None, f"parameter resolver failed: {error}", [])


def _reroute_registered_file_refs(
    schema: list[dict],
    mapped: dict[str, Any],
    sources: dict[str, str],
    notes: list[str],
) -> None:
    """Move a registered-file reference off a `File` upload param onto the ChooseOne registered-selector.

    Mythic file-exec commands (execute_assembly, inline_assembly, load-assembly, ...) have TWO parameter
    groups: a "Default"/registered group with a ChooseOne selector (cli `Assembly`/`assembly`/`filename`)
    for an ALREADY-REGISTERED file, and a "New File" group with a File-type param (cli `assembly_file`/
    `file`) for uploading a NEW file. Sage references already-registered tools, so a value the model placed
    on the File/upload param is a registered reference — it belongs on the ChooseOne selector. Leaving it on
    the File param selects the "New File"/upload group, which (with a registered UUID) crashes Merlin and
    misbehaves on Apollo. Reroute so the registered group is chosen; ChooseOne validation then either accepts
    a valid registered name or returns a repair hint listing the valid names.
    """
    try:
        file_params = [p for p in schema if p.get("type") == "File"]
        if not file_params:
            return
        selector = next(
            (
                p for p in schema
                if p.get("type") == "ChooseOne"
                and any(tok in (_lower(p["cli_name"]) + " " + _lower(p["name"])) for tok in ("file", "assembly"))
            ),
            None,
        )
        if not selector:
            return
        sel_cli = selector["cli_name"]
        for fp in file_params:
            fcli = fp["cli_name"]
            if fcli in mapped and sel_cli not in mapped:
                mapped[sel_cli] = mapped.pop(fcli)
                for key, cli in list(sources.items()):
                    if cli == fcli:
                        sources[key] = sel_cli
                notes.append(f"rerouted '{fcli}' (upload arg) -> '{sel_cli}' (registered selector)")
    except Exception:
        return


def _clean_schema(command_parameters: Any) -> list[dict]:
    cleaned: list[dict] = []
    if not isinstance(command_parameters, list):
        return cleaned
    for entry in command_parameters:
        try:
            if not isinstance(entry, dict):
                continue
            cli_name = _text(entry.get("cli_name") or entry.get("name"))
            if not cli_name:
                continue
            name = _text(entry.get("name") or cli_name)
            group = _text(entry.get("parameter_group_name") or "Default") or "Default"
            choices = entry.get("choices")
            if not isinstance(choices, list):
                choices = []
            cleaned.append(
                {
                    "cli_name": cli_name,
                    "name": name,
                    "parameter_group_name": group,
                    "type": _text(entry.get("type") or "String") or "String",
                    "choices": [_text(choice) for choice in choices],
                    "required": bool(entry.get("required")),
                    "default_value": entry.get("default_value"),
                }
            )
        except Exception:
            continue
    return cleaned


def _clean_supplied(supplied: Any) -> list[tuple[str, Any]]:
    if not isinstance(supplied, dict):
        return []
    items: list[tuple[str, Any]] = []
    for key, value in supplied.items():
        try:
            text_key = _text(key)
            if text_key:
                items.append((text_key, value))
        except Exception:
            continue
    return items


def _text(value: Any) -> str:
    if value is None:
        return ""
    return str(value)


def _alias_supplied(
    schema: list[dict],
    supplied_items: list[tuple[str, Any]],
    command: str,
) -> tuple[dict[str, Any], dict[str, str], list[str]]:
    exact_cli: dict[str, str] = {}
    exact_name: dict[str, str] = {}
    schema_aliases: dict[str, str] = {}
    cli_names = {_lower(param["cli_name"]): param["cli_name"] for param in schema}

    for param in schema:
        cli_name = param["cli_name"]
        cli_key = _lower(cli_name)
        name_key = _lower(param["name"])
        if cli_key and cli_key not in exact_cli:
            exact_cli[cli_key] = cli_name
        if name_key and name_key not in exact_name:
            exact_name[name_key] = cli_name

    for param in schema:
        for source in (param["cli_name"], param["name"]):
            for alias in _morphs(source, ""):
                if alias and alias not in schema_aliases:
                    schema_aliases[alias] = param["cli_name"]

    mapped: dict[str, Any] = {}
    sources: dict[str, str] = {}
    priorities: dict[str, int] = {}
    notes: list[str] = []
    for key, value in supplied_items:
        cli_name, priority = _resolve_supplied_key(key, command, exact_cli, exact_name, schema_aliases, cli_names)
        if not cli_name:
            sources[key] = ""
            continue
        if cli_name in mapped and priorities.get(cli_name, 0) > priority:
            notes.append(f"dropped '{key}' (duplicate for '{cli_name}')")
            continue
        if cli_name in mapped and priorities.get(cli_name, 0) <= priority:
            old_key = next((source for source, cli in sources.items() if cli == cli_name), "")
            if old_key:
                notes.append(f"dropped '{old_key}' (duplicate for '{cli_name}')")
        mapped[cli_name] = value
        sources[key] = cli_name
        priorities[cli_name] = priority
        if key != cli_name:
            notes.append(f"mapped '{key}' to '{cli_name}'")
    return mapped, sources, notes


def _resolve_supplied_key(
    key: str,
    command: str,
    exact_cli: dict[str, str],
    exact_name: dict[str, str],
    schema_aliases: dict[str, str],
    cli_names: dict[str, str],
) -> tuple[str | None, int]:
    lowered = _lower(key)
    if lowered in exact_cli:
        return exact_cli[lowered], 4
    if lowered in exact_name:
        return exact_name[lowered], 3
    for alias in _morphs(key, command):
        if alias in schema_aliases:
            return schema_aliases[alias], 2
    resolved = _static_target_to_cli(_STATIC_ALIASES.get(lowered), cli_names, exact_name)
    if resolved:
        return resolved, 1
    for alias in _morphs(key, command):
        resolved = _static_target_to_cli(_STATIC_ALIASES.get(alias), cli_names, exact_name)
        if resolved:
            return resolved, 1
    return None, 0


def _static_target_to_cli(
    static_target: str | None,
    cli_names: dict[str, str],
    exact_name: dict[str, str],
) -> str | None:
    # A static alias may point at a schema cli_name OR a param's `name`. e.g. merlin execute-assembly:
    # cli_name="assembly", name="filename" — so assembly_name -> filename must resolve via the name map,
    # not only cli_names (which only knows "assembly").
    if not static_target:
        return None
    target = _lower(static_target)
    if target in cli_names:
        return cli_names[target]
    if target in exact_name:
        return exact_name[target]
    return None


def _lower(value: str) -> str:
    return _text(value).strip().lower()


def _morphs(value: str, command: str) -> list[str]:
    forms: list[str] = []
    raw = _lower(value)
    command_raw = _lower(command)
    command_flat = command_raw.replace("_", "")

    def add(item: str) -> None:
        if item and item not in forms:
            forms.append(item)

    add(raw)
    if command_raw and raw.startswith(command_raw + "_"):
        add(raw[len(command_raw) + 1 :])
    flat = raw.replace("_", "")
    add(flat)
    if command_flat and flat.startswith(command_flat):
        add(flat[len(command_flat) :])

    for item in list(forms):
        if item.endswith("s") and len(item) > 1:
            add(item[:-1])
        else:
            add(item + "s")
    return forms


def _candidate_groups(schema: list[dict]) -> list[tuple[str | None, list[dict]]]:
    names: list[str] = []
    for param in schema:
        group = param["parameter_group_name"]
        if group not in names:
            names.append(group)
    if not names:
        return []
    if len(names) == 1:
        return [(names[0], list(schema))]

    candidates: list[tuple[str | None, list[dict]]] = []
    default_params = [param for param in schema if param["parameter_group_name"] == "Default"]
    optional_defaults = [param for param in default_params if not param["required"]]
    for name in names:
        if name == "Default":
            candidates.append((name, list(default_params)))
            continue
        group_params = [param for param in schema if param["parameter_group_name"] == name]
        group_params.extend(param for param in optional_defaults if param not in group_params)
        candidates.append((name, group_params))
    return candidates


def _select_group(
    schema: list[dict],
    candidates: list[tuple[str | None, list[dict]]],
    mapped: dict[str, Any],
) -> tuple[str | None, list[dict]]:
    scored: list[tuple[int, int, int, int, str | None, list[dict]]] = []
    for name, params in candidates:
        missing = [
            param["cli_name"]
            for param in params
            if param["required"] and param["cli_name"] not in mapped and not _has_default(param.get("default_value"))
        ]
        valid = {param["cli_name"] for param in params}
        covered = sum(1 for cli_name in mapped if cli_name in valid)
        defaulted = sum(
            1
            for param in params
            if not param["required"] and param["cli_name"] not in mapped and _has_default(param.get("default_value"))
        )
        hard_ok = 1 if not missing else 0
        scored.append((hard_ok, covered, -defaulted, -len(missing), name, params))

    viable = [score for score in scored if score[0] == 1]
    if viable:
        viable.sort(key=lambda score: (score[1], score[2], score[3], -_candidate_index(candidates, score[4])), reverse=True)
        return viable[0][4], viable[0][5]

    scored.sort(key=lambda score: (score[1], score[3], score[2], -_candidate_index(candidates, score[4])), reverse=True)
    return scored[0][4], scored[0][5]


def _candidate_index(candidates: list[tuple[str | None, list[dict]]], name: str | None) -> int:
    for index, (candidate_name, _params) in enumerate(candidates):
        if candidate_name == name:
            return index
    return 0


def _all_candidates_have_no_required(schema: list[dict], candidates: list[tuple[str | None, list[dict]]]) -> bool:
    if not schema:
        return True
    for _name, params in candidates:
        if any(param["required"] for param in params):
            return False
    return True


def _build_params(group_params: list[dict], mapped: dict[str, Any]) -> tuple[dict, list[str]]:
    params: dict[str, Any] = {}
    defaulted_cli_names: list[str] = []
    for param in group_params:
        cli_name = param["cli_name"]
        if cli_name in mapped:
            params[cli_name] = _normalize_value(mapped[cli_name])
        elif _has_default(param.get("default_value")):
            params[cli_name] = _normalize_value(param.get("default_value"))
            defaulted_cli_names.append(cli_name)
    return params, defaulted_cli_names


def _has_default(value: Any) -> bool:
    return _normalize_value(value) != ""


def _normalize_value(value: Any) -> Any:
    if value is None:
        return ""
    if value == {}:
        return ""
    if isinstance(value, str) and value.strip() in ("", "{}", '""', "''"):
        return ""
    return value


def _normalize_and_validate_choices(group_params: list[dict], params: dict) -> tuple[dict, list[tuple[str, Any, list[str]]]]:
    invalid: list[tuple[str, Any, list[str]]] = []
    param_by_cli = {param["cli_name"]: param for param in group_params}
    for cli_name, value in list(params.items()):
        param = param_by_cli.get(cli_name)
        if not param:
            continue
        choices = [choice for choice in param.get("choices", []) if choice != ""]
        if param.get("type") == "ChooseOne" and choices:
            matched = _match_choice(value, choices)
            if matched is None:
                invalid.append((cli_name, value, choices))
            else:
                params[cli_name] = matched
        elif cli_name == "operating_system" and choices:
            matched = _match_choice(value, choices)
            if matched is not None:
                params[cli_name] = matched
    return params, invalid


def _match_choice(value: Any, choices: list[str]) -> str | None:
    raw = _text(value)
    for choice in choices:
        if raw.casefold() == choice.casefold():
            return choice
    return None


def _missing_required(group_params: list[dict], params: dict) -> list[str]:
    missing: list[str] = []
    for param in group_params:
        if param["required"] and param["cli_name"] not in params:
            missing.append(param["cli_name"])
    return missing


def _repair_message(
    missing: list[str],
    invalid_choices: list[tuple[str, Any, list[str]]],
    group_name: str | None,
    group_params: list[dict],
) -> str:
    parts: list[str] = []
    group_label = _display_group(group_name)
    if missing:
        valid = ", ".join(param["cli_name"] for param in group_params)
        parts.append(f"missing required param(s) for group '{group_label}': {', '.join(missing)}; valid params: {valid}")
    for cli_name, value, choices in invalid_choices:
        options = ", ".join(choices)
        parts.append(f"invalid ChooseOne param '{cli_name}' for group '{group_label}': {_text(value)!r}; choices: {options}")
    return " | ".join(parts)


def _display_group(group_name: str | None) -> str:
    return group_name if group_name is not None else "implicit"
