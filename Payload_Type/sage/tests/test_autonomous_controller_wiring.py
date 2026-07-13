"""Wiring tests for Model._run_autonomous_controller (the runtime seam adapters).

Run: cd Payload_Type/sage && python3 -m pytest tests/test_autonomous_controller_wiring.py -q

These prove the RUNTIME boundary the controller's own unit tests (which use dict-returning fakes) cannot:
the real `execute_capability` returns a JSON *string*, and the seam adapters must build a dict payload/inputs
from a CapabilityAction and parse the string result as a real outcome — NOT coerce a failure into a silent
success (Forge finding #1). We instantiate a bare Model via object.__new__ and inject only the attributes the
method touches, so no live Mythic/RabbitMQ is needed.
"""
import asyncio
import json
import re
import sys
from pathlib import Path

import pytest


async def _nosleep(*_a, **_k):
    return None

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from ai.langgraph import model  # noqa: E402
from ai.langgraph import capabilities  # noqa: E402
from ai.langgraph import engagement_state as es  # noqa: E402
from ai.langgraph import mythic_tools as mt  # noqa: E402


def _state_with_remote_exec():
    """A real EngagementState where remote-exec:braavos@essos is achieved (GOAD literals are fine in TESTS) ->
    the real frontier offers adcs-ca-private-key-export."""
    foothold = es.Foothold(callback_id="3", agent="apollo", host="braavos", forest="essos.local",
                           identity="essos\\administrator", integrity="high", alive=True,
                           source="test", timestamp="")
    hop = es.Hop(id="h", technique="capability:execute-as-local-admin", target="braavos",
                 effect="remote-exec:braavos@essos.local", status="achieved", evidence={},
                 preconditions=[], satisfied_effects=["remote-exec:braavos@essos.local"],
                 source="test", timestamp="")
    return es.EngagementState(objective="obtain administrative control of essos.local",
                             footholds=[foothold], hops=[hop], graph_facts=[])


def _bare_model(execute_return, state, calls):
    m = object.__new__(model.Model)

    class FakeMythic:
        async def execute_capability(self, payload, inputs):
            calls.append((payload, inputs))
            return execute_return

    m.mythic_client = FakeMythic()

    async def _observe():
        return state
    m._build_current_engagement_state = _observe
    m._objective_completion_report = lambda require_autonomous=False: None
    m._format_message_for_streaming = lambda msg, agent_name=None: getattr(msg, "content", "")

    async def _stream(_text):
        return True
    m._stream_message_to_mythic = _stream
    return m


def test_string_capability_failure_flows_to_blocked_not_silent_success():
    """THE wiring C2 proof: execute_capability returns a JSON STRING failure; the adapters + controller must
    parse it as a blocker and reach halted_blocked — never coerce it to a silent success."""
    calls = []
    blocked_string = json.dumps({"ok": False, "verdict": "blocked", "capability": "adcs-ca-private-key-export",
                                 "reason": "CA host enumeration failed",
                                 "suggested_capability": "adcs-esc-certificate-enroll"})
    m = _bare_model(blocked_string, _state_with_remote_exec(), calls)

    report = asyncio.run(m._run_autonomous_controller("obtain administrative control of essos.local"))

    # the real seam adapters fired: a dict payload + dict inputs were built from the CapabilityAction
    assert calls, "execute_capability was never called"
    payload, inputs = calls[0]
    assert isinstance(payload, dict) and payload.get("name") == "adcs-ca-private-key-export", payload
    assert isinstance(inputs, dict)
    # the STRING result was parsed as a FAILURE (not coerced to success) -> clean terminal blocker
    assert "halted_blocked" in report, report
    assert "adcs-ca-private-key-export" in report
    telemetry = m.controller_runtime_telemetry()
    assert telemetry["controller_cycle_count"] == len(telemetry["controller_cycles"])
    assert telemetry["controller_cycles"][0]["action"] == "adcs-ca-private-key-export"
    assert telemetry["controller_cycles"][0]["ok"] is False


def test_observe_none_halts_cleanly_without_crash():
    """A failed observe (real method returns None on any error) must produce a clean halt, not a traceback."""
    calls = []
    m = _bare_model(json.dumps({"ok": True}), None, calls)
    report = asyncio.run(m._run_autonomous_controller("obtain administrative control of essos.local"))
    assert "halted_no_action" in report, report
    assert calls == []  # never executed anything without a state


def test_llm_policy_decision_is_attached_to_capability_inputs():
    calls = []
    blocked_string = json.dumps({
        "ok": False,
        "verdict": "blocked",
        "capability": "adcs-ca-private-key-export",
        "reason": "stop after provenance probe",
    })
    m = _bare_model(blocked_string, _state_with_remote_exec(), calls)
    m.policy_mode = "llm"
    m.provider = "test"
    m.model = "selector"

    class FakeLLM:
        async def ainvoke(self, messages):
            request = json.loads(messages[-1].content)
            assert request["selection_contract"] == "semantic_catalog"
            assert "candidates" not in request
            return type("Response", (), {
                "content": json.dumps({
                    "disposition": "select",
                    "capability": "adcs-ca-private-key-export",
                    "rationale": "selected from normalized state and the capability catalog",
                })
            })()

    m.llm = FakeLLM()
    asyncio.run(m._run_autonomous_controller("obtain administrative control of essos.local"))

    payload, inputs = calls[0]
    decision = inputs["policy_decision"]
    assert decision["policy_mode"] == "llm"
    assert decision["selected_capability"] == "adcs-ca-private-key-export"
    assert decision["decision_id"].startswith("decision-")
    assert payload["intent"]["policy_decision"]["decision_id"] == decision["decision_id"]
    telemetry = m.controller_runtime_telemetry()
    assert telemetry["policy_mode"] == "llm"
    assert telemetry["model_provider"] == "test"
    assert telemetry["model_id"] == "selector"
    assert telemetry["model_calls"] == 2
    assert telemetry["semantic_transaction_count"] == 2
    assert telemetry["authorized_transaction_count"] == 2
    assert telemetry["semantic_policy_coverage"] == 1.0


@pytest.mark.parametrize("policy_mode", ["llm", "hybrid"])
def test_controller_resume_executes_exact_approved_action_without_second_model_decision(policy_mode):
    calls = []
    events = []
    state = _state_with_remote_exec()
    m = _bare_model(
        json.dumps({
            "ok": False,
            "verdict": "blocked",
            "capability": "adcs-ca-private-key-export",
            "reason": "stop after approved replay",
        }),
        state,
        calls,
    )
    m.mode = "supervised"
    m.command_name = "chat"
    m._autonomous_solve = True
    m.policy_mode = policy_mode
    m.provider = "test"
    m.model = "selector"
    m._controller_hitl_pending = None
    m._controller_hitl_objective = state.objective

    original_execute = m.mythic_client.execute_capability

    async def ordered_execute(payload, inputs):
        events.append("execute")
        return await original_execute(payload, inputs)

    m.mythic_client.execute_capability = ordered_execute

    class RecordingLLM:
        async def ainvoke(self, _messages):
            events.append("llm")
            return type("Response", (), {
                "content": json.dumps({
                    "disposition": "stop",
                    "rationale": "stop after the approved replay",
                })
            })()

    m.llm = RecordingLLM()
    action = capabilities.actions_from_state(state)[0]
    payload = model._capability_action_payload(action)
    inputs = model._autonomous_capability_inputs(action, state)
    payload["intent"]["policy_decision"] = {"decision_id": "original"}
    inputs["policy_decision"] = {"decision_id": "original"}
    pending = m._controller_hitl_capability_request(payload, inputs, state.objective)
    m._controller_hitl_approved_pending = pending
    m._controller_hitl_approved_key = pending["key"]

    report = asyncio.run(m._run_autonomous_controller(state.objective))

    assert len(calls) == 1
    assert calls[0][0]["name"] == action.name
    assert calls[0][0]["target"] == action.target
    assert events == ["execute", "llm"]
    assert "halted_no_action" in report
    telemetry = m.controller_runtime_telemetry()
    assert telemetry["policy_mode"] == policy_mode
    assert telemetry["configured_policy_mode"] == policy_mode
    assert telemetry["policy_identity_valid"] is True
    assert telemetry["policy_switches"] == []


