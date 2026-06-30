import sys
from pathlib import Path
from types import SimpleNamespace
import asyncio

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))  # Payload_Type/sage
from ai.langgraph import model as model_mod
from ai import bloodhound_config as bloodhound_config_mod
from container.agent_functions import chat as chat_mod
from container.agent_functions import query as query_mod
from container.agent_functions import utils as utils_mod


class _Args:
    def __init__(self, values):
        self._values = values

    def get_arg(self, key):
        return self._values.get(key)


class _Secrets:
    def get(self, key):
        return None


class _BuildParam:
    def __init__(self, name, value):
        self.Name = name
        self.Value = value


class _FailingModel:
    def __init__(self, **kwargs):
        self.kwargs = kwargs
        self.provider = kwargs.get("provider")
        self.model = kwargs.get("model")
        self._stop_requested = False
        self.command_name = None
        self.task_display_id = None

    async def initialize(self):
        return None

    def set_verbose(self, enabled):
        self.verbose = enabled

    async def invoke(self, prompt):
        raise RuntimeError("provider 404")


def test_query_provider_exception_completes_task_without_crashing(monkeypatch):
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

    async def fake_bloodhound_preflight(task_id):
        assert task_id == 123
        return True, "ok"

    monkeypatch.setattr(query_mod, "Model", _FailingModel)
    monkeypatch.setattr(query_mod, "ensure_bloodhound_task_preflight", fake_bloodhound_preflight)
    monkeypatch.setattr(query_mod, "SendMythicRPCTaskUpdate", fake_update)
    monkeypatch.setattr(query_mod, "SendMythicRPCResponseCreate", fake_response)
    monkeypatch.setattr(query_mod, "SendMythicRPCCallbackUpdate", fake_callback)
    monkeypatch.setattr(
        query_mod,
        "MythicRPCTaskUpdateMessage",
        lambda **kwargs: SimpleNamespace(**kwargs),
    )
    monkeypatch.setattr(
        query_mod,
        "MythicRPCResponseCreateMessage",
        lambda task_id, response: SimpleNamespace(TaskID=task_id, Response=response),
    )
    monkeypatch.setattr(
        query_mod,
        "MythicRPCCallbackUpdateMessage",
        lambda **kwargs: SimpleNamespace(**kwargs),
    )

    task_data = SimpleNamespace(
        Task=SimpleNamespace(ID=123, AgentTaskID="agent-task", DisplayID=456),
        args=_Args({
            "prompt": "continue",
            "verbose": True,
            "autonomous_solve": True,
            "mode": "auto",
            "max_steps": 3,
        }),
        Secrets=_Secrets(),
        BuildParameters=[
            _BuildParam("provider", "OpenAI"),
            _BuildParam("model", "gpt-test"),
            _BuildParam("API_ENDPOINT", "http://127.0.0.1:8100/v1"),
            _BuildParam("API_KEY", "dummy-key"),
        ],
    )

    async def _case():
        command = query_mod.QueryCommand.__new__(query_mod.QueryCommand)
        return await command.create_go_tasking(task_data)

    result = asyncio.run(_case())

    assert result.Success is True
    assert result.Completed is True
    assert result.TaskStatus == "error"
    assert "123" not in model_mod.sessions

    assert any(
        getattr(msg, "TaskID", None) == 123
        and getattr(msg, "UpdateStatus", None) == "LLM Processing..."
        for msg in updates
    )
    error_updates = [
        msg for msg in updates
        if getattr(msg, "TaskID", None) == 123
        and getattr(msg, "UpdateStatus", None) == "error"
    ]
    assert error_updates
    assert getattr(error_updates[-1], "UpdateCompleted", False) is True

    rendered = b"\n".join(getattr(msg, "Response", b"") for msg in responses).decode()
    assert "RuntimeError: provider 404" in rendered


