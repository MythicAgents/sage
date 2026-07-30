"""Immutable, data-only execution scope compiled from one operator objective.

The contract sits below planning.  It describes the required result, selects the existing
execution engine, and narrows guarded Mythic actions to a resolved live callback.  It does not
choose an attack path or add prompt policy.
"""

from __future__ import annotations

from dataclasses import dataclass, replace
import hashlib
import json
import re
import shlex
from typing import Any, Literal


ObjectiveEngine = Literal["supervisor_graph", "controller"]
ObjectiveApprovalPolicy = Literal["turn_hitl", "controller_hitl"]
ObjectiveScopeKind = Literal["bounded_report", "open_ended", "unclassified"]
ObjectiveTaskScope = Literal["", "sharphound_collection"]
ObjectiveScopeResolution = Literal["not_required", "unresolved", "resolved"]

_PUBLIC_OUTPUT_DIRECTORY = r"C:\Users\Public"
_SAFE_TOKEN_RE = re.compile(r"^[A-Za-z0-9_.:-]+$")
_SAFE_DOMAIN_RE = re.compile(r"^[A-Za-z0-9_.-]+$")
_RUNNER_SELECTOR_KEYS = frozenset({"assembly", "assembly_name", "filename"})
_RUNNER_ARGUMENT_KEYS = frozenset({
    "argument",
    "arguments",
    "args",
    "assembly_arguments",
    "commandline",
})
_SHARPHOUND_VALUE_FLAGS = {
    "-c": "collection_methods",
    "--collectionmethods": "collection_methods",
    "-d": "domain",
    "--domain": "domain",
    "--domaincontroller": "domain_controller",
    "-o": "output_directory",
    "--outputdirectory": "output_directory",
    "--zipfilename": "zip_filename",
}
_SHARPHOUND_BOOLEAN_FLAGS = {
    "--collectallproperties": "collect_all_properties",
    "--searchforest": "search_forest",
}
_UNKNOWN_FOREST_VALUES = frozenset({
    "unknown",
    "(unknown)",
    "unknown forest",
    "(unknown forest)",
    "n/a",
    "none",
    "null",
})
_INFORMATIONAL_OBJECTIVE_RE = re.compile(
    r"^(?:(?:(?:can|could|would|will)\s+you(?:\s+please)?|please)\s+)?"
    r"(?:explain|describe|summarize|discuss|compare|tell\s+me|show\s+me|"
    r"why|what|when|where|who|which|how)\b",
    re.IGNORECASE,
)
_NEGATION_PREFIX_RE = re.compile(
    r"(?:\bdo\s+not\b|\bdon't\b|\bdont\b|\bshould\s+not\b|\bmust\s+not\b|"
    r"\bmay\s+not\b|\bnever\b|\bwithout\b|\bexcept\b|\bavoid(?:ing)?\b|"
    r"\brefrain\s+from\b)[^,;.!?]{0,80}$",
    re.IGNORECASE,
)
_NEGATION_SUFFIX_RE = re.compile(
    r"\b(?:not|never|except|should\s+not|must\s+not|may\s+not|no)\b",
    re.IGNORECASE,
)
_CALLBACK_RE = re.compile(r"\bcallback(?:_display_id)?\s*(?:=|:)?\s*(\d+)\b", re.IGNORECASE)


def _normalize_text(value: Any) -> str:
    return re.sub(r"\s+", " ", str(value or "").strip())


def _contains_any(text: str, terms: tuple[str, ...]) -> bool:
    return any(re.search(rf"\b{re.escape(term)}\b", text) for term in terms)


