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

import mythic_tools  # noqa: E402
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


def test_the_card_preview_plans_a_proved_ca_export_instead_of_blocking_the_card(monkeypatch, tmp_path):
    from test_step_bounds import model_module

    mt = _tools(monkeypatch, tmp_path)
    _export_ca(mt, monkeypatch, tmp_path)
    message, _ = _ground(mt, {"proof_host": "dc01.lab.local"})
    [call] = message.tool_calls

    preview = model_module.Model._capability_card_plan_text(SimpleNamespace(mythic_client=mt), call["args"])

    assert "could not be built" not in preview and "Planned inner tasks" in preview, preview


def test_without_a_proved_export_certificate_auth_still_needs_an_explicit_input(monkeypatch, tmp_path):
    mt = _tools(monkeypatch, tmp_path)
    mt._engagement_hops = []

    message, record = _ground(mt, {"proof_host": "dc01.lab.local"})

    assert message.tool_calls == [] and "could not be planned yet" in str(message.content)
    assert record.status is not None


# --- issued identity: a step's task and command come from Mythic's issuance record ------------------------
#
# Channel 3 requests 3-4: `dcsync-krbtgt` failed twice, a Rubeus `execute_assembly` ran as task 17, and the next
# `dcsync-krbtgt` was refused by Sage's own repeat-failure circuit breaker before any Mythic task existed. The
# executor reported that refusal as `dcsync` task 17 with a `partial` "DCSync ran" verdict read from Sage's STOP
# text. The fixtures run the production executor and issue path; only the Mythic API seam is inert.

DCSYNC = {"capability": "dcsync-krbtgt", "domain": "lab.local", "account": "krbtgt", "callback_id": "13"}
BAD_DN = "ERROR kuhl_m_lsadump_dcsync ; kull_m_rpc_drsr_CrackName: 0x00000002 (ERROR_DS_NAME_ERROR_NOT_FOUND) bad DN"


def _dcsync_tools(monkeypatch, tmp_path):
    mt = _tools(monkeypatch, tmp_path)
    mt._resolve_payload_type = lambda callback_id: asyncio.sleep(0, result="apollo")
    mt._resolve_domain_controller_host = lambda domain: asyncio.sleep(0, result="dc01.lab.local")
    return mt


def _run_dcsync(mt, output, task_ids) -> tuple[dict, dict]:
    calls: dict = {}
    with _split_issue(lambda: output, calls, display_id=lambda: next(task_ids)):
        result = json.loads(asyncio.run(mt.execute_capability(dict(DCSYNC), {"timeout": 5})))
    return result, calls


def test_a_step_refused_before_issue_reports_no_task_and_never_borrows_the_last_ones(monkeypatch, tmp_path):
    mt = _dcsync_tools(monkeypatch, tmp_path)
    task_ids = iter([14, 15, 16])
    for expected in (14, 15):
        failed, calls = _run_dcsync(mt, BAD_DN, task_ids)
        assert [(item["command"], item["task_id"]) for item in failed["issued"]] == [("dcsync", expected)]
        assert [issue["command_name"] for issue in calls["issued"]] == ["dcsync"]

    calls = {}
    with _split_issue(lambda: "[*] Action: Ask TGT\n[+] TGT request successful!", calls, display_id=17):
        asyncio.run(mt.issue_task_and_waitfor_task_output(
            "execute_assembly", {"assembly_name": "Rubeus.exe", "assembly_arguments": "asktgt /user:svc"}, 13,
        ))
    assert [issue["command_name"] for issue in calls["issued"]] == ["execute_assembly"]

    refused, calls = _run_dcsync(mt, BAD_DN, task_ids)

    assert calls.get("issued", []) == []  # the circuit breaker refused it: no Mythic task exists
    assert [(item["command"], item["task_id"]) for item in refused["issued"]] == [("dcsync", None)]
    assert refused["issued"][0]["task_issued"] is False
    assert refused["verdict"] == "failed" and refused.get("stopped_after") == "not_issued"
    assert "no mythic task was issued" in refused["reason"].casefold()
    assert "krbtgt-hash:lab.local" not in refused["recorded_effects"]


