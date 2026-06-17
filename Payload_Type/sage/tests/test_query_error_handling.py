import sys
from pathlib import Path
from types import SimpleNamespace
import asyncio

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))  # Payload_Type/sage
from ai.langgraph import model as model_mod
from container.agent_functions import query as query_mod


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

    async def fake_bloodhound_connected():
        return True, "ok"

    monkeypatch.setattr(query_mod, "Model", _FailingModel)
    monkeypatch.setattr(query_mod, "ensure_bloodhound_connected", fake_bloodhound_connected)
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