def _action_polarity(text: str, actions: tuple[str, ...]) -> tuple[bool, bool]:
    """Return positive/negated action mentions without treating a negated clause as authority."""
    positive = False
    negated = False
    for match in re.finditer(
        rf"\b(?:{'|'.join(re.escape(action) for action in actions)})\b",
        text,
        re.IGNORECASE,
    ):
        clause_prefix = text[:match.start()]
        clause_prefix = re.split(r"[,;.!?]|\bbut\b|\bhowever\b|\bwhile\b", clause_prefix)[-1]
        clause_suffix = re.split(
            r"[,;.!?]|\bbut\b|\bhowever\b|\bwhile\b|\bthen\b|\band\b",
            text[match.end():],
            maxsplit=1,
        )[0]
        if (
            _NEGATION_PREFIX_RE.search(clause_prefix)
            or _NEGATION_SUFFIX_RE.search(clause_suffix)
        ):
            negated = True
        else:
            positive = True
    return positive, negated


def _objective_is_informational(text: str) -> bool:
    return bool(_INFORMATIONAL_OBJECTIVE_RE.match(_normalize_text(text)))


def _decode_mapping(value: Any) -> dict[str, Any] | None:
    if isinstance(value, dict):
        return dict(value)
    if isinstance(value, str):
        stripped = value.strip()
        if not stripped:
            return {}
        try:
            decoded = json.loads(stripped)
        except Exception:
            return None
        return dict(decoded) if isinstance(decoded, dict) else None
    return None


def _decode_json_value(value: str) -> Any:
    try:
        return json.loads(value)
    except Exception:
        return None


def _canonical_json(value: Any) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), default=str)


def _normalized_command(value: Any) -> str:
    return _normalize_text(value).casefold().replace("-", "_")


def _normalized_callback_id(value: Any) -> str:
    raw = _normalize_text(value).casefold().lstrip("#").removeprefix("cb")
    try:
        parsed = int(raw)
    except (TypeError, ValueError):
        return ""
    return str(parsed) if parsed > 0 else ""


def _normalized_forest(value: Any) -> str:
    forest = _normalize_text(value).casefold()
    return "" if forest in _UNKNOWN_FOREST_VALUES else forest


def _canonical_windows_path(value: Any) -> str:
    text = _normalize_text(value).strip("\"'").replace("/", "\\")
    if not text or ".." in text.split("\\"):
        return ""
    return text.casefold()


def _collection_filename_matches(value: Any, token: str) -> bool:
    raw = _normalize_text(value)
    if not raw or not token or "/" in raw or "\\" in raw or raw in {".", ".."}:
        return False
    return bool(re.fullmatch(
        rf"(?:\d{{8,14}}_)?bloodhound_{re.escape(token)}\.zip",
        raw,
        re.IGNORECASE,
    ))


def _safe_collection_zip_path(value: Any, token: str) -> bool:
    path = _canonical_windows_path(value)
    prefix = _PUBLIC_OUTPUT_DIRECTORY.casefold() + "\\"
    if not path.startswith(prefix):
        return False
    filename = path[len(prefix):]
    return bool(filename and "\\" not in filename and _collection_filename_matches(filename, token))


def _strip_cli_quotes(value: Any) -> str:
    text = str(value or "").strip()
    if len(text) >= 2 and text[0] == text[-1] and text[0] in {"'", '"'}:
        return text[1:-1]
    return text


