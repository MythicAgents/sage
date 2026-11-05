"""Supervised steps record proven capability gains in the engagement ledger.

The ledger records progress, not events. Pure cases cover how callback context gains are found and credited and the
`mythic_callback` proof origin. Writer cases drive `MythicTools._record_step_ledger` with recorded Mythic rows,
including persistence and a reload through the load quarantine. An assembled case drives the real graph through
the scripted handoff harness with a real MythicTools client whose Mythic readers serve recorded rows.
"""

from __future__ import annotations
from tests.esl_fixtures import seed_ledger  # noqa: E402

import asyncio
import importlib.util
import json
from pathlib import Path
from typing import Any

from langchain_core.messages import AIMessage
from langchain_core.tools import tool
import pytest

from ai.langgraph import engagement_state, proof_boundary
from ai.langgraph import model as model_module
from ai.langgraph import step_bounds as sb
from ai.langgraph import step_ledger as sl
from ai.langgraph import mythic_tools as mythic_tools_module
from ai.langgraph.mythic_tools import MythicTools

SECRET = "Autumn2026!-unique-secret"
TASK_AT = "2026-10-30T10:00:00"


def _callback(display_id: int, host: str, user: str, integrity: int, init: str = "2026-10-30T10:02:00") -> dict:
    return {"display_id": display_id, "agent_callback_id": f"agent-{display_id}", "host": host, "user": user,
            "integrity_level": integrity, "init_callback": init}


def _ctx(*args, **kwargs) -> sl.CallbackContext:
    return sl.callback_context(_callback(*args, **kwargs))


def _task(task_id: int, callback: int = 1, status: str = "success", at: str = TASK_AT) -> sl.IssuedTask:
    return sl.IssuedTask(task_id=task_id, callback_display_id=callback, command="jump_psexec", status=status,
                         timestamp=at)


# --- finding and crediting gains -------------------------------------------------------------------------------

def test_a_callback_context_normalizes_mythic_integrity_and_identity():
    assert _ctx(7, "SRV02", "CONTOSO\\svc", 4).integrity == "system"
    assert _ctx(7, "SRV02", "NT AUTHORITY\\SYSTEM", 2).integrity == "system"
    assert (_ctx(7, "SRV02", "CONTOSO\\svc", 3).host, _ctx(7, "SRV02", "x", 3).integrity) == ("srv02", "high")
    assert sl.callback_context({"display_id": 7, "host": "x"}) is None  # no agent callback id


def test_a_new_callback_is_credited_to_the_latest_task_before_it_checked_in():
    gains = sl.context_gains([_ctx(7, "SRV02", "NT AUTHORITY\\SYSTEM", 4)], {},
                             [_task(89, at="2026-10-30T09:55:00"), _task(90), _task(91, at="2026-10-30T10:05:00")],
                             step_tasks=[90])
    [gain] = gains
    assert (gain.kind, gain.task.task_id) == ("new_callback", 90)
    assert gain.effects == ("session:nt authority\\system@srv02", "system:srv02")


@pytest.mark.parametrize("init", ["2026-10-30T10:11:00", "2026-10-30T09:59:00"])
def test_a_callback_outside_the_window_or_before_the_task_is_not_credited(init):
    [gain] = sl.context_gains([_ctx(7, "SRV02", "CONTOSO\\svc", 2, init=init)], {}, [_task(90)], step_tasks=[90])
    assert gain.task is None


def test_an_integrity_rise_is_credited_to_this_steps_task_on_that_callback():
    seen = {3: _ctx(3, "WS01", "CONTOSO\\jdoe", 2)}
    risen = [_ctx(3, "WS01", "CONTOSO\\jdoe", 4)]
    [gain] = sl.context_gains(risen, seen, [_task(90, callback=3)], step_tasks=[90])
    assert (gain.kind, gain.effects, gain.task.task_id) == ("integrity_rise", ("system:ws01",), 90)
    [elsewhere] = sl.context_gains(risen, seen, [_task(90, callback=4)], step_tasks=[90])
    assert elsewhere.task is None  # a task on another callback did not raise this one


