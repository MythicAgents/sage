"""Unit tests for the sage_chat scaffold (Phase 1 MVP).

Covers the safety-critical always-terminal invariant, response_key discipline, cancel re-raise,
error path, the config precedence shim, the channel session key, and the streaming emitter — all
with a stubbed Model (no live LLM), per PRD Section 13.
"""

import asyncio
import copy
import json

import pytest

from sage_chat.config import build_model_kwargs
from sage_chat.models import SAGE_MODELS
from sage_chat.session import (
    bind_channel_thread_id,
    channel_session_key,
    drop_channel_session,
    get_channel_session,
    put_channel_session,
)
from sage_chat.streaming import ChatStreamEmitter
from sage_chat.headless import HeadlessSageChat, build_chat_request


def _run(coro):
    return asyncio.new_event_loop().run_until_complete(coro)


class _FakeModel:
    """Stand-in for ai.langgraph.model.Model — records invoke() and drives the emitter."""

    provider = "test"
    model = "test"

    def __init__(self, behavior="ok", stream=("🤖[Agent]> hi",), return_value="done"):
        self._response_emitter = None
        self._thread_id_override = None
        self.behavior = behavior
        self.stream = stream
        self.return_value = return_value
        self.stop_called = False
        self.closed_delegation_statuses = []
        self.invoked_with = None
        self.installed_request_contracts = []

    def request_stop(self):
        self.stop_called = True

    async def _close_all_delegations(self, status="finished"):
        self.closed_delegation_statuses.append(status)

    async def _hitl_interrupt_pending(self, thread_id):
        return False

    def install_request_contract(self, contract):
        self._request_contract = contract
        self.installed_request_contracts.append(contract)

    async def invoke(self, prompt, is_interactive=False):
        self.invoked_with = (prompt, is_interactive)
        for block in self.stream:
            await self._response_emitter(block)
        if self.behavior == "cancel":
            raise asyncio.CancelledError()
        if self.behavior == "error":
            raise RuntimeError("boom")
        return self.return_value


class _DriverChat(HeadlessSageChat):
    """HeadlessSageChat with _get_or_create_model stubbed to a _FakeModel."""

    def __init__(self, model, preexisted=False):
        super().__init__()
        self._model = model
        self._preexisted = preexisted

    async def _get_or_create_model(self, request):
        return self._model, self._preexisted


# --------------------------------------------------------------------------------------
# Always-terminal + response_key discipline
# --------------------------------------------------------------------------------------

def test_happy_path_emits_exactly_one_terminal():
    model = _FakeModel(stream=("🤖> one", "🛠️> two"))
    chat = _DriverChat(model)
    req = build_chat_request("do the thing", channel_id=5, request_id=9)
    _run(chat.chat(req))

    terminals = chat.terminal_emissions
    assert len(terminals) == 1, f"expected exactly one terminal, got {chat.emissions}"
    assert terminals[0]["kind"] == "complete"
    # prompt threaded through, first turn → is_interactive False
    assert model.invoked_with == ("do the thing", False)


def test_native_service_reconciles_one_typed_terminal_after_request_terminal():
    from ai.langgraph.request_events import RequestEventLedger, stable_event_id

    class _LifecycleModel(_FakeModel):
        def request_event_transcript(self):
            return self.ledger.reconstruct_transcript()

        def request_control_transitions(self):
            return [
                row
                for row in self.request_event_transcript()
                if row["kind"] == "control_transition"
            ]

        def begin_visibility_turn(
            self,
            _scope,
            *,
            operator_prompt,
            native_request_id,
            logical_request_id,
        ):
            self.ledger = RequestEventLedger(logical_request_id)
            self.ledger.record(
                event_id=stable_event_id(
                    logical_request_id,
                    "operator_input",
                    native_request_id,
                ),
                kind="operator_input",
                phase="received",
                content=operator_prompt,
            )

        def install_request_contract(self, contract):
            super().install_request_contract(contract)
            self.ledger.record(
                event_id=stable_event_id(
                    contract.request_id,
                    "control_transition",
                    "contract",
                ),
                kind="control_transition",
                phase="request_installed",
                content="request contract installed",
            )

        async def finalize_visibility_turn(self, *, require_final):
            return self.ledger.reconcile(require_final=require_final)

        def record_request_terminal(self, status):
            self.ledger.record(
                event_id=stable_event_id(
                    self.ledger.request_id,
                    "control_transition",
                    "terminal",
                ),
                kind="control_transition",
                phase="request_terminal",
                content=status,
            )

        def record_final_response(self, content, *, response_key):
            event_id = stable_event_id(
                self.ledger.request_id,
                "final_response",
                "terminal",
            )
            self.ledger.record(
                event_id=event_id,
                kind="final_response",
                phase="emitted",
                content=content,
            )
            return event_id

        def record_final_response_projection(self, event_id, *, response_key):
            self.ledger.record_projection(
                event_id=event_id,
                kind="final_response",
                phase="emitted",
                projection_key=response_key,
            )

    model = _LifecycleModel(stream=(), return_value="done")
    chat = _DriverChat(model)
    request = build_chat_request(
        "exact operator prompt",
        channel_id=5,
        request_id=91,
    )

    _run(chat.chat(request))

    assert len(chat.terminal_emissions) == 1
    report = model.ledger.reconcile()
    assert report["ok"] is True
    transcript = model.ledger.reconstruct_transcript()
    assert transcript[0]["kind"] == "operator_input"
    assert transcript[0]["content"] == "exact operator prompt"
    assert transcript[-2]["phase"] == "request_terminal"
    assert transcript[-1]["kind"] == "final_response"
    assert chat.terminal_emissions[0]["metadata"]["event_id"] == transcript[-1]["event_id"]
    assert [
        row["phase"]
        for row in chat.terminal_emissions[0]["metadata"]["control_transitions"]
    ] == ["request_installed", "request_terminal"]

    error_model = _LifecycleModel(behavior="error", stream=())
    error_chat = _DriverChat(error_model)
    _run(error_chat.chat(build_chat_request(
        "failing operator prompt",
        channel_id=5,
        request_id=92,
    )))
    assert len(error_chat.terminal_emissions) == 1
    assert error_chat.terminal_emissions[0]["kind"] == "error"
    error_report = error_model.ledger.reconcile()
    assert error_report["ok"] is True, error_report
    error_transcript = error_model.ledger.reconstruct_transcript()
    assert error_transcript[-2]["content"] == "error"
    assert error_transcript[-1]["content"] == "boom"


def test_same_channel_turns_are_serialized():
    class _SerialModel(_FakeModel):
        def __init__(self):
            super().__init__(stream=())
            self.active = 0
            self.max_active = 0
            self.prompts = []

        async def invoke(self, prompt, is_interactive=False):
            self.active += 1
            self.max_active = max(self.max_active, self.active)
            self.prompts.append(prompt)
            try:
                await asyncio.sleep(0.02)
                await self._response_emitter(f"done:{prompt}")
            finally:
                self.active -= 1
            return "done"

    async def scenario():
        model = _SerialModel()
        chat = _DriverChat(model, preexisted=True)
        first = build_chat_request("first", channel_id=105, request_id=1)
        second = build_chat_request("second", channel_id=105, request_id=2)
        await asyncio.gather(chat.chat(first), chat.chat(second))
        return model

    model = _run(scenario())
    assert model.max_active == 1
    assert model.prompts == ["first", "second"]


def test_response_key_discipline():
    model = _FakeModel(stream=("block-a", "block-b"), return_value="")
    chat = _DriverChat(model)
    req = build_chat_request("hi", channel_id=5, request_id=9)
    _run(chat.chat(req))

    texts = [e for e in chat.emissions if e["kind"] == "text"]
    assert [t["response_key"] for t in texts] == ["assistant:9:1", "assistant:9:2"]
    complete = [e for e in chat.emissions if e["kind"] == "complete"][0]
    assert complete["response_key"] == "assistant:9:2"
    assert complete["content"] == "block-b"
    assert complete["complete_request"] is True


def test_preexisting_session_is_interactive():
    model = _FakeModel()
    chat = _DriverChat(model, preexisted=True)
    _run(chat.chat(build_chat_request("second turn", channel_id=5, request_id=10)))
    assert model.invoked_with[1] is True


def test_native_chat_marks_model_command_name_chat():
    model = _FakeModel()
    chat = _DriverChat(model)
    _run(chat.chat(build_chat_request("first turn", channel_id=5, request_id=10)))
    assert model.command_name == "chat"


def test_native_chat_installs_one_typed_contract_before_invoke():
    model = _FakeModel()
    model.mode = "supervised"
    chat = _DriverChat(model)
    request = build_chat_request(
        "same prose",
        channel_id=5,
        operation_id=8,
        request_id=10,
        config={"mode": "supervised"},
    )
    _run(chat.chat(request))

    assert len(model.installed_request_contracts) == 1
    contract = model.installed_request_contracts[0]
    assert contract.request_id == "chat:5:request:10"
    assert contract.scope.operation_id == "8"
    assert contract.scope.channel_id == "5"
    assert contract.lane.value == "supervised_workflow"
    assert model.invoked_with == ("same prose", False)


def test_identical_prompt_uses_typed_mode_not_prose_for_lane():
    supervised_model = _FakeModel()
    supervised_model.mode = "supervised"
    auto_model = _FakeModel()
    auto_model.mode = "auto"
    auto_model._autonomous_solve = True
    prompt = "Explain the current objective."

    _run(_DriverChat(supervised_model).chat(build_chat_request(
        prompt,
        channel_id=51,
        operation_id=8,
        request_id=1,
        config={"mode": "supervised"},
    )))
    _run(_DriverChat(auto_model).chat(build_chat_request(
        prompt,
        channel_id=52,
        operation_id=8,
        request_id=1,
        config={"mode": "auto"},
    )))

    assert supervised_model.installed_request_contracts[0].lane.value == "supervised_workflow"
    assert auto_model.installed_request_contracts[0].lane.value == "autonomous_objective"


def test_autonomous_native_chat_fails_closed_without_exact_bloodhound_tools(monkeypatch):
    from ai import bloodhound_config
    from sage_chat.service import SageChat

    async def _ensure(**_kwargs):
        return True, "connected"

    monkeypatch.setattr(bloodhound_config, "ensure_bloodhound_connected", _ensure)
    monkeypatch.setattr(
        bloodhound_config,
        "bloodhound_tool_admission",
        lambda: {
            "ready": False,
            "reason": "BloodHound MCP missing exact tools: cypher_query.",
        },
    )

    class _Model:
        def __init__(self, **_kwargs):
            self.initialized = False

        async def initialize(self):
            self.initialized = True

        def set_verbose(self, _value):
            return None

    monkeypatch.setattr("ai.langgraph.model.Model", _Model)
    chat = SageChat()

    with pytest.raises(RuntimeError, match="requires BloodHound MCP exact-tool admission"):
        _run(
            chat._get_or_create_model(
                build_chat_request(
                    "objective",
                    channel_id=801,
                    request_id=1,
                    config={"autonomous_solve": "true"},
                )
            )
        )


def test_supervised_native_chat_keeps_bloodhound_fail_soft(monkeypatch):
    from ai import bloodhound_config
    from sage_chat.service import SageChat

    events = []

    async def _ensure(**_kwargs):
        return False, "missing config"

    monkeypatch.setattr(bloodhound_config, "ensure_bloodhound_connected", _ensure)
    monkeypatch.setattr(
        bloodhound_config,
        "bloodhound_tool_admission",
        lambda: {"ready": False, "reason": "not used for supervised mode"},
    )

    class _Model:
        def __init__(self, **_kwargs):
            self.provider = "openai"
            self.model = "test-model"

        async def initialize(self):
            events.append("initialize")

        def set_verbose(self, _value):
            return None

    monkeypatch.setattr("ai.langgraph.model.Model", _Model)
    chat = SageChat()

    _run(
        chat._get_or_create_model(
            build_chat_request(
                "inspect",
                channel_id=802,
                request_id=1,
                config={"mode": "supervised", "autonomous_solve": "false"},
            )
        )
    )

    assert events == ["initialize"]


def test_autonomous_native_chat_initializes_after_exact_bloodhound_admission(monkeypatch):
    from ai import bloodhound_config
    from sage_chat.service import SageChat

    events = []

    async def _ensure(**_kwargs):
        events.append("connect")
        return True, "connected"

    monkeypatch.setattr(bloodhound_config, "ensure_bloodhound_connected", _ensure)
    monkeypatch.setattr(
        bloodhound_config,
        "bloodhound_tool_admission",
        lambda: {
            "ready": True,
            "server": "BloodHound",
            "reason": "BloodHound MCP exposes the required exact tools.",
        },
    )
    monkeypatch.setattr(
        SageChat,
        "_bloodhound_connection_locally_pinned",
        staticmethod(lambda _server: True),
    )

    class _Model:
        def __init__(self, **_kwargs):
            self.provider = "openai"
            self.model = "test-model"

        async def initialize(self):
            events.append("initialize")

        def set_verbose(self, _value):
            return None

    monkeypatch.setattr("ai.langgraph.model.Model", _Model)
    chat = SageChat()

    _run(
        chat._get_or_create_model(
            build_chat_request(
                "objective",
                channel_id=803,
                request_id=1,
                config={"mode": "auto"},
            )
        )
    )

    assert events == ["connect", "initialize"]


def test_bloodhound_tool_admission_requires_exact_names(monkeypatch):
    from ai import bloodhound_config

    class _Tool:
        def __init__(self, name):
            self.name = name

    monkeypatch.setattr(
        bloodhound_config.MCPManager,
        "get_connected_servers",
        lambda: ["BloodHound"],
    )
    monkeypatch.setattr(
        bloodhound_config.MCPManager,
        "is_bloodhound_server",
        lambda server: server == "BloodHound",
    )
    monkeypatch.setattr(
        bloodhound_config.MCPManager,
        "get_tools_by_server",
        lambda _server: [_Tool("file_upload"), _Tool("domain_info"), _Tool("cypher-query")],
    )

    admission = bloodhound_config.bloodhound_tool_admission()

    assert admission["ready"] is False
    assert admission["missing_tools"] == ["cypher_query"]


def test_bloodhound_tool_admission_rejects_whitespace_near_match(monkeypatch):
    from ai import bloodhound_config

    class _Tool:
        def __init__(self, name):
            self.name = name

    monkeypatch.setattr(
        bloodhound_config.MCPManager,
        "get_connected_servers",
        lambda: ["BloodHound"],
    )
    monkeypatch.setattr(
        bloodhound_config.MCPManager,
        "is_bloodhound_server",
        lambda server: server == "BloodHound",
    )
    monkeypatch.setattr(
        bloodhound_config.MCPManager,
        "get_tools_by_server",
        lambda _server: [_Tool(" file_upload"), _Tool("domain_info"), _Tool("cypher_query")],
    )

    admission = bloodhound_config.bloodhound_tool_admission()

    assert admission["ready"] is False
    assert admission["missing_tools"] == ["file_upload"]
    assert admission["invalid_tool_name_count"] == 1


def test_bloodhound_tool_admission_rejects_duplicate_exact_name(monkeypatch):
    from ai import bloodhound_config

    class _Tool:
        def __init__(self, name):
            self.name = name

    monkeypatch.setattr(
        bloodhound_config.MCPManager,
        "get_connected_servers",
        lambda: ["BloodHound"],
    )
    monkeypatch.setattr(
        bloodhound_config.MCPManager,
        "is_bloodhound_server",
        lambda server: server == "BloodHound",
    )
    monkeypatch.setattr(
        bloodhound_config.MCPManager,
        "get_tools_by_server",
        lambda _server: [
            _Tool("file_upload"),
            _Tool("file_upload"),
            _Tool("domain_info"),
            _Tool("cypher_query"),
        ],
    )

    admission = bloodhound_config.bloodhound_tool_admission()

    assert admission["ready"] is False
    assert admission["missing_tools"] == []
    assert admission["duplicate_tool_names"] == ["file_upload"]
    assert "duplicate exact tools: file_upload" in admission["reason"]


def test_bloodhound_tool_admission_rejects_invalid_unrelated_name(monkeypatch):
    from ai import bloodhound_config

    class _Tool:
        def __init__(self, name):
            self.name = name

    monkeypatch.setattr(
        bloodhound_config.MCPManager,
        "get_connected_servers",
        lambda: ["BloodHound"],
    )
    monkeypatch.setattr(
        bloodhound_config.MCPManager,
        "is_bloodhound_server",
        lambda server: server == "BloodHound",
    )
    monkeypatch.setattr(
        bloodhound_config.MCPManager,
        "get_tools_by_server",
        lambda _server: [
            _Tool("file_upload"),
            _Tool("domain_info"),
            _Tool("cypher_query"),
            _Tool(" group_info"),
        ],
    )

    admission = bloodhound_config.bloodhound_tool_admission()

    assert admission["ready"] is False
    assert admission["missing_tools"] == []
    assert admission["invalid_tool_name_count"] == 1
    assert "invalid tool names: 1" in admission["reason"]


def test_bloodhound_tool_admission_rejects_multiple_matching_servers(monkeypatch):
    from ai import bloodhound_config

    monkeypatch.setattr(
        bloodhound_config.MCPManager,
        "get_connected_servers",
        lambda: ["BloodHound", "BloodHound-Replica"],
    )
    monkeypatch.setattr(
        bloodhound_config.MCPManager,
        "is_bloodhound_server",
        lambda _server: True,
    )

    admission = bloodhound_config.bloodhound_tool_admission()

    assert admission["ready"] is False
    assert admission["matching_server_count"] == 2
    assert admission["server"] is None
    assert admission["matching_servers"] == ["BloodHound", "BloodHound-Replica"]


def test_new_model_records_exact_admission_state(monkeypatch):
    from ai import bloodhound_config
    from sage_chat.service import SageChat

    async def _ensure(**_kwargs):
        return True, "connected"

    monkeypatch.setattr(bloodhound_config, "ensure_bloodhound_connected", _ensure)
    monkeypatch.setattr(
        bloodhound_config,
        "bloodhound_tool_admission",
        lambda: {"ready": True, "server": "BloodHound", "reason": "ready"},
    )
    monkeypatch.setattr(
        SageChat,
        "_bloodhound_connection_locally_pinned",
        staticmethod(lambda _server: True),
    )

    class _Model:
        def __init__(self, **_kwargs):
            self.provider = "openai"
            self.model = "test-model"
            self.mode = "supervised"
            self._autonomous_solve = False

        async def initialize(self):
            return None

        def set_verbose(self, _value):
            return None

    monkeypatch.setattr("ai.langgraph.model.Model", _Model)
    chat = SageChat()

    model, preexisted = _run(
        chat._get_or_create_model(
            build_chat_request("inspect", channel_id=804, request_id=1)
        )
    )

    assert preexisted is False
    assert model._bloodhound_exact_admission_at_initialize is True


def test_reused_auto_session_is_recreated_for_current_supervised_request(monkeypatch):
    import sage_chat.service as service

    request = build_chat_request("objective", channel_id=805, request_id=1)
    prior_request = build_chat_request(
        "prior objective",
        channel_id=805,
        request_id=0,
        config={"mode": "auto", "autonomous_solve": "true"},
    )
    stopped = []
    dropped = []
    created = []

    class _Existing:
        mode = "auto"
        _autonomous_solve = True
        policy_mode = "hybrid"
        _max_steps = 200
        _bloodhound_exact_admission_at_initialize = False
        apitoken_id = request.APITokenID
        operation_id = request.OperationID

        def request_stop(self):
            stopped.append(True)

    existing = _Existing()
    existing._chat_request_config_signature = service._model_config_signature(
        service.build_model_kwargs(prior_request)
    )

    async def _get_existing(_request):
        return existing

    async def _drop(_request, *, expected_model=None):
        dropped.append(expected_model)
        return True

    async def _put(_request, model):
        created.append(model)

    async def _ensure(_self, *, autonomous_required=False, **_kwargs):
        assert autonomous_required is False
        return False

    class _Model:
        def __init__(self, **kwargs):
            created.append(kwargs)
            self.mode = kwargs["mode"]
            self._autonomous_solve = kwargs["autonomous_solve"]
            self.policy_mode = kwargs["policy_mode"]
            self._max_steps = kwargs["max_steps"]
            self.apitoken_id = kwargs["apitoken_id"]
            self.operation_id = kwargs["operation_id"]

        async def initialize(self):
            return None

        def set_verbose(self, _value):
            return None

    monkeypatch.setattr(service, "get_channel_session", _get_existing)
    monkeypatch.setattr(service, "drop_channel_session", _drop)
    monkeypatch.setattr(service, "put_channel_session", _put)
    monkeypatch.setattr(service.SageChat, "_ensure_bloodhound_connected", _ensure)
    monkeypatch.setattr("ai.langgraph.model.Model", _Model)

    replacement, preexisted = _run(service.SageChat()._get_or_create_model(request))

    assert replacement is not existing
    assert preexisted is False
    assert stopped == [True]
    assert dropped == [existing]
    assert replacement.mode == "conversation"
    assert replacement._autonomous_solve is False


