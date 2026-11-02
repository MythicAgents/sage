"""A worker that only re-reads the same recon target must end its turn, not burn the budget.

`_recon_reread_guard` already appended a "STOP re-reading" nudge, and a live request ignored it 33
times in a row — 36 `list_callbacks` calls inside one delegation until the graph hit
`Recursion limit of 250`. The loop never crossed a delegation boundary, so the no-progress and
pair-bounce detectors (which fire when a specialist RETURNS) never saw it. These tests pin the
structural stop that does.
"""

import json
import types

import pytest
from langchain_core.messages import AIMessage, HumanMessage, ToolMessage

from ai.langgraph.model import _RECON_REREAD_STOP_LIMIT, _ReconRereadStopMiddleware
from ai.langgraph.mythic_tools import MythicTools


class _Counter:
    """Just the recon-counter surface of MythicTools, using the real methods."""

    def __init__(self):
        self._recon_epoch = 0
        self._recon_call_log = {}

    read = MythicTools._recon_reread_guard
    _max_recon_reread_in_epoch = MythicTools._max_recon_reread_in_epoch
    _recon_reread_peak = MythicTools._recon_reread_peak
    _start_recon_scope = MythicTools._start_recon_scope

    def issue_command(self):
        """Mirror the epoch bump a real issued command performs."""
        self._recon_epoch += 1


class _Model:
    def __init__(self, counter, delegation="mythic_operator:chat:1:request:1:1"):
        self.mythic_client = counter
        self._delegation = delegation

    def current_delegation_id(self, agent_name):
        return self._delegation


def _request(name="list_callbacks", call_id="call-1"):
    return types.SimpleNamespace(
        tool_call={"name": name, "args": {}, "id": call_id},
        tool=None,
        state={},
        runtime=None,
    )


def _drive(mw, counter, times, tool="list_callbacks", key="cb:1"):
    """Run `times` tool calls through the middleware, as the react loop would."""
    results = []
    for _ in range(times):
        result = mw.wrap_tool_call(
            _request(tool),
            lambda req: (counter.read(tool, key), ToolMessage(
                content="[]", name=tool, tool_call_id="call-1"))[1],
        )
        results.append(result)
    return results


def _blocked(result) -> bool:
    try:
        return json.loads(result.content).get("capability") == "recon-reread-boundary"
    except (AttributeError, ValueError):
        return False


def test_reads_below_the_limit_are_untouched():
    counter = _Counter()
    mw = _ReconRereadStopMiddleware(_Model(counter), "Mythic_Operator")

    results = _drive(mw, counter, _RECON_REREAD_STOP_LIMIT - 1)

    assert not any(_blocked(r) for r in results)
    assert mw._before_model_update() is None


def test_the_limit_ends_the_turn():
    counter = _Counter()
    mw = _ReconRereadStopMiddleware(_Model(counter), "Mythic_Operator")

    _drive(mw, counter, _RECON_REREAD_STOP_LIMIT)

    update = mw._before_model_update()
    assert update["jump_to"] == "end"


def test_the_stop_leaves_a_typed_observation_naming_what_was_reread():
    """A bare jump left the handback empty, so the Supervisor re-sent the same step."""

    counter = _Counter()
    mw = _ReconRereadStopMiddleware(_Model(counter), "Mythic_Operator")
    _drive(mw, counter, _RECON_REREAD_STOP_LIMIT, key="all")

    (message,) = mw._before_model_update()["messages"]

    assert isinstance(message, AIMessage)
    assert message.name == "Mythic_Operator"
    assert message.additional_kwargs["_recon_reread_boundary"] == {
        "capability": "recon-reread-boundary",
        "tool": "list_callbacks",
        "target": "all",
        "count": _RECON_REREAD_STOP_LIMIT,
        "limit": _RECON_REREAD_STOP_LIMIT,
    }
    assert "list_callbacks" in message.content and "'all'" in message.content


def test_further_tool_calls_are_blocked_after_the_limit():
    counter = _Counter()
    mw = _ReconRereadStopMiddleware(_Model(counter), "Mythic_Operator")
    _drive(mw, counter, _RECON_REREAD_STOP_LIMIT)

    after = _drive(mw, counter, 1)[0]

    assert _blocked(after)
    payload = json.loads(after.content)
    assert payload["next_action"] == "handback_to_supervisor"
    assert payload["ok"] is False


def test_issuing_a_command_resets_the_epoch_so_legitimate_rereads_never_trip():
    """The valid near-match: read, act, read again, forever. Must never stop."""

    counter = _Counter()
    mw = _ReconRereadStopMiddleware(_Model(counter), "Mythic_Operator")

    for _ in range(_RECON_REREAD_STOP_LIMIT * 3):
        _drive(mw, counter, 2)
        counter.issue_command()

    assert mw._before_model_update() is None


def test_distinct_targets_do_not_aggregate():
    """Reading many different targets once each is exploration, not a loop."""

    counter = _Counter()
    mw = _ReconRereadStopMiddleware(_Model(counter), "Mythic_Operator")

    for i in range(_RECON_REREAD_STOP_LIMIT * 2):
        _drive(mw, counter, 1, key=f"cb:{i}")

    assert mw._before_model_update() is None