@pytest.mark.parametrize("now", [
    [_ctx(3, "WS01", "CONTOSO\\jdoe", 2)],      # unchanged
    [_ctx(3, "WS01", "CONTOSO\\jdoe", 1)],      # integrity dropped (rev2self)
    [],                                         # callback died
])
def test_unchanged_or_lost_contexts_are_not_gains(now):
    assert sl.context_gains(now, {3: _ctx(3, "WS01", "CONTOSO\\jdoe", 2)}, [_task(90, callback=3)], [90]) == []


def test_the_first_observation_is_a_baseline_not_unattributed_gains():
    gains = sl.context_gains([_ctx(3, "WS01", "CONTOSO\\jdoe", 2)], {}, [], step_tasks=[])
    assert sl.unattributed(gains, first_observation=True) == []
    assert sl.unattributed(gains, first_observation=False) == [
        {"callback_display_id": 3, "kind": "new_callback", "effects": ["session:contoso\\jdoe@ws01"]}]


# --- the mythic_callback proof origin --------------------------------------------------------------------------

def _envelope(**overrides) -> proof_boundary.ProofEnvelope:
    context = _ctx(7, "SRV02", "NT AUTHORITY\\SYSTEM", 4)
    fields = dict(engagement_id="eng", callback_id=7, task_id=90, terminal_status="success", command="jump_psexec",
                  agent_callback_id=context.agent_callback_id, callback_row=context.row(),
                  verifier_id=sl.VERIFIER_ID, captured_at="2026-10-30T10:03:00", transaction_id="callback-context:7:90",
                  verifier_result={"kind": "new_callback", "effects": ["system:srv02"]})
    fields.update(overrides)
    return proof_boundary.make_runtime_callback_envelope(**fields)


def test_a_callback_proof_is_admitted_only_with_its_callback_row_and_a_successful_task():
    admit = lambda envelope, **kw: proof_boundary.admit_runtime_envelope(envelope, current_engagement_id="eng", **kw)
    good = _envelope()
    assert good.origin == proof_boundary.ORIGIN_MYTHIC_CALLBACK and admit(good, expected_artifact_id="agent-7").admitted
    assert not admit(good, expected_artifact_id="agent-8").admitted
    assert not admit(proof_boundary.ProofEnvelope(**{**good.__dict__, "artifact_sha256": ""})).admitted
    assert not admit(_envelope(terminal_status="error: access denied")).admitted
    forged = proof_boundary.ProofEnvelope(**{**good.__dict__, "artifact_sha256": "0" * 64})
    assert forged.artifact_sha256 != proof_boundary.canonical_commitment_sha256(_ctx(7, "SRV02", "x", 2).row())


# --- the writer ------------------------------------------------------------------------------------------------

def _credential(credential_id: int, task_id: int, callback: int | None = 1) -> dict:
    task = {"display_id": task_id, "status": "success", "completed": True, "command_name": "mimikatz",
            "callback": {"display_id": callback} if callback is not None else None}
    return {"id": credential_id, "account": "jdoe", "realm": "CONTOSO", "type": "plaintext",
            "credential_text": SECRET, "task": task}


