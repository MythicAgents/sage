import re
from typing import Any


AXES: list[str] = [
    "disk_artifact",
    "new_beacon",
    "new_process",
    "flagged_tool",
    "lateral_hop",
    "network_signature",
    "reversibility",
]

KNOWN_AGENT_TYPES: set[str] = {
    "apollo",
    "merlin",
    "poseidon",
    "athena",
    "apfell",
    "medusa",
    "thanatos",
    "sage",
    "atlas",
    "freyja",
    "havoc",
    "hermes",
    "jxa",
    "leviathan",
    "mythic",
    "nimplant",
    "typhon",
}

KNOWN_TOOL_SIGNATURES: set[str] = {
    "adfind",
    "bloodhound",
    "certify",
    "ghostpack",
    "invoke-kerberoast",
    "kerberoast",
    "lapstoolkit",
    "mimikatz",
    "powerview",
    "powerup",
    "rubeus",
    "safetykatz",
    "seatbelt",
    "sharpdpapi",
    "sharpgpoabuse",
    "sharphound",
    "sharpview",
    "watson",
    "winpeas",
}

_MITRE_RE = re.compile(r"^T\d{4}(\.\d{3})?$")
_TOOL_SLUG_RE = re.compile(r"^[a-z0-9][a-z0-9_-]*$")
_DOMAIN_RE = re.compile(
    r"\b[a-z0-9-]+\.(local|com|net|org|io|corp|lab|internal)\b", re.IGNORECASE
)
_AGENT_VALUE_RE = re.compile(
    r"\b(" + "|".join(re.escape(agent) for agent in sorted(KNOWN_AGENT_TYPES)) + r")\b",
    re.IGNORECASE,
)
_PATH_PARAM_RE = re.compile(
    r"(file|path|output|outputdirectory|zipfilename|remote_path|destination)",
    re.IGNORECASE,
)
_PATH_VALUE_RE = re.compile(
    r"(^[a-z]:[\\/]|^[/\\~]|[/\\]|"
    r"\.(exe|dll|ps1|bat|cmd|zip|txt|json|csv|bin|dat|log|config)$)",
    re.IGNORECASE,
)
_NEW_BEACON_COMMAND_RE = re.compile(r"^(jump_|link|execute_pe$|psexec|wmi.*exec)", re.IGNORECASE)
_NEW_PROCESS_COMMAND_RE = re.compile(
    r"(run|execute|exec|shell|spawn|inject|shinject|powershell|inline_assembly|"
    r"execute_assembly|wmiexecute|psexec)",
    re.IGNORECASE,
)
_LATERAL_COMMAND_RE = re.compile(r"(jump_|psexec|wmi.*exec|ssh)", re.IGNORECASE)
_NETWORK_COMMAND_RE = re.compile(r"(download|upload|portscan|socks|rpfwd)", re.IGNORECASE)
_PERSISTENCE_RE = re.compile(
    r"(registry|service|run_key|persist|schtask|autorun)", re.IGNORECASE
)


def load_technique_map(raw: dict) -> dict:
    """Validate and return a technique residual table."""
    if not isinstance(raw, dict):
        raise ValueError("technique map must be a dict")

    for key, entry in raw.items():
        if not isinstance(key, str):
            raise ValueError("technique map keys must be strings")
        if not (_MITRE_RE.match(key) or _TOOL_SLUG_RE.match(key)):
            raise ValueError(f"invalid technique/tool key: {key!r}")
        if key.lower() in KNOWN_AGENT_TYPES:
            raise ValueError(f"technique map key must not be an agent type: {key!r}")

        _reject_guardrail_strings(entry)

        if not isinstance(entry, dict):
            raise ValueError(f"technique map entry for {key!r} must be a dict")
        if set(entry) != {"axes", "note"}:
            raise ValueError(f"technique map entry for {key!r} must contain axes and note")
        if not isinstance(entry["note"], str):
            raise ValueError(f"technique map note for {key!r} must be a string")
        if not isinstance(entry["axes"], dict):
            raise ValueError(f"technique map axes for {key!r} must be a dict")

        for axis, value in entry["axes"].items():
            if axis not in AXES:
                raise ValueError(f"unknown axis for {key!r}: {axis!r}")
            if isinstance(value, bool) or not isinstance(value, int) or value < 0 or value > 3:
                raise ValueError(f"axis score for {key!r}/{axis!r} must be 0..3")

    return raw


