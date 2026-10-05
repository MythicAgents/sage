"""Inert checks for the native operator matrix harness boundary."""

import asyncio
import hashlib
import importlib.util
import json
from pathlib import Path
import socket

from langchain_core.messages import AIMessage
import pytest


SCRIPT = Path(__file__).resolve().parents[3] / "skills/sage-focused-capability-tests/scripts/run_operator_prompt_matrix.py"


def module():
    spec = importlib.util.spec_from_file_location("operator_prompt_matrix", SCRIPT)
    loaded = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(loaded)
    return loaded


def card(actions):
    return [{"metadata": {"input_requested": {"data": {"actions": actions}}}}]


def action(command, parameters="", callback=7):
    return {"tool_name": "issue_task_and_waitfor_task_output", "arguments": {
        "command": command, "parameters": parameters, "callback_display_id": callback}}


def test_scope_accepts_only_bounded_local_actions():
    runner = module()
    assert runner.card_scope("04_whoami_accept", card([action("whoami")]))[0]
    assert runner.card_scope("04_whoami_accept", card([action("shell", '{"command":"whoami"}')]))[0]
    assert not runner.card_scope("04_whoami_accept", card([action("ps")]))[0]
    assert not runner.card_scope("04_whoami_accept", card([action("whoami", callback=8)]))[0]
    assert runner.card_scope("05_enumerate_accept", card([
        action("shell", '{"command":"hostname"}'), action("ps"),
        action("shell", '{"command":"net user"}')]))[0]
    assert not runner.card_scope("05_enumerate_accept", card([action("shell", '{"command":"net user /domain"}')]))[0]
    assert runner.card_scope("05_enumerate_accept", card([action("shell", '{"command":"query user"}')]))[0]
    assert runner.card_scope("05_enumerate_accept", card([action("shell", '{"command":"quser"}')]))[0]
    assert not runner.card_scope("05_enumerate_accept", card([action("shell", '{"command":"query user /server:remote"}')]))[0]
    assert not runner.card_scope("05_enumerate_accept", card([action("shell", '{"command":"whoami","command":"hostname"}')]))[0]


def test_dry_run_does_not_open_network(tmp_path, monkeypatch, capsys):
    runner = module()
    profile = tmp_path / "profile"
    profile.write_text("provider=openai\nmodel=synthetic-test\nAPI_ENDPOINT=https://127.0.0.1:7443/v1\nAPI_KEY=secret-never-print\n")
    monkeypatch.setattr(socket.socket, "connect", lambda *_: (_ for _ in ()).throw(AssertionError("network")))
    assert runner.main(["--config", str(profile), "--output-dir", str(tmp_path / "out")]) == 0
    output = capsys.readouterr().out
    assert json.loads(output)["status"] == "dry_run_ready"
    assert "secret-never-print" not in output


def test_callback_inventory_runs_through_real_chat_without_network(tmp_path, monkeypatch):
    runner = module()
    from sage_chat import session
    monkeypatch.setattr(session, "_CHANNEL_MARKER_DB_PATH", str(tmp_path / "sage.db"))
    monkeypatch.setattr(socket.socket, "connect", lambda *_: (_ for _ in ()).throw(AssertionError("network")))
    monkeypatch.setattr(socket.socket, "connect_ex", lambda *_: (_ for _ in ()).throw(AssertionError("network")))
    fake = runner.CASE.ScriptedModel(messages=iter([AIMessage(content="Synthetic callback inventory shown.")]))
    monkeypatch.setattr(runner.model_module, "init_chat_model", lambda *a, **kw: fake)
    profile = {"provider": "openai", "model": "synthetic-test", "API_ENDPOINT": "https://127.0.0.1:7443/v1", "API_KEY": "offline"}
    limits = type("Limits", (), {"max_case_calls": 4, "max_calls": 20, "max_case_tokens": 10000,
        "max_tokens": 200000, "output_tokens": 512, "request_seconds": 5, "max_wall": 30})()
    result = asyncio.run(runner.run_case("01_callbacks", "list_callbacks", None, profile,
        tmp_path, limits, None, {"127.0.0.1"}))
    assert result["status"] == "observed", result.get("error")
    assert any(event["tool"] == "list_callbacks" and "LAB-WS01" in event["result"]
               for event in result["tool_events"])
    assert not result["effects"]


