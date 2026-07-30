import json
import sqlite3
import sys
from pathlib import Path


sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "ai"))

from trajectory.corpus import build_manifest  # noqa: E402
from trajectory.exporter import export_sage_artifact, export_text_artifact, export_transitions  # noqa: E402
from trajectory.labeler import classify_observation, repair_for_label  # noqa: E402
from trajectory.replay import replay_score  # noqa: E402
from trajectory.runtime import TrajectoryRepairBridge  # noqa: E402
from trajectory.schema import TransitionRecord, load_jsonl, redact_text, write_jsonl  # noqa: E402


def test_labeler_classifies_dcsync_ambiguous_name():
    text = "ERROR kull_m_rpc_drsr_CrackName ; CrackNames (name status): 0x00000003 (3) - ERROR_NOT_UNIQUE"

    result = classify_observation(text)

    assert result.label == "ambiguous_account_name"
    assert "ambiguous_account_name" in result.labels


def test_labeler_classifies_dcsync_bad_dn_or_context():
    text = """
    mimikatz(commandline) # lsadump::dcsync /domain:essos.local /dc:meereen.essos.local /user:ESSOS\\krbtgt
    ERROR kuhl_m_lsadump_dcsync ; GetNCChanges: 0x000020f7 (8439)
    """

    result = classify_observation(text)
    repair = repair_for_label(result.label)

    assert result.label == "dcsync_bad_dn_or_context"
    assert result.labels[0] == "dcsync_bad_dn_or_context"
    assert repair is not None
    assert repair[0] == "rebuild_dcsync_target_and_materialize_context"


def test_labeler_classifies_gpo_delayed_effect():
    text = "[SAGE RESULT] GPO setup pending: wait for Group Policy to apply with wait_for_seconds"

    result = classify_observation(text)

    assert result.label == "delayed_effect"


def test_labeler_classifies_unresolved_gpo_identity_before_delayed_effect_noise():
    text = """
    [PowerShellHost Error] : Unhandled exception: GPO not found in SYSVOL policy root: starkwallpaper
    Generated fallback script referenced ScheduledTasks.xml and version bump, but no GPO was resolved.
    """

    result = classify_observation(text)

    assert result.label == "unresolved_gpo_identity"
    assert result.labels[0] == "unresolved_gpo_identity"
    assert "delayed_effect" in result.labels


def test_labeler_classifies_command_template_error_before_inert_gpo_template_text():
    text = """
    [PowerShellHost Error] : Unhandled exception: The specified wildcard character pattern is not valid:
    *[{00000000-0000-0000-0000-000000000000}{CAB54552-DEEA-4691-817E-ED4A4D1AFC72}]*
    Generated script also contains the inert fallback string:
    GPO identity unresolved: could not resolve GPO '$gpoName'.
    """

    result = classify_observation(text)

    assert result.label == "command_template_error"
    assert result.labels[0] == "command_template_error"
    assert "unresolved_gpo_identity" in result.labels


def test_labeler_classifies_directory_bind_error():
    text = '[PowerShellHost Error] : Unhandled exception: The following exception occurred while retrieving member "Put": "An operations error occurred."'

    result = classify_observation(text)

    assert result.label == "directory_bind_error"
    assert result.labels[0] == "directory_bind_error"


def test_labeler_does_not_treat_success_summary_no_ticket_refresh_as_wrong_context():
    text = """
    Result: ok=true, verdict=achieved. Verifier saw real secret material.
    No ticket refresh, ticket forge, or unrelated recon was run.
    """

    result = classify_observation(text)

    assert result.label == "unclassified"
    assert "wrong_security_context" not in result.labels


def test_labeler_classifies_real_missing_ticket_context():
    text = "No tickets in current context. Access to \\\\dc01.lab.local\\SYSVOL was not proven."

    result = classify_observation(text)

    assert result.label == "wrong_security_context"
    assert result.labels[0] == "wrong_security_context"


def test_labeler_classifies_command_size_before_no_proof():
    text = """
    GPO fallback marker probe is blocked at the writer step: Apollo returned
    The command line is too long. No proof output exists because proof-read was not reached.
    """

    result = classify_observation(text)

    assert result.label == "command_size_limit"
    assert "verifier_false_positive" in result.labels


def test_redaction_replaces_common_secret_material():
    ntlm = "2b576acbe6bcfda7294d6bd18041b8fe"
    aes = "a1b2c3d4e5f60718293a4b5c6d7e8f90a1b2c3d4e5f60718293a4b5c6d7e8f90"
    laps = "&30j5ozTm8z/u]"
    pfx_secret = "SageCert-fe2b948820662d8a-administrator_essos_local_3"
    pfx_blob = "A" * 80

    redacted = redact_text(
        f"Hash NTLM: {ntlm}\n"
        f"aes256_hmac: {aes}\n"
        "password: Heartsbane\n"
        f"recovered managed local admin secret for host: `{laps}`\n"
        f"recovered managed local admin secret for `braavos.essos.local`: `{laps}`\n"
        f'{{"credential": "{laps}", "forged_pfx_password": "{pfx_secret}"}}\n'
        f"PFX_BASE64={pfx_blob}"
    )

    assert ntlm not in redacted
    assert aes not in redacted
    assert "Heartsbane" not in redacted
    assert laps not in redacted
    assert pfx_secret not in redacted
    assert pfx_blob not in redacted
    assert "<ntlm:sha256:" in redacted
    assert "<aes256:sha256:" in redacted
    assert "password=<password:redacted>" in redacted
    assert '"credential": "<password:redacted>"' in redacted
    assert "PFX_BASE64=<base64_blob>" in redacted


