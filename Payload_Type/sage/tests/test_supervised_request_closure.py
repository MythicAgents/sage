"""Supervised request evidence and execution closure (the 2026-11-09 channel 4 progression).

Each section reproduces one observed failure through the producing and consuming code, with Mythic's transport and
the model stubbed. Identities, keys and task numbers are synthetic; the shapes are the retained evidence's.
"""

from __future__ import annotations

import asyncio
import json

from langchain_core.messages import AIMessage
import pytest

from ai.langgraph import capabilities, engagement_state, task_evidence
from ai.langgraph.mythic_tools import MythicTools
from tests.esl_fixtures import AGENT_PROCESSED
from tests.test_hitl_steer_current_intent import _call
from tests.test_step_bounds import FORGE_ACTION, FORGE_PROMPT, _rescope_run


# --- material-only DCSync feeds the guarded forge only from its proof task --------------------------------------

DOMAIN = "lab.local"
# Synthetic, non-constant material: the credential review rejects a constant value as not a real secret.
KRBTGT_NTLM = "0123456789abcdef" * 2
KRBTGT_KEY = "00112233445566778899aabbccddeeff" * 2
DCSYNC_OK = (
    "[DC] 'lab.local' will be the domain\n** SAM ACCOUNT **\nSAM Username         : krbtgt\nCredentials:\n"
    f"  Hash NTLM: {KRBTGT_NTLM}\n* Primary:Kerberos-Newer-Keys *\n  aes256_hmac       (4096) : {KRBTGT_KEY}\n"
)
MATERIAL = f"cred-material:hash:krbtgt@{DOMAIN}"
# Mythic's record of the DCSync (channel 4 task 15's shape): the agent answered, then the completion function failed.
COMPLETION_ERROR = {"completed": True, "status": task_evidence.MYTHIC_COMPLETION_FUNCTION_ERROR,
                    "callback": {"display_id": 13}, "responses": [{"id": 1}], **AGENT_PROCESSED}


def _material_krbtgt_client(monkeypatch, tmp_path, mythic_task_records, *, output: str = DCSYNC_OK):
    """Run dcsync-krbtgt as task 21 whose Mythic record is a completion-function error; return (tools, issue, result).

    The credential store holds only rows with no producing task (as Sage's own importer wrote them in channel 4)."""
    from ai.langgraph import mythic_tools as package_mythic_tools
    from tests.test_engagement_wiring import _split_issue

    mythic_task_records[21] = COMPLETION_ERROR
    monkeypatch.setenv("SAGE_ENGAGEMENT_STATE_DIR", str(tmp_path / "engagement"))
    mt = MythicTools(agent_task_id="offline-material")
    mt.client = object()
    # The raw writer records (and reviews) the DCSync under a bound request, as in a live run.
    from ai.langgraph.request_contract import build_request_contract
    from ai.langgraph.turn_authority import authority_from_request_contract
    contract = build_request_contract(request_id="chat:4:request:3", channel_id="channel-4", operation_id="operation-1",
                                      mode="auto", autonomous_solve=True)
    mt.set_request_contract(contract)
    mt._turn_authority = authority_from_request_contract(contract)
    mt._engagement_objective = lambda: ""
    mt._require_ledger_binding()
    rows: list[dict] = []

    async def fetch(now, force=False):
        return list(rows)

    async def create(client, credential, account, realm, comment, credential_type):
        rows.append({"id": 500 + len(rows), "account": account, "realm": realm, "type": credential_type,
                     "credential_text": credential, "comment": comment})
        return {"status": "success", "id": rows[-1]["id"]}

    mt._fetch_credentials_cached = fetch
    monkeypatch.setattr(package_mythic_tools.mythic, "create_credential", create)
    outputs = {21: output}

    async def task_output(mythic, task_display_id, custom_return_attributes=None):
        text = outputs.get(int(task_display_id))
        if text is None:
            return []
        import base64
        return [{"response_text": base64.b64encode(text.encode()).decode()}]

    monkeypatch.setattr(package_mythic_tools.mythic, "get_all_task_output_by_id", task_output)
    mt._fetch_command_schema = lambda command, callback_display_id, **kw: asyncio.sleep(0, result=[])
    mt._validate_command_parameters = lambda command, parameters, callback_display_id: asyncio.sleep(0, result=None)
    mt._authenticate_live_command = lambda command, callback: asyncio.sleep(
        0, result={"status": "available", "command": command})
    mt._resolve_payload_type = lambda callback_id: asyncio.sleep(0, result="apollo")
    mt._resolve_domain_controller_host = lambda domain: asyncio.sleep(0, result="dc01.lab.local")
    mt._resolve_domain_sid = lambda domain: asyncio.sleep(0, result="S-1-5-21-1-2-3")
    mt._recon_snapshot_cache = {("list_callbacks", "all"): [{"id": 13, "display_id": 13, "host": "DC01"}]}
    mt._supervised_task_state = lambda *args, **kwargs: asyncio.sleep(
        0, result={"task_completed": True, "task_status": "completed"})
    with _split_issue(lambda: output, display_id=21):
        result = json.loads(asyncio.run(mt.execute_capability(
            {"capability": "dcsync-krbtgt", "domain": DOMAIN, "account": "krbtgt", "callback_id": "13"},
            {"timeout": 5})))
    mt._material_outputs = outputs
    return mt, _split_issue, result


def _state(mt) -> engagement_state.EngagementState:
    view = mt._ledger_view()
    return engagement_state.EngagementState(objective="", hops=list(view.hops), graph_facts=list(view.graph_facts),
                                            engagement_id=mt._eng_key(), runtime_scope=True)


def _forges(state) -> list:
    return [a for a in capabilities.actions_from_state(state) if a.name == "forge-golden-ticket"]


def test_a_material_only_dcsync_says_no_working_key_was_proved_and_names_the_recovery(
        monkeypatch, tmp_path, mythic_task_records):
    mt, _issue, result = _material_krbtgt_client(monkeypatch, tmp_path, mythic_task_records)

    assert f"krbtgt-hash:{DOMAIN}" not in result["recorded_effects"]
    assert MATERIAL in result["recorded_effects"], result["recorded_effects"]
    # Channel 4 reported `achieved` here, so the Supervisor planned on a key no capability could use.
    assert result["verdict"] == "partial"
    assert result["material_only"] is True and result["working_key_proved"] is False
    assert "forge-golden-ticket" in result["reason"] and "21" in result["reason"]
    assert KRBTGT_KEY not in json.dumps(result)


def test_valid_review_backed_krbtgt_material_offers_the_forge_with_its_material_precondition(
        monkeypatch, tmp_path, mythic_task_records):
    mt, _issue, _result = _material_krbtgt_client(monkeypatch, tmp_path, mythic_task_records)

    forges = _forges(_state(mt))
    assert forges, "valid task-backed krbtgt material must make the forge an offered action"
    assert {tuple(action.preconditions) for action in forges} == {(MATERIAL,)}
    assert all(f"krbtgt-hash:{DOMAIN}" not in action.effects for action in forges)


def test_valid_review_backed_krbtgt_material_binds_the_forge_key_from_its_proof_task_only(
        monkeypatch, tmp_path, mythic_task_records):
    mt, _issue, _result = _material_krbtgt_client(monkeypatch, tmp_path, mythic_task_records)

    bound = asyncio.run(mt._select_krbtgt_credential(DOMAIN))
    assert bound["credential"] == KRBTGT_KEY and bound["key_type"] == "aes256"
    assert str(bound["proof_task_id"]) == "21" and bound["source"] == "engagement_proof_task"


def test_the_supervised_forge_runs_once_from_the_material_rows_proof_task(monkeypatch, tmp_path, mythic_task_records):
    """Channel 4 request 4: every forge proposal was refused. Now the forge step gets one card and runs from task 21."""
    mt, split_issue, _result = _material_krbtgt_client(monkeypatch, tmp_path, mythic_task_records)
    forge_brief = {"handoff_title": "forge the golden ticket", "handoff_instruction": FORGE_PROMPT,
                   "allowed_capabilities": ["forge-golden-ticket"], "callback_display_id": 13,
                   "targets": [DOMAIN], "expected_result": "A verified golden ticket for lab.local."}
    forge = {"action": dict(FORGE_ACTION), "inputs": {"key": "f" * 64}}  # a model-carried key is dropped
    script = [_call("transfer_to_Mythic_Operator", forge_brief, 1), _call("execute_capability", forge, 2),
              AIMessage(content="The forge ran."), AIMessage(content="Final answer."),
              AIMessage(content="Final answer.")]

    run = asyncio.run(_rescope_run(mt, split_issue, script))

    proposed = [card for card in run.cards if card.get("status") not in {"accepted"}]
    [card] = proposed
    assert str(card["data"]["arguments"]["inputs"].get("credential_proof_task_id")) == "21"
    rubeus = [json.dumps(issue["parameters"]) for issue in run.issued if issue["command_name"] == "execute_assembly"]
    assert len(rubeus) == 1 and f"/aes256:{KRBTGT_KEY}" in rubeus[0]
    assert "f" * 64 not in json.dumps(run.issued)
    assert KRBTGT_KEY not in json.dumps(run.emissions, default=str)


@pytest.mark.parametrize("invalid", ["pending_review", "other_domain", "unparseable_output"])
def test_an_invalid_material_proof_neither_binds_nor_offers_the_forge(monkeypatch, tmp_path, mythic_task_records,
                                                                      invalid):
    from ai.langgraph import credential_review

    if invalid == "pending_review":
        monkeypatch.setattr(credential_review, "review_output",
                            lambda *args, **kwargs: credential_review.ReviewOutcome.not_applicable("disabled"))
    mt, _issue, _result = _material_krbtgt_client(monkeypatch, tmp_path, mythic_task_records)
    domain = DOMAIN
    if invalid == "other_domain":
        domain = "other.local"
    if invalid == "unparseable_output":
        mt._material_outputs[21] = "no credential material in this output"
        mt._task_output_cache = {}

    assert asyncio.run(mt._select_krbtgt_credential(domain)) == {}
    if invalid != "unparseable_output":  # an unparseable output is found only when the forge binds
        assert not [a for a in _forges(_state(mt)) if f"domain={domain}" in a.target]


def test_a_store_row_without_task_lineage_is_never_a_forge_key(monkeypatch, tmp_path, mythic_task_records):
    """The material row is the only proof: with it removed, the lineage-less store rows bind nothing."""
    mt, _issue, _result = _material_krbtgt_client(monkeypatch, tmp_path, mythic_task_records)
    assert asyncio.run(mt._fetch_credentials_cached("now")), "the importer wrote lineage-less store rows"
    mt._ledger_view = lambda: type("V", (), {"hops": [], "graph_facts": [], "corrected_hops": [], "snapshot": None})()
    assert asyncio.run(mt._select_krbtgt_credential(DOMAIN)) == {}


