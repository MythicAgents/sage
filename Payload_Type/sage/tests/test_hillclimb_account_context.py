"""Raw request/response boundary probes for the account-context oracle."""
from copy import deepcopy
import json

import pytest

from ai.hillclimb.account_context import account_kerberos_context_observation as observe
from ai.hillclimb import effect_registry


def rows():
    def row(task_id, command, params, output):
        return {"operation_id": 7, "display_id": task_id, "callback_display_id": 4,
                "callback_active": True, "completed": True, "command_name": command,
                "original_params": json.dumps(params) if isinstance(params, dict) else params, "output": output}
    return [
        row(11, "make_token", {"credential": {"account": "alice", "realm": "branch.local",
                                             "credential": "synthetic-only"}, "netOnly": True},
            "Successfully impersonated NORTH\\samwell for local access and branch.local\\alice for remote access."),
        row(12, "ticket_store_add", {"base64ticket": "synthetic-only-ticket"}, "Added ticket"),
        row(13, "ticket_store_list", {"luid": ""},
            "Cached Tickets\nCurrent LUID    : 0x1ae0916\n#0> Client: alice @ BRANCH.LOCAL\nServer: krbtgt/branch.local @ BRANCH.LOCAL"),
        row(14, "shell", r"dir \\dc.branch.local\SYSVOL", r"Directory of \\dc.branch.local\SYSVOL"),
    ]


def candidate(data, **kwargs):
    return observe(data, operation_id=7, callback_id=4, account="alice", domain="branch.local",
                   baseline_task_ids={10}, **kwargs)


def test_actual_account_context_sequence_is_typed_and_sanitized():
    proof = candidate(rows())
    assert effect_registry.admit_observation("ACCOUNT_KERBEROS_CONTEXT", proof,
                                             expected_target="branch.local", expected_subject="alice@branch.local").proven
    assert "synthetic-only" not in json.dumps(proof.to_dict())


@pytest.mark.parametrize("mutation", ["echo-cache", "echo-access", "wrong-client", "substring-client",
                                      "wrong-realm", "wrong-callback", "stale", "missing-import",
                                      "missing-context", "failed-import", "reordered", "mixed-cache-entry",
                                      "wrong-target", "wrong-logon", "wrong-remote-identity",
                                      "generatedpass", "metadata-only"])
def test_incomplete_wrong_identity_or_self_report_is_not_proof(mutation):
    data = deepcopy(rows())
    if mutation == "echo-cache":
        data[2]["original_params"] = 'echo "Client: alice @ BRANCH.LOCAL"'
    elif mutation == "echo-access":
        data[3]["original_params"] = 'echo "Directory of \\\\dc.branch.local\\SYSVOL"'
    elif mutation == "wrong-client":
        data[2]["output"] = data[2]["output"].replace("alice", "bob")
    elif mutation == "substring-client":
        data[2]["output"] = data[2]["output"].replace("alice", "notalice")
    elif mutation == "wrong-realm":
        data[2]["output"] = data[2]["output"].replace("BRANCH.LOCAL", "SUB.BRANCH.LOCAL")
    elif mutation == "wrong-callback":
        data[1]["callback_display_id"] = 5
    elif mutation == "stale":
        data[0]["display_id"] = 9
    elif mutation == "missing-import":
        del data[1]
    elif mutation == "missing-context":
        del data[0]
    elif mutation == "failed-import":
        data[1]["output"] = "ticket_store_add failed"
    elif mutation == "reordered":
        data[1]["display_id"], data[2]["display_id"] = data[2]["display_id"], data[1]["display_id"]
    elif mutation == "mixed-cache-entry":
        data[2]["output"] = "Client: alice @ BRANCH.LOCAL\nServer: krbtgt/other.local\nClient: bob @ BRANCH.LOCAL\nServer: krbtgt/branch.local"
    elif mutation == "wrong-target":
        data[3]["original_params"] = r"dir \\dc.sub.branch.local\SYSVOL"
    elif mutation == "wrong-logon":
        data[0]["original_params"] = data[0]["original_params"].replace("alice", "bob")
    elif mutation == "wrong-remote-identity":
        data[0]["output"] = data[0]["output"].replace("branch.local\\alice", "branch.local\\bob")
    elif mutation == "generatedpass":
        data[0]["command_name"] = "shell"
        data[0]["original_params"] = "generatepass /domain:branch.local /user:alice"
    elif mutation == "metadata-only":
        data = [{"passes": True}]
    assert candidate(data) is None


def test_explicit_session_mismatch_and_targeted_session_are_unavailable():
    data = rows()
    data[1]["original_params"] = json.dumps({"base64ticket": "synthetic-only-ticket", "luid": "0x111"})
    data[2]["original_params"] = json.dumps({"luid": "0x222"})
    assert candidate(data) is None

    targeted = rows()
    targeted[1]["original_params"] = json.dumps({"base64ticket": "synthetic-only-ticket", "luid": "0x111"})
    targeted[2]["original_params"] = json.dumps({"luid": "0x111"})
    targeted[2]["output"] = targeted[2]["output"].replace("0x1ae0916", "0x111")
    assert candidate(targeted) is None


def test_default_current_session_allows_one_agreeing_native_luid():
    assert candidate(rows()) is not None
    data = rows()
    data[1]["output"] += "\nTarget LUID: 0x222"
    assert candidate(data) is None


def test_registry_rejects_self_report_for_all_capability_effect_contracts():
    for contract in effect_registry.CAPABILITY_EFFECT_REGISTRY:
        result = effect_registry.admit_observation(contract.milestone, {"passes": True})
        assert not result.proven