def test_manifest_scans_read_only_artifact_types(tmp_path):
    (tmp_path / "sage_20260613.db").write_text("not really sqlite", encoding="utf-8")
    phoenix_dir = tmp_path / ".phoenix"
    phoenix_dir.mkdir()
    (phoenix_dir / "phoenix.db").write_text("not really sqlite", encoding="utf-8")
    (tmp_path / "essos_da_0609.out").write_text("ERROR_NOT_UNIQUE", encoding="utf-8")
    ledger_dir = tmp_path / ".sage_engagement"
    ledger_dir.mkdir()
    (ledger_dir / "state_Operation_X.json").write_text("{}", encoding="utf-8")

    manifest = build_manifest([tmp_path])
    kinds = {item.kind for item in manifest}

    assert {"sage_db", "phoenix_db", "solve_log", "engagement_ledger"} <= kinds
    assert all(item.readable for item in manifest)


def test_export_text_artifact_creates_repair_transitions(tmp_path):
    log = tmp_path / "essos_da_0609.out"
    log.write_text(
        """
        [SAGE RESULT] GPO setup pending: SharpGPOAbuse modified the GPO setup artifact,
        but this is not SYSTEM execution proof. Wait for Group Policy to apply.
        ERROR kull_m_rpc_drsr_CrackName ; CrackNames (name status): 0x00000003 (3) - ERROR_NOT_UNIQUE
        """,
        encoding="utf-8",
    )

    records = export_text_artifact(log)
    by_label = {record.failure_label: record for record in records}

    assert by_label["delayed_effect"].repair.kind == "bounded_poll_wait_for_verifier"
    assert by_label["ambiguous_account_name"].repair.kind == "qualify_principal_with_target_netbios"
    assert by_label["ambiguous_account_name"].capability == "dcsync-account"


def test_export_command_size_prefers_actual_gpo_context_over_do_not_dcsync_noise(tmp_path):
    log = tmp_path / "essos_da_size.out"
    log.write_text(
        """
        Focused live GPO fallback marker probe only. Do not DCSync.
        The deterministic writer command emitted by build_capability_commands is too long for Apollo shell.
        The command line is too long. No proof output exists because the proof-read command was not reached.
        """,
        encoding="utf-8",
    )

    records = export_text_artifact(log)

    assert len(records) == 1
    assert records[0].failure_label == "command_size_limit"
    assert records[0].repair.kind == "stage_or_shorten_command"
    assert records[0].capability == "gpo-controlled-system-exec"


def test_export_transitions_reads_corpus_root(tmp_path):
    log = tmp_path / "essos_da_test.out"
    log.write_text("ERROR_NOT_UNIQUE", encoding="utf-8")

    records = export_transitions([tmp_path])

    assert len(records) == 1
    assert records[0].failure_label == "ambiguous_account_name"


def test_replay_scores_historical_repair_match(tmp_path):
    log = tmp_path / "essos_da_test.out"
    log.write_text("ERROR_NOT_UNIQUE", encoding="utf-8")
    records = export_text_artifact(log)
    train = tmp_path / "train.jsonl"
    eval_path = tmp_path / "eval.jsonl"
    write_jsonl(str(train), records)
    write_jsonl(str(eval_path), records)

    result = replay_score(
        [TransitionRecord.from_dict(json.loads(line)) for line in train.read_text().splitlines()],
        [TransitionRecord.from_dict(json.loads(line)) for line in eval_path.read_text().splitlines()],
        include_diagnostic=True,
    )

    assert result.total == 1
    assert result.exact_repair_rate == 1.0
    assert result.label_match_rate == 1.0


def test_runtime_bridge_records_and_recalls_dcsync_ambiguity(tmp_path):
    store = tmp_path / "runtime_transitions.jsonl"
    bridge = TrajectoryRepairBridge(store)

    first = bridge.record_failure(
        action={
            "name": "dcsync-account",
            "target": "domain=essos.local;account=krbtgt",
            "effects": ["krbtgt-hash:essos.local"],
            "intent": {"domain": "essos.local", "account": "krbtgt"},
        },
        inputs={"domain": "essos.local", "account": "krbtgt", "password": "Heartsbane"},
        callback_id="2",
        reason="ERROR kull_m_rpc_drsr_CrackName ; CrackNames (name status): 0x00000003 (3) - ERROR_NOT_UNIQUE",
        issued=[
            {
                "command": "dcsync",
                "parameters": {"domain": "essos.local", "user": "krbtgt"},
                "_output": "password: Heartsbane ERROR_NOT_UNIQUE",
            }
        ],
    )
    second = bridge.record_failure(
        action={"name": "dcsync-account", "target": "domain=essos.local;account=krbtgt"},
        inputs={"domain": "essos.local", "account": "krbtgt"},
        callback_id="2",
        reason="CrackNames (name status): 0x00000003 - ERROR_NOT_UNIQUE",
        issued=[],
    )

    assert first["failure_label"] == "ambiguous_account_name"
    assert first["repair"]["kind"] == "qualify_principal_with_target_netbios"
    assert second["decision"]["evidence_count"] == 2
    assert "Heartsbane" not in store.read_text(encoding="utf-8")
    records = load_jsonl(str(store))
    assert records[0].failure_label == "ambiguous_account_name"
    assert records[0].repair.kind == "qualify_principal_with_target_netbios"