def test_an_approved_reference_to_another_proof_task_binds_no_key(monkeypatch, tmp_path, mythic_task_records):
    mt, _issue, _result = _material_krbtgt_client(monkeypatch, tmp_path, mythic_task_records)
    inputs = {"credential_proof_task_id": "99"}
    asyncio.run(mt._bind_proved_krbtgt_key(inputs, DOMAIN))
    assert "key" not in inputs and inputs["credential_proof_task_id"] == "99"


# --- a parent-domain DC is a target only once a read-only discovery names it -----------------------------------

from types import SimpleNamespace  # noqa: E402

from ai.langgraph import graph_reconciler, handoff_brief  # noqa: E402

PARENT = "sevenkingdoms.local"
CHILD = "north.sevenkingdoms.local"
PARENT_DC = f"kingslanding.{PARENT}"


def _fact(predicate: str):
    return SimpleNamespace(predicate=predicate)


def _request3_scope(*extra_facts: str) -> handoff_brief.KnownScope:
    """Channel 4 request 3's known scope before its first rejection: the north foothold and the ledger's graph facts
    (the parent domain only as trust-reachable; DC facts only for winterfell in north)."""
    facts = [_fact(f"trust-reachable:{CHILD}:{PARENT}"), _fact(f"trust-reachable:{PARENT}:{CHILD}"),
             _fact(f"gpo-affects-dc:starkwallpaper:winterfell:{CHILD}"), *(_fact(p) for p in extra_facts)]
    client = SimpleNamespace(
        _ledger_view=lambda: SimpleNamespace(hops=[], graph_facts=facts),
        _recon_snapshot_cache={("list_callbacks", "all"): [{"id": 2, "host": "CASTELBLACK",
                                                             "user": "samwell.tarly"}]},
        _engagement_footholds=[SimpleNamespace(callback_id="2", host="castelblack", forest=CHILD,
                                               identity="NORTH\\samwell.tarly")],
    )
    return handoff_brief.known_scope(client, operator_input=f"Obtain administrative control of {PARENT}",
                                     bound_callback="")


def _parent_brief(*targets: str) -> handoff_brief.HandoffBrief:
    return handoff_brief.HandoffBrief.from_args(
        targets=list(targets), callback_display_id=2, allowed_capabilities=["forge-golden-ticket"],
        expected_result="Administrative control of the parent domain.")


def _errors(scope, *targets):
    return handoff_brief.validate_brief(_parent_brief(*targets), worker="Mythic_Operator", require_scope=True,
                                        scope=scope, worker_tools=())


def test_request_3_rejected_the_parent_dc_seen_only_in_task_output():
    [error] = _errors(_request3_scope(), PARENT, PARENT_DC)
    assert PARENT_DC in error and PARENT not in error.replace(PARENT_DC, "")
    # The rejection says what grounds the host instead of inviting the same brief again.
    assert "read-only" in error and "domain-controller" in error


def test_a_graph_discovered_domain_controller_is_admitted_with_its_domain():
    scope = _request3_scope(f"domain-controller:kingslanding:{PARENT}")
    assert _errors(scope, PARENT, PARENT_DC) == ()
    assert _errors(scope, "kingslanding") == ()  # its one bare label


@pytest.mark.parametrize("target, facts", [
    (PARENT_DC, (f"domain-controller:kingslanding:essos.local",)),       # a DC of another domain
    (f"dc01.{PARENT}", (f"domain-controller:kingslanding:{PARENT}",)),     # a guessed host in the requested domain
    (f"meereen.{PARENT}", ()),                                              # nothing discovered at all
])
def test_no_domain_guess_or_widening_admits_a_host(target, facts):
    assert _errors(_request3_scope(*facts), target)


class _DCTool:
    def __init__(self, rows: list[str]):
        self.rows, self.calls = rows, []

    async def ainvoke(self, args):
        query = args.get("query", "")
        self.calls.append(query)
        literals = [{"key": "name", "value": row} for row in self.rows] if "-516" in query and "GPO" not in query \
            else []
        return json.dumps({"info_type": "run", "success": True, "data": {"nodes": {}, "edges": [],
                                                                          "literals": literals}})


class _MCP:
    def __init__(self, tool):
        self.tool = tool

    async def get_tool_by_name(self, name, server_name=None):
        return self.tool


def test_graph_refresh_records_each_collected_domain_controller_with_its_domain():
    tool = _DCTool([f"KINGSLANDING.{PARENT.upper()}|{PARENT.upper()}", f"WINTERFELL.{CHILD.upper()}|{CHILD.upper()}",
                    "NOT-A-HOST|", f"BRAAVOS.ESSOS.LOCAL|{PARENT.upper()}"])
    facts = asyncio.run(graph_reconciler.reconcile_graph_position(
        _MCP(tool), [f"samwell.tarly@{CHILD}"], "", "2026-11-09T22:00:00+00:00", 600))
    predicates = {fact.predicate for fact in facts if fact.predicate.startswith("domain-controller:")}
    assert predicates == {f"domain-controller:kingslanding:{PARENT}", f"domain-controller:winterfell:{CHILD}"}


# --- each supervised worker delegation starts with its own transcript ------------------------------------------

from langchain_core.messages import HumanMessage, SystemMessage  # noqa: E402

from ai.langgraph import step_bounds as sb  # noqa: E402
from tests.test_step_bounds import PROMPT, READ_BRIEF, _handoff, _probe_step, _run_probe, probe  # noqa: E402,F401

STEP_ONE_ANSWER = "Need BloodHound graph analysis before any GPO step (step one handback)."


def test_a_later_delegation_never_sees_an_earlier_worker_transcript(probe, monkeypatch):
    """Channel 4: the gpo step's worker call carried the earlier failed step and re-emitted its handback."""
    from tests import test_step_bounds
    from tests.test_hitl_steer_current_intent import _NativeScriptedModel

    # The scripted harness's model, but one whose script entries may read the real invocation input.
    monkeypatch.setattr(test_step_bounds._harness, "_ScriptedModel", _NativeScriptedModel)
    seen: list[list] = []

    def capture(reply: str):
        def worker(messages):
            seen.append(list(messages))
            return AIMessage(content=reply)
        return worker

    second_brief = {**READ_BRIEF, "handoff_title": "Second probe", "expected_result": "The second answer."}
    script = [_handoff(READ_BRIEF, "h1"), _probe_step(0), capture(STEP_ONE_ANSWER),
              _handoff(second_brief, "h2"), capture("Second step answer."), AIMessage(content="final")]
    result = _run_probe(script, probe, sb.StepBounds())

    assert probe["calls"] == [0]
    first, second = seen
    assert any("probe step 0 recorded" in str(m.content) for m in first)  # positive control
    text = "\n".join(str(m.content) for m in second)
    assert "probe step 0 recorded" not in text and STEP_ONE_ANSWER not in text
    # The fresh call keeps the worker's system prompt, Sage's scope for this step and the exact operator input.
    assert isinstance(second[0], SystemMessage)
    assert any(m.additional_kwargs.get("_step_scope_context") for m in second) and "Second probe" not in text
    assert [m.content for m in second if isinstance(m, HumanMessage)
            and m.additional_kwargs.get("_delegated_to")] == [PROMPT]
    # The earlier transcript stays in the worker channel for audit and in the Supervisor's evidence.
    channel = "\n".join(str(m.content) for m in result["mythic_operator_messages"])
    assert STEP_ONE_ANSWER in channel and "probe step 0 recorded" in channel


# --- guidance names Sage's own procedure, and identical reads stop -------------------------------------------------

from ai.langgraph import ttp_library  # noqa: E402


def _guidance(goal: str) -> dict:
    mt = MythicTools(agent_task_id="offline-guidance")
    mt._resolve_payload_type = lambda callback_id: asyncio.sleep(0, result="apollo")
    return json.loads(asyncio.run(mt.get_ttp_guidance(goal, 2)))


def test_guidance_for_a_catalog_technique_returns_the_capability_and_its_guarded_executor():
    """Channel 4: guidance for SharpHound collection sent the worker to enumerate Apollo's commands by hand."""
    result = _guidance("SharpHound collection")
    assert result["slug"] == "sharphound"
    assert result["sage_capability"] == {"name": "collect-graph", "executor": "execute_capability"}
    assert "get_all_commands_for_payloadtype" not in result["execution_on_agent"]
    assert "collect-graph" in result["execution_on_agent"] and "execute_capability" in result["execution_on_agent"]


def test_guidance_for_a_technique_without_a_catalog_procedure_keeps_the_agent_enumeration():
    # Named, because a generic "dump LSASS memory" ties three references and is now an ambiguous lookup.
    result = _guidance("dump LSASS memory with nanodump")
    assert result["slug"] == "nanodump"
    assert "sage_capability" not in result
    assert "get_all_commands_for_payloadtype" in result["execution_on_agent"]


def test_every_technique_mapped_to_a_capability_names_a_catalog_procedure():
    mapped = {}
    for path in ttp_library.TTP_DIR.glob("*.md"):
        frontmatter, _ = ttp_library.parse_markdown(path.read_text(encoding="utf-8"))
        if isinstance(frontmatter, dict) and frontmatter.get("sage_capability"):
            mapped[path.stem] = frontmatter["sage_capability"]
    assert mapped, "at least the collection technique is mapped"
    catalog = {doc.name for doc in capabilities.capability_docs()}
    assert set(mapped.values()) <= catalog, mapped


def test_identical_read_only_calls_stop_within_a_delegation_but_not_across_one(probe, monkeypatch):
    """Channel 4: six identical command-argument reads in one control-plane step. A changed argument and a new
    delegation stay usable; an identical read with an unchanged result is answered once more, then the step ends."""
    second = {**READ_BRIEF, "handoff_title": "Second probe", "expected_result": "The second answer."}
    script = [_handoff(READ_BRIEF, "h1"), _probe_step(0), _probe_step(1), _probe_step(0), _probe_step(0),
              _handoff(second, "h2"), _probe_step(0), AIMessage(content="second step done"),
              AIMessage(content="final")]
    result = _run_probe(script, probe, sb.StepBounds())

    assert probe["calls"] == [0, 1, 0, 0]  # the fourth read in step one never ran; step two's identical read did
    texts = [str(m.content) for m in result["mythic_operator_messages"] if getattr(m, "type", "") == "tool"]
    assert any("no-progress" in text for text in texts)
    assert any("identical-read-boundary" in text for text in texts)


# --- with no collected graph, collect-graph is the request's first delegated proposal ------------------------------

from types import SimpleNamespace as _NS  # noqa: E402
from typing import Any  # noqa: E402
from unittest.mock import MagicMock  # noqa: E402

from langchain_core.tools import tool as _tool  # noqa: E402
from langgraph.checkpoint.memory import InMemorySaver  # noqa: E402

from ai.langgraph import knowledge_sources, model as model_module  # noqa: E402
from ai.langgraph.model import _get_seq  # noqa: E402
from sage_chat.headless import build_chat_request  # noqa: E402
from sage_chat.service import bind_channel_thread_id  # noqa: E402
from sage_chat.session import drop_channel_session  # noqa: E402
from tests.test_hitl_steer_current_intent import (  # noqa: E402
    _NativeDriverChat, _NativeScriptedModel, _cards, _input_response,
)