def test_accepted_whoami_card_reaches_inert_final_sink_once(tmp_path, monkeypatch):
    runner = module()
    from sage_chat import session
    monkeypatch.setattr(session, "_CHANNEL_MARKER_DB_PATH", str(tmp_path / "sage.db"))
    monkeypatch.setattr(socket.socket, "connect", lambda *_: (_ for _ in ()).throw(AssertionError("network")))
    monkeypatch.setattr(socket.socket, "connect_ex", lambda *_: (_ for _ in ()).throw(AssertionError("network")))
    fake = runner.CASE.ScriptedModel(messages=iter([
        runner.CASE.call("transfer_to_Mythic_Operator", {
            "handoff_title": "identity", "handoff_instruction": "Run whoami on callback 7 and report the output."}, 1),
        runner.CASE.call("issue_task_and_waitfor_task_output", {
            "command": "whoami", "parameters": "", "callback_display_id": 7}, 2),
        AIMessage(content="I will report the accepted task output."),
        AIMessage(content="The task output identifies LAB\\analyst."),
    ]))
    monkeypatch.setattr(runner.model_module, "init_chat_model", lambda *a, **kw: fake)
    profile = {"provider": "openai", "model": "synthetic-test", "API_ENDPOINT": "https://127.0.0.1:7443/v1", "API_KEY": "offline"}
    limits = type("Limits", (), {"max_case_calls": 6, "max_calls": 20, "max_case_tokens": 10000,
        "max_tokens": 200000, "output_tokens": 512, "request_seconds": 5, "max_wall": 30})()
    result = asyncio.run(runner.run_case("04_whoami_accept", "Run whoami on callback 7 and report the output.",
        "accept", profile, tmp_path, limits, None, {"127.0.0.1"}))
    assert result["status"] == "observed", result.get("error")
    assert result["pre_decision_effects"] == []
    assert result["card_reviews"][0]["exact"] is True
    assert result["response_matches_pending"] == [True]
    assert len(result["effects"]) == 1
    assert result["effects"][0]["task_id"] == 701
    assert result["effects"][0]["output"].endswith("LAB\\analyst")
    assert [event["status"] for event in result["execution_events"]] == ["started", "completed"]
    assert not result["denials"]
    assert not any(event.get("error") for event in result["tool_events"])


def test_two_sequential_main_cases_keep_logging_stream_open(tmp_path, monkeypatch, capsys):
    runner = module()
    from mythic_container.logging import logger as mythic_logger
    monkeypatch.setattr(mythic_logger, "handlers", [])
    profile = tmp_path / "profile"
    endpoint = "https://127.0.0.1:7443/v1"
    profile.write_text(f"provider=openai\nmodel=synthetic-test\nAPI_ENDPOINT={endpoint}\nAPI_KEY=offline\n")
    monkeypatch.setattr(runner, "PROMPTS", (
        ("03_commands", "Which commands are available on callback 7?", None),
        ("06_explain", "Explain how you would enumerate processes on callback 7. Do not execute anything.", None),
    ))
    monkeypatch.setattr(runner.REAL, "sage_tls_context", lambda: None)
    # Fake models make no provider requests; their callback usage has no wire token count.
    monkeypatch.setattr(runner, "totals", lambda _path: (0, 0, False))
    constructed = 0
    def fake_factory(*_args, **_kwargs):
        nonlocal constructed
        constructed += 1
        if constructed == 1:
            messages = [
                runner.CASE.call("transfer_to_Mythic_Operator", {
                    "handoff_title": "commands", "handoff_instruction": "Which commands are available on callback 7?"}, 1),
                runner.CASE.call("list_callbacks", {}, 2),
                runner.CASE.call("get_all_command_names_for_payloadtype", {"payload": "apollo"}, 3),
                AIMessage(content="Apollo has whoami, shell, ps."),
                AIMessage(content="Available commands: whoami, shell, ps."),
            ]
        else:
            messages = [AIMessage(content="A bounded read-only answer.")]
        return runner.CASE.ScriptedModel(messages=iter(messages))
    monkeypatch.setattr(runner.model_module, "init_chat_model", fake_factory)
    monkeypatch.setattr(socket.socket, "connect", lambda *_: (_ for _ in ()).throw(AssertionError("network")))
    output = tmp_path / "out"
    runner.main(["--run", "--config", str(profile), "--output-dir", str(output),
        "--expected-model", "synthetic-test", "--expected-endpoint-sha256",
        hashlib.sha256(endpoint.encode()).hexdigest()])
    capsys.readouterr()
    first = json.loads((output / "case_03_commands.json").read_text())
    second = json.loads((output / "case_06_explain.json").read_text())
    assert first["status"] == second["status"] == "observed"
    assert "closed file" not in json.dumps(second)


