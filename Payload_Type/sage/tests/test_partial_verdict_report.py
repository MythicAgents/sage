"""A supervised step whose executor verdict is not `achieved` leads the operator's closing with Sage's verification.

The Supervisor model writes the request's closing. When a step in that request ran a catalog procedure that did not
achieve its effect, and no later step achieved the same capability and target, Sage puts its own line first: the
capability, the task, the verdict, the effects recorded, the declared effects not confirmed and the Mythic status of
any task that is not a terminal success. The narrative follows unchanged.

Real: the native chat driver, graph, catalog binding, approval card and resume, MythicTools' executor and credential
review, the step outcome and its Mythic references, and the closing stream. Synthetic: the ledger state (from the
catalog-binding fixtures), Mythic I/O (inert task issue, task output and task records) and the scripted model choices.
The two closings are recorded Supervisor narratives (`fixtures/model_input_boundaries/partial_verdict_closings.json`);
scripted output proves what code does with them, not what a model would write.
"""

from __future__ import annotations

import asyncio
import base64
import json
from pathlib import Path
import socket
from types import SimpleNamespace

import pytest
from langchain_core.messages import AIMessage
from langgraph.checkpoint.memory import InMemorySaver

from ai.langgraph import model as model_module
from ai.langgraph import mythic_tools as package_mythic_tools
from ai.langgraph import step_bounds as sb
from ai.langgraph import task_evidence
from sage_chat.headless import build_chat_request
from sage_chat.service import bind_channel_thread_id
from sage_chat.session import drop_channel_session
from tests.esl_fixtures import AGENT_PROCESSED
from tests.test_hitl_steer_current_intent import _NativeDriverChat, _NativeScriptedModel, _call, _cards, _input_response
from tests.test_supervised_catalog_step_binding import PROMPT, _isolated, _run, _select, _state_for
from tests.test_worker_answer_handback import _ANSWER, _valid_state

FIXTURE = json.loads((Path(__file__).parent / "fixtures/model_input_boundaries/partial_verdict_closings.json")
                     .read_text())["rows"]
NARRATIVES = {row["id"]: row["narrative"] for row in FIXTURE}
LEAD = "**Sage verification:**"
NTLM, AES = "0123456789abcdef" * 2, "00112233445566778899aabbccddeeff" * 2
DCSYNC = (
    "[DC] 'child.lab.local' will be the domain\n[DC] 'WS02.child.lab.local' will be the DC server\n"
    "[DC] 'CHILD\\krbtgt' will be the user account\n** SAM ACCOUNT **\nSAM Username         : krbtgt\n"
    f"Credentials:\n  Hash NTLM: {NTLM}\n* Primary:Kerberos-Newer-Keys *\n  aes256_hmac       (4096) : {AES}\n"
    "[*] PE function returned with exit code: 0\n"
)
COMPLETION_ERROR = {"completed": True, "status": task_evidence.MYTHIC_COMPLETION_FUNCTION_ERROR,
                    "callback": {"display_id": 14}, "responses": [{"id": 1}], **AGENT_PROCESSED}


@pytest.fixture(autouse=True)
def no_network(monkeypatch):
    def denied(*args, **kwargs):
        raise AssertionError("network attempted")
    monkeypatch.setattr(socket.socket, "connect", denied)
    monkeypatch.setattr(socket.socket, "connect_ex", denied)


def _dcsync_client(monkeypatch, path, records, *, completion_error=True):
    """The catalog fixtures (own ledger under `path`) with a dcsync-krbtgt candidate for child.lab.local; its task (31
    onward) returns krbtgt output, and Mythic records the completion-function error on it unless `completion_error`
    is False. Mythic's step-reference query serves the same records (an ordinary success when none is set)."""
    mt, split = _isolated(monkeypatch, path)
    _state_for(mt, "dcsync-krbtgt")
    if completion_error:
        records.update({task: COMPLETION_ERROR for task in range(31, 36)})
    earlier = package_mythic_tools.mythic.get_all_task_output_by_id
    earlier_query = package_mythic_tools.mythic.execute_custom_query

    async def query(mythic, query, variables=None, **kwargs):
        if "sageStepTaskRecords" not in query:
            return await earlier_query(mythic=mythic, query=query, variables=variables, **kwargs)
        return {"task": [{"display_id": i, "command_name": "dcsync", "callback": {"display_id": 14},
                          **{key: (records.get(i) or {"status": "success", "completed": True})[key]
                             for key in ("status", "completed")}} for i in variables["display_ids"]]}

    monkeypatch.setattr(package_mythic_tools.mythic, "execute_custom_query", query)

    async def task_output(mythic, task_display_id, custom_return_attributes=None):
        if int(task_display_id) >= 31:
            return [{"response_text": base64.b64encode(DCSYNC.encode()).decode()}]
        return await earlier(mythic, task_display_id, custom_return_attributes)

    monkeypatch.setattr(package_mythic_tools.mythic, "get_all_task_output_by_id", task_output)
    return mt, lambda _ignored, calls, display_id: split(lambda: DCSYNC, calls, display_id=display_id)