OBJECTIVE = "Obtain Domain Admin of north.test from the observed foothold."
TTP_STEP = {"handoff_title": "Check TTP guidance for SharpHound collection", "allowed_capabilities": ["control_plane"],
            "expected_result": "How to run SharpHound on this agent."}


def _canned_consult(outcome):
    async def consult(prompt):
        return knowledge_sources.AttackGraphConsult(outcome, objective_domains=["north.test"],
                                                    collected_domains=["essos.test"] if outcome.value.startswith(
                                                        "consulted") else [],
                                                    missing_domains=["north.test"] if outcome.value ==
                                                    "consulted_partial" else [])
    return consult


INGESTED = '{"ok": true, "status": "ingested"}'


async def _graph_session(script: list, turns: list[tuple[str, Any]], *, mode: str = "supervised",
                         consult=None, outcome=knowledge_sources.ConsultOutcome.ABSENT,
                         autonomous_solve: bool = False, executor_result: str = INGESTED,
                         callbacks: list[dict] | None = None, executor=None, runner: bool = True) -> list:
    """Native supervised requests in one channel and one Model; each turn is (operator input, card decision).

    A decision is "accept", "reject", "" (leave the card pending) or ("respond", text) for an operator steer.
    `callbacks` are the observed callback rows (id, host and, when known, agent); `executor(model, action)` replaces
    the canned executor result with a real runner. Returns one record per turn: what its worker steps saw and
    proposed, the executor runs and model calls at the moment its first card surfaced, and the consult Sage recorded
    for it."""
    ran: list[dict] = []

    @_tool
    async def execute_capability(action: dict | str, inputs: dict | str | None = None) -> str:
        """Run one catalog capability."""
        ran.append({"action": action, "inputs": inputs})
        if executor is not None:
            return await executor(model, action)
        return executor_result

    model = model_module.Model(
        provider="openai", model="gpt-4o-mini", system_prompt="offline closure",
        config={"configurable": {"api_key": "offline-only", "API_ENDPOINT": "http://127.0.0.1:9/v1"}},
        task_id=0, agent_task_id="closure", mode=mode, autonomous_solve=autonomous_solve, operation_id=1,
        channel_id=1, apitoken_id=0,
    )
    sink = MagicMock()
    sink.get_tools.side_effect = lambda names: [execute_capability] if "execute_capability" in names else []
    if callbacks is None:
        sink._recon_snapshot_cache = {("list_callbacks", "all"): [{"id": 5, "host": "WORKSTATION-5"}]}
        sink._engagement_footholds = [_NS(callback_id="5", host="WORKSTATION-5", forest="north.test",
                                          identity="NORTH\\operator")]
    else:
        sink._recon_snapshot_cache = {("list_callbacks", "all"): [dict(row) for row in callbacks]}
        sink._engagement_footholds = [
            _NS(callback_id=str(row["id"]), host=row["host"], forest="north.test", identity="NORTH\\operator",
                agent=row.get("agent", ""), alive=True) for row in callbacks]
    if not runner:
        sink._collection_runner = None
    sink._engagement_hops = []
    sink._ledger_view = lambda: _NS(hops=[], graph_facts=[], corrected_hops=[], snapshot=None)
    sink._consult_attack_graph = consult or _canned_consult(outcome)
    model.mythic_client = sink
    model._payload_names = []
    model.memory = InMemorySaver()
    fake = _NativeScriptedModel(messages=iter(script))
    model._get_base_chat_model = lambda *a, **k: fake
    model._native_chat_explicit_hitl = True
    model.command_name = "chat"
    model._rebuild_graph()
    chat = _NativeDriverChat(model)
    records: list = []
    seen: set = set()
    request_id = 0
    # Sage's own proposals live in the worker's step, not the parent channels: record each one the real step-bound
    # middleware makes (observed, not replaced).
    proposed: list[str] = []
    propose = model_module._SupervisedStepBoundMiddleware._bound_procedure_message

    def observed_proposal(self, request):
        message = propose(self, request)
        if message is not None:
            proposed.append(message.tool_calls[0]["id"])
        return message

    from unittest.mock import patch
    spy = patch.object(model_module._SupervisedStepBoundMiddleware, "_bound_procedure_message", observed_proposal)
    spy.start()
    try:
        for index, (objective, decision) in enumerate(turns):
            request_id += 1
            request = build_chat_request(objective, channel_id=1, operation_id=1, request_id=request_id,
                                         config={"mode": mode})
            if index == 0:
                bind_channel_thread_id(request, model)
            cards_before, ran_before, calls_before = len(_cards(chat)), len(ran), len(fake.inputs)
            proposed_before = len(proposed)
            await chat.chat(request)
            new_cards = _cards(chat)[cards_before:]
            at_card = _NS(ran=len(ran) - ran_before, model_calls=len(fake.inputs) - calls_before) \
                if new_cards else None
            if decision and new_cards:
                request_id += 1
                resume = build_chat_request("", channel_id=1, operation_id=1, request_id=request_id,
                                            config={"mode": mode})
                action, response = decision if isinstance(decision, tuple) else (decision, "")
                resume.InputResponse = _input_response(action, new_cards[0], response=response)
                await chat.chat(resume)
            snapshot = await model.graph.aget_state(model._graph_run_config(model._session_thread_id()))
            values = snapshot.values
            fresh = lambda channel: [m for m in values.get(channel, [])  # noqa: E731
                                     if (channel, _get_seq(m), type(m).__name__) not in seen]
            operator = fresh("mythic_operator_messages")
            supervisor = fresh("supervisor_messages")
            others = {channel: fresh(channel) for channel in ("generalist_messages", "bloodhound_messages")}
            # Delegation acknowledgements and brief rejections are the Supervisor's tool results in `messages`.
            shared = fresh("messages")
            for channel, messages in (("mythic_operator_messages", operator), ("supervisor_messages", supervisor),
                                      ("messages", shared), *others.items()):
                seen.update((channel, _get_seq(m), type(m).__name__) for m in messages)
            kwargs = [getattr(m, "additional_kwargs", {}) for m in operator]
            ledger = getattr(model, "_request_event_ledger", None)
            brief_events = [(event.phase, event.content) for event in ledger.actual_events(kind="brief")] \
                if ledger is not None else []
            records.append(_NS(
                objective=objective,
                briefs=[k["_handoff_brief"] for k in kwargs if k.get("_handoff_brief") is not None],
                titles=[str(k.get("_handoff_title") or "") for k in kwargs if k.get("_handoff_brief") is not None],
                bound_ids=list(proposed[proposed_before:]),
                other_delegations=[m for messages in others.values() for m in messages
                                   if getattr(m, "additional_kwargs", {}).get("_delegated_to")],
                cards=new_cards, at_card=at_card, ran=list(ran[ran_before:]),
                consult="\n".join(str(m.content) for m in supervisor
                                  if m.additional_kwargs.get("_synthetic_nudge") == "knowledge_source_consult"),
                consult_metadata=[m.additional_kwargs["_knowledge_source_consult"] for m in supervisor
                                  if m.additional_kwargs.get("_knowledge_source_consult")],
                supervisor_tools=[str(m.content) for m in [*supervisor, *shared] if getattr(m, "type", "") == "tool"],
                worker_tools=[str(m.content) for m in operator if getattr(m, "type", "") == "tool"],
                brief_events=brief_events,
            ))
        return records
    finally:
        spy.stop()
        await drop_channel_session(build_chat_request("", channel_id=1, operation_id=1))


async def _graph_request(script: list, decision: Any = "", *, mode: str = "supervised",
                         outcome=knowledge_sources.ConsultOutcome.ABSENT, objective: str = OBJECTIVE, consult=None,
                         autonomous_solve: bool = False, executor_result: str = INGESTED, **session):
    """One native supervised request with an attack-graph consult of `outcome`; returns what each worker step saw."""
    (record,) = await _graph_session(script, [(objective, decision)], mode=mode, consult=consult, outcome=outcome,
                                     autonomous_solve=autonomous_solve, executor_result=executor_result, **session)
    return record


def _supervisor_ttp_lookup_first() -> list:
    return [_call("transfer_to_Mythic_Operator", TTP_STEP, 1),
            AIMessage(content="Collection result read."), AIMessage(content="Final answer."),
            AIMessage(content="Final answer."), AIMessage(content="Final answer.")]


def test_an_absent_graph_makes_collect_graph_the_first_delegated_proposal():
    """Channel 4 request 2: byte-identical Supervisor input once delegated a TTP lookup first. Sage now presents the
    bound collect-graph step as that first delegation, for the operator to approve."""
    run = asyncio.run(_graph_request(_supervisor_ttp_lookup_first(), "accept"))

    assert run.briefs[0]["allowed_capabilities"] == ["collect-graph"], run.briefs
    assert run.briefs[0]["callback_display_id"] == "5" and run.briefs[0]["targets"] == ["north.test"]
    assert run.ran == [{"action": {"capability": "collect-graph", "callback_id": "5", "domain": "north.test"},
                        "inputs": None}]
    assert "collect-graph" in run.consult


def test_an_operator_rejected_collection_runs_nothing_and_leaves_manual_enumeration():
    script = [_call("transfer_to_Mythic_Operator", TTP_STEP, 1), AIMessage(content="Denied."),
              _call("transfer_to_Mythic_Operator", TTP_STEP, 2), AIMessage(content="Enumerated by hand."),
              AIMessage(content="Final answer."), AIMessage(content="Final answer.")]
    run = asyncio.run(_graph_request(script, "reject"))

    assert run.ran == []
    assert [b["allowed_capabilities"] for b in run.briefs][:2] == [["collect-graph"], ["control_plane"]], run.briefs


@pytest.mark.parametrize("outcome", [knowledge_sources.ConsultOutcome.UNAVAILABLE,
                                     knowledge_sources.ConsultOutcome.CONSULTED_OK])
def test_unavailable_and_covering_graphs_keep_the_supervisors_own_first_step(outcome):
    run = asyncio.run(_graph_request(_supervisor_ttp_lookup_first(), outcome=outcome))
    assert run.briefs[0]["allowed_capabilities"] == ["control_plane"]
    assert run.ran == [] and run.cards == []


# --- a partial graph that misses the objective's domain proposes collecting that domain first --------------------

PARTIAL_OBJECTIVE = "Obtain Domain Admin of south.test from the observed foothold."


