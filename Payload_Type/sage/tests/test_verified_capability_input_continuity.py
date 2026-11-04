"""A proved capability input reaches the next capability; a guess never overrides it.

A CA private-key export that Sage proved through Mythic task output records a PFX artifact. The next
`adcs-certificate-auth` transaction must forge from that exact artifact, staged to the selected callback, even
when the proposal also carries a plausible but wrong `ca_pfx_path`. A verified artifact that fails one of its
checks stops before the forge and names the check. Without a verified export, a caller path stays an explicit
input that is never recorded as a proved export.

The terminal cases cover what the executor reports after its tasks ran: a fallback that the operator's approval
does not cover is refused before it issues anything, and that refusal must not erase the tasks that did run or
claim the approved action never ran.

Fixtures run the production executor, recorder, materializer and adapter; only the Mythic API seam is inert, and
every value is synthetic. Identifiers are generic (`lab.local`, `ca01`).
"""

from __future__ import annotations

import asyncio
import base64
import hashlib
import json
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "ai" / "langgraph"))
sys.path.insert(0, str(Path(__file__).resolve().parent))

import request_contract  # noqa: E402
from test_engagement_wiring import (  # noqa: E402
    _make_tools, _proof_hop, _split_issue, _write_test_ca_pfx,
)

CA_PASSWORD = "Synthetic CA pw 1"
STAGED = r"C:\Windows\Temp\sage_ca_signing_administrator_lab_local_13.pfx"
GUESSED = r"C:\Windows\Temp\sage_ca_export_ca01_13_backup\LAB-CA.p12"
CERT_AUTH = {
    "capability": "adcs-certificate-auth",
    "domain": "lab.local",
    "account": "administrator",
    "ca_host": "ca01",
    "callback_id": "13",
}
CERT_AUTH_OUTPUTS = [
    "No tickets in current context.",
    "Access is denied.",
    "Certify\nSaved forged certificate to 'C:\\Windows\\Temp\\sage_forged_cert_administrator_lab_local_13.pfx'.\n",
    "\n".join([
        "SAGE_CERT_AUTH_PROOF_administrator_lab_local_13",
        "CERT_AUTH_METHOD=schannel-ldap",
        "CERT_AUTH_DOMAIN=lab.local",
        "CERT_AUTH_ACCOUNT=administrator",
        "CERT_AUTH_LDAP_BIND=True",
        r"CERT_AUTH_WHOAMI=u:LAB\administrator",
        "DA_ONLY_READ_PROVEN=True",
        "CERT_AUTH_STATUS=OK",
    ]),
]


def _tools(monkeypatch, tmp_path):
    monkeypatch.setenv("SAGE_ENGAGEMENT_STATE_DIR", str(tmp_path / "engagement"))
    mt = _make_tools()
    mt._fetch_command_schema = lambda command, callback_display_id: asyncio.sleep(0, result=[])
    mt._validate_command_parameters = lambda command, parameters, callback_display_id: asyncio.sleep(0, result=None)
    return mt