@pytest.fixture
def ledger(tmp_path, monkeypatch):
    monkeypatch.setattr(mythic_tools_module, "SAGE_ENGAGEMENT_ID", "eng-step-ledger")
    tools = MythicTools(agent_task_id="step-ledger")
    tools.client = object()
    state: dict[str, Any] = {"callbacks": [], "tasks": {}, "credentials": []}

    async def callbacks():
        return state["callbacks"]

    async def records(task_ids):
        return {i: state["tasks"][i] for i in task_ids if i in state["tasks"]}

    async def credentials(now, force=False):
        return state["credentials"]

    tools._step_callback_rows, tools._step_task_records, tools._fetch_credentials_cached = (
        callbacks, records, credentials)
    monkeypatch.setattr(tools, "_engagement_objective", lambda: "")
    path = tmp_path / "sage.db"

    def issue(task_id: int, callback: int = 1, status: str = "success", at: str = TASK_AT) -> None:
        tools._task_source_request_ids[task_id] = "chat:1:request:1"
        state["tasks"][task_id] = {"status": status, "completed": True, "command": "jump_psexec",
                                   "callback_display_id": callback, "timestamp": at}

    def step(task_ids: list[int], credential_refs: list[dict] | None = None) -> dict:
        references = {"tasks": [{"task_id": i} for i in task_ids], "credentials": credential_refs or [],
                      "graph_reads": []}
        return asyncio.run(tools._record_step_ledger(references, task_ids))

    return tools, state, issue, step, path


def _hops(tools) -> dict[str, Any]:
    return {hop.target: hop for hop in tools._engagement_hops}


def test_lateral_movement_to_a_new_system_callback_records_proven_session_and_system(ledger):
    tools, state, issue, step, path = ledger
    state["callbacks"] = [_callback(1, "WS01", "CONTOSO\\jdoe", 2, init="2026-10-30T09:00:00")]
    issue(90)
    assert step([90])["recorded"] == []  # first boundary: the foothold is the baseline
    state["callbacks"].append(_callback(7, "SRV02", "NT AUTHORITY\\SYSTEM", 4))
    summary = step([])  # the beacon checked in after its step ended; credited at the next boundary

    assert [(r["effect"], r["task_id"], r["status"]) for r in summary["recorded"]] == [
        ("session:nt authority\\system@srv02", 90, "achieved"), ("system:srv02", 90, "achieved")]
    hop = _hops(tools)["system:srv02"]
    assert hop.proof_envelope["origin"] == "mythic_callback" and str(hop.evidence["mythic_task_id"]) == "90"
    assert {"system:srv02", "session:nt authority\\system@srv02"} <= engagement_state.EngagementState(
        objective="", hops=tools._engagement_hops, engagement_id="eng-step-ledger", runtime_scope=True,
    ).achieved_effects()

    # Persisted: a fresh session reads the shared store through the runtime quarantine and the proof is re-admitted.
    assert _hops(MythicTools(agent_task_id="fresh-session"))["system:srv02"].status == "achieved"


def test_a_repeat_gain_updates_its_row_and_the_ledger_does_not_grow(ledger):
    tools, state, issue, step, _ = ledger
    state["callbacks"] = [_callback(1, "WS01", "CONTOSO\\jdoe", 2, init="2026-10-30T09:00:00")]
    issue(90)
    step([90])
    state["callbacks"].append(_callback(7, "SRV02", "NT AUTHORITY\\SYSTEM", 4))
    step([])
    count = len(tools._engagement_hops)
    issue(95, at="2026-10-30T10:20:00")
    state["callbacks"].append(_callback(8, "SRV02", "NT AUTHORITY\\SYSTEM", 4, init="2026-10-30T10:21:00"))
    step([95])
    assert len(tools._engagement_hops) == count and str(_hops(tools)["system:srv02"].evidence["mythic_task_id"]) == "95"


def test_a_gain_credited_to_an_errored_task_is_never_achieved(ledger):
    tools, state, issue, step, _ = ledger
    state["callbacks"] = []
    issue(90, status="error: the service did not respond")
    step([90])
    state["callbacks"] = [_callback(7, "SRV02", "NT AUTHORITY\\SYSTEM", 4)]
    step([])
    assert {hop.status for hop in tools._engagement_hops} == {"legacy_unverified"}


def test_gains_with_no_sage_task_and_lost_contexts_write_nothing(ledger):
    tools, state, issue, step, _ = ledger
    state["callbacks"] = [_callback(1, "WS01", "CONTOSO\\jdoe", 2, init="2026-10-30T09:00:00")]
    issue(90)
    step([90])
    state["callbacks"] = [_callback(9, "SRV03", "CONTOSO\\other", 3, init="2026-10-30T12:00:00")]  # 1 died, 9 new
    summary = step([])
    assert tools._engagement_hops == [] and summary["recorded"] == []
    assert summary["unattributed"] == [{"callback_display_id": 9, "kind": "new_callback",
                                        "effects": ["session:contoso\\other@srv03", "admin:srv03"]}]


