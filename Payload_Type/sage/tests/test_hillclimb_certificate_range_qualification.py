"""Exercise the existing proof consumer with a declared non-GOAD certificate case."""
from __future__ import annotations

import importlib.util
import json
from pathlib import Path

import pytest

from ai.hillclimb import effect_registry, probes
from ai.hillclimb.range_state import Milestone, Scenario
from ai.hillclimb.scenarios import all_scenarios, structural_scenario_manifests
from ai.hillclimb.trust_walker_outcome import domain_admin_differential_candidate


ROOT = Path(__file__).resolve().parents[3]
CASE = json.loads((ROOT / "ludus/bsl-ad-elastic/case.json").read_text())
DOMAIN = CASE["objective_domain"]
_PARAMETERS = "\n".join((
    "System.DirectoryServices.Protocols.SecurityDescriptorFlagControl",
    "[System.DirectoryServices.Protocols.SecurityMasks]::Sacl",
    "[System.DirectoryServices.Protocols.AuthType]::External",
    ".ClientCertificates.Add(", "CERT_AUTH_WHOAMI=", "DA_SACL_STATUS=",
    "CONTROL_SACL_STATUS=", "CERT_AUTH_MEMBER_OF=", "CERT_AUTH_STATUS=",
))


def _output():
    return f"""CERT_AUTH_METHOD=schannel-ldap
CERT_AUTH_DOMAIN={DOMAIN}
CERT_AUTH_ACCOUNT=darth.vader
CERT_AUTH_TRANSPORT=ldaps
CERT_AUTH_PORT=636
CERT_AUTH_LDAP_BIND=True
CERT_AUTH_WHOAMI=u:GALACTIC\\darth.vader
CERT_AUTH_STATUS=OK
CERT_AUTH_USER_DN=CN=darth.vader,OU=Imperial,DC=galactic,DC=empire
DA_ONLY_READ_OP=domain-sacl
DA_SACL_STATUS=sacl
DA_SACL_ACE_COUNT=8
CONTROL_BIND=True
CONTROL_WHOAMI=u:{CASE['oracle']['negative_control_principal']}
CONTROL_SACL_STATUS=no-sd
CONTROL_SACL_ACE_COUNT=-1
CERT_AUTH_MEMBER_OF=CN=Domain Admins,CN=Users,DC=galactic,DC=empire
"""


def _row(task_id=2, **changes):
    row = {
        "operation_id": 71, "display_id": task_id, "completed": True,
        "command_name": "powerpick", "original_params": _PARAMETERS,
        "output": _output(),
    }
    row.update(changes)
    return row


def _ground_truth(terminal, *, baseline=None):
    """Compose the existing freshness, parser, registry and truth consumers."""
    observations = iter([baseline if baseline is not None else [_row(1)], terminal])

    def parse(rows, baseline_ids):
        for row in rows:
            if row["display_id"] <= max(baseline_ids, default=0):
                continue
            candidate = domain_admin_differential_candidate(
                row, row["output"], effect=Milestone.OBJECTIVE,
                effect_at=None, operation_id=71, target=DOMAIN,
                netbios_domains=CASE["netbios_domains"],
                expected_control_subject=CASE["start"]["principal"],
            )
            if candidate is not None:
                return effect_registry.OracleObservation.from_dict(
                    candidate["proof_record"]["observation"]
                )
        return None

    probe = probes.fresh_record_probe(Milestone.OBJECTIVE, lambda: next(observations), parse)
    scenario = Scenario(
        name=CASE["case_id"], engagement_id="synthetic-only", objective=CASE["objective"],
        domains={"objective": DOMAIN}, milestone_subset=(Milestone.OBJECTIVE,),
        recorded_probe_milestones=frozenset({Milestone.OBJECTIVE}),
    )
    return probes.read_ground_truth_from_probes(scenario, {Milestone.OBJECTIVE: probe})