def test_supervised_denied_pending_selection_keeps_backend_provenance_telemetry():
    calls = []
    m = _bare_model(json.dumps({"ok": True}), _state_with_remote_exec(), calls)
    m.mode = "supervised"
    m.command_name = "chat"
    m._autonomous_solve = True
    m.policy_mode = "llm"
    m.provider = "configured-provider"
    m.model = "configured-model"
    m._controller_hitl_pending = None
    m._controller_hitl_approved_key = ""
    m._controller_hitl_approved_pending = None
    m._controller_hitl_objective = ""
    m._write_hitl_audit = lambda *_args, **_kwargs: None

    class FakeLLM:
        async def ainvoke(self, _messages):
            return type("Response", (), {
                "content": json.dumps({
                    "disposition": "select",
                    "capability": "adcs-ca-private-key-export",
                    "rationale": "select the only admissible action",
                }),
                "response_metadata": {
                    "model_provider": "runtime-provider",
                    "model_name": "runtime-model",
                },
            })()

    m.llm = FakeLLM()
    assert asyncio.run(m._run_autonomous_controller(_state_with_remote_exec().objective)) == ""
    assert calls == []

    telemetry = m.controller_runtime_telemetry()
    assert telemetry["model_calls"] == 1
    assert telemetry["semantic_transaction_count"] == 0
    assert telemetry["backend_provenance_complete"] is True
    assert telemetry["effective_backend_requests"] == [{
        "decision_id": telemetry["decisions"][0]["decision_id"],
        "policy_mode": "llm",
        "effective_backend": "runtime-provider:runtime-model",
        "effective_model_provider": "runtime-provider",
        "effective_model_id": "runtime-model",
        "backend_provenance_source": "response_metadata.model_name",
        "response_metadata": {
            "model_name": "runtime-model",
            "model_provider": "runtime-provider",
        },
    }]

    assert asyncio.run(m.handle_controller_hitl_resume("deny")) == ""
    telemetry = m.controller_runtime_telemetry()
    assert telemetry["controller_status"] == "halted_denied"
    assert telemetry["controller_terminal_reason"] == "operator denied adcs-ca-private-key-export"
    assert telemetry["model_calls"] == 1
    assert telemetry["effective_backends"] == ["runtime-provider:runtime-model"]
    assert telemetry["backend_provenance_complete"] is True


def test_verbose_controller_streams_progress_before_terminal_report():
    """Verbose controller progress is visible as Sage-owned execution updates, not a second agent persona."""
    calls = []
    blocked_string = json.dumps({"ok": False, "verdict": "blocked", "capability": "adcs-ca-private-key-export",
                                 "reason": "CA host enumeration failed"})
    m = _bare_model(blocked_string, _state_with_remote_exec(), calls)
    m.verbose = True
    streamed = []

    async def _stream(text):
        streamed.append(text)
        return True

    m._stream_message_to_mythic = _stream
    report = asyncio.run(m._run_autonomous_controller("obtain administrative control of essos.local"))

    assert streamed[-1] == report
    assert any("**Execution started**" in item for item in streamed[:-1]), streamed
    assert any("**Selected action**" in item and "adcs-ca-private-key-export" in item for item in streamed[:-1]), streamed
    assert any("**Executing action**" in item and "adcs-ca-private-key-export" in item for item in streamed[:-1]), streamed
    assert any("**Verification**" in item for item in streamed[:-1]), streamed
    assert all("Autonomous_Controller" not in item for item in streamed), streamed
    assert "Autonomous controller" not in report


def test_non_verbose_controller_only_streams_terminal_report():
    """Verbose-off behavior stays quiet: controller internals do not leak into normal parent-task output."""
    calls = []
    blocked_string = json.dumps({"ok": False, "verdict": "blocked", "capability": "adcs-ca-private-key-export",
                                 "reason": "CA host enumeration failed"})
    m = _bare_model(blocked_string, _state_with_remote_exec(), calls)
    m.verbose = False
    streamed = []

    async def _stream(text):
        streamed.append(text)
        return True

    m._stream_message_to_mythic = _stream
    report = asyncio.run(m._run_autonomous_controller("obtain administrative control of essos.local"))

    assert streamed == [report]
    assert "Autonomous controller" not in report


def test_supervised_controller_deny_is_sage_owned_not_controller_prefixed():
    """A denied controller-native approval is surfaced as Sage stopping, not as a second chat speaker."""
    calls = []
    m = _bare_model(json.dumps({"ok": True}), _state_with_remote_exec(), calls)
    m.mode = "supervised"
    m.command_name = "chat"
    m._autonomous_solve = True
    m._controller_hitl_pending = {
        "tool": "execute_capability",
        "args": {},
        "objective": "obtain administrative control of essos.local",
        "key": "pending-key",
    }
    m._controller_hitl_objective = "obtain administrative control of essos.local"
    m._supervised_objective_active = False
    streamed = []

    async def _stream(text):
        streamed.append(text)
        return True

    m._stream_message_to_mythic = _stream
    m._write_hitl_audit = lambda *_args, **_kwargs: None

    assert asyncio.run(m.handle_controller_hitl_resume("deny")) == ""
    assert calls == []
    assert streamed == [
        "**Execution stopped**\n"
        "Operator denied `execute_capability`. Sage stopped before execution.\n"
    ]
    assert "Autonomous_Controller" not in streamed[0]


def test_supervised_chat_controller_pauses_before_execute_capability():
    """Controller-native HITL must escape the controller loop before the real capability seam fires."""
    calls = []
    m = _bare_model(json.dumps({"ok": True}), _state_with_remote_exec(), calls)
    m.mode = "supervised"
    m.command_name = "chat"
    m._autonomous_solve = True
    m.verbose = False
    streamed = []

    async def _stream(text):
        streamed.append(text)
        return True

    m._stream_message_to_mythic = _stream
    report = asyncio.run(m._run_autonomous_controller("obtain administrative control of essos.local"))

    assert report == ""
    assert calls == []
    assert m._controller_hitl_pending["tool"] == "execute_capability"
    assert m._controller_hitl_pending["args"]["capability"]["name"] == "adcs-ca-private-key-export"
    assert any("Approval required" in item for item in streamed), streamed


def test_gate_defaults_on_for_auto_and_supervised_chat_but_not_query_or_interactive():
    """Controller-native HITL allows supervised autonomous chat, while query remains one-shot and interactive
    replies are routed through the pending-approval resume path instead of starting a fresh controller run."""
    import os
    saved_controller = os.environ.get("SAGE_AUTONOMOUS_CONTROLLER")
    saved_hitl = os.environ.get("SAGE_CONTROLLER_HITL")
    os.environ.pop("SAGE_AUTONOMOUS_CONTROLLER", None)
    os.environ.pop("SAGE_CONTROLLER_HITL", None)
    try:
        m = object.__new__(model.Model)
        m._autonomous_solve = True
        m.command_name = "chat"
        # supervised autonomous chat -> controller-native HITL
        m.mode = "supervised"
        assert m._should_use_controller(is_interactive=False) is True
        # supervised query has no interactive approval transport -> legacy graph path
        m.command_name = "query"
        assert m._should_use_controller(is_interactive=False) is False
        # auto mode but interactive follow-up -> fall through to normal path
        m.mode = "auto"
        assert m._should_use_controller(is_interactive=True, prompt="compromise the corp domain") is False
        # auto + non-interactive + an EXPLICIT OBJECTIVE -> run the controller
        assert m._should_use_controller(is_interactive=False, prompt="compromise the corp domain") is True
        # auto + non-interactive + a GREETING/non-objective -> DEFAULT-DENY, do NOT initiate.
        # (bug-1 fix: a bare "hello" must not launch the deterministic offensive controller.)
        assert m._should_use_controller(is_interactive=False, prompt="hello") is False
        assert m._should_use_controller(is_interactive=False, prompt="what callbacks are active?") is False
        # not an autonomous solve -> never
        m._autonomous_solve = False
        assert m._should_use_controller(is_interactive=False, prompt="compromise the corp domain") is False
        # flag off -> never
        m._autonomous_solve = True
        os.environ["SAGE_AUTONOMOUS_CONTROLLER"] = "0"
        assert m._should_use_controller(is_interactive=False) is False
        # controller-native HITL has its own rollback flag
        os.environ.pop("SAGE_AUTONOMOUS_CONTROLLER", None)
        m.mode = "supervised"
        m.command_name = "chat"
        os.environ["SAGE_CONTROLLER_HITL"] = "0"
        assert m._should_use_controller(is_interactive=False) is False
    finally:
        if saved_controller is None:
            os.environ.pop("SAGE_AUTONOMOUS_CONTROLLER", None)
        else:
            os.environ["SAGE_AUTONOMOUS_CONTROLLER"] = saved_controller
        if saved_hitl is None:
            os.environ.pop("SAGE_CONTROLLER_HITL", None)
        else:
            os.environ["SAGE_CONTROLLER_HITL"] = saved_hitl