def test_reused_session_with_identical_resolved_config_is_preserved(monkeypatch):
    import sage_chat.service as service

    request = build_chat_request("inspect", channel_id=806, request_id=1)
    kwargs = service.build_model_kwargs(request)

    class _Existing:
        mode = kwargs["mode"]
        _autonomous_solve = kwargs["autonomous_solve"]
        policy_mode = kwargs["policy_mode"]
        _max_steps = kwargs["max_steps"]
        _bloodhound_exact_admission_at_initialize = False
        apitoken_id = request.APITokenID
        operation_id = request.OperationID
        _chat_request_config_signature = service._model_config_signature(kwargs)

    existing = _Existing()

    async def _get_existing(_request):
        return existing

    async def _unexpected_drop(*_args, **_kwargs):
        raise AssertionError("identical config must not rotate the session")

    monkeypatch.setattr(service, "get_channel_session", _get_existing)
    monkeypatch.setattr(service, "drop_channel_session", _unexpected_drop)

    reused, preexisted = _run(service.SageChat()._get_or_create_model(request))

    assert reused is existing
    assert preexisted is True


@pytest.mark.parametrize(
    ("config", "override_mode", "expected_autonomy"),
    (
        ({"mode": "supervised", "autonomous_solve": "false"}, "auto", True),
        ({"mode": "auto", "autonomous_solve": "true"}, "supervised", True),
    ),
)
def test_slash_mode_override_reuses_unchanged_base_request_with_bound_autonomy(
    monkeypatch,
    config,
    override_mode,
    expected_autonomy,
):
    import sage_chat.service as service
    from sage_chat.slash import _handle_mode

    request = build_chat_request("next turn", channel_id=807, request_id=2, config=config)
    kwargs = service.build_model_kwargs(request)
    signature = service._model_config_signature(kwargs)
    ensure_calls = []

    class _Existing:
        mode = kwargs["mode"]
        _autonomous_solve = kwargs["autonomous_solve"]
        policy_mode = kwargs["policy_mode"]
        _max_steps = kwargs["max_steps"]
        _bloodhound_exact_admission_at_initialize = True
        apitoken_id = request.APITokenID
        operation_id = request.OperationID
        _chat_request_config_signature = signature
        _chat_request_base_autonomous_solve = kwargs["autonomous_solve"]

    existing = _Existing()
    assert "Mode set" in _handle_mode(existing, override_mode)

    async def _get_existing(_request):
        return existing

    async def _unexpected_drop(*_args, **_kwargs):
        raise AssertionError("an override bound to unchanged base config must not rotate")

    async def _ensure(_self, *, autonomous_required=False, **_kwargs):
        ensure_calls.append(autonomous_required)
        return True

    monkeypatch.setattr(service, "get_channel_session", _get_existing)
    monkeypatch.setattr(service, "drop_channel_session", _unexpected_drop)
    monkeypatch.setattr(service.SageChat, "_ensure_bloodhound_connected", _ensure)

    reused, preexisted = _run(service.SageChat()._get_or_create_model(request))

    assert reused is existing
    assert preexisted is True
    assert reused.mode == override_mode
    assert reused._autonomous_solve is expected_autonomy
    assert reused._chat_mode_override == override_mode
    assert reused._chat_mode_override_base_autonomous_solve is kwargs["autonomous_solve"]
    assert ensure_calls == ([True] if expected_autonomy else [])


def test_slash_supervised_restores_bound_base_autonomy_after_auto_override():
    from sage_chat.slash import _handle_mode

    class _Existing:
        mode = "supervised"
        _autonomous_solve = False
        _chat_request_config_signature = "base-signature"
        _chat_request_base_autonomous_solve = False

    existing = _Existing()
    assert "Mode set" in _handle_mode(existing, "auto")
    assert existing.mode == "auto"
    assert existing._autonomous_solve is True

    assert "Mode set" in _handle_mode(existing, "supervised")
    assert existing.mode == "supervised"
    assert existing._autonomous_solve is False
    assert existing._chat_mode_override_base_signature == "base-signature"
    assert existing._chat_mode_override_base_autonomous_solve is False


def test_slash_conversation_is_exact_and_disables_bound_autonomy():
    from sage_chat.slash import _handle_mode

    class _Existing:
        mode = "auto"
        _autonomous_solve = True
        _chat_request_config_signature = "base-signature"
        _chat_request_base_autonomous_solve = True

    existing = _Existing()
    assert "Mode set" in _handle_mode(existing, "conversation")
    assert existing.mode == "conversation"
    assert existing._autonomous_solve is False
    assert "conversation" in _handle_mode(existing, "")
    assert "Valid: `conversation`, `supervised`, `auto`" in _handle_mode(
        existing,
        "execute",
    )


def test_base_request_config_change_rotates_session_and_clears_slash_mode_override(monkeypatch):
    import sage_chat.service as service
    from sage_chat.slash import _handle_mode

    prior_request = build_chat_request("prior", channel_id=808, request_id=1)
    current_request = build_chat_request(
        "current",
        channel_id=808,
        request_id=2,
        config={"max_steps": "201"},
    )
    prior_kwargs = service.build_model_kwargs(prior_request)
    stopped = []
    dropped = []

    class _Existing:
        mode = prior_kwargs["mode"]
        _autonomous_solve = prior_kwargs["autonomous_solve"]
        policy_mode = prior_kwargs["policy_mode"]
        _max_steps = prior_kwargs["max_steps"]
        _bloodhound_exact_admission_at_initialize = False
        apitoken_id = current_request.APITokenID
        operation_id = current_request.OperationID
        _chat_request_config_signature = service._model_config_signature(prior_kwargs)

        def request_stop(self):
            stopped.append(True)

    existing = _Existing()
    _handle_mode(existing, "auto")

    async def _get_existing(_request):
        return existing

    async def _drop(_request, *, expected_model=None):
        dropped.append(expected_model)
        return True

    async def _put(_request, _model):
        return None

    async def _ensure(_self, *, autonomous_required=False, **_kwargs):
        assert autonomous_required is False
        return False

    class _Model:
        def __init__(self, **kwargs):
            self.mode = kwargs["mode"]
            self._autonomous_solve = kwargs["autonomous_solve"]
            self.policy_mode = kwargs["policy_mode"]
            self._max_steps = kwargs["max_steps"]
            self.apitoken_id = kwargs["apitoken_id"]
            self.operation_id = kwargs["operation_id"]

        async def initialize(self):
            return None

        def set_verbose(self, _value):
            return None

    monkeypatch.setattr(service, "get_channel_session", _get_existing)
    monkeypatch.setattr(service, "drop_channel_session", _drop)
    monkeypatch.setattr(service, "put_channel_session", _put)
    monkeypatch.setattr(service.SageChat, "_ensure_bloodhound_connected", _ensure)
    monkeypatch.setattr("ai.langgraph.model.Model", _Model)

    replacement, preexisted = _run(service.SageChat()._get_or_create_model(current_request))

    assert replacement is not existing
    assert preexisted is False
    assert stopped == [True]
    assert dropped == [existing]
    assert replacement.mode == "conversation"
    assert replacement._chat_mode_override == ""
    assert replacement._chat_mode_override_base_signature == ""
    assert replacement._chat_mode_override_base_autonomous_solve is None
    assert replacement._chat_request_config_signature == service._model_config_signature(
        service.build_model_kwargs(current_request)
    )


def test_channel_metadata_publishes_before_model_invoke():
    class _MetadataAwareModel(_FakeModel):
        async def invoke(self, prompt, is_interactive=False):
            assert chat.channel_metadata_updates
            return await super().invoke(prompt, is_interactive=is_interactive)

    model = _MetadataAwareModel()
    chat = _DriverChat(model)
    _run(chat.chat(build_chat_request("first turn", channel_id=5, request_id=10)))


def test_channel_metadata_heartbeat_publishes_changed_rounds(monkeypatch):
    import sage_chat.service as service

    class _LongModel(_FakeModel):
        _global_step_count = 0
        _policy_model_calls = 0

        async def invoke(self, prompt, is_interactive=False):
            self._policy_model_calls = 1
            await asyncio.sleep(0.03)
            self._policy_model_calls = 2
            await asyncio.sleep(0.03)
            return "done"

    monkeypatch.setattr(service, "_CHANNEL_METADATA_HEARTBEAT_SECONDS", 0.01)
    chat = _DriverChat(_LongModel())
    _run(chat.chat(build_chat_request("long turn", channel_id=5, request_id=10)))

    rounds = [
        next(item["value"] for item in update["items"] if item["key"] == "rounds")
        for update in chat.channel_metadata_updates
    ]
    assert rounds[0] == 0
    assert 1 in rounds
    assert rounds[-1] == 2


def test_channel_metadata_tracks_active_agent_then_returns_idle(monkeypatch):
    import sage_chat.service as service

    class _ActivityModel(_FakeModel):
        _global_step_count = 0
        _policy_model_calls = 0

        def set_active_agent(self, name):
            self._active_agent_label = name

        async def invoke(self, prompt, is_interactive=False):
            self.set_active_agent("Mythic Operator")
            await asyncio.sleep(0.03)
            return "done"

    monkeypatch.setattr(service, "_CHANNEL_METADATA_HEARTBEAT_SECONDS", 0.01)
    chat = _DriverChat(_ActivityModel(stream=()))
    _run(chat.chat(build_chat_request("inspect", channel_id=5, request_id=10)))

    agents = [
        next(item["value"] for item in update["items"] if item["key"] == "active_agent")
        for update in chat.channel_metadata_updates
    ]
    assert agents[0] == "Supervisor"
    assert "Mythic Operator" in agents
    assert agents[-1] == "Idle"


# --------------------------------------------------------------------------------------
# Cancel + error paths
# --------------------------------------------------------------------------------------

def test_cancel_reraises_and_cooperatively_stops():
    model = _FakeModel(behavior="cancel")
    chat = _DriverChat(model)
    req = build_chat_request("go", channel_id=505, request_id=11)
    _run(put_channel_session(req, model))
    with pytest.raises(asyncio.CancelledError):
        _run(chat.chat(req))
    # cooperative stop fired; the SDK (not us) emits the cancelled terminal, so we must NOT have.
    assert model.stop_called is True
    assert chat.terminal_emissions == []
    assert _run(get_channel_session(req)) is None


def test_handler_exception_emits_one_error_terminal():
    model = _FakeModel(behavior="error")
    chat = _DriverChat(model)
    req = build_chat_request("go", channel_id=5, request_id=12)
    _run(chat.chat(req))  # run_chat_turn swallows the exception into send_error
    errors = [e for e in chat.emissions if e["kind"] == "error"]
    assert len(errors) == 1
    assert errors[0]["complete_request"] is True
    assert len(chat.terminal_emissions) == 1
    assert model.closed_delegation_statuses == ["error"]


# --------------------------------------------------------------------------------------
# Streaming emitter
# --------------------------------------------------------------------------------------

def test_emitter_skips_empty_and_increments_blocks():
    sent = []

    class _Rec:
        async def send_text(self, request, response_key, content="", metadata=None):
            sent.append((response_key, content))

    req = build_chat_request("x", request_id=3)
    emitter = ChatStreamEmitter(_Rec(), req)
    assert _run(emitter("")) is False       # empty → no send
    assert _run(emitter("a")) is True
    assert _run(emitter("b")) is True
    assert sent == [("assistant:3:1", "a"), ("assistant:3:2", "b")]
    assert emitter.last_response_key == "assistant:3:2"
    assert emitter.last_content == "b"


def test_no_assistant_output_preserves_nonempty_native_return_text():
    chat = _DriverChat(_FakeModel(stream=()))
    _run(chat.chat(build_chat_request("quiet turn", channel_id=5, request_id=13)))

    assert chat.terminal_emissions == [{
        "kind": "complete",
        "response_key": "assistant:13:turn",
        "content": "done",
        "metadata": {"channel_id": 5},
        "complete_request": True,
    }]


def test_native_return_replaces_last_stream_block_as_the_single_terminal():
    chat = _DriverChat(
        _FakeModel(
            stream=("prompt echo", "progress update"),
            return_value="distinct terminal report",
        )
    )
    _run(chat.chat(build_chat_request("objective", channel_id=5, request_id=131)))

    assert chat.terminal_emissions == [{
        "kind": "complete",
        "response_key": "assistant:131:2",
        "content": "distinct terminal report",
        "metadata": {"channel_id": 5},
        "complete_request": True,
    }]


def test_no_assistant_output_uses_nonempty_terminal_fallback_when_native_return_is_blank():
    chat = _DriverChat(_FakeModel(stream=(), return_value="   "))
    _run(chat.chat(build_chat_request("quiet turn", channel_id=5, request_id=130)))

    assert chat.terminal_emissions == [{
        "kind": "complete",
        "response_key": "assistant:130:turn",
        "content": "Completed.",
        "metadata": {"channel_id": 5},
        "complete_request": True,
    }]


def test_terminal_message_carries_observed_runtime_telemetry():
    class _TelemetryModel(_FakeModel):
        def controller_runtime_telemetry(self):
            return {
                "policy_mode": "llm",
                "model_calls": 2,
                "semantic_transaction_count": 2,
                "authorized_transaction_count": 2,
                "semantic_policy_coverage": 1.0,
            }

    chat = _DriverChat(_TelemetryModel(stream=("final",)))
    _run(chat.chat(build_chat_request("objective", channel_id=5, request_id=14)))

    terminal = chat.terminal_emissions[0]
    assert terminal["response_key"] == "assistant:14:1"
    assert terminal["metadata"]["runtime_telemetry"]["policy_mode"] == "llm"
    assert terminal["metadata"]["runtime_telemetry"]["semantic_policy_coverage"] == 1.0


def test_emit_tool_use_produces_tool_use_card():
    chat = HeadlessSageChat()
    req = build_chat_request("x")
    emitter = ChatStreamEmitter(chat, req)

    assert _run(emitter.emit_tool_use(
        tool_call_id="call_1",
        tool_name="search_credentials",
        tool_source="mythic",
        status="started",
        content="Using Mythic tool `search_credentials`...",
        complete=False,
        arguments_present=True,
    )) is True

    assert len(chat.emissions) == 1
    emitted = chat.emissions[0]
    assert emitted["response_key"] == "tool_use:call_1:search_credentials"
    assert emitted["metadata"]["special_type"] == "tool_use"
    tool_use = emitted["metadata"]["tool_use"]
    assert tool_use["status"] == "started"
    assert tool_use["tool_name"] == "search_credentials"
    assert tool_use["tool_source"] == "mythic"
    assert tool_use["arguments_present"] is True
    assert "delegation_id" not in emitted["metadata"]
    assert "delegation_name" not in emitted["metadata"]
    assert "delegation_id" not in tool_use
    assert "delegation_name" not in tool_use


def test_emit_tool_use_ships_full_output_lazily():
    """Large result: result_preview holds a short preview; tool_use.output carries the full raw result
    (Mythic serves that lazily via 'View output', so it never inflates the chat message)."""
    chat = HeadlessSageChat()
    emitter = ChatStreamEmitter(chat, build_chat_request("x"))
    assert _run(emitter.emit_tool_use(
        tool_call_id="call_9", tool_name="get_task_history_for_callback", tool_source="mythic",
        status="completed", content="finished", complete=True,
        result_preview="preview…[View output]", output="FULL RAW RESULT " * 500,
    )) is True
    tool_use = chat.emissions[-1]["metadata"]["tool_use"]
    assert tool_use["result_preview"] == "preview…[View output]"
    assert tool_use["output"].startswith("FULL RAW RESULT")


def test_emit_tool_use_small_result_has_no_lazy_output():
    """Small result stays fully inline (result_preview) with no separate lazy output field."""
    chat = HeadlessSageChat()
    emitter = ChatStreamEmitter(chat, build_chat_request("x"))
    assert _run(emitter.emit_tool_use(
        tool_call_id="call_1", tool_name="whoami", tool_source="mythic",
        status="completed", content="done", complete=True,
        result_preview="CORP\\kevin", output=None,
    )) is True
    tool_use = chat.emissions[-1]["metadata"]["tool_use"]
    assert tool_use["result_preview"] == "CORP\\kevin"
    assert "output" not in tool_use          # nothing lazy for a small result


def test_emit_subagent_status_produces_subagent_card():
    chat = HeadlessSageChat()
    req = build_chat_request("x")
    emitter = ChatStreamEmitter(chat, req)

    assert _run(emitter.emit_subagent_status(
        title="List all domains",
        prompt="List all active domains and report the source of each one.",
        delegation_id="bloodhound:1",
        delegation_name="BloodHound",
        status="running",
        tool_count=0,
        icon="BH",
    )) is True

    assert len(chat.emissions) == 1
    emitted = chat.emissions[0]
    assert emitted["kind"] == "response"
    assert emitted["metadata"]["special_type"] == "subagent"
    assert emitted["metadata"]["delegation_id"] == "bloodhound:1"
    assert emitted["metadata"]["delegation_name"] == "BloodHound"
    subagent = emitted["metadata"]["subagent"]
    assert subagent["title"] == "List all domains"
    assert subagent["prompt"] == "List all active domains and report the source of each one."
    assert subagent["status"] == "running"
    assert subagent["tool_count"] == 0
    assert subagent["icon"] == "BH"


def test_finished_subagent_card_carries_summary_without_duplicate_content():
    chat = HeadlessSageChat()
    emitter = ChatStreamEmitter(chat, build_chat_request("x"))

    assert _run(emitter.emit_subagent_status(
        title="List callbacks",
        prompt="List current callbacks.",
        delegation_id="mythic_operator:1",
        delegation_name="Mythic_Operator",
        status="finished",
        summary="Two active callbacks were found.",
        content="",
        complete=True,
    )) is True

    emitted = chat.emissions[0]
    assert emitted["metadata"]["subagent"]["summary"] == "Two active callbacks were found."
    assert emitted["content"] == ""


def test_emit_subagent_status_forwards_icon_color():
    """A provided icon_color reaches the subagent card metadata (fixes per-card random colors)."""
    chat = HeadlessSageChat()
    req = build_chat_request("x")
    emitter = ChatStreamEmitter(chat, req)

    assert _run(emitter.emit_subagent_status(
        title="List all domains",
        delegation_id="bloodhound:1",
        delegation_name="BloodHound",
        status="running",
        tool_count=0,
        icon="BH",
        icon_color="#E5484D",
    )) is True

    subagent = chat.emissions[0]["metadata"]["subagent"]
    assert subagent["icon_color"] == "#E5484D"


def test_delegation_color_is_deterministic_and_frontmatter_driven():
    """Model._delegation_color pins one stable color per agent, resolved in the documented order:
    the agent's prompt frontmatter ``color:`` (operator-editable), then the built-in fallback
    palette, then '' (Mythic auto-derives)."""
    from ai.langgraph.model import Model
    from ai.langgraph.prompt_loader import load_prompt_meta

    # Frontmatter is authoritative for any agent that ships a prompt file. Assert the resolution
    # contract, not a literal: an operator recoloring a card is a supported edit, not a failure.
    for agent, prompt in (("BloodHound", "bloodhound"), ("Mythic_Operator", "mythic_operator")):
        assert Model._delegation_color(agent) == load_prompt_meta(prompt)["color"].strip()
    # No prompt file at all → the built-in fallback palette supplies the color.
    assert Model._delegation_color("Execution") == "#3B82F6"
    # Same agent → same color every call (the whole point — no per-card drift).
    assert Model._delegation_color("BloodHound") == Model._delegation_color("BloodHound")
    # Unknown agent with no prompt file / no frontmatter color → empty (UI derives).
    assert Model._delegation_color("Nonexistent_Agent") == ""


def test_emit_agent_text_is_delegation_tagged_not_main_text():
    chat = HeadlessSageChat()
    req = build_chat_request("x")
    emitter = ChatStreamEmitter(chat, req)

    assert _run(emitter.emit_agent_text(
        content="I'll inspect callback state",
        delegation_id="mythic_operator:1",
        delegation_name="Mythic_Operator",
    )) is True

    assert len(chat.emissions) == 1
    emitted = chat.emissions[0]
    assert emitted["response_key"].startswith("agent_text:mythic_operator:1:")
    assert not emitted["response_key"].startswith("assistant:")
    assert emitted["metadata"]["delegation_id"] == "mythic_operator:1"
    assert emitted["metadata"]["delegation_name"] == "Mythic_Operator"


def test_emit_tool_use_includes_delegation_metadata_when_provided():
    chat = HeadlessSageChat()
    req = build_chat_request("x")
    emitter = ChatStreamEmitter(chat, req)

    assert _run(emitter.emit_tool_use(
        tool_call_id="call_2",
        tool_name="list_domains",
        tool_source="mcp",
        status="started",
        content="Using MCP tool `list_domains`...",
        complete=False,
        delegation_id="bloodhound:1",
        delegation_name="BloodHound",
    )) is True

    emitted = chat.emissions[0]
    assert emitted["metadata"]["delegation_id"] == "bloodhound:1"
    assert emitted["metadata"]["delegation_name"] == "BloodHound"
    tool_use = emitted["metadata"]["tool_use"]
    assert tool_use["delegation_id"] == "bloodhound:1"
    assert tool_use["delegation_name"] == "BloodHound"


