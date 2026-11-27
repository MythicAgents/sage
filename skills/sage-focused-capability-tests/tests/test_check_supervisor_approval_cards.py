"""Offline checks for the Supervisor approval-card live check (no live Mythic, no provider).

Inputs, labelled where used:
- Real: Sage's own `mythic_tools.py` for the test patch; Sage's real graph, HITL and chat service driven through a real
  `ChatOpenAI` client over loopback HTTP to the check's stand-in, so the stand-in is exercised at its wire boundary.
- Recorded: ``fixtures/sandbox_request125_rows.json`` (worker request 125's rows; a worker card, so it is the
  negative case for the Supervisor precondition) and its sandbox result shape.
- Synthetic: case records and row mutations, labelled in each test, prove each rule rejects its falsifier.
"""

from __future__ import annotations

import asyncio
import copy
import importlib.util
import json
import socket
import sys
import threading
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[3]
SCRIPT = ROOT / "skills/sage-focused-capability-tests/scripts/check_supervisor_approval_cards.py"
SPEC = importlib.util.spec_from_file_location("check_supervisor_approval_cards", SCRIPT)
assert SPEC is not None and SPEC.loader is not None
check = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(check)
sys.path.insert(0, str(ROOT / "Payload_Type/sage"))
RECORDED = ROOT / "skills/sage-focused-capability-tests/tests/fixtures/sandbox_request125_rows.json"
MYTHIC_TOOLS = ROOT / "Payload_Type/sage/ai/langgraph/mythic_tools.py"
REL = "Payload_Type/sage/ai/langgraph/mythic_tools.py"


# --------------------------------------------------------------------------------------------------
# The declared test patch
# --------------------------------------------------------------------------------------------------

def test_test_guard_adds_exactly_the_guard_and_the_counter():
    source = MYTHIC_TOOLS.read_text()
    patched = check.apply_test_guard(source)
    compile(patched, "mythic_tools.py", "exec")
    added = [line for line in patched.splitlines() if line not in set(source.splitlines())]
    assert added == [check.GUARD_LINE.rstrip("\n"), *check.COUNTER_LINES.rstrip("\n").splitlines()]
    assert patched.index(check.COUNTER_LINES) < patched.index("validate_reader_selection(numbers")
    with pytest.raises(ValueError, match="already applied"):
        check.apply_test_guard(patched)
    with pytest.raises(ValueError, match="anchors"):
        check.apply_test_guard(source.replace(check.GUARD_ANCHOR, "GUARDED: set[str] = {\n"))


def test_declared_patch_holds_only_for_that_patch_on_that_file():
    source = MYTHIC_TOOLS.read_text()
    patched = check.apply_test_guard(source)
    assert check.declared_patch_holds(source, patched, [REL])
    assert not check.declared_patch_holds(source, patched, [REL, "Payload_Type/sage/sage_chat/service.py"])
    assert not check.declared_patch_holds(source, patched + "# extra\n", [REL])
    assert not check.declared_patch_holds(source, source, [])


# --------------------------------------------------------------------------------------------------
# The stand-in through Sage's real graph, over loopback HTTP
# --------------------------------------------------------------------------------------------------

@pytest.fixture
def sage(monkeypatch, tmp_path):
    from ai.langgraph import engagement_ledger
    from ai.langgraph import mythic_tools
    from sage_chat import session

    server = check.make_stub_server(0, tmp_path / "stub.jsonl")
    port = server.server_address[1]
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    real_connect = socket.socket.connect

    def loopback_only(sock, address):
        if address[:2] != ("127.0.0.1", port):
            raise AssertionError(f"offline test attempted network access to {address!r}")
        return real_connect(sock, address)

    monkeypatch.setattr(socket.socket, "connect", loopback_only)
    monkeypatch.setattr(session, "_CHANNEL_MARKER_DB_PATH", str(tmp_path / "sage.db"))
    reads = []
    real_validate = engagement_ledger.validate_reader_selection
    monkeypatch.setattr(engagement_ledger, "validate_reader_selection",
                        lambda *a, **k: (reads.append(1), real_validate(*a, **k))[1])
    sandbox_runs = []
    monkeypatch.setattr(mythic_tools.sandbox_executor, "select_executor", lambda: ("local", ""))

    def run_local(language, code, timeout):
        sandbox_runs.append((language, code))
        return {"exit_code": 0, "stdout": "4\n", "stderr": "", "timed_out": False, "truncated": False}

    monkeypatch.setattr(mythic_tools.sandbox_executor, "run_local", run_local)
    assert check.READER not in mythic_tools.GUARDED_TOOLS
    mythic_tools.GUARDED_TOOLS.add(check.READER)
    try:
        yield {"port": port, "log": tmp_path / "stub.jsonl", "reads": reads, "sandbox_runs": sandbox_runs}
    finally:
        mythic_tools.GUARDED_TOOLS.discard(check.READER)
        server.shutdown()
        server.server_close()


