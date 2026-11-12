"""A request that ends mid-delegation terminates normally instead of as status=error.

A refusal that ends a request while a specialist's delegation is open (an unplannable capability, an
identical re-proposal stop), or a delegation whose card was lost to an interrupted resume, left a
delegation `opened` event with no terminal. The pre-terminal reconciliation then raised and the request
ended `status=error` with its sub-agent card stuck on "running" (supervised runs 2026-10-16 and
2026-10-29). Open delegations now close as `stopped`, like open tools already did.
"""

import asyncio
from types import SimpleNamespace

import pytest

from ai.langgraph import model as model_module
from ai.langgraph.request_events import RequestEventLedger
from sage_chat.headless import HeadlessSageChat, build_chat_request


class _Emitter:
    def __init__(self):
        self.statuses = []
        self.texts = []

    async def __call__(self, text):
        self.texts.append(text)
        return True

    async def emit_subagent_status(self, **kwargs):
        self.statuses.append(kwargs)
        return True

    async def emit_tool_use(self, **kwargs):
        return True


def _model(request_id="chat:5:request:3", cls=model_module.Model):
    model = object.__new__(cls)
    model._request_event_ledger = RequestEventLedger(request_id)
    model._response_emitter = _Emitter()
    model._active_delegations = {}
    model._delegation_seq = 0
    model._delegation_scope = request_id
    model.task_id = 0
    # A real request has already recorded its operator input and installed contract before any
    # delegation opens; reconciliation requires both.
    ledger = model._request_event_ledger
    ledger.record_once(event_id=f"{request_id}:input", kind="operator_input", phase="received",
                       content="run the gpo capability", metadata={})
    ledger.record_once(event_id=f"{request_id}:contract", kind="control_transition",
                       phase="request_installed", content="request contract installed", metadata={})
    return model


async def _open(model, agent="Mythic_Operator"):
    await model._open_delegation(agent, "Run the GPO capability.", source_seq=7, title="GPO exec")
    return model._active_delegations[agent]["event_id"]


def test_ledger_only_open_delegation_is_closed_as_stopped_and_reconciles():
    model = _model()

    async def run():
        event_id = await _open(model)
        model._active_delegations.clear()  # the in-memory card is gone; only the ledger knows
        before = await model.finalize_visibility_turn(require_final=False)
        await model._close_all_request_lifecycles(status="stopped")
        after = await model.finalize_visibility_turn(require_final=False)
        return event_id, before, after

    event_id, before, after = asyncio.run(run())
    assert not before["ok"] and any("delegation terminal count=0" in e for e in before["errors"])
    assert after["ok"], after["errors"]
    closes = [s for s in model._response_emitter.statuses if s.get("complete")]
    assert len(closes) == 1
    assert closes[0]["event_id"] == event_id and closes[0]["status"] == "stopped"
    assert closes[0]["delegation_name"] == "Mythic_Operator" and closes[0]["title"] == "GPO exec"


def test_in_memory_open_card_closes_once_without_a_second_terminal():
    model = _model()

    async def run():
        await _open(model)
        await model._close_all_request_lifecycles(status="stopped")
        await model._close_all_request_lifecycles(status="stopped")  # repeat is a no-op
        return await model.finalize_visibility_turn(require_final=False)

    summary = asyncio.run(run())
    assert summary["ok"], summary["errors"]
    assert len([s for s in model._response_emitter.statuses if s.get("complete")]) == 1


def test_finished_delegation_is_left_alone():
    model = _model()

    async def run():
        await _open(model)
        await model._close_delegation("Mythic_Operator", content="Done.", status="finished")
        await model._close_all_request_lifecycles(status="stopped")
        return await model.finalize_visibility_turn(require_final=False)

    summary = asyncio.run(run())
    assert summary["ok"], summary["errors"]
    closes = [s for s in model._response_emitter.statuses if s.get("complete")]
    assert [c["status"] for c in closes] == ["finished"]