def test_runtime_bridge_recommends_wait_for_gpo_delayed_effect(tmp_path):
    store = tmp_path / "runtime_transitions.jsonl"
    bridge = TrajectoryRepairBridge(store)

    result = bridge.record_failure(
        action={
            "name": "gpo-controlled-system-exec",
            "target": "gpo=controlled-policy",
            "effects": ["system-exec:target-host"],
        },
        inputs={"gpo": "controlled-policy", "proof_path": r"C:\Users\Public\sage-proof.txt"},
        callback_id="2",
        reason="[SAGE RESULT] GPO setup pending: wait for Group Policy to apply before proof",
        issued=[{"command": "execute_assembly", "parameters": {"assembly_arguments": "--AddComputerTask"}}],
    )

    assert result["failure_label"] == "delayed_effect"
    assert result["repair"]["kind"] == "bounded_poll_wait_for_verifier"


def test_runtime_bridge_recommends_gpo_guid_resolution_for_unresolved_gpo(tmp_path):
    store = tmp_path / "runtime_transitions.jsonl"
    bridge = TrajectoryRepairBridge(store)

    result = bridge.record_failure(
        action={
            "name": "gpo-controlled-system-exec",
            "target": "gpo=starkwallpaper;domain=north.sevenkingdoms.local",
            "effects": ["system-exec:gpo:starkwallpaper@north.sevenkingdoms.local"],
        },
        inputs={"gpo": "starkwallpaper", "domain": "north.sevenkingdoms.local"},
        callback_id="4",
        reason="GPO identity unresolved: could not resolve GPO 'starkwallpaper' in domain 'north.sevenkingdoms.local'",
        issued=[{"command": "powerpick", "parameters": "ScheduledTasks.xml writer"}],
    )

    assert result["failure_label"] == "unresolved_gpo_identity"
    assert result["repair"]["kind"] == "resolve_gpo_guid_then_retry"


def test_runtime_bridge_ignores_inert_template_text_when_labeling_failure(tmp_path):
    store = tmp_path / "runtime_transitions.jsonl"
    bridge = TrajectoryRepairBridge(store)

    result = bridge.record_failure(
        action={"name": "gpo-controlled-system-exec", "target": "gpo=starkwallpaper;domain=north.sevenkingdoms.local"},
        inputs={"gpo": "starkwallpaper", "domain": "north.sevenkingdoms.local"},
        callback_id="4",
        reason="The specified wildcard character pattern is not valid: *[{GUID}{CSE}]*",
        issued=[{
            "command": "powerpick",
            "parameters": "throw \"GPO identity unresolved: inert template text\"",
        }],
    )

    assert result["failure_label"] == "command_template_error"
    assert result["repair"]["kind"] == "fix_deterministic_builder_template"
    assert "GPO identity unresolved" not in result["observation_excerpt"]


def test_runtime_bridge_recommends_writable_dc_bind_for_directory_error(tmp_path):
    store = tmp_path / "runtime_transitions.jsonl"
    bridge = TrajectoryRepairBridge(store)

    result = bridge.record_failure(
        action={"name": "gpo-controlled-system-exec", "target": "gpo=starkwallpaper;domain=north.sevenkingdoms.local"},
        inputs={"gpo": "starkwallpaper", "domain": "north.sevenkingdoms.local"},
        callback_id="4",
        reason='The following exception occurred while retrieving member "Put": "An operations error occurred."',
        issued=[],
    )

    assert result["failure_label"] == "directory_bind_error"
    assert result["repair"]["kind"] == "bind_to_writable_domain_controller"


def test_export_sage_db_artifact_creates_repair_transition(tmp_path):
    db_path = tmp_path / "sage_test.db"
    con = sqlite3.connect(db_path)
    con.execute("CREATE TABLE checkpoints (id INTEGER PRIMARY KEY, payload TEXT)")
    con.execute(
        "INSERT INTO checkpoints (payload) VALUES (?)",
        ("tool output: ERROR_NOT_UNIQUE during dcsync",),
    )
    con.commit()
    con.close()

    records = export_sage_artifact(db_path)

    assert len(records) == 1
    assert records[0].failure_label == "ambiguous_account_name"
    assert records[0].capability == "dcsync-account"