# --- krbtgt -> forge-golden-ticket: the card binds the proved credential by reference --------------------
#
# A `dcsync-krbtgt` proved through the production executor and verifier (task 21) records `krbtgt-hash:lab.local`
# and writes the key to Mythic's credential store. The next `forge-golden-ticket` card must name that credential
# and its proof task, never carry the raw key, and the executor must forge from that exact credential. Grounding
# happens before approval, so it may not write to Mythic. A key that only an operator or a task-output read
# supplies is not an admitted `krbtgt-hash` and must not reach a forge.

KRBTGT_KEY = "c" * 64
FORGE = {"capability": "forge-golden-ticket", "domain": "lab.local", "callback_id": "13"}
DCSYNC_OK = (
    "[DC] 'lab.local' will be the domain\n** SAM ACCOUNT **\nSAM Username         : krbtgt\nCredentials:\n"
    f"  Hash NTLM: {'d' * 32}\n* Primary:Kerberos-Newer-Keys *\n  aes256_hmac       (4096) : {KRBTGT_KEY}\n"
)


def _credential_store(mt, monkeypatch) -> dict:
    """An inert Mythic credential store: rows the client reads back, and every write Sage makes."""
    store: dict = {"rows": [], "writes": []}

    async def fetch(now):
        return list(store["rows"])

    async def create(client, credential, account, realm, comment, credential_type):
        row = {"id": 500 + len(store["rows"]), "account": account, "realm": realm, "type": credential_type,
               "credential_text": credential, "comment": comment}
        store["rows"].append(row)
        store["writes"].append(row)
        return {"status": "success", "id": row["id"]}

    mt._fetch_credentials_cached = fetch
    monkeypatch.setattr(mythic_tools.mythic, "create_credential", create)
    return store


def _krbtgt_tools(monkeypatch, tmp_path):
    mt = _dcsync_tools(monkeypatch, tmp_path)
    mt._resolve_domain_sid = lambda domain: asyncio.sleep(0, result="S-1-5-21-1-2-3")
    store = _credential_store(mt, monkeypatch)
    store["task_outputs"] = {}

    async def task_output(mythic, task_display_id, custom_return_attributes=None):
        # Mythic's record of a task's output: only tasks this fixture actually ran have one.
        text = store["task_outputs"].get(int(task_display_id), "")
        return [{"response_text": base64.b64encode(text.encode()).decode()}] if text else []

    monkeypatch.setattr(mythic_tools.mythic, "get_all_task_output_by_id", task_output)
    return mt, store


def _prove_krbtgt(mt, store, task_id=21):
    store["task_outputs"][task_id] = DCSYNC_OK
    result, _ = _run_dcsync(mt, DCSYNC_OK, iter([task_id]))
    assert result["verdict"] == "achieved" and "krbtgt-hash:lab.local" in result["recorded_effects"], result


def _ground_forge(mt, inputs: dict | None = None, action: dict | None = None):
    from langchain_core.messages import AIMessage
    from test_step_bounds import _GroundRecord, _ground_middleware

    _ground_middleware.record = _GroundRecord()
    message = AIMessage(content="", name="Mythic_Operator", tool_calls=[{
        "name": "execute_capability", "id": "g1", "type": "tool_call",
        "args": {"action": dict(action or FORGE), "inputs": dict(inputs or {})}}])
    asyncio.run(_ground_middleware(mt)._ground_and_limit_capability(SimpleNamespace(state={"messages": []}), message))
    return message


def _forge_from(mt, inputs: dict, action: dict | None = None) -> tuple[dict, dict]:
    calls: dict = {}
    with _split_issue(lambda: "ok", calls, display_id=lambda: 30 + len(calls.get("issued", []))):
        result = json.loads(asyncio.run(mt.execute_capability(dict(action or FORGE), {**inputs, "timeout": 5})))
    return result, calls


def _rubeus_issues(calls: dict) -> list[str]:
    return [json.dumps(issue["parameters"]) for issue in calls.get("issued", [])
            if issue["command_name"] == "execute_assembly"]