def _closing(form: str, text: str):
    if form == "direct":
        return AIMessage(content=text)
    if form == "direct-list":
        return AIMessage(content=[{"type": "text", "text": text}])
    if form == "respond-completed":
        return _call("respond_to_user", {"final_response": text, "outcome": "completed"}, 9)
    return _call("respond_to_user", {"final_response": text}, 9)


def _terminal(run) -> str:
    return "\n".join(str(e.get("content") or "") for e in run.terminals).strip()


def _supervisor_input(run) -> str:
    return "\n".join(str(m.content) for m in run.values.get("supervisor_messages", []))


async def _requests(mt, split, script, prompts=()):
    """`_run` followed by further operator requests on the same channel, each answered by the script."""
    model = model_module.Model(
        provider="openai", model="gpt-4o-mini", system_prompt="offline partial verdict",
        config={"configurable": {"api_key": "offline-only", "API_ENDPOINT": "http://127.0.0.1:9/v1"}},
        task_id=0, agent_task_id="partial-verdict", mode="supervised", operation_id=1, channel_id=1, apitoken_id=0)
    model.mythic_client, model._payload_names, model.memory = mt, [], InMemorySaver()
    fake = _NativeScriptedModel(messages=iter(list(script) + [AIMessage(content="Final answer.")] * 6))
    model._get_base_chat_model = lambda *a, **k: fake
    model._native_chat_explicit_hitl, model.command_name = True, "chat"
    model._rebuild_graph()
    chat, calls = _NativeDriverChat(model), {}
    try:
        with split(lambda: "ok", calls, display_id=lambda: 30 + len(calls.get("issued", []))):
            first = build_chat_request(PROMPT, channel_id=1, operation_id=1, request_id=1, config={"mode": "supervised"})
            bind_channel_thread_id(first, model)
            await chat.chat(first)
            card = next(c for c in _cards(chat) if c.get("status") not in {"accepted", "rejected", "responded"})
            answer = build_chat_request("", channel_id=1, operation_id=1, request_id=2, config={"mode": "supervised"})
            answer.InputResponse = _input_response("accept", card)
            await chat.chat(answer)
            marks = []
            for number, prompt in enumerate(prompts, start=3):
                marks.append(len(chat.emissions))
                await chat.chat(build_chat_request(prompt, channel_id=1, operation_id=1, request_id=number,
                                                   config={"mode": "supervised"}))
        return SimpleNamespace(model=model, emissions=chat.emissions, marks=marks, issued=calls.get("issued", []))
    finally:
        await drop_channel_session(build_chat_request("", channel_id=1, operation_id=1))


def _run_dcsync(monkeypatch, path, records, closing, **kwargs):
    mt, split = _dcsync_client(monkeypatch, path, records, **kwargs)
    return asyncio.run(_run(mt, split, [_select("dcsync-krbtgt"), closing]))


# --- (a) the line leads every model-written closing after a partial step -------------------------------------------

BANNER = "📊 **Final Report**"


def _line_block(terminal: str, narrative: str, form: str) -> str:
    """The text Sage put before the narrative, after any banner; asserts the narrative follows it unchanged."""
    head = terminal
    if form == "respond-completed":
        assert head.startswith(BANNER + "\n"), head[:200]
        head = head[len(BANNER) + 1:]
    assert head.startswith(LEAD), head[:300]
    body = narrative.strip()
    assert body in head, "the narrative must follow unchanged"
    return head[:head.index(body)].strip()