def footprint(
    command: str, params: dict | str, schema: list[dict], technique_map: dict | None = None
) -> dict:
    """Return footprint axes, total, and rationale for a pending command."""
    try:
        schema_items = _validate_schema(schema)
        command_text = command if isinstance(command, str) else str(command)
        param_pairs, string_values = _extract_params(params)
        axes = _zero_axes()
        rationale: list[str] = []

        if _schema_has_file_param(schema_items):
            _add(axes, "disk_artifact", 2)
            rationale.append("schema accepts a file-like artifact")
        if _params_include_path_artifact(param_pairs):
            _add(axes, "disk_artifact", 2)
            rationale.append("parameters include a path-like artifact")

        if _schema_has_payload_selector(schema_items) or _NEW_BEACON_COMMAND_RE.search(command_text):
            axes["new_beacon"] = 3
            rationale.append("payload selector or beacon-spawning command shape")

        if _NEW_PROCESS_COMMAND_RE.search(command_text):
            _add(axes, "new_process", 2)
            rationale.append("command name indicates process execution")

        if _values_contain_known_tool(string_values):
            _add(axes, "flagged_tool", 1)
            rationale.append("parameters reference a known offensive tool signature")

        if _schema_has_lateral_param(schema_items) or _LATERAL_COMMAND_RE.search(command_text):
            axes["lateral_hop"] = 3
            rationale.append("schema or command shape targets a remote host")

        network_score = max(
            2 if axes["lateral_hop"] >= 1 else 0,
            1 if _NETWORK_COMMAND_RE.search(command_text) else 0,
        )
        if network_score:
            axes["network_signature"] = network_score
            rationale.append("command likely creates a network-visible action")

        reversibility = _structural_reversibility(command_text, string_values, axes)
        if reversibility:
            axes["reversibility"] = reversibility
            rationale.append("action leaves state that is harder to clean up")

        needs_admin, has_opsec = _extract_command_metadata(schema_items)
        if needs_admin:
            _add(axes, "new_process", 1)
            _add(axes, "reversibility", 1)
            rationale.append("Mythic metadata marks the command as requiring admin")
        if has_opsec:
            highest_axis = max(AXES, key=lambda axis: axes[axis])
            _add(axes, highest_axis, 1)
            rationale.append("Mythic metadata declares OPSEC pre/post checks")

        if technique_map:
            loaded_map = load_technique_map(technique_map)
            for key in _matching_technique_keys(params, string_values, loaded_map):
                entry = loaded_map[key]
                for axis, value in entry["axes"].items():
                    axes[axis] = max(axes[axis], value)
                rationale.append(entry["note"])

        return {"axes": axes, "total": sum(axes.values()), "rationale": rationale}
    except Exception as exc:
        return {
            "axes": _zero_axes(),
            "total": 0,
            "rationale": [f"could not score footprint: {exc}"],
        }


def _zero_axes() -> dict[str, int]:
    return {axis: 0 for axis in AXES}


def _add(axes: dict[str, int], axis: str, value: int) -> None:
    axes[axis] = min(3, axes[axis] + value)


def _validate_schema(schema: list[dict]) -> list[dict]:
    if not isinstance(schema, list):
        raise TypeError("schema must be a list")
    for item in schema:
        if not isinstance(item, dict):
            raise TypeError("schema entries must be dicts")
    return schema


def _reject_guardrail_strings(value: Any) -> None:
    if isinstance(value, str):
        if _DOMAIN_RE.search(value):
            raise ValueError(f"technique map value contains a domain pattern: {value!r}")
        if _AGENT_VALUE_RE.search(value):
            raise ValueError(f"technique map value contains an agent type: {value!r}")
        return
    if isinstance(value, dict):
        for key, child in value.items():
            _reject_guardrail_strings(key)
            _reject_guardrail_strings(child)
        return
    if isinstance(value, (list, tuple, set)):
        for child in value:
            _reject_guardrail_strings(child)