def _parse_sharphound_arguments(value: Any) -> dict[str, Any] | None:
    """Parse the admitted SharpHound flags semantically, independent of order/casing/quotes."""

    raw = str(value or "").strip()
    if not raw or any(marker in raw for marker in ("\r", "\n", "&&", "||", ";", "|", ">", "<")):
        return None
    try:
        tokens = [_strip_cli_quotes(token) for token in shlex.split(raw, posix=False)]
    except ValueError:
        return None
    parsed: dict[str, Any] = {}
    index = 0
    while index < len(tokens):
        token = tokens[index]
        inline_value: str | None = None
        flag = token
        if "=" in token:
            flag, inline_value = token.split("=", 1)
            inline_value = _strip_cli_quotes(inline_value)
        normalized_flag = flag.casefold()
        if normalized_flag in _SHARPHOUND_BOOLEAN_FLAGS:
            if inline_value not in (None, ""):
                return None
            key = _SHARPHOUND_BOOLEAN_FLAGS[normalized_flag]
            if key in parsed:
                return None
            parsed[key] = True
            index += 1
            continue
        key = _SHARPHOUND_VALUE_FLAGS.get(normalized_flag)
        if key is None or key in parsed:
            return None
        if inline_value is None:
            index += 1
            if index >= len(tokens):
                return None
            inline_value = _strip_cli_quotes(tokens[index])
        if not inline_value or inline_value.startswith("-"):
            return None
        parsed[key] = inline_value
        index += 1

    required = {
        "collection_methods",
        "collect_all_properties",
        "output_directory",
        "zip_filename",
    }
    if not required.issubset(parsed):
        return None
    if str(parsed["collection_methods"]).casefold() != "all":
        return None
    if _canonical_windows_path(parsed["output_directory"]) != _PUBLIC_OUTPUT_DIRECTORY.casefold():
        return None
    if bool(parsed.get("search_forest")) == bool(parsed.get("domain")):
        return None
    if parsed.get("domain_controller"):
        return None
    for key in ("domain", "domain_controller"):
        candidate = str(parsed.get(key) or "")
        if candidate and not _SAFE_DOMAIN_RE.fullmatch(candidate):
            return None
    return parsed


def _parameters_are_empty(value: Any) -> bool:
    if value in (None, ""):
        return True
    if isinstance(value, (dict, list, tuple)):
        return not value
    if isinstance(value, str):
        stripped = value.strip()
        if not stripped:
            return True
        try:
            return not json.loads(stripped)
        except Exception:
            return False
    return False


def _parameters_match(value: Any, expected_json: str) -> bool:
    expected = _decode_json_value(expected_json)
    if expected in (None, "", {}, []):
        return _parameters_are_empty(value)
    actual = value
    if isinstance(actual, str):
        stripped = actual.strip()
        if stripped.startswith("{") or stripped.startswith("[") or stripped.startswith('"'):
            try:
                actual = json.loads(stripped)
            except Exception:
                return False
    return _canonical_json(actual) == _canonical_json(expected)