def test_fresh_history_and_local_ttp_guidance_use_fixture_only(tmp_path, monkeypatch):
    runner = module()
    from sage_chat import session
    monkeypatch.setattr(session, "_CHANNEL_MARKER_DB_PATH", str(tmp_path / "sage.db"))
    monkeypatch.setattr(socket.socket, "connect", lambda *_: (_ for _ in ()).throw(AssertionError("network")))
    monkeypatch.setattr(socket.socket, "connect_ex", lambda *_: (_ for _ in ()).throw(AssertionError("network")))
    goal = "enumerate host information, running processes, and local users"
    fake = runner.CASE.ScriptedModel(messages=iter([
        runner.CASE.call("transfer_to_Mythic_Operator", {
            "handoff_title": "enumerate", "handoff_instruction": "Which commands are available on callback 7?"}, 1),
        runner.CASE.call("get_task_history_for_callback", {"callback_display_id": 7}, 2),
        runner.CASE.call("get_ttp_guidance", {"goal": goal, "callback_display_id": 7}, 3),
        runner.CASE.call("list_ttp_categories", {}, 4),
        runner.CASE.call("get_ttp_full_reference", {"slug": "sharphound"}, 5),
        runner.CASE.call("list_open_artifacts", {}, 6),
        AIMessage(content="No historical tasks; local guidance consulted."),
        AIMessage(content="No historical tasks; local guidance consulted."),
    ]))
    monkeypatch.setattr(runner.model_module, "init_chat_model", lambda *a, **kw: fake)
    profile = {"provider": "openai", "model": "synthetic-test", "API_ENDPOINT": "https://127.0.0.1:7443/v1", "API_KEY": "offline"}
    limits = type("Limits", (), {"max_case_calls": 6, "max_calls": 20, "max_case_tokens": 10000,
        "max_tokens": 200000, "output_tokens": 512, "request_seconds": 5, "max_wall": 30})()
    result = asyncio.run(runner.run_case("03_commands", "Which commands are available on callback 7?",
        None, profile, tmp_path, limits, None, {"127.0.0.1"}))
    assert result["status"] == "observed", result.get("error")
    assert [event["tool"] for event in result["tool_events"]] == [
        "get_task_history_for_callback", "get_ttp_guidance", "list_ttp_categories",
        "get_ttp_full_reference", "list_open_artifacts"]
    assert json.loads(result["tool_events"][0]["result"]) == []
    guidance = json.loads(result["tool_events"][1]["result"])
    assert guidance.get("goal") == goal or guidance.get("payload_type") == "apollo"
    assert isinstance(json.loads(result["tool_events"][2]["result"]), dict)
    assert json.loads(result["tool_events"][3]["result"])["slug"] == "sharphound"
    assert json.loads(result["tool_events"][4]["result"]) == []
    assert not result["effects"]


def test_accepted_task_appears_in_synthetic_history(tmp_path, monkeypatch):
    runner = module()
    from sage_chat import session
    monkeypatch.setattr(session, "_CHANNEL_MARKER_DB_PATH", str(tmp_path / "sage.db"))
    monkeypatch.setattr(socket.socket, "connect", lambda *_: (_ for _ in ()).throw(AssertionError("network")))
    monkeypatch.setattr(socket.socket, "connect_ex", lambda *_: (_ for _ in ()).throw(AssertionError("network")))
    fake = runner.CASE.ScriptedModel(messages=iter([
        runner.CASE.call("transfer_to_Mythic_Operator", {
            "handoff_title": "identity", "handoff_instruction": "Run whoami on callback 7 and report the output."}, 1),
        runner.CASE.call("issue_task_and_waitfor_task_output", {
            "command": "whoami", "parameters": "", "callback_display_id": 7}, 2),
        runner.CASE.call("get_task_history_for_callback", {"callback_display_id": 7}, 3),
        runner.CASE.call("get_all_task_output_by_task_id", {"task_id": 701}, 4),
        AIMessage(content="Task 701 returned an identity."),
        AIMessage(content="Task 701 returned an identity."),
    ]))
    monkeypatch.setattr(runner.model_module, "init_chat_model", lambda *a, **kw: fake)
    profile = {"provider": "openai", "model": "synthetic-test", "API_ENDPOINT": "https://127.0.0.1:7443/v1", "API_KEY": "offline"}
    limits = type("Limits", (), {"max_case_calls": 6, "max_calls": 20, "max_case_tokens": 10000,
        "max_tokens": 200000, "output_tokens": 512, "request_seconds": 5, "max_wall": 30})()
    result = asyncio.run(runner.run_case("04_whoami_accept", "Run whoami on callback 7 and report the output.",
        "accept", profile, tmp_path, limits, None, {"127.0.0.1"}))
    assert result["status"] == "observed", result.get("error")
    history = json.loads(next(event["result"] for event in result["tool_events"]
                              if event["tool"] == "get_task_history_for_callback"))
    assert len(history) == 1
    assert history[0]["display_id"] == result["effects"][0]["task_id"] == 701
    assert history[0]["request_provenance"]["origin"] == "current_request"
    output = json.loads(next(event["result"] for event in result["tool_events"]
                             if event["tool"] == "get_all_task_output_by_task_id"))
    assert output["provenance"]["origin"] == "current_request"
    assert output["task_output"][0]["task"]["callback"] == {"display_id": 7}
    assert output["task_output"][0]["response_text"].endswith("LAB\\analyst")