def test_emit_tool_use_started_then_finished_reuse_key():
    chat = HeadlessSageChat()
    req = build_chat_request("x")
    emitter = ChatStreamEmitter(chat, req)

    assert _run(emitter.emit_tool_use(
        tool_call_id="call_1",
        tool_name="search_credentials",
        tool_source="mythic",
        status="started",
        content="Using Mythic tool `search_credentials`...",
        complete=False,
        arguments_present=True,
    )) is True
    assert _run(emitter.emit_tool_use(
        tool_call_id="call_1",
        tool_name="search_credentials",
        tool_source="mythic",
        status="completed",
        content="Tool `search_credentials` finished.",
        complete=True,
        result_preview="ok done",
    )) is True

    assert len(chat.emissions) == 2
    started, finished = chat.emissions
    assert started["response_key"] == "tool_use:call_1:search_credentials"
    assert finished["response_key"] == "tool_use:call_1:search_credentials"
    assert started["status"] == "streaming"
    assert finished["status"] == "complete"
    assert finished["metadata"]["tool_use"]["result_preview"] == "ok done"
    assert finished["metadata"]["tool_use"]["status"] == "completed"


def test_lifecycle_event_id_is_shared_by_tool_evidence_and_projection():
    chat = HeadlessSageChat()
    request = build_chat_request("x", request_id=34)
    emitter = ChatStreamEmitter(chat, request)
    event_id = "tool:stable-lifecycle-id"

    for status, complete in (("started", False), ("completed", True)):
        assert _run(emitter.emit_tool_use(
            event_id=event_id,
            tool_call_id="provider-reused-id",
            tool_name="list_callbacks",
            tool_source="mythic",
            status=status,
            content=status,
            complete=complete,
        ))

    assert [row["response_key"] for row in chat.emissions] == [
        f"event:{event_id}",
        f"event:{event_id}",
    ]
    assert all(
        row["metadata"]["event_id"] == event_id
        for row in chat.emissions
    )


def test_stop_final_text_projection_carries_the_lifecycle_event_id():
    chat = HeadlessSageChat()
    emitter = ChatStreamEmitter(
        chat,
        build_chat_request("stop", request_id=35),
    )

    assert _run(emitter.emit_final_response(
        event_id="final_response:stable-id",
        content="Session stopped.",
    ))

    assert chat.emissions == [{
        "kind": "text",
        "response_key": "event:final_response:stable-id",
        "content": "Session stopped.",
        "metadata": {"event_id": "final_response:stable-id"},
    }]


def test_subagent_lifecycle_reuses_key_and_tags_tool_card():
    chat = HeadlessSageChat()
    req = build_chat_request("x")
    emitter = ChatStreamEmitter(chat, req)
    delegation_id = "bloodhound:1"

    assert _run(emitter.emit_subagent_status(
        title="List all domains",
        prompt="List all active domains and report the source of each one.",
        delegation_id=delegation_id,
        delegation_name="BloodHound",
        status="running",
        tool_count=0,
        icon="BH",
    )) is True
    assert _run(emitter.emit_tool_use(
        tool_call_id="call_3",
        tool_name="list_domains",
        tool_source="mcp",
        status="started",
        content="Using MCP tool `list_domains`...",
        complete=False,
        delegation_id=delegation_id,
        delegation_name="BloodHound",
    )) is True
    assert _run(emitter.emit_subagent_status(
        title="List all domains",
        prompt="List all active domains and report the source of each one.",
        delegation_id=delegation_id,
        delegation_name="BloodHound",
        status="running",
        tool_count=1,
        icon="BH",
    )) is True
    assert _run(emitter.emit_subagent_status(
        title="List all domains",
        prompt="List all active domains and report the source of each one.",
        delegation_id=delegation_id,
        delegation_name="BloodHound",
        status="finished",
        tool_count=1,
        icon="BH",
        content="Found one domain.",
        complete=True,
    )) is True

    subagent_emissions = [
        emitted for emitted in chat.emissions
        if emitted["metadata"]["special_type"] == "subagent"
    ]
    tool_emissions = [
        emitted for emitted in chat.emissions
        if emitted["metadata"]["special_type"] == "tool_use"
    ]
    assert len(subagent_emissions) == 3
    assert {emitted["response_key"] for emitted in subagent_emissions} == {"subagent:bloodhound_1"}
    assert subagent_emissions[0]["metadata"]["subagent"]["tool_count"] == 0
    assert subagent_emissions[1]["metadata"]["subagent"]["tool_count"] == 1
    assert subagent_emissions[2]["metadata"]["subagent"]["status"] == "finished"
    assert [
        emitted["metadata"]["subagent"]["prompt"]
        for emitted in subagent_emissions
    ] == [
        "List all active domains and report the source of each one.",
        "List all active domains and report the source of each one.",
        "List all active domains and report the source of each one.",
    ]
    assert subagent_emissions[2]["complete"] is True
    assert subagent_emissions[2]["content"] == "Found one domain."
    assert len(tool_emissions) == 1
    assert tool_emissions[0]["metadata"]["delegation_id"] == delegation_id
    assert tool_emissions[0]["metadata"]["tool_use"]["delegation_id"] == delegation_id


def test_control_tools_do_not_render_operator_cards():
    from ai.langgraph.model import _is_control_tool

    assert _is_control_tool("handback_to_supervisor") is True
    assert _is_control_tool("summarize_and_handback") is True
    assert _is_control_tool("transfer_to_Mythic_Operator") is True
    assert _is_control_tool("respond_to_user") is True
    assert _is_control_tool("request_continuation") is True
    assert _is_control_tool("execute_capability") is False
    assert _is_control_tool("list_callbacks") is False
    assert _is_control_tool("") is False


def test_supervisor_handoff_text_streams_once_but_tool_only_handoff_stays_silent():
    from ai.langgraph.model import Model
    from langchain_core.messages import AIMessage

    model = Model.__new__(Model)
    model.channel_id = 7
    model.verbose = True
    model._streamed_supervisor_message_keys = set()
    streamed = []

    async def _stream(content):
        streamed.append(content)
        return True

    model._stream_message_to_mythic = _stream
    handoff = AIMessage(
        content="I found the callback inventory and need Mythic-specific verification.",
        name="Supervisor",
        tool_calls=[{
            "id": "handoff-1",
            "name": "transfer_to_Mythic_Operator",
            "args": {"task_description": "Verify callbacks"},
            "type": "tool_call",
        }],
    )
    event = {"Supervisor": {"supervisor_messages": [handoff]}}

    _run(model._process_stream_event(event))
    _run(model._process_stream_event(event))
    _run(model._process_stream_event({
        "Supervisor": {"supervisor_messages": [AIMessage(
            content="",
            name="Supervisor",
            tool_calls=[{
                "id": "handoff-2",
                "name": "transfer_to_BloodHound",
                "args": {},
                "type": "tool_call",
            }],
        )]},
    }))

    assert streamed == ["I found the callback inventory and need Mythic-specific verification.\n"]


def test_message_capture_marks_real_agent_activity_not_summarization():
    from uuid import uuid4
    from ai.langgraph.model import MessageCaptureCallback

    active = []
    callback = MessageCaptureCallback("MCP_Manager", activity_func=active.append)
    _run(callback.on_chat_model_start({}, [], run_id=uuid4(), metadata={}))
    _run(callback.on_chat_model_start(
        {}, [], run_id=uuid4(), metadata={"lc_source": "summarization"}
    ))

    assert active == ["MCP_Manager"]


class _SubagentStatusRecorder:
    def __init__(self):
        self.calls = []

    async def emit_subagent_status(self, **kwargs):
        self.calls.append(kwargs)
        return True


def test_delegation_reentry_same_source_seq_keeps_one_card():
    from ai.langgraph.model import Model

    emitter = _SubagentStatusRecorder()
    model = Model.__new__(Model)
    model._active_delegations = {}
    model._delegation_seq = 0
    model._response_emitter = emitter

    _run(model._open_delegation("Mythic_Operator", "do X", 7))
    _run(model._open_delegation("Mythic_Operator", "do X", 7))

    assert len(emitter.calls) == 1
    assert emitter.calls[0]["delegation_id"] == "mythic_operator:1"
    assert model._active_delegations["Mythic_Operator"]["source_seq"] == 7

    _run(model._open_delegation("Mythic_Operator", "do Y", 9))

    running_calls = [call for call in emitter.calls if call["status"] == "running"]
    assert [call["delegation_id"] for call in running_calls] == [
        "mythic_operator:1",
        "mythic_operator:2",
    ]
    assert model._active_delegations["Mythic_Operator"]["source_seq"] == 9


def test_delegation_card_uses_short_title_but_retains_full_instruction():
    from ai.langgraph.model import Model

    emitter = _SubagentStatusRecorder()
    model = Model.__new__(Model)
    model._active_delegations = {}
    model._delegation_seq = 0
    model._response_emitter = emitter

    instruction = "List all active Mythic callbacks and report each host, user, and integrity level."
    _run(model._open_delegation("Mythic_Operator", instruction, 7, title="List active callbacks"))
    _run(model._bump_delegation_progress("Mythic_Operator"))
    _run(model._close_delegation("Mythic_Operator"))

    assert emitter.calls[0]["title"] == "List active callbacks"
    assert emitter.calls[0]["prompt"] == instruction
    assert emitter.calls[1]["prompt"] == instruction
    assert emitter.calls[2]["prompt"] == instruction
    assert emitter.calls[2]["complete"] is True


def test_delegation_safety_close_falls_back_to_last_text():
    from ai.langgraph.model import Model

    emitter = _SubagentStatusRecorder()
    model = Model.__new__(Model)
    model._response_emitter = emitter
    model._active_delegations = {
        "Mythic_Operator": {
            "id": "mythic_operator:1",
            "name": "Mythic_Operator",
            "title": "do X",
            "tool_count": 2,
            "icon": "MO",
            "source_seq": 7,
            "last_text": "Callback 1 history shows only failed tasks.",
        }
    }

    _run(model._close_delegation("Mythic_Operator"))

    assert len(emitter.calls) == 1
    finished = emitter.calls[0]
    assert finished["content"] == ""
    assert finished["summary"] == "Callback 1 history shows only failed tasks."
    assert finished["status"] == "finished"
    assert finished["complete"] is True
    assert finished["tool_count"] == 2


def test_delegation_close_prefers_final_summary_then_explicit_content():
    from ai.langgraph.model import Model

    emitter = _SubagentStatusRecorder()
    model = Model.__new__(Model)
    model._response_emitter = emitter
    delegation = {
        "id": "mythic_operator:1",
        "name": "Mythic_Operator",
        "title": "do X",
        "tool_count": 3,
        "icon": "MO",
        "source_seq": 7,
        "last_text": "mid-run reasoning line",
        "final_summary": "",
    }

    model._active_delegations = {"Mythic_Operator": dict(delegation)}
    _run(model._capture_delegation_final_summary("Mythic_Operator", "DONE: dumped 4 hashes from DC01"))
    _run(model._close_delegation("Mythic_Operator"))
    assert emitter.calls[-1]["content"] == "DONE: dumped 4 hashes from DC01"

    model._active_delegations = {"Mythic_Operator": dict(delegation)}
    _run(model._capture_delegation_final_summary("Mythic_Operator", "DONE: dumped 4 hashes from DC01"))
    _run(model._close_delegation("Mythic_Operator", content="explicit copy-back summary"))
    assert emitter.calls[-1]["content"] == "explicit copy-back summary"


# --------------------------------------------------------------------------------------
# Config precedence + session key
# --------------------------------------------------------------------------------------

def test_config_precedence_config_over_secret_over_env(monkeypatch):
    monkeypatch.setenv("provider", "ollama")
    monkeypatch.setenv("model", "env-model")
    req = build_chat_request(
        "hi",
        config={"provider": "anthropic"},           # Config wins over Secret + env
        secrets={"model": "secret-model", "API_KEY": "sk-abc"},  # Secret wins over env
    )
    kwargs = build_model_kwargs(req)
    assert kwargs["provider"] == "anthropic"
    assert kwargs["model"] == "secret-model"
    assert kwargs["config"]["configurable"]["api_key"] == "sk-abc"
    # neutral placeholders — chat has no task
    assert kwargs["task_id"] == 0 and kwargs["agent_task_id"] == ""
    assert kwargs["operation_id"] == req.OperationID


def test_mode_auto_sets_autonomous_solve():
    req = build_chat_request("hi", config={"mode": "auto"})
    kwargs = build_model_kwargs(req)
    assert kwargs["mode"] == "auto"
    assert kwargs["autonomous_solve"] is True


def test_build_model_kwargs_threads_eval_force_prefix_from_channel_config(monkeypatch):
    monkeypatch.setenv("SAGE_EVAL_FORCE_CAPABILITY_PREFIX_JSON", '[{"capability":"env"}]')
    raw = '[{"capability":"read-managed-local-admin-secret","exact_target":"target=blue-ops01"}]'
    req = build_chat_request("hi", config={"SAGE_EVAL_FORCE_CAPABILITY_PREFIX_JSON": raw})

    kwargs = build_model_kwargs(req)

    assert kwargs["eval_force_capability_prefix_json"] == raw


def test_channel_session_key_is_channel_id():
    req = build_chat_request("hi", channel_id=77)
    assert channel_session_key(req) == "77"


def test_channel_checkpoint_generations_are_stable_per_model_and_unique_across_replacements():
    req = build_chat_request("hi", channel_id=77)
    first = _FakeModel()
    second = _FakeModel()

    first_id = bind_channel_thread_id(req, first)
    assert bind_channel_thread_id(req, first) == first_id
    second_id = bind_channel_thread_id(req, second)

    assert first_id.startswith("77:generation:")
    assert second_id.startswith("77:generation:")
    assert first_id != second_id


# --------------------------------------------------------------------------------------
# Phase 3 — slash commands
# --------------------------------------------------------------------------------------

from mythic_container.ChatBase import ChatSlashCommandInvocation
from sage_chat.slash import handle_slash, SLASH_COMMANDS


def _slash_req(name, argument="", channel_id=5, request_id=1):
    r = build_chat_request("", channel_id=channel_id, request_id=request_id)
    r.SlashCommand = ChatSlashCommandInvocation(name=name, argument=argument)
    return r


def test_slash_commands_declared():
    assert {c.Name for c in SLASH_COMMANDS} == {"state", "list", "mode", "stop", "mcp", "bloodhound", "sandbox"}


def test_slash_state_no_session_is_handled_with_one_terminal(monkeypatch):
    from sage_chat import slash

    async def _no_resolve(model, request):  # no Mythic in unit tests — simulate an unresolvable operation
        return ""

    monkeypatch.setattr(slash, "_resolve_chat_engagement_id", _no_resolve)
    chat = HeadlessSageChat()
    handled = _run(handle_slash(chat, _slash_req("state"), None, "slash:1"))
    assert handled is True
    assert len(chat.terminal_emissions) == 1
    assert "start a session" in chat.emissions[-1]["content"]


def test_slash_mode_show_then_set():
    chat = HeadlessSageChat()

    class _M:
        mode = "supervised"
        _autonomous_solve = False
        _chat_request_config_signature = "base-signature"
        _chat_request_base_autonomous_solve = False

    m = _M()
    _run(handle_slash(chat, _slash_req("mode"), m, "slash:1"))
    assert "supervised" in chat.emissions[-1]["content"]
    _run(handle_slash(chat, _slash_req("mode", "auto"), m, "slash:2"))
    assert m.mode == "auto"
    assert m._autonomous_solve is True
    assert m._chat_mode_override == "auto"
    assert m._chat_mode_override_base_signature == "base-signature"
    assert m._chat_mode_override_base_autonomous_solve is False


def test_slash_unknown_falls_through_without_emitting():
    chat = HeadlessSageChat()
    handled = _run(handle_slash(chat, _slash_req("frobnicate"), None, "slash:1"))
    assert handled is False
    assert chat.emissions == []


def test_slash_mcp_and_bloodhound_declared():
    assert {c.Name for c in SLASH_COMMANDS} >= {"mcp", "bloodhound", "sandbox"}


def test_slash_mcp_list_empty():
    from ai import mcp as mcpmod
    import sage_chat.slash as slashmod
    orig = mcpmod.MCPManager.get_connected_servers
    mcpmod.MCPManager.get_connected_servers = lambda: []
    try:
        chat = HeadlessSageChat()
        _run(handle_slash(chat, _slash_req("mcp", "list"), None, "slash:1"))
        assert "No MCP servers connected" in chat.emissions[-1]["content"]
    finally:
        mcpmod.MCPManager.get_connected_servers = orig


def test_slash_mcp_connect_invalid_json():
    chat = HeadlessSageChat()
    _run(handle_slash(chat, _slash_req("mcp", "connect {not valid}"), None, "slash:1"))
    assert "Invalid JSON" in chat.emissions[-1]["content"]


def test_slash_mcp_connect_rejects_nonlist_read_only_tool_policy():
    chat = HeadlessSageChat()
    _run(handle_slash(
        chat,
        _slash_req(
            "mcp",
            'connect {"type":"sse","name":"Nemesis","url":"https://nemesis.local/mcp",'
            '"read_only_tools":"search-files"}',
        ),
        None,
        "slash:1",
    ))
    assert "must be a JSON list" in chat.emissions[-1]["content"]


def test_slash_mcp_connect_rejects_whitespace_normalized_tool_authority():
    chat = HeadlessSageChat()
    _run(handle_slash(
        chat,
        _slash_req(
            "mcp",
            'connect {"type":"sse","name":"Nemesis","url":"https://nemesis.local/mcp",'
            '"read_only_tools":[" search-files"]}',
        ),
        None,
        "slash:1",
    ))
    assert "without surrounding whitespace" in chat.emissions[-1]["content"]


def test_slash_mcp_connect_sse_passes_tls_and_timeout_options(monkeypatch):
    from ai import mcp as mcpmod

    captured = {}

    class _Config:
        extra_params = None

    sentinel = _Config()

    def _sse_config(**kwargs):
        captured.update(kwargs)
        return sentinel

    async def _connect(conf):
        assert conf is sentinel
        return True, None

    monkeypatch.setattr(mcpmod, "create_sse_config", _sse_config)
    monkeypatch.setattr(mcpmod.MCPManager, "connect_server", _connect)

    chat = HeadlessSageChat()
    _run(
        handle_slash(
            chat,
            _slash_req(
                "mcp",
                'connect {"type":"sse","name":"Nemesis","url":"https://nemesis.local/mcp/sse",'
                '"headers":{"Authorization":"Basic bjpu"},"timeout":12,"sse_read_timeout":45,'
                '"ssl_verify":false,"session_kwargs":{"read_timeout_seconds":90},'
                '"read_only_tools":["search-files","count-files","search-files"],'
                '"sage_execution_class":"non_target_control_plane"}',
            ),
            None,
            "slash:1",
        )
    )

    assert captured == {
        "name": "Nemesis",
        "url": "https://nemesis.local/mcp/sse",
        "headers": {"Authorization": "Basic bjpu"},
        "timeout": 12,
        "sse_read_timeout": 45,
        "ssl_verify": False,
        "session_kwargs": {"read_timeout_seconds": 90},
        "sage_execution_class": "non_target_control_plane",
    }
    assert sentinel.extra_params == {"read_only_tools": ["search-files", "count-files"]}
    assert "Connected MCP server `Nemesis`" in chat.emissions[-1]["content"]


def test_slash_mcp_disconnect(monkeypatch):
    from ai import mcp as mcpmod

    async def _disc(name):
        return True

    monkeypatch.setattr(mcpmod.MCPManager, "disconnect_server", _disc)
    chat = HeadlessSageChat()
    _run(handle_slash(chat, _slash_req("mcp", "disconnect srv1"), None, "slash:1"))
    assert "Disconnected MCP server `srv1`" in chat.emissions[-1]["content"]


def test_slash_mcp_call_invokes_one_exact_locally_allowlisted_tool(monkeypatch):
    from ai import mcp as mcpmod

    observed = {}

    class _Config:
        extra_params = {"read_only_tools": ["search-files"]}

    class _Tool:
        name = "search-files"

        async def ainvoke(self, arguments):
            observed["arguments"] = arguments
            return {"rows": [{"path": "a.txt"}]}

    monkeypatch.setattr(mcpmod.MCPManager, "configs", {"Nemesis": _Config()}, raising=False)
    monkeypatch.setattr(mcpmod.MCPManager, "get_connected_servers", lambda: ["Nemesis"])
    monkeypatch.setattr(mcpmod.MCPManager, "get_tools_by_server", lambda server: [_Tool()] if server == "Nemesis" else [])
    monkeypatch.setattr(mcpmod.MCPManager, "is_bloodhound_server", lambda server: False)

    chat = HeadlessSageChat()
    _run(handle_slash(
        chat,
        _slash_req("mcp", 'call Nemesis search-files {"query":"hello"}'),
        None,
        "slash:1",
    ))

    assert observed["arguments"] == {"query": "hello"}
    assert "MCP result — `Nemesis.search-files`" in chat.emissions[-1]["content"]
    assert '"path": "a.txt"' in chat.emissions[-1]["content"]