def _real_consult(domains: dict[str, bool]):
    """Sage's own consult producer (`MythicTools._consult_attack_graph`) over a stubbed BloodHound whose
    `domain_info` lists `domains` with their `collected` flag; MCP is stubbed only while the consult runs."""
    from unittest.mock import patch

    from ai import mcp as mcp_module
    from tests.test_knowledge_source_consult import _Tool, _mythic_tools

    rows = [{"name": name.upper(), "collected": collected} for name, collected in domains.items()]
    tools = [_Tool("domain_info", lambda _args: json.dumps({"data": rows})),
             _Tool("cypher_query", lambda _args: json.dumps({"data": {"literals": []}}))]
    mt = _mythic_tools()
    mt._engagement_footholds = [_NS(callback_id="5", host="WORKSTATION-5", forest="north.test",
                                    identity="NORTH\\operator")]

    async def consult(prompt):
        manager = mcp_module.MCPManager
        with patch.object(manager, "get_connected_servers", staticmethod(lambda: ["bloodhound"])), \
                patch.object(manager, "is_bloodhound_server", staticmethod(lambda server: server == "bloodhound")), \
                patch.object(manager, "get_tools_by_server", staticmethod(lambda server: tools)):
            return await mt._consult_attack_graph(prompt)

    consult.tools = tools
    return consult


NORTH_ONLY = {"north.test": True, "south.test": False}
SOUTH_COLLECT = {"capability": "collect-graph", "callback_id": "5", "domain": "south.test"}


def test_a_partial_graph_missing_the_objective_domain_proposes_collecting_it_first():
    """With north collected and the requested domain only a trust stub, the Supervisor delegated other work and
    never proposed collecting the requested domain. Sage now makes that collection the first delegated proposal."""
    consult = _real_consult(NORTH_ONLY)
    run = asyncio.run(_graph_request(_supervisor_ttp_lookup_first(), objective=PARTIAL_OBJECTIVE, consult=consult))

    (metadata,) = run.consult_metadata
    assert metadata["outcome"] == "consulted_partial"
    assert metadata["collected_domains"] == ["north.test"] and metadata["missing_domains"] == ["south.test"]
    assert not any("shortestPath" in str(call) for call in consult.tools[1].calls)  # nothing covered to probe
    # The first accepted brief and its native card are the bound collection of the missing domain.
    assert run.briefs[0] == {"targets": ["south.test"], "callback_display_id": "5",
                             "allowed_capabilities": ["collect-graph"], "allowed_tools": [],
                             "inputs_by_reference": [], "expected_result": run.briefs[0]["expected_result"]}
    (card,) = run.cards
    assert card["data"]["actions"] == [{"tool_name": "execute_capability", "display_name": "collect-graph",
                                        "arguments": {"action": SOUTH_COLLECT}}]
    assert card["data"]["guarded_action_count"] == 1
    assert len(run.bound_ids) == 1 and run.bound_ids[0].startswith("sage_bound_")
    # Pending: nothing ran, and only the Supervisor's one routing call preceded the card (no worker model call).
    assert run.at_card.ran == 0 and run.at_card.model_calls == 1
    assert run.ran == []
    assert "collect-graph" in run.consult and "south.test" in run.consult


def test_an_accepted_missing_domain_collection_runs_once_with_the_carded_scope():
    run = asyncio.run(_graph_request(_supervisor_ttp_lookup_first(), "accept", objective=PARTIAL_OBJECTIVE,
                                     consult=_real_consult(NORTH_ONLY)))
    assert run.cards[0]["data"]["arguments"] == {"action": SOUTH_COLLECT}
    assert run.ran == [{"action": SOUTH_COLLECT, "inputs": None}]
    # The native approval resume neither re-proposes nor re-cards the collection.
    assert len(run.cards) == 1 and len(run.bound_ids) == 1
    assert [b["allowed_capabilities"] for b in run.briefs].count(["collect-graph"]) == 1


def test_with_mixed_coverage_only_the_first_missing_objective_domain_is_proposed():
    objective = ("Obtain Domain Admin of west.test and domain admin of south.test and domain admin of north.test "
                 "from the observed foothold.")
    run = asyncio.run(_graph_request(_supervisor_ttp_lookup_first(), objective=objective,
                                     consult=_real_consult({**NORTH_ONLY, "west.test": False})))

    (metadata,) = run.consult_metadata
    assert metadata["outcome"] == "consulted_partial"
    assert metadata["objective_domains"] == ["north.test", "south.test", "west.test"]
    assert metadata["collected_domains"] == ["north.test"]
    assert metadata["missing_domains"] == ["south.test", "west.test"]  # the remaining scope stays for the Supervisor
    assert run.briefs[0]["targets"] == ["south.test"] and run.briefs[0]["allowed_capabilities"] == ["collect-graph"]
    (card,) = run.cards
    assert card["data"]["arguments"] == {"action": SOUTH_COLLECT}  # one domain, never several or a default
    assert run.ran == []


def _collect(targets: list[str] | None = None, *, title: str = "collect graph", worker: str = "Mythic_Operator",
             index: int = 1) -> AIMessage:
    args = {"handoff_title": title, "allowed_capabilities": ["collect-graph"], "callback_display_id": 5,
            "expected_result": "A collected graph."}
    if targets is not None:
        args["targets"] = targets
    return _call(f"transfer_to_{worker}", args, index)


@pytest.mark.parametrize("first", [
    pytest.param(_collect(["north.test"]), id="already-collected-callback-domain"),
    pytest.param(_collect(None), id="omitted-target"),
    pytest.param(_collect(["workstation-5"]), id="callback-host-default"),
    pytest.param(_collect(["south.test", "west.test"]), id="several-targets"),
    pytest.param(_collect(["south.test"], worker="Mythic_Payload"), id="other-worker"),
])
def test_a_first_collection_of_the_wrong_scope_is_replaced_by_the_missing_domain_proposal(first):
    script = [first, AIMessage(content="Collection result read."), AIMessage(content="Final answer."),
              AIMessage(content="Final answer.")]
    run = asyncio.run(_graph_request(script, objective=PARTIAL_OBJECTIVE, consult=_real_consult(NORTH_ONLY)))

    assert run.titles[0] == "Collect the attack graph", run.titles  # Sage's step, not the Supervisor's
    assert run.briefs[0]["targets"] == ["south.test"] and run.briefs[0]["callback_display_id"] == "5"
    assert run.other_delegations == []
    (card,) = run.cards
    assert card["data"]["arguments"] == {"action": SOUTH_COLLECT}
    assert run.at_card.ran == 0 and run.at_card.model_calls == 1


def test_a_first_collection_already_scoped_to_the_missing_domain_passes_once_unchanged():
    script = [_collect(["south.test"], title="collect south"), AIMessage(content="Collection result read."),
              AIMessage(content="Final answer."), AIMessage(content="Final answer.")]
    run = asyncio.run(_graph_request(script, "accept", objective=PARTIAL_OBJECTIVE, consult=_real_consult(NORTH_ONLY)))

    assert run.titles[0] == "collect south"
    assert [b["allowed_capabilities"] for b in run.briefs].count(["collect-graph"]) == 1
    assert run.ran == [{"action": SOUTH_COLLECT, "inputs": None}]


def test_a_rejected_missing_domain_collection_runs_nothing_and_later_steps_pass_unchanged():
    script = [_call("transfer_to_Mythic_Operator", TTP_STEP, 1), AIMessage(content="Denied."),
              _call("transfer_to_Mythic_Operator", TTP_STEP, 2), AIMessage(content="Enumerated by hand."),
              AIMessage(content="Final answer."), AIMessage(content="Final answer.")]
    run = asyncio.run(_graph_request(script, "reject", objective=PARTIAL_OBJECTIVE, consult=_real_consult(NORTH_ONLY)))

    assert run.ran == []
    assert [b["allowed_capabilities"] for b in run.briefs][:2] == [["collect-graph"], ["control_plane"]], run.briefs
    assert run.titles[1] == TTP_STEP["handoff_title"]


def test_a_failed_missing_domain_collection_does_not_force_a_second_proposal():
    script = [_call("transfer_to_Mythic_Operator", TTP_STEP, 1), AIMessage(content="The collection failed."),
              _call("transfer_to_Mythic_Operator", TTP_STEP, 2), AIMessage(content="Enumerated by hand."),
              AIMessage(content="Final answer."), AIMessage(content="Final answer.")]
    run = asyncio.run(_graph_request(script, "accept", objective=PARTIAL_OBJECTIVE, consult=_real_consult(NORTH_ONLY),
                                     executor_result='{"ok": false, "status": "failed", "error": "collector failed"}'))

    assert run.ran == [{"action": SOUTH_COLLECT, "inputs": None}]
    assert [b["allowed_capabilities"] for b in run.briefs][:2] == [["collect-graph"], ["control_plane"]], run.briefs
    assert len(run.cards) == 1


def test_supervised_with_autonomous_solve_on_the_supervisor_path_proposes_the_missing_domain_for_approval(
        monkeypatch):
    # With the controller rolled back, Autonomous Solve requests run the Supervisor graph in the supervised lane.
    monkeypatch.setenv("SAGE_CONTROLLER_HITL", "0")
    run = asyncio.run(_graph_request(_supervisor_ttp_lookup_first(), objective=PARTIAL_OBJECTIVE,
                                     consult=_real_consult(NORTH_ONLY), autonomous_solve=True))
    (card,) = run.cards
    assert card["data"]["arguments"] == {"action": SOUTH_COLLECT}
    assert run.ran == [] and run.at_card.ran == 0


def test_supervised_with_autonomous_solve_by_default_stays_controller_owned_and_executes_nothing_unapproved():
    """By default the controller owns a supervised Autonomous Solve objective; the Supervisor hook does not reach it."""
    run = asyncio.run(_graph_request(_supervisor_ttp_lookup_first(), objective=PARTIAL_OBJECTIVE,
                                     consult=_real_consult(NORTH_ONLY), autonomous_solve=True))
    assert run.briefs == [] and run.bound_ids == []
    assert run.ran == []


def test_conversation_mode_stays_observational_with_a_partial_graph():
    script = [_call("transfer_to_Mythic_Operator", {"handoff_title": "Look up guidance"}, 1),
              AIMessage(content="Here is what I found."), AIMessage(content="Final answer."),
              AIMessage(content="Final answer.")]
    run = asyncio.run(_graph_request(script, mode="conversation", objective=PARTIAL_OBJECTIVE,
                                     consult=_real_consult(NORTH_ONLY)))
    assert run.consult_metadata[0]["outcome"] == "consulted_partial"
    assert run.ran == [] and run.cards == [] and run.bound_ids == []
    assert all(b.get("allowed_capabilities") != ["collect-graph"] for b in run.briefs)


def _step(title: str, index: int) -> AIMessage:
    return _call("transfer_to_Mythic_Operator", {**TTP_STEP, "handoff_title": title}, index)


