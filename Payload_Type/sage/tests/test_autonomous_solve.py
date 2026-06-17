import importlib
import asyncio
import json
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest
from langchain_core.messages import AIMessage, HumanMessage, ToolMessage
from langgraph.prebuilt.tool_node import ToolCallRequest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "ai" / "langgraph"))
import prompt_loader  # noqa: E402


def _load_model_class():
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
    try:
        mod = importlib.import_module("ai.langgraph.model")
    except Exception as e:
        pytest.skip(f"model.py runtime unavailable: {e}")
    return mod.Model


def _load_model_module():
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
    try:
        return importlib.import_module("ai.langgraph.model")
    except Exception as e:
        pytest.skip(f"model.py runtime unavailable: {e}")


def _engagement_modules():
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
    try:
        return (
            importlib.import_module("ai.langgraph.engagement_state"),
            importlib.import_module("ai.langgraph.mythic_tools"),
        )
    except Exception as e:
        pytest.skip(f"engagement modules unavailable: {e}")


def _fake_hop(es, effect):
    return es.Hop(
        id=f"seed:{effect}",
        technique="seed",
        target="seed",
        effect=effect,
        status="achieved",
        evidence={"provenance": "test"},
        preconditions=[],
        satisfied_effects=[effect],
        source="test",
        timestamp="2026-06-16T12:00:00Z",
    )


def _fake_fact(es, predicate):
    return es.GraphFact(
        predicate=predicate,
        source="bloodhound:test",
        timestamp="2026-06-16T12:00:00Z",
        ttl_seconds=600,
    )


def _fake_foothold(es, forest="north.sevenkingdoms.local", callback_id="3"):
    return es.Foothold(
        callback_id=callback_id,
        agent="apollo",
        host="CASTELBLACK",
        forest=forest,
        identity="NORTH\\samwell.tarly",
        integrity="medium",
        alive=True,
        source="test",
        timestamp="2026-06-16T12:00:00Z",
    )


@pytest.mark.parametrize("autonomous_solve", [False, True])
def test_bounded_one_action_execute_capability_result_ends_graph(autonomous_solve):
    mod = _load_model_module()
    Model = mod.Model

    class FakeAgent:
        def __init__(self):
            self.calls = 0

        async def ainvoke(self, args, config=None):
            self.calls += 1
            payload = {
                "ok": False,
                "verdict": "failed",
                "capability": "ensure-account-kerberos-context",
                "reason": "account key material is missing",
                "issued": [],
                "recorded_effects": [],
            }
            return {
                "messages": list(args["messages"]) + [
                    ToolMessage(
                        content=json.dumps(payload, sort_keys=True),
                        name="execute_capability",
                        tool_call_id="call_1",
                    ),
                    AIMessage(content="Executor verdict: failed.", name="Mythic_Operator"),
                ]
            }

    m = Model.__new__(Model)
    m._autonomous_solve = autonomous_solve
    m._message_seq = 3
    m.state = {"_message_seq": 3}
    m.llm = None
    m.mythic_client = None

    channel = [
        HumanMessage(
            content=(
                "Continue by executing exactly one next grounded capability action using "
                "the generic execute_capability tool. Retry at most once, then stop."
            )
        )
    ]
    state = {
        "_message_seq": 3,
        "supervisor_messages": [],
        "generalist_messages": [],
        "mythic_operator_messages": channel,
        "mythic_payload_messages": [],
        "mcp_manager_messages": [],
        "bloodhound_messages": [],
    }
    fake = FakeAgent()

    wrapped = m._wrap_create_agent(fake, "mythic_operator_messages", "Mythic_Operator")
    result = asyncio.run(wrapped(state, {}))

    assert fake.calls == 1
    assert getattr(result, "goto", None) == mod.END
    update = getattr(result, "update", {})
    assert "supervisor_messages" in update
    assert update["recursion_handback"] is True
    assert update["supervisor_messages"][0].additional_kwargs["_is_final_report"] is True
    assert "Executor verdict: `failed`" in update["supervisor_messages"][0].content
    assert "account key material is missing" in update["supervisor_messages"][0].content
    assert "This was a bounded one-action capability request" in update["supervisor_messages"][0].content