def test_supervised_explicit_objective_turn_routes_to_controller_without_autonomous_toggle():
    """A normal supervised chat objective should borrow the controller, while scoped prompts stay on graph."""
    import os
    saved_controller = os.environ.get("SAGE_AUTONOMOUS_CONTROLLER")
    saved_hitl = os.environ.get("SAGE_CONTROLLER_HITL")
    os.environ.pop("SAGE_AUTONOMOUS_CONTROLLER", None)
    os.environ.pop("SAGE_CONTROLLER_HITL", None)
    try:
        m = object.__new__(model.Model)
        m.mode = "supervised"
        m.command_name = "chat"
        m._autonomous_solve = False

        assert m._looks_like_explicit_objective_prompt("Compromise the CORP domain") is True
        assert m._looks_like_explicit_objective_prompt(
            "From the current foothold, achieve administrative control of child.lab.local."
        ) is True
        assert m._looks_like_explicit_objective_prompt("list callbacks") is False
        assert m._looks_like_explicit_objective_prompt("How would you compromise the CORP domain?") is False
        assert m._looks_like_explicit_objective_prompt("Obtain information about the CORP domain") is False

        m._supervised_objective_active = m._supervised_objective_controller_enabled_for_prompt(
            "Compromise the CORP domain"
        )
        assert m._controller_owned_solve() is True
        # Native chat marks any reused channel turn as interactive; that must not block a fresh objective.
        assert m._should_use_controller(is_interactive=True) is True

        m._supervised_objective_active = m._supervised_objective_controller_enabled_for_prompt("list callbacks")
        assert m._controller_owned_solve() is False
        assert m._should_use_controller(is_interactive=False) is False
    finally:
        if saved_controller is None:
            os.environ.pop("SAGE_AUTONOMOUS_CONTROLLER", None)
        else:
            os.environ["SAGE_AUTONOMOUS_CONTROLLER"] = saved_controller
        if saved_hitl is None:
            os.environ.pop("SAGE_CONTROLLER_HITL", None)
        else:
            os.environ["SAGE_CONTROLLER_HITL"] = saved_hitl


def test_invoke_routes_reused_supervised_objective_turn_into_controller(monkeypatch):
    """The invoke seam itself must activate the controller before it would enter LangGraph."""
    monkeypatch.delenv("SAGE_AUTONOMOUS_CONTROLLER", raising=False)
    monkeypatch.delenv("SAGE_CONTROLLER_HITL", raising=False)
    m = object.__new__(model.Model)
    m.mode = "supervised"
    m.command_name = "chat"
    m._autonomous_solve = False
    m._supervised_objective_active = False
    m._controller_hitl_pending = None
    m._thread_id_override = "channel-7"
    m._running_tasks = set()
    m._message_seq = 1
    m.graph = object()
    m.state = {"messages": [], "_message_seq": 1}
    m.mythic_client = None
    m.provider = "test"
    m.model = "test"
    seen = {}

    async def _no_interrupt(_thread_id):
        return False

    async def _no_completion(**_kwargs):
        return False

    async def _run_controller(prompt):
        seen["controller_prompt"] = prompt
        seen["active"] = m._supervised_objective_active
        return "controller"

    m._hitl_interrupt_pending = _no_interrupt
    m._maybe_stream_objective_completion_stop = _no_completion
    m._seed_autonomous_objective = lambda prompt: seen.setdefault("seeded", prompt)
    m._run_autonomous_controller = _run_controller

    assert asyncio.run(m.invoke("Compromise the CORP domain", is_interactive=True)) == "controller"
    assert seen == {
        "seeded": "Compromise the CORP domain",
        "controller_prompt": "Compromise the CORP domain",
        "active": True,
    }


def test_observe_attaches_graph_facts():
    """Forge HIGH: the observe seam must attach graph_facts (refresh-if-stale then read the cache) so the
    frontier can derive GPO/ADCS actions; without them the frontier is falsely empty at those walls."""
    calls = []

    class GF:
        predicate = "gpo-domain:starkwallpaper:north.sevenkingdoms.local"
        source = "bloodhound"
        timestamp = ""
        ttl_seconds = 0

    state = _state_with_remote_exec()
    m = _bare_model(json.dumps({"ok": True}), state, calls)

    refreshed = {"n": 0}

    class FakeMythicGF:
        _engagement_graph_facts = [GF()]

        async def _refresh_graph_facts_if_stale(self, now, force=False):
            refreshed["n"] += 1

        async def execute_capability(self, payload, inputs):
            calls.append((payload, inputs))
            return json.dumps({"ok": False, "reason": "stop here"})
    m.mythic_client = FakeMythicGF()

    asyncio.run(m._run_autonomous_controller("obtain administrative control of essos.local"))
    assert refreshed["n"] >= 1, "observe must refresh graph facts"
    # state.graph_facts was populated from the cache on observe
    assert any(getattr(f, "predicate", "") for f in (state.graph_facts or [])), state.graph_facts


def _foothold(callback_id="2", agent="apollo", host="dc01", identity="north\\admin", forest="north.local"):
    return es.Foothold(callback_id=callback_id, agent=agent, host=host, forest=forest,
                       identity=identity, integrity="high", alive=True, source="test", timestamp="")


def _live_foothold_state(callback_id="2", agent="apollo"):
    return es.EngagementState(objective="obtain administrative control of essos.local",
                             footholds=[_foothold(callback_id, agent)], hops=[], graph_facts=[])


class _CollectMythic:
    """Models the REAL seams. execute_assembly captures the run's `--ZipFilename` token; `ls` returns STRUCTURED
    JSON (Apollo's real shape) with the on-disk file optionally carrying a SharpHound TIMESTAMP PREFIX; the
    download is resolved by token; ingest_collection returns the real taxonomy with `graph_verified`."""
    def __init__(
        self,
        ingest,
        *,
        ls_has_zip=True,
        timestamp_prefix=True,
        download_visible=True,
        whoami_output="north\\admin",
        ticket_output=(
            '[{"client_name":"admin","client_realm":"NORTH.LOCAL",'
            '"service_name":"krbtgt/NORTH.LOCAL","luid":"0x123","current_luid":"0x123"}]'
        ),
    ):
        self.calls = []
        self.ingest_kwargs = []
        self._ingest = ingest
        self._ls_has_zip = ls_has_zip
        self._ts = timestamp_prefix
        self._dl_visible = download_visible
        self._zipname = None
        self._whoami_output = whoami_output
        self._ticket_output = ticket_output

    async def issue_task_and_waitfor_task_output(self, command, parameters, callback_display_id, **kw):
        self.calls.append((command, parameters, callback_display_id))
        if command == "whoami":
            return self._whoami_output
        if command == "rev2self":
            return "Reverted token"
        if command == "ticket_cache_list":
            return self._ticket_output
        if command == "execute_assembly":
            mt = re.search(r"--ZipFilename\s+(\S+)", parameters.get("assembly_arguments", ""))
            self._zipname = mt.group(1) if mt else None
            return "SharpHound enumeration completed"
        if command == "ls":
            files = [{"name": "apollo.exe", "full_name": "C:\\Users\\Public\\apollo.exe", "is_file": True}]
            if self._ls_has_zip and self._zipname:
                on_disk = (f"20260101000000_{self._zipname}" if self._ts else self._zipname)
                files.insert(0, {"name": on_disk, "full_name": f"C:\\Users\\Public\\{on_disk}", "is_file": True})
            return json.dumps({"files": files, "success": True})
        return "task output"

    async def probe_authentication_context(
        self,
        callback_display_id,
        host="",
        adapter=None,
        known_domain_authorities=(),
    ):
        from ai.langgraph import auth_context
        identity = await self.issue_task_and_waitfor_task_output("whoami", "", callback_display_id)
        tickets = await self.issue_task_and_waitfor_task_output(
            "ticket_cache_list",
            {"luid": "", "getSystemTickets": False},
            callback_display_id,
        )
        return auth_context.build_authentication_context(
            callback_display_id,
            host,
            identity,
            tickets,
            known_domain_authorities,
        )

    async def _latest_download_for_callback(self, cb, name_contains="zip"):
        self.calls.append(("_latest_download", cb, name_contains))
        if self._dl_visible and self._zipname and name_contains and name_contains in self._zipname:
            on_disk = (f"20260101000000_{self._zipname}" if self._ts else self._zipname)
            return {"agent_file_id": 11, "filename_utf8": on_disk}
        return None

    async def ingest_collection(self, file_uuid="", callback_display_id=None, file_name="", **kw):
        self.calls.append(("ingest_collection", file_uuid, callback_display_id))
        self.ingest_kwargs.append(dict(kw))
        return json.dumps(self._ingest)