@pytest.mark.parametrize("form", ["direct", "direct-list", "respond-completed", "respond-no-outcome"])
def test_partial_step_leads_the_closing_with_sages_line(monkeypatch, tmp_path, mythic_task_records, form):
    blocks = {}
    for row_id, narrative in NARRATIVES.items():
        run = _run_dcsync(monkeypatch, tmp_path / row_id, mythic_task_records, _closing(form, narrative))
        assert len(run.cards) == 1 and [row["command_name"] for row in run.issued] == ["dcsync"]
        supervisor_input = _supervisor_input(run)
        assert "Executor verdict: `partial` for `dcsync-krbtgt`" in supervisor_input
        assert LEAD not in supervisor_input            # the Supervisor's input is unchanged: the line is chat-only
        blocks[row_id] = _line_block(_terminal(run), narrative, form)
    line = blocks["run5-request8"]
    assert blocks["run5-request11"] == line           # the same outcome gives the same line, whatever the narrative
    for fact in ("`dcsync-krbtgt`", "task 31", "`partial`", "`cred-material:hash:krbtgt@child.lab.local`"):
        assert fact in line, (fact, line)
    # Mythic's status as Sage's record read returns it (folded, as in the recorded step outcome).
    assert f"task 31 `{task_evidence.MYTHIC_COMPLETION_FUNCTION_ERROR.casefold()}`" in line
    assert "Not confirmed: `krbtgt-hash:child.lab.local`" in line
    assert "success" not in line.replace("terminal success", "")   # never the payload's issued result_class


# --- (b) scope: a blocked step gets a line; rejected near-matches and other lanes get none ------------------------

def test_blocked_collection_step_gets_a_line(monkeypatch, tmp_path):
    mt, split = _isolated(monkeypatch, tmp_path / "run")
    brief = {"handoff_title": "collect", "handoff_instruction": PROMPT, "allowed_capabilities": ["collect-graph"],
             "callback_display_id": "14", "targets": ["child.lab.local"], "expected_result": "Graph collected."}
    mt._collection_runner = None                         # Sage has no collector here: a refusal before any task
    run = asyncio.run(_run(mt, split, [_call("transfer_to_Mythic_Operator", brief, 1),
                                       AIMessage(content="Collection is done.")]))
    terminal = _terminal(run)
    assert len(run.cards) == 1 and run.issued == []
    assert terminal.startswith(LEAD) and "`collect-graph`" in terminal and "`blocked`" in terminal
    assert terminal.endswith("Collection is done.")


def test_achieved_step_adds_no_line(monkeypatch, tmp_path, mythic_task_records):
    run = _run_dcsync(monkeypatch, tmp_path / "run", mythic_task_records, AIMessage(content="The DCSync completed."),
                      completion_error=False)
    assert "Executor verdict: `achieved` for `dcsync-krbtgt`" in _supervisor_input(run)
    assert _terminal(run) == "The DCSync completed."


def test_proposals_json_closing_is_exempt(monkeypatch, tmp_path, mythic_task_records):
    proposals = json.dumps({"proposals": [{"capability": "forge-golden-ticket"}]})
    run = _run_dcsync(monkeypatch, tmp_path / "run", mythic_task_records,
                      _call("respond_to_user", {"final_response": proposals}, 9))
    assert _terminal(run) == proposals


def test_line_is_not_carried_into_a_later_request(monkeypatch, tmp_path, mythic_task_records):
    mt, split = _dcsync_client(monkeypatch, tmp_path / "run", mythic_task_records)
    later = "Callback 14 is still live."
    run = asyncio.run(_requests(mt, split, [_select("dcsync-krbtgt"), AIMessage(content=NARRATIVES["run5-request8"]),
                                            AIMessage(content=later)], prompts=["Is callback 14 still live?"]))
    led = [e for e in run.emissions[:run.marks[0]] if LEAD in str(e.get("content") or "")]
    second = "\n".join(str(e.get("content") or "") for e in run.emissions[run.marks[0]:])
    # Only the closing (its streamed chunk and its final response) carries the line: no card, tool or handoff.
    assert led and all(str(e["content"]).startswith(LEAD) and not (e.get("metadata") or {}).get("special_type")
                       for e in led)
    # The later request ran no step, so its closing is Sage's zero-step notice (test_supervisor_report_provenance).
    assert LEAD not in second and later not in second and "Sage has no completed step recorded" in second


