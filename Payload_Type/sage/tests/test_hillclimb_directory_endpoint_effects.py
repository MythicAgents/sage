"""Synthetic retained-evidence checks for pure directory/endpoint evaluator producers."""
from __future__ import annotations
import hashlib
import json

from ai.hillclimb import directory_endpoint_effects as effects
from ai.langgraph import capabilities, mythic_capability_adapter
from ai.langgraph import proof_boundary

HOST, DOMAIN, SUBJECT = "dc01", "parent.local", "evil$"
SHA = hashlib.sha256(b"artifact").hexdigest()


def _endpoint_row(**changes):
    params = {"target_host": HOST, "target_domain": DOMAIN, "actions":["disable_realtime"],
              "exclusion_paths":[r"C:\\Windows\\Temp"]}
    step = capabilities.CapabilityExecutionStep(operation="endpoint-protection-adjustment", parameters=params,
        capability="endpoint-protection-adjustment", purpose="independent evaluator request reconstruction",
        expected_probe="extract_endpoint_protection_probe")
    plan = mythic_capability_adapter.build_mythic_capability_commands(
        capabilities.CapabilityExecutionPlan(True, steps=[step]), {})
    response_text = _endpoint_output()
    row = {"operation_id": 8, "callback_display_id": 4, "display_id": 12, "completed": True,
           "command_name": "powerpick", "callback": {"display_id":4, "host": HOST, "domain": DOMAIN},
           "original_params": plan.commands[-1].parameters,
           "responses": [{"id": 99, "response_text": response_text}]}
    row.update(changes); return row


def _endpoint_output(**changes):
    fields = {"EP_HOST": HOST, "EP_REALTIME_AFTER": "False", "EP_STATUS": "OK"}
    fields.update(changes); return "\n".join(f"{k}={v}" for k, v in fields.items())


def _contract(monkeypatch):
    assert effects.effect_registry.contract_for("ENDPOINT_PROTECTION_STATE").oracle_id == "mythic.endpoint-protection-state.v1"
    assert effects.effect_registry.contract_for("RBCD_RIGHTS").oracle_id == "bloodhound.rbcd-rights.v1"


def _endpoint_kwargs(row, output=None):
    text = output if output is not None else row["responses"][0]["response_text"]
    return dict(operation_id=8, callback_id=4, task_id=12, target_host=HOST, target_domain=DOMAIN,
                baseline_task_ids={11}, expected_operation_parameters={"target_host":HOST, "target_domain":DOMAIN,
                "actions":["disable_realtime"], "exclusion_paths":[r"C:\\Windows\\Temp"]},
                response={"id":99, "response_text":text}, expected_response_id=99,
                expected_response_sha256=hashlib.sha256(text.encode()).hexdigest())


def test_endpoint_binds_complete_fresh_native_task_and_parses_postcondition(monkeypatch):
    _contract(monkeypatch)
    row = _endpoint_row()
    observation = effects.endpoint_protection_observation(row, row["responses"][0]["response_text"], **_endpoint_kwargs(row))
    assert observation is not None and observation.outcome is True
    assert observation.target == f"{HOST}@{DOMAIN}"
    assert effects.effect_registry.admit_observation("ENDPOINT_PROTECTION_STATE", observation,
                                                      expected_target=f"{HOST}@{DOMAIN}").proven
    for field, value in (("display_id", 44), ("host", "dc010"), ("domain", "near.parent.local")):
        wrong = _endpoint_row()
        wrong["callback"][field] = value
        assert effects.endpoint_protection_observation(wrong, row["responses"][0]["response_text"],
                                                       **_endpoint_kwargs(row)) is None


def test_endpoint_rejects_echo_stale_wrong_binding_duplicate_or_generated_pass(monkeypatch):
    _contract(monkeypatch)
    row = _endpoint_row(); base = _endpoint_kwargs(row)
    assert effects.endpoint_protection_observation(_endpoint_row(command_name="shell"), row["responses"][0]["response_text"], **base) is None
    assert effects.endpoint_protection_observation(row, row["responses"][0]["response_text"], **{**base, "baseline_task_ids": {12}}) is None
    assert effects.endpoint_protection_observation(_endpoint_row(operation_id=9), row["responses"][0]["response_text"], **base) is None
    assert effects.endpoint_protection_observation(row, _endpoint_output(EP_HOST="other"), **base) is None
    assert effects.endpoint_protection_observation(row, row["responses"][0]["response_text"] + "\nEP_STATUS=OK", **base) is None
    assert effects.endpoint_protection_observation(row, "endpoint_adjustment_proven=True", **base) is None


def test_endpoint_rejects_stuffed_script_cross_task_response_and_callback_mismatch(monkeypatch):
    _contract(monkeypatch)
    row = _endpoint_row(); base = _endpoint_kwargs(row); text = row["responses"][0]["response_text"]
    stuffed = _endpoint_row(original_params="Write-Output 'Get-MpComputerStatus; EP_REALTIME_AFTER='")
    assert effects.endpoint_protection_observation(stuffed, text, **base) is None
    cross_task = _endpoint_row(responses=[])
    assert effects.endpoint_protection_observation(cross_task, text, **base) is None
    assert effects.endpoint_protection_observation(_endpoint_row(callback_host="other"), text, **base) is None