@dataclass(frozen=True)
class CollectionTaskProfile:
    """Resolved command schema for the payload on the bound callback."""

    payload_type: str
    runner_command: str
    runner_tool_param: str
    runner_args_param: str
    ls_command: str
    ls_path_param: str
    download_command: str
    download_path_param: str
    identity_command: str
    identity_parameters_json: str
    ticket_command: str
    ticket_parameters_json: str
    revert_command: str
    search_forest_supported: bool

    @classmethod
    def from_adapter(cls, payload_type: Any, adapter: dict[str, Any] | None) -> "CollectionTaskProfile":
        config = adapter if isinstance(adapter, dict) else {}

        def text(key: str, default: str) -> str:
            return str(config.get(key, default) or "").strip()

        return cls(
            payload_type=_normalize_text(payload_type).casefold(),
            runner_command=text("dotnet_runner_command", "execute_assembly"),
            runner_tool_param=text("dotnet_tool_param", "assembly_name"),
            runner_args_param=text("dotnet_args_param", "assembly_arguments"),
            ls_command=text("collection_ls_command", "ls"),
            ls_path_param=text("collection_ls_path_param", "path"),
            download_command=text("collection_download_command", "download"),
            download_path_param=text("collection_download_path_param", "path"),
            identity_command=text("collection_identity_command", "whoami"),
            identity_parameters_json=_canonical_json(config.get("collection_identity_parameters", "")),
            ticket_command=text("collection_ticket_command", "ticket_cache_list"),
            ticket_parameters_json=_canonical_json(config.get(
                "collection_ticket_parameters",
                {"luid": "", "getSystemTickets": False},
            )),
            revert_command=text("collection_revert_command", "rev2self"),
            search_forest_supported=config.get("collection_search_forest_supported", True) is not False,
        )

    def to_payload(self, *, callback_id: str, token: str, forest: str = "") -> dict[str, Any]:
        zip_name = f"bloodhound_{token}.zip"
        scope_arguments = (
            "--SearchForest"
            if self.search_forest_supported
            else f"--Domain {forest}"
        )
        arguments = (
            f"-c All --CollectAllProperties {scope_arguments} "
            f"--OutputDirectory {_PUBLIC_OUTPUT_DIRECTORY} --ZipFilename {zip_name}"
        )
        preflight = [
            {
                "purpose": "authentication_identity_read",
                "command": self.identity_command,
                "parameters": _decode_json_value(self.identity_parameters_json),
            },
        ]
        if self.ticket_command:
            preflight.append({
                "purpose": "authentication_ticket_read",
                "command": self.ticket_command,
                "parameters": _decode_json_value(self.ticket_parameters_json),
            })
        if self.revert_command:
            preflight.append({
                "purpose": "conditional_process_context_restore",
                "command": self.revert_command,
                "parameters": "",
            })
        return {
            "payload_type": self.payload_type,
            "callback_display_id": int(callback_id),
            "collection_token": token,
            "zip_filename": zip_name,
            "output_directory": _PUBLIC_OUTPUT_DIRECTORY,
            "preflight_tasks": preflight,
            "preferred_collection_task": {
                "command": self.runner_command,
                "parameters": {
                    self.runner_tool_param: "SharpHound.exe",
                    self.runner_args_param: arguments,
                },
            },
            "artifact_discovery_task": {
                "command": self.ls_command,
                "parameters": {self.ls_path_param: _PUBLIC_OUTPUT_DIRECTORY},
            },
            "artifact_download_task": {
                "command": self.download_command,
                "required_parameter": self.download_path_param,
                "required_filename_token": token,
            },
            "preferred_ingest_args": {
                "callback_display_id": int(callback_id),
                "name_contains": token,
            },
            "assembly_identity": {
                "registered_filename": "SharpHound.exe",
                "parameter_group": "registered_file_selector",
                "upload_group_allowed": False,
            },
        }


