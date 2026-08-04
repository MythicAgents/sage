"""Hermetic native-chat behavioral harness.

The driver always enters at ``SageChat.chat``. Authority cases exercise the typed request
projection behind that boundary. Protocol cases replay typed boundary observations so the same
result/evidence assertions cover request authority, subgoal control, and terminal lifecycle.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from ai.langgraph.turn_authority import (
    TurnAuthority,
    authority_from_request_contract,
)
from sage_chat.headless import HeadlessSageChat, build_chat_request

from .cases import ConversationCase


@dataclass(frozen=True)
class CaseResult:
    case_id: str
    entered_sage_chat: bool
    events: tuple[str, ...]
    terminal_state: str
    control_plane: dict[str, int]
    emissions: tuple[dict[str, Any], ...]
    first_divergence: str = ""

    @property
    def required_missing(self) -> tuple[str, ...]:
        return ()


class _BehaviorProbeModel:
    provider = "test"
    model = "conversation-contract"
    mode = "supervised"
    policy_mode = "symbolic"
    _max_steps = 0
    _autonomous_solve = False

    def __init__(self, case: ConversationCase):
        self.case = case
        self.mode = case.session_mode
        self._autonomous_solve = case.session_mode == "auto"
        self._response_emitter = None
        self._thread_id_override = None
        self._pending_approval_context = None
        self._controller_hitl_pending = None
        self._hitl_card_pending = False
        self._stop_requested = False
        self._turn_authority = TurnAuthority(mode="observe")
        self.events: list[str] = []
        self.control_plane = {
            "mythic_tasks": 0,
            "bloodhound_ingests": 0,
            "control_plane_reads": 0,
        }
        self.terminal_state = case.terminal_state

    def install_request_contract(self, contract):
        self._request_contract = contract

    async def _hitl_interrupt_pending(self, _thread_id):
        return False

    def request_stop(self, reason: str = "unspecified"):
        self._stop_requested = True

    async def _close_all_delegations(self, status="finished"):
        del status

    def begin_visibility_turn(self, _turn_id):
        return None

    async def finalize_visibility_turn(self):
        return None

    def controller_runtime_telemetry(self):
        return {}

    def _record(self, event: str) -> None:
        self.events.append(event)
        if event == "external.mythic_task":
            self.control_plane["mythic_tasks"] += 1
        elif event == "external.bloodhound_ingest":
            self.control_plane["bloodhound_ingests"] += 1
        elif event == "external.control_plane_read":
            self.control_plane["control_plane_reads"] += 1

    def _run_authority_case(self, prompt: str) -> None:
        del prompt
        authority = authority_from_request_contract(self._request_contract)
        self._turn_authority = authority
        self._record(f"authority.{authority.mode}")
        if self.case.source_class in {"bounded_read", "set_subtraction"}:
            self._record("proposal.created")
            self._record("external.control_plane_read")
        elif self.case.source_class in {"bounded_action", "mixed_positive_negative"}:
            self._record("proposal.created")
            self._record("approval.pending")
        elif authority.mode == "autonomous_objective":
            self._record("subgoal.assigned")
            self._record("external.mythic_task")

    async def invoke(self, prompt, is_interactive=False):
        del is_interactive
        self._record("operator.input")
        self._record("request.metadata")
        if self.case.driver == "authority":
            self._run_authority_case(prompt)
        else:
            for event in self.case.protocol_events:
                self._record(event)
        self._record("request.terminal")
        await self._response_emitter(
            f"case={self.case.case_id} terminal={self.terminal_state}"
        )
        return ""


class _HarnessChat(HeadlessSageChat):
    def __init__(self, model: _BehaviorProbeModel):
        super().__init__()
        self.model = model
        self.entered_sage_chat = False

    async def chat(self, request):
        self.entered_sage_chat = True
        return await super().chat(request)

    async def _get_or_create_model(self, request):
        del request
        return self.model, False


def _first_divergence(case: ConversationCase, events: tuple[str, ...], state: dict[str, int]) -> str:
    for event in case.required_events:
        if event not in events:
            return f"missing required event: {event}"
    for event in case.forbidden_events:
        if event in events:
            return f"observed forbidden event: {event}"
    for key, expected in case.expected_control_plane.items():
        if state.get(key, 0) != expected:
            return (
                f"control-plane mismatch: {key} expected={expected} "
                f"actual={state.get(key, 0)}"
            )
    return ""


async def run_case(case: ConversationCase) -> CaseResult:
    model = _BehaviorProbeModel(case)
    chat = _HarnessChat(model)
    request = build_chat_request(
        case.prompt,
        channel_id=41,
        request_id=int(case.case_id[1:3]),
        config={"mode": case.session_mode, "autonomous_solve": False},
    )
    await chat.chat(request)
    events = tuple(model.events)
    return CaseResult(
        case_id=case.case_id,
        entered_sage_chat=chat.entered_sage_chat,
        events=events,
        terminal_state=model.terminal_state,
        control_plane=dict(model.control_plane),
        emissions=tuple(chat.emissions),
        first_divergence=_first_divergence(case, events, model.control_plane),
    )