def test_cli_selected_cases_and_resume_identity(tmp_path, monkeypatch, capsys):
    runner = module()
    endpoint = "https://127.0.0.1:7443/v1"
    profile = tmp_path / "profile"
    profile.write_text(f"provider=openai\nmodel=synthetic-test\nAPI_ENDPOINT={endpoint}\nAPI_KEY=offline\n")
    output = tmp_path / "out"
    assert runner.main(["--config", str(profile), "--output-dir", str(output),
                        "--cases", "05_enumerate_accept,05_enumerate_reject"]) == 0
    ready = json.loads(capsys.readouterr().out)
    assert ready["selected_cases"] == ["05_enumerate_accept", "05_enumerate_reject"]
    assert "09_after_enumeration" in ready["pending"] and "10_recommend" in ready["pending"]
    assert runner.main(["--config", str(profile), "--output-dir", str(output),
                        "--cases", "09_after_enumeration"]) == 2
    assert json.loads(capsys.readouterr().out)["reason"] == "unknown or non-parent case ID"
    monkeypatch.setattr(runner.REAL, "sage_tls_context", lambda: None)
    monkeypatch.setattr(runner.model_module, "init_chat_model", lambda *a, **kw:
        runner.CASE.ScriptedModel(messages=iter([AIMessage(content="No action.")])))
    monkeypatch.setattr(socket.socket, "connect", lambda *_: (_ for _ in ()).throw(AssertionError("network")))
    monkeypatch.setattr(runner, "totals", lambda _path: (0, 0, False))
    args = ["--run", "--config", str(profile), "--output-dir", str(output),
            "--expected-model", "synthetic-test", "--expected-endpoint-sha256",
            hashlib.sha256(endpoint.encode()).hexdigest(), "--cases", "06_explain"]
    runner.main(args)
    capsys.readouterr()
    assert (output / "case_06_explain.json").exists()
    assert not (output / "case_05_enumerate_accept.json").exists()
    assert json.loads((output / "budget.json").read_text())["selected_cases"] == ["06_explain"]
    assert runner.main(args[:-1] + ["03_commands"]) == 2
    assert json.loads(capsys.readouterr().out)["reason"] == "resume identity mismatch"


def test_supplemental_clarification_cases_require_explicit_selection(tmp_path, capsys):
    runner = module()
    profile = tmp_path / "profile"
    profile.write_text("provider=openai\nmodel=synthetic-test\nAPI_ENDPOINT=https://127.0.0.1:7443/v1\nAPI_KEY=offline\n")
    output = tmp_path / "out"
    assert runner.main(["--config", str(profile), "--output-dir", str(output)]) == 0
    default = json.loads(capsys.readouterr().out)
    assert "08_explicit_all" not in default["selected_cases"]
    assert "08_named_host" not in default["selected_cases"]
    assert "09_direct_evidence" not in default["selected_cases"]
    assert "09_stored_enumeration" not in default["selected_cases"]
    assert "08_target_resolved" in default["pending"]
    assert runner.main(["--config", str(profile), "--output-dir", str(output),
                        "--cases", "08_explicit_all,08_named_host"]) == 0
    selected = json.loads(capsys.readouterr().out)
    assert selected["selected_cases"] == ["08_explicit_all", "08_named_host"]
    assert selected["pending"] == selected["selected_cases"]
    assert runner.main(["--config", str(profile), "--output-dir", str(output),
                        "--cases", "09_stored_enumeration"]) == 0
    stored = json.loads(capsys.readouterr().out)
    assert stored["selected_cases"] == ["09_stored_enumeration"]
    assert stored["pending"] == ["09_stored_enumeration", "10_stored_recommend"]