@dataclass(frozen=True)
class ObjectiveContract:
    """Structured, immutable scope compiled from one operator objective."""

    objective_text: str
    required_outcomes: tuple[str, ...] = ()
    allowed_capability_families: tuple[str, ...] = ()
    allowed_action_families: tuple[str, ...] = ()
    stop_conditions: tuple[str, ...] = ()
    evidence_requirements: tuple[str, ...] = ()
    engine: ObjectiveEngine = "supervisor_graph"
    approval_policy: ObjectiveApprovalPolicy = "turn_hitl"
    scope_kind: ObjectiveScopeKind = "unclassified"
    task_scope: ObjectiveTaskScope = ""
    scope_resolution: ObjectiveScopeResolution = "not_required"
    scope_resolution_reason: str = ""
    requested_callback_id: str = ""
    resolved_callback_id: str = ""
    resolved_forest: str = ""
    collection_token: str = ""
    collection_profile: CollectionTaskProfile | None = None

    @property
    def is_bounded(self) -> bool:
        return self.scope_kind == "bounded_report"

    @property
    def preserves_capability_freedom(self) -> bool:
        return "*" in self.allowed_capability_families

    @property
    def preserves_action_freedom(self) -> bool:
        return "*" in self.allowed_action_families

    @property
    def is_wildcard(self) -> bool:
        return self.preserves_capability_freedom and self.preserves_action_freedom

    @property
    def requires_collection_scope(self) -> bool:
        return self.task_scope == "sharphound_collection"

    @property
    def collection_scope_resolved(self) -> bool:
        return bool(
            self.requires_collection_scope
            and self.scope_resolution == "resolved"
            and self.resolved_callback_id
            and self.collection_token
            and self.collection_profile is not None
        )

    def bind_turn(self, turn_id: Any) -> "ObjectiveContract":
        if not self.requires_collection_scope:
            return self
        raw_turn_id = _normalize_text(turn_id)
        token = hashlib.sha256(f"sage-collection:{raw_turn_id}".encode("utf-8")).hexdigest()[:16]
        return replace(
            self,
            scope_resolution="unresolved",
            scope_resolution_reason="live collection foothold has not been reconciled",
            resolved_callback_id="",
            resolved_forest="",
            collection_token=token,
            collection_profile=None,
        )

    def with_requested_callback(self, callback_display_id: Any) -> "ObjectiveContract":
        """Return a copy that must bind one exact callback during scope reconciliation."""
        callback_id = _normalized_callback_id(callback_display_id)
        return replace(self, requested_callback_id=callback_id)

    def resolve_collection_scope(
        self,
        *,
        turn_id: Any,
        callback_display_id: Any,
        payload_type: Any,
        forest: Any = "",
        adapter: dict[str, Any] | None = None,
    ) -> "ObjectiveContract":
        bound = self.bind_turn(turn_id)
        callback_id = _normalized_callback_id(callback_display_id)
        payload = _normalize_text(payload_type).casefold()
        if not callback_id or not payload:
            return bound.with_unresolved_scope(
                "the selected collection foothold did not expose a numeric callback and payload type"
            )
        profile = CollectionTaskProfile.from_adapter(payload, adapter)
        resolved_forest = _normalized_forest(forest)
        if not resolved_forest and not profile.search_forest_supported:
            return bound.with_unresolved_scope(
                "the selected collection foothold has no forest and its payload profile cannot search the forest"
            )
        return replace(
            bound,
            scope_resolution="resolved",
            scope_resolution_reason="unique supported live foothold",
            resolved_callback_id=callback_id,
            resolved_forest=resolved_forest,
            collection_profile=profile,
        )

    def with_unresolved_scope(self, reason: Any) -> "ObjectiveContract":
        if not self.requires_collection_scope:
            return self
        return replace(
            self,
            scope_resolution="unresolved",
            scope_resolution_reason=_normalize_text(reason) or "collection scope could not be resolved",
            resolved_callback_id="",
            resolved_forest="",
            collection_profile=None,
        )

    def to_payload(self) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "objective_text": self.objective_text,
            "required_outcomes": list(self.required_outcomes),
            "allowed_capability_families": list(self.allowed_capability_families),
            "allowed_action_families": list(self.allowed_action_families),
            "stop_conditions": list(self.stop_conditions),
            "evidence_requirements": list(self.evidence_requirements),
            "engine": self.engine,
            "approval_policy": self.approval_policy,
            "scope_kind": self.scope_kind,
            "task_scope": self.task_scope or None,
            "resolved_scope": {
                "status": self.scope_resolution,
                "reason": self.scope_resolution_reason or None,
                "callback_display_id": (
                    int(self.resolved_callback_id) if self.resolved_callback_id else None
                ),
                "forest": self.resolved_forest or None,
            },
        }
        if self.requested_callback_id:
            payload["resolved_scope"]["requested_callback_display_id"] = int(self.requested_callback_id)
        if self.requires_collection_scope:
            payload["collection_token"] = self.collection_token or None
        if self.collection_scope_resolved and self.collection_profile is not None:
            payload["collection_task_spec"] = self.collection_profile.to_payload(
                callback_id=self.resolved_callback_id,
                token=self.collection_token,
                forest=self.resolved_forest,
            )
        return payload

    def allows_capability(self, capability_name: Any) -> bool:
        name = _normalize_text(capability_name).casefold()
        if not name:
            return False
        if self.preserves_capability_freedom:
            return True
        return name in {item.casefold() for item in self.allowed_capability_families}

    def allows_action(self, action_name: Any) -> bool:
        name = _normalize_text(action_name).casefold()
        if not name:
            return False
        if self.requires_collection_scope and not self.collection_scope_resolved:
            return False
        if "*" in self.allowed_action_families:
            return True
        return name in {item.casefold() for item in self.allowed_action_families}

    def allows_guarded_tool(self, tool_name: Any, *, capability_name: Any = "") -> bool:
        tool = _normalize_text(tool_name).casefold()
        if not tool:
            return False
        if tool == "execute_capability":
            return self.allows_capability(capability_name)
        return self.allows_action(tool)

    def allows_guarded_tool_call(self, tool_name: Any, args: Any = None) -> bool:
        tool = _normalize_text(tool_name).casefold()
        if self.requires_collection_scope and not self.collection_scope_resolved:
            return False
        if tool == "issue_task_and_waitfor_task_output":
            payload = _decode_mapping(args)
            if payload is None or set(payload) - {
                "command",
                "parameters",
                "callback_display_id",
                "token_id",
                "timeout",
            }:
                return False
            return self.allows_mythic_task(
                payload.get("command"),
                payload.get("parameters"),
                callback_display_id=payload.get("callback_display_id"),
                token_id=payload.get("token_id"),
            )
        if tool == "ingest_collection":
            return self.allows_ingest_collection(args)
        if tool == "read_credentials":
            payload = _decode_mapping(args)
            return bool(
                payload is not None
                and not set(payload) - {"realm", "account"}
                and not _normalize_text(payload.get("realm"))
                and not _normalize_text(payload.get("account"))
                and self.allows_action(tool)
            )
        return self.allows_guarded_tool(
            tool,
            capability_name=self._capability_name_from_args(args),
        )

    @staticmethod
    def _capability_name_from_args(args: Any) -> str:
        payload = _decode_mapping(args)
        if payload is None:
            return ""
        action = payload.get("action")
        if isinstance(action, dict):
            return _normalize_text(action.get("capability") or action.get("name"))
        return _normalize_text(payload.get("capability") or action)

    def _matches_resolved_callback(self, value: Any) -> bool:
        return bool(
            self.collection_scope_resolved
            and _normalized_callback_id(value) == self.resolved_callback_id
        )

    def _matches_runner_task(self, parameters: Any) -> bool:
        profile = self.collection_profile
        payload = _decode_mapping(parameters)
        if profile is None or payload is None or len(payload) != 2:
            return False
        selector_values: list[Any] = []
        argument_values: list[Any] = []
        for raw_key, value in payload.items():
            key = _normalize_text(raw_key).casefold().replace("-", "_")
            if key in _RUNNER_SELECTOR_KEYS:
                selector_values.append(value)
            elif key in _RUNNER_ARGUMENT_KEYS:
                argument_values.append(value)
            else:
                return False
        if len(selector_values) != 1 or len(argument_values) != 1:
            return False
        if _normalize_text(selector_values[0]).casefold() != "sharphound.exe":
            return False
        parsed = _parse_sharphound_arguments(argument_values[0])
        if parsed is None or not _collection_filename_matches(
            parsed.get("zip_filename"),
            self.collection_token,
        ):
            return False
        domain = _normalize_text(parsed.get("domain")).casefold()
        if domain and (not self.resolved_forest or domain != self.resolved_forest):
            return False
        if parsed.get("search_forest") and not profile.search_forest_supported:
            return False
        return True

    @staticmethod
    def _single_parameter(parameters: Any, expected_key: str) -> Any | None:
        payload = _decode_mapping(parameters)
        if payload is None or len(payload) != 1:
            return None
        raw_key, value = next(iter(payload.items()))
        if _normalize_text(raw_key).casefold().replace("-", "_") != expected_key.casefold().replace("-", "_"):
            return None
        return value

    def allows_mythic_task(
        self,
        command: Any,
        parameters: Any,
        *,
        callback_display_id: Any = None,
        token_id: Any = None,
    ) -> bool:
        if self.task_scope != "sharphound_collection":
            return self.preserves_capability_freedom
        profile = self.collection_profile
        if profile is None or not self._matches_resolved_callback(callback_display_id):
            return False
        if token_id is not None:
            return False
        normalized_command = _normalized_command(command)
        if normalized_command == _normalized_command(profile.identity_command):
            return _parameters_match(parameters, profile.identity_parameters_json)
        if profile.ticket_command and normalized_command == _normalized_command(profile.ticket_command):
            return _parameters_match(parameters, profile.ticket_parameters_json)
        if profile.revert_command and normalized_command == _normalized_command(profile.revert_command):
            return _parameters_are_empty(parameters)
        if normalized_command == _normalized_command(profile.runner_command):
            return self._matches_runner_task(parameters)
        if normalized_command == _normalized_command(profile.ls_command):
            path = self._single_parameter(parameters, profile.ls_path_param)
            return path is not None and _canonical_windows_path(path) == _PUBLIC_OUTPUT_DIRECTORY.casefold()
        if normalized_command == _normalized_command(profile.download_command):
            path = self._single_parameter(parameters, profile.download_path_param)
            return path is not None and _safe_collection_zip_path(path, self.collection_token)
        return False

    def allows_ingest_collection(self, args: Any) -> bool:
        if self.task_scope != "sharphound_collection":
            return self.allows_action("ingest_collection")
        if not self.collection_scope_resolved:
            return False
        payload = _decode_mapping(args)
        if payload is None or set(payload) - {
            "file_uuid",
            "callback_display_id",
            "file_name",
            "name_contains",
            "collection_scope_domain",
        }:
            return False
        callback_value = payload.get("callback_display_id")
        callback_was_supplied = callback_value not in (None, "")
        if callback_was_supplied and not self._matches_resolved_callback(callback_value):
            return False
        scope_domain = _normalize_text(payload.get("collection_scope_domain"))
        if scope_domain and not _SAFE_DOMAIN_RE.fullmatch(scope_domain):
            return False
        if scope_domain and (
            not self.resolved_forest
            or scope_domain.casefold() != self.resolved_forest
        ):
            return False
        file_uuid = _normalize_text(payload.get("file_uuid"))
        file_name = _normalize_text(payload.get("file_name"))
        name_contains = _normalize_text(payload.get("name_contains"))
        if file_name and not _collection_filename_matches(file_name, self.collection_token):
            return False
        if file_uuid:
            if not _SAFE_TOKEN_RE.fullmatch(file_uuid):
                return False
            # `name_contains` is ignored by the production UUID resolution path; its public default is
            # `zip`. The authoritative source callback + token-bearing filename are checked at the sink.
            if name_contains and name_contains.casefold() not in {
                "zip",
                self.collection_token.casefold(),
            }:
                return False
            return True
        return bool(
            callback_was_supplied
            and name_contains
            and name_contains.casefold() == self.collection_token.casefold()
        )

    def allows_resolved_ingest(
        self,
        args: Any,
        *,
        source_metadata: Any,
    ) -> bool:
        if not self.requires_collection_scope:
            return self.allows_action("ingest_collection")
        profile = self.collection_profile
        source = _decode_mapping(source_metadata)
        task = source.get("task") if isinstance(source, dict) else None
        task = task if isinstance(task, dict) else {}
        callback = task.get("callback") if isinstance(task.get("callback"), dict) else {}
        return bool(
            profile is not None
            and source is not None
            and self.allows_ingest_collection(args)
            and self._matches_resolved_callback(callback.get("display_id"))
            and _collection_filename_matches(source.get("filename_utf8"), self.collection_token)
            and _normalized_callback_id(task.get("display_id"))
            and _normalized_command(task.get("command_name")) == _normalized_command(profile.download_command)
            and source.get("is_download_from_agent") is True
            and source.get("complete") is True
            and source.get("deleted") is False
        )

    def denial_reason(self, tool_name: Any, *, capability_name: Any = "") -> str:
        if self.requires_collection_scope and not self.collection_scope_resolved:
            return (
                "objective contract has unresolved collection scope: "
                f"{self.scope_resolution_reason or 'no unique supported live foothold'}"
            )
        tool = _normalize_text(tool_name) or "unknown_tool"
        if tool == "execute_capability":
            capability = _normalize_text(capability_name)
            if capability:
                return f"objective contract denies capability `{capability}`"
            return "objective contract requires an admitted capability family"
        return f"objective contract denies guarded action `{tool}`"