def _export_ca(mt, monkeypatch, tmp_path, *, ca_host="ca01", task_id=7401) -> bytes:
    """Prove a CA private-key export through the production executor and recorder; return the PFX bytes."""
    mt._engagement_hops = [
        *list(getattr(mt, "_engagement_hops", []) or []),
        _proof_hop(f"remote-exec:{ca_host}@lab.local", task_id - 100, callback_id="13"),
        _proof_hop(f"local-admin:{ca_host}@lab.local", task_id - 99, callback_id="13"),
    ]

    async def fake_select(target_host, target_domain, local_account):
        return {"id": 91, "account": local_account, "realm": f"{target_host}.{target_domain}",
                "credential": "synthetic-local-admin"}

    monkeypatch.setattr(mt, "_select_managed_local_admin_credential", fake_select)
    pfx_blob = _write_test_ca_pfx(tmp_path / "source" / f"{ca_host}.pfx", CA_PASSWORD).read_bytes()
    sha = hashlib.sha256(pfx_blob).hexdigest()
    output = (
        f"b'SAGE_CA_EXPORT_PROOF_{ca_host}_13\\r\\n"
        f"CA_HOST={ca_host.upper()}\\r\\n"
        "CA_EXPORT_STATUS=OK\\r\\n"
        "CA_SUBJECT=CN=LAB-CA\\r\\n"
        "CA_ISSUER=CN=LAB-CA\\r\\n"
        "CA_THUMBPRINT=ABCDEF1234\\r\\n"
        f"CA_PFX_PATH=C:\\\\Windows\\\\Temp\\\\sage_ca_export_{ca_host}_13.pfx\\r\\n"
        f"PFX_ARTIFACT_ID=artifact-{ca_host}-{task_id}\\r\\n"
        f"PFX_SHA256={sha}\\r\\n"
        f"PFX_BASE64={base64.b64encode(pfx_blob).decode('ascii')}\\r\\n'\n"
    )
    outputs = iter(["Command executed successfully", output])
    with _split_issue(lambda: next(outputs), {"issue": 0}, display_id=task_id):
        result = json.loads(asyncio.run(mt.execute_capability(
            {"capability": "adcs-ca-private-key-export", "target_host": ca_host,
             "target_domain": "lab.local", "callback_id": "13"},
            {"timeout": 5},
        )))
    assert result["verdict"] == "achieved", result
    assert f"adcs-ca-private-key:{ca_host}@lab.local" in result["recorded_effects"]
    return pfx_blob


def _stage_recorder(mt, monkeypatch, *, upload_output="Uploaded file", upload_task=9001):
    staged: dict = {"registered": [], "uploads": []}

    async def register(filename, contents):
        staged["registered"].append(contents)
        return "file-uuid-1"

    async def upload(command, parameters, file_uuid, callback_display_id, token_id=None, timeout=None):
        mt._last_issued_task_display_id = upload_task
        staged["uploads"].append({"parameters": dict(parameters), "callback": callback_display_id})
        return upload_output

    monkeypatch.setattr(mt, "_register_file", register)
    monkeypatch.setattr(mt, "upload_file_by_file_uuid", upload)
    return staged


def _cert_auth(mt, inputs: dict, outputs=None) -> tuple[dict, dict]:
    calls: dict = {"issue": 0}
    replies = iter(list(outputs or CERT_AUTH_OUTPUTS))
    with _split_issue(lambda: next(replies), calls, display_id=9191):
        result = json.loads(asyncio.run(mt.execute_capability(dict(CERT_AUTH), {"timeout": 5, **inputs})))
    return result, calls


def _forges(result: dict) -> list[str]:
    return [str(item["parameters"]["assembly_arguments"]) for item in result.get("issued") or []
            if item.get("command") == "execute_assembly"
            and "forge --ca-cert" in str((item.get("parameters") or {}).get("assembly_arguments"))]


# --- positive: the proved artifact is what the forge consumes ------------------------------------------------

@pytest.mark.parametrize("suggested", [
    {"ca_pfx_path": GUESSED},
    {"ca_cert_path": GUESSED},
    {"ca_pfx_path": "not a path"},
    {"ca_pfx_path": GUESSED, "ca_pfx_artifact_id": "artifact-ca01-7401"},
    {"CA_PFX_PATH": GUESSED},
    {"ca_pfx_path ": GUESSED},
], ids=["guessed-backup-path", "alias-key", "malformed", "matching-artifact-id", "upper-case-key", "padded-key"])
def test_a_suggested_path_cannot_redirect_the_verified_ca_artifact(monkeypatch, tmp_path, suggested):
    mt = _tools(monkeypatch, tmp_path)
    pfx_blob = _export_ca(mt, monkeypatch, tmp_path)
    staged = _stage_recorder(mt, monkeypatch)

    result, _ = _cert_auth(mt, {"ca_pfx_password": CA_PASSWORD, "proof_host": "dc01.lab.local", **suggested})

    # The exact proved bytes were staged to the selected callback, and the forge read the staged copy.
    assert staged["registered"] == [pfx_blob]
    assert staged["uploads"] == [{"parameters": {"File": "file-uuid-1", "Path": STAGED}, "callback": 13}]
    [forge] = _forges(result)
    assert f"forge --ca-cert {STAGED} " in forge
    assert "backup" not in forge and "not a path" not in forge
    # The later verifier alone decided the outcome.
    assert result["verdict"] == "achieved", result
    assert "certificate-auth:administrator@lab.local" in result["recorded_effects"]