def test_direct_evidence_control_reads_seeded_output_without_effect(tmp_path, monkeypatch):
    runner = module()
    from sage_chat import session
    monkeypatch.setattr(session, "_CHANNEL_MARKER_DB_PATH", str(tmp_path / "sage.db"))
    monkeypatch.setattr(socket.socket, "connect", lambda *_: (_ for _ in ()).throw(AssertionError("network")))
    monkeypatch.setattr(socket.socket, "connect_ex", lambda *_: (_ for _ in ()).throw(AssertionError("network")))
    supervisor_evidence = []
    def supervisor_after_worker(messages):
        supervisor_evidence.extend(message for message in messages if isinstance(message, AIMessage)
            and message.additional_kwargs.get("_worker_task_evidence"))
        return AIMessage(content="Task 811 shows LAB-WS01 is joined to example.test; trusts were not collected.")
    fake = runner.CASE.ScriptedModel(messages=iter([
        runner.CASE.call("transfer_to_Mythic_Operator", {
            "handoff_title": "read task 811",
            "handoff_instruction": "Read output of task 811 and report domain membership and trusts."}, 1),
        runner.CASE.call("get_all_task_output_by_task_id", {"task_id": 811}, 2),
        AIMessage(content="Task 811 shows LAB-WS01 is joined to example.test; trusts were not collected."),
        supervisor_after_worker,
    ]))
    monkeypatch.setattr(runner.model_module, "init_chat_model", lambda *a, **kw: fake)
    profile = {"provider": "openai", "model": "synthetic-test", "API_ENDPOINT": "https://127.0.0.1:7443/v1", "API_KEY": "offline"}
    limits = type("Limits", (), {"max_case_calls": 6, "max_calls": 20, "max_case_tokens": 10000,
        "max_tokens": 200000, "output_tokens": 512, "request_seconds": 5, "max_wall": 30})()
    result = asyncio.run(runner.run_case("09_direct_evidence",
        "Read the output of task 811 and report what it establishes about LAB-WS01 domain membership and domain trusts. Do not execute anything.",
        None, profile, tmp_path, limits, None, {"127.0.0.1"}))
    assert result["status"] == "observed", result.get("error")
    assert result["prompt_sha256"] == {
        name: hashlib.sha256((runner.SAGE / "prompts" / name).read_bytes()).hexdigest()
        for name in ("supervisor.md", "mythic_operator.md")}
    assert result["seeded_observations"][0]["source"] == "synthetic historical task output"
    reads = [event for event in result["tool_events"] if event["tool"] == "get_all_task_output_by_task_id"]
    assert len(reads) == 1 and reads[0]["arguments"] == {"task_id": 811}
    payload = json.loads(reads[0]["result"])
    assert payload["provenance"]["task_id"] == 811
    assert payload["provenance"]["origin"] == "historical_or_external"
    assert "PartOfDomain: True" in payload["task_output"][0]["response_text"]
    assert "Trust inventory: not collected" in payload["task_output"][0]["response_text"]
    assert result["initial_cards"] == result["effects"] == []
    assert len(supervisor_evidence) == 1
    assert '"task_id": 811' in supervisor_evidence[0].content
    assert "PartOfDomain: True" in supervisor_evidence[0].content
    assert any("example.test" in str(emission.get("content")) and "trusts were not collected" in str(emission.get("content"))
               for emission in result["turns"][0]["emissions"] if emission.get("complete_request"))


