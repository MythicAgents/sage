"""Offline synthetic checks for request-bound credential-effect parsers."""
from __future__ import annotations

import json

import pytest

from ai.hillclimb import credential_effects
from ai.langgraph.mythic_capability_adapter import _managed_secret_powershell


DOMAIN = "child.lab.local"
HOST = "ws01"
SECRET = "M1-Synthetic-Laps!42"
NTLM = "e19ccf75ee54e06b06a5907af13cef42"

_LAPS_ATTRIBUTES = [
    "ms-Mcs-AdmPwd", "ms-Mcs-AdmPwdExpirationTime", "msLAPS-Password",
    "msLAPS-EncryptedPassword", "msLAPS-PasswordExpirationTime",
    "distinguishedName", "dNSHostName", "sAMAccountName",
]
_MANAGED_PRODUCER = {
    "domain_controller": f"dc01.{DOMAIN}",
    "search_base": "DC=child,DC=lab,DC=local",
    "attributes": _LAPS_ATTRIBUTES,
}


def _powerpick_script(host=HOST, domain=DOMAIN):
    return _managed_secret_powershell(
        f"dc01.{domain}", ",".join(f"DC={part}" for part in domain.split(".")),
        host, domain, _LAPS_ATTRIBUTES,
    )


def _row(task_id, command, params, output, **extra):
    row = {
        "operation_id": 7, "display_id": task_id, "completed": True, "status": "completed",
        "callback_display_id": 4, "callback": {"display_id": 4, "host": f"{HOST}.{DOMAIN}", "domain": DOMAIN},
        "command_name": command,
        "original_params": params if isinstance(params, str) else json.dumps(params),
        "output": output,
    }
    row.update(extra)
    return row


def _secret_row(task_id=11, **extra):
    return _row(task_id, "powerpick", _powerpick_script(),
                f"distinguishedName=CN={HOST},CN=Computers,DC=child,DC=lab,DC=local\n"
                f"dNSHostName={HOST}.{DOMAIN}\nms-Mcs-AdmPwd={SECRET}", **extra)


def _secret(rows, **extra):
    extra.setdefault("producer_binding", _MANAGED_PRODUCER)
    return credential_effects.managed_local_admin_secret_candidate(
        rows, operation_id=7, callback_id=4, target_host=HOST, target_domain=DOMAIN,
        subject="Administrator", baseline_task_ids={10}, **extra,
    )


def test_managed_secret_accepts_current_powershell_and_sharpview_shapes():
    result = _secret([_secret_row()])
    assert result is not None and result.target == f"{HOST}@{DOMAIN}"
    assert result.subject == f"administrator@{HOST}"
    assert "attribute-ms-mcs-admpwd" in result.observation_id

    arguments = (
        "Get-DomainComputer -Identity ws01.child.lab.local -Domain child.lab.local "
        "-Server dc01.child.lab.local -SearchBase DC=child,DC=lab,DC=local "
        "-Properties ms-Mcs-AdmPwd,msLAPS-Password,dNSHostName,distinguishedName -FindOne"
    )
    sharpview = _row(11, "invoke-assembly", {"assembly": "SharpView.exe", "arguments": arguments},
                     f"dNSHostName : {HOST}.{DOMAIN}\nmsLAPS-Password : {{\"n\":\"Administrator\",\"p\":\"{SECRET}\"}}")
    assert _secret([sharpview]) is not None


def test_managed_secret_powerpick_requires_exact_evaluator_reconstruction():
    row = _secret_row()
    assert credential_effects.recognized_managed_secret_request(row, HOST, DOMAIN) is False
    assert credential_effects.managed_local_admin_secret_candidate(
        [row], operation_id=7, callback_id=4, target_host=HOST,
        target_domain=DOMAIN, baseline_task_ids={10},
    ) is None

    actual = _powerpick_script()
    row["original_params"] = "$quoted=@'\n" + actual + "\n'@;Write-Output 'echoed fields'"
    assert credential_effects.recognized_managed_secret_request(
        row, HOST, DOMAIN, producer_binding=_MANAGED_PRODUCER,
    ) is False
    assert _secret([row]) is None


@pytest.mark.parametrize("mutate", [
    lambda row: row.update(operation_id=8),
    lambda row: row.update(callback_display_id=5),
    lambda row: row.update(completed=False),
    lambda row: row.update(display_id=10),
    lambda row: row.update(command_name="shell", original_params=f"echo {_powerpick_script()}"),
    lambda row: row.update(command_name="unknown-custom"),
    lambda row: row.update(original_params=_powerpick_script("ws010")),
])
def test_managed_secret_rejects_wrong_or_stale_request_binding(mutate):
    row = _secret_row()
    mutate(row)
    assert _secret([row]) is None