def test_autonomous_handoff_redirects_stale_gpo_redelegation_to_bloodhound():
    mod = _load_model_module()
    state = {
        "supervisor_messages": [
            ToolMessage(
                content=(
                    "🔄 **Handback to Supervisor** — Need graph-supported next hop selection.\n\n"
                    "**DONE (do NOT repeat):** STARKWALLPAPER GPO hop completed; net group "
                    "\"Domain Admins\" /domain includes samwell.tarly; PAC refresh proven on callback:3.\n"
                    "**FAILED (do NOT blindly retry):** sevenkingdoms krbtgt DCSync failed twice with "
                    "0x20f7/8439.\n"
                    "**BLOCKER / MISSING CAPABILITY:** Need graph-supported next action for Essos path; "
                    "route to BloodHound."
                ),
                name="handback_to_supervisor",
                tool_call_id="h1",
            )
        ],
        "messages": [],
    }

    redirect = mod._autonomous_handoff_redirect(
        "Mythic_Operator",
        (
            "Execute the next graph-supported hop: use SharpGPOAbuse on STARKWALLPAPER GPO to add "
            "samwell.tarly to Domain Admins with net group."
        ),
        state,
    )

    assert redirect is not None
    target, instruction = redirect
    assert target == "BloodHound"
    assert "callback 3" in instruction
    assert "Do not repeat the STARKWALLPAPER/GPO hop" in instruction
    assert "0x20f7/8439" in instruction


def test_autonomous_handoff_redirects_verified_collection_regression_to_current_failure():
    mod = _load_model_module()
    state = {
        "mythic_operator_messages": [
            ToolMessage(
                content=json.dumps({
                    "ok": True,
                    "graph_verified": True,
                    "job_status": "Complete",
                    "domains": ["north.sevenkingdoms.local", "sevenkingdoms.local"],
                }),
                name="ingest_collection",
                tool_call_id="ingest_1",
            ),
            ToolMessage(
                content=json.dumps({
                    "ok": False,
                    "verdict": "failed",
                    "capability": "gpo-controlled-system-exec",
                    "reason": "PowerShell Put operation failed while updating the GPO object.",
                    "issued": [{"task_id": 6, "command": "powerpick"}],
                }),
                name="execute_capability",
                tool_call_id="cap_1",
            ),
        ],
        "supervisor_messages": [],
        "messages": [],
    }

    redirect = mod._autonomous_handoff_redirect(
        "Mythic_Operator",
        (
            "Continue the objective. BloodHound domain_info was empty and SharpHound was started, but "
            "completion and ZIP path are not yet confirmed. Do not rerun SharpHound. List C:\\Users\\Public, "
            "download the exact ZIP, run ingest_collection, verify graph_verified, then stop."
        ),
        state,
    )

    assert redirect is not None
    target, instruction = redirect
    assert target == "Mythic_Operator"
    assert "graph_verified=true" in instruction
    assert "Do not perform SharpHound collection confirmation" in instruction
    assert "`gpo-controlled-system-exec` returned `failed`" in instruction
    assert "task(s) 6" in instruction
    assert "Do not regress to collection work" in instruction


def test_autonomous_handoff_redirects_verified_collection_regression_to_bloodhound_without_failure():
    mod = _load_model_module()
    state = {
        "mythic_operator_messages": [
            ToolMessage(
                content='{"ok": true, "graph_verified": true, "job_status": "Complete"}',
                name="ingest_collection",
                tool_call_id="ingest_1",
            ),
        ],
        "messages": [],
    }

    redirect = mod._autonomous_handoff_redirect(
        "Mythic_Operator",
        "SharpHound completion and ZIP path are not confirmed; download the ZIP and ingest_collection.",
        state,
    )

    assert redirect is not None
    target, instruction = redirect
    assert target == "BloodHound"
    assert "already shows `graph_verified=true`" in instruction
    assert "Analyze the verified BloodHound graph" in instruction


def test_autonomous_handoff_redirects_stale_kerberos_context_redelegation_to_dcsync():
    mod = _load_model_module()
    state = {
        "mythic_operator_messages": [
            ToolMessage(
                content=json.dumps({
                    "ok": True,
                    "verdict": "achieved",
                    "capability": "ensure-kerberos-context",
                    "recorded_effects": ["kerberos-context:north.sevenkingdoms.local@callback:3"],
                    "issued": [
                        {"task_id": 31, "command": "shell", "parameters": "klist"},
                        {
                            "task_id": 32,
                            "command": "shell",
                            "parameters": r"dir \\winterfell.north.sevenkingdoms.local\C$",
                        },
                    ],
                }),
                name="execute_capability",
                tool_call_id="cap_context",
            ),
        ],
        "supervisor_messages": [],
        "messages": [],
    }

    redirect = mod._autonomous_handoff_redirect(
        "Mythic_Operator",
        (
            "Continue the autonomous GOAD solve toward essos.local from callback 3. Verify NORTH Domain "
            "Admins membership and current token/ticket state, refresh Kerberos/PAC if membership is present, "
            "then proceed to NORTH DCSync remotely from CASTELBLACK using NETBIOS-qualified NORTH\\krbtgt."
        ),
        state,
    )

    assert redirect is not None
    target, instruction = redirect
    assert target == "Mythic_Operator"
    assert "already recorded `kerberos-context:north.sevenkingdoms.local@callback:3`" in instruction
    assert "Do not repeat Domain Admins membership checks" in instruction
    assert "DCSync `NORTH\\krbtgt`" in instruction
    assert "`krbtgt-hash:north.sevenkingdoms.local`" in instruction