@pytest.mark.parametrize(
    ("argument", "expected"),
    [
        ('call Nemesis search-files {"query":"hello"}', "no local `read_only_tools` allowlist"),
        ('call Other search-files {"query":"hello"}', "no connected server named exactly `Other`"),
        ('call Nemesis search-files {not-json}', "Invalid JSON"),
    ],
)
def test_slash_mcp_call_denies_without_exact_local_authority(monkeypatch, argument, expected):
    from ai import mcp as mcpmod

    class _Config:
        extra_params = {}

    class _Tool:
        name = "search-files"

    monkeypatch.setattr(mcpmod.MCPManager, "configs", {"Nemesis": _Config()}, raising=False)
    monkeypatch.setattr(mcpmod.MCPManager, "get_connected_servers", lambda: ["Nemesis"])
    monkeypatch.setattr(mcpmod.MCPManager, "get_tools_by_server", lambda server: [_Tool()] if server == "Nemesis" else [])
    monkeypatch.setattr(mcpmod.MCPManager, "is_bloodhound_server", lambda server: False)

    chat = HeadlessSageChat()
    _run(handle_slash(chat, _slash_req("mcp", argument), None, "slash:1"))

    assert expected in chat.emissions[-1]["content"]


@pytest.mark.parametrize(
    "raw_json",
    [
        '{"outer":{"key":1,"key":2}}',
        '{"value":NaN}',
        '{"value":Infinity}',
        '{"value":-Infinity}',
        '{"value":1e999}',
    ],
)
def test_slash_mcp_call_rejects_ambiguous_or_nonfinite_json_without_invocation(
    monkeypatch, raw_json
):
    from ai import mcp as mcpmod

    invocations = []

    class _Config:
        extra_params = {"read_only_tools": ["search-files"]}

    class _Tool:
        name = "search-files"

        async def ainvoke(self, arguments):
            invocations.append(arguments)
            return {}

    monkeypatch.setattr(mcpmod.MCPManager, "configs", {"Nemesis": _Config()}, raising=False)
    monkeypatch.setattr(mcpmod.MCPManager, "get_connected_servers", lambda: ["Nemesis"])
    monkeypatch.setattr(mcpmod.MCPManager, "get_tools_by_server", lambda server: [_Tool()])
    monkeypatch.setattr(mcpmod.MCPManager, "is_bloodhound_server", lambda server: False)

    chat = HeadlessSageChat()
    _run(handle_slash(
        chat,
        _slash_req("mcp", f"call Nemesis search-files {raw_json}"),
        None,
        "slash:1",
    ))

    assert "Invalid JSON for `/mcp call`" in chat.emissions[-1]["content"]
    assert invocations == []


def test_slash_mcp_call_denies_duplicate_or_malformed_tool_registry(monkeypatch):
    from ai import mcp as mcpmod

    class _Config:
        extra_params = {"read_only_tools": ["search-files"]}

    class _Tool:
        def __init__(self, name):
            self.name = name

    monkeypatch.setattr(mcpmod.MCPManager, "configs", {"Nemesis": _Config()}, raising=False)
    monkeypatch.setattr(mcpmod.MCPManager, "get_connected_servers", lambda: ["Nemesis"])
    monkeypatch.setattr(mcpmod.MCPManager, "is_bloodhound_server", lambda server: False)

    monkeypatch.setattr(
        mcpmod.MCPManager,
        "get_tools_by_server",
        lambda server: [_Tool("search-files"), _Tool("search-files")],
    )
    duplicate_chat = HeadlessSageChat()
    _run(handle_slash(
        duplicate_chat,
        _slash_req("mcp", 'call Nemesis search-files {"query":"hello"}'),
        None,
        "slash:1",
    ))
    assert "duplicate tools named `search-files`" in duplicate_chat.emissions[-1]["content"]

    monkeypatch.setattr(
        mcpmod.MCPManager,
        "get_tools_by_server",
        lambda server: [_Tool(" search-files")],
    )
    malformed_chat = HeadlessSageChat()
    _run(handle_slash(
        malformed_chat,
        _slash_req("mcp", 'call Nemesis search-files {"query":"hello"}'),
        None,
        "slash:1",
    ))
    assert "exposes malformed tool names" in malformed_chat.emissions[-1]["content"]


def test_slash_mcp_call_denies_duplicate_unselected_catalog_names(monkeypatch):
    from ai import mcp as mcpmod

    invocations = []

    class _Config:
        extra_params = {"read_only_tools": ["search-files"]}

    class _Tool:
        def __init__(self, name):
            self.name = name

        async def ainvoke(self, arguments):
            invocations.append(arguments)
            return {}

    monkeypatch.setattr(mcpmod.MCPManager, "configs", {"Nemesis": _Config()}, raising=False)
    monkeypatch.setattr(mcpmod.MCPManager, "get_connected_servers", lambda: ["Nemesis"])
    monkeypatch.setattr(
        mcpmod.MCPManager,
        "get_tools_by_server",
        lambda server: [_Tool("search-files"), _Tool("other"), _Tool("other")],
    )
    monkeypatch.setattr(mcpmod.MCPManager, "is_bloodhound_server", lambda server: False)

    chat = HeadlessSageChat()
    _run(handle_slash(
        chat,
        _slash_req("mcp", 'call Nemesis search-files {"query":"hello"}'),
        None,
        "slash:1",
    ))

    assert "exposes duplicate tool names" in chat.emissions[-1]["content"]
    assert invocations == []


def test_slash_mcp_call_denies_canonical_bloodhound_server(monkeypatch):
    from ai import mcp as mcpmod

    class _Config:
        extra_params = {"read_only_tools": ["cypher_query"]}

    class _Tool:
        name = "cypher_query"

    monkeypatch.setattr(mcpmod.MCPManager, "configs", {"BloodHound": _Config()}, raising=False)
    monkeypatch.setattr(mcpmod.MCPManager, "get_connected_servers", lambda: ["BloodHound"])
    monkeypatch.setattr(mcpmod.MCPManager, "get_tools_by_server", lambda server: [_Tool()])
    monkeypatch.setattr(mcpmod.MCPManager, "is_bloodhound_server", lambda server: server == "BloodHound")

    chat = HeadlessSageChat()
    _run(handle_slash(
        chat,
        _slash_req("mcp", 'call BloodHound cypher_query {"query":"MATCH (n) RETURN n"}'),
        None,
        "slash:1",
    ))

    assert "excludes the canonical BloodHound server" in chat.emissions[-1]["content"]


def test_slash_mcp_call_denies_casefold_bloodhound_name_without_canonical_match(monkeypatch):
    from ai import mcp as mcpmod

    class _Config:
        extra_params = {"read_only_tools": ["read"]}

    monkeypatch.setattr(mcpmod.MCPManager, "configs", {"bLoOdHoUnD": _Config()}, raising=False)
    monkeypatch.setattr(mcpmod.MCPManager, "get_connected_servers", lambda: ["bLoOdHoUnD"])
    monkeypatch.setattr(mcpmod.MCPManager, "get_tools_by_server", lambda server: [])
    monkeypatch.setattr(mcpmod.MCPManager, "is_bloodhound_server", lambda server: False)

    chat = HeadlessSageChat()
    _run(handle_slash(
        chat,
        _slash_req("mcp", 'call bLoOdHoUnD read {"query":"x"}'),
        None,
        "slash:1",
    ))

    assert "excludes the canonical BloodHound server" in chat.emissions[-1]["content"]


@pytest.mark.parametrize(
    "tool_factory",
    [
        lambda: type(
            "_Tool",
            (),
            {
                "name": "search-files",
                "metadata": {"readOnlyHint": False},
                "ainvoke": lambda self, arguments: (_ for _ in ()).throw(AssertionError("must not invoke")),
            },
        )(),
        lambda: type(
            "_Tool",
            (),
            {
                "name": "search-files",
                "annotations": {"destructiveHint": True},
                "ainvoke": lambda self, arguments: (_ for _ in ()).throw(AssertionError("must not invoke")),
            },
        )(),
    ],
)
def test_slash_mcp_call_denies_explicit_non_read_only_annotations(monkeypatch, tool_factory):
    from ai import mcp as mcpmod

    class _Config:
        extra_params = {"read_only_tools": ["search-files"]}

    monkeypatch.setattr(mcpmod.MCPManager, "configs", {"Nemesis": _Config()}, raising=False)
    monkeypatch.setattr(mcpmod.MCPManager, "get_connected_servers", lambda: ["Nemesis"])
    monkeypatch.setattr(mcpmod.MCPManager, "get_tools_by_server", lambda server: [tool_factory()])
    monkeypatch.setattr(mcpmod.MCPManager, "is_bloodhound_server", lambda server: False)

    chat = HeadlessSageChat()
    _run(handle_slash(
        chat,
        _slash_req("mcp", 'call Nemesis search-files {"query":"hello"}'),
        None,
        "slash:1",
    ))

    assert "has an explicit non-read-only annotation" in chat.emissions[-1]["content"]


def test_slash_mcp_call_times_out_after_bounded_wait(monkeypatch):
    from ai import mcp as mcpmod
    import sage_chat.slash as slashmod

    class _Config:
        extra_params = {"read_only_tools": ["search-files"]}

    class _Tool:
        name = "search-files"

        async def ainvoke(self, arguments):
            await asyncio.sleep(0.02)
            return {"rows": []}

    monkeypatch.setattr(mcpmod.MCPManager, "configs", {"Nemesis": _Config()}, raising=False)
    monkeypatch.setattr(mcpmod.MCPManager, "get_connected_servers", lambda: ["Nemesis"])
    monkeypatch.setattr(mcpmod.MCPManager, "get_tools_by_server", lambda server: [_Tool()])
    monkeypatch.setattr(mcpmod.MCPManager, "is_bloodhound_server", lambda server: False)
    monkeypatch.setattr(slashmod, "_MCP_CALL_TIMEOUT_SECONDS", 0.001)

    chat = HeadlessSageChat()
    _run(handle_slash(
        chat,
        _slash_req("mcp", 'call Nemesis search-files {"query":"hello"}'),
        None,
        "slash:1",
    ))

    assert "timed out after 0.001 seconds" in chat.emissions[-1]["content"]


def test_slash_mcp_call_deadline_survives_cancellation_resistant_tool(monkeypatch):
    from ai import mcp as mcpmod
    import sage_chat.slash as slashmod

    invocations = 0

    class _Config:
        extra_params = {"read_only_tools": ["search-files"]}

    class _Tool:
        name = "search-files"

        async def ainvoke(self, arguments):
            nonlocal invocations
            invocations += 1
            try:
                await asyncio.sleep(1)
            except asyncio.CancelledError:
                await asyncio.sleep(0.05)
                return {"late": True}

    monkeypatch.setattr(mcpmod.MCPManager, "configs", {"Nemesis": _Config()}, raising=False)
    monkeypatch.setattr(mcpmod.MCPManager, "get_connected_servers", lambda: ["Nemesis"])
    monkeypatch.setattr(mcpmod.MCPManager, "get_tools_by_server", lambda server: [_Tool()])
    monkeypatch.setattr(mcpmod.MCPManager, "is_bloodhound_server", lambda server: False)
    monkeypatch.setattr(slashmod, "_MCP_CALL_TIMEOUT_SECONDS", 0.005)

    async def scenario():
        chat = HeadlessSageChat()
        started = asyncio.get_running_loop().time()
        await handle_slash(
            chat,
            _slash_req("mcp", 'call Nemesis search-files {"query":"hello"}'),
            None,
            "slash:1",
        )
        slash_elapsed = asyncio.get_running_loop().time() - started
        await asyncio.sleep(0.07)
        return chat, slash_elapsed

    chat, slash_elapsed = _run(scenario())

    assert slash_elapsed < 0.04
    assert invocations == 1
    assert "timed out after 0.005 seconds" in chat.emissions[-1]["content"]


def test_slash_bloodhound(monkeypatch):
    from ai import bloodhound_config as bh

    async def _ensure(directory=None, **_kwargs):
        return (True, "BloodHound MCP connected.")

    monkeypatch.setattr(bh, "ensure_bloodhound_connected", _ensure)
    chat = HeadlessSageChat()
    _run(handle_slash(chat, _slash_req("bloodhound"), None, "slash:1"))
    assert "BloodHound MCP connected" in chat.emissions[-1]["content"]


def test_slash_sandbox_executes_without_model_turn(monkeypatch):
    import sage_chat.slash as slashmod

    class _Tools:
        async def sandbox_exec(self, code_or_command, language="shell", timeout=None):
            assert code_or_command == "print(2 + 2)"
            assert language == "python"
            assert timeout is None
            return '{"exit_code": 0, "status": "ok", "stderr": "", "stdout": "4\\n", "timed_out": false, "truncated": false}'

    async def _tools_for_request(model, request):
        assert model is None
        return _Tools()

    monkeypatch.setattr(slashmod, "_sandbox_tools_for_request", _tools_for_request)
    chat = HeadlessSageChat()
    _run(handle_slash(chat, _slash_req("sandbox", "python print(2 + 2)"), None, "slash:1"))
    text = chat.emissions[-1]["content"]
    assert "**Sandbox result**" in text
    assert "| Language | `python` |" in text
    assert "```stdout\n4\n\n```" in text


def test_slash_sandbox_usage_for_missing_code():
    chat = HeadlessSageChat()
    _run(handle_slash(chat, _slash_req("sandbox"), None, "slash:1"))
    assert "Usage: `/sandbox [shell|python] <code>`" in chat.emissions[-1]["content"]


def test_slash_dispatched_via_chat_without_creating_model():
    class _NoModelChat(HeadlessSageChat):
        async def _get_or_create_model(self, request):  # must not be called for a handled slash
            raise AssertionError("slash command should not construct a Model")

    chat = _NoModelChat()
    _run(chat.chat(_slash_req("state", channel_id=9, request_id=3)))
    assert len(chat.terminal_emissions) == 1


def test_slash_sandbox_dispatched_via_chat_without_creating_model(monkeypatch):
    import sage_chat.slash as slashmod

    class _NoModelChat(HeadlessSageChat):
        async def _get_or_create_model(self, request):
            raise AssertionError("slash command should not construct a Model")

    class _Tools:
        async def sandbox_exec(self, code_or_command, language="shell", timeout=None):
            return '{"exit_code": 0, "status": "ok", "stderr": "", "stdout": "ok\\n", "timed_out": false, "truncated": false}'

    async def _tools_for_request(model, request):
        return _Tools()

    monkeypatch.setattr(slashmod, "_sandbox_tools_for_request", _tools_for_request)
    chat = _NoModelChat()
    _run(chat.chat(_slash_req("sandbox", "shell printf ok", channel_id=9, request_id=4)))
    assert len(chat.terminal_emissions) == 1
    assert "**Sandbox result**" in chat.emissions[-1]["content"]


# --------------------------------------------------------------------------------------
# Phase 2 P0 — MythicTools chat-token auth rewrite
# --------------------------------------------------------------------------------------

def test_build_model_kwargs_threads_channel_auth():
    req = build_chat_request("hi", channel_id=42, operation_id=7)
    req.APITokenID = 99
    kwargs = build_model_kwargs(req)
    assert kwargs["channel_id"] == 42
    assert kwargs["apitoken_id"] == 99
    assert kwargs["operation_id"] == 7


def test_login_chat_branch_degrades_when_mint_fails(monkeypatch):
    """channel_id set → chat branch; if the token mint fails (offline), client stays None (fail closed).

    Patches ``create`` on the real ``ChatAPITokenProvider`` class (not the module attribute) so the fake
    is used no matter how ``login()`` imports the symbol — otherwise the real RPC blocks on absent RabbitMQ.
    """
    from ai.langgraph import mythic_tools as mt
    from mythic_container.ChatBase import ChatAPITokenProvider

    async def _boom(cls, *a, **k):
        raise RuntimeError("no mythic reachable")

    monkeypatch.setattr(ChatAPITokenProvider, "create", classmethod(_boom))
    client = mt.MythicTools(channel_id=5, operation_id=1, apitoken_id=2)
    _run(client.login())
    assert client.client is None  # degraded, did not raise or hang


def test_login_chat_branch_mints_channel_token(monkeypatch):
    """Happy chat path: token minted via ChatAPITokenProvider(ChatChannelID) and passed to mythic.login."""
    from ai.langgraph import mythic_tools as mt
    from mythic_container.ChatBase import ChatAPITokenProvider

    class _Prov:
        async def get_token(self):
            return "chat-token"

    async def _create(cls, op, ch, tok):
        assert (op, ch, tok) == (1, 5, 2)
        return _Prov()

    seen = {}

    async def _fake_login(apitoken, server_ip, server_port, ssl):
        seen["apitoken"] = apitoken
        return "CLIENT"

    monkeypatch.setattr(ChatAPITokenProvider, "create", classmethod(_create))
    monkeypatch.setattr(mt.mythic, "login", _fake_login)
    client = mt.MythicTools(channel_id=5, operation_id=1, apitoken_id=2)
    _run(client.login())
    assert client.client == "CLIENT"
    assert seen["apitoken"] == "chat-token"


def test_login_task_branch_unchanged(monkeypatch):
    """No channel_id but agent_task_id set → legacy task path mints from AgentTaskID."""
    from ai.langgraph import mythic_tools as mt

    seen = {}

    class _Resp:
        Success = True
        APIToken = "task-token"

    async def _fake_rpc(msg):
        return _Resp()

    async def _fake_login(apitoken, server_ip, server_port, ssl):
        seen["apitoken"] = apitoken
        return "TASKCLIENT"

    monkeypatch.setattr(mt, "SendMythicRPCAPITokenCreate", _fake_rpc)
    monkeypatch.setattr(mt.mythic, "login", _fake_login)
    client = mt.MythicTools(agent_task_id="task-123")
    _run(client.login())
    assert client.client == "TASKCLIENT"
    assert seen["apitoken"] == "task-token"


# --------------------------------------------------------------------------------------
# Phase 2 — native-card HITL (Option C)
# --------------------------------------------------------------------------------------

from sage_chat.hitl import (
    approved_action_ids_for_request,
    approval_action_digest,
    approval_action_fingerprint,
    approval_claim_actions,
    approval_proposal_digest,
    approval_selection_digest,
    approval_response_matches,
    build_approval_request,
    make_card_emitter,
    resume_decision_for_request,
    should_confirm,
)
from mythic_container.ChatBase import ChatInputResponse


class _HitlModel:
    """Stub Model that raises a card on the first turn and resumes on the next."""

    def __init__(self):
        self._response_emitter = None
        self._hitl_card_emitter = None
        self._hitl_card_pending = False
        self._thread_id_override = None
        self._pending = False
        self.resumed_with = None
        self.approval_claims = []
        self.approval_claim_clears = 0
        self.open_tool_close_statuses = []
        self.open_tool_ids = ("claimed-tool",)

    async def _hitl_interrupt_pending(self, thread_id):
        return self._pending

    def install_approval_claim(self, context):
        self.approval_claims.append(dict(context))

    def apply_request_action_selection(self, context, approved_action_ids):
        rebound = dict(context)
        rebound["approved_action_ids"] = list(approved_action_ids)
        rebound["approved_actions"] = [
            action
            for action in context["actions"]
            if approval_action_fingerprint(action) in approved_action_ids
        ]
        rebound["selection_digest"] = (
            approval_selection_digest(
                rebound["request_contract_digest"],
                rebound["action_digest"],
                approved_action_ids,
            )
            if approved_action_ids
            else ""
        )
        return rebound

    def clear_approval_claim(self):
        self.approval_claim_clears += 1

    def _open_tool_lifecycle_ids(self):
        return self.open_tool_ids

    async def _close_open_tool_lifecycles(
        self,
        status="cancelled",
        *,
        event_ids=None,
    ):
        self.open_tool_close_statuses.append((status, tuple(event_ids or ())))

    async def invoke(self, prompt, is_interactive=False):
        # Simulate hitting a guarded tool: emit the card (which finishes request N) and pause.
        await self._hitl_card_emitter([{"name": "execute_capability", "args": {"target": "DC01"}}])
        self._hitl_card_pending = True
        self._pending = True

    async def handle_hitl_resume(
        self,
        decision,
        thread_id,
        operator_message="",
        expected_action_digest="",
        approved_action_ids=None,
    ):
        del expected_action_digest, approved_action_ids
        self.resumed_with = decision
        self.steered_with = operator_message
        self._pending = False
        await self._response_emitter(f"🤖> resume:{decision}")
        return ""

    def request_stop(self):
        pass


class _HitlDriverChat(HeadlessSageChat):
    def __init__(self, model):
        super().__init__()
        self._model = model

    async def _get_or_create_model(self, request):
        return self._model, self._model._pending  # preexisted-ish; routing uses interrupt_pending anyway


