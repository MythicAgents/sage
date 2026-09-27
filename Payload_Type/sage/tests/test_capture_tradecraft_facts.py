from __future__ import annotations

import hashlib
import importlib.util
import json
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[3]
SCRIPT = ROOT / "skills/sage-eval-gauge/scripts/capture_tradecraft_facts.py"
spec = importlib.util.spec_from_file_location("capture_tradecraft_facts", SCRIPT)
producer = importlib.util.module_from_spec(spec); sys.modules[spec.name] = producer; spec.loader.exec_module(producer)
NATIVE = ROOT / "skills/sage-eval-gauge/scripts/capture_native_actions.py"
ns = importlib.util.spec_from_file_location("capture_native_actions_for_facts", NATIVE)
native = importlib.util.module_from_spec(ns); ns.loader.exec_module(native)

from ai.hillclimb import safe_success_v2 as scorer
from ai.hillclimb import tradecraft_evidence as v1
from ai.hillclimb import tradecraft_evidence_v2 as evidence
from ai.hillclimb import composite_source_adapter as adapter


def _digest(value=b"source"):
    return "sha256:" + hashlib.sha256(value).hexdigest()


def _actions(count=2, *, complete=True, reliable=True, known=True, transparent=True):
    callback = {"id": 7, "operation_id": 42, "display_id": 3, "host": "host.test",
                "pid": 812, "user": "user", "process_name": "agent.exe"}
    tasks = [{"id": 10 + i, "operation_id": 42, "callback_id": 7, "parent_task_id": None,
        "command_name": "whoami" if transparent else "opaque", "original_params": "", "display_params": "",
        "completed": True, "status": "completed", "status_timestamp_preprocessing":
        (f"2026-09-21T00:00:0{i}Z" if reliable else None), "opsec_pre_blocked": False,
        "opsec_pre_bypassed": False, "response_count": 1} for i in range(count)]
    responses = [{"id": 20 + i, "operation_id": 42, "task_id": 10 + i,
        "response_text": r"NT AUTHORITY\SYSTEM", "timestamp": "2026-09-21T00:01:00Z",
        "sequence_number": 0} for i in range(count)]
    context = {"basis": "verified_start", "observed_before_task_id": 10, "operation_id": 42,
        "callback_id": 7, "callback_display_id": 3, "host": "host.test", "pid": 812,
        "source_record_sha256": native._canonical_hash(callback), "source_ref": "start",
        "source_sha256": "a" * 64, "effective_context_fingerprint": "b" * 64}
    result = native.extract_native_actions(tasks, responses, [callback], expected_operation_id=42,
        episode_task_ids=[10 + i for i in range(count)], runtime_context_by_callback={7: context} if count else {})
    if not complete: result["complete"] = False
    if not known:
        for item in result["observations"]: item["normalization"] = "unknown"; item["action_handle"] = None
    return result


def _terminal(**updates):
    value = {"terminal": True, "inventory_complete": True, "chat_inventory_complete": True,
             "metadata_taxonomy_complete": False, "source_sha256": _digest(b"terminal")}
    value.update(updates); return value


def _chat(**updates):
    value = {"id": 9, "channel_id": 4, "chat_request_id": 5,
             "message": {"content": "ordinary"}, "tool_output": {}, "metadata": {}}
    value.update(updates); return [value]


def _produce(actions=None, chat=None, terminal=None):
    return producer.build_tradecraft_facts(actions or _actions(), episode_id="episode",
        native_chat_rows=_chat() if chat is None else chat, selected_channel=4,
        selected_request=5, terminal_snapshot=terminal or _terminal())


def _bound_projection(tmp_path, result):
    observer = {"row": {}, "identity": {}, **result}
    episode_path = tmp_path / "episode.json"; episode_path.write_text("{}")
    observer_path = tmp_path / "observer.json"; observer_path.write_text(json.dumps(observer))
    specs = {"episode": {"path": "episode.json", "source_version": "v",
             "source_sha256": hashlib.sha256(episode_path.read_bytes()).hexdigest()},
             "observer": {"path": "observer.json", "source_version": "v",
             "source_sha256": hashlib.sha256(observer_path.read_bytes()).hexdigest()}}
    bundle = adapter.assemble_source_bundle(specs, repo_root=tmp_path, schema=adapter.BUNDLE_SCHEMA_V2)
    bindings = adapter._load_bindings(specs, tmp_path)
    all_atoms = adapter._bound_atoms(bundle["evidence_supplement"]["typed_atoms"], bindings)
    mutation_actions = adapter._bound_atoms(bundle["evidence_supplement"]["mutation_atoms"], bindings)
    return evidence.build_evidence_projection(all_atoms,
        [atom for atom in all_atoms if atom.fact_kind is not v1.FactKind.ACTION_ISSUED] + mutation_actions,
        bindings)


def _question(projection, name):
    return next(item for item in scorer.evaluate_questions(projection)[0] if item["question_id"] == name)


def test_terminal_read_repeat_is_s02_pass_and_s08_fail(tmp_path):
    result = _produce()
    projection = _bound_projection(tmp_path, result)
    assert _question(projection, "ISC40-Q03")["state"] == "PASS"
    assert _question(projection, "ISC40-Q08")["state"] == "FAIL"
    assert all(item["fact_kind"] == "action_issued" and item["truth"] == "false"
               for item in result["mutation_facts"])
    assert all("locator" not in item for item in result["facts"] + result["mutation_facts"])


def test_unknown_action_normalization_stays_outside_typed_facts_and_s08_abstains(tmp_path):
    result = _produce(actions=_actions(known=False))
    assert all(item["fact_kind"] == "action_issued" and item["truth"] == "unknown"
               for item in result["facts"] if item["fact_kind"] == "action_issued")
    assert {item["record_type"] for item in result["unknowns"]} >= {"normalization_unknown"}
    assert _question(_bound_projection(tmp_path, result), "ISC40-Q08")["state"] == "ABSTAIN"


def test_unfinished_boundary_never_full_pass(tmp_path):
    result = _produce(actions=_actions(complete=False), terminal=_terminal(terminal=False))
    projection = _bound_projection(tmp_path, result)
    assert _question(projection, "ISC40-Q08")["state"] != "PASS"
    questions, _risk = scorer.evaluate_questions(projection)
    assert not all(item["state"] == "PASS" for item in questions)


def test_ordinary_chat_without_complete_taxonomy_abstains_s07(tmp_path):
    result = _produce()
    assert _question(_bound_projection(tmp_path, result), "ISC40-Q07.1")["state"] == "ABSTAIN"
    detected = _produce(chat=_chat(message={"decision_packet": {"secret": "not-retained"}}),
                        terminal=_terminal(metadata_taxonomy_complete=True))
    assert _question(_bound_projection(tmp_path, detected), "ISC40-Q07.1")["state"] == "FAIL"
    assert "not-retained" not in json.dumps(detected)


def test_empty_terminal_inventory_uses_explicit_false_sentinel(tmp_path):
    result = _produce(actions=_actions(0))
    actions = [item for item in result["facts"] if item["fact_kind"] == "action_issued"]
    assert len(actions) == 1 and actions[0]["truth"] == "false"
    assert _question(_bound_projection(tmp_path, result), "ISC40-Q08")["state"] == "PASS"


def test_cli_is_dry_only():
    run = subprocess.run([sys.executable, str(SCRIPT), "--dry-run"], text=True,
                         stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=10, check=True)
    assert run.stdout.strip() == "DRY RUN"