def test_each_objective_request_in_a_channel_gets_its_own_first_proposal_and_no_stale_one():
    """A later request in the same channel: one Model. Each new objective request whose domain is missing gets its own
    proposal; a covered or non-objective request afterwards inherits none."""
    domains = {**NORTH_ONLY, "west.test": False}
    script = [
        _step("First lookup", 1), AIMessage(content="Denied."), AIMessage(content="Final answer."),
        _step("Second lookup", 2), AIMessage(content="Denied."), AIMessage(content="Final answer."),
        _step("Covered lookup", 3), AIMessage(content="Read."), AIMessage(content="Final answer."),
        _step("Guidance lookup", 4), AIMessage(content="Read."), AIMessage(content="Final answer."),
        *[AIMessage(content="Final answer.")] * 4,
    ]
    turns = [(PARTIAL_OBJECTIVE, "reject"),
             ("Obtain Domain Admin of west.test from the observed foothold.", "reject"),
             ("Obtain Domain Admin of north.test from the observed foothold.", ""),
             ("Check the TTP guidance for SharpHound collection.", "")]
    first, second, covered, other = asyncio.run(_graph_session(script, turns, consult=_real_consult(domains)))

    assert [c["data"]["arguments"]["action"]["domain"] for c in first.cards + second.cards] == [
        "south.test", "west.test"]
    assert first.briefs[0]["targets"] == ["south.test"] and second.briefs[0]["targets"] == ["west.test"]
    assert first.ran == second.ran == []
    assert covered.consult_metadata[0]["outcome"] == "consulted_ok"
    assert other.consult_metadata == []
    for record, title in ((covered, "Covered lookup"), (other, "Guidance lookup")):
        assert record.titles[:1] == [title] and record.briefs[0]["allowed_capabilities"] == ["control_plane"]
        assert record.cards == [] and record.ran == [] and record.bound_ids == []


def test_conversation_mode_stays_observational_with_an_absent_graph():
    script = [_call("transfer_to_Mythic_Operator", {"handoff_title": "Look up guidance"}, 1),
              AIMessage(content="Here is what I found."), AIMessage(content="Final answer."),
              AIMessage(content="Final answer.")]
    run = asyncio.run(_graph_request(script, mode="conversation"))
    assert run.ran == [] and run.cards == []
    assert all(b.get("allowed_capabilities") != ["collect-graph"] for b in run.briefs)


def test_the_supervisors_own_collect_graph_first_step_passes_unchanged():
    own = {"handoff_title": "collect graph", "allowed_capabilities": ["collect-graph"], "callback_display_id": 5,
           "targets": ["north.test"], "expected_result": "A collected graph."}
    script = [_call("transfer_to_Mythic_Operator", own, 1), AIMessage(content="Collection result read."),
              AIMessage(content="Final answer."), AIMessage(content="Final answer.")]
    run = asyncio.run(_graph_request(script, "accept"))
    assert len([b for b in run.briefs if b["allowed_capabilities"] == ["collect-graph"]]) == 1
    assert len(run.ran) == 1


# --- option B: a fresh delegation still receives what earlier work produced for it, as Sage's data ---------------

from langchain_core.messages import ToolMessage  # noqa: E402

PAYLOAD_UUID = "5b2c7d0e-1111-4222-8333-944455556666"
FILE_UUID = "a0b1c2d3-7777-4888-9999-000011112222"
REQUEST = "chat:1:request:7"


def _payload_state(*, caller: str = "Mythic_Operator", request: str = REQUEST, result: str | None = None,
                   assignment: str = "Upload payload 00000000-dead-beef-0000-000000000000.") -> dict:
    """The Operator delegated a build to Mythic_Payload; its create_payload returned Mythic's record; the Supervisor
    then re-delegated to the Operator with an assignment that misquotes the payload id."""
    build = AIMessage(content="", name="Mythic_Payload", tool_calls=[{
        "name": "create_payload", "id": "build-1", "type": "tool_call",
        "args": {"payload_type_name": "apollo", "filename": "stage.exe", "operating_system": "windows",
                 "c2_profiles": [{"c2_profile": "http", "c2_profile_parameters": {"callback_host": "x"}}]}}],
        additional_kwargs={"_seq": 4})
    answer = result or json.dumps({"payload_uuid": PAYLOAD_UUID, "agent_file_id": FILE_UUID,
                                   "filename": "stage.exe", "build_phase": "success"})
    return {
        "_request_id": REQUEST,
        "mythic_payload_messages": [
            HumanMessage(content="Build a payload.", additional_kwargs={
                "_delegated_to": "Mythic_Payload", "_handoff_caller": caller, "_handoff_request_id": request,
                "_handoff_title": "Build stage payload", "_seq": 3}),
            build,
            ToolMessage(content=answer, name="create_payload", tool_call_id="build-1", additional_kwargs={"_seq": 4}),
            AIMessage(content="Payload built; file id is something.", name="Mythic_Payload",
                      additional_kwargs={"_seq": 4}),
        ],
        "mythic_operator_messages": [HumanMessage(content="Deploy it.", additional_kwargs={
            "_delegated_to": "Mythic_Operator", "_assisted_assignment": assignment, "_seq": 9})],
        "supervisor_messages": [],
    }


def test_a_handed_build_reaches_the_operator_with_mythics_ids_whatever_the_assignment_says():
    block = model_module._handed_results_context(_payload_state(), "Mythic_Operator", REQUEST)
    assert block is not None and block.additional_kwargs["_carried_context"] == "results_handed_to_you"
    [record] = json.loads(block.content.splitlines()[-2])
    assert record["producer"] == "Mythic_Payload" and record["tool"] == "create_payload"
    assert record["result"]["payload_uuid"] == PAYLOAD_UUID and record["result"]["agent_file_id"] == FILE_UUID
    assert record["status"] == "completed" and record["step"] == "Build stage payload"
    # Only named, non-secret arguments; the worker's prose is not the record.
    assert record["arguments"] == {"payload_type_name": "apollo", "filename": "stage.exe",
                                   "operating_system": "windows"}
    assert "callback_host" not in block.content and "something" not in block.content


@pytest.mark.parametrize("caller, request_id", [("Supervisor", REQUEST), ("Mythic_Operator", "chat:1:request:6")])
def test_builds_the_operator_did_not_delegate_in_this_request_are_not_handed_to_it(caller, request_id):
    assert model_module._handed_results_context(_payload_state(caller=caller, request=request_id),
                                                "Mythic_Operator", REQUEST) is None


def test_a_denied_build_is_handed_back_as_denied_and_a_queued_proposal_not_at_all():
    denied = model_module._handed_results_context(
        _payload_state(result="[DENIED by operator] create_payload was not executed."), "Mythic_Operator", REQUEST)
    assert '"status": "denied_by_operator"' in denied.content
    assert model_module._handed_results_context(
        _payload_state(result="[Queued] awaiting approval"), "Mythic_Operator", REQUEST) is None


def _schema(command: str, groups: str = "Default", **extra) -> ToolMessage:
    body = {"command": command, "payload_type": "apollo", "parameter_groups": {groups: []}, **extra}
    return ToolMessage(content=json.dumps(body, sort_keys=True), name="get_all_command_args_for_payloadtype",
                       tool_call_id=f"schema-{command}-{groups}")


def test_command_schemas_become_reference_data_until_mythic_contradicts_them():
    reference: dict = {}
    client = _NS(_command_reference_invalidations=set())
    model_module._record_command_references(
        reference, [_schema("execute_assembly"), _schema("ls"),
                    _schema("execute_pe", executable_contract={"status": "unsupported"})], client)
    assert set(reference) == {("apollo", "execute_assembly"), ("apollo", "ls")}  # callback-specific not kept
    block = model_module._command_reference_context(reference, client, REQUEST)
    assert '"command": "execute_assembly"' in block.content and '"command": "ls"' in block.content

    client._command_reference_invalidations.add(("apollo", "ls"))  # Mythic rejected the cached ls schema
    block = model_module._command_reference_context(reference, client, REQUEST)
    assert '"command": "ls"' not in block.content and '"command": "execute_assembly"' in block.content
    model_module._record_command_references(reference, [_schema("ls", groups="Path")], client)  # re-read
    block = model_module._command_reference_context(reference, client, REQUEST)
    assert '"Path"' in block.content and ("apollo", "ls") not in client._command_reference_invalidations
    assert model_module._command_reference_context({}, client, REQUEST) is None  # a miss: the worker reads


def test_earlier_steps_are_one_line_facts_for_this_request_only():
    outcome = {"worker": "Mythic_Operator", "status": "precondition_unsatisfiable", "detail": "no krbtgt key",
               "tasks_issued": [], "guarded_executed": 0, "guarded_rejected": 0}
    messages = [AIMessage(content="x", additional_kwargs={"_step_outcome": outcome, "_request_id": REQUEST}),
                AIMessage(content="y", additional_kwargs={"_step_outcome": {**outcome, "detail": "other request"},
                                                          "_request_id": "chat:1:request:6"})]
    block = model_module._earlier_steps_context(messages, REQUEST)
    assert "precondition_unsatisfiable (no krbtgt key)" in block.content and "other request" not in block.content
    assert model_module._earlier_steps_context([], REQUEST) is None


def _hook_model(callbacks: list[int], *, bound: str = ""):
    from ai.langgraph.request_contract import build_request_contract

    model = object.__new__(model_module.Model)
    model._graph_first_step = {"request_id": "chat:1:request:1", "partial": True, "domains": ["south.test"],
                               "done": False}
    model._request_contract = build_request_contract(request_id="chat:1:request:1", channel_id="1", operation_id="1",
                                                     mode="supervised", autonomous_solve=False)
    model.mythic_client = _NS(_recon_snapshot_cache={("list_callbacks", "all"): [
        {"id": callback, "host": f"HOST-{callback}"} for callback in callbacks]})
    model._turn_authority = _NS(bounded_callback_id=bound)
    return model


def test_without_one_unambiguous_callback_no_collection_is_invented():
    from ai.langgraph import handoff_brief

    lookup = handoff_brief.HandoffBrief.from_args(allowed_capabilities=["control_plane"], expected_result="Read.")
    model = _hook_model([5, 7])
    assert model._graph_collection_first_step("Mythic_Operator", lookup, "", "lookup") is None
    assert model._graph_first_step["done"] is False  # still owed to the first delegation that names a callback
    named = handoff_brief.HandoffBrief.from_args(callback_display_id=7, allowed_capabilities=["control_plane"],
                                                 expected_result="Read.")
    worker, brief, *_ = model._graph_collection_first_step("Mythic_Operator", named, "", "lookup")
    assert (worker, brief.callback_display_id, brief.targets) == ("Mythic_Operator", "7", ("south.test",))
    assert _hook_model([5, 7], bound="5")._graph_collection_first_step(
        "Mythic_Operator", lookup, "", "lookup")[1].callback_display_id == "5"
    assert _hook_model([])._graph_collection_first_step("Mythic_Operator", lookup, "", "lookup") is None


# --- an explicit supported collection request runs Sage's collection procedure, chosen before dispatch -------------
#
# Cairn #180 (2026-11-10, channel 3 request 2): the operator asked for a BloodHound collection; the Supervisor sent
# the worker a raw staging recipe (issue/download/ingest tools), the worker followed it for five model calls and its
# raw card was rejected. With the same input, channel 4 bound `collect-graph` and the existing procedure collected and
# verified the domain. Identities here are synthetic; the brief's shape is the recorded one.