class _ControllerHitlModel(_HitlModel):
    """Stub controller-owned approval that is pending outside LangGraph checkpoint state."""

    def __init__(self):
        super().__init__()
        self._controller_hitl_pending = {"tool": "execute_capability", "args": {"target": "DC01"}}

    async def handle_controller_hitl_resume(self, decision, expected_action_digest=""):
        del expected_action_digest
        self.resumed_with = decision
        self._controller_hitl_pending = None
        await self._response_emitter(f"🤖> controller-resume:{decision}")
        return ""


class _MultiHitlModel(_HitlModel):
    def __init__(self):
        super().__init__()
        self.selected_action_ids = None

    async def invoke(self, prompt, is_interactive=False):
        del prompt, is_interactive
        await self._hitl_card_emitter([
            {
                "name": "issue_task_and_waitfor_task_output",
                "args": {
                    "command": "whoami",
                    "parameters": "",
                    "callback_display_id": 7,
                },
            },
            {
                "name": "add_credential",
                "args": {"credential": "example", "account": "sam"},
            },
        ])
        self._hitl_card_pending = True
        self._pending = True

    async def handle_hitl_resume(
        self,
        decision,
        thread_id,
        operator_message="",
        expected_action_digest="",
        approved_action_ids=None,
    ):
        del thread_id, operator_message, expected_action_digest
        self.resumed_with = decision
        self.selected_action_ids = tuple(approved_action_ids or ())
        self._pending = False
        return ""


def _install_pending_approval_context(
    model,
    request,
    approval_id="approval-1",
    action_requests=None,
):
    from ai.langgraph.request_contract import build_request_contract

    thread_id = bind_channel_thread_id(request, model)
    action_requests = action_requests or [
        {"name": "execute_capability", "args": {"target": "DC01"}}
    ]
    contract = build_request_contract(
        request_id=f"chat:{request.ChannelID}:request:{request.RequestID}",
        channel_id=str(request.ChannelID),
        operation_id=str(request.OperationID),
        mode="supervised",
        autonomous_solve=False,
    )
    model._request_contract = contract
    action_digest = approval_action_digest(action_requests)
    context = {
        "approval_id": approval_id,
        "thread_id": thread_id,
        "turn_id": contract.request_id,
        "request_id": contract.request_id,
        "request_contract_digest": contract.digest,
        "tool_name": "execute_capability",
        "actions": approval_claim_actions(action_requests),
        "selection_mode": "single",
        "action_digest": action_digest,
        "proposal_digest": approval_proposal_digest(contract.digest, action_digest),
        "operation_id": str(request.OperationID),
        "apitoken_id": str(request.APITokenID),
    }
    model._pending_approval_context = context
    return context


def _input_response_for_context(action, context, choice=None):
    card = build_approval_request(context["actions"])
    input_type = (
        "single_choice"
        if context.get("selection_mode") == "exact_one"
        else "approval"
    )
    canonical_choices = card.get("choices", [])
    selected_choice = choice or {}
    if (
        action == "select"
        and isinstance(choice, dict)
        and set(choice) == {"id"}
    ):
        selected_choice = next(
            (
                dict(candidate)
                for candidate in canonical_choices
                if candidate["id"] == choice["id"]
            ),
            dict(choice),
        )
    input_request_message_id = 71
    resolved_by_operator_id = 19
    resolved_by = "operator"
    resolved_at = "2026-07-24T00:00:00Z"
    response_payload = {
        "action": action,
        "input_request_message_id": input_request_message_id,
        "resolved_by_operator_id": resolved_by_operator_id,
        "resolved_by": resolved_by,
        "resolved_at": resolved_at,
    }
    if action == "select":
        response_payload["choice"] = selected_choice
    input_request = {
        "status": {
            "accept": "accepted",
            "reject": "rejected",
            "respond": "responded",
            "select": "selected",
        }[action],
        "input_type": input_type,
        "title": card["title"],
        "prompt": card["prompt"],
        "description": card["description"],
        # Mythic persists this field for all input-request types, including
        # an empty catalog for a one-action approval card.
        "choices": canonical_choices,
        "data": {
            **card["data"],
            "sage_approval_context": dict(context),
        },
        "response": response_payload,
        "resolved_by_operator_id": resolved_by_operator_id,
        "resolved_by": resolved_by,
        "resolved_at": resolved_at,
    }
    return ChatInputResponse(
        action=action,
        choice=selected_choice,
        input_request_message_id=input_request_message_id,
        input_request=input_request,
        resolved_by_operator_id=resolved_by_operator_id,
        resolved_by=resolved_by,
        resolved_at=resolved_at,
    )


def test_should_confirm_policy():
    assert should_confirm("execute_capability") is True
    assert should_confirm("list_callbacks") is False          # not guarded
    assert should_confirm("execute_capability", "auto") is False  # auto never arms


def test_approval_request_shape():
    req = build_approval_request([{"name": "create_payload", "args": {"os": "windows"}}])
    assert set(req) == {"title", "prompt", "description", "data"}
    assert "create_payload" in req["title"]
    assert req["data"]["tool_name"] == "create_payload"
    assert req["data"]["display_name"] == "create_payload"
    assert req["data"]["arguments"] == {"os": "windows"}
    assert req["data"]["guarded_action_count"] == 1


def test_approval_request_uses_capability_display_name_without_changing_guarded_tool():
    req = build_approval_request([{
        "name": "execute_capability",
        "args": {"action": {"name": "forge-golden-ticket"}, "inputs": {"callback_id": "3"}},
    }])
    assert req["title"] == "Approve: forge-golden-ticket"
    assert "forge-golden-ticket" in req["prompt"]
    assert req["data"]["tool_name"] == "execute_capability"
    assert req["data"]["display_name"] == "forge-golden-ticket"


def test_approval_request_discloses_every_guarded_action_and_exact_arguments():
    req = build_approval_request([
        {"name": "create_payload", "args": {"os": "windows", "arch": "x64"}},
        {"name": "execute_capability", "args": {"action": {"name": "dcsync-krbtgt"}}},
    ])

    assert req["title"] == "Select 1 of 2 guarded actions"
    assert "Select exactly one" in req["prompt"]
    assert "Action 1: create_payload" in req["description"]
    assert "os: windows" in req["description"]
    assert "Action 2: dcsync-krbtgt" in req["description"]
    assert req["data"]["guarded_action_count"] == 2
    assert [action["tool_name"] for action in req["data"]["actions"]] == [
        "create_payload",
        "execute_capability",
    ]
    assert len(req["choices"]) == 2
    assert len({choice["id"] for choice in req["choices"]}) == 2


def test_approval_request_rejects_duplicate_action_identities():
    action = {
        "name": "execute_capability",
        "args": {"action": {"name": "example"}, "inputs": {"callback_id": "7"}},
    }
    with pytest.raises(ValueError, match="duplicate guarded action identities"):
        build_approval_request([action, dict(action)])


def test_failed_approval_card_send_never_installs_resumable_context():
    class _BrokenChat:
        async def send_approval_request(self, *_args, **_kwargs):
            raise RuntimeError("transport down")

    request = build_chat_request("x", channel_id=6, request_id=1)
    stored = []
    emitter = make_card_emitter(
        _BrokenChat(),
        request,
        approval_context_store=stored.append,
    )

    with pytest.raises(RuntimeError, match="transport down"):
        _run(emitter([{"name": "execute_capability", "args": {"target": "DC01"}}]))
    assert stored == []


def test_approval_cards_use_unique_keys_and_optional_delegation_tags():
    chat = HeadlessSageChat()
    req = build_chat_request("x", channel_id=5, request_id=9)
    emitter = make_card_emitter(chat, req)

    _run(emitter([{"name": "execute_capability", "args": {"callback": 2}}]))
    _run(emitter([{"name": "execute_capability", "args": {"callback": 2}}]))

    input_reqs = [
        emitted
        for emitted in chat.emissions
        if emitted["kind"] == "complete"
        and emitted.get("metadata", {}).get("special_type") == "input_requested"
    ]
    assert len(input_reqs) == 2
    assert input_reqs[0]["response_key"] != input_reqs[1]["response_key"]   # each approval is a fresh card
    assert all(
        emitted["response_key"].startswith("input_requested:")             # SDK's unique input_requested:{uuid}
        for emitted in input_reqs
    )
    assert all(emitted["complete_request"] is False for emitted in input_reqs)

    tagged_chat = HeadlessSageChat()
    tagged_emitter = make_card_emitter(
        tagged_chat,
        req,
        delegation_lookup=lambda: ("mythic_operator:1", "Mythic_Operator"),
    )
    _run(tagged_emitter([{"name": "execute_capability", "args": {"callback": 2}}]))
    tagged = tagged_chat.emissions[0]
    assert tagged["metadata"]["delegation_id"] == "mythic_operator:1"
    assert tagged["metadata"]["delegation_name"] == "Mythic_Operator"


def test_hitl_confirm_flow_input_request_then_resume():
    model = _HitlModel()
    chat = _HitlDriverChat(model)

    # Request N: guarded tool → approval request. This posts an `input_requested` block with
    # complete_request=False (the channel-release), NOT a terminal. Handler returns None.
    _run(chat.chat(build_chat_request("do risky thing", channel_id=5, request_id=1)))
    assert chat.terminal_emissions == []  # released by input_requested, not a complete-terminal
    input_reqs = [e for e in chat.emissions if e.get("metadata", {}).get("special_type") == "input_requested"]
    assert len(input_reqs) == 1
    assert model._pending is True
    paused_contract = model._request_contract

    # Request N+1: operator ACCEPTS → InputResponse(action="accept") → resume APPROVE, one terminal.
    chat.emissions.clear()
    reqN1 = build_chat_request("", channel_id=5, request_id=2)
    reqN1.InputResponse = _input_response_for_context("accept", model._pending_approval_context)
    _run(chat.chat(reqN1))
    assert model._request_contract is paused_contract
    assert model.resumed_with == "approve"
    assert model._pending is False
    assert len(model.approval_claims) == 1
    assert model.approval_claims[0]["request_contract_digest"] == paused_contract.digest
    assert model.approval_claim_clears == 1
    assert model.open_tool_close_statuses == [
        ("cancelled", ("claimed-tool",)),
    ]
    assert len(chat.terminal_emissions) == 1


def test_hitl_reject_resumes_deny():
    """Operator REJECT → InputResponse(action='reject') → resume deny (server now sends a real response)."""
    model = _HitlModel()
    model._pending = True  # an interrupt is already pending on this channel
    chat = _HitlDriverChat(model)
    reqR = build_chat_request("", channel_id=5, request_id=2)
    context = _install_pending_approval_context(model, reqR)
    reqR.InputResponse = _input_response_for_context("reject", context)
    _run(chat.chat(reqR))
    assert model.resumed_with == "deny"
    assert model.approval_claims == []
    assert model.open_tool_close_statuses == [
        ("cancelled", ("claimed-tool",)),
    ]
    assert len(chat.terminal_emissions) == 1


def test_multi_action_native_card_selects_one_and_denies_batch_accept():
    model = _MultiHitlModel()
    chat = _HitlDriverChat(model)
    _run(chat.chat(build_chat_request(
        "Run one approved action.",
        channel_id=105,
        request_id=1,
    )))
    card = next(
        item for item in chat.emissions
        if item.get("metadata", {}).get("special_type") == "input_requested"
    )
    input_request = card["metadata"]["input_requested"]
    assert input_request["input_type"] == "single_choice"
    assert len(input_request["choices"]) == 2
    context = dict(model._pending_approval_context)
    selected_id = input_request["choices"][0]["id"]

    response = build_chat_request("", channel_id=105, request_id=2)
    response.InputResponse = _input_response_for_context(
        "select",
        context,
        choice={"id": selected_id},
    )
    _run(chat.chat(response))
    assert model.resumed_with == "approve"
    assert model.selected_action_ids == (selected_id,)
    assert model.approval_claims[0]["approved_action_ids"] == [selected_id]

    model = _MultiHitlModel()
    chat = _HitlDriverChat(model)
    _run(chat.chat(build_chat_request(
        "Run one approved action.",
        channel_id=107,
        request_id=1,
    )))
    context = dict(model._pending_approval_context)
    response = build_chat_request("", channel_id=107, request_id=2)
    response.InputResponse = _input_response_for_context("accept", context)
    _run(chat.chat(response))
    assert model.resumed_with == "deny"
    assert model.selected_action_ids == ()
    assert model.approval_claims == []


def test_replayed_approval_response_resumes_checkpoint_only_once():
    class _SlowResumeModel(_HitlModel):
        def __init__(self):
            super().__init__()
            self.resume_calls = 0

        async def handle_hitl_resume(
            self,
            decision,
            thread_id,
            operator_message="",
            expected_action_digest="",
            approved_action_ids=None,
        ):
            del expected_action_digest, approved_action_ids
            self.resume_calls += 1
            await asyncio.sleep(0.02)
            self.resumed_with = decision
            self._pending = False
            return ""

    async def scenario():
        model = _SlowResumeModel()
        model._pending = True
        chat = _HitlDriverChat(model)
        first = build_chat_request("", channel_id=106, request_id=1)
        context = _install_pending_approval_context(model, first)
        first.InputResponse = _input_response_for_context("accept", context)
        second = build_chat_request("", channel_id=106, request_id=2)
        second.InputResponse = _input_response_for_context("accept", context)
        await asyncio.gather(chat.chat(first), chat.chat(second))
        return model, chat

    model, chat = _run(scenario())
    assert model.resume_calls == 1
    assert any("no longer active" in str(item.get("content", "")) for item in chat.emissions)


def test_controller_hitl_card_response_resumes_controller_pending_move():
    model = _ControllerHitlModel()
    chat = _HitlDriverChat(model)
    req = build_chat_request("", channel_id=5, request_id=3)
    context = _install_pending_approval_context(model, req)
    req.InputResponse = _input_response_for_context("accept", context)
    _run(chat.chat(req))
    assert model.resumed_with == "approve"
    assert model._controller_hitl_pending is None
    assert len(chat.terminal_emissions) == 1


def test_delayed_approval_for_old_generation_cannot_approve_new_pending_action():
    old_model = _HitlModel()
    old_chat = _HitlDriverChat(old_model)
    _run(old_chat.chat(build_chat_request("old guarded action", channel_id=51, request_id=1)))
    old_context = dict(old_model._pending_approval_context)

    new_model = _HitlModel()
    new_chat = _HitlDriverChat(new_model)
    _run(new_chat.chat(build_chat_request("new guarded action", channel_id=51, request_id=2)))
    assert new_model._pending is True
    assert new_model._pending_approval_context != old_context

    stale = build_chat_request("", channel_id=51, request_id=3)
    stale.InputResponse = _input_response_for_context("accept", old_context)
    new_chat.emissions.clear()
    _run(new_chat.chat(stale))

    assert new_model.resumed_with is None
    assert new_model._pending is True
    assert new_model._pending_approval_context != old_context
    assert new_model.open_tool_close_statuses == []
    assert any("no longer active" in str(item.get("content", "")) for item in new_chat.emissions)


def test_fresh_prompt_replaces_pending_hitl_session_instead_of_resuming_it():
    old_model = _HitlModel()
    old_model._pending = True
    replacement = _FakeModel(stream=("fresh answer",))

    class _RotatingChat(HeadlessSageChat):
        def __init__(self):
            super().__init__()
            self.calls = 0

        async def _get_or_create_model(self, request):
            self.calls += 1
            return (old_model, True) if self.calls == 1 else (replacement, False)

    chat = _RotatingChat()
    _run(chat.chat(build_chat_request("What callbacks do we have?", channel_id=52, request_id=1)))

    assert chat.calls == 2
    assert old_model.resumed_with is None
    assert replacement.invoked_with == ("What callbacks do we have?", False)


def test_fresh_prompt_rotates_session_when_hitl_probe_fails():
    class _ProbeFailureModel(_HitlModel):
        async def _hitl_interrupt_pending(self, thread_id):
            raise RuntimeError("checkpoint unavailable")

    old_model = _ProbeFailureModel()
    replacement = _FakeModel(stream=("fresh answer",))

    class _RotatingChat(HeadlessSageChat):
        def __init__(self):
            super().__init__()
            self.calls = 0

        async def _get_or_create_model(self, request):
            self.calls += 1
            return (old_model, True) if self.calls == 1 else (replacement, False)

    chat = _RotatingChat()
    _run(chat.chat(build_chat_request("What callbacks do we have?", channel_id=54, request_id=1)))

    assert chat.calls == 2
    assert replacement.invoked_with == ("What callbacks do we have?", False)


def test_approval_response_correlation_fails_closed_without_echoed_context():
    model = _HitlModel()
    request = build_chat_request("", channel_id=53, request_id=1)
    expected = _install_pending_approval_context(model, request)
    request.InputResponse = ChatInputResponse(action="accept")

    assert approval_response_matches(request, expected) is False


@pytest.mark.parametrize(
    "field",
    (
        "approval_id",
        "thread_id",
        "turn_id",
        "request_id",
        "request_contract_digest",
        "tool_name",
        "selection_mode",
        "actions",
        "action_digest",
        "proposal_digest",
        "operation_id",
        "apitoken_id",
    ),
)
def test_approval_response_rejects_every_mismatched_binding_field(field):
    model = _HitlModel()
    request = build_chat_request("", channel_id=153, request_id=8)
    expected = _install_pending_approval_context(model, request)
    actual = dict(expected)
    actual[field] = f"{actual[field]}-stale"
    if field == "actions":
        request.InputResponse = _input_response_for_context("accept", expected)
        request.InputResponse.InputRequest["data"]["sage_approval_context"] = actual
    else:
        request.InputResponse = _input_response_for_context("accept", actual)

    assert approval_response_matches(request, expected) is False


def test_approval_response_rejects_action_mutation_even_when_outer_ids_match():
    model = _HitlModel()
    request = build_chat_request("", channel_id=154, request_id=9)
    expected = _install_pending_approval_context(model, request)
    actual = {
        **expected,
        "actions": [
            {
                "name": "execute_capability",
                "args": {"target": "DC02"},
            }
        ],
    }
    request.InputResponse = _input_response_for_context("accept", actual)

    assert approval_response_matches(request, expected) is False


def test_single_action_approval_requires_exact_mythic_empty_choice_catalog():
    model = _HitlModel()
    request = build_chat_request("", channel_id=154, request_id=10)
    expected = _install_pending_approval_context(model, request)
    valid = _input_response_for_context("accept", expected)

    assert valid.InputRequest["choices"] == []
    request.InputResponse = valid
    assert approval_response_matches(request, expected) is True
    assert approved_action_ids_for_request(request, expected)

    malformed = []
    missing = copy.deepcopy(valid)
    del missing.InputRequest["choices"]
    malformed.append(missing)
    nonempty = copy.deepcopy(valid)
    nonempty.InputRequest["choices"] = [{
        "id": "f" * 64,
        "label": "not canonical",
        "description": "",
        "data": {},
    }]
    malformed.append(nonempty)
    unknown_key = copy.deepcopy(valid)
    unknown_key.InputRequest["unexpected"] = True
    malformed.append(unknown_key)

    for response in malformed:
        request.InputResponse = response
        assert approval_response_matches(request, expected) is False
        assert approved_action_ids_for_request(request, expected) == ()


def test_json_number_normalized_echo_matches_and_installs_one_exact_claim():
    model = _HitlModel()
    model._pending = True
    request = build_chat_request("", channel_id=157, request_id=11)
    raw_actions = [{
        "name": "execute_capability",
        "args": {
            "callback_id": "1",
            "identity": "samwell.tarly",
            "policy_decision": {
                "model_branch_coverage": 1.0,
                "nested": [{"count": 2.0}, -0.0],
            },
        },
    }]
    expected = _install_pending_approval_context(
        model,
        request,
        action_requests=raw_actions,
    )
    response = _input_response_for_context("accept", expected)
    encoded_card = json.dumps(
        response.InputRequest,
        sort_keys=True,
        separators=(",", ":"),
    )
    response.InputRequest = json.loads(encoded_card)
    request.InputResponse = response

    normalized_args = expected["actions"][0]["args"]
    assert normalized_args["callback_id"] == "1"
    assert normalized_args["identity"] == "samwell.tarly"
    assert normalized_args["policy_decision"] == {
        "model_branch_coverage": 1,
        "nested": [{"count": 2}, 0],
    }
    assert '"model_branch_coverage":1' in encoded_card
    assert approval_response_matches(request, expected) is True
    approved_ids = approved_action_ids_for_request(request, expected)
    assert approved_ids == (
        approval_action_fingerprint(expected["actions"][0]),
    )

    chat = _HitlDriverChat(model)
    _run(chat.chat(request))
    assert model.resumed_with == "approve"
    assert len(model.approval_claims) == 1
    assert model.approval_claims[0]["approved_action_ids"] == list(approved_ids)
    assert model.approval_claims[0]["approved_actions"] == expected["actions"]

    replay = build_chat_request("", channel_id=157, request_id=12)
    replay.InputResponse = _input_response_for_context("accept", expected)
    _run(chat.chat(replay))
    assert len(model.approval_claims) == 1