def compile_objective_contract(
    objective_text: Any,
    *,
    stored_objective_trigger: bool,
    objective_is_open_ended: bool,
) -> ObjectiveContract:
    """Compile the thinnest admissible data contract for one operator objective."""

    text = _normalize_text(objective_text)
    lowered = text.casefold()
    callback_match = _CALLBACK_RE.search(text)
    requested_callback_id = _normalized_callback_id(callback_match.group(1) if callback_match else "")
    graph_positive, graph_negated = _action_polarity(
        lowered,
        ("collect", "collecting", "ingest", "ingesting"),
    )
    credential_positive, credential_negated = _action_polarity(
        lowered,
        ("read", "reading", "show", "showing", "list", "listing", "report", "reporting"),
    )
    open_positive, open_negated = _action_polarity(
        lowered,
        ("compromise", "compromising", "obtain", "achieve", "gain", "control", "execute", "run"),
    )
    typed_collection_tooling = "sharphound" in lowered and "bloodhound" in lowered
    graph_collection = bool(
        (
            "graph" in lowered
            and graph_positive
        )
        or (
            typed_collection_tooling
            and (graph_positive or open_positive)
        )
    ) and not graph_negated
    credential_report = (
        _contains_any(lowered, ("credential", "credentials"))
        and credential_positive
        and not credential_negated
    )
    bounded_vocabulary = (
        "graph" in lowered
        or typed_collection_tooling
        or _contains_any(lowered, ("credential", "credentials"))
    )

    if (
        _objective_is_informational(text)
        or graph_negated
        or (open_negated and not open_positive)
        or (bounded_vocabulary and not graph_collection)
    ):
        return ObjectiveContract(
            objective_text=text,
            engine="supervisor_graph",
            approval_policy="turn_hitl",
            scope_kind="unclassified",
        )

    if graph_collection and credential_report:
        return ObjectiveContract(
            objective_text=text,
            required_outcomes=("graph_ingested", "credentials_reported"),
            allowed_action_families=(
                "issue_task_and_waitfor_task_output",
                "ingest_collection",
                "read_credentials",
            ),
            stop_conditions=("graph_ingested", "credentials_reported"),
            evidence_requirements=("bloodhound_ingest", "mythic_credential_store"),
            engine="supervisor_graph",
            approval_policy="turn_hitl",
            scope_kind="bounded_report",
            task_scope="sharphound_collection",
            scope_resolution="unresolved",
            requested_callback_id=requested_callback_id,
        )

    if graph_collection:
        return ObjectiveContract(
            objective_text=text,
            required_outcomes=("graph_ingested",),
            allowed_action_families=("issue_task_and_waitfor_task_output", "ingest_collection"),
            stop_conditions=("graph_ingested",),
            evidence_requirements=("bloodhound_ingest",),
            engine="supervisor_graph",
            approval_policy="turn_hitl",
            scope_kind="bounded_report",
            task_scope="sharphound_collection",
            scope_resolution="unresolved",
            requested_callback_id=requested_callback_id,
        )

    if objective_is_open_ended:
        return ObjectiveContract(
            objective_text=text,
            required_outcomes=("objective_satisfied",),
            allowed_capability_families=("*",),
            allowed_action_families=("*",),
            stop_conditions=("objective_satisfied",),
            evidence_requirements=("runtime_verifier_proof",),
            engine="supervisor_graph" if stored_objective_trigger else "controller",
            approval_policy="turn_hitl" if stored_objective_trigger else "controller_hitl",
            scope_kind="open_ended",
        )

    return ObjectiveContract(
        objective_text=text,
        engine="supervisor_graph",
        approval_policy="turn_hitl",
        scope_kind="unclassified",
    )