class _RefusalMidDelegationModel:
    """The chat service's terminal path against a real Model ledger, ending mid-delegation."""

    provider = "test"
    model = "test"

    def __init__(self):
        self._inner = _model("chat:5:request:3")
        self._thread_id_override = None

    def __getattr__(self, name):
        return getattr(self._inner, name)

    @property
    def _response_emitter(self):
        return self._inner._response_emitter

    @_response_emitter.setter
    def _response_emitter(self, emitter):
        # Keep the service's real stream emitter, but record sub-agent card closes through it too.
        self._inner._response_emitter = emitter

    async def _hitl_interrupt_pending(self, thread_id):
        return False

    def install_request_contract(self, contract):
        self._inner._request_contract = contract

    async def invoke(self, prompt, is_interactive=False):
        await self._inner._open_delegation("Mythic_Operator", "Run the GPO capability.", 7, "GPO exec")
        self._inner._active_delegations.clear()
        await self._inner._response_emitter("⛔ Blocked — the capability request could not be planned.")
        return "⛔ Blocked — the capability request could not be planned."


class _DriverChat(HeadlessSageChat):
    def __init__(self, model):
        super().__init__()
        self._model = model

    async def _get_or_create_model(self, request):
        return self._model, False


def test_assembled_refusal_mid_delegation_terminates_complete_not_error():
    model = _RefusalMidDelegationModel()
    chat = _DriverChat(model)
    asyncio.run(chat.chat(build_chat_request("run the gpo capability", channel_id=5, request_id=3)))
    terminal = chat.terminal_emissions[-1]
    assert terminal["kind"] == "complete", terminal
    assert "reconciliation failed" not in str(terminal.get("content"))
    assert terminal["content"].startswith("⛔ Blocked")
    stopped = [e for e in chat.emissions
               if (e.get("metadata") or {}).get("special_type") == "subagent"
               and (e.get("metadata") or {}).get("subagent", {}).get("status") == "stopped"]
    assert len(stopped) == 1


# ── The real interrupt surface's refusal branches, end to end through the chat service ──────────────
#
# The test above scripts the refusal text. These drive `Model._surface_hitl_interrupt` itself: its two
# refusal returns (an identical re-proposal stop, a capability that cannot be planned) end the worker's
# step while the specialist's delegation and a child tool are still open, as live supervised requests
# did. Everything after the scripted worker step is production code: the service's terminal
# path, the lifecycle closes, the stream emitter, the ledger and the reconciliation.

_UNPLANNABLE = {
    # Graph collection with no callback: the card planner reports "could not be built: callback_id".
    "name": "execute_capability",
    "args": {"action": {"capability": "collect-graph", "domain": "lab.local"}},
}
_REPEATED = {
    "name": "issue_task_and_waitfor_task_output",
    "args": {"command": "ticket_cache_list", "callback_display_id": 7, "parameters": ""},
}
_REFUSAL_PREFIX = {
    "unplannable": "⛔ **Blocked — the capability request could not be planned.**",
    "repeated": f"🛑 **Stopped — the same action was proposed {model_module._ZERO_PROGRESS_DELEGATION_CAP} times.**",
}


def _interrupt(action):
    return {"__interrupt__": [SimpleNamespace(value={"action_requests": [dict(action)]})]}


class _WorkerStepModel(model_module.Model):
    """A real Model whose turn is one scripted worker step ending at the real interrupt surface.

    Only `invoke` and the two session hooks the service calls before it are replaced. The step opens the
    delegation and starts a child tool through the production card paths, optionally loses the in-memory
    card (an interrupted resume), then hands the proposal to `_surface_hitl_interrupt` and returns "" as the
    worker loop does after an interrupt.
    """

    provider = "test"
    model = "test"
    proposal = _UNPLANNABLE
    lose_card = False

    async def _hitl_interrupt_pending(self, thread_id):
        return False

    def install_request_contract(self, contract):
        self._request_contract = contract

    async def invoke(self, prompt, is_interactive=False):
        await self._open_delegation("Mythic_Operator", "Collect the graph.", 7, title="Collect")
        await self._emit_tool_use_card(
            tool_call_id="call-1",
            tool_name="issue_task_and_waitfor_task_output",
            status="started",
            complete=False,
            delegation_id=self.current_delegation_id("Mythic_Operator"),
            delegation_name="Mythic_Operator",
            tool_source="mythic",
        )
        if self.lose_card:
            self._active_delegations.clear()
        await self._surface_hitl_interrupt(_interrupt(self.proposal))
        return ""