class _MerlinCollectMythic(_CollectMythic):
    def __init__(self, ingest, *, whoami_outputs=None, **kwargs):
        super().__init__(ingest, ticket_output="", **kwargs)
        self._whoami_outputs = iter(whoami_outputs or [(
            "Process (Primary) Token:\n"
            "\tUser: NORTH\\samwell.tarly,Token ID: 0x1,Logon ID: 0x123,Privilege Count: 1,"
            "Group Count: 1,Type: Primary,Impersonation Level: Anonymous,Integrity Level: High\n"
            "Thread (Primary) Token:\n"
            "\tUser: NORTH\\samwell.tarly,Token ID: 0x2,Logon ID: 0x123,Privilege Count: 1,"
            "Group Count: 1,Type: Primary,Impersonation Level: Anonymous,Integrity Level: High"
        )])

    async def issue_task_and_waitfor_task_output(self, command, parameters, callback_display_id, **kw):
        self.calls.append((command, parameters, callback_display_id))
        if command == "token":
            return next(self._whoami_outputs)
        if command == "rev2Self":
            return "Successfully reverted to self and dropped the impersonation token"
        if command == "execute-assembly":
            mt = re.search(r"--ZipFilename\s+(\S+)", parameters.get("arguments", ""))
            self._zipname = mt.group(1) if mt else None
            return "SharpHound enumeration completed"
        if command == "ls":
            on_disk = f"20260101000000_{self._zipname}" if self._ts else self._zipname
            rows = ["-rw-rw-rw-\t2026-01-01 00:00:00\t123\tmerlin.exe"]
            if self._ls_has_zip and self._zipname:
                rows.insert(0, f"-rw-rw-rw-\t2026-01-01 00:00:00\t123\t{on_disk}")
            return "Directory listing for: C:\\Users\\Public\r\n\r\n" + "\n".join(rows)
        return "task output"

    async def probe_authentication_context(
        self,
        callback_display_id,
        host="",
        adapter=None,
        known_domain_authorities=(),
    ):
        from ai.langgraph import auth_context
        identity = await self.issue_task_and_waitfor_task_output(
            "token",
            {"method": "whoami"},
            callback_display_id,
        )
        return auth_context.build_authentication_context(
            callback_display_id,
            host,
            identity,
            "",
            known_domain_authorities,
            identity_parser="merlin-token",
        )


def test_collect_discovers_timestamped_zip_and_ingests_it():
    """The CRITICAL fix: SharpHound writes <timestamp>_<name>, so collect must DISCOVER the real path via `ls`
    (not predict it) and download THAT, then ingest by file_uuid+callback. ok only because graph_verified."""
    m = object.__new__(model.Model)
    fake = _CollectMythic({"status": "ingested", "graph_verified": True}, timestamp_prefix=True)
    m.mythic_client = fake
    result = asyncio.run(m._controller_collect(_live_foothold_state("2")))
    assert result["ok"] is True, result
    issued = [c[0] for c in fake.calls if c[0] in ("execute_assembly", "ls", "download", "ingest_collection")]
    assert issued == ["execute_assembly", "ls", "download", "ingest_collection"], issued
    # downloaded the DISCOVERED timestamped path, not the predicted bare name
    dl_path = next(c[1]["path"] for c in fake.calls if c[0] == "download")
    assert dl_path.startswith("C:\\Users\\Public\\20260101000000_bloodhound_"), dl_path
    assert ("ingest_collection", 11, 2) in fake.calls


def test_collect_verbose_streams_progress_to_parent_task():
    """Initial controller collection must not be silent in Mythic when verbose output is enabled."""
    m = object.__new__(model.Model)
    m.verbose = True
    fake = _CollectMythic({"status": "ingested", "graph_verified": True}, timestamp_prefix=True)
    m.mythic_client = fake
    streamed = []

    async def _stream(text):
        streamed.append(text)
        return True

    async def _run():
        result = await m._controller_collect(_live_foothold_state("2"))
        await m._flush_controller_verbose_events()
        return result

    m._stream_message_to_mythic = _stream
    result = asyncio.run(_run())

    assert result["ok"] is True, result
    assert any("**Collection started**" in item and "SharpHound collection" in item for item in streamed), streamed
    assert any("**Collection artifact**" in item and "fresh collection artifact" in item for item in streamed), streamed
    assert any("**Collection verified**" in item and "graph_verified=true" in item for item in streamed), streamed
    assert all("Autonomous_Controller" not in item for item in streamed), streamed


def test_collect_restores_domain_identity_before_sharphound_when_callback_is_host_local():
    m = object.__new__(model.Model)
    fake = _CollectMythic(
        {"status": "ingested", "graph_verified": True},
        whoami_output="Local Identity: north\\samwell.tarly\nImpersonation Identity: north\\samwell.tarly",
    )
    m.mythic_client = fake
    state = es.EngagementState(
        objective="obtain administrative control of essos.local",
        footholds=[_foothold("2", host="braavos", identity="BRAAVOS\\Administrator")],
        hops=[],
        graph_facts=[],
    )

    result = asyncio.run(m._controller_collect(state))

    assert result["ok"] is True, result
    issued = [call[0] for call in fake.calls]
    assert issued[:3] == ["whoami", "ticket_cache_list", "execute_assembly"], issued


def test_authority_change_collect_probes_effective_identity_and_restores_stale_local_token():
    m = object.__new__(model.Model)

    class EffectiveIdentityMythic(_CollectMythic):
        def __init__(self):
            super().__init__({"status": "ingested", "graph_verified": True})
            self.whoami_outputs = iter([
                "Local Identity: braavos\\administrator\nImpersonation Identity: braavos\\administrator",
                "Local Identity: north\\samwell.tarly\nImpersonation Identity: north\\samwell.tarly",
            ])
            self.ticket_outputs = iter([
                "0x456",
                (
                    '[{"client_name":"samwell.tarly","client_realm":"NORTH.LOCAL",'
                    '"service_name":"krbtgt/NORTH.LOCAL","luid":"0x123","current_luid":"0x123"}]'
                ),
            ])

        async def issue_task_and_waitfor_task_output(self, command, parameters, callback_display_id, **kw):
            if command == "whoami":
                self.calls.append((command, parameters, callback_display_id))
                return next(self.whoami_outputs)
            if command == "ticket_cache_list":
                self.calls.append((command, parameters, callback_display_id))
                return next(self.ticket_outputs)
            return await super().issue_task_and_waitfor_task_output(
                command,
                parameters,
                callback_display_id,
                **kw,
            )

    fake = EffectiveIdentityMythic()
    m.mythic_client = fake
    state = es.EngagementState(
        objective="obtain administrative control of essos.local",
        footholds=[_foothold("2", host="braavos", identity="NORTH\\samwell.tarly")],
        hops=[],
        graph_facts=[],
    )
    request = model._ControllerCollectionRequest(
        foothold=state.footholds[0],
        reason="authority-change",
    )

    result = asyncio.run(m._controller_collect(state, request=request))

    assert result["ok"] is True, result
    issued = [call[0] for call in fake.calls]
    assert issued[:6] == [
        "whoami",
        "ticket_cache_list",
        "rev2self",
        "whoami",
        "ticket_cache_list",
        "execute_assembly",
    ], issued


def test_collect_refuses_sharphound_when_restored_identity_is_still_host_local():
    m = object.__new__(model.Model)
    fake = _CollectMythic(
        {"status": "ingested", "graph_verified": True},
        whoami_output=(
            "Local Identity: braavos\\administrator\n"
            "Impersonation Identity: braavos\\administrator"
        ),
        ticket_output="0x123",
    )
    m.mythic_client = fake
    state = es.EngagementState(
        objective="obtain administrative control of essos.local",
        footholds=[_foothold("2", host="braavos", identity="BRAAVOS\\Administrator")],
        hops=[],
        graph_facts=[],
    )

    result = asyncio.run(m._controller_collect(state))

    assert result["ok"] is False
    assert result["status"] == "no_domain_identity"
    assert not any(call[0] == "execute_assembly" for call in fake.calls)


def test_collect_preserves_local_token_when_current_luid_has_domain_tgt():
    m = object.__new__(model.Model)
    fake = _CollectMythic(
        {"status": "ingested", "graph_verified": True},
        whoami_output=(
            "Local Identity: braavos\\administrator\n"
            "Impersonation Identity: braavos\\administrator"
        ),
        ticket_output=(
            '[{"client_name":"administrator","client_realm":"ESSOS.LOCAL",'
            '"service_name":"krbtgt/ESSOS.LOCAL","luid":"0x123","current_luid":"0x123"}]'
        ),
    )
    m.mythic_client = fake
    state = es.EngagementState(
        objective="obtain administrative control of essos.local",
        footholds=[_foothold("2", host="braavos", identity="BRAAVOS\\Administrator")],
        hops=[],
        graph_facts=[],
    )

    result = asyncio.run(m._controller_collect(state))

    assert result["ok"] is True, result
    issued = [call[0] for call in fake.calls]
    assert "rev2self" not in issued
    assert issued[:3] == ["whoami", "ticket_cache_list", "execute_assembly"]


def test_collect_no_zip_in_output_is_no_artifact(monkeypatch):
    """SharpHound produced no token-bearing ZIP (failed/usage output) -> collect discovers nothing, downloads
    nothing, ingests nothing, and reports no_collection_artifact (fail-closed)."""
    monkeypatch.setattr(model.asyncio, "sleep", _nosleep)
    m = object.__new__(model.Model)
    fake = _CollectMythic({"status": "ingested", "graph_verified": True}, ls_has_zip=False)
    m.mythic_client = fake
    result = asyncio.run(m._controller_collect(_live_foothold_state("2")))
    assert result["ok"] is False and result["status"] == "no_collection_artifact", result
    assert not any(c[0] in ("download", "ingest_collection") for c in fake.calls), fake.calls


