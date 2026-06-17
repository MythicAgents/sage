"""Tests for the operator kill switch (request_stop) and the continuation-intent classifier.

Covers two fixes from 2026-06-01:
  - request_stop() sets the cooperative flag and cancels the active invoke task (so `exit`/`stop`
    interrupts waits/proof polling instead of letting delayed Mythic tasks fire after stop).
  - _classify_continuation_intent() maps an operator reply to CONTINUE / STOP / REDIRECT so a
    natural-language inhibit ("don't run any tasks, just give me a summary") is honored as STOP
    instead of being run as a new task (the post-recursion-handback runaway).

Uses Model.__new__ to exercise the units without the heavy __init__ (sqlite + chat model).
Run: cd Payload_Type/sage && python3 -m pytest tests/test_stop_and_intent.py -q
"""
import asyncio
import sys
from pathlib import Path
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))  # Payload_Type/sage
from ai.langgraph import model as model_mod  # noqa: E402
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
    m._running_tasks = set()
    m.llm = None
    return m


def test_request_stop_sets_flag():
    m = _bare_model()
    assert m._stop_requested is False
    m.request_stop()
    assert m._stop_requested is True


def test_request_stop_cancels_registered_running_task():
    async def _case():
        m = _bare_model()
        started = asyncio.Event()

        async def _blocked_tool_wait():
            started.set()
            await asyncio.sleep(60)

        task = asyncio.create_task(_blocked_tool_wait())
        m._register_running_task(task)
        await started.wait()

        m.request_stop()

        with pytest.raises(asyncio.CancelledError):
            await task
        assert m._stop_requested is True
        assert task.cancelled()

    asyncio.run(_case())


def test_request_stop_does_not_cancel_caller_task():
    async def _case():
        m = _bare_model()
        current = asyncio.current_task()
        m._register_running_task(current)

        m.request_stop()

        assert m._stop_requested is True
        assert current.cancelled() is False

    asyncio.run(_case())


def test_session_stop_helper_requests_stop_by_display_id():
    class _FakeModel:
        provider = "test"
        model = "test"
        task_id = 101
        task_display_id = 202

        def __init__(self):
            self.stopped = False

        def request_stop(self):
            self.stopped = True

    async def _case():
        model_mod.sessions.clear()
        fake = _FakeModel()
        await model_mod.add_session("101", fake)
        stopped = await model_mod.request_stop_for_sessions("202")
        assert stopped == {"101": fake}
        assert fake.stopped is True
        await model_mod.remove_session("101")

    asyncio.run(_case())


def test_stop_command_marks_active_llm_task_stopped(monkeypatch):
    from container.agent_functions import stop as stop_mod  # noqa: E402

    class _FakeModel:
        provider = "test"
        model = "test"
        task_id = 101
        task_display_id = 202

        def __init__(self):
            self.stopped = False

        def request_stop(self):
            self.stopped = True

    class _Args:
        def get_arg(self, key):
            return ""

    async def _case():
        model_mod.sessions.clear()
        fake = _FakeModel()
        await model_mod.add_session("101", fake)
        updates = []
        responses = []

        async def fake_update(msg):
            updates.append(msg)
            return SimpleNamespace(Success=True, Error="")

        async def fake_response(msg):
            responses.append(msg)
            return SimpleNamespace(Success=True, Error="")

        async def fake_callback(msg):
            return SimpleNamespace(Success=True, Error="")

        monkeypatch.setattr(
            stop_mod,
            "MythicRPCTaskUpdateMessage",
            lambda **kwargs: SimpleNamespace(**kwargs),
        )
        monkeypatch.setattr(
            stop_mod,
            "MythicRPCResponseCreateMessage",
            lambda task_id, response: SimpleNamespace(TaskID=task_id, Response=response),
        )
        monkeypatch.setattr(
            stop_mod,
            "MythicRPCCallbackUpdateMessage",
            lambda **kwargs: SimpleNamespace(**kwargs),
        )
        monkeypatch.setattr(stop_mod, "SendMythicRPCTaskUpdate", fake_update)
        monkeypatch.setattr(stop_mod, "SendMythicRPCResponseCreate", fake_response)
        monkeypatch.setattr(stop_mod, "SendMythicRPCCallbackUpdate", fake_callback)

        task_data = SimpleNamespace(
            Task=SimpleNamespace(ID=999),
            args=_Args(),
        )
        command = stop_mod.StopCommand.__new__(stop_mod.StopCommand)
        result = await command.create_go_tasking(task_data)

        assert result.Success is True
        assert result.Completed is True
        assert fake.stopped is True
        assert "101" not in model_mod.sessions
        stopped_updates = [
            msg for msg in updates
            if getattr(msg, "TaskID", None) == 101 and getattr(msg, "UpdateStatus", None) == "stopped"
        ]
        assert stopped_updates
        assert getattr(stopped_updates[0], "UpdateCompleted", False) is True
        rendered = "\n".join(str(getattr(msg, "Response", b"")) for msg in responses)
        assert "Stop requested" in rendered or responses

    asyncio.run(_case())


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