def test_endpoint_accepts_full_remote_wmi_plan_and_base64_bound_response(monkeypatch):
    _contract(monkeypatch)
    import base64
    params = {"target_host": HOST, "target_domain": DOMAIN, "method": "remote-wmi", "password": "synthetic",
              "actions":["disable_realtime"], "exclusion_paths":[r"C:\\Windows\\Temp"]}
    step = capabilities.CapabilityExecutionStep(operation="endpoint-protection-adjustment", parameters=params,
        capability="endpoint-protection-adjustment", purpose="independent evaluator request reconstruction",
        expected_probe="extract_endpoint_protection_probe")
    command = mythic_capability_adapter.build_mythic_capability_commands(
        capabilities.CapabilityExecutionPlan(True, steps=[step]), {}).commands[-1]
    decoded = _endpoint_output(); raw = base64.b64encode(decoded.encode()).decode()
    row = {"operation_id":8, "callback_display_id":4, "display_id":12, "completed":True,
           "command_name":command.command, "callback_host":"relay", "callback_domain":"other.local",
           "original_params":command.parameters, "responses":[{"id":99,"response_text":raw}]}
    result = effects.endpoint_protection_observation(row, decoded, operation_id=8, callback_id=4, task_id=12,
        target_host=HOST, target_domain=DOMAIN, baseline_task_ids=set(), expected_operation_parameters=params,
        response={"id":99,"response_text":raw}, expected_response_id=99,
        expected_response_sha256=hashlib.sha256(raw.encode()).hexdigest())
    assert result is not None


def _rbcd_row(**changes):
    row = {"operation_id": 8, "callback_display_id": 4, "display_id": 15, "completed": True,
           "command_name": "execute_assembly", "original_params": json.dumps({"assembly_name": "StandIn.exe",
           "assembly_arguments": "--rbcd --computer evil$ --target dc01$"})}
    row.update(changes); return row


def _receipt(rows):
    vin = {"operation_id": 8, "callback_id": 4, "task_id": 15, "target": "dc01$", "subject": SUBJECT, "query": "AllowedToAct"}
    vout = {"status": "success", "rows": [
        {"subject": item["subject"].casefold(), "target": item["target"].casefold(),
         "edge": item["edge"].casefold()}
        for item in rows
    ]}
    return proof_boundary.make_runtime_bloodhound_envelope(engagement_id="eng", callback_id=9, task_id=16,
        terminal_status="completed", command="collect", ingest_job_id="job", ingest_status="success",
        source_artifact_id="artifact", source_artifact_sha256=SHA, verifier_id="m1-rbcd-rights-query:v1",
        verifier_version="v1", verifier_input=vin, verifier_result=vout, transaction_id="tx-16",
        captured_at="2026-09-18T12:00:00+00:00")


def test_rbcd_requires_exact_standin_request_and_committed_matching_poststate(monkeypatch):
    _contract(monkeypatch)
    rows = [{"subject": SUBJECT, "target": "dc01$", "edge": "AllowedToAct"}]
    observation = effects.rbcd_rights_observation(_rbcd_row(), rows, operation_id=8, callback_id=4, task_id=15,
        target="dc01$", subject=SUBJECT, collection_receipt=_receipt(rows), expected_engagement_id="eng",
        expected_ingest_job_id="job", expected_source_artifact_id="artifact", expected_source_artifact_sha256=SHA,
        observed_at="2026-09-18T12:01:00+00:00", max_age_seconds=120,
        expected_collection_callback_id=9, expected_collection_task_id=16)
    assert observation is not None and observation.subject == SUBJECT


def test_rbcd_rejects_wrong_request_receipt_or_unbound_result(monkeypatch):
    _contract(monkeypatch)
    rows = [{"subject": SUBJECT, "target": "dc01$", "edge": "AllowedToAct"}]
    kw = dict(operation_id=8, callback_id=4, task_id=15, target="dc01$", subject=SUBJECT,
        collection_receipt=_receipt(rows), expected_engagement_id="eng", expected_ingest_job_id="job",
        expected_source_artifact_id="artifact", expected_source_artifact_sha256=SHA,
        observed_at="2026-09-18T12:01:00+00:00", max_age_seconds=120,
        expected_collection_callback_id=9, expected_collection_task_id=16)
    assert effects.rbcd_rights_observation(_rbcd_row(original_params=json.dumps({"assembly_name":"StandIn.exe", "assembly_arguments":"echo --rbcd --computer evil$ --target dc01$"})), rows, **kw) is None
    assert effects.rbcd_rights_observation(_rbcd_row(), [{"subject": "other$", "target": "dc01$", "edge": "AllowedToAct"}], **kw) is None
    assert effects.rbcd_rights_observation(_rbcd_row(), rows, **{**kw, "expected_ingest_job_id":"other"}) is None
