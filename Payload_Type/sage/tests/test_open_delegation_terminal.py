"""A request that ends mid-delegation terminates normally instead of as status=error.

A refusal that ends a request while a specialist's delegation is open (an unplannable capability, an
identical re-proposal stop), or a delegation whose card was lost to an interrupted resume, left a
delegation `opened` event with no terminal. The pre-terminal reconciliation then raised and the request
ended `status=error` with its sub-agent card stuck on "running" (supervised runs 2026-10-16 and
2026-10-29). Open delegations now close as `stopped`, like open tools already did.
"""

import asyncio

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


def _model(request_id="chat:5:request:3"):
    model = object.__new__(model_module.Model)
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