def test_json_number_normalized_echo_still_denies_authority_mutations():
    model = _HitlModel()
    request = build_chat_request("", channel_id=158, request_id=13)
    expected = _install_pending_approval_context(
        model,
        request,
        action_requests=[{
            "name": "execute_capability",
            "args": {
                "callback_id": "1",
                "identity": "samwell.tarly",
                "policy_decision": {"model_branch_coverage": 1.0},
            },
        }],
    )
    valid = _input_response_for_context("accept", expected)
    mutations = []
    for field, value in (
        ("callback_id", "2"),
        ("identity", "other.user"),
    ):
        mutated = copy.deepcopy(valid)
        mutated.InputRequest["data"]["sage_approval_context"]["actions"][0][
            "args"
        ][field] = value
        mutations.append(mutated)
    for field, value in (
        ("action_digest", "0" * 64),
        ("request_id", f"{expected['request_id']}-stale"),
    ):
        mutated = copy.deepcopy(valid)
        mutated.InputRequest["data"]["sage_approval_context"][field] = value
        mutations.append(mutated)
    changed_card = copy.deepcopy(valid)
    changed_card.InputRequest["description"] += " changed"
    mutations.append(changed_card)
    changed_choice = copy.deepcopy(valid)
    changed_choice.Choice = {"id": "f" * 64}
    mutations.append(changed_choice)

    for mutated in mutations:
        request.InputResponse = mutated
        assert approval_response_matches(request, expected) is False
        assert approved_action_ids_for_request(request, expected) == ()


def test_multi_action_approval_requires_one_exact_typed_choice():
    request = build_chat_request("", channel_id=155, request_id=10)
    model = _HitlModel()
    single = _install_pending_approval_context(model, request)
    actions = [
        {"name": "execute_capability", "args": {"target": "DC01"}},
        {"name": "add_credential", "args": {"credential": "example"}},
    ]
    action_digest = approval_action_digest(actions)
    expected = {
        **single,
        "actions": actions,
        "selection_mode": "exact_one",
        "action_digest": action_digest,
        "proposal_digest": approval_proposal_digest(
            single["request_contract_digest"],
            action_digest,
        ),
    }
    selected_id = approval_action_fingerprint(actions[1])

    request.InputResponse = _input_response_for_context(
        "select",
        expected,
        choice={"id": selected_id},
    )
    assert approved_action_ids_for_request(request, expected) == (selected_id,)

    for action, choice in (
        ("accept", None),
        ("reject", None),
        ("select", {"id": "0" * 64}),
        ("select", {"id": selected_id + "-stale"}),
    ):
        request.InputResponse = _input_response_for_context(
            action,
            expected,
            choice=choice,
        )
        assert approved_action_ids_for_request(request, expected) == ()


def test_multi_action_selection_rejects_every_malformed_typed_envelope():
    request = build_chat_request("", channel_id=156, request_id=10)
    model = _HitlModel()
    single = _install_pending_approval_context(model, request)
    actions = [
        {"name": "execute_capability", "args": {"target": "DC01"}},
        {"name": "add_credential", "args": {"credential": "example"}},
    ]
    action_digest = approval_action_digest(actions)
    expected = {
        **single,
        "actions": actions,
        "selection_mode": "exact_one",
        "action_digest": action_digest,
        "proposal_digest": approval_proposal_digest(
            single["request_contract_digest"],
            action_digest,
        ),
    }
    selected_id = approval_action_fingerprint(actions[0])
    valid = _input_response_for_context(
        "select",
        expected,
        choice={"id": selected_id},
    )
    request.InputResponse = valid
    assert approval_response_matches(request, expected) is True
    assert approved_action_ids_for_request(request, expected) == (selected_id,)

    malformed = []

    whitespace_id = copy.deepcopy(valid)
    whitespace_choice = dict(whitespace_id.Choice)
    whitespace_choice["id"] = f" {selected_id} "
    whitespace_id.Choice = whitespace_choice
    whitespace_id.InputRequest["response"]["choice"] = whitespace_choice
    malformed.append(whitespace_id)

    wrong_type = copy.deepcopy(valid)
    wrong_type.InputRequest["input_type"] = "approval"
    malformed.append(wrong_type)

    missing_catalog = copy.deepcopy(valid)
    del missing_catalog.InputRequest["choices"]
    malformed.append(missing_catalog)

    duplicate_catalog = copy.deepcopy(valid)
    duplicate_catalog.InputRequest["choices"] = [
        valid.InputRequest["choices"][0],
        valid.InputRequest["choices"][0],
    ]
    malformed.append(duplicate_catalog)

    reversed_catalog = copy.deepcopy(valid)
    reversed_catalog.InputRequest["choices"] = list(
        reversed(reversed_catalog.InputRequest["choices"])
    )
    malformed.append(reversed_catalog)

    conflicting_alias = copy.deepcopy(valid)
    conflicting_alias.Choice["ID"] = "0" * 64
    conflicting_alias.InputRequest["response"]["choice"] = conflicting_alias.Choice
    malformed.append(conflicting_alias)

    conflicting_data = copy.deepcopy(valid)
    conflicting_data.Choice["data"] = {
        "action_id": approval_action_fingerprint(actions[1]),
    }
    conflicting_data.InputRequest["response"]["choice"] = conflicting_data.Choice
    malformed.append(conflicting_data)

    extra_catalog_item = copy.deepcopy(valid)
    extra_catalog_item.InputRequest["choices"].append({
        "id": "f" * 64,
        "label": "extra",
        "description": "extra",
        "data": {"action_id": "f" * 64},
    })
    malformed.append(extra_catalog_item)

    for response in malformed:
        request.InputResponse = response
        assert approval_response_matches(request, expected) is False
        assert approved_action_ids_for_request(request, expected) == ()


# --------------------------------------------------------------------------------------
# Phase 2 P1 — scope preflight gating
# --------------------------------------------------------------------------------------

def test_tools_missing_scope_unknown_gates_nothing():
    from ai.langgraph.mythic_tools import tools_missing_scope
    assert tools_missing_scope(None) == set()  # unknown scopes → don't gate (login fail-closed covers it)


def test_tools_missing_scope_partial_grant():
    from ai.langgraph.mythic_tools import tools_missing_scope
    disabled = tools_missing_scope({"callback.write"})
    assert "execute_capability" not in disabled       # callback.write granted → kept
    assert "create_payload" in disabled                # payload.write not granted → gated
    assert "add_credential" in disabled                # credential.write not granted → gated


def test_tools_missing_scope_full_grant_gates_nothing():
    from ai.langgraph.mythic_tools import tools_missing_scope, SCOPE_REQUIREMENTS
    assert tools_missing_scope(set(SCOPE_REQUIREMENTS.values())) == set()


def test_get_tools_skips_scope_gated():
    from ai.langgraph.mythic_tools import MythicTools
    mt = MythicTools(channel_id=5)
    mt.apply_scope_gating({"callback.write"})
    names = {t.name for t in mt.get_tools(["execute_capability", "create_payload", "list_callbacks"])}
    assert "create_payload" not in names               # gated (no payload.write)
    assert {"execute_capability", "list_callbacks"} <= names


def test_tools_missing_scope_star_gates_nothing():
    from ai.langgraph.mythic_tools import tools_missing_scope
    assert tools_missing_scope({"*"}) == set()  # SCOPE_ALL grants everything


def test_tools_missing_scope_resource_wildcard():
    from ai.langgraph.mythic_tools import tools_missing_scope
    disabled = tools_missing_scope({"callback.*"})   # wildcard grants all callback.* tools
    assert "execute_capability" not in disabled       # callback.write covered by callback.*
    assert "create_payload" in disabled                # payload.write not covered


def test_whoami_scopes_offline_returns_none():
    """No client (offline/degraded) → None → tools_missing_scope gates nothing (fail-closed still applies)."""
    from ai.langgraph.mythic_tools import MythicTools
    assert _run(MythicTools(channel_id=5).whoami_scopes()) is None


def test_whoami_scopes_parses_effective_scopes(monkeypatch):
    """Query the whoami action; prefer effective_scopes (server-expanded). Source: actions.graphql whoamiOutput."""
    from ai.langgraph import mythic_tools as mt

    async def _q(mythic, query):
        assert "whoami" in query and "effective_scopes" in query
        return {"whoami": {"effective_scopes": ["callback.write", "callback.read"], "scopes": ["callback.*"]}}

    monkeypatch.setattr(mt.mythic, "execute_custom_query", _q)
    client = mt.MythicTools(channel_id=5)
    client.client = object()  # non-None → runs the query
    assert _run(client.whoami_scopes()) == {"callback.write", "callback.read"}


def test_whoami_scopes_query_failure_returns_none(monkeypatch):
    from ai.langgraph import mythic_tools as mt

    async def _boom(mythic, query):
        raise RuntimeError("graphql down")

    monkeypatch.setattr(mt.mythic, "execute_custom_query", _boom)
    client = mt.MythicTools(channel_id=5)
    client.client = object()
    assert _run(client.whoami_scopes()) is None  # failure → gate nothing


def test_refresh_auth_context_accepts_only_the_same_token_and_operation():
    chat = HeadlessSageChat()
    req = build_chat_request("hi", channel_id=5, operation_id=2)
    req.APITokenID = 99

    class _Model:
        apitoken_id = req.APITokenID
        operation_id = req.OperationID

    m = _Model()
    _run(chat._refresh_auth_context(m, req))

    req.APITokenID = 100
    with pytest.raises(RuntimeError, match="fresh Sage session"):
        _run(chat._refresh_auth_context(m, req))
    req.APITokenID = m.apitoken_id
    req.OperationID = 3
    with pytest.raises(RuntimeError, match="fresh Sage session"):
        _run(chat._refresh_auth_context(m, req))


def test_token_change_rotates_the_entire_channel_session():
    from sage_chat.session import drop_channel_session

    class _Model:
        def __init__(self):
            self.provider = "test"
            self.model = "test"
            self.apitoken_id = 1
            self.operation_id = 1
            self.stopped = False
            self.closed = []

        def request_stop(self):
            self.stopped = True

        async def _close_all_request_lifecycles(self, status):
            self.closed.append(status)

    model = _Model()
    request = _slash_req("sandbox", "shell id", channel_id=177, request_id=1)
    request.APITokenID = 99
    chat = HeadlessSageChat()
    _run(put_channel_session(request, model))
    try:
        assert _run(chat._rotate_auth_changed_session(request, model)) is None
        assert _run(get_channel_session(request)) is None
    finally:
        _run(drop_channel_session(request, expected_model=model))

    assert model.stopped is True
    assert model.closed == ["stopped"]


def test_operation_change_cannot_reuse_old_model_state():
    class _Model:
        provider = "test"
        model = "test"
        apitoken_id = 1
        operation_id = 1

        def __init__(self):
            self.requested_stop = False
            self.sensitive_cache = {"credential": "old-operation"}

        def request_stop(self):
            self.requested_stop = True

    request = build_chat_request("x", channel_id=178, request_id=1)
    request.APITokenID = 1
    request.OperationID = 2
    model = _Model()
    _run(put_channel_session(request, model))
    try:
        assert _run(HeadlessSageChat()._rotate_auth_changed_session(request, model)) is None
        assert _run(get_channel_session(request)) is None
    finally:
        _run(drop_channel_session(request, expected_model=model))
    assert model.requested_stop is True


# --------------------------------------------------------------------------------------
# Full config-option parity (restored legacy controls plus explicit policy identity).
# --------------------------------------------------------------------------------------

# The complete set the operator saw when "creating Sage" as an agent, minus the per-turn `prompt`
# (which is ChatRequest.Prompt, not config), plus the chat-era `system_prompt`.
# `verbose` is deliberately NOT here — the chat container always runs full-detail (cards ARE the
# verbose view), so there is no operator verbose toggle.
_EXPECTED_CONFIG_OPTIONS = {
    "provider", "model", "mode", "autonomous_solve", "policy_mode", "max_steps",
    "system_prompt", "API_ENDPOINT", "API_KEY",
    "AWS_ACCESS_KEY_ID", "AWS_SECRET_ACCESS_KEY", "AWS_SESSION_TOKEN", "AWS_DEFAULT_REGION",
    # BloodHound MCP connection (added 2026-07-30). Kept in this exact-equality inventory rather
    # than relaxing the assertion to a subset: the point of the set is that every option Mythic
    # renders is deliberate, so an accidental addition should fail here just as a removal does.
    "BLOODHOUND_DOMAIN", "BLOODHOUND_TOKEN_ID", "BLOODHOUND_TOKEN_KEY",
    "BLOODHOUND_PORT", "BLOODHOUND_SCHEME",
}


def test_all_legacy_config_options_restored():
    meta = SAGE_MODELS[0].Metadata
    names = {opt.Name for opt in meta.ConfigurationOptions}
    assert names == _EXPECTED_CONFIG_OPTIONS
    # round-trips through to_json (what actually syncs to Mythic)
    json_names = {o["name"] for o in SAGE_MODELS[0].to_json()["metadata"]["configuration_options"]}
    assert json_names == _EXPECTED_CONFIG_OPTIONS


def test_secrets_are_optional_not_required():
    # Config-first order means nothing is a *required* secret; sensitive keys stay optional fallbacks.
    meta = SAGE_MODELS[0].Metadata
    assert meta.RequiredUserSecrets == []
    assert "API_KEY" in meta.OptionalUserSecrets
    assert "API_ENDPOINT" in meta.OptionalUserSecrets


def test_autonomous_is_boolean_option_and_verbose_removed():
    opts = {o.Name: o for o in SAGE_MODELS[0].Metadata.ConfigurationOptions}
    assert str(opts["autonomous_solve"].Type) == "boolean"
    assert "verbose" not in opts  # removed — chat container is always full-detail


def test_mode_configuration_exposes_three_typed_lanes_with_safe_default():
    opts = {o.Name: o for o in SAGE_MODELS[0].Metadata.ConfigurationOptions}
    mode = opts["mode"]
    assert mode.DefaultValue == "conversation"
    assert {choice.Value for choice in mode.Choices} == {
        "conversation",
        "supervised",
        "auto",
    }


def test_provider_is_choice_dropdown_not_freeform_string():
    """Restored the Mythic-v3 provider dropdown: `provider` is a Choice, not a freeform String. A freeform box
    lets a typo'd provider name through to init_chat_model, which then fails at model init. Values must be the
    ones config.py / _get_base_chat_model actually handle (bedrock special-cased; the rest via init_chat_model)."""
    opts = {o.Name: o for o in SAGE_MODELS[0].Metadata.ConfigurationOptions}
    prov = opts["provider"]
    assert str(prov.Type) == "choice"
    assert prov.DefaultValue == "openai"
    values = {c.Value for c in prov.Choices}
    assert values == {"openai", "bedrock", "anthropic", "ollama"}
    # "bedrock" must be exactly this string so config.py's `if provider == "bedrock"` AWS-quad branch fires
    assert "bedrock" in values
    # round-trips through to_json (what actually syncs to Mythic) with choices intact
    pj = [o for o in SAGE_MODELS[0].to_json()["metadata"]["configuration_options"] if o["name"] == "provider"][0]
    assert pj["type"] == "choice"
    assert {c["value"] for c in pj["choices"]} == {"openai", "bedrock", "anthropic", "ollama"}


def test_provider_and_model_are_adjacent_static_header_chips():
    opts = SAGE_MODELS[0].Metadata.ConfigurationOptions
    provider_index = next(i for i, option in enumerate(opts) if option.Name == "provider")
    model_index = next(i for i, option in enumerate(opts) if option.Name == "model")

    assert model_index == provider_index + 1
    assert opts[provider_index].DisplayAsChip is True
    assert opts[model_index].DisplayAsChip is True


@pytest.mark.parametrize(
    ("config", "expected_mode", "expected_autonomy"),
    (
        ({}, "conversation", False),
        ({"mode": "conversation", "autonomous_solve": "false"}, "conversation", False),
        ({"mode": "conversation", "autonomous_solve": "true"}, "auto", True),
        ({"mode": "invalid", "autonomous_solve": "false"}, "conversation", False),
        ({"mode": "supervised", "autonomous_solve": "false"}, "supervised", False),
        ({"mode": "supervised", "autonomous_solve": "true"}, "supervised", True),
        ({"mode": "auto", "autonomous_solve": "false"}, "auto", True),
        ({"mode": "auto", "autonomous_solve": "true"}, "auto", True),
    ),
)
def test_chat_request_mode_and_autonomous_solve_matrix(config, expected_mode, expected_autonomy):
    kwargs = build_model_kwargs(build_chat_request("hi", config=config))
    assert kwargs["mode"] == expected_mode
    assert kwargs["autonomous_solve"] is expected_autonomy


def test_runtime_routing_drift_forces_recreation_without_mutating_provider_or_model():
    import sage_chat.service as service

    kwargs = build_model_kwargs(build_chat_request("hi"))
    runtime = type("Runtime", (), {
        "mode": kwargs["mode"],
        "_autonomous_solve": kwargs["autonomous_solve"],
        "policy_mode": kwargs["policy_mode"],
        "_max_steps": kwargs["max_steps"],
        "provider": "unchanged-provider",
        "model": "unchanged-model",
    })()

    assert service._runtime_routing_matches(runtime, kwargs) is True
    runtime.mode = "auto"
    assert service._runtime_routing_matches(runtime, kwargs) is False
    assert (runtime.provider, runtime.model) == ("unchanged-provider", "unchanged-model")


def test_policy_mode_defaults_hybrid_and_accepts_hybrid_and_symbolic():
    defaults = build_model_kwargs(build_chat_request("hi"))
    assert defaults["policy_mode"] == "hybrid"
    assert defaults["policy_mode_resolution"] == "default_missing"
    kwargs = build_model_kwargs(build_chat_request("hi", config={"policy_mode": "hybrid"}))
    assert kwargs["policy_mode"] == "hybrid"
    assert kwargs["policy_mode_resolution"] == "explicit_valid"
    kwargs = build_model_kwargs(build_chat_request("hi", config={"policy_mode": "symbolic"}))
    assert kwargs["policy_mode"] == "symbolic"


def test_invalid_explicit_policy_mode_resolves_hybrid_and_records_resolution():
    kwargs = build_model_kwargs(build_chat_request("hi", config={"policy_mode": "automatic"}))
    assert kwargs["policy_mode"] == "hybrid"
    assert kwargs["policy_mode_requested"] == "automatic"
    assert kwargs["policy_mode_resolution"] == "default_invalid"


def test_policy_configuration_exposes_all_three_policy_backends():
    option = next(
        item
        for item in SAGE_MODELS[0].Metadata.ConfigurationOptions
        if item.Name == "policy_mode"
    )

    assert option.DefaultValue == "hybrid"
    assert {choice.Value for choice in option.Choices} == {"llm", "hybrid", "symbolic"}


def test_api_key_and_endpoint_resolve_from_config(monkeypatch):
    # Config field wins over both the user secret and the container env for the sensitive keys.
    monkeypatch.setenv("API_KEY", "env-key")
    monkeypatch.setenv("API_ENDPOINT", "http://env-endpoint")
    req = build_chat_request(
        "hi",
        config={"API_KEY": "cfg-key", "API_ENDPOINT": "http://cfg-endpoint"},
        secrets={"API_KEY": "sec-key"},
    )
    configurable = build_model_kwargs(req)["config"]["configurable"]
    assert configurable["api_key"] == "cfg-key"
    assert configurable["base_url"] == "http://cfg-endpoint"


def test_bedrock_aws_quad_resolves_from_config():
    req = build_chat_request(
        "hi",
        config={
            "provider": "bedrock",
            "AWS_ACCESS_KEY_ID": "AKIA", "AWS_SECRET_ACCESS_KEY": "secret",
            "AWS_SESSION_TOKEN": "token", "AWS_DEFAULT_REGION": "us-west-2",
        },
    )
    configurable = build_model_kwargs(req)["config"]["configurable"]
    assert configurable["aws_access_key_id"] == "AKIA"
    assert configurable["aws_secret_access_key"] == "secret"
    assert configurable["aws_session_token"] == "token"
    assert configurable["region"] == "us-west-2"


# --------------------------------------------------------------------------------------
# Sub-agent card close: operator-stop badge + handback-summary echo into the drill-down
# --------------------------------------------------------------------------------------

class _RecEmitter:
    """Minimal response_emitter double: callable (text egress) + the two card-emit coroutines."""

    def __init__(self):
        self.subagent_calls = []
        self.agent_text_calls = []
        self.tool_use_calls = []
        self.text_sends = []
        self.final_response_calls = []
        self.last_response_key = ""

    async def __call__(self, formatted_message):
        self.text_sends.append(formatted_message)
        return True

    async def emit_subagent_status(self, **kw):
        self.subagent_calls.append(kw)
        return True

    async def emit_agent_text(self, **kw):
        self.agent_text_calls.append(kw)
        return True

    async def emit_tool_use(self, **kw):
        self.tool_use_calls.append(kw)
        return True

    async def emit_final_response(self, **kw):
        self.final_response_calls.append(kw)
        self.last_response_key = f"event:{kw['event_id']}"
        self.text_sends.append(kw["content"])
        return True


def _bare_model_with(emitter, delegations):
    """A Model with just the attributes the close-path touches (no heavy __init__)."""
    from ai.langgraph.model import Model
    m = Model.__new__(Model)
    m._response_emitter = emitter
    m._active_delegations = delegations
    m.verbose = False
    return m