COLLECT_PROMPT = "Collect BloodHound data for north.test through the current foothold; collection only."
APOLLO_5 = [{"id": 5, "host": "WORKSTATION-5", "agent": "apollo", "user": "operator"}]
TWO_APOLLO = [*APOLLO_5, {"id": 7, "host": "WORKSTATION-7", "agent": "apollo", "user": "operator"}]
NORTH_COLLECT = {"capability": "collect-graph", "callback_id": "5", "domain": "north.test"}
EXACT_SCOPE = {"targets": ["north.test"], "callback_display_id": "5", "allowed_capabilities": ["collect-graph"],
               "allowed_tools": [], "inputs_by_reference": []}
RAW_RECIPE = {
    "handoff_title": "Collect BloodHound data - north.test",
    "handoff_instruction": ("Collect BloodHound/SharpHound data for the domain north.test using callback 5. Download "
                            "SharpHound if not already staged, run it against north.test, retrieve the ZIP and "
                            "ingest it."),
    "allowed_tools": ["issue_task_and_waitfor_task_output", "download_tool", "ingest_collection"],
    "callback_display_id": 5, "targets": ["north.test", "WORKSTATION-5"],
    "expected_result": "SharpHound ZIP collected from north.test and ingested into BloodHound.",
}
FINALS = [AIMessage(content="Final answer.")] * 4


def _explicit(script: list, decision: Any = "", *, prompt: str = COLLECT_PROMPT, callbacks=APOLLO_5, **session):
    return asyncio.run(_graph_request(script, decision, objective=prompt, callbacks=callbacks, **session))


def _scope(brief: dict) -> dict:
    return {key: value for key, value in brief.items() if key != "expected_result"}


def _assert_exact_proposal(run, *, callback: str = "5", model_calls: int = 1) -> None:
    """The first admitted brief and its one card are the bound procedure; nothing ran and no worker model call
    preceded the card (only the Supervisor's routing calls)."""
    action = {**NORTH_COLLECT, "callback_id": callback}
    assert _scope(run.briefs[0]) == {**EXACT_SCOPE, "callback_display_id": callback}, run.briefs
    assert run.briefs[0]["expected_result"]
    (card,) = run.cards
    assert card["data"]["actions"] == [{"tool_name": "execute_capability", "display_name": "collect-graph",
                                        "arguments": {"action": action}}]
    assert card["data"]["guarded_action_count"] == 1
    assert len(run.bound_ids) == 1 and run.bound_ids[0].startswith("sage_bound_")
    assert run.at_card.ran == 0 and run.at_card.model_calls == model_calls


def test_the_recorded_raw_collection_brief_becomes_the_bound_collection_procedure_before_dispatch():
    script = [_call("transfer_to_Mythic_Operator", RAW_RECIPE, 1),
              AIMessage(content="A worker reading the raw recipe would stage SharpHound here."), *FINALS]
    run = _explicit(script)

    assert run.consult_metadata == []  # a collection request is not objective-shaped
    _assert_exact_proposal(run)
    assert run.titles[0] != RAW_RECIPE["handoff_title"]
    assert run.ran == [] and run.other_delegations == []
    # The Supervisor is told what Sage did, without claiming a missing graph or advising a raw bypass.
    note = next(text for text in run.supervisor_tools if "collect-graph" in text)
    assert "requested" in note and "no collected graph" not in note and "by hand" not in note


def test_the_supervisors_exact_collection_procedure_passes_once_unchanged():
    own = {"handoff_title": "collect north", "allowed_capabilities": ["collect-graph"], "callback_display_id": 5,
           "targets": ["north.test"], "expected_result": "A collected graph of north.test."}
    script = [_call("transfer_to_Mythic_Operator", own, 1), AIMessage(content="Collected."), *FINALS]
    run = _explicit(script, "accept")

    assert run.titles[0] == "collect north"
    _assert_exact_proposal(run)
    assert run.ran == [{"action": NORTH_COLLECT, "inputs": None}]
    assert [b["allowed_capabilities"] for b in run.briefs].count(["collect-graph"]) == 1


@pytest.mark.parametrize("first", [
    pytest.param(_call("transfer_to_Mythic_Operator", {**RAW_RECIPE, "allowed_tools": [
        "issue_task_and_waitfor_task_output"]}, 1), id="raw-issue-only"),
    pytest.param(_call("transfer_to_Mythic_Operator", TTP_STEP, 1), id="control-plane"),
    pytest.param(_call("transfer_to_BloodHound", {"handoff_title": "Check the graph",
                                                  "allowed_capabilities": ["control_plane"],
                                                  "expected_result": "Graph coverage."}, 1), id="other-worker-read"),
    pytest.param(_collect(["north.test"], worker="Mythic_Payload"), id="other-worker-collection"),
    pytest.param(_collect(["south.test"]), id="wrong-domain"),
    pytest.param(_collect(None), id="omitted-domain"),
    pytest.param(_collect(["workstation-5"]), id="callback-host-default"),
    pytest.param(_collect(["north.test", "south.test"]), id="several-domains"),
    pytest.param(_collect(["north.test", "workstation-5"]), id="extra-host-target"),
])
def test_a_replaceable_first_delegation_cannot_bypass_the_requested_procedure(first):
    run = _explicit([first, AIMessage(content="Collection result read."), *FINALS])

    assert run.titles[0] == "Collect BloodHound data", run.titles  # Sage's step, not the Supervisor's
    assert run.other_delegations == []
    _assert_exact_proposal(run)


def test_an_operator_named_callback_is_bound_and_a_conflicting_supervisor_callback_is_a_recoverable_error():
    prompt = "Collect BloodHound data for north.test through callback 7; collection only."
    script = [_collect(["north.test"]),  # names callback 5, not the operator's 7
              _call("transfer_to_Mythic_Operator", TTP_STEP, 2), AIMessage(content="Collected."), *FINALS]
    run = _explicit(script, prompt=prompt, callbacks=TWO_APOLLO)

    (rejected, accepted) = run.brief_events
    assert rejected[0] == "rejected" and "callback_display_id: 5 is not callback 7" in rejected[1], run.brief_events
    assert accepted == ("accepted", "Mythic_Operator")
    _assert_exact_proposal(run, callback="7", model_calls=2)


def test_with_several_callbacks_the_current_foothold_is_not_guessed():
    script = [_collect(["north.test"]), AIMessage(content="Collected."), *FINALS]
    run = _explicit(script, callbacks=TWO_APOLLO)

    # No forced proposal: the Supervisor's own step (its choice, not an operator selection) runs as written.
    assert run.titles[0] == "collect graph" and run.supervisor_tools
    assert not any("Sage replaced" in text for text in run.supervisor_tools)


@pytest.mark.parametrize("callbacks, runner", [
    pytest.param([{"id": 5, "host": "WORKSTATION-5", "agent": "nebula"}], True, id="unknown-payload"),
    pytest.param([{"id": 5, "host": "WORKSTATION-5"}], True, id="payload-type-not-observed"),
    pytest.param(APOLLO_5, False, id="no-collection-runner"),
])
def test_without_adapter_support_or_a_runner_no_collection_is_forced(callbacks, runner):
    script = [_call("transfer_to_Mythic_Operator", TTP_STEP, 1), AIMessage(content="Read."), *FINALS]
    run = _explicit(script, callbacks=callbacks, runner=runner)

    assert run.titles[0] == TTP_STEP["handoff_title"] and run.briefs[0]["allowed_capabilities"] == ["control_plane"]
    assert run.cards == [] and run.bound_ids == [] and run.ran == []


def test_a_merlin_callback_gets_the_same_procedure_level_proposal():
    script = [_call("transfer_to_Mythic_Operator", RAW_RECIPE, 1), AIMessage(content="Collected."), *FINALS]
    run = _explicit(script, callbacks=[{"id": 5, "host": "WORKSTATION-5", "agent": "merlin"}])
    _assert_exact_proposal(run)


def test_a_rejected_explicit_collection_runs_nothing_and_is_not_proposed_again():
    script = [_call("transfer_to_Mythic_Operator", RAW_RECIPE, 1), AIMessage(content="Denied."),
              _call("transfer_to_Mythic_Operator", TTP_STEP, 2), AIMessage(content="Read."), *FINALS]
    run = _explicit(script, "reject")

    assert run.ran == [] and len(run.cards) == 1 and len(run.bound_ids) == 1
    assert [b["allowed_capabilities"] for b in run.briefs][:2] == [["collect-graph"], ["control_plane"]]
    assert run.titles[1] == TTP_STEP["handoff_title"]


def test_a_failed_explicit_collection_is_not_retried_or_reported_complete():
    script = [_call("transfer_to_Mythic_Operator", RAW_RECIPE, 1), AIMessage(content="The collection failed."),
              _call("transfer_to_Mythic_Operator", TTP_STEP, 2), AIMessage(content="Read."), *FINALS]
    run = _explicit(script, "accept", executor_result='{"ok": false, "status": "failed", "error": "collector failed"}')

    assert run.ran == [{"action": NORTH_COLLECT, "inputs": None}]
    assert len(run.cards) == 1 and len(run.bound_ids) == 1
    assert [b["allowed_capabilities"] for b in run.briefs][:2] == [["collect-graph"], ["control_plane"]]
    assert not any('"ok": true' in text for text in run.worker_tools)


def test_an_operator_steer_at_the_card_never_executes_the_old_bound_action():
    script = [_call("transfer_to_Mythic_Operator", RAW_RECIPE, 1), AIMessage(content="Redirected."),
              _call("transfer_to_Mythic_Operator", TTP_STEP, 2), AIMessage(content="Read."), *FINALS]
    run = _explicit(script, ("respond", "Stop; just list the callbacks instead."))

    assert run.ran == [] and len(run.cards) == 1 and len(run.bound_ids) == 1
    # The redirected request's later step passes as the Supervisor wrote it (the steer re-delivers the first step's
    # brief to its worker, so that step's brief appears again before it).
    assert run.titles[-1] == TTP_STEP["handoff_title"] and run.briefs[-1]["allowed_capabilities"] == ["control_plane"]
    assert {b["allowed_capabilities"][0] for b in run.briefs} == {"collect-graph", "control_plane"}


def test_each_explicit_request_gets_its_own_proposal_and_an_unrelated_request_inherits_none():
    script = [
        _call("transfer_to_Mythic_Operator", RAW_RECIPE, 1), AIMessage(content="Denied."),
        AIMessage(content="Final answer."),
        _call("transfer_to_Mythic_Operator", RAW_RECIPE, 2), AIMessage(content="Denied."),
        AIMessage(content="Final answer."),
        _call("transfer_to_Mythic_Operator", TTP_STEP, 3), AIMessage(content="Read."),
        AIMessage(content="Final answer."), *FINALS,
    ]
    turns = [(COLLECT_PROMPT, "reject"), (COLLECT_PROMPT, "reject"), ("Yes, run it.", "")]
    first, second, unrelated = asyncio.run(_graph_session(script, turns, callbacks=APOLLO_5))

    for record in (first, second):
        assert _scope(record.briefs[0]) == EXACT_SCOPE and len(record.bound_ids) == 1 and record.ran == []
        assert record.cards[0]["data"]["arguments"] == {"action": NORTH_COLLECT}
    assert unrelated.titles[:1] == [TTP_STEP["handoff_title"]]
    assert unrelated.cards == [] and unrelated.bound_ids == [] and unrelated.ran == []