def test_autonomous_handoff_redirects_stale_gpo_after_krbtgt_to_ticket_forge():
    mod = _load_model_module()
    state = {
        "mythic_operator_messages": [
            ToolMessage(
                content=json.dumps({
                    "ok": True,
                    "verdict": "achieved",
                    "capability": "dcsync-account",
                    "achieved_effects": [
                        "system-exec:gpo:starkwallpaper@north.sevenkingdoms.local",
                        "da:north.sevenkingdoms.local",
                        "kerberos-context:north.sevenkingdoms.local@callback:3",
                        "krbtgt-hash:north.sevenkingdoms.local",
                        "creds:administrator@north.sevenkingdoms.local",
                    ],
                    "recorded_effects": ["creds:administrator@north.sevenkingdoms.local"],
                }),
                name="execute_capability",
                tool_call_id="cap_admin",
            ),
        ],
        "supervisor_messages": [],
        "messages": [],
    }

    redirect = mod._autonomous_handoff_redirect(
        "Mythic_Operator",
        (
            "Continue the autonomous GOAD objective. Prior work only staged a proof-only GPP task for "
            "STARKWALLPAPER and did not add samwell.tarly to NORTH Domain Admins. Execute "
            "gpo-controlled-system-exec with command cmd.exe and arguments /c net group "
            "\"Domain Admins\" samwell.tarly /add /domain, then DCSync NORTH\\krbtgt."
        ),
        state,
    )

    assert redirect is not None
    target, instruction = redirect
    assert target == "Mythic_Operator"
    assert "already prove the STARKWALLPAPER/GPO chain is past the GPO hop" in instruction
    assert "`krbtgt-hash:north.sevenkingdoms.local` is achieved" in instruction
    assert "`forge-golden-ticket`" in instruction
    assert "`domain=north.sevenkingdoms.local`" in instruction
    assert "`target_domain=sevenkingdoms.local`" in instruction
    assert "Do not repeat GPO abuse" in instruction


def test_autonomous_handoff_redirects_stale_gpo_after_task_history_dcsync_to_ticket_forge():
    mod = _load_model_module()
    state = {
        "mythic_operator_messages": [
            ToolMessage(
                content=(
                    '[{"command_name": "execute_pe", "display_id": 36, "status": "success", '
                    '"display_params": "lsadump::dcsync /domain:north.sevenkingdoms.local '
                    '/dc:WINTERFELL.NORTH.SEVENKINGDOMS.LOCAL /user:NORTH\\\\krbtgt"}]'
                ),
                name="get_task_history_for_callback",
                tool_call_id="hist_1",
            ),
            HumanMessage(
                content=(
                    "=== ENGAGEMENT STATE ===\n"
                    "Achieved hops:\n"
                    "- capability:ensure-kerberos-context: kerberos-context:north.sevenkingdoms.local@callback:3\n"
                )
            ),
        ],
        "supervisor_messages": [],
        "messages": [],
    }

    redirect = mod._autonomous_handoff_redirect(
        "Mythic_Operator",
        (
            "Continue the autonomous GOAD objective. The prior proof-only STARKWALLPAPER task did not add "
            "samwell.tarly to Domain Admins; execute gpo-controlled-system-exec with net group "
            "\"Domain Admins\" samwell.tarly /add /domain, then DCSync NORTH\\krbtgt."
        ),
        state,
    )

    assert redirect is not None
    target, instruction = redirect
    assert target == "Mythic_Operator"
    assert "`krbtgt-hash:north.sevenkingdoms.local` is achieved" in instruction
    assert "`forge-golden-ticket`" in instruction