# --- negative: a verified artifact that fails a check stops before the forge ------------------------------

def _tamper_artifact(mt):
    hop = next(h for h in mt._engagement_hops if str(h.effect).startswith("adcs-ca-private-key:"))
    path = Path(hop.evidence["pfx_artifact_path"])
    path.write_bytes(path.read_bytes() + b"\0")


def _other_engagement(mt):
    mt._engagement_key = "another-engagement"


@pytest.mark.parametrize("break_it, check", [
    (_tamper_artifact, "sha-256"),
    (_other_engagement, "engagement"),
], ids=["wrong-hash", "wrong-engagement"])
def test_a_verified_artifact_failing_a_check_stops_before_the_forge(monkeypatch, tmp_path, break_it, check):
    mt = _tools(monkeypatch, tmp_path)
    _export_ca(mt, monkeypatch, tmp_path)
    staged = _stage_recorder(mt, monkeypatch)
    break_it(mt)

    result, _ = _cert_auth(mt, {"ca_pfx_password": CA_PASSWORD, "ca_pfx_path": GUESSED,
                                "proof_host": "dc01.lab.local"})

    assert result["ok"] is False and result["verdict"] == "failed", result
    assert _forges(result) == [] and staged["uploads"] == []
    assert check in result["reason"].casefold(), result["reason"]
    assert "certificate-auth:administrator@lab.local" not in json.dumps(result.get("recorded_effects"))


def test_failed_staging_stops_before_the_forge(monkeypatch, tmp_path):
    mt = _tools(monkeypatch, tmp_path)
    _export_ca(mt, monkeypatch, tmp_path)
    staged = _stage_recorder(mt, monkeypatch, upload_output="Error: access denied writing file")

    result, _ = _cert_auth(mt, {"ca_pfx_password": CA_PASSWORD, "ca_pfx_path": GUESSED,
                                "proof_host": "dc01.lab.local"})

    assert result["ok"] is False and _forges(result) == [], result
    assert len(staged["uploads"]) == 1
    assert "staging upload" in result["reason"]


def test_a_mismatched_artifact_id_is_not_selected(monkeypatch, tmp_path):
    mt = _tools(monkeypatch, tmp_path)
    _export_ca(mt, monkeypatch, tmp_path)
    staged = _stage_recorder(mt, monkeypatch)

    result, _ = _cert_auth(mt, {"ca_pfx_password": CA_PASSWORD, "ca_pfx_path": GUESSED,
                                "ca_pfx_artifact_id": "artifact-ca02-9999", "proof_host": "dc01.lab.local"})

    assert result["ok"] is False and _forges(result) == [] and staged["uploads"] == [], result
    assert "artifact" in result["reason"].casefold() and "artifact-ca02-9999" in result["reason"]


def test_another_cas_artifact_is_never_staged(monkeypatch, tmp_path):
    mt = _tools(monkeypatch, tmp_path)
    _export_ca(mt, monkeypatch, tmp_path, ca_host="ca02", task_id=7501)
    staged = _stage_recorder(mt, monkeypatch)

    result, calls = _cert_auth(mt, {"ca_pfx_password": CA_PASSWORD, "proof_host": "dc01.lab.local"})

    assert result["ok"] is False and calls["issue"] == 0 and staged["registered"] == [], result
    assert "adcs-ca-private-key:ca01@lab.local" in result["missing"]