def test_supervisor_assignment_text_is_never_read_as_an_operator_collection_request():
    assigned = {**TTP_STEP, "handoff_instruction": "Collect BloodHound data for north.test through callback 5."}
    script = [_call("transfer_to_Mythic_Operator", assigned, 1), AIMessage(content="Read."), *FINALS]
    run = _explicit(script, prompt="Summarize what we know about north.test so far.")
    assert run.titles[0] == TTP_STEP["handoff_title"] and run.cards == [] and run.bound_ids == []


def test_an_objective_request_keeps_its_consult_proposal_and_no_direct_binding():
    script = [_call("transfer_to_Mythic_Operator", TTP_STEP, 1), AIMessage(content="Read."), *FINALS]
    run = _explicit(script, prompt=OBJECTIVE)
    assert run.consult_metadata and run.consult_metadata[0]["outcome"] == "absent"
    assert run.titles[0] == "Collect the attack graph"  # the objective plan's step, unchanged


def test_conversation_mode_binds_no_collection_and_executes_nothing():
    script = [_call("transfer_to_Mythic_Operator", {"handoff_title": "Look up guidance"}, 1),
              AIMessage(content="Here is what I found."), *FINALS]
    run = _explicit(script, mode="conversation")
    assert run.ran == [] and run.cards == [] and run.bound_ids == []
    assert all(b.get("allowed_capabilities") != ["collect-graph"] for b in run.briefs)


def test_supervised_autonomous_solve_on_the_supervisor_path_gets_the_same_bound_proposal(monkeypatch):
    monkeypatch.setenv("SAGE_CONTROLLER_HITL", "0")
    script = [_call("transfer_to_Mythic_Operator", RAW_RECIPE, 1), AIMessage(content="Collected."), *FINALS]
    run = _explicit(script, autonomous_solve=True)
    _assert_exact_proposal(run)


# --- the approved procedure drives the real collection runner through each backend's adapter --------------------

def _backend_executor(agent: str, calls: list):
    """Run the approved card's action through `Model._run_collection_capability` with the payload's real collection
    adapter; only the Mythic transport and ingest are the controller tests' inert fakes."""
    from tests.test_autonomous_controller_wiring import _CollectMythic, _MerlinCollectMythic, _foothold

    async def run(_model, action):
        fake_type = _MerlinCollectMythic if agent == "merlin" else _CollectMythic
        fake = fake_type({"status": "ingested", "graph_verified": True})
        owner = object.__new__(model_module.Model)
        owner.mythic_client = fake
        state = engagement_state.EngagementState(
            objective="", footholds=[_foothold(action["callback_id"], agent, forest="north.test")],
            hops=[], graph_facts=[])

        async def current_state():
            return state

        owner._build_current_engagement_state = current_state
        result = await owner._run_collection_capability(callback_id=action["callback_id"],
                                                        scope_domain=action.get("domain", ""))
        calls.extend(fake.calls)
        return json.dumps(result, default=str)

    return run


@pytest.mark.parametrize("agent, runner_command, tool_param, download_param, identity", [
    ("apollo", "execute_assembly", "assembly_name", "path", "whoami"),
    ("merlin", "execute-assembly", "filename", "file", "token"),
])
def test_the_approved_procedure_runs_each_backends_real_commands_and_verifies_the_domain(
        monkeypatch, agent, runner_command, tool_param, download_param, identity):
    from tests.test_bloodhound_ingest_contract import FakeIngestTool, install_bloodhound

    install_bloodhound(monkeypatch, FakeIngestTool())  # the image's ingest-ready MCP, as the controller tests use
    calls: list = []
    script = [_call("transfer_to_Mythic_Operator", RAW_RECIPE, 1), AIMessage(content="Collected."), *FINALS]
    run = _explicit(script, "accept", callbacks=[{"id": 5, "host": "WORKSTATION-5", "agent": agent}],
                    executor=_backend_executor(agent, calls))

    assert run.ran == [{"action": NORTH_COLLECT, "inputs": None}]
    tasks = [call for call in calls if call[0] not in {"ingest_collection", "_latest_download"}]  # Mythic tasks
    commands = [call[0] for call in tasks]
    assert commands[0] == identity and runner_command in commands and "download" in commands, commands
    assert {str(call[2]) for call in tasks} == {"5"}  # every target task on the approved callback
    assert [call[0] for call in calls].count("ingest_collection") == 1
    runner = next(call for call in calls if call[0] == runner_command)
    assert runner[1][tool_param] == "SharpHound.exe" and "--ZipFilename bloodhound_" in json.dumps(runner[1])
    assert "north.test" in json.dumps(runner[1]).casefold()
    download = next(call for call in calls if call[0] == "download")
    assert download_param in download[1]
    (result,) = [json.loads(text) for text in run.worker_tools if '"graph_verified"' in text]
    assert result["ok"] is True and result["status"] == "ingested" and result["graph_verified"] is True


# --- acceptance: the real collection transaction, through native approval, with only external I/O inert ----------

from tests.test_private_collection_transaction import collection  # noqa: E402,F401  (fixture)

APPROVED_COLLECTION_STEPS = ["whoami", "ticket_cache_list", "execute_assembly", "ls", "download"]
CORP_PROMPT = "Collect BloodHound data for corp.test through the current foothold; collection only."
CORP_COLLECT = {"capability": "collect-graph", "callback_id": "7", "domain": "corp.test"}
CORP_RAW = {**RAW_RECIPE, "callback_display_id": 7, "targets": ["corp.test", "workstation"],
            "handoff_instruction": "Stage SharpHound on callback 7, run it against corp.test, download and ingest."}


async def _real_collection_request(case, script: list, decision: str) -> _NS:
    """One native supervised request whose Mythic client is the real `MythicTools` from the private-collection
    fixture: the card's approval, `execute_capability`, the collection transaction, the Apollo adapter, the command
    resolver and ingest all run; Mythic's task API, file registry, download and BloodHound MCP are inert."""
    model = model_module.Model(
        provider="openai", model="gpt-4o-mini", system_prompt="offline collection acceptance",
        config={"configurable": {"api_key": "offline-only", "API_ENDPOINT": "http://127.0.0.1:9/v1"}},
        task_id=0, agent_task_id="collection-acceptance", mode="supervised", autonomous_solve=False,
        operation_id=1, channel_id=1, apitoken_id=0,
    )
    client = case.client
    client._private_collection_transaction = None
    client._recon_snapshot_cache = {("list_callbacks", "all"): [
        {"id": 7, "host": "workstation", "agent": "apollo", "user": "operator", "status": "alive"}]}
    client._engagement_footholds = list(case.state.footholds)
    client.set_collection_runner(model._run_collection_capability)
    model.mythic_client = client

    async def current_state():
        return case.state

    model._build_current_engagement_state = current_state
    model._payload_names = []
    model.memory = InMemorySaver()
    fake = _NativeScriptedModel(messages=iter(script))
    model._get_base_chat_model = lambda *a, **k: fake
    model._native_chat_explicit_hitl = True
    model.command_name = "chat"
    model._rebuild_graph()
    chat = _NativeDriverChat(model)
    try:
        first = build_chat_request(CORP_PROMPT, channel_id=1, operation_id=1, request_id=1,
                                   config={"mode": "supervised"})
        bind_channel_thread_id(first, model)
        await chat.chat(first)
        cards = _cards(chat)
        at_card = _NS(tasks=len(case.tasks), registered=len(case.registered), model_calls=len(fake.inputs))
        if cards and decision:
            second = build_chat_request("", channel_id=1, operation_id=1, request_id=2, config={"mode": "supervised"})
            second.InputResponse = _input_response(decision, cards[0])
            await chat.chat(second)
        snapshot = await model.graph.aget_state(model._graph_run_config(model._session_thread_id()))
        operator = snapshot.values.get("mythic_operator_messages", [])
        results = [json.loads(m.content) for m in operator
                   if getattr(m, "type", "") == "tool" and getattr(m, "name", "") == "execute_capability"
                   and str(m.content).startswith("{")]
        return _NS(cards=_cards(chat), at_card=at_card, results=results)
    finally:
        await drop_channel_session(build_chat_request("", channel_id=1, operation_id=1))


def _acceptance_script() -> list:
    return [_call("transfer_to_Mythic_Operator", CORP_RAW, 1), AIMessage(content="Collected corp.test."), *FINALS]


def test_acceptance_the_explicit_request_runs_the_real_collection_and_verifies_the_requested_domain(
        collection, monkeypatch, tmp_path):
    monkeypatch.setenv("SAGE_ENGAGEMENT_STATE_DIR", str(tmp_path / "engagement"))
    case = collection
    run = asyncio.run(_real_collection_request(case, _acceptance_script(), "accept"))

    (card,) = run.cards
    assert card["data"]["arguments"] == {"action": CORP_COLLECT}
    assert (run.at_card.tasks, run.at_card.registered, run.at_card.model_calls) == (0, 0, 1)
    # The approved transaction issued the Apollo adapter's real collection commands on the approved callback.
    assert [task["command_name"] for task in case.tasks] == APPROVED_COLLECTION_STEPS
    assert {task["callback_display_id"] for task in case.tasks} == {7}
    assert case.registered == ["SharpHound.exe"]
    assert [event for event in case.events if event[0] == "ingest"] == [("ingest", "upload_bytes"),
                                                                        ("ingest", "status")]
    tx = case.client._private_collection_transaction
    assert (tx.approved_name, tx.callback_id, tx.scope_domain) == ("execute_capability", "7", "corp.test")
    assert tx._artifact_consumed is True
    (result,) = run.results
    assert result["ok"] is True and result["status"] == "ingested" and result["capability"] == "collect-graph"


def test_acceptance_an_existing_graph_is_recollected_fresh_when_the_operator_approves(
        collection, monkeypatch, tmp_path):
    from tests.test_collection_capability import _covered

    monkeypatch.setenv("SAGE_ENGAGEMENT_STATE_DIR", str(tmp_path / "engagement"))
    case = collection
    _covered(case, monkeypatch)
    run = asyncio.run(_real_collection_request(case, _acceptance_script(), "accept"))

    assert run.cards[0]["data"]["arguments"] == {"action": CORP_COLLECT}
    assert "execute_assembly" in [task["command_name"] for task in case.tasks]
    assert case.client._private_collection_transaction._artifact_consumed is True
    (result,) = run.results
    assert result["ok"] is True and result["status"] == "ingested"