def test_autonomous_step_gate_compiles_bloodhound_handoff_to_next_capability(monkeypatch):
    mod = _load_model_module()
    es, mythic_tools = _engagement_modules()
    monkeypatch.setattr(mythic_tools, "ENGAGEMENT_GATE_ENABLED", True)

    class FakeMythic:
        _engagement_footholds = [_fake_foothold(es)]
        _engagement_hops = [
            _fake_hop(es, "system-exec:gpo:starkwallpaper@north.sevenkingdoms.local"),
            _fake_hop(es, "da:north.sevenkingdoms.local"),
            _fake_hop(es, "kerberos-context:north.sevenkingdoms.local@callback:3"),
            _fake_hop(es, "krbtgt-hash:north.sevenkingdoms.local"),
        ]
        _engagement_graph_facts = [
            _fake_fact(es, "generic-write:gpo:starkwallpaper"),
            _fake_fact(es, "gpo-domain:starkwallpaper:north.sevenkingdoms.local"),
            _fake_fact(es, "credential-target:arya.stark@north.sevenkingdoms.local"),
        ]

        def _engagement_objective(self):
            return "compromise essos.local"

    model = mod.Model.__new__(mod.Model)
    model._autonomous_solve = True
    model.mythic_client = FakeMythic()

    redirect = model._autonomous_handoff_step_redirect(
        "BloodHound",
        "Analyze the graph again and return the next STARKWALLPAPER/GPO hop.",
        {},
    )

    assert redirect is not None
    target, instruction = redirect
    assert target == "Autonomous_Executor"
    assert "AUTONOMOUS STEP DRIVER" in instruction
    assert '"name": "forge-golden-ticket"' in instruction
    assert '"target_domain": "sevenkingdoms.local"' in instruction
    assert '"callback_id": "3"' in instruction
    assert "supersedes the delegated prose from BloodHound" in instruction
    assert "STARKWALLPAPER" in instruction
    assert mod._is_bounded_one_action_capability_request([HumanMessage(content=instruction)]) is False


def test_autonomous_step_gate_compiles_collection_needed_over_stale_gpo(monkeypatch):
    mod = _load_model_module()
    es, mythic_tools = _engagement_modules()
    monkeypatch.setattr(mythic_tools, "ENGAGEMENT_GATE_ENABLED", True)

    foothold = _fake_foothold(es)
    old_access_key = es.access_context_key(es.EngagementState(objective="compromise essos.local", footholds=[foothold]), foothold)

    class FakeMythic:
        _engagement_footholds = [foothold]
        _engagement_hops = [
            _fake_hop(es, f"graph-built:{old_access_key}"),
            _fake_hop(es, "da:north.sevenkingdoms.local"),
            _fake_hop(es, "kerberos-context:north.sevenkingdoms.local@callback:3"),
            _fake_hop(es, "krbtgt-hash:north.sevenkingdoms.local"),
            _fake_hop(es, "da:sevenkingdoms.local"),
            _fake_hop(es, "kerberos-context:sevenkingdoms.local@callback:3"),
            _fake_hop(es, "krbtgt-hash:sevenkingdoms.local"),
        ]
        _engagement_graph_facts = [
            _fake_fact(es, "generic-write:computer:kingslanding"),
            _fake_fact(es, "write-dacl:domain:sevenkingdoms.local"),
        ]

        def _engagement_objective(self):
            return "obtain administrative control of essos.local"

    model = mod.Model.__new__(mod.Model)
    model._autonomous_solve = True
    model.mythic_client = FakeMythic()

    redirect = model._autonomous_handoff_step_redirect(
        "Mythic_Operator",
        "Execute the STARKWALLPAPER GPO Domain Admins add again with SharpGPOAbuse.",
        {},
    )

    assert redirect is not None
    target, instruction = redirect
    assert target == "Mythic_Operator"
    assert "AUTONOMOUS COLLECTION DRIVER" in instruction
    assert "callback 3" in instruction
    assert "one NEW SharpHound collection" in instruction
    assert "Do not use task history, task 5" in instruction
    assert "callback_display_id=3" in instruction
    assert "Do not run GPO abuse" in instruction
    assert "STARKWALLPAPER" in instruction