def test_a_steps_credential_is_recorded_with_lineage_proof_and_without_its_secret(ledger):
    tools, state, issue, step, path = ledger
    state["credentials"] = [_credential(12, 90), _credential(13, 90, callback=None), _credential(14, 77)]
    issue(90)
    refs = [{"credential_id": 12, "source_task_id": 90}, {"credential_id": 13, "source_task_id": 90}]
    summary = step([90], refs)

    # 12 has full lineage; 13 is the same account with no callback, which must not downgrade the proven row.
    assert [(row["effect"], row["status"]) for row in summary["recorded"]] == [("creds:jdoe@contoso", "achieved")]
    assert _hops(tools)["jdoe@contoso"].status == "achieved"
    step([90], refs[1:])  # the weaker observation never downgrades the proven row
    assert _hops(tools)["jdoe@contoso"].status == "achieved"
    export = json.dumps(tools._ledger_store().export(tools._require_ledger_binding()))
    assert SECRET not in export  # rows, payloads and full history carry no secret
    assert all(hop.evidence.get("credential_id") != 14 for hop in tools._engagement_hops)


def test_an_incomplete_credential_lineage_alone_is_no_proof(ledger):
    tools, state, issue, step, _ = ledger
    state["credentials"] = [_credential(13, 90, callback=None)]
    issue(90)
    step([90], [{"credential_id": 13, "source_task_id": 90}])
    assert _hops(tools)["jdoe@contoso"].status == "legacy_unverified"


def test_nothing_is_read_when_this_process_issued_nothing(ledger):
    tools, state, issue, step, _ = ledger

    async def unexpected(*args, **kwargs):
        raise AssertionError("no read expected")

    tools._step_callback_rows = tools._step_task_records = unexpected
    assert step([]) == {"recorded": [], "unattributed": []}


def test_the_rendered_outcome_names_what_the_ledger_recorded():
    outcome = sb.step_outcome(sb.StepRecord("r", 1, "Mythic_Operator", 0.0), sb.StepActivity(), now=1.0)
    outcome["ledger"] = {"recorded": [{"effect": "system:srv02", "task_id": 90, "status": "achieved"}],
                         "unattributed": [{"callback_display_id": 9, "kind": "new_callback",
                                           "effects": ["admin:srv03"]}]}
    text = sb.render_outcome(outcome)
    assert "Engagement ledger: `system:srv02` achieved (task 90)." in text
    assert "callback 9 (admin:srv03)" in text


# --- assembled: the real graph with a real MythicTools client --------------------------------------------------

_HARNESS_PATH = Path(__file__).resolve().with_name("test_scripted_handoff.py")
_SPEC = importlib.util.spec_from_file_location("scripted_handoff_harness_ledger", _HARNESS_PATH)
_harness = importlib.util.module_from_spec(_SPEC)
assert _SPEC and _SPEC.loader
_SPEC.loader.exec_module(_harness)

BRIEF = {"handoff_title": "Probe", "allowed_capabilities": ["control_plane"], "expected_result": "Probe results."}


def _call(name: str, args: dict, call_id: str) -> AIMessage:
    return AIMessage(content="", tool_calls=[{"name": name, "args": args, "id": call_id, "type": "tool_call"}])