@pytest.mark.parametrize("output", [
    "dNSHostName=ws01.child.lab.local\nmsLAPS-EncryptedPassword=synthetic-blob",
    "dNSHostName=ws01.child.lab.local\nmsLAPS-PasswordExpirationTime=42",
    "dNSHostName=ws01.child.lab.local\nms-Mcs-AdmPwd=PLACEHOLDER",
    f"dNSHostName=ws010.child.lab.local\nms-Mcs-AdmPwd={SECRET}",
    f"PASSES=true\nmanaged_local_admin_secret_present=true\ndNSHostName={HOST}.{DOMAIN}",
])
def test_managed_secret_requires_target_matched_plaintext(output):
    assert _secret([_row(11, "powerpick", _powerpick_script(), output)]) is None


def test_managed_secret_rejects_near_domain_search_base():
    assert _secret([_row(11, "powerpick", _powerpick_script(domain="near.child.lab.local"),
                         f"dNSHostName={HOST}.{DOMAIN}\nms-Mcs-AdmPwd={SECRET}")]) is None


def _token(task_id=11, *, credential_id="91", password=SECRET, credential_type="plaintext",
           output="created netonly token", **extra):
    return _row(task_id, "make_token", {
        "credential": {"id": credential_id, "account": "Administrator", "realm": HOST,
                       "credential": password, "type": credential_type},
        "netOnly": True,
    }, output, **extra)


def _listing(task_id=12, *, host=f"{HOST}.{DOMAIN}", output="", **extra):
    output = output or (rf"Directory of \\{host}\C$" + "\n<DIR> Windows")
    return _row(task_id, "ls", {"path": rf"\\{host}\C$"},
                output, **extra)


def _access(rows, **extra):
    extra.setdefault("managed_secret_producer_binding", _MANAGED_PRODUCER)
    return credential_effects.managed_local_admin_access_candidate(
        rows, operation_id=7, callback_id=4, target_host=HOST, target_domain=DOMAIN,
        subject="Administrator", baseline_task_ids={10}, **extra,
    )


def test_local_admin_access_accepts_exact_id_or_recovered_value_lineage():
    by_id = _access([_token(), _listing()], credential_id="91")
    assert by_id is not None and "lineage-credential-id" in by_id.observation_id
    by_value = _access([_secret_row(), _token(task_id=12, credential_id=""), _listing(task_id=13)])
    assert by_value is not None and "lineage-recovered-value" in by_value.observation_id

    native = _listing(output='{"host":"WS01.CHILD.LAB.LOCAL","name":"C$","success":true}')
    assert _access([_token(), native], credential_id="91") is not None


@pytest.mark.parametrize("rows,kwargs", [
    (lambda: [_token(), _listing()], {}),
    (lambda: [_token(credential_id="92"), _listing()], {"credential_id": "91"}),
    (lambda: [_listing(task_id=11), _token(task_id=12)], {"credential_id": "91"}),
    (lambda: [_token(extra=False), _listing(callback_display_id=5)], {"credential_id": "91"}),
    (lambda: [_token(), _listing(host="ws010.child.lab.local")], {"credential_id": "91"}),
    (lambda: [_token(password="other-synthetic-secret"), _listing()], {"recovered_secret": SECRET}),
    (lambda: [_token(extra=False), _listing(output=rf"Directory of \\{HOST}.{DOMAIN}\C$\n"
                                                    rf"Directory of \\ws010.{DOMAIN}\C$")],
     {"credential_id": "91"}),
    (lambda: [_token(credential_type="hash"), _listing()], {"credential_id": "91"}),
    (lambda: [_token(extra=False), _listing(task_id=25)], {"credential_id": "91"}),
])
def test_local_admin_access_rejects_missing_wrong_order_callback_target_lineage_or_window(rows, kwargs):
    assert _access(rows(), **kwargs) is None


def test_local_admin_access_rejects_failure_anywhere_in_ordered_window():
    failed_token = _token(output="created token\nLogon failure")
    assert _access([failed_token, _listing()], credential_id="91") is None
    assert _access([_token(), _listing(output=r"Directory of \\ws01.child.lab.local\C$ Access is denied")],
                   credential_id="91") is None
    assert _access([_token(), _row(12, "whoami", {}, "exception: bad"), _listing(task_id=13)],
                   credential_id="91") is None


def _lsass(rows, **extra):
    return credential_effects.host_credential_material_candidate(
        rows, operation_id=7, callback_id=4, target_host=HOST, target_domain=DOMAIN,
        baseline_task_ids={10}, **extra,
    )