def test_autonomous_step_gate_routes_blocked_mythic_handoff_to_bloodhound(monkeypatch):
    mod = _load_model_module()
    es, mythic_tools = _engagement_modules()
    monkeypatch.setattr(mythic_tools, "ENGAGEMENT_GATE_ENABLED", True)

    foothold = _fake_foothold(es)
    state_with_control = es.EngagementState(
        objective="obtain administrative control of essos.local",
        footholds=[foothold],
        hops=[
            _fake_hop(es, "da:sevenkingdoms.local"),
            _fake_hop(es, "kerberos-context:sevenkingdoms.local@callback:3"),
            _fake_hop(es, "krbtgt-hash:sevenkingdoms.local"),
        ],
    )
    current_key = es.access_context_key(state_with_control, foothold)

    class FakeMythic:
        _engagement_footholds = [foothold]
        _engagement_hops = [
            *state_with_control.hops,
            _fake_hop(es, f"graph-built:{current_key}"),
        ]
        _engagement_graph_facts = [_fake_fact(es, "domain-collected:sevenkingdoms.local")]

        def _engagement_objective(self):
            return "obtain administrative control of essos.local"

    model = mod.Model.__new__(mod.Model)
    model._autonomous_solve = True
    model.mythic_client = FakeMythic()

    redirect = model._autonomous_handoff_step_redirect(
        "Mythic_Operator",
        "Execute STARKWALLPAPER again.",
        {"bloodhound_messages": []},
    )

    assert redirect is not None
    target, instruction = redirect
    assert target == "BloodHound"
    assert "AUTONOMOUS BLOCKED-STATE GRAPH ANALYSIS" in instruction
    assert "Do not ask Mythic_Operator to rerun GPO abuse" in instruction
    assert "Execute STARKWALLPAPER again" in instruction


def test_autonomous_step_gate_terminalizes_blocked_after_bloodhound_blocker(monkeypatch):
    mod = _load_model_module()
    es, mythic_tools = _engagement_modules()
    monkeypatch.setattr(mythic_tools, "ENGAGEMENT_GATE_ENABLED", True)

    foothold = _fake_foothold(es)
    state_with_control = es.EngagementState(
        objective="obtain administrative control of essos.local",
        footholds=[foothold],
        hops=[
            _fake_hop(es, "da:sevenkingdoms.local"),
            _fake_hop(es, "kerberos-context:sevenkingdoms.local@callback:3"),
            _fake_hop(es, "krbtgt-hash:sevenkingdoms.local"),
        ],
    )
    current_key = es.access_context_key(state_with_control, foothold)

    class FakeMythic:
        _engagement_footholds = [foothold]
        _engagement_hops = [
            *state_with_control.hops,
            _fake_hop(es, f"graph-built:{current_key}"),
        ]
        _engagement_graph_facts = [_fake_fact(es, "domain-collected:sevenkingdoms.local")]

        def _engagement_objective(self):
            return "obtain administrative control of essos.local"

    model = mod.Model.__new__(mod.Model)
    model._autonomous_solve = True
    model.mythic_client = FakeMythic()
    runtime_state = {
        "bloodhound_messages": [
            AIMessage(content=(
                "BLOCKER / MISSING CAPABILITY: ESSOS.LOCAL collected=false; users: `0`; groups: `0`; "
                "computers: `0`; no modeled hop available."
            ))
        ],
        "supervisor_messages": [],
        "messages": [],
    }

    redirect = model._autonomous_handoff_step_redirect(
        "Mythic_Operator",
        "Execute STARKWALLPAPER again.",
        runtime_state,
    )

    assert redirect is not None
    target, instruction = redirect
    assert target == "__terminal__"
    assert "AUTONOMOUS BLOCKED REPORT" in instruction
    assert "No reset is indicated" in instruction
    assert "Suppressed handoff text: Execute STARKWALLPAPER again." in instruction


def test_autonomous_executor_node_calls_execute_capability_once(monkeypatch):
    mod = _load_model_module()
    es, mythic_tools = _engagement_modules()
    monkeypatch.setattr(mythic_tools, "ENGAGEMENT_GATE_ENABLED", True)

    class FakeMythic:
        _engagement_footholds = [_fake_foothold(es)]
        _engagement_hops = [
            _fake_hop(es, "da:sevenkingdoms.local"),
            _fake_hop(es, "kerberos-context:sevenkingdoms.local@callback:3"),
        ]
        _engagement_graph_facts = []

        def __init__(self):
            self.calls = []

        def _engagement_objective(self):
            return "compromise essos.local"

        async def execute_capability(self, action, inputs):
            self.calls.append((action, inputs))
            return json.dumps({
                "ok": True,
                "verdict": "achieved",
                "capability": action["name"],
                "issued": [{"task_id": 60, "command": "dcsync"}],
                "recorded_effects": ["krbtgt-hash:sevenkingdoms.local"],
                "achieved_effects": ["krbtgt-hash:sevenkingdoms.local"],
            }, sort_keys=True)

    fake_mythic = FakeMythic()
    model = mod.Model.__new__(mod.Model)
    model._autonomous_solve = True
    model.mythic_client = fake_mythic
    model._message_seq = 0
    model.state = {"_message_seq": 0}

    action = {
        "name": "dcsync-krbtgt",
        "target": "domain=sevenkingdoms.local;account=krbtgt",
        "preconditions": ["kerberos-context:sevenkingdoms.local@callback:3"],
        "effects": ["krbtgt-hash:sevenkingdoms.local"],
        "intent": {"capability": "dcsync-krbtgt", "domain": "sevenkingdoms.local", "account": "krbtgt"},
        "verifier": {"achieved_any": ["krbtgt_hash_present"]},
        "reason": "test",
        "source_facts": ["kerberos-context:sevenkingdoms.local@callback:3"],
    }
    instruction = (
        "AUTONOMOUS STEP DRIVER: test\n"
        f"`action={json.dumps(action, sort_keys=True)}`\n"
        '`inputs={"callback_id": "3"}`'
    )
    state = {
        "messages": [],
        "supervisor_messages": [],
        "mythic_operator_messages": [],
        "bloodhound_messages": [],
        "autonomous_executor_messages": [HumanMessage(content=instruction)],
    }

    command = asyncio.run(model._autonomous_executor_node(state))

    assert command.goto == "Supervisor"
    assert len(fake_mythic.calls) == 1
    called_action, called_inputs = fake_mythic.calls[0]
    assert called_action["name"] == "dcsync-krbtgt"
    assert called_inputs == {"callback_id": "3"}
    update = command.update
    assert "mythic_operator_messages" not in update
    assert update["messages"][0].name == "execute_capability"
    assert "Executor verdict: `achieved` for `dcsync-krbtgt`" in update["supervisor_messages"][0].content