def test_a_proved_krbtgt_reaches_the_forge_card_by_reference_and_the_forge_uses_it(monkeypatch, tmp_path):
    mt, store = _krbtgt_tools(monkeypatch, tmp_path)
    _prove_krbtgt(mt, store)
    [row] = [row for row in store["rows"] if row["type"] == "key"]
    writes_before = len(store["writes"])

    message = _ground_forge(mt)

    [call] = message.tool_calls
    inputs = call["args"]["inputs"]
    assert KRBTGT_KEY not in json.dumps(call)  # the card, stream and checkpoint never hold the raw key
    assert inputs["credential_id"] == row["id"] and str(inputs["credential_proof_task_id"]) == "21"
    assert inputs["key_type"] == "aes256"
    assert len(store["writes"]) == writes_before

    result, calls = _forge_from(mt, inputs)

    [forge] = _rubeus_issues(calls)
    assert f"/aes256:{KRBTGT_KEY}" in forge
    assert KRBTGT_KEY not in json.dumps(result)


def test_recovering_a_proved_krbtgt_key_writes_nothing_before_approval(monkeypatch, tmp_path):
    mt, store = _krbtgt_tools(monkeypatch, tmp_path)
    _prove_krbtgt(mt, store)
    store["rows"].clear()  # the store row is gone; the proved hop and its Mythic task remain
    writes_before = len(store["writes"])

    message = _ground_forge(mt)

    [call] = message.tool_calls
    assert len(store["writes"]) == writes_before  # no Mythic credential write before the operator approves
    assert KRBTGT_KEY not in json.dumps(call)
    assert str(call["args"]["inputs"]["credential_proof_task_id"]) == "21"


def _assert_no_forge(mt, inputs: dict, action: dict | None = None):
    message = _ground_forge(mt, inputs, action)
    assert message.tool_calls == [], message.tool_calls
    # The worker is told the missing proof and that nothing ran, not invited to supply a key that is not proof.
    note = str(message.content)
    assert "proved `krbtgt-hash:lab.local`" in note and "no forge was issued" in note.casefold(), note
    assert "supply that input" not in note.casefold(), note
    result, calls = _forge_from(mt, inputs, action)
    assert _rubeus_issues(calls) == [], result
    assert result["verdict"] != "achieved"
    assert "proved `krbtgt-hash:lab.local`" in result["reason"], result["reason"]
    assert "no forge was issued" in result["reason"].casefold(), result["reason"]
    assert "nothing was issued" not in result["reason"].casefold(), result["reason"]


TYPED_KEY = "e" * 64
# Every place the forge builder reads a ticket key from: the action intent first, then the inputs.
TYPED_KEY_FORMS = [
    ({"key": TYPED_KEY, "key_type": "aes256"}, None),
    ({"krbtgt_aes256": TYPED_KEY}, None),
    ({"secret": TYPED_KEY}, None),
    ({"credential_text": TYPED_KEY}, None),
    ({"credential": TYPED_KEY}, None),
    ({}, {**FORGE, "intent": {"aes256": TYPED_KEY}}),
]
TYPED_KEY_IDS = ["key", "krbtgt_aes256", "secret", "credential_text", "credential", "intent-aes256"]


@pytest.mark.parametrize("inputs, action", TYPED_KEY_FORMS, ids=TYPED_KEY_IDS)
def test_an_operator_asserted_krbtgt_key_never_reaches_a_forge(monkeypatch, tmp_path, inputs, action):
    mt, _ = _krbtgt_tools(monkeypatch, tmp_path)
    mt._engagement_hops = []

    _assert_no_forge(mt, inputs, action)


@pytest.mark.parametrize("inputs, action", TYPED_KEY_FORMS, ids=TYPED_KEY_IDS)
def test_a_typed_key_beside_a_proved_one_is_neither_shown_nor_used(monkeypatch, tmp_path, inputs, action):
    mt, store = _krbtgt_tools(monkeypatch, tmp_path)
    _prove_krbtgt(mt, store)

    [call] = _ground_forge(mt, inputs, action).tool_calls
    card = json.dumps(call)
    assert TYPED_KEY not in card and KRBTGT_KEY not in card
    assert str(call["args"]["inputs"]["credential_proof_task_id"]) == "21"
    _, calls = _forge_from(mt, call["args"]["inputs"], call["args"]["action"])
    [forge] = _rubeus_issues(calls)
    assert f"/aes256:{KRBTGT_KEY}" in forge and TYPED_KEY not in forge