def test_a_new_delegation_starts_clean():
    counter = _Counter()
    model = _Model(counter)
    mw = _ReconRereadStopMiddleware(model, "Mythic_Operator")
    _drive(mw, counter, _RECON_REREAD_STOP_LIMIT)
    assert mw._before_model_update()["jump_to"] == "end"

    # No command is issued: the new delegation's own scope start is what clears the count.
    model._delegation = "mythic_operator:chat:1:request:2:1"
    counter._start_recon_scope()

    assert mw._before_model_update() is None
    assert not _blocked(_drive(mw, counter, 1)[0])


def test_a_headless_run_resets_on_the_new_epoch_alone():
    """Headless runs open no delegation card, so the delegation key never changes; the epoch must clear the trip."""

    counter = _Counter()
    model = _Model(counter, delegation=None)
    mw = _ReconRereadStopMiddleware(model, "Mythic_Operator")
    _drive(mw, counter, _RECON_REREAD_STOP_LIMIT)
    assert mw._before_model_update()["jump_to"] == "end"

    counter._start_recon_scope()

    assert mw._before_model_update() is None


def test_missing_counter_surface_never_stops_anything():
    """Fail open on the *containment* path: a stop must never fire on a bad reading."""

    mw = _ReconRereadStopMiddleware(_Model(object()), "Mythic_Operator")
    assert mw._current_streak() == 0
    assert mw._before_model_update() is None


@pytest.mark.parametrize("agent", ["Mythic_Operator", "Mythic_Payload"])
def test_both_mythic_workers_get_the_stop_wired(agent):
    """The flag has to actually reach the agents that own the recon reads."""

    import inspect

    from ai.langgraph.model import Model

    builder = {
        "Mythic_Operator": Model._mythic_operator_agent,
        "Mythic_Payload": Model._mythic_payload_agent,
    }[agent]
    assert "recon_reread_stop=True" in inspect.getsource(builder)


# --- Assembled path: the real worker-node wrapper starts the scope -------------------------------


def _wrapped_worker(counter, mw, seen):
    """A real `_wrap_create_agent` Mythic_Operator node whose agent consults the real middleware first."""

    from ai.langgraph import model as model_mod
    from ai.langgraph import turn_authority as turn_mod

    class FakeAgent:
        async def ainvoke(self, args, config=None):
            seen.append(mw._before_model_update())
            return {"messages": list(args["messages"]) + [AIMessage(content="done", name="Mythic_Operator")]}

    m = model_mod.Model.__new__(model_mod.Model)
    m._turn_authority = turn_mod.compile_turn_authority("List callbacks.", objective_classifier=lambda _t: False)
    m._autonomous_solve = False
    m._message_seq = 1
    m.state = {"_message_seq": 1}
    m.llm = None
    m.verbose = False
    m.mythic_client = counter
    return m, m._wrap_create_agent(FakeAgent(), "mythic_operator_messages", "Mythic_Operator")


def _state(*delegated):
    return {
        "_message_seq": 1,
        "supervisor_messages": [],
        "generalist_messages": [],
        "mythic_operator_messages": list(delegated),
        "mythic_payload_messages": [],
        "mcp_manager_messages": [],
        "bloodhound_messages": [],
        "sandbox_messages": [],
    }


def _delegated(msg_id, seq):
    return HumanMessage(
        content="List callbacks.",
        id=msg_id,
        additional_kwargs={"_delegated_to": "Mythic_Operator", "_seq": seq},
    )


def test_supervisor_reads_do_not_stop_the_next_worker_delegation():
    """Channel 18 request 25: six Supervisor list_callbacks reads, then the worker died before its model call."""

    import asyncio

    counter = _Counter()
    for _ in range(_RECON_REREAD_STOP_LIMIT):
        counter.read("list_callbacks", "all")  # the Supervisor filling briefs; no task issued
    mw = _ReconRereadStopMiddleware(_Model(counter, delegation=None), "Mythic_Operator")
    seen = []
    _m, worker = _wrapped_worker(counter, mw, seen)

    asyncio.run(worker(_state(_delegated("d1", 5)), {}))

    assert seen == [None]


def test_reads_inside_one_delegation_still_stop_it():
    """The original 36-call loop shape: the scope start must not reset reads within the delegation."""

    import asyncio

    counter = _Counter()
    mw = _ReconRereadStopMiddleware(_Model(counter, delegation=None), "Mythic_Operator")
    seen = []
    _m, worker = _wrapped_worker(counter, mw, seen)
    state = _state(_delegated("d1", 5))
    asyncio.run(worker(state, {}))
    for _ in range(_RECON_REREAD_STOP_LIMIT):
        counter.read("list_callbacks", "all")

    # Same delegated instruction re-entered (an approval resume): the count carries over.
    asyncio.run(worker(_state(_delegated("d1", 5)), {}))

    assert seen[0] is None
    assert seen[1]["jump_to"] == "end"


def test_a_new_delegated_instruction_starts_at_zero():
    import asyncio

    counter = _Counter()
    mw = _ReconRereadStopMiddleware(_Model(counter, delegation=None), "Mythic_Operator")
    seen = []
    _m, worker = _wrapped_worker(counter, mw, seen)
    asyncio.run(worker(_state(_delegated("d1", 5)), {}))
    for _ in range(_RECON_REREAD_STOP_LIMIT):
        counter.read("list_callbacks", "all")

    asyncio.run(worker(_state(_delegated("d2", 9)), {}))

    assert seen == [None, None]
