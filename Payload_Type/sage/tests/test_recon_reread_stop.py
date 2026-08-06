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
from langchain_core.messages import ToolMessage

from ai.langgraph.model import _RECON_REREAD_STOP_LIMIT, _ReconRereadStopMiddleware
from ai.langgraph.mythic_tools import MythicTools


class _Counter:
    """Just the recon-counter surface of MythicTools, using the real methods."""

    def __init__(self):
        self._recon_epoch = 0
        self._recon_call_log = {}

    read = MythicTools._recon_reread_guard
    _max_recon_reread_in_epoch = MythicTools._max_recon_reread_in_epoch

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

    assert mw._before_model_update() == {"jump_to": "end"}


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
    assert mw._before_model_update() == {"jump_to": "end"}

    model._delegation = "mythic_operator:chat:1:request:2:1"
    counter.issue_command()

    assert mw._before_model_update() is None
    assert not _blocked(_drive(mw, counter, 1)[0])


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