def test_a_historical_task_read_is_not_an_admitted_krbtgt(monkeypatch, tmp_path):
    mt, store = _krbtgt_tools(monkeypatch, tmp_path)
    mt._engagement_hops = []

    store["task_outputs"][24] = DCSYNC_OK  # a task this request never issued, holding real-looking material
    read = asyncio.run(mt.get_all_task_output_by_task_id(24))
    assert "historical_or_external" in json.dumps(read, default=str)

    assert store["writes"] == [] and mt._engagement_hops == []
    _assert_no_forge(mt, {})


def test_a_krbtgt_row_typed_into_the_credential_store_is_not_proof(monkeypatch, tmp_path):
    mt, store = _krbtgt_tools(monkeypatch, tmp_path)
    mt._engagement_hops = []
    store["rows"].append({"id": 700, "account": "krbtgt", "realm": "lab.local", "type": "key",
                          "credential_text": "f" * 64, "comment": "entered by hand", "task": None})

    _assert_no_forge(mt, {})


def test_a_krbtgt_row_an_agent_reported_from_a_completed_task_is_proof(monkeypatch, tmp_path):
    mt, store = _krbtgt_tools(monkeypatch, tmp_path)
    mt._engagement_hops = []
    store["rows"].append({"id": 701, "account": "krbtgt", "realm": "lab.local", "type": "key",
                          "credential_text": KRBTGT_KEY, "comment": "reported by the agent",
                          "task": {"display_id": 24, "status": "completed", "completed": True,
                                   "command_name": "dcsync", "callback": {"display_id": 13}}})

    [call] = _ground_forge(mt).tool_calls
    inputs = call["args"]["inputs"]
    assert KRBTGT_KEY not in json.dumps(call)
    assert inputs["credential_id"] == 701 and str(inputs["credential_proof_task_id"]) == "24"
    _, calls = _forge_from(mt, inputs)
    [forge] = _rubeus_issues(calls)
    assert f"/aes256:{KRBTGT_KEY}" in forge


@pytest.mark.parametrize("predecessor", ["wrong-domain", "failed", "no-parsed-secret"])
def test_an_unadmitted_predecessor_never_reaches_a_forge(monkeypatch, tmp_path, predecessor):
    mt, store = _krbtgt_tools(monkeypatch, tmp_path)
    mt._engagement_hops = []
    if predecessor == "wrong-domain":
        mt._engagement_hops = [_proof_hop("krbtgt-hash:other.local", 22, callback_id="13")]
    else:
        output = BAD_DN if predecessor == "failed" else "[DC] 'lab.local' will be the domain\n[DC] ms-DRSR connected"
        result, _ = _run_dcsync(mt, output, iter([22]))
        assert "krbtgt-hash:lab.local" not in result["recorded_effects"]

    assert [row for row in store["rows"] if row["realm"] == "lab.local"] == []
    _assert_no_forge(mt, {})


# --- an approval binds one credential; a forge binds the key for the domain it forges ---------------------------
#
# An approved card names a credential and its proof task. The executor resolves that key again after approval and
# runs more than one binding pass over the same inputs, so every pass must refuse a reference that no longer
# resolves; a later pass may not substitute whatever key is current. The forge plan issues the ticket in its source
# (krbtgt) domain; the key and domain SID bound for it must be that domain's, even when the request also names the
# domain it wants the ticket for.

def _add_reported_krbtgt(store, *, row_id, realm, key, task_id):
    store["rows"].append({"id": row_id, "account": "krbtgt", "realm": realm, "type": "key",
                          "credential_text": key, "comment": "reported by the agent",
                          "task": {"display_id": task_id, "status": "completed", "completed": True,
                                   "command_name": "dcsync", "callback": {"display_id": 13}}})