def test_lsass_material_requires_exact_callback_host_and_labeled_usable_material():
    output = f"sekurlsa::logonpasswords\nUsername : alice\nDomain : CHILD\n* NTLM : {NTLM}"
    row = _row(11, "mimikatz", {"commands": ["privilege::debug", "sekurlsa::logonpasswords"]}, output)
    result = _lsass([row])
    assert result is not None and "material-ntlm" in result.observation_id
    assert result.target == f"{HOST}@{DOMAIN}"


@pytest.mark.parametrize("mutate", [
    lambda row: row.update(command_name="shell", original_params="echo sekurlsa::logonpasswords"),
    lambda row: row.update(command_name="unknown-custom"),
    lambda row: row["callback"].update(host="ws010.child.lab.local"),
    lambda row: row.update(display_id=10),
    lambda row: row.update(operation_id=8),
])
def test_lsass_rejects_echo_unknown_wrong_host_and_stale(mutate):
    row = _row(11, "mimikatz", {"commands": "sekurlsa::logonpasswords"}, f"NTLM : {NTLM}")
    mutate(row)
    assert _lsass([row]) is None


@pytest.mark.parametrize("output", [
    "credentials dumped successfully",
    "NTLM : REPLACE_ME",
    "NTLM : 00000000000000000000000000000000",
    "NTLM : 31d6cfe0d16ae931b73c59d7e0c089c0",
    "PASSES=true\ncredentials_dumped=true",
])
def test_lsass_rejects_naked_placeholder_degenerate_and_self_report(output):
    assert _lsass([_row(11, "mimikatz", {"commands": "sekurlsa::logonpasswords"}, output)]) is None


def test_observations_never_serialize_credential_material():
    observations = [
        _secret([_secret_row()]),
        _access([_token(), _listing()], credential_id="91"),
        _lsass([_row(11, "mimikatz", {"commands": "sekurlsa::logonpasswords"}, f"NTLM : {NTLM}")]),
    ]
    serialized = json.dumps([item.to_dict() for item in observations if item]).casefold()
    assert SECRET.casefold() not in serialized
    assert NTLM not in serialized


def _account_material(rows, **extra):
    return credential_effects.account_credential_material_candidate(
        rows, operation_id=7, callback_id=4, target_domain=DOMAIN,
        subject=f"alice@{DOMAIN}", baseline_task_ids={10}, **extra,
    )


@pytest.mark.parametrize("row", [
    lambda: _row(11, "dcsync", {"domain": DOMAIN, "user": "alice"},
                 f"Hash NTLM: {NTLM}"),
    lambda: _row(11, "mimikatz", {"commands": f"lsadump::dcsync /domain:{DOMAIN} /user:CHILD\\alice"},
                 f"Object RDN : alice\nHash NTLM: {NTLM}"),
    lambda: _row(11, "invoke-assembly", {
        "assembly": "SharpKatz.exe",
        "arguments": f"--Command dcsync --User CHILD\\alice --Domain {DOMAIN}",
    }, f"Object RDN : alice\naes256_hmac (4096) : {'a1' * 32}"),
])
def test_account_material_accepts_exact_current_dcsync_requests(row):
    result = _account_material([row()])
    assert result is not None
    assert result.target == DOMAIN and result.subject == f"alice@{DOMAIN}"


@pytest.mark.parametrize("row", [
    lambda: _row(11, "dcsync", {"domain": "other.lab.local", "user": "alice"}, f"Hash NTLM: {NTLM}"),
    lambda: _row(11, "dcsync", {"domain": DOMAIN, "user": "alice2"}, f"Hash NTLM: {NTLM}"),
    lambda: _row(11, "dcsync", {"domain": DOMAIN, "user": "krbtgt"}, f"Hash NTLM: {NTLM}"),
    lambda: _row(11, "shell", f"echo dcsync /domain:{DOMAIN} /user:alice", f"Hash NTLM: {NTLM}"),
    lambda: _row(11, "mimikatz", {"commands": f"lsadump::dcsync /domain:{DOMAIN} /user:CHILD\\alice"},
                 f"Object RDN : bob\nHash NTLM: {NTLM}"),
    lambda: _row(10, "dcsync", {"domain": DOMAIN, "user": "alice"}, f"Hash NTLM: {NTLM}"),
])
def test_account_material_rejects_wrong_domain_account_krbtgt_echo_output_and_stale(row):
    assert _account_material([row()]) is None


def test_account_material_never_serializes_secret():
    observation = _account_material([_row(
        11, "dcsync", {"domain": DOMAIN, "user": "alice"}, f"Hash NTLM: {NTLM}",
    )])
    assert observation is not None and NTLM not in json.dumps(observation.to_dict())