def test_collect_stops_immediately_when_registered_file_preflight_fails():
    class _PreflightFailCollectMythic(_CollectMythic):
        async def issue_task_and_waitfor_task_output(self, command, parameters, callback_display_id, **kw):
            if command == "execute_assembly":
                self.calls.append((command, parameters, callback_display_id))
                return (
                    f"{mt._REGISTERED_FILE_PREFLIGHT_PREFIX} could not register 'SharpHound.exe' "
                    "before 'execute_assembly': upload failed"
                )
            return await super().issue_task_and_waitfor_task_output(command, parameters, callback_display_id, **kw)

    m = object.__new__(model.Model)
    fake = _PreflightFailCollectMythic({"status": "ingested", "graph_verified": True})
    m.mythic_client = fake

    result = asyncio.run(m._controller_collect(_live_foothold_state("2")))

    assert result["ok"] is False and result["status"] == "tool_preflight_failed", result
    assert "SharpHound.exe" in result["reason"]
    assert not any(c[0] in ("ls", "download", "ingest_collection") for c in fake.calls), fake.calls


def test_collect_ingest_failed_is_not_ok():
    """Forge H2: ingest_failed (graph_verified False) must be ok=False, not a false success."""
    m = object.__new__(model.Model)
    m.mythic_client = _CollectMythic({"status": "ingest_failed", "graph_verified": False})
    result = asyncio.run(m._controller_collect(_live_foothold_state("2")))
    assert result["ok"] is False and result["status"] == "ingest_failed", result


def test_collect_pending_ingest_is_not_ok():
    """Forge H2: uploaded_pending_ingest (graph_verified False) must be ok=False so the gate stays missing."""
    m = object.__new__(model.Model)
    m.mythic_client = _CollectMythic({"status": "uploaded_pending_ingest", "graph_verified": False})
    result = asyncio.run(m._controller_collect(_live_foothold_state("2")))
    assert result["ok"] is False and result["status"] == "uploaded_pending_ingest", result


def test_collect_already_ingested_is_ok():
    m = object.__new__(model.Model)
    m.mythic_client = _CollectMythic({"status": "already_ingested", "graph_verified": True})
    result = asyncio.run(m._controller_collect(_live_foothold_state("2")))
    assert result["ok"] is True, result


def test_collect_merlin_uses_profiled_command_forms_and_text_ls():
    m = object.__new__(model.Model)
    fake = _MerlinCollectMythic({"status": "ingested", "graph_verified": True})
    m.mythic_client = fake

    result = asyncio.run(m._controller_collect(_live_foothold_state("2", agent="merlin")))

    assert result["ok"] is True, result
    issued = [call[0] for call in fake.calls if call[0] in ("token", "execute-assembly", "ls", "download")]
    assert issued == ["token", "execute-assembly", "ls", "download"], issued
    execute = next(call for call in fake.calls if call[0] == "execute-assembly")
    assert execute[1]["filename"] == "SharpHound.exe"
    assert "--ZipFilename bloodhound_" in execute[1]["arguments"]
    download = next(call for call in fake.calls if call[0] == "download")
    assert download[1]["file"].startswith("C:\\Users\\Public\\20260101000000_bloodhound_")


def test_collect_merlin_uses_profiled_revert_command_for_local_token():
    local = (
        "Process (Primary) Token:\n"
        "\tUser: BRAAVOS\\Administrator,Token ID: 0x1,Logon ID: 0x456,Privilege Count: 1,"
        "Group Count: 1,Type: Primary,Impersonation Level: Anonymous,Integrity Level: High\n"
        "Thread (Primary) Token:\n"
        "\tUser: BRAAVOS\\Administrator,Token ID: 0x2,Logon ID: 0x456,Privilege Count: 1,"
        "Group Count: 1,Type: Primary,Impersonation Level: Anonymous,Integrity Level: High"
    )
    domain = (
        "Process (Primary) Token:\n"
        "\tUser: NORTH\\samwell.tarly,Token ID: 0x1,Logon ID: 0x123,Privilege Count: 1,"
        "Group Count: 1,Type: Primary,Impersonation Level: Anonymous,Integrity Level: High\n"
        "Thread (Primary) Token:\n"
        "\tUser: NORTH\\samwell.tarly,Token ID: 0x2,Logon ID: 0x123,Privilege Count: 1,"
        "Group Count: 1,Type: Primary,Impersonation Level: Anonymous,Integrity Level: High"
    )
    m = object.__new__(model.Model)
    fake = _MerlinCollectMythic(
        {"status": "ingested", "graph_verified": True},
        whoami_outputs=[local, domain],
    )
    m.mythic_client = fake
    state = es.EngagementState(
        objective="obtain administrative control of essos.local",
        footholds=[_foothold("2", agent="merlin", host="braavos", identity="BRAAVOS\\Administrator")],
        hops=[],
        graph_facts=[],
    )

    result = asyncio.run(m._controller_collect(state))

    assert result["ok"] is True, result
    issued = [call[0] for call in fake.calls]
    assert issued[:4] == ["token", "rev2Self", "token", "execute-assembly"], issued


def test_collect_skips_unprofiled_foothold(monkeypatch):
    """Forge N2: an unprofiled missing foothold is NOT selected (so no slot is burned). With only a beacon
    foothold, the target resolver yields nothing -> no_target, and SharpHound is never even issued."""
    monkeypatch.setattr(model.asyncio, "sleep", _nosleep)
    m = object.__new__(model.Model)
    fake = _CollectMythic({"status": "ingested", "graph_verified": True})
    m.mythic_client = fake
    result = asyncio.run(m._controller_collect(_live_foothold_state("2", agent="beacon")))
    assert result["ok"] is False and result["status"] == "no_target", result
    assert not any(c[0] == "execute_assembly" for c in fake.calls), "must not run SharpHound on an unprofiled foothold"


def test_collect_no_target():
    m = object.__new__(model.Model)
    m.mythic_client = _CollectMythic({"status": "ingested", "graph_verified": True})
    state = es.EngagementState(objective="x", footholds=[], hops=[], graph_facts=[])
    result = asyncio.run(m._controller_collect(state))
    assert result["ok"] is False and result["status"] == "no_target", result


# --- _find_token_zip_path: parses the REAL Apollo `ls` JSON shape captured live on cb2 ---
_LS_SAMPLE = ('{"files":[{"name":"20260622_bloodhound_ab12cd34.zip","full_name":'
              '"C:\\\\Users\\\\Public\\\\20260622_bloodhound_ab12cd34.zip","is_file":true,"size":1234},'
              '{"name":"apollo.exe","full_name":"C:\\\\Users\\\\Public\\\\apollo.exe","is_file":true}],'
              '"success":true}')


def test_find_token_zip_path_discovers_timestamped_name():
    path = model._find_token_zip_path(_LS_SAMPLE, "ab12cd34")
    assert path == "C:\\Users\\Public\\20260622_bloodhound_ab12cd34.zip", path


def test_find_token_zip_path_token_absent_returns_empty():
    assert model._find_token_zip_path(_LS_SAMPLE, "deadbeef") == ""


def test_find_token_zip_path_ignores_non_matching_zip():
    """Token disambiguation (Forge LOW): a different run's ZIP on the same dir must NOT be selected."""
    two_zips = ('{"files":[{"name":"20260622_bloodhound_OTHER999.zip","full_name":'
                '"C:\\\\Users\\\\Public\\\\20260622_bloodhound_OTHER999.zip","is_file":true},'
                '{"name":"20260622_bloodhound_ab12cd34.zip","full_name":'
                '"C:\\\\Users\\\\Public\\\\20260622_bloodhound_ab12cd34.zip","is_file":true}],"success":true}')
    assert model._find_token_zip_path(two_zips, "ab12cd34").endswith("bloodhound_ab12cd34.zip")


def test_find_token_zip_path_handles_concatenated_json_objects():
    """Apollo streams >1 JSON object in one task output; the parser must walk them all."""
    doubled = _LS_SAMPLE + '{"files":[],"success":true}'
    assert model._find_token_zip_path(doubled, "ab12cd34").endswith("bloodhound_ab12cd34.zip")


def test_find_token_zip_path_handles_merlin_text_listing():
    listing = (
        "Directory listing for: C:\\Users\\Public\r\n\r\n"
        "-rw-rw-rw-\t2026-06-22 10:22:31\t1234\t20260622_bloodhound_ab12cd34.zip\n"
        "-rw-rw-rw-\t2026-06-22 10:22:31\t1234\tmerlin.exe\n"
    )
    assert model._find_token_zip_path(listing, "ab12cd34") == (
        "C:\\Users\\Public\\20260622_bloodhound_ab12cd34.zip"
    )