def test_stored_enumeration_reads_prior_tasks_then_recommends_without_effect(tmp_path, monkeypatch):
    runner = module()
    from sage_chat import session
    monkeypatch.setattr(session, "_CHANNEL_MARKER_DB_PATH", str(tmp_path / "sage.db"))
    monkeypatch.setattr(socket.socket, "connect", lambda *_: (_ for _ in ()).throw(AssertionError("network")))
    monkeypatch.setattr(socket.socket, "connect_ex", lambda *_: (_ for _ in ()).throw(AssertionError("network")))
    prompt = next(text for case_id, text, _ in runner.SUPPLEMENTAL_PROMPTS if case_id == "09_stored_enumeration")
    task_reads = AIMessage(content="", tool_calls=[
        {"name": "get_all_task_output_by_task_id", "args": {"task_id": task_id},
         "id": f"task_{task_id}", "type": "tool_call"}
        for task_id in (701, 702, 703)])
    supervisor_evidence = []
    def supervisor_after_worker(messages):
        supervisor_evidence.extend(message for message in messages if isinstance(message, AIMessage)
            and message.additional_kwargs.get("_worker_task_evidence"))
        return AIMessage(content="Task 701 shows LAB\\analyst; 702 shows two processes; 703 lists local accounts. Trusts are unknown.")
    fake = runner.CASE.ScriptedModel(messages=iter([
        runner.CASE.call("transfer_to_Mythic_Operator", {
            "handoff_title": "review existing evidence", "handoff_instruction": prompt}, 1),
        runner.CASE.call("list_callbacks", {}, 2),
        runner.CASE.call("get_task_history_for_callback", {"callback_display_id": 7}, 3),
        task_reads,
        AIMessage(content="Tasks 701, 702, and 703 show current user, processes, and local accounts."),
        supervisor_after_worker,
        runner.CASE.call("transfer_to_Mythic_Operator", {
            "handoff_title": "check credential inventory",
            "handoff_instruction": "Read the operation credential inventory; recommend a next step without execution."}, 5),
        runner.CASE.call("read_credentials", {"realm": "", "account": ""}, 6),
        AIMessage(content="The Mythic credential inventory is empty. Recommend an evidence-gathering step."),
        AIMessage(content="The credential store is empty. Next, verify domain context through an approved callback 7 task. I have not executed it."),
    ]))
    monkeypatch.setattr(runner.model_module, "init_chat_model", lambda *a, **kw: fake)
    profile = {"provider": "openai", "model": "synthetic-test", "API_ENDPOINT": "https://127.0.0.1:7443/v1", "API_KEY": "offline"}
    limits = type("Limits", (), {"max_case_calls": 8, "max_calls": 20, "max_case_tokens": 10000,
        "max_tokens": 200000, "output_tokens": 512, "request_seconds": 5, "max_wall": 30})()
    result = asyncio.run(runner.run_case("09_stored_enumeration", prompt,
        None, profile, tmp_path, limits, None, {"127.0.0.1"}))
    followup = json.loads((tmp_path / "case_10_stored_recommend.json").read_text())
    assert result["status"] == followup["status"] == "observed", result.get("error")
    assert {row["task_id"] for row in result["seeded_observations"]} == {701, 702, 703}
    assert all(row["source"] == "synthetic historical task output" for row in result["seeded_observations"])
    history = json.loads(next(event["result"] for event in result["tool_events"]
                              if event["tool"] == "get_task_history_for_callback"))
    assert {row["display_id"] for row in history} == {701, 702, 703}
    assert {row["request_provenance"]["origin"] for row in history} == {"historical_or_external"}
    reads = [event for event in result["tool_events"] if event["tool"] == "get_all_task_output_by_task_id"]
    assert {event["arguments"]["task_id"] for event in reads} == {701, 702, 703}
    for event in reads:
        payload = json.loads(event["result"])
        task_id = event["arguments"]["task_id"]
        assert payload["provenance"]["task_id"] == task_id
        assert payload["provenance"]["origin"] == "historical_or_external"
        assert payload["task_output"][0]["task"]["display_id"] == task_id
        assert payload["task_output"][0]["task"]["callback"] == {"display_id": 7}
    assert result["initial_cards"] == result["effects"] == followup["effects"] == followup["approval_cards"] == []
    assert len(supervisor_evidence) == 1
    assert all(f'"task_id": {task_id}' in supervisor_evidence[0].content for task_id in (701, 702, 703))
    assert "LAB\\\\analyst" in supervisor_evidence[0].content
    assert "Trust inventory" not in supervisor_evidence[0].content
    assert result["synthetic_credential_inventory"] == {
        "source": "empty synthetic Mythic credential store", "entry_count": 0}
    credential_reads = [event for event in followup["tool_events"] if event["tool"] == "read_credentials"]
    assert len(credential_reads) == 1
    assert credential_reads[0]["arguments"] == {"realm": "", "account": ""}
    assert credential_reads[0]["result"] == "No credentials in the Mythic store (matching the given filters)."
    assert any("Next, verify domain context" in str(emission.get("content"))
               for emission in followup["emissions"] if emission.get("complete_request"))