def _begin_request_lifecycle(model, request_id="request:test"):
    from ai.langgraph.request_events import stable_event_id

    model.begin_visibility_turn(
        request_id,
        operator_prompt="operator prompt",
        native_request_id="1",
        logical_request_id=request_id,
    )
    ledger = model._request_event_ledger
    ledger.record_once(
        event_id=stable_event_id(
            request_id,
            "control_transition",
            "contract-installed",
        ),
        kind="control_transition",
        phase="request_installed",
        content="request contract installed",
    )


def test_close_all_delegations_marks_open_cards_stopped():
    """Operator stop closes every still-open card with status 'stopped' (was: stuck 'running')."""
    emitter = _RecEmitter()
    m = _bare_model_with(emitter, {
        "Mythic_Operator": {"id": "mythic_operator:1", "name": "Mythic_Operator", "title": "t",
                            "tool_count": 3, "icon": "MO", "icon_color": "#3B82F6",
                            "final_summary": "", "last_text": ""},
    })

    _run(m._close_all_delegations(status="stopped"))

    assert m._active_delegations == {}                     # all closed, none left running
    assert len(emitter.subagent_calls) == 1
    call = emitter.subagent_calls[0]
    assert call["status"] == "stopped"
    assert call["complete"] is True
    assert call["icon_color"] == "#3B82F6"                 # color still applied on the stopped card


def test_close_delegation_persists_handback_summary_once_via_card_close():
    """Mythic persists terminal card content as the drill-down's final output."""
    emitter = _RecEmitter()
    m = _bare_model_with(emitter, {
        "BloodHound": {"id": "bloodhound:1", "name": "BloodHound", "title": "t", "tool_count": 1,
                       "icon": "BH", "icon_color": "#E5484D",
                       "final_summary": "DONE — ingested job 228.", "last_text": "streamed reasoning"},
    })

    _run(m._close_delegation("BloodHound"))

    assert emitter.subagent_calls[0]["content"] == "DONE — ingested job 228."
    assert emitter.subagent_calls[0]["summary"] == "DONE — ingested job 228."
    assert emitter.agent_text_calls == []


def test_close_delegation_does_not_reecho_streamed_last_text():
    """With no handback summary (only streamed last_text), nothing is re-echoed — last_text was
    already streamed to the drill-down live, so re-emitting it would duplicate the block."""
    emitter = _RecEmitter()
    m = _bare_model_with(emitter, {
        "Generalist": {"id": "generalist:1", "name": "Generalist", "title": "t", "tool_count": 0,
                       "icon": "GN", "icon_color": "#10B981",
                       "final_summary": "", "last_text": "already streamed"},
    })

    _run(m._close_delegation("Generalist"))

    assert emitter.agent_text_calls == []
    assert emitter.subagent_calls[0]["content"] == ""
    assert emitter.subagent_calls[0]["summary"] == "already streamed"


def test_close_delegation_suppresses_explicit_content_already_streamed():
    emitter = _RecEmitter()
    m = _bare_model_with(emitter, {
        "Generalist": {"id": "generalist:1", "name": "Generalist", "title": "t", "tool_count": 0,
                       "icon": "GN", "icon_color": "#10B981",
                       "final_summary": "", "last_text": "Hello! How can I help?"},
    })

    _run(m._close_delegation("Generalist", content="Hello! How can I help?"))

    assert emitter.subagent_calls[0]["content"] == ""
    assert emitter.subagent_calls[0]["summary"] == "Hello! How can I help?"


def test_close_delegation_suppresses_explicit_content_matching_full_streamed_transcript():
    emitter = _RecEmitter()
    m = _bare_model_with(emitter, {
        "BloodHound": {"id": "bloodhound:1", "name": "BloodHound", "title": "t", "tool_count": 0,
                       "icon": "BH", "icon_color": "#E5484D",
                       "final_summary": "", "last_text": "", "streamed_text_chunks": []},
    })

    _run(m._emit_agent_text(
        content="Found two active callbacks.",
        delegation_id="bloodhound:1",
        delegation_name="BloodHound",
    ))
    _run(m._emit_agent_text(
        content="Both are on CASTELBLACK.",
        delegation_id="bloodhound:1",
        delegation_name="BloodHound",
    ))
    _run(m._close_delegation(
        "BloodHound",
        content="Found two active callbacks.\n\nBoth are on CASTELBLACK.",
    ))

    assert [call["content"] for call in emitter.agent_text_calls] == [
        "Found two active callbacks.",
        "Both are on CASTELBLACK.",
    ]
    assert emitter.subagent_calls[0]["content"] == ""
    assert emitter.subagent_calls[0]["summary"] == (
        "Found two active callbacks.\n\nBoth are on CASTELBLACK."
    )


def test_agent_text_sequence_survives_per_request_emitter_replacement():
    chat = HeadlessSageChat()
    m = _bare_model_with(ChatStreamEmitter(chat, build_chat_request("x", request_id=1)), {
        "BloodHound": {
            "id": "bloodhound:request-7:1",
            "name": "BloodHound",
            "title": "Inspect graph",
            "text_seq": 0,
            "streamed_text_chunks": [],
        },
    })

    _run(m._emit_agent_text(
        content="Before approval.",
        delegation_id="bloodhound:request-7:1",
        delegation_name="BloodHound",
    ))
    m._response_emitter = ChatStreamEmitter(chat, build_chat_request("approve", request_id=2))
    _run(m._emit_agent_text(
        content="After approval.",
        delegation_id="bloodhound:request-7:1",
        delegation_name="BloodHound",
    ))

    assert [emission["response_key"] for emission in chat.emissions] == [
        "agent_text:bloodhound:request-7:1:1",
        "agent_text:bloodhound:request-7:1:2",
    ]


def test_request_scope_prevents_delegation_id_reuse_after_restart():
    from ai.langgraph.model import Model

    emitter = _SubagentStatusRecorder()

    first = Model.__new__(Model)
    first._active_delegations = {}
    first._delegation_seq = 0
    first._response_emitter = emitter
    first.begin_visibility_turn("chat:3:request:4")
    _run(first._open_delegation("Generalist", "hello", 1))

    second = Model.__new__(Model)
    second._active_delegations = {}
    second._delegation_seq = 0
    second._response_emitter = emitter
    second.begin_visibility_turn("chat:3:request:5")
    _run(second._open_delegation("Generalist", "hello", 1))

    assert emitter.calls[0]["delegation_id"] == "generalist:chat:3:request:4:1"
    assert emitter.calls[1]["delegation_id"] == "generalist:chat:3:request:5:1"


def test_hitl_continuation_keeps_one_logical_ledger_with_both_operator_inputs():
    model = _bare_model_with(_RecEmitter(), {})
    model.begin_visibility_turn(
        "chat:3:request:4",
        operator_prompt="run bounded action",
        native_request_id="4",
        logical_request_id="logical-request",
    )
    first_ledger = model._request_event_ledger
    model.begin_visibility_turn(
        "chat:3:request:5",
        operator_prompt="approve",
        native_request_id="5",
        logical_request_id="logical-request",
    )

    assert model._request_event_ledger is first_ledger
    operator_rows = [
        row
        for row in model.request_event_transcript()
        if row["kind"] == "operator_input"
    ]
    assert [row["content"] for row in operator_rows] == [
        "run bounded action",
        "approve",
    ]


def test_run_operator_stop_shielded_streams_notice_and_stops_cards():
    """The shielded operator-stop cleanup streams the stop notice AND flips every open card to
    'stopped' — the fix for a mid-run card left stuck on 'running' after the operator hits stop."""
    emitter = _RecEmitter()
    m = _bare_model_with(emitter, {
        "BloodHound": {"id": "bloodhound:1", "name": "BloodHound", "title": "t", "tool_count": 1,
                       "icon": "BH", "icon_color": "#E5484D", "final_summary": "", "last_text": ""},
    })

    _run(m._run_operator_stop_shielded("\n🛑 Session stopped by operator.\n"))

    assert any("stopped by operator" in t for t in emitter.text_sends)   # notice reached egress
    assert all("🛑>" not in t for t in emitter.text_sends)
    assert m._active_delegations == {}                                    # card closed
    assert emitter.subagent_calls[-1]["status"] == "stopped"              # ...as stopped


def test_execute_capability_tool_card_uses_semantic_capability_header():
    emitter = _RecEmitter()
    m = _bare_model_with(emitter, {})
    m._classify_tool_source = lambda _tool_name: "mythic"

    _run(m._emit_tool_use_card(
        tool_call_id="call-capability",
        tool_name="execute_capability",
        status="started",
        complete=False,
        arguments={"action": {"name": "forge-golden-ticket"}, "inputs": {"callback_id": "3"}},
    ))

    assert len(emitter.tool_use_calls) == 1
    call = emitter.tool_use_calls[0]
    assert call["tool_name"] == "forge-golden-ticket"
    assert "Request: execute_capability(" in call["content"]


def test_execution_observer_surfaces_real_callback_command_name():
    emitter = _RecEmitter()
    m = _bare_model_with(emitter, {})
    m._classify_tool_source = lambda _tool_name: "mythic"
    _begin_request_lifecycle(m)
    base_event = {
        "event_id": "mythic-task:3:42",
        "source": "mythic",
        "tool_name": "ticket_cache_purge",
        "callback_id": 3,
        "task_id": 42,
        "parameters": "",
        "capability": "forge-golden-ticket",
        "purpose": "clear stale tickets",
        "activity": {"id": "execution:1", "name": "Execution"},
    }
    _run(m._emit_execution_event({**base_event, "status": "started"}))
    _run(m._emit_execution_event({
        **base_event,
        "status": "completed",
        "result_preview": "Ticket cache purged.",
    }))

    assert [call["status"] for call in emitter.tool_use_calls] == ["started", "completed"]
    assert [call["tool_name"] for call in emitter.tool_use_calls] == ["ticket_cache_purge", "ticket_cache_purge"]
    assert emitter.tool_use_calls[0]["tool_call_id"] == emitter.tool_use_calls[1]["tool_call_id"]
    assert "forge-golden-ticket" in emitter.tool_use_calls[0]["arguments"]
    assert emitter.tool_use_calls[1]["result_preview"] == "Ticket cache purged."
    assert emitter.tool_use_calls[0]["delegation_id"] == "execution:1"
    assert _run(m.finalize_visibility_turn(require_final=False))["failed"] == 0


def test_execution_observer_preserves_raw_large_arguments_and_output():
    emitter = _RecEmitter()
    m = _bare_model_with(emitter, {})
    m.begin_visibility_turn()
    raw_blob = "A" * 6000
    raw_result = '{"token":"operator-secret","blob":"' + raw_blob + '"}'

    _run(m._emit_execution_event({
        "event_id": "mcp:BloodHound:file_upload:9",
        "source": "mcp",
        "tool_name": "file_upload",
        "status": "completed",
        "arguments": {
            "file_bytes_base64": raw_blob,
            "api_key": "operator-secret",
        },
        "result_preview": raw_result,
        "output": raw_result,
    }))

    call = emitter.tool_use_calls[0]
    assert raw_blob in call["arguments"]
    assert "operator-secret" in call["arguments"]
    assert call["result_preview"] == raw_result
    assert call["output"] == raw_result


def test_visibility_reconciliation_surfaces_failed_card_emission():
    class _FailedCardEmitter(_RecEmitter):
        async def emit_tool_use(self, **kw):
            self.tool_use_calls.append(kw)
            return False

    emitter = _FailedCardEmitter()
    m = _bare_model_with(emitter, {})
    _begin_request_lifecycle(m)

    event = {
        "event_id": "mcp:BloodHound:cypher_query:1",
        "source": "mcp",
        "tool_name": "cypher_query",
        "arguments": {"query": "MATCH (n) RETURN n"},
    }
    _run(m._emit_execution_event({**event, "status": "started"}))
    _run(m._emit_execution_event({**event, "status": "completed"}))
    summary = _run(m.finalize_visibility_turn(require_final=False))

    assert summary["ok"] is False
    assert summary["projection_count"] == 0
    assert summary["failed"] == 2
    assert all("projection count=0" in error for error in summary["errors"])
    assert "lifecycle reconciliation failed" in emitter.text_sends[0].lower()


def test_operator_stop_terminalizes_every_open_tool_and_delegation_projection():
    emitter = _RecEmitter()
    model = _bare_model_with(emitter, {})
    model._delegation_seq = 0
    _begin_request_lifecycle(model, "request:stop")
    _run(model._open_delegation("Generalist", "inspect", 1))
    _run(model._emit_tool_use_card(
        tool_call_id="call-1",
        tool_name="list_callbacks",
        status="started",
        complete=False,
        delegation_id=model.current_delegation_id("Generalist"),
        delegation_name="Generalist",
    ))

    _run(model._emit_operator_stop("Session stopped."))

    assert model._request_event_ledger.open_lifecycles() == ()
    report = _run(model.finalize_visibility_turn(require_final=True))
    assert report["ok"] is True
    assert [call["status"] for call in emitter.tool_use_calls] == [
        "started",
        "stopped",
    ]
    assert [call["status"] for call in emitter.subagent_calls if call["complete"]] == [
        "stopped",
    ]
    assert emitter.text_sends == ["Session stopped."]
    transcript = model.request_event_transcript()
    assert transcript[-2]["phase"] == "request_terminal"
    assert transcript[-1]["kind"] == "final_response"


def test_resume_cleanup_terminalizes_only_tools_that_remain_open():
    emitter = _RecEmitter()
    model = _bare_model_with(emitter, {})
    _begin_request_lifecycle(model, "request:resume-tools")

    for call_id in ("approved", "denied"):
        _run(model._emit_tool_use_card(
            tool_call_id=call_id,
            tool_name="issue_task_and_waitfor_task_output",
            status="started",
            complete=False,
        ))
    claimed = model._open_tool_lifecycle_ids()
    _run(model._emit_tool_use_card(
        tool_call_id="approved",
        tool_name="issue_task_and_waitfor_task_output",
        status="completed",
        complete=True,
    ))

    _run(model._close_open_tool_lifecycles(
        status="cancelled",
        event_ids=claimed,
    ))
    _run(model._close_open_tool_lifecycles(
        status="cancelled",
        event_ids=claimed,
    ))

    assert [
        (call["tool_call_id"], call["status"])
        for call in emitter.tool_use_calls
    ] == [
        ("approved", "started"),
        ("denied", "started"),
        ("approved", "completed"),
        ("denied", "cancelled"),
    ]
    assert model._request_event_ledger.open_lifecycles() == ()


def test_resume_cleanup_preserves_tool_opened_by_chained_hitl():
    emitter = _RecEmitter()
    model = _bare_model_with(emitter, {})
    _begin_request_lifecycle(model, "request:chained-hitl")
    _run(model._emit_tool_use_card(
        tool_call_id="old-denied",
        tool_name="issue_task_and_waitfor_task_output",
        status="started",
        complete=False,
    ))
    claimed = model._open_tool_lifecycle_ids()
    _run(model._emit_tool_use_card(
        tool_call_id="new-pending",
        tool_name="issue_task_and_waitfor_task_output",
        status="started",
        complete=False,
    ))

    _run(model._close_open_tool_lifecycles(
        status="cancelled",
        event_ids=claimed,
    ))

    statuses = [
        (call["tool_call_id"], call["status"])
        for call in emitter.tool_use_calls
    ]
    assert statuses == [
        ("old-denied", "started"),
        ("new-pending", "started"),
        ("old-denied", "cancelled"),
    ]
    remaining = model._open_tool_lifecycle_ids()
    assert len(remaining) == 1
    opened = model._request_event_ledger.actual_events(
        event_id=remaining[0],
        kind="tool",
        phase="started",
    )
    assert dict(opened[0].metadata)["tool_call_id"] == "new-pending"


def test_late_subgoal_transition_cannot_follow_request_terminal():
    from ai.langgraph.subgoal_state import assign_and_admit, new_subgoal

    emitter = _RecEmitter()
    model = _bare_model_with(emitter, {})
    _begin_request_lifecycle(model, "request:cancel-race")
    _run(model._emit_operator_stop("Session cancelled.", status="cancelled"))
    before = list(model.request_control_transitions())

    late = assign_and_admit(
        new_subgoal("request:cancel-race", "operator_stop"),
        owner="BloodHound",
        method="transfer_to_BloodHound",
    )
    model._record_subgoal_control_events(late)

    assert model.request_control_transitions() == before
    assert before[-1]["phase"] == "request_terminal"
    assert _run(model.finalize_visibility_turn(require_final=True))["ok"] is True


def test_duplicate_or_conflicting_tool_delivery_cannot_project_twice():
    emitter = _RecEmitter()
    model = _bare_model_with(emitter, {})
    _begin_request_lifecycle(model, "request:duplicate-tool")

    for status in ("started", "started", "completed", "error"):
        _run(model._emit_tool_use_card(
            tool_call_id="call-1",
            tool_name="list_callbacks",
            status=status,
            complete=status != "started",
        ))

    assert [call["status"] for call in emitter.tool_use_calls] == [
        "started",
        "completed",
    ]
    report = _run(model.finalize_visibility_turn(require_final=False))
    assert report["ok"] is True


def test_model_final_response_boundary_is_idempotent_and_reconciles_once():
    model = _bare_model_with(_RecEmitter(), {})
    _begin_request_lifecycle(model, "request:final")
    model.record_request_terminal("complete")

    first = model.record_final_response("done", response_key="assistant:1")
    second = model.record_final_response("duplicate", response_key="assistant:2")
    model.record_final_response_projection(first, response_key="assistant:1")
    model.record_final_response_projection(second, response_key="assistant:2")

    assert first == second
    report = _run(model.finalize_visibility_turn())
    assert report["ok"] is True
    events = model._request_event_ledger.actual_events(
        kind="final_response",
        phase="emitted",
    )
    assert len(events) == 1
    assert events[0].content == "done"


def test_repeated_stop_has_one_lifecycle_owned_final_projection():
    emitter = _RecEmitter()
    model = _bare_model_with(emitter, {})
    _begin_request_lifecycle(model, "request:repeated-stop")

    _run(model._emit_operator_stop("Session stopped."))
    _run(model._emit_operator_stop("Session stopped."))

    assert len(emitter.final_response_calls) == 1
    assert len(emitter.text_sends) == 1
    assert emitter.final_response_calls[0]["event_id"]
    assert _run(model.finalize_visibility_turn(require_final=True))["ok"] is True


def test_failed_final_projection_retries_same_event_once_then_suppresses():
    class _FailOnceChat:
        def __init__(self):
            self.attempts = []

        async def send_text(self, _request, response_key, *, content, metadata):
            self.attempts.append({
                "response_key": response_key,
                "content": content,
                "metadata": metadata,
            })
            if len(self.attempts) == 1:
                raise RuntimeError("transient transport failure")

    chat = _FailOnceChat()
    request = build_chat_request(
        "stop",
        channel_id=12,
        request_id=34,
    )
    emitter = ChatStreamEmitter(chat, request)
    model = _bare_model_with(emitter, {})
    _begin_request_lifecycle(model, "request:fail-once-final")

    _run(model._emit_operator_stop("Session stopped."))
    first_report = _run(model.finalize_visibility_turn(require_final=True))
    _run(model._emit_operator_stop("different retry text is inert"))
    _run(model._emit_operator_stop("already projected"))
    final_report = _run(model.finalize_visibility_turn(require_final=True))

    assert first_report["ok"] is False
    assert len(chat.attempts) == 2
    assert chat.attempts[0] == chat.attempts[1]
    assert chat.attempts[0]["response_key"].startswith("event:final_response:")
    assert chat.attempts[0]["content"] == "Session stopped."
    assert final_report["ok"] is True


def test_concurrent_stop_serializes_failed_then_successful_final_projection():
    class _FailFirstEmitter(_RecEmitter):
        async def emit_final_response(self, **kw):
            self.final_response_calls.append(kw)
            self.last_response_key = f"event:{kw['event_id']}"
            await asyncio.sleep(0)
            return len(self.final_response_calls) > 1

    emitter = _FailFirstEmitter()
    model = _bare_model_with(emitter, {})
    _begin_request_lifecycle(model, "request:concurrent-stop")

    async def scenario():
        await asyncio.gather(
            model._emit_operator_stop("Session stopped."),
            model._emit_operator_stop("Session stopped."),
        )

    _run(scenario())

    assert len(emitter.final_response_calls) == 2
    assert (
        emitter.final_response_calls[0]["event_id"]
        == emitter.final_response_calls[1]["event_id"]
    )
    assert _run(model.finalize_visibility_turn(require_final=True))["ok"] is True


def test_paused_request_cleanup_terminalizes_before_model_is_dropped():
    emitter = _RecEmitter()
    model = _bare_model_with(emitter, {})
    _begin_request_lifecycle(model, "request:paused-hitl")
    model.request_stop = lambda: None
    chat = HeadlessSageChat()

    _run(chat._stop_and_close_request_lifecycles(model, status="stopped"))

    assert len(emitter.final_response_calls) == 1
    assert _run(model.finalize_visibility_turn(require_final=True))["ok"] is True


