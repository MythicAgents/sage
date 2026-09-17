"""Historical replay never invents raw response metadata or rewrites evidence."""
from copy import deepcopy
import importlib.util
from pathlib import Path

import pytest

from ai.hillclimb.trust_walker_outcome import classify_terminal_snapshot

FIXTURE_SPEC = importlib.util.spec_from_file_location(
    "reanalysis_outcome_fixtures", Path(__file__).with_name("test_trust_walker_outcome.py"),
)
fixtures = importlib.util.module_from_spec(FIXTURE_SPEC)
FIXTURE_SPEC.loader.exec_module(fixtures)
CERT_PROBE_SOURCE, CERT_PROOF_OUTPUT, snapshot = (
    fixtures.CERT_PROBE_SOURCE, fixtures.CERT_PROOF_OUTPUT, fixtures.snapshot,
)

SCRIPT = Path(__file__).resolve().parents[3] / "skills/sage-eval-gauge/scripts/reanalyze_trust_walker.py"
SPEC = importlib.util.spec_from_file_location("trust_walker_reanalysis", SCRIPT)
replay = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(replay)


def retained_arm():
    output = CERT_PROOF_OUTPUT.replace(r"u:NORTH\samwell.tarly", r"u:NORTH\Administrator")
    value = snapshot(tasks=[{
        "operation_id": 8, "display_id": 54, "command_name": "powerpick", "completed": True,
        "created_at": "2026-09-13T12:20:00+00:00",
        "first_response_at": "2026-09-13T12:20:30+00:00",
        "last_update_at": "2026-09-13T12:21:00+00:00",
    }], task_accounting={"limit": 100, "reservations": 1, "confirmed_task_ids": [54]},
       intermediate_milestones={"OBJECTIVE": {"disposition": "disproven"}})
    result = {
        "arm": "candidate-sage", "record_type": "arm_result", "terminal_snapshot": value,
        "outcome": classify_terminal_snapshot(value).to_record(), "execution": {"cost": {"total_tokens": 10}},
    }
    start = {"runtime_identity": {"operation_id": 8, "callback_id": 2}}
    card = {
        "tool_source": "mythic", "status": "completed", "output_available": True,
        "output_size": len(output), "result_preview": output,
        "arguments": replay.json.dumps({"task_id": 54, "callback_id": 2, "parameters": CERT_PROBE_SOURCE}),
    }
    transcript = {"chat_channel_id": 7, "chat_request_id": 9, "messages": [{
        "id": 11, "channel_id": 7, "chat_request_id": 9, "deleted": False, "edited": False,
        "created_at": "2026-09-13T12:19:59", "updated_at": "2026-09-13T12:21:01",
        "metadata": {"tool_use": card},
    }]}
    return result, start, transcript


def test_complete_native_artifact_proves_by_deadline_without_invented_response_metadata():
    inputs = retained_arm()
    originals = deepcopy(inputs)
    result, detail = replay.reconstruct_arm(*inputs)
    assert inputs == originals
    assert result["outcome"]["disposition"] == "proven"
    candidate = result["terminal_snapshot"]["proof_candidates"][0]
    assert candidate["effect_at"] == "2026-09-13T12:21:01"
    assert candidate["effect_time_basis"] == "native_chat_artifact_upper_bound"
    assert "response_id" not in candidate
    assert detail["proof_provenance"][0]["exact_first_proof_response_at"] is None
    assert detail["reconstruction_gaps"] == []


@pytest.mark.parametrize("mutation", ["truncated", "missing_time", "late_bound", "missing_card"])
def test_incomplete_or_late_artifact_stays_unknown_not_a_new_negative(mutation):
    result, start, transcript = retained_arm()
    message = transcript["messages"][0]
    if mutation == "truncated":
        message["metadata"]["tool_use"]["output_size"] += 1
    elif mutation == "missing_time":
        message.pop("updated_at")
    elif mutation == "late_bound":
        message["updated_at"] = "2026-09-13T12:45:01"
    else:
        transcript["messages"] = []
    derived, detail = replay.reconstruct_arm(result, start, transcript)
    assert derived["outcome"]["disposition"] == "unmeasured"
    assert detail["reconstruction_gaps"]


@pytest.mark.parametrize("mutation", ["operation", "callback", "channel", "request", "edited", "duplicate"])
def test_reanalysis_rejects_wrong_lineage_or_edited_card(mutation):
    result, start, transcript = retained_arm()
    message = transcript["messages"][0]
    if mutation == "operation":
        start["runtime_identity"]["operation_id"] = 99
    elif mutation == "callback":
        start["runtime_identity"]["callback_id"] = 99
    elif mutation == "channel":
        message["channel_id"] = 99
    elif mutation == "request":
        message["chat_request_id"] = 99
    elif mutation == "edited":
        message["edited"] = True
    else:
        transcript["messages"].append(deepcopy(message))
    with pytest.raises(ValueError, match="binding|lineage|duplicate"):
        replay.reconstruct_arm(result, start, transcript)


def test_artifact_replay_still_rejects_same_principal_negative_control():
    result, start, transcript = retained_arm()
    card = transcript["messages"][0]["metadata"]["tool_use"]
    card["result_preview"] = card["result_preview"].replace(r"u:NORTH\Administrator", r"u:ESSOS\Administrator")
    card["output_size"] = len(card["result_preview"])
    derived, detail = replay.reconstruct_arm(result, start, transcript)
    assert derived["outcome"]["disposition"] == "disproven"
    assert "identical" in derived["outcome"]["rejected_proofs"][0]["reason"]
    assert detail["proof_provenance"] == []