def _assert_stale_reference_refused(result, calls, stale_key=None):
    assert calls.get("issued", []) == [] and result["issued"] == [], result  # it stops before any task
    assert result["verdict"] != "achieved"
    reason = result["reason"]
    assert "krbtgt credential reference" in reason and "no longer resolves" in reason, reason
    assert "new approval" in reason and "no forge was issued" in reason.casefold(), reason
    assert "has no proved" not in reason, reason  # a proved key exists; it is not the referenced one
    assert KRBTGT_KEY not in json.dumps(result)
    if stale_key:
        assert stale_key not in json.dumps(result)


@pytest.mark.parametrize("pin", [
    {"credential_proof_task_id": "99"},
    {"credential_id": 9999},
    {"credential_proof_task_id": "99", "credential_id": 9999},
], ids=["proof-task", "credential-id", "both"])
def test_an_approved_reference_that_no_longer_resolves_forges_nothing(monkeypatch, tmp_path, pin):
    mt, store = _krbtgt_tools(monkeypatch, tmp_path)
    _prove_krbtgt(mt, store)  # task 21 proves a key: the current one, but not the one approved

    result, calls = _forge_from(mt, dict(pin))

    _assert_stale_reference_refused(result, calls)


def test_a_key_proved_after_approval_is_not_the_approved_key(monkeypatch, tmp_path):
    mt, store = _krbtgt_tools(monkeypatch, tmp_path)
    _prove_krbtgt(mt, store)
    [call] = _ground_forge(mt).tool_calls
    approved = call["args"]["inputs"]
    assert str(approved["credential_proof_task_id"]) == "21"
    rotated = "b" * 64
    store["rows"][:] = [row for row in store["rows"] if row["type"] != "key"]
    _add_reported_krbtgt(store, row_id=702, realm="lab.local", key=rotated, task_id=30)

    result, calls = _forge_from(mt, approved, call["args"]["action"])

    _assert_stale_reference_refused(result, calls, stale_key=rotated)


@pytest.mark.parametrize("pin", [{"credential_proof_task_id": "99"}, {"credential_proof_task_id": "21"}, {}],
                         ids=["stale", "matching", "unpinned"])
def test_binding_the_same_inputs_again_binds_the_same_key(monkeypatch, tmp_path, pin):
    import capabilities

    mt, store = _krbtgt_tools(monkeypatch, tmp_path)
    _prove_krbtgt(mt, store)
    inputs = dict(pin)
    action = mt._capability_tool_action(dict(FORGE), inputs, capabilities)

    bound = []
    for _ in range(3):  # the card, the executor, and its command build each bind the same inputs
        asyncio.run(mt._augment_capability_runtime_inputs(action, inputs))
        bound.append((inputs.get("key"), str(inputs.get("credential_proof_task_id") or "")))

    expected_key = None if pin.get("credential_proof_task_id") == "99" else KRBTGT_KEY
    expected_task = pin.get("credential_proof_task_id") or "21"
    assert bound == [(expected_key, expected_task)] * 3


PARENT_KEY = "a" * 64
LAB_SID, PARENT_SID = "S-1-5-21-1-2-3", "S-1-5-21-9-9-9"


def _two_domain_tools(monkeypatch, tmp_path):
    mt, store = _krbtgt_tools(monkeypatch, tmp_path)
    sids = {"lab.local": LAB_SID, "parent.local": PARENT_SID}
    mt._resolve_domain_sid = lambda domain: asyncio.sleep(0, result=sids.get(str(domain).casefold(), ""))
    return mt, store


def test_a_forge_for_its_source_domain_binds_that_domains_key_and_sid(monkeypatch, tmp_path):
    mt, store = _two_domain_tools(monkeypatch, tmp_path)
    _prove_krbtgt(mt, store)  # lab.local key, task 21
    _add_reported_krbtgt(store, row_id=701, realm="parent.local", key=PARENT_KEY, task_id=24)
    request = {"domain": "parent.local", "source_domain": "lab.local", "extra_sids": [f"{PARENT_SID}-519"]}

    [call] = _ground_forge(mt, request).tool_calls

    inputs = call["args"]["inputs"]
    assert str(inputs["credential_proof_task_id"]) == "21" and inputs["credential_id"] != 701
    assert inputs["domain_sid"] == LAB_SID
    assert PARENT_KEY not in json.dumps(call) and KRBTGT_KEY not in json.dumps(call)

    _, calls = _forge_from(mt, inputs, call["args"]["action"])

    [forge] = _rubeus_issues(calls)
    assert "/domain:lab.local" in forge and f"/sid:{LAB_SID} " in forge, forge
    assert f"/aes256:{KRBTGT_KEY}" in forge and PARENT_KEY not in forge, forge
    assert f"/sids:{PARENT_SID}-519" in forge, forge