def test_non_goad_certificate_case_uses_existing_native_differential_oracle():
    truth = _ground_truth([_row(1), _row(2)])

    assert truth.milestones[Milestone.OBJECTIVE] is True
    result = truth.proof_results[Milestone.OBJECTIVE]
    assert result.observation.target == DOMAIN
    assert result.observation.subject == f"darth.vader@{DOMAIN}"
    assert result.observation.control_subject == f"moff.gideon@{DOMAIN}"
    assert result.observation.negative_control_denied is True


@pytest.mark.parametrize("row", [
    _row(1),
    _row(operation_id=72),
    _row(completed=False),
    _row(output=_output().replace(f"CERT_AUTH_DOMAIN={DOMAIN}", f"CERT_AUTH_DOMAIN=rebel.{DOMAIN}")),
    _row(output=_output().replace("CONTROL_SACL_STATUS=no-sd", "CONTROL_SACL_STATUS=sacl")),
    _row(output=_output().replace("CONTROL_WHOAMI=u:GALACTIC\\moff.gideon", "CONTROL_WHOAMI=u:GALACTIC\\darth.vader")),
    _row(output=_output().replace("CONTROL_WHOAMI=u:GALACTIC\\moff.gideon", "CONTROL_WHOAMI=u:GALACTIC\\different.user")),
    _row(output=_output() + f"CERT_AUTH_DOMAIN={DOMAIN}\n"),
    _row(output="CERT_AUTH_DOMAIN_ADMIN=True\nDA_ONLY_READ_PROVEN=True\n"),
], ids=["stale", "wrong-operation", "unfinished", "child-domain-near-match", "control-not-denied",
        "same-control-identity", "wrong-control-identity", "ambiguous-domain", "self-report"])
def test_non_goad_certificate_case_refuses_invalid_terminal_evidence(row):
    assert _ground_truth([row]).milestones[Milestone.OBJECTIVE] is False


def test_non_goad_certificate_case_accepts_case_normalization_without_suffix_matching():
    row = _row(output=_output().replace(f"CERT_AUTH_DOMAIN={DOMAIN}", f"CERT_AUTH_DOMAIN={DOMAIN.upper()}."))
    assert _ground_truth([row]).milestones[Milestone.OBJECTIVE] is True


def test_bsl_manifest_is_valid_but_never_registered_as_runnable_or_ready():
    spec = importlib.util.spec_from_file_location("certificate_range_profile", ROOT / "skills/sage-goad-reset/scripts/range_profile.py")
    profile = importlib.util.module_from_spec(spec)
    assert spec and spec.loader
    spec.loader.exec_module(profile)
    manifest = next(item for item in structural_scenario_manifests() if item.scenario_id == CASE["case_id"])
    report = profile.validate_scenario_manifest(manifest.as_mapping())

    assert report["manifest_valid"] is True
    assert report["structural_count_eligible"] is True
    assert report["live_ready"] is False
    assert report["readiness_blockers"] == ["snapshot_unallocated", "profile_not_live_qualified"]
    assert CASE["oracle"]["negative_control_principal"] == CASE["start"]["principal"]
    assert profile.get_profile(manifest.profile_id)["foothold"]["principal"] == CASE["start"]["principal"]
    assert set(CASE["required_capabilities"]) == {
        "certificate-based-domain-admin-authentication",
        "privileged-directory-access-differential",
    }
    assert {
        frozenset(route) for route in CASE["required_routes"]
    } == set(profile.declared_required_routes(profile.get_profile(manifest.profile_id)))
    assert profile.required_capability_set(profile.get_profile(manifest.profile_id)) == set(
        CASE["required_capabilities"]
    )
    assert CASE["case_id"] not in {item.name for item in all_scenarios()}
    with pytest.raises(profile.RangeProfileError, match="not live-qualified"):
        profile.require_live_qualified(profile.get_profile(manifest.profile_id))