def test_an_unproved_explicit_path_stays_explicit_and_is_never_a_proved_export(monkeypatch, tmp_path):
    mt = _tools(monkeypatch, tmp_path)
    mt._engagement_hops = []
    staged = _stage_recorder(mt, monkeypatch)

    result, _ = _cert_auth(mt, {"ca_pfx_password": CA_PASSWORD, "ca_pfx_path": GUESSED,
                                "proof_host": "dc01.lab.local"})

    # No verified export exists, so nothing is staged and the caller's path is used as the explicit input it is.
    assert staged["registered"] == [] and staged["uploads"] == []
    [forge] = _forges(result)
    assert f"forge --ca-cert {GUESSED} " in forge
    assert not any(str(h.effect).startswith("adcs-ca-private-key:") for h in mt._engagement_hops)


def test_a_proved_enrolled_certificate_is_not_overridden_by_a_caller_path(monkeypatch, tmp_path):
    mt = _tools(monkeypatch, tmp_path)
    _export_ca(mt, monkeypatch, tmp_path)
    mt._engagement_hops.append(_proof_hop("adcs-enrolled-certificate:administrator@lab.local", 7700,
                                          callback_id="13", target="enrolled"))
    staged = _stage_recorder(mt, monkeypatch)

    result, _ = _cert_auth(mt, {"ca_pfx_password": CA_PASSWORD, "ca_pfx_path": GUESSED,
                                "proof_host": "dc01.lab.local"})

    # The enrolled certificate is the proved input: nothing is forged from the caller path, nothing staged.
    assert _forges(result) == [] and staged["registered"] == [], result


# --- terminal: what the executor reports after its tasks ran ----------------------------------------------

def _install_supervised_claim(mt, action: dict, inputs: dict):
    contract = request_contract.build_request_contract(
        request_id="request-continuity", channel_id="channel-1", operation_id="operation-1",
        mode="supervised", autonomous_solve=False, callback_ids=("13",),
    )
    mt._request_contract = contract
    mt._turn_authority = SimpleNamespace(request_id=contract.request_id,
                                         request_contract_digest=contract.digest,
                                         bounded_callback_id="13")
    mt._active_approval_claim = {
        "approval_id": "approval-continuity",
        "request_id": contract.request_id,
        "request_contract_digest": contract.digest,
        "approved_actions": [{"name": "execute_capability", "args": {"action": action, "inputs": inputs}}],
    }
    mt._last_effect_denial = None

    async def completed(task_display_id, callback_display_id, command, parameters):
        # The inert Mythic seam reports each task's own terminal state, as Mythic does for a finished task.
        return {"task_completed": True, "task_status": "completed"}

    mt._supervised_task_state = completed


SCHANNEL_FAILED = [
    *CERT_AUTH_OUTPUTS[:3],
    "\n".join([
        "SAGE_CERT_AUTH_PROOF_administrator_lab_local_13",
        "CERT_AUTH_METHOD=schannel-ldap",
        "CERT_AUTH_LDAP_BIND=False",
        "CERT_AUTH_STATUS=FAILED",
    ]),
]