@pytest.mark.parametrize("inputs, action", [
    ({"domain": "parent.local", "source_domain": "lab.local", "extra_sids": [f"{PARENT_SID}-519"]}, None),
    ({"domain": "parent.local"}, {"capability": "forge-golden-ticket", "target": "domain=lab.local", "callback_id": "13"}),
], ids=["source-domain-input", "action-target-domain"])
def test_another_domains_key_never_forges_the_source_domain(monkeypatch, tmp_path, inputs, action):
    mt, store = _two_domain_tools(monkeypatch, tmp_path)
    mt._engagement_hops = []
    _add_reported_krbtgt(store, row_id=701, realm="parent.local", key=PARENT_KEY, task_id=24)

    _assert_no_forge(mt, inputs, action)


def test_an_approved_reference_that_no_longer_resolves_issues_nothing_under_the_approval(monkeypatch, tmp_path):
    mt, store = _krbtgt_tools(monkeypatch, tmp_path)
    _prove_krbtgt(mt, store)
    [call] = _ground_forge(mt).tool_calls
    action, approved = dict(call["args"]["action"]), {**call["args"]["inputs"], "timeout": 5}
    store["rows"][:] = [row for row in store["rows"] if row["type"] != "key"]
    _add_reported_krbtgt(store, row_id=702, realm="lab.local", key="b" * 64, task_id=30)
    _install_supervised_claim(mt, action, approved)

    calls: dict = {}
    with _split_issue(lambda: "ok", calls, display_id=lambda: 30 + len(calls.get("issued", []))):
        result = json.loads(asyncio.run(mt.execute_capability(action, approved)))

    _assert_stale_reference_refused(result, calls, stale_key="b" * 64)


EKC = {"capability": "ensure-kerberos-context", "domain": "lab.local", "callback_id": "13"}


def test_a_kerberos_context_card_never_carries_a_reference_that_no_longer_resolves(monkeypatch, tmp_path):
    mt, store = _krbtgt_tools(monkeypatch, tmp_path)
    _prove_krbtgt(mt, store)

    message = _ground_forge(mt, {"credential_proof_task_id": "99"}, dict(EKC))

    assert message.tool_calls == [], message.tool_calls  # its card would show a forge the executor cannot run
    note = str(message.content)
    assert "no longer resolves" in note and "no forge was issued" in note.casefold(), note


def test_a_kerberos_context_whose_approved_key_is_gone_runs_no_other_plan(monkeypatch, tmp_path):
    mt, store = _krbtgt_tools(monkeypatch, tmp_path)
    _prove_krbtgt(mt, store)
    [call] = _ground_forge(mt, None, dict(EKC)).tool_calls
    approved = call["args"]["inputs"]
    assert str(approved["credential_proof_task_id"]) == "21"
    store["rows"][:] = [row for row in store["rows"] if row["type"] != "key"]
    _add_reported_krbtgt(store, row_id=702, realm="lab.local", key="b" * 64, task_id=30)

    result, calls = _forge_from(mt, approved, call["args"]["action"])

    _assert_stale_reference_refused(result, calls, stale_key="b" * 64)  # no refresh plan, no ticket purge


def test_a_refused_forge_never_says_its_earlier_checks_did_not_run(monkeypatch, tmp_path):
    mt, _ = _krbtgt_tools(monkeypatch, tmp_path)
    mt._engagement_hops = []

    result, calls = _forge_from(mt, {})

    assert _rubeus_issues(calls) == []
    assert result["issued"], result  # the context checks ahead of the build issued their own tasks
    assert "nothing was issued" not in result["reason"].casefold(), result["reason"]
    assert "no forge was issued" in result["reason"].casefold(), result["reason"]