def test_reused_answer_is_led_and_a_sage_authored_final_is_not(monkeypatch, tmp_path, mythic_task_records):
    """Synthetic stream events over the real step outcome of a native partial run. A worker answer cannot be reused
    after an executor step (its handback is Sage's report), so the reused final comes from the real `respond_to_user`
    over a state that has one; a Sage-authored final carries `_is_final_report` without the model's mark."""
    run = _run_dcsync(monkeypatch, tmp_path / "run", mythic_task_records, AIMessage(content="done"))
    emitted: list[str] = []

    async def emit(text):
        emitted.append(text)
        return True

    run.model._response_emitter = emit
    respond = model_module._create_respond_to_user_tool()
    reused = respond.func(SimpleNamespace(state=_valid_state(), tool_call_id="reuse"), reuse_latest_worker_answer=True,
                          outcome="completed").update["supervisor_messages"][0]
    sage_final = AIMessage(content="Sage stopped this request.", name="Supervisor",
                           additional_kwargs={"_is_final_report": True, "_turn_outcome": "blocked"})
    for message in (reused, sage_final):
        asyncio.run(run.model._process_stream_event({"Supervisor": {"supervisor_messages": [message]}}))
    led, plain = emitted[-2].strip(), emitted[-1]
    assert led.startswith(BANNER + "\n" + LEAD) and "`partial`" in led and _ANSWER.strip() in led
    assert LEAD not in plain and "Sage stopped this request." in plain


@pytest.mark.parametrize("lane", ["conversation", "autonomous_solve"])
def test_other_lanes_get_no_line(monkeypatch, tmp_path, mythic_task_records, lane):
    """Neither lane keeps supervised step records. Conversation answers directly; offline, Autonomous Solve ends on its
    own policy blocker (its policy model is unreachable here), and neither ending carries a line."""
    mt, split = _dcsync_client(monkeypatch, tmp_path / "run", mythic_task_records)
    text = NARRATIVES["run5-request8"]
    kwargs = {"mode": "conversation"} if lane == "conversation" else {"autonomous_solve": True}
    run = asyncio.run(_run(mt, split, [AIMessage(content=text)], **kwargs))
    assert run.outcomes == [] and _terminal(run) and LEAD not in _terminal(run)
    if lane == "conversation":
        assert _terminal(run) == text.strip()


# --- the renderer, on recorded payloads and near-matches -----------------------------------------------------------

def _outcome(results, tasks=()):
    return {"executor_results": results, "references": {"tasks": list(tasks), "credentials": [], "graph_reads": []}}


def _recorded(row):
    payload = dict(row["executor_payload"])
    payload["action"] = {"target": "child.lab.local", "effects": payload.pop("action_effects")}  # recorded effects
    return sb.executor_result(payload)


def test_recorded_payload_reads_missing_effects_not_issued_result_class():
    result = _recorded(FIXTURE[0])
    assert result["verdict"] == "partial" and result["task_ids"] == [16]
    assert result["unconfirmed"] == ["krbtgt-hash:child.lab.local"]
    unread = sb.verdict_lines([_outcome([result])])
    status = {"task_id": 16, "read": "ok", "completed": True, "status": "error: completion function"}
    read = sb.verdict_lines([_outcome([result], [status])])
    assert "task 16 status not read" in unread[0] and "`error: completion function`" in read[0]
    # Output read but the task record not (record query failed or ambiguous): the status is still unread.
    no_record = {"task_id": 16, "read": "ok", "completed": None, "status": None}
    assert "task 16 status not read" in sb.verdict_lines([_outcome([result], [no_record])])[0]


def test_unconfirmed_effects_without_missing_effects_are_declared_minus_achieved():
    result = sb.executor_result({"capability": "x", "verdict": "partial", "action": {"target": "t", "effects": [
        "a:1", "B:2"]}, "achieved_effects": ["b:2"], "issued": []})
    assert result["unconfirmed"] == ["a:1"]


def test_a_later_achieved_result_supersedes_and_achieved_terminal_success_adds_nothing():
    partial = _recorded(FIXTURE[0])
    achieved = dict(partial, verdict="achieved", unconfirmed=[], task_ids=[17])
    success = {"task_id": 17, "read": "ok", "completed": True, "status": "success"}
    assert sb.verdict_lines([_outcome([partial]), _outcome([achieved], [success])]) == []
    assert sb.verdict_lines([_outcome([achieved], [success]), _outcome([partial])])   # a later partial still leads
    other_target = dict(achieved, target="lab.local")
    assert sb.verdict_lines([_outcome([partial]), _outcome([other_target], [success])])
