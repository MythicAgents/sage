import json

from ai.hillclimb.ticket_context import cross_domain_context_candidate


def _row(task_id, output, **extra):
    forge = "golden" in output.casefold() or "ticket successfully imported" in output.casefold()
    access = "directory of" in output.casefold()
    value = {"operation_id": 7, "display_id": task_id, "completed": True,
             "callback_display_id": 4, "callback_active": True, "output": output}
    if forge:
        value.update({
            "command_name": "execute_assembly",
            "original_params": json.dumps({
                "assembly_name": "Rubeus.exe",
                "assembly_arguments": "golden /domain:parent.local /sid:S-1-5-21-1",
            }),
        })
    elif access:
        value.update({"command_name": "shell", "original_params": r"dir \\dc01.parent.local\c$"})
    else:
        value.update({"command_name": "shell", "original_params": "whoami"})
    value.update(extra)
    return value


def _rows():
    return [
        _row(13, "Rubeus golden /ptt ticket successfully imported"),
        _row(14, r"Directory of \\dc01.parent.local\c$"),
    ]


def test_positive_candidate_binds_operation_callback_target_and_lineage():
    result = cross_domain_context_candidate(_rows(), operation_id=7, callback_id=4,
                                            target="parent.local", baseline_task_ids={12})
    assert result is not None
    assert result.target == "parent.local"
    assert "tasks-13,14" in result.observation_id
    assert result.envelope_id.startswith("sha256:")


def test_existing_mimikatz_forge_and_native_ls_requests_are_admitted():
    rows = [
        _row(13, "kerberos::golden ticket forged", command_name="mimikatz",
             original_params=json.dumps({"commands": "kerberos::golden /domain:parent.local /sid:S-1-5-21-1"})),
        _row(14, r"Directory of \\dc01.parent.local\c$", command_name="ls",
             original_params=json.dumps({"path": r"\\dc01.parent.local\c$"})),
    ]
    assert cross_domain_context_candidate(rows, operation_id=7, callback_id=4,
                                          target="parent.local", baseline_task_ids=set()) is not None


def test_rejects_stale_wrong_identity_realm_order_denial_and_missing_import():
    assert cross_domain_context_candidate(_rows(), operation_id=8, callback_id=4, target="parent.local", baseline_task_ids=set()) is None
    assert cross_domain_context_candidate(_rows(), operation_id=7, callback_id=5, target="parent.local", baseline_task_ids=set()) is None
    assert cross_domain_context_candidate(_rows(), operation_id=7, callback_id=4, target="other.local", baseline_task_ids=set()) is None
    assert cross_domain_context_candidate(_rows(), operation_id=7, callback_id=4, target="parent.local", baseline_task_ids={13}) is None
    reversed_rows = [_row(13, r"Directory of \\dc01.parent.local\c$"), _row(14, "Rubeus golden /ptt ticket successfully imported")]
    assert cross_domain_context_candidate(reversed_rows, operation_id=7, callback_id=4, target="parent.local", baseline_task_ids=set()) is None
    denied = [_row(13, "Rubeus golden /ptt ticket successfully imported"), _row(14, r"Directory of \\dc01.parent.local\c$ Access is denied")]
    assert cross_domain_context_candidate(denied, operation_id=7, callback_id=4, target="parent.local", baseline_task_ids=set()) is None
    forge_without_literal_import = [_row(13, "Rubeus golden ticket forged"), _rows()[1]]
    assert cross_domain_context_candidate(forge_without_literal_import, operation_id=7, callback_id=4, target="parent.local", baseline_task_ids=set()) is not None


def test_rejects_malformed_duplicate_and_inactive_rows():
    duplicate = _rows() + [_row(14, "other")]
    assert cross_domain_context_candidate(duplicate, operation_id=7, callback_id=4, target="parent.local", baseline_task_ids=set()) is None
    inactive = _rows()
    for row in inactive:
        row["callback_active"] = False
    assert cross_domain_context_candidate(inactive, operation_id=7, callback_id=4, target="parent.local", baseline_task_ids=set()) is None
    assert cross_domain_context_candidate(_rows(), operation_id=None, callback_id=4, target="parent.local", baseline_task_ids=set()) is None


def test_intermediate_failure_cannot_disappear_from_the_proof_window():
    for active in (True, False, None):
        rows = [_rows()[0], _row(14, "KDC_ERR: failed", callback_active=active),
                _row(15, r"Directory of \\dc01.parent.local\c$")]
        assert cross_domain_context_candidate(rows, operation_id=7, callback_id=4,
                                              target="parent.local", baseline_task_ids=set()) is None


def test_latest_forge_can_recover_an_earlier_failed_window():
    rows = [_row(12, "Rubeus golden /ptt ticket successfully imported"),
            _row(13, "KDC_ERR: failed"),
            _row(14, "Rubeus golden ticket forged"),
            _row(15, r"Directory of \\dc01.parent.local\c$")]
    assert cross_domain_context_candidate(rows, operation_id=7, callback_id=4,
                                          target="parent.local", baseline_task_ids=set()) is not None


def test_request_echoes_and_wrong_request_target_cannot_be_promoted_by_output_text():
    echo_forge = _rows()
    echo_forge[0].update({"command_name": "shell", "original_params": "echo Rubeus golden /domain:parent.local"})
    assert cross_domain_context_candidate(echo_forge, operation_id=7, callback_id=4,
                                          target="parent.local", baseline_task_ids=set()) is None
    echo_listing = _rows()
    echo_listing[1].update({"command_name": "shell", "original_params": r"echo dir \\dc01.parent.local\c$"})
    assert cross_domain_context_candidate(echo_listing, operation_id=7, callback_id=4,
                                          target="parent.local", baseline_task_ids=set()) is None
    wrong_target = _rows()
    wrong_target[0]["original_params"] = json.dumps({
        "assembly_name": "Rubeus.exe", "assembly_arguments": "golden /domain:other.local /sid:S-1-5-21-1",
    })
    assert cross_domain_context_candidate(wrong_target, operation_id=7, callback_id=4,
                                          target="parent.local", baseline_task_ids=set()) is None
