"""Offline producer probes for child-domain administrative differential evidence."""
from __future__ import annotations

import pytest

from ai.hillclimb.range_state import Milestone
from ai.hillclimb.trust_walker_outcome import domain_admin_differential_candidate


_SOURCE = "\n".join((
    "System.DirectoryServices.Protocols.SecurityDescriptorFlagControl",
    "[System.DirectoryServices.Protocols.SecurityMasks]::Sacl",
    "[System.DirectoryServices.Protocols.AuthType]::External",
    ".ClientCertificates.Add(",
    "CERT_AUTH_WHOAMI=", "DA_SACL_STATUS=", "CONTROL_SACL_STATUS=",
    "CERT_AUTH_MEMBER_OF=", "CERT_AUTH_STATUS=",
))
_TARGET = "north.sevenkingdoms.local"
_DOMAINS = {"NORTH": _TARGET, "ESSOS": "essos.local"}


def _task(**changes):
    row = {
        "operation_id": 8,
        "display_id": 54,
        "command_name": "powerpick",
        "display_params": _SOURCE,
        "original_params": _SOURCE,
        "completed": True,
    }
    row.update(changes)
    return row


def _output(**changes):
    fields = {
        "CERT_AUTH_METHOD": "schannel-ldap",
        "CERT_AUTH_DOMAIN": _TARGET,
        "CERT_AUTH_ACCOUNT": "administrator",
        "CERT_AUTH_TRANSPORT": "ldaps",
        "CERT_AUTH_PORT": "636",
        "CERT_AUTH_LDAP_BIND": "True",
        "CERT_AUTH_WHOAMI": r"u:NORTH\Administrator",
        "CERT_AUTH_STATUS": "OK",
        "CERT_AUTH_USER_DN": "CN=Administrator,CN=Users,DC=north,DC=sevenkingdoms,DC=local",
        "DA_ONLY_READ_OP": "domain-sacl",
        "DA_SACL_STATUS": "sacl",
        "DA_SACL_ACE_COUNT": "8",
        "CONTROL_BIND": "True",
        "CONTROL_WHOAMI": r"u:NORTH\samwell.tarly",
        "CONTROL_SACL_STATUS": "no-sd",
        "CONTROL_SACL_ACE_COUNT": "-1",
        "CERT_AUTH_MEMBER_OF": "CN=Domain Admins,CN=Users,DC=north,DC=sevenkingdoms,DC=local",
    }
    fields.update(changes)
    return "\n".join(f"{name}={value}" for name, value in fields.items()) + "\n"


def _candidate(row=None, output=None, *, target=_TARGET, milestone=Milestone.DA_CHILD):
    return domain_admin_differential_candidate(
        row or _task(), output or _output(), milestone=milestone,
        effect_at="2026-09-18T12:00:00+00:00", operation_id=8,
        target=target, netbios_domains=_DOMAINS,
    )


def test_valid_child_domain_admin_differential_is_admitted():
    candidate = _candidate()

    assert candidate is not None
    proof = candidate["proof_record"]
    assert proof["milestone"] == "DA_CHILD"
    assert proof["disposition"] == "proven"
    assert proof["observation"]["target"] == _TARGET
    assert proof["observation"]["control_subject"] == f"samwell.tarly@{_TARGET}"


def test_objective_wrapper_semantics_remain_available_through_general_producer():
    candidate = _candidate(milestone=Milestone.OBJECTIVE, target="essos.local", output=_output(
        CERT_AUTH_DOMAIN="essos.local",
        CERT_AUTH_WHOAMI=r"u:ESSOS\Administrator",
        CERT_AUTH_USER_DN="CN=Administrator,CN=Users,DC=essos,DC=local",
        CERT_AUTH_MEMBER_OF="CN=Domain Admins,CN=Users,DC=essos,DC=local",
        CONTROL_WHOAMI=r"u:NORTH\samwell.tarly",
    ))

    assert candidate is not None
    assert candidate["proof_record"]["milestone"] == "OBJECTIVE"


@pytest.mark.parametrize(
    ("row", "output", "target"),
    [
        (_task(operation_id=9), _output(), _TARGET),
        (_task(), _output(CERT_AUTH_DOMAIN="north.sevenkingdoms.local.evil"), _TARGET),
        (_task(), _output(), "north.sevenkingdoms.local.evil"),
        (_task(), _output(CONTROL_WHOAMI=r"u:NORTH\Administrator"), _TARGET),
        (_task(), _output(CONTROL_SACL_STATUS="sacl"), _TARGET),
        (_task(), _output(CONTROL_WHOAMI=r"u:ESSOS\samwell.tarly"), _TARGET),
    ],
    ids=("wrong-operation", "suffix-in-output", "suffix-target", "same-principal", "control-succeeds", "different-control-domain"),
)
def test_child_domain_near_matches_never_prove(row, output, target):
    assert _candidate(row, output, target=target) is None


def test_duplicate_single_value_field_and_generated_flags_cannot_prove():
    assert _candidate(output=_output() + "CERT_AUTH_STATUS=OK\n") is None
    generated_only = "\n".join((
        "DA_ONLY_READ_PROVEN=True",
        "CERT_AUTH_DOMAIN_ADMIN=True",
        "CERT_AUTH_STATUS=OK",
    ))
    assert _candidate(output=generated_only) is None


def test_only_objective_and_child_domain_milestones_are_supported():
    assert _candidate(milestone=Milestone.SYSTEM_ON_HOST) is None
