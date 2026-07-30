"""Typed request authority for Sage native chat.

This module deliberately accepts no operator prose.  Transport/session fields select the
maximum execution lane; model output may create a proposal, but only a typed approval binding
can authorize a supervised external action.
"""

from __future__ import annotations

from dataclasses import dataclass, replace
from enum import Enum
import hashlib
import json
import math
from typing import Any, Iterable, Mapping


SCHEMA_VERSION = 1


class RequestLane(str, Enum):
    CONVERSATIONAL = "conversational"
    SUPERVISED_WORKFLOW = "supervised_workflow"
    AUTONOMOUS_OBJECTIVE = "autonomous_objective"


class RequestIntent(str, Enum):
    RESPOND = "respond"
    EXECUTE = "execute"
    CONTINUE = "continue"
    AMEND = "amend"
    STOP = "stop"


class ApprovalDecision(str, Enum):
    APPROVED = "approved"
    REJECTED = "rejected"


class StopConditionKind(str, Enum):
    RESPONSE_EMITTED = "response_emitted"
    ACTIONS_COMPLETE = "actions_complete"
    OBJECTIVE_PROVED = "objective_proved"
    OPERATOR_STOP = "operator_stop"


def _required_text(value: Any, field_name: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{field_name} must be a non-empty string")
    return value.strip()


def _optional_text(value: Any, field_name: str) -> str:
    if value is None:
        return ""
    if not isinstance(value, str):
        raise ValueError(f"{field_name} must be a string")
    return value.strip()


def _unique_text_tuple(values: Iterable[Any], field_name: str) -> tuple[str, ...]:
    normalized = tuple(_required_text(value, field_name) for value in values)
    if len(normalized) != len(set(normalized)):
        raise ValueError(f"{field_name} must not contain duplicates")
    return normalized


def _canonical_json(payload: Mapping[str, Any]) -> str:
    return json.dumps(
        payload,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
        allow_nan=False,
    )


def _digest(payload: Mapping[str, Any]) -> str:
    return hashlib.sha256(_canonical_json(payload).encode("utf-8")).hexdigest()


def canonical_action_arguments(value: Any, path: str = "arguments") -> Any:
    """Return the JSON-native value used for action identity at every boundary."""
    if value is None or isinstance(value, (bool, int, str)):
        return value
    if isinstance(value, float):
        if not math.isfinite(value):
            raise ValueError(f"{path} contains a non-finite number")
        if value.is_integer():
            return int(value)
        return value
    if isinstance(value, list):
        return [
            canonical_action_arguments(item, f"{path}[{index}]")
            for index, item in enumerate(value)
        ]
    if isinstance(value, dict):
        canonical: dict[str, Any] = {}
        for key, item in value.items():
            if not isinstance(key, str):
                raise ValueError(f"{path} contains a non-string object key")
            canonical[key] = canonical_action_arguments(item, f"{path}.{key}")
        return canonical
    raise ValueError(f"{path} contains non-JSON value {type(value).__name__}")


def action_fingerprint(tool_name: Any, arguments: Any) -> str:
    """Canonical identity of one exact model-visible action."""
    name = _required_text(tool_name, "tool_name")
    if not isinstance(arguments, dict):
        raise ValueError("action arguments must be an object")
    payload = {
        "arguments": canonical_action_arguments(arguments),
        "tool_name": name,
    }
    return hashlib.sha256(
        json.dumps(
            payload,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        ).encode("utf-8")
    ).hexdigest()


@dataclass(frozen=True)
class ActionBindings:
    """Exact workflow bindings recovered from one canonical action envelope."""

    callback_id: str = ""
    capability: str = ""


@dataclass(frozen=True)
class ParsedCapabilityRequest:
    """One shared parse used by capability execution and request authorization."""

    action_data: Mapping[str, Any]
    input_values: Mapping[str, Any]
    intent: Mapping[str, Any]
    bindings: ActionBindings


def _decode_binding_object(value: Any, path: str) -> Mapping[str, Any] | None:
    if isinstance(value, Mapping):
        return value
    if not isinstance(value, str):
        return None

    def _object_without_duplicate_keys(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
        result: dict[str, Any] = {}
        for key, item in pairs:
            if key in result:
                raise ValueError(f"{path} contains duplicate JSON key {key!r}")
            result[key] = item
        return result

    try:
        decoded = json.loads(value, object_pairs_hook=_object_without_duplicate_keys)
    except json.JSONDecodeError:
        return None
    return decoded if isinstance(decoded, Mapping) else None


def _binding_text(value: Any, path: str) -> str:
    if value in (None, ""):
        return ""
    if isinstance(value, (Mapping, list, tuple, set)):
        raise ValueError(f"{path} binding must be scalar")
    return str(value).strip()


def _normalized_callback_binding(value: Any, path: str) -> str:
    normalized = _binding_text(value, path).casefold().lstrip("#")
    if normalized.startswith("cb") and normalized[2:].isdigit():
        return normalized[2:]
    return normalized


def _canonical_capability_binding(
    value: Any,
    *,
    action_data: Mapping[str, Any],
    intent: Mapping[str, Any],
    inputs: Mapping[str, Any],
    path: str,
) -> str:
    capability = _binding_text(value, path)
    normalized = capability.casefold()
    if normalized in {
        "prove-domain-admin-control",
        "prove-administrative-control",
        "domain-admin-control-proof",
        "administrative-control-proof",
        "prove-domain-control",
    }:
        return "ensure-kerberos-context"
    if normalized != "dcsync":
        return capability

    raw_account = _binding_text(
        inputs.get("account")
        or inputs.get("user")
        or inputs.get("target_account")
        or intent.get("account")
        or intent.get("user")
        or intent.get("target_account")
        or action_data.get("account")
        or action_data.get("user")
        or action_data.get("target_account"),
        "capability account",
    ).casefold()
    if "\\" in raw_account:
        raw_account = raw_account.rsplit("\\", 1)[1].strip()
    if "@" in raw_account:
        raw_account = raw_account.split("@", 1)[0].strip()
    return "dcsync-krbtgt" if not raw_account or raw_account == "krbtgt" else "dcsync-account"


def parse_capability_request(action: Any, inputs: Any = None) -> ParsedCapabilityRequest:
    """Parse one capability request for both execution and authorization.

    The parser deliberately rejects contradictory binding sources instead of reproducing the
    former first-value precedence. That makes the bytes selected for execution identical to the
    callback/capability identity checked by the request contract.
    """
    action_data = _decode_binding_object(action, "action")
    if action_data is None:
        if not isinstance(action, str) or not action.strip():
            raise ValueError("capability action must be an object or non-empty string")
        if action.lstrip().startswith(("{", "[")):
            raise ValueError("capability action JSON must be a valid object")
        action_data = {"name": action.strip()}
    else:
        action_data = dict(action_data)

    input_values = _decode_binding_object(inputs, "inputs")
    if input_values is None:
        if inputs in (None, ""):
            input_values = {}
        elif isinstance(inputs, str) and inputs.lstrip().startswith(("{", "[")):
            raise ValueError("capability inputs JSON must be a valid object")
        elif isinstance(inputs, Mapping):
            input_values = dict(inputs)
        else:
            input_values = {}
    else:
        input_values = dict(input_values)

    raw_intent = action_data.get("intent")
    intent = _decode_binding_object(raw_intent, "action.intent")
    if intent is None:
        if isinstance(raw_intent, str) and raw_intent.lstrip().startswith(("{", "[")):
            raise ValueError("capability intent JSON must be a valid object")
        intent = {}
    else:
        intent = dict(intent)

    callback_ids: set[str] = set()
    capabilities: set[str] = set()

    def _callback(value: Any, path: str) -> None:
        normalized = _normalized_callback_binding(value, path)
        if normalized:
            callback_ids.add(normalized)

    def _capability(value: Any, path: str) -> None:
        normalized = _canonical_capability_binding(
            value,
            action_data=action_data,
            intent=intent,
            inputs=input_values,
            path=path,
        )
        if normalized:
            capabilities.add(normalized)

    for path, source in (
        ("action.name", action_data.get("name")),
        ("action.capability", action_data.get("capability")),
        ("action.intent.capability", intent.get("capability")),
        ("inputs.capability", input_values.get("capability")),
    ):
        _capability(source, path)
    for path, source in (
        ("action.callback_id", action_data.get("callback_id")),
        ("action.callback", action_data.get("callback")),
        ("action.intent.callback_id", intent.get("callback_id")),
        ("action.intent.callback", intent.get("callback")),
        ("action.intent.callback_display_id", intent.get("callback_display_id")),
        ("inputs.callback_id", input_values.get("callback_id")),
        ("inputs.callback", input_values.get("callback")),
        ("inputs.callback_display_id", input_values.get("callback_display_id")),
    ):
        _callback(source, path)
    target = action_data.get("target")
    if target not in (None, ""):
        if not isinstance(target, str):
            raise ValueError("action.target binding must be a string")
        for part in target.split(";"):
            if "=" not in part:
                continue
            key, candidate = part.split("=", 1)
            if key.strip().casefold() in {"callback", "callback_id"}:
                _callback(candidate, f"action.target.{key.strip()}")
    if len(callback_ids) > 1:
        raise ValueError("action binding contains conflicting callback IDs")
    if len(capabilities) > 1:
        raise ValueError("action binding contains conflicting capabilities")
    bindings = ActionBindings(
        callback_id=next(iter(callback_ids), ""),
        capability=next(iter(capabilities), ""),
    )
    return ParsedCapabilityRequest(
        action_data=action_data,
        input_values=input_values,
        intent=intent,
        bindings=bindings,
    )


def action_binding_values(arguments: Any) -> ActionBindings:
    """Return the exact binding used by the production effect represented by ``arguments``."""
    if not isinstance(arguments, Mapping):
        raise ValueError("action binding arguments must be an object")
    if "action" in arguments:
        unsupported_bindings = {
            key
            for key in (
                "capability",
                "callback_id",
                "callback",
                "callback_display_id",
            )
            if arguments.get(key) not in (None, "")
        }
        if unsupported_bindings:
            raise ValueError(
                "capability request contains conflicting unsupported outer binding fields"
            )
        return parse_capability_request(
            arguments.get("action"),
            arguments.get("inputs"),
        ).bindings

    callbacks = {
        value
        for key in ("callback_id", "callback", "callback_display_id")
        if (value := _normalized_callback_binding(arguments.get(key), f"arguments.{key}"))
    }
    capabilities = {
        value
        for key in ("capability",)
        if (value := _binding_text(arguments.get(key), f"arguments.{key}"))
    }
    if len(callbacks) > 1:
        raise ValueError("action binding contains conflicting callback IDs")
    if len(capabilities) > 1:
        raise ValueError("action binding contains conflicting capabilities")
    return ActionBindings(
        callback_id=next(iter(callbacks), ""),
        capability=next(iter(capabilities), ""),
    )


def action_spec_from_tool_call(action: Mapping[str, Any]) -> "ActionSpec":
    """Build the typed request action from a LangChain-style ``name``/``args`` call."""
    if not isinstance(action, Mapping):
        raise ValueError("action must be an object")
    name = _required_text(action.get("name"), "tool_name")
    arguments = action.get("args")
    if not isinstance(arguments, dict):
        raise ValueError("action arguments must be an object")
    canonical_arguments = canonical_action_arguments(arguments)
    callback_value = action_binding_values(canonical_arguments).callback_id
    target_value = canonical_arguments.get("target")
    return ActionSpec(
        action_id=action_fingerprint(name, canonical_arguments),
        kind="model_tool",
        name=name,
        target="" if target_value in (None, "") else str(target_value),
        callback_id=callback_value,
        arguments_digest=_digest({"arguments": canonical_arguments}),
    )


@dataclass(frozen=True)
class ActionSpec:
    action_id: str
    kind: str
    name: str
    target: str = ""
    callback_id: str = ""
    arguments_digest: str = ""

    def __post_init__(self) -> None:
        object.__setattr__(self, "action_id", _required_text(self.action_id, "action_id"))
        object.__setattr__(self, "kind", _required_text(self.kind, "kind"))
        object.__setattr__(self, "name", _required_text(self.name, "name"))
        object.__setattr__(self, "target", _optional_text(self.target, "target"))
        object.__setattr__(
            self,
            "callback_id",
            _optional_text(self.callback_id, "callback_id"),
        )
        object.__setattr__(
            self,
            "arguments_digest",
            _optional_text(self.arguments_digest, "arguments_digest"),
        )

    def to_payload(self) -> dict[str, str]:
        return {
            "action_id": self.action_id,
            "kind": self.kind,
            "name": self.name,
            "target": self.target,
            "callback_id": self.callback_id,
            "arguments_digest": self.arguments_digest,
        }


@dataclass(frozen=True)
class ActionSelector:
    action_id: str = ""
    kind: str = ""
    name: str = ""
    target: str = ""
    callback_id: str = ""

    def __post_init__(self) -> None:
        for field_name in ("action_id", "kind", "name", "target", "callback_id"):
            object.__setattr__(
                self,
                field_name,
                _optional_text(getattr(self, field_name), field_name),
            )
        if not any(
            getattr(self, field_name)
            for field_name in ("action_id", "kind", "name", "target", "callback_id")
        ):
            raise ValueError("an action selector must constrain at least one field")

    def matches(self, action: ActionSpec) -> bool:
        return all(
            not expected or getattr(action, field_name) == expected
            for field_name, expected in self.to_payload().items()
        )

    def to_payload(self) -> dict[str, str]:
        return {
            "action_id": self.action_id,
            "kind": self.kind,
            "name": self.name,
            "target": self.target,
            "callback_id": self.callback_id,
        }


@dataclass(frozen=True)
class ScopeSpec:
    operation_id: str
    channel_id: str
    callback_ids: tuple[str, ...] = ()
    targets: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        object.__setattr__(
            self,
            "operation_id",
            _required_text(self.operation_id, "operation_id"),
        )
        object.__setattr__(
            self,
            "channel_id",
            _required_text(self.channel_id, "channel_id"),
        )
        object.__setattr__(
            self,
            "callback_ids",
            _unique_text_tuple(self.callback_ids, "callback_ids"),
        )
        object.__setattr__(
            self,
            "targets",
            _unique_text_tuple(self.targets, "targets"),
        )

    def allows(self, action: ActionSpec) -> bool:
        if self.callback_ids and action.callback_id not in self.callback_ids:
            return False
        if self.targets and action.target not in self.targets:
            return False
        return True

    def to_payload(self) -> dict[str, Any]:
        return {
            "operation_id": self.operation_id,
            "channel_id": self.channel_id,
            "callback_ids": list(self.callback_ids),
            "targets": list(self.targets),
        }


@dataclass(frozen=True)
class StopCondition:
    kind: StopConditionKind
    value: str = ""

    def __post_init__(self) -> None:
        if not isinstance(self.kind, StopConditionKind):
            object.__setattr__(self, "kind", StopConditionKind(self.kind))
        object.__setattr__(self, "value", _optional_text(self.value, "value"))

    def to_payload(self) -> dict[str, str]:
        return {"kind": self.kind.value, "value": self.value}


@dataclass(frozen=True)
class RequestContract:
    request_id: str
    revision: int
    lane: RequestLane
    intent: RequestIntent
    scope: ScopeSpec
    stop_condition: StopCondition
    requested_actions: tuple[ActionSpec, ...] = ()
    prohibited_actions: tuple[ActionSelector, ...] = ()
    parent_request_id: str = ""
    schema_version: int = SCHEMA_VERSION

    def __post_init__(self) -> None:
        object.__setattr__(
            self,
            "request_id",
            _required_text(self.request_id, "request_id"),
        )
        if isinstance(self.revision, bool) or not isinstance(self.revision, int):
            raise ValueError("revision must be an integer")
        if self.revision < 0:
            raise ValueError("revision must be non-negative")
        if not isinstance(self.lane, RequestLane):
            object.__setattr__(self, "lane", RequestLane(self.lane))
        if not isinstance(self.intent, RequestIntent):
            object.__setattr__(self, "intent", RequestIntent(self.intent))
        if not isinstance(self.scope, ScopeSpec):
            raise ValueError("scope must be a ScopeSpec")
        if not isinstance(self.stop_condition, StopCondition):
            raise ValueError("stop_condition must be a StopCondition")
        object.__setattr__(
            self,
            "parent_request_id",
            _optional_text(self.parent_request_id, "parent_request_id"),
        )
        if self.schema_version != SCHEMA_VERSION:
            raise ValueError(f"unsupported request contract schema: {self.schema_version}")
        action_ids = tuple(action.action_id for action in self.requested_actions)
        if len(action_ids) != len(set(action_ids)):
            raise ValueError("requested action IDs must be unique")

    @property
    def digest(self) -> str:
        return _digest(self.to_payload())

    @property
    def permitted_actions(self) -> tuple[ActionSpec, ...]:
        return tuple(
            action
            for action in self.requested_actions
            if not any(selector.matches(action) for selector in self.prohibited_actions)
        )

    @property
    def is_terminal_control(self) -> bool:
        return self.intent == RequestIntent.STOP

    def to_payload(self) -> dict[str, Any]:
        return {
            "schema_version": self.schema_version,
            "request_id": self.request_id,
            "revision": self.revision,
            "lane": self.lane.value,
            "intent": self.intent.value,
            "scope": self.scope.to_payload(),
            "stop_condition": self.stop_condition.to_payload(),
            "requested_actions": [
                action.to_payload() for action in self.requested_actions
            ],
            "prohibited_actions": [
                selector.to_payload() for selector in self.prohibited_actions
            ],
            "parent_request_id": self.parent_request_id,
        }

    def amend(
        self,
        *,
        requested_actions: tuple[ActionSpec, ...] | None = None,
        prohibited_actions: tuple[ActionSelector, ...] | None = None,
        scope: ScopeSpec | None = None,
        stop_condition: StopCondition | None = None,
    ) -> "RequestContract":
        return replace(
            self,
            revision=self.revision + 1,
            intent=RequestIntent.AMEND,
            requested_actions=(
                self.requested_actions
                if requested_actions is None
                else requested_actions
            ),
            prohibited_actions=(
                self.prohibited_actions
                if prohibited_actions is None
                else prohibited_actions
            ),
            scope=self.scope if scope is None else scope,
            stop_condition=(
                self.stop_condition
                if stop_condition is None
                else stop_condition
            ),
        )

    def stop(self) -> "RequestContract":
        return replace(
            self,
            revision=self.revision + 1,
            intent=RequestIntent.STOP,
            stop_condition=StopCondition(StopConditionKind.OPERATOR_STOP),
        )


@dataclass(frozen=True)
class RequestProposal:
    contract_digest: str
    actions: tuple[ActionSpec, ...]
    prohibited_actions: tuple[ActionSelector, ...] = ()

    def __post_init__(self) -> None:
        object.__setattr__(
            self,
            "contract_digest",
            _required_text(self.contract_digest, "contract_digest"),
        )
        action_ids = tuple(action.action_id for action in self.actions)
        if not action_ids:
            raise ValueError("a request proposal must contain at least one action")
        if len(action_ids) != len(set(action_ids)):
            raise ValueError("proposal action IDs must be unique")

    @property
    def digest(self) -> str:
        return _digest(self.to_payload())

    @property
    def permitted_actions(self) -> tuple[ActionSpec, ...]:
        return tuple(
            action
            for action in self.actions
            if not any(selector.matches(action) for selector in self.prohibited_actions)
        )

    def to_payload(self) -> dict[str, Any]:
        return {
            "contract_digest": self.contract_digest,
            "actions": [action.to_payload() for action in self.actions],
            "prohibited_actions": [
                selector.to_payload() for selector in self.prohibited_actions
            ],
        }


@dataclass(frozen=True)
class ApprovalBinding:
    contract_digest: str
    proposal_digest: str
    decision: ApprovalDecision
    action_ids: tuple[str, ...]
    actor_id: str

    def __post_init__(self) -> None:
        object.__setattr__(
            self,
            "contract_digest",
            _required_text(self.contract_digest, "contract_digest"),
        )
        object.__setattr__(
            self,
            "proposal_digest",
            _required_text(self.proposal_digest, "proposal_digest"),
        )
        if not isinstance(self.decision, ApprovalDecision):
            object.__setattr__(
                self,
                "decision",
                ApprovalDecision(self.decision),
            )
        object.__setattr__(
            self,
            "action_ids",
            _unique_text_tuple(self.action_ids, "action_ids"),
        )
        object.__setattr__(
            self,
            "actor_id",
            _required_text(self.actor_id, "actor_id"),
        )


@dataclass(frozen=True)
class ExecutionLedger:
    admitted_action_ids: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        object.__setattr__(
            self,
            "admitted_action_ids",
            _unique_text_tuple(
                self.admitted_action_ids,
                "admitted_action_ids",
            ),
        )

    def admit(self, action_id: str) -> tuple["ExecutionLedger", bool]:
        normalized = _required_text(action_id, "action_id")
        if normalized in self.admitted_action_ids:
            return self, False
        return replace(
            self,
            admitted_action_ids=(*self.admitted_action_ids, normalized),
        ), True


@dataclass(frozen=True)
class Admission:
    allowed: bool
    reason: str
    ledger: ExecutionLedger


def contract_action_denial_reason(
    contract: RequestContract,
    action: ActionSpec,
) -> str:
    """Return the base request/scope/set denial shared by middleware and final sinks."""
    if contract.is_terminal_control:
        return "request is stopped"
    if not contract.scope.allows(action):
        return "action is outside request scope"
    if any(selector.matches(action) for selector in contract.prohibited_actions):
        return "action is prohibited by request contract"
    if contract.requested_actions and action.action_id not in {
        item.action_id for item in contract.permitted_actions
    }:
        return "action is not in the permitted requested set"
    if contract.lane == RequestLane.CONVERSATIONAL:
        return "conversational lane denies external actions"
    return ""


def build_request_contract(
    *,
    request_id: Any,
    channel_id: Any,
    operation_id: Any,
    mode: Any,
    autonomous_solve: Any,
    intent: RequestIntent | str | None = None,
    requested_actions: tuple[ActionSpec, ...] = (),
    prohibited_actions: tuple[ActionSelector, ...] = (),
    callback_ids: tuple[str, ...] = (),
    targets: tuple[str, ...] = (),
    parent_request_id: Any = "",
) -> RequestContract:
    """Build authority from typed transport/session fields only.

    There is intentionally no ``prompt`` or classifier parameter.
    """
    normalized_mode = _required_text(str(mode), "mode").casefold()
    if not isinstance(autonomous_solve, bool):
        raise ValueError("autonomous_solve must be a boolean")
    if normalized_mode == "auto" or (
        normalized_mode == "conversation" and autonomous_solve
    ):
        lane = RequestLane.AUTONOMOUS_OBJECTIVE
        default_intent = RequestIntent.EXECUTE
        stop_kind = StopConditionKind.OBJECTIVE_PROVED
    elif normalized_mode == "supervised":
        lane = RequestLane.SUPERVISED_WORKFLOW
        default_intent = RequestIntent.EXECUTE
        stop_kind = StopConditionKind.ACTIONS_COMPLETE
    elif normalized_mode == "conversation":
        lane = RequestLane.CONVERSATIONAL
        default_intent = RequestIntent.RESPOND
        stop_kind = StopConditionKind.RESPONSE_EMITTED
    else:
        raise ValueError(f"unsupported request mode: {normalized_mode}")
    resolved_intent = default_intent if intent is None else RequestIntent(intent)
    return RequestContract(
        request_id=_required_text(str(request_id), "request_id"),
        revision=0,
        lane=lane,
        intent=resolved_intent,
        scope=ScopeSpec(
            operation_id=_required_text(str(operation_id), "operation_id"),
            channel_id=_required_text(str(channel_id), "channel_id"),
            callback_ids=callback_ids,
            targets=targets,
        ),
        stop_condition=StopCondition(stop_kind),
        requested_actions=requested_actions,
        prohibited_actions=prohibited_actions,
        parent_request_id=_optional_text(
            str(parent_request_id) if parent_request_id is not None else "",
            "parent_request_id",
        ),
    )


def admit_action(
    contract: RequestContract,
    action: ActionSpec,
    *,
    proposal: RequestProposal | None = None,
    approval: ApprovalBinding | None = None,
    ledger: ExecutionLedger | None = None,
) -> Admission:
    """Evaluate an exact action against one immutable request contract."""
    state = ledger or ExecutionLedger()
    denial = contract_action_denial_reason(contract, action)
    if denial:
        return Admission(False, denial, state)
    if contract.lane == RequestLane.SUPERVISED_WORKFLOW:
        if proposal is None or proposal.contract_digest != contract.digest:
            return Admission(False, "supervised action lacks a bound proposal", state)
        if action.action_id not in {
            item.action_id for item in proposal.permitted_actions
        }:
            return Admission(False, "action is not permitted by the proposal", state)
        if approval is None:
            return Admission(False, "supervised action lacks approval", state)
        if (
            approval.contract_digest != contract.digest
            or approval.proposal_digest != proposal.digest
        ):
            return Admission(False, "approval binding is stale or mismatched", state)
        if approval.decision != ApprovalDecision.APPROVED:
            return Admission(False, "action was rejected", state)
        if action.action_id not in approval.action_ids:
            return Admission(False, "approval does not cover this action", state)
    next_state, admitted = state.admit(action.action_id)
    if not admitted:
        return Admission(False, "action was already admitted", state)
    return Admission(True, "", next_state)