def test_chat_runs_bloodhound_preflight_before_model_initialize(monkeypatch):
    events = []

    class _ChatModel:
        def __init__(self, **kwargs):
            self.kwargs = kwargs
            self.provider = kwargs.get("provider")
            self.model = kwargs.get("model")
            self._stop_requested = False
            self.command_name = None
            self.task_display_id = None

        async def initialize(self):
            events.append("initialize")
            assert events == ["preflight", "initialize"]

        def set_verbose(self, enabled):
            self.verbose = enabled

        async def invoke(self, prompt, is_interactive=False):
            events.append("invoke")
            assert prompt == "continue"
            assert is_interactive is False

    async def fake_preflight(task_id):
        assert task_id == 321
        events.append("preflight")
        return True, "ok"

    async def fake_update(msg):
        return SimpleNamespace(Success=True, Error="")

    async def fake_response(msg):
        return SimpleNamespace(Success=True, Error="")

    async def fake_callback(msg):
        return SimpleNamespace(Success=True, Error="")

    monkeypatch.setattr(chat_mod, "Model", _ChatModel)
    monkeypatch.setattr(chat_mod, "ensure_bloodhound_task_preflight", fake_preflight)
    monkeypatch.setattr(chat_mod, "SendMythicRPCTaskUpdate", fake_update)
    monkeypatch.setattr(chat_mod, "SendMythicRPCResponseCreate", fake_response)
    monkeypatch.setattr(chat_mod, "SendMythicRPCCallbackUpdate", fake_callback)
    monkeypatch.setattr(
        chat_mod,
        "MythicRPCTaskUpdateMessage",
        lambda **kwargs: SimpleNamespace(**kwargs),
    )
    monkeypatch.setattr(
        chat_mod,
        "MythicRPCResponseCreateMessage",
        lambda task_id, response: SimpleNamespace(TaskID=task_id, Response=response),
    )
    monkeypatch.setattr(
        chat_mod,
        "MythicRPCCallbackUpdateMessage",
        lambda **kwargs: SimpleNamespace(**kwargs),
    )

    task_data = SimpleNamespace(
        Task=SimpleNamespace(
            ID=321,
            AgentTaskID="agent-task",
            DisplayID=654,
            IsInteractiveTask=False,
        ),
        args=_Args({
            "prompt": "continue",
            "verbose": True,
            "autonomous_solve": True,
            "mode": "supervised",
            "max_steps": 3,
        }),
        Secrets=_Secrets(),
        BuildParameters=[
            _BuildParam("provider", "OpenAI"),
            _BuildParam("model", "gpt-test"),
            _BuildParam("API_ENDPOINT", "http://127.0.0.1:8100/v1"),
            _BuildParam("API_KEY", "dummy-key"),
        ],
    )

    async def _case():
        model_mod.sessions.clear()
        command = chat_mod.ChatCommand.__new__(chat_mod.ChatCommand)
        result = await command.create_go_tasking(task_data)
        model_mod.sessions.clear()
        return result

    result = asyncio.run(_case())

    assert result.Success is True
    assert events == ["preflight", "initialize", "invoke"]


def test_bloodhound_task_preflight_warns_and_fails_soft(monkeypatch):
    warnings = []

    async def fake_connect():
        return False, "not configured"

    async def fake_event(msg):
        warnings.append(msg)
        return SimpleNamespace(Success=True, Error="")

    monkeypatch.setattr(bloodhound_config_mod, "ensure_bloodhound_connected", fake_connect)
    monkeypatch.setattr(utils_mod, "SendMythicRPCOperationEventLogCreate", fake_event)
    monkeypatch.setattr(
        utils_mod,
        "MythicRPCOperationEventLogCreateMessage",
        lambda **kwargs: SimpleNamespace(**kwargs),
    )

    connected, message = asyncio.run(utils_mod.ensure_bloodhound_task_preflight(777))

    assert connected is False
    assert message == "not configured"
    assert len(warnings) == 1
    assert warnings[0].TaskID == 777
    assert warnings[0].Warning is True
    assert "could not auto-connect BloodHound" in warnings[0].Message
