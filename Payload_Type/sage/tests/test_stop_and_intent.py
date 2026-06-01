"""Tests for the operator kill switch (request_stop) and the continuation-intent classifier.

Covers two fixes from 2026-06-01:
  - request_stop() sets the cooperative flag the graph.astream loops check (so `exit` stops a run).
  - _classify_continuation_intent() maps an operator reply to CONTINUE / STOP / REDIRECT so a
    natural-language inhibit ("don't run any tasks, just give me a summary") is honored as STOP
    instead of being run as a new task (the post-recursion-handback runaway).

Uses Model.__new__ to exercise the units without the heavy __init__ (sqlite + chat model).
Run: cd Payload_Type/sage && python3 -m pytest tests/test_stop_and_intent.py -q
"""
import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))  # Payload_Type/sage
from ai.langgraph.model import Model, _StopCheckMiddleware, _OperatorStopRequested  # noqa: E402
import pytest  # noqa: E402


def _run(coro):
    return asyncio.new_event_loop().run_until_complete(coro)


class _Mdl:
    def __init__(self, stop):
        self._stop_requested = stop


def test_stopcheck_before_model_raises_when_stop_requested():
    mw = _StopCheckMiddleware(_Mdl(True))
    with pytest.raises(_OperatorStopRequested):
        mw.before_model({}, None)


def test_stopcheck_before_model_passes_when_not_stopped():
    mw = _StopCheckMiddleware(_Mdl(False))
    assert mw.before_model({}, None) is None


def test_stopcheck_tool_call_raises_when_stop_requested():
    mw = _StopCheckMiddleware(_Mdl(True))
    async def _handler(req):
        return "ran"
    with pytest.raises(_OperatorStopRequested):
        _run(mw.awrap_tool_call("req", _handler))


def test_stopcheck_tool_call_runs_when_not_stopped():
    mw = _StopCheckMiddleware(_Mdl(False))
    async def _handler(req):
        return "ran"
    assert _run(mw.awrap_tool_call("req", _handler)) == "ran"


def _bare_model() -> Model:
    m = Model.__new__(Model)  # skip heavy __init__ (sqlite, base chat model)
    m.task_id = 1
    m._stop_requested = False
    m.llm = None
    return m


def test_request_stop_sets_flag():
    m = _bare_model()
    assert m._stop_requested is False
    m.request_stop()
    assert m._stop_requested is True


def test_intent_exact_continue_and_stop_no_llm():
    m = _bare_model()  # llm None: exact-match paths must not need the LLM
    assert asyncio.run(m._classify_continuation_intent("continue")) == "CONTINUE"
    assert asyncio.run(m._classify_continuation_intent("  YES ")) == "CONTINUE"
    assert asyncio.run(m._classify_continuation_intent("stop")) == "STOP"
    assert asyncio.run(m._classify_continuation_intent("quit")) == "STOP"


def test_intent_no_llm_falls_back_to_redirect():
    m = _bare_model()  # non-exact + llm None → REDIRECT
    assert asyncio.run(m._classify_continuation_intent("go scan the subnet")) == "REDIRECT"


def test_intent_llm_classifies_inhibit_as_stop():
    m = _bare_model()

    class _Resp:
        content = "STOP"

    class _FakeLLM:
        async def ainvoke(self, msgs):
            return _Resp()

    m.llm = _FakeLLM()
    got = asyncio.run(m._classify_continuation_intent(
        "Don't run any tasks on the callbacks, just give me a summary of progress so far."))
    assert got == "STOP"


def test_intent_llm_redirect_passthrough():
    m = _bare_model()

    class _Resp:
        content = "the answer is REDIRECT"  # extra words around the label still parse

    class _FakeLLM:
        async def ainvoke(self, msgs):
            return _Resp()

    m.llm = _FakeLLM()
    assert asyncio.run(m._classify_continuation_intent("now pivot to the DC and dump creds")) == "REDIRECT"


def test_intent_llm_error_falls_back_to_redirect():
    m = _bare_model()

    class _BadLLM:
        async def ainvoke(self, msgs):
            raise RuntimeError("boom")

    m.llm = _BadLLM()
    assert asyncio.run(m._classify_continuation_intent("some ambiguous instruction")) == "REDIRECT"


if __name__ == "__main__":
    for name, fn in sorted(globals().items()):
        if name.startswith("test_") and callable(fn):
            fn()
            print(f"PASS {name}")
    print("all stop/intent tests passed")