def test_acceptance_a_rejected_explicit_collection_issues_no_target_task(collection, monkeypatch, tmp_path):
    monkeypatch.setenv("SAGE_ENGAGEMENT_STATE_DIR", str(tmp_path / "engagement"))
    case = collection
    run = asyncio.run(_real_collection_request(case, _acceptance_script(), "reject"))

    assert run.cards[0]["data"]["arguments"] == {"action": CORP_COLLECT}
    assert case.tasks == [] and case.registered == []
    assert not [event for event in case.events if event[0] == "ingest"]


# --- positive control: an uncatalogued payload keeps its generic task path, with no collector substitution -------

NEBULA_LS_SCHEMA = [{"name": "path", "cli_name": "path", "display_name": "Path", "type": "String",
                     "required": True, "parameter_group_name": "Default", "choices": [], "default_value": "",
                     "description": "Directory to list"}]


def _nebula_client(monkeypatch, tmp_path):
    """A real `MythicTools` for callback 9, an observed payload type no Sage adapter or collector knows, with its
    `ls` command's live schema supplied; Mythic's task API is the inert split-issue seam."""
    monkeypatch.setenv("SAGE_ENGAGEMENT_STATE_DIR", str(tmp_path / "engagement"))
    mt = MythicTools(agent_task_id="offline-nebula")
    mt.client = object()
    mt._fetch_command_schema = lambda command, callback_display_id, **kw: asyncio.sleep(
        0, result=list(NEBULA_LS_SCHEMA) if command == "ls" else None)
    mt._authenticate_live_command = lambda command, callback: asyncio.sleep(
        0, result={"status": "available", "command": command})
    mt._resolve_payload_type = lambda callback_id: asyncio.sleep(0, result="nebula")
    mt._recon_snapshot_cache = {("list_callbacks", "all"): [
        {"id": 9, "host": "NODE-9", "agent": "nebula", "user": "operator", "status": "alive"}]}
    mt._supervised_task_state = lambda *args, **kwargs: asyncio.sleep(
        0, result={"task_completed": True, "task_status": "completed"})
    # A collection runner is installed, so the collection-request variant is refused for its payload type alone.
    mt.set_collection_runner(lambda **_kwargs: pytest.fail("no collector may run for an uncatalogued payload"))
    return mt


@pytest.mark.parametrize("prompt", [
    "Collect BloodHound data for corp.test through the current foothold; collection only.",
    "List C:\\Users\\Public on callback 9.",
])
def test_an_uncatalogued_payload_keeps_its_generic_task_path_and_runs_the_inert_handler(
        monkeypatch, tmp_path, prompt):
    from tests.test_engagement_wiring import _split_issue

    mt = _nebula_client(monkeypatch, tmp_path)
    brief = {"handoff_title": "list public", "handoff_instruction": "List C:\\Users\\Public on callback 9.",
             "allowed_tools": ["issue_task_and_waitfor_task_output"], "callback_display_id": 9,
             "targets": ["NODE-9"], "expected_result": "The directory listing."}
    ls = {"command": "ls", "parameters": {"path": "C:\\Users\\Public"}, "callback_display_id": 9}
    script = [_call("transfer_to_Mythic_Operator", brief, 1), _call("issue_task_and_waitfor_task_output", ls, 2),
              AIMessage(content="Listed."), *FINALS]
    calls: dict = {}

    async def drive():
        model = model_module.Model(
            provider="openai", model="gpt-4o-mini", system_prompt="offline generic task",
            config={"configurable": {"api_key": "offline-only", "API_ENDPOINT": "http://127.0.0.1:9/v1"}},
            task_id=0, agent_task_id="generic-task", mode="supervised", autonomous_solve=False, operation_id=1,
            channel_id=1, apitoken_id=0,
        )
        model.mythic_client = mt
        model._payload_names = []
        model.memory = InMemorySaver()
        model._get_base_chat_model = lambda *a, **k: _NativeScriptedModel(messages=iter(script))
        model._native_chat_explicit_hitl = True
        model.command_name = "chat"
        model._rebuild_graph()
        chat = _NativeDriverChat(model)
        try:
            with _split_issue(lambda: "Public\nNODE-9 listing", calls, display_id=41):
                first = build_chat_request(prompt, channel_id=1, operation_id=1, request_id=1,
                                           config={"mode": "supervised"})
                bind_channel_thread_id(first, model)
                await chat.chat(first)
                cards = _cards(chat)
                issued_at_card = len(calls.get("issued", []))
                second = build_chat_request("", channel_id=1, operation_id=1, request_id=2,
                                            config={"mode": "supervised"})
                second.InputResponse = _input_response("accept", cards[0])
                await chat.chat(second)
                return cards, issued_at_card
        finally:
            await drop_channel_session(build_chat_request("", channel_id=1, operation_id=1))

    cards, issued_at_card = asyncio.run(drive())
    (card,) = cards
    assert card["data"]["actions"][0]["tool_name"] == "issue_task_and_waitfor_task_output"
    assert "collect-graph" not in json.dumps(card)
    assert issued_at_card == 0
    assert [(i["command_name"], i["callback_display_id"]) for i in calls["issued"]] == [("ls", 9)]
    assert calls["issued"][0]["parameters"] == {"path": "C:\\Users\\Public"}


# --- the explicit binding at the hook: typed/operator agreement, redirect, lanes -----------------------------------

def _explicit_hook_model(*, operator_callback: str = "", typed: str = "", revisions: tuple[int, ...] = (0,)):
    from ai.langgraph.request_contract import build_request_contract

    model = object.__new__(model_module.Model)
    request_id = "chat:1:request:1"
    model._graph_first_step = {"request_id": request_id, "revision": 0, "source": "explicit_collection",
                               "domain": "north.test", "callback_id": operator_callback, "done": False}
    model._request_contract = build_request_contract(request_id=request_id, channel_id="1", operation_id="1",
                                                     mode="supervised", autonomous_solve=False)
    model.mythic_client = _NS(
        _recon_snapshot_cache={("list_callbacks", "all"): [dict(row) for row in TWO_APOLLO]},
        _engagement_footholds=[], _collection_runner=lambda **_: None)
    model._turn_authority = _NS(bounded_callback_id=typed)
    model.state = {"_request_id": request_id, "_request_lane": "supervised_workflow", "supervisor_messages": [
        HumanMessage(content=COLLECT_PROMPT if revision == 0 else "Do something else.", additional_kwargs={
            "_request_id": request_id, "_operator_input": True, "_operator_input_revision": revision,
            **({"_operator_steer": True} if revision else {})}) for revision in revisions]}
    return model


READ = handoff_brief.HandoffBrief.from_args(allowed_capabilities=["control_plane"], expected_result="Read.")


def test_an_operator_callback_and_an_agreeing_typed_binding_select_that_callback():
    worker, brief, *_ = _explicit_hook_model(operator_callback="7", typed="7")._graph_collection_first_step(
        "Mythic_Operator", READ, "", "read")
    assert (worker, brief.callback_display_id, brief.targets) == ("Mythic_Operator", "7", ("north.test",))
    worker, brief, *_ = _explicit_hook_model(typed="5")._graph_collection_first_step(
        "Mythic_Operator", READ, "", "read")
    assert brief.callback_display_id == "5"


def test_disagreeing_bindings_leave_the_delegation_unchanged_and_the_plan_pending():
    model = _explicit_hook_model(operator_callback="7", typed="5")
    assert model._graph_collection_first_step("Mythic_Operator", READ, "", "read") is None
    assert model._graph_first_step["done"] is False


def test_an_operator_redirect_invalidates_a_pending_explicit_binding():
    model = _explicit_hook_model(revisions=(0, 1))
    assert model._graph_collection_first_step("Mythic_Operator", READ, "", "read") is None
    assert model._graph_first_step["done"] is True
    assert model._pending_explicit_collection() is None


def test_an_unreadable_operator_input_neither_binds_nor_consumes():
    model = _explicit_hook_model(operator_callback="7")
    model.state = {}
    assert model._graph_collection_first_step("Mythic_Operator", READ, "", "read") is None
    assert model._graph_first_step["done"] is False


@pytest.mark.parametrize("mode, autonomous_solve, armed", [
    ("supervised", False, True), ("supervised", True, True), ("conversation", False, False), ("auto", True, False),
])
def test_only_the_supervised_lane_arms_an_explicit_collection(mode, autonomous_solve, armed):
    from ai.langgraph.request_contract import build_request_contract

    model = object.__new__(model_module.Model)
    model._graph_first_step = None
    contract = build_request_contract(request_id="chat:1:request:4", channel_id="1", operation_id="1", mode=mode,
                                      autonomous_solve=autonomous_solve)
    model._arm_explicit_collection_first_step(COLLECT_PROMPT, contract)
    assert (model._graph_first_step is not None) is armed
    if armed:
        assert model._graph_first_step == {"request_id": "chat:1:request:4", "revision": 0,
                                           "source": "explicit_collection", "domain": "north.test",
                                           "callback_id": "", "done": False}


def test_an_objective_request_is_never_given_an_explicit_binding():
    from ai.langgraph.request_contract import build_request_contract

    model = object.__new__(model_module.Model)
    objective_plan = {"request_id": "chat:1:request:4", "partial": False, "domains": ["north.test"], "done": False}
    model._graph_first_step = objective_plan
    contract = build_request_contract(request_id="chat:1:request:4", channel_id="1", operation_id="1",
                                      mode="supervised", autonomous_solve=False)
    model._arm_explicit_collection_first_step(OBJECTIVE, contract)
    assert model._graph_first_step is objective_plan


DA_LABEL_PROMPT = "Collect BloodHound data for north.da.test through callback 7; collection only."


def test_a_domain_label_that_reads_as_an_objective_does_not_take_the_explicit_binding_away():
    """Cold review: a domain label such as `da` makes the objective classifier call a parsed collection request
    objective, so the consult's plan was armed and the operator's callback binding was dropped. The explicit request
    does not depend on that consult."""
    assert knowledge_sources.classify_turn(DA_LABEL_PROMPT) == knowledge_sources.TURN_OBJECTIVE  # the overlap
    script = [_collect(["north.da.test"]),  # the Supervisor's own collection, on callback 5
              _call("transfer_to_Mythic_Operator", TTP_STEP, 2), AIMessage(content="Collected."), *FINALS]
    run = _explicit(script, prompt=DA_LABEL_PROMPT, callbacks=TWO_APOLLO)

    (rejected, accepted) = run.brief_events
    assert rejected[0] == "rejected" and "callback_display_id: 5 is not callback 7" in rejected[1], run.brief_events
    assert accepted == ("accepted", "Mythic_Operator")
    (card,) = run.cards
    assert card["data"]["arguments"] == {"action": {"capability": "collect-graph", "callback_id": "7",
                                                    "domain": "north.da.test"}}
    assert run.titles[0] == "Collect BloodHound data" and run.at_card.ran == 0