def test_a_refused_fallback_keeps_the_tasks_that_ran_and_the_failure_that_stopped_them(monkeypatch, tmp_path,
                                                                                       caplog):
    mt = _tools(monkeypatch, tmp_path)
    _export_ca(mt, monkeypatch, tmp_path)
    _stage_recorder(mt, monkeypatch)
    inputs = {"timeout": 5, "ca_pfx_password": CA_PASSWORD, "proof_host": "dc01.lab.local"}
    _install_supervised_claim(mt, dict(CERT_AUTH), inputs)

    with caplog.at_level("WARNING"):
        result, calls = _cert_auth(mt, inputs, outputs=SCHANNEL_FAILED)

    # The refusal's diagnostic was logged, without the artifact passwords the follow-on carried.
    assert "approval-coverage denial" in caplog.text
    assert CA_PASSWORD not in caplog.text and "SageCert-" not in caplog.text
    # The approved transaction's own tasks ran and are reported with their issued commands.
    assert [item["command"] for item in result["issued"]] == ["shell", "shell", "execute_assembly", "powerpick"]
    assert calls["issue"] == 4
    assert result["ok"] is False and result["verdict"] != "achieved", result
    # The stop names the proof failure, not a serialization or effect-boundary refusal.
    assert "non-JSON" not in json.dumps(result) and "not canonical" not in result["reason"]
    assert "approved proposal does not cover" not in result["reason"]
    # The internal fallback refusal is reported as such, and it is not a denial of the operator's approved action.
    assert result["transaction"]["fallback_denied"]["fallback"] == "pkinit-kerberos"
    assert mt._last_effect_denial is None
    # What this repair writes (the stop reason and the refusal record) carries no secret value.
    assert CA_PASSWORD not in result["reason"] + json.dumps(result["transaction"]["fallback_denied"])


def test_an_executor_exception_after_issue_keeps_the_issued_history(monkeypatch, tmp_path):
    mt = _tools(monkeypatch, tmp_path)
    _export_ca(mt, monkeypatch, tmp_path)
    _stage_recorder(mt, monkeypatch)

    def explode(*_args, **_kwargs):
        raise RuntimeError("synthetic verifier crash")

    monkeypatch.setattr(mt, "_capability_transaction_update_artifact", explode)
    result, calls = _cert_auth(mt, {"ca_pfx_password": CA_PASSWORD, "proof_host": "dc01.lab.local"})

    assert calls["issue"] >= 1
    assert result["ok"] is False and "synthetic verifier crash" in result["reason"]
    assert [item["command"] for item in result["issued"]][:1] == ["shell"]


# --- grounding: the card is offered from the proved input, not from a guessed one -------------------------

def _ground(mt, inputs: dict):
    from langchain_core.messages import AIMessage
    from test_step_bounds import _GroundRecord, _ground_middleware

    _ground_middleware.record = _GroundRecord()
    middleware = _ground_middleware(mt)
    message = AIMessage(content="", name="Mythic_Operator", tool_calls=[{
        "name": "execute_capability", "id": "c1", "type": "tool_call",
        "args": {"action": dict(CERT_AUTH), "inputs": dict(inputs)}}])
    asyncio.run(middleware._ground_and_limit_capability(SimpleNamespace(state={"messages": []}), message))
    return message, _ground_middleware.record


@pytest.mark.parametrize("suggested", [{}, {"ca_pfx_path": GUESSED}], ids=["no-path", "guessed-path"])
def test_a_proved_ca_export_lets_certificate_auth_reach_its_card_without_a_path(monkeypatch, tmp_path, suggested):
    mt = _tools(monkeypatch, tmp_path)
    _export_ca(mt, monkeypatch, tmp_path)

    message, record = _ground(mt, {"proof_host": "dc01.lab.local", **suggested})

    [call] = message.tool_calls
    assert record.status is None
    assert not any(key in call["args"]["inputs"] for key in ("ca_pfx_path", "ca_cert_path", "ca_certificate_path"))
    assert call["args"]["inputs"]["ca_host"] == "ca01"


def test_without_a_proved_export_certificate_auth_still_needs_an_explicit_input(monkeypatch, tmp_path):
    mt = _tools(monkeypatch, tmp_path)
    mt._engagement_hops = []

    message, record = _ground(mt, {"proof_host": "dc01.lab.local"})

    assert message.tool_calls == [] and "could not be planned yet" in str(message.content)
    assert record.status is not None
