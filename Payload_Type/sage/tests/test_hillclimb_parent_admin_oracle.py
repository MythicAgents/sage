"""Offline parent-domain administrator observation from retained certificate SACL evidence."""
from __future__ import annotations

from ai.hillclimb import effect_registry
from ai.hillclimb.range_state import Milestone
from ai.hillclimb.trust_walker_outcome import domain_admin_differential_candidate, domain_admin_observation


_SOURCE = "\n".join((
    "System.DirectoryServices.Protocols.SecurityDescriptorFlagControl",
    "[System.DirectoryServices.Protocols.SecurityMasks]::Sacl",
    "[System.DirectoryServices.Protocols.AuthType]::External",
    ".ClientCertificates.Add(", "CERT_AUTH_WHOAMI=", "DA_SACL_STATUS=",
    "CONTROL_SACL_STATUS=", "CERT_AUTH_MEMBER_OF=", "CERT_AUTH_STATUS=",
))


def _row(**changes):
    row = {
        "operation_id": 8, "display_id": 54, "command_name": "powerpick",
        "original_params": _SOURCE, "completed": True,
    }
    row.update(changes)
    return row


def _output(target="sevenkingdoms.local", admin="SEVENKINGDOMS\\Administrator",
            control="SEVENKINGDOMS\\regular.user"):
    dc = ",".join(f"DC={part}" for part in target.split("."))
    return f"""CERT_AUTH_METHOD=schannel-ldap
CERT_AUTH_DOMAIN={target}
CERT_AUTH_ACCOUNT=administrator
CERT_AUTH_TRANSPORT=ldaps
CERT_AUTH_PORT=636
CERT_AUTH_LDAP_BIND=True
CERT_AUTH_WHOAMI=u:{admin}
CERT_AUTH_STATUS=OK
CERT_AUTH_USER_DN=CN=Administrator,CN=Users,{dc}
DA_ONLY_READ_OP=domain-sacl
DA_SACL_STATUS=sacl
DA_SACL_ACE_COUNT=8
CONTROL_BIND=True
CONTROL_WHOAMI=u:{control}
CONTROL_SACL_STATUS=no-sd
CONTROL_SACL_ACE_COUNT=-1
CERT_AUTH_MEMBER_OF=CN=Domain Admins,CN=Users,{dc}
"""


def _observation(effect="DA_PARENT", output=None, target="sevenkingdoms.local", **extra):
    return domain_admin_observation(
        _row(**extra), output if output is not None else _output(target),
        effect=effect, effect_at="2026-09-18T12:00:00+00:00", operation_id=8,
        target=target, netbios_domains={
            "SEVENKINGDOMS": "sevenkingdoms.local",
            "NORTH": "north.sevenkingdoms.local",
            "ESSOS": "essos.local",
        },
    )


def test_parent_effect_returns_registry_bound_typed_differential():
    observation = _observation()
    assert isinstance(observation, effect_registry.OracleObservation)
    assert observation.oracle_id == "mythic.task.parent-domain-admin-differential.v1"
    assert observation.proof_kind == "domain_admin_differential"
    assert observation.target == "sevenkingdoms.local"
    assert observation.subject == "administrator@sevenkingdoms.local"
    assert observation.negative_control_denied is True
    assert observation.control_subject == "regular.user@sevenkingdoms.local"
    assert observation.control_envelope_id == observation.envelope_id


def test_parent_effect_rejects_wrong_realm_missing_control_and_model_pass_fields():
    assert _observation(output=_output("north.sevenkingdoms.local")) is None
    assert _observation(output=_output().replace("CONTROL_WHOAMI=u:SEVENKINGDOMS\\regular.user\n", "")) is None
    assert _observation(output="DA_ONLY_READ_PROVEN=True\nCERT_AUTH_DOMAIN_ADMIN=True\nPASSES=True\n") is None


def test_objective_and_child_effects_preserve_shared_observation_semantics():
    objective = _observation(
        effect=Milestone.OBJECTIVE, target="essos.local",
        output=_output("essos.local", "ESSOS\\Administrator", "NORTH\\samwell.tarly"),
    )
    assert objective is not None
    assert objective.oracle_id == effect_registry.contract_for(Milestone.OBJECTIVE).oracle_id
    assert objective.control_subject == "samwell.tarly@north.sevenkingdoms.local"

    child = _observation(
        effect=Milestone.DA_CHILD, target="north.sevenkingdoms.local",
        output=_output("north.sevenkingdoms.local", "NORTH\\Administrator", "NORTH\\samwell.tarly"),
    )
    assert child is not None
    assert child.oracle_id == effect_registry.contract_for(Milestone.DA_CHILD).oracle_id

    candidate = domain_admin_differential_candidate(
        _row(), _output(), milestone="DA_PARENT", effect_at=None,
        operation_id=8, target="sevenkingdoms.local",
        netbios_domains={"SEVENKINGDOMS": "sevenkingdoms.local"},
    )
    assert candidate is not None and candidate["proof_record"]["disposition"] == "proven"