def _driver():
    path = ROOT / "skills/sage-focused-capability-tests/scripts/run_offline_human_led_case.py"
    spec = importlib.util.spec_from_file_location("approval_check_driver", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _episode(sage, prompt, decide):
    from langgraph.checkpoint.memory import InMemorySaver
    from mythic_container.ChatBase import ChatRequest

    from ai.langgraph.model import Model
    from ai.langgraph.mythic_tools import MythicTools
    from sage_chat import session

    driver = _driver()
    config = check.channel_config(sage["port"])

    def request(prompt_text=""):
        return ChatRequest(container_name="sage", operation_id=4, channel_id=9, request_id=125, apitokens_id=74,
                           model="Sage", prompt=prompt_text, config=dict(config), secrets={})

    async def scenario():
        model = Model(
            provider=config["provider"], model=check.MODEL, system_prompt="offline test",
            config={"configurable": {"api_key": check.PLACEHOLDER_KEY, "base_url": config["API_ENDPOINT"],
                                     "openai_api": "chat_completions"}},
            task_id=0, agent_task_id="offline-approval-check", mode="supervised", autonomous_solve=False,
            operation_id=4, channel_id=9, apitoken_id=74,
        )
        model.mythic_client = MythicTools(agent_task_id="offline-approval-check", operation_id=4, channel_id=9)
        model._payload_names = []
        model.memory = InMemorySaver()
        model._native_chat_explicit_hitl = True
        model.command_name = "chat"
        model._rebuild_graph()
        chat = driver.DriverChat(model)
        first = request(prompt)
        session.bind_channel_thread_id(first, model)
        rounds = []
        try:
            await chat.chat(first)
            chat._used = True
            for _ in range(3):
                rounds.append(list(chat.emissions))
                found = [e for e in chat.emissions if (e.get("metadata") or {}).get("special_type") == "input_requested"]
                if chat.terminal_emissions or not found:
                    break
                chat.emissions.clear()
                followup = request()
                followup.InputResponse = driver.input_response(decide, found[-1]["metadata"]["input_requested"])
                await chat.chat(followup)
            rounds.append(list(chat.emissions))
        finally:
            await session.drop_channel_session(first)
        return rounds

    return asyncio.run(scenario())


def _log(sage):
    return [json.loads(line) for line in sage["log"].read_text().splitlines()]


@pytest.mark.parametrize(("decide", "reads"), [("accept", 1), ("reject", 0)])
def test_stand_in_raises_one_supervisor_card_first_and_closes(sage, decide, reads):
    rounds = _episode(sage, check.LEDGER_PROMPT, decide)

    before = rounds[0]
    assert len(before) == 1 and before[0]["metadata"]["special_type"] == "input_requested"
    card = before[0]["metadata"]["input_requested"]
    assert card["data"]["tool_name"] == check.READER and not before[0]["metadata"].get("delegation_id")
    assert sage["reads"] == [1] * reads
    final = rounds[-1]
    terminals = [e for e in final if e.get("complete_request")]
    assert len(terminals) == 1 and terminals[0]["content"].strip() == check.LEDGER_CLOSING
    log = _log(sage)
    assert [entry["reply"] for entry in log] == [check.READER, "text"]
    assert all(entry["placeholder_key"] and entry["model"] == check.MODEL for entry in log)


def test_stand_in_drives_one_worker_sandbox_card(sage):
    rounds = _episode(sage, check.SANDBOX_PROMPT, "accept")

    worker_cards = [e for e in rounds[0] if (e.get("metadata") or {}).get("special_type") == "input_requested"]
    assert len(worker_cards) == 1
    assert worker_cards[0]["metadata"]["delegation_name"] == "Sandbox"
    assert worker_cards[0]["metadata"]["input_requested"]["data"]["tool_name"] == "sandbox_exec"
    # The worker card follows streaming subagent/tool rows, which is what moves its Mythic request to streaming.
    assert any((e.get("metadata") or {}).get("special_type") == "subagent" for e in rounds[0])
    assert sage["sandbox_runs"] == [("python", check.SANDBOX_CODE)]
    terminals = [e for e in rounds[-1] if e.get("complete_request")]
    assert len(terminals) == 1 and check.SANDBOX_CLOSING in terminals[0]["content"]
    assert [entry["reply"] for entry in _log(sage)] == ["transfer_to_Sandbox", "sandbox_exec", "text",
                                                        "respond_to_user"]


def test_stand_in_streams_the_same_reply(sage):
    from langchain_openai import ChatOpenAI

    client = ChatOpenAI(model=check.MODEL, api_key=check.PLACEHOLDER_KEY, streaming=True,
                        base_url=f"http://127.0.0.1:{sage['port']}/v1", stream_usage=True)
    tool = {"type": "function", "function": {"name": check.READER, "description": "reader",
                                             "parameters": {"type": "object", "properties": {}}}}
    reply = client.bind_tools([tool]).invoke(check.LEDGER_PROMPT)
    assert [call["name"] for call in reply.tool_calls] == [check.READER]
    assert _log(sage)[-1] == {**_log(sage)[-1], "stream": True, "placeholder_key": True}


def test_stand_in_closes_on_anything_else():
    assert check.stub_decision({"messages": [{"role": "user", "content": "hello"}], "tools": []}) == {
        "kind": "text", "text": check.DEFAULT_CLOSING}
    reader_tool = [{"type": "function", "function": {"name": check.READER}}]
    answered = {"messages": [{"role": "user", "content": check.LEDGER_PROMPT},
                             {"role": "assistant", "tool_calls": [{"id": "c1", "function": {"name": check.READER}}]},
                             {"role": "tool", "tool_call_id": "c1", "content": "User rejected the tool call"}],
                "tools": reader_tool}
    assert check.stub_decision(answered)["kind"] == "text"


# --------------------------------------------------------------------------------------------------
# Pass rules
# --------------------------------------------------------------------------------------------------

def _supervisor_rows(status="pending", card_status="pending", **card_metadata):
    """Synthetic: the request rows the headless Supervisor episode emits before a decision."""
    return {"status": status, "messages": [
        {"id": 2000, "author_type": "operator", "metadata": {}},
        {"id": 2001, "author_type": "ai", "metadata": {
            "special_type": "input_requested", **card_metadata,
            "input_requested": {"status": card_status, "input_type": "approval",
                                "data": {"tool_name": check.READER}}}},
    ]}


def test_pre_decision_needs_a_pending_supervisor_card_and_no_reads():
    assert check.pre_decision(_supervisor_rows(), 0)["exercised"]
    assert not check.pre_decision(_supervisor_rows(status="streaming"), 0)["exercised"]
    assert not check.pre_decision(_supervisor_rows(), 1)["exercised"]
    assert not check.pre_decision(_supervisor_rows(delegation_id="d1"), 0)["exercised"]
    assert not check.pre_decision(_supervisor_rows(card_status="accepted"), 0)["exercised"]
    recorded = json.loads(RECORDED.read_text())
    assert not check.pre_decision({**recorded, "status": "pending"}, 0)["exercised"]  # a worker card


def _decision_case(name, *, after_status=None, after_delta=None, request_status="complete", helper=None,
                   mythic=None, settled_status=None, settled_delta=None):
    accept = name.endswith("accept")
    want_status, want_delta = ("accepted", 1) if accept else ("rejected", 0)
    pre = check.pre_decision(_supervisor_rows(), 0)
    after_rows = _supervisor_rows(status=request_status, card_status=after_status or want_status)
    settled_rows = _supervisor_rows(status=request_status, card_status=settled_status or want_status)
    return {"name": name, "pre": pre,
            "after": {"rows": after_rows, "reader_delta": want_delta if after_delta is None else after_delta},
            "second": {"helper": helper or {"refused": True, "mutations_sent": 0},
                       "mythic": mythic or {"status": "error", "error": "input request is no longer pending"},
                       "rows": settled_rows,
                       "reader_delta": want_delta if settled_delta is None else settled_delta}}


@pytest.mark.parametrize("name", check.DECISION_CASES)
def test_decision_rules_pass_on_the_intended_effect(name):
    result = check.decision_outcome(_decision_case(name))
    assert result["exercised"] and result["P1"] and result["P2"]


@pytest.mark.parametrize("name", check.DECISION_CASES)
@pytest.mark.parametrize(("change", "rule"), [
    ({"after_status": "pending"}, "P1"),
    ({"after_delta": 2}, "P1"),
    ({"request_status": "streaming"}, "P1"),
    ({"helper": {"refused": False, "mutations_sent": 1}}, "P2"),
    ({"helper": {"refused": True, "mutations_sent": 1}}, "P2"),
    ({"mythic": {"status": "success", "error": ""}}, "P2"),
    ({"settled_status": "responded"}, "P2"),
    ({"settled_delta": 3}, "P2"),
])
def test_decision_rules_reject_each_falsifier(name, change, rule):
    """Synthetic mutations of the passing record, one at a time."""
    assert not check.decision_outcome(_decision_case(name, **change))[rule]


def test_reject_must_not_move_the_reader():
    assert not check.decision_outcome(_decision_case("ui-reject", after_delta=1, settled_delta=1))["P1"]


def _stop_case(**changes):
    case = {"pre": {"exercised": True},
            "after": {"rows": {"status": "cancelled"}, "acknowledged": True, "reader_delta": 0},
            "idle": {"status": "complete"}}
    for key, value in changes.items():
        section, field = key.split("__")
        case[section][field] = value
    return case


def test_stop_rule():
    assert check.stop_outcome(_stop_case())["R1"]
    for change in ({"after__acknowledged": False}, {"after__reader_delta": 1}, {"idle__status": "error"},
                   {"after__rows": {"status": "complete"}}):
        assert not check.stop_outcome(_stop_case(**change))["R1"]


def _worker_case(stdout="4\n", decisions=None, status="complete"):
    """The recorded request 125 rows, with the sandbox stdout replaced (synthetic) by this check's calculation."""
    rows = copy.deepcopy(json.loads(RECORDED.read_text()))
    rows["status"] = status
    for row in rows["messages"]:
        tool_use = (row.get("metadata") or {}).get("tool_use")
        if tool_use and tool_use.get("tool_name") == "sandbox_exec":
            result = json.loads(row.get("tool_output") or tool_use.get("result_preview"))
            result["stdout"] = stdout
            row["tool_output"] = json.dumps(result)
    return {"after": {"rows": rows},
            "decisions": decisions if decisions is not None else [{"decision": "accept", "request_status": "streaming"}]}


def test_worker_rule_on_recorded_rows():
    recorded = json.loads(RECORDED.read_text())
    original = check.worker_outcome({"after": {"rows": recorded},
                                     "decisions": [{"decision": "accept", "request_status": "streaming"}]})
    assert original["exercised"] and not original["R2"]  # its stdout is the digest run, not 4
    assert check.worker_outcome(_worker_case())["R2"]
    assert not check.worker_outcome(_worker_case(stdout="5\n"))["R2"]
    assert not check.worker_outcome(_worker_case(status="cancelled"))["R2"]
    assert not check.worker_outcome(
        _worker_case(decisions=[{"decision": "accept", "request_status": "pending"}]))["exercised"]


def test_global_rule():
    spans = [{"model": check.MODEL}]
    log = [{"placeholder_key": True}]
    assert check.global_outcome(spans, log, 0, 7, 600)["G"]
    assert not check.global_outcome([*spans, {"model": "gpt-5.6-cyber"}], log, 0, 7, 600)["G"]
    assert not check.global_outcome(spans, [{"placeholder_key": False}], 0, 7, 600)["G"]
    assert not check.global_outcome(spans, [], 0, 7, 600)["G"]
    assert not check.global_outcome(spans, log, 1, 7, 600)["G"]
    assert not check.global_outcome(spans, log, 0, 11, 600)["G"]
    assert not check.global_outcome(spans, log, 0, 7, 1201)["G"]


def test_overall_verdict():
    good = {"exercised": True, "P1": True, "P2": True}
    assert check.overall({"a": good, "b": {"exercised": True, "R1": True}}, {"G": True}) == "pass"
    assert check.overall({"a": good, "b": {"exercised": True, "R1": False}}, {"G": True}) == "fail"
    assert check.overall({"a": good}, {"G": False}) == "fail"
    assert check.overall({"a": good, "b": {"exercised": False}}, {"G": True}) == "unexercised"