def test_autonomous_step_gate_is_inactive_outside_autonomous_mode(monkeypatch):
    mod = _load_model_module()
    es, mythic_tools = _engagement_modules()
    monkeypatch.setattr(mythic_tools, "ENGAGEMENT_GATE_ENABLED", True)

    class FakeMythic:
        _engagement_footholds = [_fake_foothold(es)]
        _engagement_hops = [_fake_hop(es, "krbtgt-hash:north.sevenkingdoms.local")]
        _engagement_graph_facts = []

        def _engagement_objective(self):
            return "compromise essos.local"

    model = mod.Model.__new__(mod.Model)
    model._autonomous_solve = False
    model.mythic_client = FakeMythic()

    assert model._autonomous_handoff_step_redirect(
        "BloodHound",
        "Analyze the graph.",
        {},
    ) is None


def test_handoff_tool_uses_autonomous_redirect_target_channel():
    mod = _load_model_module()
    tool = mod._create_handoff_tool(
        agent_name="BloodHound",
        autonomous_redirect=lambda agent, instruction, state: (
            "Autonomous_Executor",
            "compiled deterministic capability instruction",
        ),
    )
    old_supervisor_msg = HumanMessage(content="old supervisor context")
    runtime = SimpleNamespace(
        state={
            "messages": [old_supervisor_msg],
            "supervisor_messages": [old_supervisor_msg],
            "mythic_operator_messages": [],
            "generalist_messages": [],
            "mythic_payload_messages": [],
            "mcp_manager_messages": [],
            "bloodhound_messages": [],
            "autonomous_executor_messages": [],
        },
        tool_call_id="handoff-test",
    )

    command = tool.func(runtime, "stale BloodHound planner handoff")

    assert command.goto == "Autonomous_Executor"
    assert "autonomous_executor_messages" in command.update
    assert "bloodhound_messages" not in command.update
    assert command.update["autonomous_executor_messages"][1].content == "compiled deterministic capability instruction"


def test_handoff_tool_terminal_redirect_sets_recursion_handback():
    mod = _load_model_module()
    tool = mod._create_handoff_tool(
        agent_name="Mythic_Operator",
        autonomous_redirect=lambda agent, instruction, state: (
            "__terminal__",
            "AUTONOMOUS BLOCKED REPORT: no modeled hop",
        ),
    )
    runtime = SimpleNamespace(
        state={
            "messages": [],
            "supervisor_messages": [],
            "mythic_operator_messages": [],
            "generalist_messages": [],
            "mythic_payload_messages": [],
            "mcp_manager_messages": [],
            "bloodhound_messages": [],
            "autonomous_executor_messages": [],
        },
        tool_call_id="handoff-terminal",
    )

    command = tool.func(runtime, "stale GPO")

    assert command.goto == "Supervisor"
    assert command.update["recursion_handback"] is True
    assert "supervisor_messages" in command.update
    assert command.update["supervisor_messages"][1].content == "AUTONOMOUS BLOCKED REPORT: no modeled hop"


def test_autonomous_handoff_redirect_does_not_block_collection_without_tool_evidence():
    mod = _load_model_module()
    state = {
        "supervisor_messages": [
            HumanMessage(content="The prompt mentioned graph_verified=true as a desired future state.")
        ],
        "messages": [],
    }

    redirect = mod._autonomous_handoff_redirect(
        "Mythic_Operator",
        "SharpHound completion and ZIP path are not confirmed; download the ZIP and ingest_collection.",
        state,
    )

    assert redirect is None