def test_collection_target_picks_the_missing_foothold_not_the_first():
    """Forge H3: when foothold A's forest is already collected and B's distinct forest is missing, the target
    must be B — not the first live foothold A. Same-forest same-authority footholds now intentionally dedupe."""
    a = _foothold(callback_id="2", host="hostA", identity="north\\a")
    b = _foothold(callback_id="3", host="hostB", identity="other\\b", forest="other.local")
    state = es.EngagementState(objective="x", footholds=[a, b], hops=[], graph_facts=[])
    key_a = es.access_context_key(state, a)
    hop = es.Hop(id="g", technique="collect-graph", target="hostA", effect=f"graph-built:{key_a}",
                 status="achieved",
                 evidence={"graph_verified": True, "covered_domains": ["north.local"]},
                 preconditions=[], satisfied_effects=[f"graph-built:{key_a}"],
                 source="test", timestamp="")
    state = es.EngagementState(objective="x", footholds=[a, b], hops=[hop], graph_facts=[])
    m = object.__new__(model.Model)
    target = m._controller_collection_target(state)
    assert target is not None and target.callback_id == "3", target


def test_collection_request_targets_trusted_domain_only_after_default_scope_is_covered():
    foothold = _foothold(callback_id="2", host="castelblack", identity="north\\samwell.tarly")
    base = es.EngagementState(objective="obtain administrative control of essos.local", footholds=[foothold])
    key = es.collection_target_key(base, foothold)
    hop = es.Hop(
        id="collect-default",
        technique="collect-graph",
        target=key,
        effect=f"graph-built:{key}",
        status="achieved",
        evidence={"graph_verified": True, "covered_domains": ["north.local"]},
        preconditions=[],
        satisfied_effects=[f"graph-built:{key}"],
        source="test",
        timestamp="",
    )
    state = es.EngagementState(
        objective="obtain administrative control of essos.local",
        footholds=[foothold],
        hops=[hop],
        graph_facts=[
            es.GraphFact("domain-collected:north.local", "test", "", 600),
            es.GraphFact("trust-reachable:north.local:essos.local", "test", "", 600),
        ],
    )
    m = object.__new__(model.Model)

    assert m._controller_collection_request(state, include_trusted_scope=False) is None
    request = m._controller_collection_request(state, include_trusted_scope=True)
    assert request is not None
    assert request.foothold.callback_id == "2"
    assert request.scope_domain == "essos.local"
    assert request.reason == "objective-scope-expansion"


def test_collection_request_prefers_latest_proven_callback_lane_for_scope_expansion():
    older = _foothold(callback_id="4", agent="merlin", host="castelblack", identity="north\\samwell.tarly")
    newer = _foothold(callback_id="5", agent="apollo", host="castelblack", identity="north\\samwell.tarly")
    base = es.EngagementState(objective="obtain administrative control of essos.local", footholds=[older, newer])
    baseline_key = es.collection_target_key(base, older)
    baseline_hop = es.Hop(
        id="collect-default",
        technique="collect-graph",
        target=baseline_key,
        effect=f"graph-built:{baseline_key}",
        status="achieved",
        evidence={"graph_verified": True, "covered_domains": ["north.local"]},
        preconditions=[],
        satisfied_effects=[f"graph-built:{baseline_key}"],
        source="test",
        timestamp="",
    )
    latest_hop = es.Hop(
        id="ctx",
        technique="capability:ensure-kerberos-context",
        target="domain=sevenkingdoms.local;callback=5",
        effect="kerberos-context:sevenkingdoms.local@callback:5",
        status="achieved",
        evidence={"callback_id": "5"},
        preconditions=[],
        satisfied_effects=["kerberos-context:sevenkingdoms.local@callback:5"],
        source="test",
        timestamp="",
    )
    state = es.EngagementState(
        objective="obtain administrative control of essos.local",
        footholds=[older, newer],
        hops=[baseline_hop, latest_hop],
        graph_facts=[
            es.GraphFact("domain-collected:north.local", "test", "", 600),
            es.GraphFact("trust-reachable:north.local:essos.local", "test", "", 600),
        ],
    )
    m = object.__new__(model.Model)

    request = m._controller_collection_request(
        state,
        include_trusted_scope=True,
        include_optional_recollection=True,
    )

    assert request is not None
    assert request.foothold.callback_id == "5"
    assert request.scope_domain == "essos.local"
    assert request.reason == "objective-scope-expansion"


def test_collection_request_prefers_objective_scope_over_optional_authority_recollection():
    foothold = _foothold(callback_id="2", host="castelblack", identity="north\\samwell.tarly")
    base = es.EngagementState(objective="obtain administrative control of essos.local", footholds=[foothold])
    baseline_key = es.collection_target_key(base, foothold)
    baseline_hop = es.Hop(
        id="collect-default",
        technique="collect-graph",
        target=baseline_key,
        effect=f"graph-built:{baseline_key}",
        status="achieved",
        evidence={"graph_verified": True, "covered_domains": ["north.local"]},
        preconditions=[],
        satisfied_effects=[f"graph-built:{baseline_key}"],
        source="test",
        timestamp="",
    )
    da_hop = es.Hop(
        id="da",
        technique="domain-admin-membership-check",
        target="north.local",
        effect="da:north.local",
        status="achieved",
        evidence={},
        preconditions=[],
        satisfied_effects=["da:north.local"],
        source="test",
        timestamp="",
    )
    state = es.EngagementState(
        objective="obtain administrative control of essos.local",
        footholds=[foothold],
        hops=[baseline_hop, da_hop],
        graph_facts=[
            es.GraphFact("domain-collected:north.local", "test", "", 600),
            es.GraphFact("trust-reachable:north.local:essos.local", "test", "", 600),
        ],
    )
    m = object.__new__(model.Model)

    assert es.graph_collection_covers_foothold(state, foothold) is False
    request = m._controller_collection_request(
        state,
        include_trusted_scope=True,
        include_optional_recollection=True,
    )
    assert request is not None
    assert request.scope_domain == "essos.local"
    assert request.reason == "objective-scope-expansion"

    authority_request = m._controller_collection_request(
        state,
        include_trusted_scope=False,
        include_optional_recollection=True,
    )
    assert authority_request is not None
    assert authority_request.scope_domain == ""
    assert authority_request.reason == "authority-change"


def test_trusted_objective_scope_collection_wins_after_broad_account_frontier_is_suppressed():
    from ai.langgraph import capabilities

    def achieved(hop_id, effect):
        return es.Hop(
            id=hop_id,
            technique="seed",
            target="lab.local",
            effect=effect,
            status="achieved",
            evidence={},
            preconditions=[],
            satisfied_effects=[effect],
            source="test",
            timestamp="",
        )

    foothold = _foothold(callback_id="2", host="dc01", identity="lab\\operator", forest="lab.local")
    base = es.EngagementState(objective="obtain administrative control of child.lab.local", footholds=[foothold])
    baseline_key = es.collection_target_key(base, foothold)
    baseline_hop = es.Hop(
        id="collect-default",
        technique="collect-graph",
        target=baseline_key,
        effect=f"graph-built:{baseline_key}",
        status="achieved",
        evidence={"graph_verified": True, "covered_domains": ["lab.local"]},
        preconditions=[],
        satisfied_effects=[f"graph-built:{baseline_key}"],
        source="test",
        timestamp="",
    )
    state = es.EngagementState(
        objective="obtain administrative control of child.lab.local",
        footholds=[foothold],
        hops=[
            baseline_hop,
            achieved("rights", "ds-replication-rights:lab.local"),
            achieved("hash", "krbtgt-hash:lab.local"),
            achieved("da", "da:lab.local"),
            achieved("ctx", "kerberos-context:lab.local@callback:2"),
        ],
        graph_facts=[
            es.GraphFact("domain-collected:lab.local", "test", "", 600),
            es.GraphFact("trust-reachable:lab.local:child.lab.local", "test", "", 600),
            es.GraphFact("credential-target:alice@lab.local", "test", "", 600),
            es.GraphFact("credential-target:bob@lab.local", "test", "", 600),
        ],
    )
    m = object.__new__(model.Model)

    assert capabilities.actions_from_state(state) == []
    request = m._controller_collection_request(
        state,
        include_trusted_scope=True,
        include_optional_recollection=True,
    )
    assert request is not None
    assert request.scope_domain == "child.lab.local"
    assert request.reason == "objective-scope-expansion"