def _worker_step_model(branch, lose_card):
    model = _model("chat:5:request:3", cls=_WorkerStepModel)
    model.verbose = False
    model.mythic_client = SimpleNamespace(_last_issued_task_display_id=None)
    model.proposal = _UNPLANNABLE if branch == "unplannable" else _REPEATED
    model.lose_card = lose_card
    if branch == "repeated":
        # The cards before the refused one, through the real streak counter. The service rebinds the card
        # emitter and clears `_hitl_card_pending` at the start of the turn that follows.
        cards = []

        async def earlier_card(action_requests):
            cards.append(action_requests)

        model._hitl_card_emitter = earlier_card
        for _ in range(model_module._ZERO_PROGRESS_DELEGATION_CAP - 1):
            assert asyncio.run(model._surface_hitl_interrupt(_interrupt(_REPEATED))) is True
        assert len(cards) == model_module._ZERO_PROGRESS_DELEGATION_CAP - 1
    return model


def _phases(ledger, kind, *, projected=False):
    return [e.phase for e in ledger.events if e.kind == kind and e.projected is projected]


@pytest.mark.parametrize("lose_card", [False, True], ids=["card-in-memory", "card-lost"])
@pytest.mark.parametrize("branch", ["unplannable", "repeated"])
def test_real_refusal_branch_mid_delegation_terminates_complete_and_reconciles(branch, lose_card):
    model = _worker_step_model(branch, lose_card)
    chat = _DriverChat(model)
    asyncio.run(chat.chat(build_chat_request("collect the graph", channel_id=5, request_id=3)))

    # One non-error terminal that carries the refusal, and no reconciliation warning anywhere.
    assert len(chat.terminal_emissions) == 1, chat.terminal_emissions
    terminal = chat.terminal_emissions[0]
    assert terminal["kind"] == "complete", terminal
    assert terminal["content"].startswith(_REFUSAL_PREFIX[branch]), terminal["content"]
    assert not any("reconciliation failed" in str(e.get("content", "")) + str(e.get("error", ""))
                   for e in chat.emissions)
    assert model._hitl_card_pending is False  # a refusal releases no card

    # The delegation and its child tool each end once as stopped, each projected once; nothing stays open.
    ledger = model._request_event_ledger
    assert _phases(ledger, "delegation") == ["opened", "stopped"]
    assert _phases(ledger, "delegation", projected=True) == ["opened", "stopped"]
    assert _phases(ledger, "tool") == ["started", "stopped"]
    assert _phases(ledger, "tool", projected=True) == ["started", "stopped"]
    assert ledger.open_lifecycles() == ()
    terminals = [e for e in ledger.events if e.kind == "control_transition" and e.phase == "request_terminal"]
    assert [e.content for e in terminals] == ["complete"]
    final = ledger.reconcile(require_final=True)
    assert final["ok"], final["errors"]

    # Mythic sees the sub-agent card close as stopped, once, so it is not left running.
    delegation_id = next(e.event_id for e in ledger.events if e.kind == "delegation")
    stopped_cards = [
        e for e in chat.emissions
        if (e.get("metadata") or {}).get("special_type") == "subagent"
        and (e.get("metadata") or {}).get("event_id") == delegation_id
        and (e.get("metadata") or {}).get("subagent", {}).get("status") == "stopped"
    ]
    assert len(stopped_cards) == 1, stopped_cards


def test_a_card_still_awaiting_approval_keeps_its_delegation_open_without_a_final():
    """Near match: the same step, but the proposal is plannable and becomes a real approval card."""
    model = _worker_step_model("repeated", lose_card=False)
    model._last_card_fingerprint = ""  # a fresh proposal, not the third repeat
    model._repeat_card_count = 0
    chat = _DriverChat(model)
    asyncio.run(chat.chat(build_chat_request("collect the graph", channel_id=5, request_id=3)))

    assert model._hitl_card_pending is True
    assert chat.terminal_emissions == []
    assert any((e.get("metadata") or {}).get("special_type") == "input_requested"
               or "input_requested" in str(e.get("response_key", "")) for e in chat.emissions)
    ledger = model._request_event_ledger
    assert _phases(ledger, "delegation") == ["opened"]
    assert _phases(ledger, "tool") == ["started"]
    assert not [e for e in ledger.events if e.kind == "final_response"]
    assert {kind for kind, _ in ledger.open_lifecycles()} == {"delegation", "tool"}
    assert not any((e.get("metadata") or {}).get("subagent", {}).get("status") == "stopped"
                   for e in chat.emissions)