def test_capability_wait_observer_emits_operator_progress_messages():
    emitter = _RecEmitter()
    m = _bare_model_with(emitter, {})

    base = {
        "trace_id": "capability_command:1",
        "command": "wait_for_seconds",
        "parameters": {
            "seconds": 300,
            "reason": "wait for Group Policy refresh after GPO task write",
        },
    }
    _run(m._emit_capability_command_card({**base, "status": "started"}))
    _run(m._emit_capability_command_card({
        **base,
        "status": "progress",
        "result_preview": "1 minute elapsed; 4 minutes remaining",
    }))
    _run(m._emit_capability_command_card({**base, "status": "completed"}))

    assert "Waiting for propagation" in emitter.text_sends[0]
    assert "sleeping for 5 minutes" in emitter.text_sends[0]
    assert "No operator action is required" in emitter.text_sends[0]
    assert "4 minutes remaining" in emitter.text_sends[1]
    assert "No operator action is required" in emitter.text_sends[1]
    assert "Propagation wait complete" in emitter.text_sends[2]
    assert emitter.tool_use_calls == []


def test_tool_result_is_error_ignores_nested_errors_in_a_listing():
    """A data-listing result (e.g. get_task_history_for_callback) whose records include a historical
    'error' status must NOT tag the call Failed — only the TOP-LEVEL shape signals tool failure."""
    from ai.langgraph.model import _tool_result_is_error
    import json as _json

    listing = _json.dumps([
        {"command_name": "whoami", "status": "success"},
        {"command_name": "net_dclist", "status": "error"},    # a historical task, not this call
        {"command_name": "ls", "status": "completed"},
    ])
    assert _tool_result_is_error(listing) is False                        # the reported false-positive
    # Genuine failures are still detected:
    assert _tool_result_is_error(_json.dumps({"status": "error", "error": "boom"})) is True
    assert _tool_result_is_error("Error: something blew up") is True
    # Top-level success payloads unaffected:
    assert _tool_result_is_error(_json.dumps({"status": "success"})) is False


# --------------------------------------------------------------------------------------
# /state slash command — re-homed engagement-ledger show + edit
# --------------------------------------------------------------------------------------

def test_handle_state_shows_engagement_ledger(monkeypatch):
    """/state (no arg) renders the engagement hop ledger, not just session info."""
    from sage_chat import slash
    from ai.langgraph import engagement_ledger

    ledger = {
        "objective": "compromise CORP",
        "hops": [
            {"id": "da:corp.local", "effect": "da:corp.local", "status": "achieved",
             "evidence": {"mythic_task_id": 31, "callback_id": 1, "result_preview": "dcsync ok"}},
        ],
    }
    async def _resolve(_model, _request):
        return "Operation_Chimera_1"

    monkeypatch.setattr(slash, "_resolve_chat_engagement_id", _resolve)
    monkeypatch.setattr(engagement_ledger, "load", lambda eid=None: {"objective": ledger["objective"],
                                                                     "hops": [dict(h) for h in ledger["hops"]]})
    text = _run(slash._handle_state(None, build_chat_request("x"), ""))
    assert "Operation_Chimera_1" in text          # the engagement, not the channel
    assert "compromise CORP" in text              # objective surfaced
    assert "da:corp.local" in text                # a hop row
    assert "Achieved hops:** 1" in text


def test_handle_state_remove_mutates_and_saves(monkeypatch):
    """/state remove <row> drops the hop and persists via engagement_ledger.save."""
    from sage_chat import slash
    from ai.langgraph import engagement_ledger

    hops = [{"id": "a", "effect": "e1", "status": "x"}, {"id": "b", "effect": "e2", "status": "y"}]
    saved = {}
    async def _resolve(_model, _request):
        return "op"

    monkeypatch.setattr(slash, "_resolve_chat_engagement_id", _resolve)
    monkeypatch.setattr(engagement_ledger, "load", lambda eid=None: {"hops": [dict(h) for h in hops]})
    monkeypatch.setattr(engagement_ledger, "save", lambda d, eid=None: saved.update(data=d) or "path")

    text = _run(slash._handle_state(None, build_chat_request("x"), "remove 1"))
    assert "Removed 1 hop" in text
    assert [h["id"] for h in saved["data"]["hops"]] == ["b"]   # row 1 (1-based) removed


def test_handle_state_set_status_mutates_and_saves(monkeypatch):
    """/state set <row> <status> flips a hop's status and persists it."""
    from sage_chat import slash
    from ai.langgraph import engagement_ledger

    saved = {}
    async def _resolve(_model, _request):
        return "op"

    monkeypatch.setattr(slash, "_resolve_chat_engagement_id", _resolve)
    monkeypatch.setattr(engagement_ledger, "load",
                        lambda eid=None: {"hops": [{"id": "a", "effect": "e1", "status": "achieved"}]})
    monkeypatch.setattr(engagement_ledger, "save", lambda d, eid=None: saved.update(data=d) or "path")

    text = _run(slash._handle_state(None, build_chat_request("x"), "set 1 pending"))
    assert saved["data"]["hops"][0]["status"] == "pending"
    assert "pending" in text


def test_handle_state_set_cannot_promote_to_achieved(monkeypatch):
    from sage_chat import slash
    from ai.langgraph import engagement_ledger

    saved = {}
    async def _resolve(_model, _request):
        return "op"

    monkeypatch.setattr(slash, "_resolve_chat_engagement_id", _resolve)
    monkeypatch.setattr(engagement_ledger, "load",
                        lambda eid=None: {"hops": [{"id": "a", "effect": "e1", "status": "pending"}]})
    monkeypatch.setattr(engagement_ledger, "save", lambda d, eid=None: saved.update(data=d) or "path")

    text = _run(slash._handle_state(None, build_chat_request("x"), "set 1 achieved"))
    assert "cannot promote" in text
    assert saved == {}


def test_state_objective_uses_current_client_engagement_key_and_clears_pending_refinement(monkeypatch):
    from sage_chat import slash
    from ai.langgraph import engagement_ledger

    loads = []
    saves = []

    class _Client:
        _engagement_key = "Operation_Chimera_1_current"

        async def _ensure_engagement_key(self):
            return None

    model = type(
        "_Model",
        (),
        {
            "mythic_client": _Client(),
            "state": {"_pending_objective_refinement": {"objective_text": "stale"}},
        },
    )()

    monkeypatch.setattr(engagement_ledger, "active_engagement_id", lambda: "Operation_Chimera_1_wrong")
    monkeypatch.setattr(
        engagement_ledger,
        "load",
        lambda eid=None: loads.append(eid) or {"engagement_id": eid, "hops": []},
    )
    monkeypatch.setattr(
        engagement_ledger,
        "save",
        lambda data, eid=None: saves.append((eid, dict(data))) or "path",
    )

    text = _run(slash._handle_state(model, build_chat_request("x"), "objective collect graph"))

    assert "Objective updated" in text
    assert loads == ["Operation_Chimera_1_current"]
    assert saves[0][0] == "Operation_Chimera_1_current"
    assert saves[0][1]["objective"] == "collect graph"
    assert model.state["_pending_objective_refinement"] is None


# --------------------------------------------------------------------------------------
# HITL Respond/Select steering (Phase 3)
# --------------------------------------------------------------------------------------

def test_hitl_respond_select_steer_deny_and_carry_text():
    """Respond/Select must NEVER approve the guarded action (safety) — they map to deny AND surface the
    operator's free-text as the steering message. Accept → approve; Reject → deny with no steer text."""
    from sage_chat.hitl import resume_decision_for_request, resume_steer_message_for_request

    class _IR:
        def __init__(self, action, response=""):
            self.Action, self.Response = action, response

    class _Req:
        def __init__(self, ir):
            self.InputResponse = ir

    for act in ("respond", "select"):
        req = _Req(_IR(act, "use aes256, not rc4"))
        assert resume_decision_for_request(req) == "deny"                       # never blind-run
        assert resume_steer_message_for_request(req) == "use aes256, not rc4"   # guidance carried

    accept = _Req(_IR("accept", "ignored"))
    assert resume_decision_for_request(accept) == "approve"
    assert resume_steer_message_for_request(accept) == ""

    reject = _Req(_IR("reject"))
    assert resume_decision_for_request(reject) == "deny"
    assert resume_steer_message_for_request(reject) == ""

    class _Bare:
        InputResponse = None
    assert resume_decision_for_request(_Bare()) == "deny"                        # default-deny, safe
    assert resume_steer_message_for_request(_Bare()) == ""


# --------------------------------------------------------------------------------------
# Phase 2 header chips — live channel metadata
# --------------------------------------------------------------------------------------

def test_mythic_tools_preauth_client_adopts_without_mint():
    """Headless/eval auth (Option A): a preauth_client is adopted by login() directly — no channel/task
    context, no token mint — so the in-process gauge solve authenticates from the harness's admin client."""
    from ai.langgraph.mythic_tools import MythicTools

    sentinel = object()
    mt = MythicTools(preauth_client=sentinel)   # no channel_id, no agent_task_id
    _run(mt.login())
    assert mt.client is sentinel                # adopted as-is; the mint branches were skipped


def test_headless_solver_imports_and_wraps():
    """The in-process solve seam imports and exposes the harness-facing entry points."""
    from ai.hillclimb import headless_solver
    assert callable(headless_solver.run_headless_solve)
    assert callable(headless_solver.solve_headless)


def test_make_headless_solver_routes_to_in_process_solve(monkeypatch):
    """The alongside headless harness builder returns the same solve(objective)->status contract and
    routes into run_headless_solve with the injected client + pinned engagement id."""
    from ai.hillclimb import live_seams, headless_solver

    calls = {}

    async def _fake_run(objective, **kw):
        calls.update(objective=objective, **kw)
        return "completed"

    monkeypatch.setattr(headless_solver, "run_headless_solve", _fake_run)
    solve = live_seams.make_headless_solver("CLIENT", engagement_id="Operation_Chimera_1",
                                            operation_id=7, timeout=99, max_steps=0, policy_mode="symbolic",
                                            provider="openai", model="test-model",
                                            api_endpoint="http://127.0.0.1:8100/v1", api_key="route-key")
    assert solve("compromise CORP") == "completed"
    assert calls["objective"] == "compromise CORP"
    assert calls["client"] == "CLIENT"
    assert calls["engagement_id"] == "Operation_Chimera_1"
    assert calls["operation_id"] == 7
    assert calls["policy_mode"] == "symbolic"
    assert calls["provider"] == "openai"
    assert calls["model"] == "test-model"
    assert calls["api_endpoint"] == "http://127.0.0.1:8100/v1"
    assert calls["api_key"] == "route-key"
    assert calls["return_details"] is True


def test_headless_solver_builds_model_config_from_explicit_route(monkeypatch):
    """Headless runs bypass chat metadata, so they must still carry the selected route into Model config."""
    from ai.hillclimb import headless_solver

    captured = {}

    class FakeModel:
        def __init__(self, **kwargs):
            captured.update(kwargs)
            self.llm = object()

        async def initialize(self):
            return None

        async def invoke(self, _objective):
            return None

        def controller_runtime_telemetry(self):
            return {}

    monkeypatch.setattr("ai.langgraph.model.Model", FakeModel)
    result = _run(headless_solver.run_headless_solve(
        "objective",
        client=object(),
        operation_id=1,
        engagement_id="headless-route",
        provider="openai",
        model="test-model",
        api_endpoint="http://127.0.0.1:8100/v1",
        api_key="route-key",
        return_details=True,
    ))

    assert result["status"] == "completed"
    assert captured["config"] == {
        "configurable": {
            "api_key": "route-key",
            "base_url": "http://127.0.0.1:8100/v1",
        },
    }


def test_headless_solver_connects_bloodhound_before_model_initialize(monkeypatch):
    """Headless runs must mirror native chat's BloodHound-before-graph-construction ordering."""
    from ai import bloodhound_config
    from ai.hillclimb import headless_solver

    events = []

    async def _ensure(**_kwargs):
        events.append("bloodhound")
        return True, "connected"

    class FakeModel:
        def __init__(self, **_kwargs):
            self.llm = object()

        async def initialize(self):
            events.append("initialize")

        async def invoke(self, _objective):
            return None

        def controller_runtime_telemetry(self):
            return {}

    monkeypatch.setattr(bloodhound_config, "ensure_bloodhound_connected", _ensure)
    monkeypatch.setattr("ai.langgraph.model.Model", FakeModel)
    result = _run(headless_solver.run_headless_solve(
        "objective",
        client=object(),
        operation_id=1,
        engagement_id="headless-bloodhound",
        provider="openai",
        model="test-model",
        return_details=True,
    ))

    assert result["status"] == "completed"
    assert events == ["bloodhound", "initialize"]


def test_make_native_chat_solver_creates_treatment_channel(monkeypatch):
    """Explicit model treatments must bypass the default prepared channel and travel in channel metadata."""
    import sys
    import types

    from ai.hillclimb import live_seams

    calls = {}

    async def _fake_run(client, objective, **kwargs):
        calls.update(client=client, objective=objective, **kwargs)
        return {"status": "completed"}

    fake_native_chat = types.SimpleNamespace(
        default_ai_metadata=lambda: {
            "config": {
                "provider": "openai",
                "model": "default-model",
                "API_ENDPOINT": "http://proxy/v1",
                "API_KEY": "secret",
            },
        },
        run_native_chat_turn=_fake_run,
    )
    monkeypatch.setitem(sys.modules, "native_chat", fake_native_chat)

    solve = live_seams.make_native_chat_solver(
        "CLIENT",
        provider="openai",
        model="bedrock-claude-4-6-sonnet",
    )

    assert solve("objective") == "completed"
    assert calls["use_prepared_channel"] is False
    assert calls["metadata"]["config"]["provider"] == "openai"
    assert calls["metadata"]["config"]["model"] == "bedrock-claude-4-6-sonnet"
    assert calls["metadata"]["config"]["API_ENDPOINT"] == "http://proxy/v1"


def test_make_native_chat_solver_creates_eval_override_channel_without_model_treatment(monkeypatch):
    """A post-reset exact-target fixture must travel in fresh channel metadata, not child-process env only."""
    import sys
    import types

    from ai.hillclimb import live_seams

    calls = {}

    async def _fake_run(client, objective, **kwargs):
        calls.update(client=client, objective=objective, **kwargs)
        return {"status": "completed"}

    fake_native_chat = types.SimpleNamespace(
        default_ai_metadata=lambda: {
            "config": {
                "provider": "openai",
                "model": "default-model",
                "API_ENDPOINT": "http://proxy/v1",
                "API_KEY": "secret",
            },
        },
        run_native_chat_turn=_fake_run,
    )
    monkeypatch.setitem(sys.modules, "native_chat", fake_native_chat)
    raw = '[{"capability":"read-managed-local-admin-secret","exact_target":"target=blue-ops01"}]'

    solve = live_seams.make_native_chat_solver(
        "CLIENT",
        eval_force_capability_prefix_json=raw,
    )

    assert solve("objective") == "completed"
    assert calls["use_prepared_channel"] is False
    assert calls["metadata"]["config"]["provider"] == "openai"
    assert calls["metadata"]["config"]["model"] == "default-model"
    assert calls["metadata"]["config"]["SAGE_EVAL_FORCE_CAPABILITY_PREFIX_JSON"] == raw


def test_build_channel_metadata_live_counts(monkeypatch):
    """The live header chips reflect MCP tool/server counts, session rounds, and BloodHound state."""
    from sage_chat.metadata import build_channel_metadata
    from ai import mcp

    monkeypatch.setattr(mcp.MCPManager, "get_tools_summary",
                        lambda: {"total_tools": 13, "connected_servers": 1}, raising=False)
    monkeypatch.setattr(mcp.MCPManager, "get_connected_servers", lambda: ["BloodHound"], raising=False)
    monkeypatch.setattr(mcp.MCPManager, "is_bloodhound_server", lambda name: name == "BloodHound", raising=False)

    class _M:
        _global_step_count = 7
        model = "claude-sonnet-5"
        mode = "auto"
        _autonomous_solve = True
        policy_mode = "llm"
        _active_agent_label = "Controller"

    items = {i["key"]: i for i in build_channel_metadata(_M())["items"]}
    assert items["mcp_tools"]["value"] == 13
    assert items["mcp_servers"]["value"] == 1
    assert items["rounds"]["value"] == 7
    assert items["active_agent"]["value"] == "Controller"
    assert items["bloodhound"]["value"] is True
    assert items["bloodhound"]["display_value"] == "connected"
    assert "mythic_tools" in items                       # scope-usable Mythic tool count present
    # Policy leads the operational controls; state-aware colors make the three controls distinguishable.
    assert "cfg_model" not in items
    assert items["cfg_policy"]["value"] == "llm" and items["cfg_policy"]["color"] == "success"
    assert items["cfg_policy"]["order"] < items["cfg_mode"]["order"] < items["cfg_autonomous"]["order"]
    assert items["cfg_mode"]["value"] == "auto" and items["cfg_mode"]["color"] == "warning"
    assert items["cfg_autonomous"]["display_value"] == "on" and items["cfg_autonomous"]["color"] == "warning"


def test_build_channel_metadata_rounds_accumulate_across_controller_resets(monkeypatch):
    from sage_chat.metadata import build_channel_metadata
    from ai import mcp

    monkeypatch.setattr(mcp.MCPManager, "get_tools_summary", lambda: {}, raising=False)
    monkeypatch.setattr(mcp.MCPManager, "get_connected_servers", lambda: [], raising=False)

    class _M:
        _global_step_count = 2
        _policy_model_calls = 3

    model = _M()
    items = {item["key"]: item for item in build_channel_metadata(model)["items"]}
    assert items["rounds"]["value"] == 5

    model._policy_model_calls = 0
    build_channel_metadata(model)
    model._policy_model_calls = 2
    items = {item["key"]: item for item in build_channel_metadata(model)["items"]}
    assert items["rounds"]["value"] == 7


def test_channel_metadata_default_control_colors_are_distinct(monkeypatch):
    from sage_chat.metadata import build_channel_metadata
    from ai import mcp

    monkeypatch.setattr(mcp.MCPManager, "get_tools_summary", lambda: {}, raising=False)
    monkeypatch.setattr(mcp.MCPManager, "get_connected_servers", lambda: [], raising=False)

    class _M:
        model = "test"
        mode = "supervised"
        _autonomous_solve = False
        policy_mode = "llm"

    items = {item["key"]: item for item in build_channel_metadata(_M())["items"]}
    colors = [
        items["cfg_policy"]["color"],
        items["cfg_mode"]["color"],
        items["cfg_autonomous"]["color"],
    ]
    assert colors == ["success", "info", "neutral"]
    assert len(set(colors)) == 3


def test_channel_metadata_labels_hybrid_policy_distinctly(monkeypatch):
    from sage_chat.metadata import build_channel_metadata
    from ai import mcp

    monkeypatch.setattr(mcp.MCPManager, "get_tools_summary", lambda: {}, raising=False)
    monkeypatch.setattr(mcp.MCPManager, "get_connected_servers", lambda: [], raising=False)

    class _M:
        mode = "supervised"
        _autonomous_solve = False
        policy_mode = "hybrid"

    items = {item["key"]: item for item in build_channel_metadata(_M())["items"]}
    assert items["cfg_policy"]["value"] == "hybrid"
    assert items["cfg_policy"]["color"] == "info"


def test_scope_usable_mythic_tools_reflects_disabled():
    """Scope-usable Mythic-tools = the declared universe minus the token's scope-gated tools."""
    from sage_chat.metadata import scope_usable_mythic_tools, _mythic_tool_universe

    universe = _mythic_tool_universe()
    assert len(universe) > 0                             # real frontmatter has Mythic tasking tools

    one = next(iter(universe))
    class _Client:
        disabled_tools = {one}
    class _M:
        mythic_client = _Client()
    assert scope_usable_mythic_tools(_M()) == len(universe) - 1          # a gated Mythic tool drops the count

    class _Client2:
        disabled_tools = {"not_a_real_mythic_tool_xyz"}
    class _M2:
        mythic_client = _Client2()
    assert scope_usable_mythic_tools(_M2()) == len(universe)             # gating a non-Mythic name is a no-op

    assert scope_usable_mythic_tools(object()) == len(universe)          # no client → full universe


def test_build_channel_metadata_degrades_safely(monkeypatch):
    """A header must never break a turn: MCP lookups failing → counts fall back to 0/off, no raise."""
    from sage_chat.metadata import build_channel_metadata
    from ai import mcp

    def _boom(*a, **k):
        raise RuntimeError("mcp down")

    monkeypatch.setattr(mcp.MCPManager, "get_tools_summary", _boom, raising=False)
    monkeypatch.setattr(mcp.MCPManager, "get_connected_servers", _boom, raising=False)

    items = {i["key"]: i for i in build_channel_metadata(object())["items"]}
    assert items["mcp_tools"]["value"] == 0
    assert items["mcp_servers"]["value"] == 0
    assert items["rounds"]["value"] == 0
    assert items["bloodhound"]["value"] is False