def _extract_params(params: dict | str) -> tuple[list[tuple[str, Any]], list[str]]:
    if isinstance(params, str):
        return [], [params]
    if not isinstance(params, dict):
        raise TypeError("params must be a dict or string")

    pairs: list[tuple[str, Any]] = []
    strings: list[str] = []

    def visit(value: Any, name: str = "") -> None:
        if isinstance(value, dict):
            for child_name, child_value in value.items():
                child_name_text = str(child_name)
                pairs.append((child_name_text, child_value))
                visit(child_value, child_name_text)
        elif isinstance(value, (list, tuple, set)):
            for child in value:
                visit(child, name)
        elif isinstance(value, str):
            strings.append(value)

    visit(params)
    return pairs, strings


def _schema_names(item: dict) -> list[str]:
    return [str(item.get(key) or "") for key in ("name", "cli_name")]


def _schema_has_file_param(schema: list[dict]) -> bool:
    file_names = {"file", "assembly_file", "shellcode", "upload"}
    for item in schema:
        names = {name.lower() for name in _schema_names(item)}
        if item.get("type") == "File" or names.intersection(file_names):
            return True
    return False


def _params_include_path_artifact(param_pairs: list[tuple[str, Any]]) -> bool:
    for name, value in param_pairs:
        if not _PATH_PARAM_RE.search(name):
            continue
        for string_value in _strings_from_value(value):
            if _PATH_VALUE_RE.search(string_value):
                return True
    return False


def _schema_has_payload_selector(schema: list[dict]) -> bool:
    for item in schema:
        names = {name.lower() for name in _schema_names(item)}
        if item.get("type") == "ChooseOne" and "payload" in names:
            return True
    return False


def _schema_has_lateral_param(schema: list[dict]) -> bool:
    lateral_names = {"host", "computer", "remote_host"}
    return any(
        name.lower() in lateral_names for item in schema for name in _schema_names(item)
    )


def _extract_command_metadata(schema: list[dict]) -> tuple[bool, bool]:
    needs_admin = any(bool(item.get("needs_admin")) for item in schema)
    has_opsec = any(bool(item.get("has_opsec_pre") or item.get("has_opsec_post")) for item in schema)
    return needs_admin, has_opsec


def _values_contain_known_tool(string_values: list[str]) -> bool:
    for value in string_values:
        lowered = value.lower()
        if any(signature in lowered for signature in KNOWN_TOOL_SIGNATURES):
            return True
    return False


def _structural_reversibility(
    command: str, string_values: list[str], axes: dict[str, int]
) -> int:
    combined = " ".join([command] + string_values)
    if _PERSISTENCE_RE.search(combined):
        return 3
    if axes["new_beacon"] >= 1:
        return 2
    if axes["disk_artifact"] >= 1:
        return 1
    return 0


def _strings_from_value(value: Any) -> list[str]:
    strings: list[str] = []

    def visit(child: Any) -> None:
        if isinstance(child, str):
            strings.append(child)
        elif isinstance(child, dict):
            for nested in child.values():
                visit(nested)
        elif isinstance(child, (list, tuple, set)):
            for nested in child:
                visit(nested)

    visit(value)
    return strings


def _matching_technique_keys(
    params: dict | str, string_values: list[str], technique_map: dict
) -> list[str]:
    matches: list[str] = []
    seen: set[str] = set()

    if isinstance(params, dict):
        technique = params.get("_technique")
        if isinstance(technique, str):
            technique_key = technique.upper()
            if technique_key in technique_map:
                matches.append(technique_key)
                seen.add(technique_key)

    lowered_values = [value.lower() for value in string_values]
    for key in technique_map:
        if key in seen or not _TOOL_SLUG_RE.match(key):
            continue
        if any(key in value for value in lowered_values):
            matches.append(key)
            seen.add(key)

    return matches