def test_autonomous_handoff_redirect_ignores_new_mythic_work():
    mod = _load_model_module()
    state = {
        "supervisor_messages": [
            ToolMessage(
                content=(
                    "🔄 **Handback to Supervisor** — Need graph-supported next hop selection.\n\n"
                    "**DONE (do NOT repeat):** STARKWALLPAPER GPO hop completed.\n"
                    "**BLOCKER / MISSING CAPABILITY:** Need graph-supported next action for Essos path; "
                    "route to BloodHound."
                ),
                name="handback_to_supervisor",
                tool_call_id="h1",
            )
        ],
    }

    redirect = mod._autonomous_handoff_redirect(
        "Mythic_Operator",
        "Execute the graph-selected LAPS read for BRAAVOS from callback 3.",
        state,
    )

    assert redirect is None


def test_handoff_tool_returns_delta_only_for_add_reducers():
    mod = _load_model_module()
    tool = mod._create_handoff_tool(agent_name="Mythic_Operator")
    old_supervisor_msg = HumanMessage(content="old supervisor context")
    runtime = SimpleNamespace(
        state={
            "messages": [old_supervisor_msg],
            "supervisor_messages": [old_supervisor_msg],
            "mythic_operator_messages": [],
            "generalist_messages": [],
            "mythic_payload_messages": [],
            "mcp_manager_messages": [],
            "bloodhound_messages": [],
        },
        tool_call_id="handoff-test",
    )

    command = tool.func(runtime, "Run execute_capability dcsync-krbtgt once.")

    assert command.goto == "Mythic_Operator"
    assert "supervisor_messages" not in command.update
    assert command.update["messages"] == command.update["mythic_operator_messages"]
    assert len(command.update["messages"]) == 2
    assert all(msg is not old_supervisor_msg for msg in command.update["messages"])


def test_terminal_execute_capability_report_includes_existing_proof_chain():
    mod = _load_model_module()

    report = mod._terminal_execute_capability_report({
        "ok": True,
        "verdict": "achieved",
        "capability": "ensure-kerberos-context",
        "reason": "requested capability effect is already achieved",
        "issued": [],
        "recorded_effects": [],
        "proof_chain": [
            {"effect": "kerberos-context:essos.local@callback:14", "task_id": "670", "callback_id": "14"},
            {"effect": "da:essos.local", "task_id": "655"},
        ],
    })

    assert "Proof chain:" in report
    assert "`kerberos-context:essos.local@callback:14` task=670 cb=14" in report
    assert "`da:essos.local` task=655" in report


def test_partial_execute_capability_result_is_terminal_for_bounded_action():
    mod = _load_model_module()
    payload = {
        "ok": False,
        "verdict": "partial",
        "capability": "dcsync-krbtgt",
        "reason": "precheck blocked actual tasking",
        "issued": [{"command": "dcsync", "task_id": None}],
        "recorded_effects": [],
    }

    terminal = mod._terminal_execute_capability_payload([
        ToolMessage(
            content=json.dumps(payload, sort_keys=True),
            name="execute_capability",
            tool_call_id="call_1",
        )
    ])

    assert terminal is not None
    assert terminal["verdict"] == "partial"
    assert "precheck blocked" in mod._terminal_execute_capability_report(terminal)


def test_bounded_execute_capability_middleware_ends_agent_loop_after_terminal_result():
    mod = _load_model_module()
    payload = {
        "ok": True,
        "verdict": "achieved",
        "capability": "dcsync-krbtgt",
        "recorded_effects": ["krbtgt-hash:essos.local"],
    }
    messages = [
        HumanMessage(
            content=(
                "Continue by executing exactly one next grounded capability action using "
                "the generic execute_capability tool. Retry at most once, then stop."
            )
        ),
        ToolMessage(
            content=json.dumps(payload, sort_keys=True),
            name="execute_capability",
            tool_call_id="call_1",
        ),
    ]
    mw = mod._BoundedExecuteCapabilityStopMiddleware(model=object())

    update = mw.before_model({"messages": messages}, runtime=None)

    assert update == {"jump_to": "end"}