def test_collection_request_does_not_recollect_authority_after_objective_domain_is_collected():
    foothold = _foothold(callback_id="2", host="castelblack", identity="north\\samwell.tarly")
    base = es.EngagementState(objective="obtain administrative control of essos.local", footholds=[foothold])
    baseline_key = es.collection_target_key(base, foothold)
    baseline_hop = es.Hop(
        id="collect-default",
        technique="collect-graph",
        target=baseline_key,
        effect=f"graph-built:{baseline_key}",
        status="achieved",
        evidence={"graph_verified": True, "covered_domains": ["north.local"]},
        preconditions=[],
        satisfied_effects=[f"graph-built:{baseline_key}"],
        source="test",
        timestamp="",
    )
    da_hop = es.Hop(
        id="da",
        technique="domain-admin-membership-check",
        target="north.local",
        effect="da:north.local",
        status="achieved",
        evidence={},
        preconditions=[],
        satisfied_effects=["da:north.local"],
        source="test",
        timestamp="",
    )
    current_epoch = es.EngagementState(
        objective="obtain administrative control of essos.local",
        footholds=[foothold],
        hops=[baseline_hop, da_hop],
    )
    targeted_key = es.collection_target_key(current_epoch, foothold, "essos.local")
    targeted_hop = es.Hop(
        id="collect-target",
        technique="collect-graph",
        target=targeted_key,
        effect=f"graph-built:{targeted_key}",
        status="achieved",
        evidence={"graph_verified": True, "covered_domains": ["essos.local"]},
        preconditions=[],
        satisfied_effects=[f"graph-built:{targeted_key}"],
        source="test",
        timestamp="",
    )
    state = es.EngagementState(
        objective="obtain administrative control of essos.local",
        footholds=[foothold],
        hops=[baseline_hop, da_hop, targeted_hop],
        graph_facts=[
            es.GraphFact("domain-collected:north.local", "test", "", 600),
            es.GraphFact("domain-collected:essos.local", "test", "", 600),
            es.GraphFact("trust-reachable:north.local:essos.local", "test", "", 600),
        ],
    )
    m = object.__new__(model.Model)

    assert es.graph_collection_covers_foothold(state, foothold) is False
    assert es.graph_domain_has_verified_collection(state, "essos.local") is True
    request = m._controller_collection_request(
        state,
        include_trusted_scope=True,
        include_optional_recollection=True,
    )
    assert request is None


def test_collection_request_does_not_expand_scope_after_retryable_capability_failure():
    foothold = _foothold(callback_id="2", host="castelblack", identity="north\\samwell.tarly")
    base = es.EngagementState(objective="obtain administrative control of essos.local", footholds=[foothold])
    baseline_key = es.collection_target_key(base, foothold)
    baseline_hop = es.Hop(
        id="collect-default",
        technique="collect-graph",
        target=baseline_key,
        effect=f"graph-built:{baseline_key}",
        status="achieved",
        evidence={"graph_verified": True, "covered_domains": ["north.local"]},
        preconditions=[],
        satisfied_effects=[f"graph-built:{baseline_key}"],
        source="test",
        timestamp="",
    )
    retryable_failure = es.Hop(
        id="failed-cert-auth",
        technique="capability:adcs-certificate-auth",
        target="domain=essos.local;account=administrator;ca_host=braavos;callback=2",
        effect="da:essos.local",
        status="failed",
        evidence={"terminal_failure": False, "failure_class": "transient"},
        preconditions=[],
        satisfied_effects=["da:essos.local", "certificate-auth:administrator@essos.local"],
        source="test",
        timestamp="",
    )
    state = es.EngagementState(
        objective="obtain administrative control of essos.local",
        footholds=[foothold],
        hops=[baseline_hop, retryable_failure],
        graph_facts=[
            es.GraphFact("domain-collected:north.local", "test", "", 600),
            es.GraphFact("trust-reachable:north.local:essos.local", "test", "", 600),
        ],
    )
    m = object.__new__(model.Model)

    request = m._controller_collection_request(
        state,
        include_trusted_scope=True,
        include_optional_recollection=True,
    )

    assert request is None


def test_collect_targeted_scope_passes_domain_to_sharphound_and_ingest():
    m = object.__new__(model.Model)
    fake = _CollectMythic({"status": "ingested", "graph_verified": True})
    m.mythic_client = fake
    state = _live_foothold_state("2")
    request = model._ControllerCollectionRequest(
        foothold=state.footholds[0],
        scope_domain="essos.local",
        reason="objective-scope-expansion",
    )

    result = asyncio.run(m._controller_collect(state, request=request))

    assert result["ok"] is True, result
    assembly_args = next(
        call[1]["assembly_arguments"]
        for call in fake.calls
        if call[0] == "execute_assembly"
    )
    assert "--Domain essos.local" in assembly_args
    assert "--SearchForest" not in assembly_args
    assert fake.ingest_kwargs == [{"collection_scope_domain": "essos.local"}]
    assert result["collection_reason"] == "objective-scope-expansion"


def test_capability_inputs_pass_controlled_principal():
    """The controller must pass the foothold identity as controlled_principal/current_user so deterministic
    self-escalation builders (gpo-controlled-system-exec -> add-to-Domain-Admins) can fill in the command."""
    from ai.langgraph import capabilities as cap
    action = cap.CapabilityAction(name="gpo-controlled-system-exec",
                                  target="gpo=starkwallpaper;domain=north.sevenkingdoms.local")
    snap = es.EngagementState(objective="x", footholds=[_foothold("2", "apollo", "castelblack", "north\\samwell.tarly")],
                             hops=[], graph_facts=[])
    inputs = model._autonomous_capability_inputs(action, snap)
    assert inputs.get("controlled_principal") == "north\\samwell.tarly", inputs
    assert inputs.get("current_user") == "north\\samwell.tarly", inputs


def test_capability_inputs_enable_proof_only_for_non_dc_gpo_system_exec():
    """Non-DC GPO actions explicitly model a SYSTEM proof hop, so the autonomous builder must authorize the
    proof marker path instead of rejecting the action for lacking a durable domain-visible command."""
    from ai.langgraph import capabilities as cap

    snap = es.EngagementState(
        objective="x",
        footholds=[_foothold("2", "apollo", "ws01", "range\\user1", "range.local")],
        hops=[],
        graph_facts=[],
    )
    proof_action = cap.CapabilityAction(
        name="gpo-controlled-system-exec",
        target="gpo=srv02-policy;domain=range.local",
        intent={"preferred_effect": "system-exec-proof"},
    )
    durable_action = cap.CapabilityAction(
        name="gpo-controlled-system-exec",
        target="gpo=dc-policy;domain=range.local",
        intent={"preferred_effect": "domain-admin-membership"},
    )

    assert model._autonomous_capability_inputs(proof_action, snap).get("allow_proof_only") is True
    assert "allow_proof_only" not in model._autonomous_capability_inputs(durable_action, snap)


def test_capability_inputs_use_bounded_gpo_wait_override_for_gpo_lane(monkeypatch):
    from ai.langgraph import capabilities as cap

    snap = es.EngagementState(
        objective="x",
        footholds=[_foothold("2", "apollo", "ws01", "range\\user1", "range.local")],
        hops=[],
        graph_facts=[],
    )
    monkeypatch.setenv("SAGE_GPO_WAIT_SECONDS", "120")

    gpo_action = cap.CapabilityAction(
        name="gpo-controlled-system-exec",
        target="gpo=srv02-policy;domain=range.local",
    )
    grant_action = cap.CapabilityAction(name="grant-directory-rights", target="domain=range.local")
    laps_action = cap.CapabilityAction(
        name="read-managed-local-admin-secret",
        target="host=ca01;domain=range.local",
    )
    gpo_inputs = model._autonomous_capability_inputs(
        gpo_action,
        snap,
    )
    grant_inputs = model._autonomous_capability_inputs(
        grant_action,
        snap,
    )
    laps_inputs = model._autonomous_capability_inputs(
        laps_action,
        snap,
    )
    policy_candidates = model._autonomous_policy_candidates([gpo_action, grant_action, laps_action])

    assert gpo_inputs["gpo_wait_seconds"] == 120
    assert grant_inputs["gpo_wait_seconds"] == 120
    assert "gpo_wait_seconds" not in laps_inputs
    assert policy_candidates[0].operational_cost == cap.gpo_operational_cost(120)
    assert policy_candidates[1].operational_cost == cap.gpo_operational_cost(120)
    assert policy_candidates[2].operational_cost == cap.immediate_operational_cost()


def test_capability_inputs_and_policy_cost_share_gpo_wait_alias_without_env_override(monkeypatch):
    from ai.langgraph import capabilities as cap

    monkeypatch.delenv("SAGE_GPO_WAIT_SECONDS", raising=False)
    action = cap.CapabilityAction(
        name="gpo-controlled-system-exec",
        target="gpo=srv02-policy;domain=range.local",
        intent={"gp_refresh_wait_seconds": 45},
    )

    inputs = model._autonomous_capability_inputs(action, None)
    policy_candidate = model._autonomous_policy_candidates([action])[0]

    assert inputs["gpo_wait_seconds"] == 45
    assert policy_candidate.operational_cost == cap.gpo_operational_cost(45)