@pytest.mark.parametrize(("case_id", "prompt", "callback_id"), [
    ("08_explicit_all", "Enumerate the host, processes, and users on both callbacks 7 and 8.", 7),
    ("08_named_host", "Enumerate the host, processes, and users on LAB-WS02.", 8),
])
def test_explicit_scope_controls_reach_proposal_without_execution(
        tmp_path, monkeypatch, case_id, prompt, callback_id):
    runner = module()
    from sage_chat import session
    monkeypatch.setattr(session, "_CHANNEL_MARKER_DB_PATH", str(tmp_path / "sage.db"))
    monkeypatch.setattr(socket.socket, "connect", lambda *_: (_ for _ in ()).throw(AssertionError("network")))
    monkeypatch.setattr(socket.socket, "connect_ex", lambda *_: (_ for _ in ()).throw(AssertionError("network")))
    fake = runner.CASE.ScriptedModel(messages=iter([
        runner.CASE.call("transfer_to_Mythic_Operator", {
            "handoff_title": "enumerate selected callback",
            "handoff_instruction": prompt}, 1),
        runner.CASE.call("issue_task_and_waitfor_task_output", {
            "command": "whoami", "parameters": "", "callback_display_id": callback_id}, 2),
    ]))
    monkeypatch.setattr(runner.model_module, "init_chat_model", lambda *a, **kw: fake)
    profile = {"provider": "openai", "model": "synthetic-test", "API_ENDPOINT": "https://127.0.0.1:7443/v1", "API_KEY": "offline"}
    limits = type("Limits", (), {"max_case_calls": 6, "max_calls": 20, "max_case_tokens": 10000,
        "max_tokens": 200000, "output_tokens": 512, "request_seconds": 5, "max_wall": 30})()
    result = asyncio.run(runner.run_case(case_id, prompt, None, profile,
        tmp_path, limits, None, {"127.0.0.1"}))
    assert result["status"] == "observed", result.get("error")
    actions = runner.card_actions(result["initial_cards"])
    assert len(actions) == 1
    assert actions[0]["arguments"]["callback_display_id"] == callback_id
    assert result["effects"] == result["pre_decision_effects"] == []


def test_ambiguous_followup_reaches_scoped_card_without_automatic_approval(tmp_path, monkeypatch):
    runner = module()
    from sage_chat import session
    monkeypatch.setattr(session, "_CHANNEL_MARKER_DB_PATH", str(tmp_path / "sage.db"))
    monkeypatch.setattr(socket.socket, "connect", lambda *_: (_ for _ in ()).throw(AssertionError("network")))
    monkeypatch.setattr(socket.socket, "connect_ex", lambda *_: (_ for _ in ()).throw(AssertionError("network")))
    fake = runner.CASE.ScriptedModel(messages=iter([
        AIMessage(content="Callbacks 7 and 8 are active. Which callback should I use?"),
        runner.CASE.call("transfer_to_Mythic_Operator", {
            "handoff_title": "enumerate callback 7",
            "handoff_instruction": "Use callback 7 only to enumerate the host, processes, and users."}, 1),
        runner.CASE.call("issue_task_and_waitfor_task_output", {
            "command": "whoami", "parameters": "", "callback_display_id": 7}, 2),
    ]))
    monkeypatch.setattr(runner.model_module, "init_chat_model", lambda *a, **kw: fake)
    profile = {"provider": "openai", "model": "synthetic-test", "API_ENDPOINT": "https://127.0.0.1:7443/v1", "API_KEY": "offline"}
    limits = type("Limits", (), {"max_case_calls": 6, "max_calls": 20, "max_case_tokens": 10000,
        "max_tokens": 200000, "output_tokens": 512, "request_seconds": 5, "max_wall": 30})()
    result = asyncio.run(runner.run_case("08_ambiguous", "Enumerate the host, processes, and users.",
        None, profile, tmp_path, limits, None, {"127.0.0.1"}))
    followup = json.loads((tmp_path / "case_08_target_resolved.json").read_text())
    assert result["status"] == followup["status"] == "observed", result.get("error")
    assert result["initial_cards"] == result["effects"] == []
    actions = runner.card_actions(followup["approval_cards"])
    assert len(actions) == 1
    assert actions[0]["arguments"]["callback_display_id"] == 7
    assert followup["effects"] == []
    assert result["effects"] == []