def test_execute_capability_boundary_ends_agent_loop_without_bounded_prompt():
    mod = _load_model_module()
    payload = {
        "ok": True,
        "verdict": "achieved",
        "capability": "execute-as-local-admin",
        "recorded_effects": ["remote-exec:braavos@essos.local"],
    }
    messages = [
        HumanMessage(content="Continue toward ESSOS administrative control from the current state."),
        ToolMessage(
            content=json.dumps(payload, sort_keys=True),
            name="execute_capability",
            tool_call_id="call_1",
        ),
    ]
    mw = mod._BoundedExecuteCapabilityStopMiddleware(model=object())

    update = mw.before_model({"messages": messages}, runtime=None)

    assert update == {"jump_to": "end"}


def test_bounded_execute_capability_middleware_skips_sibling_tools_without_executing():
    mod = _load_model_module()
    messages = [
        HumanMessage(
            content=(
                "Continue by executing exactly one next grounded capability action using "
                "the generic execute_capability tool. Retry at most once, then stop."
            )
        ),
        AIMessage(
            content="",
            tool_calls=[
                {"name": "execute_capability", "args": {"action": {"name": "dcsync-krbtgt"}}, "id": "call_exec"},
                {"name": "issue_task_and_waitfor_task_output", "args": {"command": "dcsync"}, "id": "call_extra"},
            ],
        ),
    ]
    request = ToolCallRequest(
        tool_call={"name": "issue_task_and_waitfor_task_output", "args": {}, "id": "call_extra"},
        tool=None,
        state={"messages": messages},
        runtime=None,
    )
    mw = mod._BoundedExecuteCapabilityStopMiddleware(model=object())
    called = False

    def handler(_request):
        nonlocal called
        called = True
        return ToolMessage(content="side effect", name="issue_task_and_waitfor_task_output", tool_call_id="call_extra")

    result = mw.wrap_tool_call(request, handler)

    assert called is False
    assert isinstance(result, ToolMessage)
    assert result.name == "issue_task_and_waitfor_task_output"
    payload = json.loads(result.content)
    assert payload["verdict"] == "blocked"
    assert "skipped sibling tool" in payload["reason"]


def test_execute_capability_boundary_skips_sibling_tools_without_bounded_prompt():
    mod = _load_model_module()
    messages = [
        HumanMessage(content="Continue toward ESSOS administrative control from the current state."),
        AIMessage(
            content="",
            tool_calls=[
                {"name": "execute_capability", "args": {"action": {"name": "execute-as-local-admin"}}, "id": "call_exec"},
                {"name": "issue_task_and_waitfor_task_output", "args": {"command": "dcsync"}, "id": "call_extra"},
            ],
        ),
    ]
    request = ToolCallRequest(
        tool_call={"name": "issue_task_and_waitfor_task_output", "args": {}, "id": "call_extra"},
        tool=None,
        state={"messages": messages},
        runtime=None,
    )
    mw = mod._BoundedExecuteCapabilityStopMiddleware(model=object())
    called = False

    def handler(_request):
        nonlocal called
        called = True
        return ToolMessage(content="side effect", name="issue_task_and_waitfor_task_output", tool_call_id="call_extra")

    result = mw.wrap_tool_call(request, handler)

    assert called is False
    payload = json.loads(result.content)
    assert payload["capability"] == "execute-capability-boundary"
    assert "atomic transaction boundary" in payload["reason"]


def test_execute_capability_boundary_allows_regular_unbounded_tool_batches():
    mod = _load_model_module()
    messages = [
        HumanMessage(content="Continue toward ESSOS administrative control from the current state."),
        AIMessage(
            content="",
            tool_calls=[
                {"name": "get_task_history_for_callback", "args": {"callback_id": "3"}, "id": "call_history"},
            ],
        ),
    ]
    request = ToolCallRequest(
        tool_call={"name": "get_task_history_for_callback", "args": {}, "id": "call_history"},
        tool=None,
        state={"messages": messages},
        runtime=None,
    )
    mw = mod._BoundedExecuteCapabilityStopMiddleware(model=object())
    called = False

    def handler(_request):
        nonlocal called
        called = True
        return ToolMessage(content="ok", name="get_task_history_for_callback", tool_call_id="call_history")

    result = mw.wrap_tool_call(request, handler)

    assert called is True
    assert result.content == "ok"


def test_bounded_execute_capability_middleware_is_opt_in_for_operator_only():
    mod = _load_model_module()
    Model = mod.Model
    m = Model.__new__(Model)
    m.mode = "auto"
    m._get_base_chat_model = lambda: None

    default_names = [type(item).__name__ for item in m._context_middleware()]
    operator_names = [
        type(item).__name__
        for item in m._context_middleware(bounded_execute_stop=True)
    ]

    assert "_BoundedExecuteCapabilityStopMiddleware" not in default_names
    assert "_BoundedExecuteCapabilityStopMiddleware" in operator_names