def test_eval_forced_capability_prefix_filters_until_release_on_failure(monkeypatch):
    from ai.langgraph import capabilities as cap

    monkeypatch.setenv(
        "SAGE_EVAL_FORCE_CAPABILITY_PREFIX_JSON",
        json.dumps([
            {
                "capability": "read-managed-local-admin-secret",
                "target_contains": "target=ca01;target_domain=range.local",
            },
            {
                "capability": "adcs-ca-private-key-export",
                "target_contains": "target=ca01;target_domain=range.local",
                "release_on_failure": True,
            },
        ]),
    )
    read_action = cap.CapabilityAction(
        name="read-managed-local-admin-secret",
        target="account=user1;target=ca01;target_domain=range.local",
    )
    export_action = cap.CapabilityAction(
        name="adcs-ca-private-key-export",
        target="target=ca01;target_domain=range.local",
    )
    gpo_action = cap.CapabilityAction(
        name="gpo-controlled-system-exec",
        target="gpo=srv02-policy;domain=range.local",
    )
    actions = [gpo_action, read_action, export_action]
    empty = es.EngagementState(objective="x")
    achieved_read = es.EngagementState(
        objective="x",
        hops=[
            es.Hop(
                id="read",
                technique="capability:read-managed-local-admin-secret",
                target=read_action.target,
                effect="managed-local-admin-secret:ca01@range.local",
                status="achieved",
                evidence={},
                preconditions=[],
                satisfied_effects=["managed-local-admin-secret:ca01@range.local"],
                source="test",
                timestamp="",
            ),
        ],
    )
    blocked_export = es.EngagementState(
        objective="x",
        hops=[
            *achieved_read.hops,
            es.Hop(
                id="export",
                technique="capability:adcs-ca-private-key-export",
                target=export_action.target,
                effect="adcs-ca-private-key:ca01@range.local",
                status="blocked",
                evidence={},
                preconditions=[],
                satisfied_effects=["adcs-ca-private-key:ca01@range.local"],
                source="test",
                timestamp="",
            ),
        ],
    )

    assert [item.name for item in model._eval_forced_capability_prefix_candidates(actions, empty)] == [
        "read-managed-local-admin-secret",
    ]
    assert [item.name for item in model._eval_forced_capability_prefix_candidates(actions, achieved_read)] == [
        "adcs-ca-private-key-export",
    ]
    assert model._eval_forced_capability_prefix_candidates(actions, blocked_export) == actions


def test_capability_inputs_ignore_dead_callback_scoped_context_fallback():
    """A stale achieved Kerberos context must not retarget a fresh capability to a dead callback."""
    from ai.langgraph import capabilities as cap

    action = cap.CapabilityAction(
        name="forge-golden-ticket",
        target="domain=north.sevenkingdoms.local;target_domain=sevenkingdoms.local",
        intent={"domain": "north.sevenkingdoms.local", "target_domain": "sevenkingdoms.local"},
    )
    dead = _foothold("3", "merlin", "castelblack", "north\\samwell.tarly", "north.sevenkingdoms.local")
    dead.alive = False
    live = _foothold("4", "merlin", "castelblack", "north\\samwell.tarly", "north.sevenkingdoms.local")
    snap = es.EngagementState(
        objective="x",
        footholds=[dead, live],
        hops=[
            es.Hop(
                id="ctx",
                technique="capability:ensure-kerberos-context",
                target="domain=north.sevenkingdoms.local;callback=3",
                effect="kerberos-context:north.sevenkingdoms.local@callback:3",
                status="achieved",
                evidence={},
                preconditions=[],
                satisfied_effects=["kerberos-context:north.sevenkingdoms.local@callback:3"],
                source="test",
                timestamp="",
            )
        ],
        graph_facts=[],
    )

    inputs = model._autonomous_capability_inputs(action, snap)

    assert inputs.get("callback_id") == "4", inputs


def test_capability_inputs_reuse_live_callback_scoped_context_fallback():
    """A still-live achieved Kerberos context remains the preferred callback for the next capability."""
    from ai.langgraph import capabilities as cap

    action = cap.CapabilityAction(
        name="forge-golden-ticket",
        target="domain=north.sevenkingdoms.local;target_domain=sevenkingdoms.local",
        intent={"domain": "north.sevenkingdoms.local", "target_domain": "sevenkingdoms.local"},
    )
    live_context = _foothold("3", "merlin", "castelblack", "north\\samwell.tarly", "north.sevenkingdoms.local")
    other_live = _foothold("4", "merlin", "castelblack", "north\\samwell.tarly", "north.sevenkingdoms.local")
    snap = es.EngagementState(
        objective="x",
        footholds=[live_context, other_live],
        hops=[
            es.Hop(
                id="ctx",
                technique="capability:ensure-kerberos-context",
                target="domain=north.sevenkingdoms.local;callback=3",
                effect="kerberos-context:north.sevenkingdoms.local@callback:3",
                status="achieved",
                evidence={},
                preconditions=[],
                satisfied_effects=["kerberos-context:north.sevenkingdoms.local@callback:3"],
                source="test",
                timestamp="",
            )
        ],
        graph_facts=[],
    )

    inputs = model._autonomous_capability_inputs(action, snap)

    assert inputs.get("callback_id") == "3", inputs


def test_capability_inputs_reuse_newest_live_callback_scoped_context_fallback():
    """When multiple live callbacks hold the same context, the newest proof wins over the lowest callback id."""
    from ai.langgraph import capabilities as cap

    action = cap.CapabilityAction(
        name="forge-golden-ticket",
        target="domain=north.sevenkingdoms.local;target_domain=sevenkingdoms.local",
        intent={"domain": "north.sevenkingdoms.local", "target_domain": "sevenkingdoms.local"},
    )
    older_live = _foothold("4", "merlin", "castelblack", "north\\samwell.tarly", "north.sevenkingdoms.local")
    newer_live = _foothold("5", "apollo", "castelblack", "north\\samwell.tarly", "north.sevenkingdoms.local")
    snap = es.EngagementState(
        objective="x",
        footholds=[older_live, newer_live],
        hops=[
            es.Hop(
                id="ctx-old",
                technique="capability:ensure-kerberos-context",
                target="domain=north.sevenkingdoms.local;callback=4",
                effect="kerberos-context:north.sevenkingdoms.local@callback:4",
                status="achieved",
                evidence={},
                preconditions=[],
                satisfied_effects=["kerberos-context:north.sevenkingdoms.local@callback:4"],
                source="test",
                timestamp="",
            ),
            es.Hop(
                id="ctx-new",
                technique="capability:ensure-kerberos-context",
                target="domain=north.sevenkingdoms.local;callback=5",
                effect="kerberos-context:north.sevenkingdoms.local@callback:5",
                status="achieved",
                evidence={},
                preconditions=[],
                satisfied_effects=["kerberos-context:north.sevenkingdoms.local@callback:5"],
                source="test",
                timestamp="",
            ),
        ],
        graph_facts=[],
    )

    inputs = model._autonomous_capability_inputs(action, snap)

    assert inputs.get("callback_id") == "5", inputs


def test_graph_reconciler_gpo_scope_query_is_ce_compatible():
    """Guardrail: the GPO scope query must not reintroduce the BloodHound-CE-incompatible constructs that
    silently dropped gpo-affects-dc (CASE WHEN / WITH-collect-any). DC-ness must be filtered in WHERE."""
    import inspect
    from ai.langgraph import graph_reconciler as gr
    src = inspect.getsource(gr.reconcile_graph_position)
    # The exact regressing construct (only ever in the broken scope cypher; not in comments/other queries):
    assert "isDc THEN" not in src, "CE-incompatible `CASE WHEN isDc THEN` reintroduced into the scope cypher"
    # And the fix must be present: DC-ness filtered in WHERE via the -516 group objectid.
    assert "ENDS WITH '-516'" in src, "DC-scope must be filtered in WHERE (CE-compatible), not via CASE WHEN"


def test_controller_flag_on_by_default_with_explicit_rollback():
    import os
    saved = os.environ.pop("SAGE_AUTONOMOUS_CONTROLLER", None)
    try:
        assert model._controller_flag_enabled() is True
        os.environ["SAGE_AUTONOMOUS_CONTROLLER"] = "1"
        assert model._controller_flag_enabled() is True
        os.environ["SAGE_AUTONOMOUS_CONTROLLER"] = "off"
        assert model._controller_flag_enabled() is False
        os.environ["SAGE_AUTONOMOUS_CONTROLLER"] = ""
        assert model._controller_flag_enabled() is True
    finally:
        os.environ.pop("SAGE_AUTONOMOUS_CONTROLLER", None)
        if saved is not None:
            os.environ["SAGE_AUTONOMOUS_CONTROLLER"] = saved