def test_ambiguous_card_does_not_trigger_resolution_followup(tmp_path, monkeypatch):
    runner = module()
    from sage_chat import session
    monkeypatch.setattr(session, "_CHANNEL_MARKER_DB_PATH", str(tmp_path / "sage.db"))
    monkeypatch.setattr(socket.socket, "connect", lambda *_: (_ for _ in ()).throw(AssertionError("network")))
    monkeypatch.setattr(socket.socket, "connect_ex", lambda *_: (_ for _ in ()).throw(AssertionError("network")))
    fake = runner.CASE.ScriptedModel(messages=iter([
        runner.CASE.call("transfer_to_Mythic_Operator", {
            "handoff_title": "enumerate", "handoff_instruction": "Enumerate the host, processes, and users."}, 1),
        runner.CASE.call("issue_task_and_waitfor_task_output", {
            "command": "whoami", "parameters": "", "callback_display_id": 7}, 2),
    ]))
    monkeypatch.setattr(runner.model_module, "init_chat_model", lambda *a, **kw: fake)
    profile = {"provider": "openai", "model": "synthetic-test", "API_ENDPOINT": "https://127.0.0.1:7443/v1", "API_KEY": "offline"}
    limits = type("Limits", (), {"max_case_calls": 6, "max_calls": 20, "max_case_tokens": 10000,
        "max_tokens": 200000, "output_tokens": 512, "request_seconds": 5, "max_wall": 30})()
    result = asyncio.run(runner.run_case("08_ambiguous", "Enumerate the host, processes, and users.",
        None, profile, tmp_path, limits, None, {"127.0.0.1"}))
    followup = json.loads((tmp_path / "case_08_target_resolved.json").read_text())
    assert result["status"] == "observed", result.get("error")
    assert runner.card_actions(result["initial_cards"])[0]["arguments"]["callback_display_id"] == 7
    assert followup["status"] == "pending_prerequisite"
    assert result["effects"] == []


def test_unknown_apollo_command_schema_is_a_recoverable_tool_result(tmp_path, monkeypatch):
    runner = module()
    from sage_chat import session
    monkeypatch.setattr(session, "_CHANNEL_MARKER_DB_PATH", str(tmp_path / "sage.db"))
    monkeypatch.setattr(socket.socket, "connect", lambda *_: (_ for _ in ()).throw(AssertionError("network")))
    monkeypatch.setattr(socket.socket, "connect_ex", lambda *_: (_ for _ in ()).throw(AssertionError("network")))
    schema_calls = AIMessage(content="", tool_calls=[
        {"name": "get_all_command_args_for_payloadtype", "args": {"payload": "apollo", "command": command},
         "id": f"schema_{command}", "type": "tool_call"}
        for command in ("getprivs", "hostname")])
    fake = runner.CASE.ScriptedModel(messages=iter([
        runner.CASE.call("transfer_to_Mythic_Operator", {
            "handoff_title": "enumerate", "handoff_instruction": "enumerate host, processes, and users on callback 7"}, 1),
        schema_calls,
        AIMessage(content="Those two commands are absent from the fixture catalog."),
        AIMessage(content="The queried commands were unavailable; no task ran."),
    ]))
    monkeypatch.setattr(runner.model_module, "init_chat_model", lambda *a, **kw: fake)
    profile = {"provider": "openai", "model": "synthetic-test", "API_ENDPOINT": "https://127.0.0.1:7443/v1", "API_KEY": "offline"}
    limits = type("Limits", (), {"max_case_calls": 6, "max_calls": 20, "max_case_tokens": 10000,
        "max_tokens": 200000, "output_tokens": 512, "request_seconds": 5, "max_wall": 30})()
    result = asyncio.run(runner.run_case("05_enumerate_reject", "enumerate host, processes, and users on callback 7",
        "reject", profile, tmp_path, limits, None, {"127.0.0.1"}))
    assert result["status"] == "observed", result.get("error")
    actual = {event["arguments"]["command"]: event["result"] for event in result["tool_events"]}
    assert set(actual) == {"getprivs", "hostname"}
    for command, response in actual.items():
        assert response == (f"Error: no parameter schema found for command '{command}' on payload type "
                            "'apollo'. Check the command name with get_all_command_names_for_payloadtype.")
    assert not result["effects"] and not result.get("harness_fault")