def test_a_supervised_step_writes_its_gain_to_the_ledger_on_the_real_graph(monkeypatch, tmp_path):
    monkeypatch.setattr(mythic_tools_module, "SAGE_ENGAGEMENT_ID", "eng-step-ledger")
    tools = MythicTools(agent_task_id="step-ledger-assembled")
    tools.client = object()
    callbacks = [_callback(1, "WS01", "CONTOSO\\jdoe", 2, init="2026-10-30T09:00:00")]
    tools._callback_contexts_seen = {1: sl.callback_context(callbacks[0])}  # an earlier boundary saw the foothold

    @tool
    def baseline_probe_step(step: int) -> str:
        """Test-only probe. Issues a task whose payload checks in as SYSTEM on another host."""
        tools._task_source_request_ids[90] = "chat:1:request:1"
        callbacks.append(_callback(7, "SRV02", "NT AUTHORITY\\SYSTEM", 4))
        return "probe issued task 90"

    async def callback_rows():
        return list(callbacks)

    async def records(task_ids):
        return {90: {"status": "success", "completed": True, "command": "jump_psexec", "callback_display_id": 1,
                     "timestamp": TASK_AT}} if 90 in task_ids else {}

    async def rows(task_id):
        return [], False

    async def credentials(now, force=False):
        return []

    tools._step_callback_rows, tools._step_task_records = callback_rows, records
    tools._task_output_rows, tools._fetch_credentials_cached = rows, credentials
    monkeypatch.setattr(tools, "_engagement_objective", lambda: "")

    original = model_module.filter_tools_by_frontmatter
    monkeypatch.setattr(model_module, "filter_tools_by_frontmatter",
                        lambda agent, ts: [*original(agent, ts), baseline_probe_step]
                        if agent == "mythic_operator" else original(agent, ts))
    harness = _harness._Harness([_call("transfer_to_Mythic_Operator", BRIEF, "h1"),
                                 _call("baseline_probe_step", {"step": 0}, "p0"),
                                 AIMessage(content="Moved to SRV02."), AIMessage(content="final")])
    original_build = harness._build

    def build(script):
        model = original_build(script)
        tools.get_tools = harness.mythic.get_tools  # keep the harness's tool surface; the readers are real
        model.mythic_client = tools
        return model

    harness._build = build
    monkeypatch.setattr(_harness, "_RECURSION_LIMIT", 200)
    result = asyncio.run(harness.run("Move to SRV02.", assisted=True))

    [outcome] = [m.additional_kwargs["_step_outcome"] for m in result["supervisor_messages"]
                 if "_step_outcome" in m.additional_kwargs]
    assert [(r["effect"], r["status"]) for r in outcome["ledger"]["recorded"]] == [
        ("session:nt authority\\system@srv02", "achieved"), ("system:srv02", "achieved")]
    saved = tools._ledger_snapshot().display_hops()  # committed to the shared store
    assert {hop["target"] for hop in saved} == {"session:nt authority\\system@srv02", "system:srv02"}


def test_the_attribution_window_reads_a_positive_override():
    assert sl.attribution_window({}) == 600.0
    assert sl.attribution_window({"SAGE_STEP_LEDGER_ATTRIBUTION_SECONDS": "120"}) == 120.0
    assert sl.attribution_window({"SAGE_STEP_LEDGER_ATTRIBUTION_SECONDS": "-5"}) == 600.0


@pytest.mark.parametrize("operation, scoped", [((4, "Op"), True), (None, False)])
def test_the_callback_read_is_scoped_to_the_resolved_operation(monkeypatch, operation, scoped):
    from ai.langgraph import mythic_tools, operation_context

    async def resolve(client):
        return operation

    seen = {}

    async def query(*, mythic, query, variables):
        seen.update(query=query, variables=variables)
        return {"callback": [_callback(1, "WS01", "CONTOSO\\jdoe", 2), "junk"]}

    monkeypatch.setattr(operation_context, "resolve_operation", resolve)
    monkeypatch.setattr(mythic_tools.mythic, "execute_custom_query", query)
    tools = MythicTools(agent_task_id="step-ledger-read")
    tools.client = object()
    rows = asyncio.run(tools._step_callback_rows())
    assert [row["display_id"] for row in rows] == [1]
    assert ("operation_id" in seen["query"], seen["variables"]) == (scoped, {"op": 4} if scoped else None)
