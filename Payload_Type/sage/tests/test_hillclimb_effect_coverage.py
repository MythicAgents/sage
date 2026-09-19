import dataclasses
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "ai" / "hillclimb"))
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "ai" / "langgraph"))

import effect_coverage as coverage  # noqa: E402
import engagement_state  # noqa: E402
import capabilities  # noqa: E402
import effect_registry  # noqa: E402


def test_inventory_has_exact_source_parity_and_registry_bindings():
    inventory = coverage.effect_coverage_inventory()
    rows = inventory["rows"]

    assert inventory["coverage_version"] == coverage.COVERAGE_VERSION
    assert {row["name"] for row in rows if row["universe"] == coverage.RUNTIME_CAPABILITY_UNIVERSE} == {
        row["name"] for row in capabilities.capability_catalog()
    }
    assert {row["name"] for row in rows if row["universe"] == coverage.LEGACY_TECHNIQUE_UNIVERSE} == set(
        engagement_state.TECHNIQUE_MODEL
    )
    assert len(rows) == 33
    assert {row["milestone"] for row in inventory["oracles"]} == {
        *effect_registry.validate_registry(),
        *effect_registry.validate_registry(effect_registry.CAPABILITY_EFFECT_REGISTRY),
    }


@pytest.mark.parametrize(
    ("rows", "message"),
    [
        ((), "empty"),
        (coverage.EFFECT_COVERAGE[:-1], "source parity mismatch"),
        (coverage.EFFECT_COVERAGE + (coverage.EFFECT_COVERAGE[0],), "duplicate"),
        (
            (dataclasses.replace(coverage.EFFECT_COVERAGE[0], oracle_id="unknown.oracle"),)
            + coverage.EFFECT_COVERAGE[1:],
            "oracle binding mismatch",
        ),
        (
            coverage.EFFECT_COVERAGE[:-1] + (
                dataclasses.replace(coverage.EFFECT_COVERAGE[-1], milestone="UNKNOWN_EFFECT"),
            ),
            "unknown effect coverage milestone",
        ),
        (
            coverage.EFFECT_COVERAGE[:-1] + (
                dataclasses.replace(coverage.EFFECT_COVERAGE[-1], name="unknown-technique"),
            ),
            "unknown source semantic name",
        ),
    ],
)
def test_validation_rejects_empty_duplicate_unknown_and_invalid_bindings(rows, message):
    with pytest.raises(ValueError, match=message):
        coverage.validate_effect_coverage(rows)


def test_supported_rows_have_real_producers_and_unavailable_rows_do_not_claim_one():
    rows = coverage.validate_effect_coverage()
    supported = [row for row in rows if row.status is coverage.CoverageStatus.SUPPORTED]
    unavailable = [row for row in rows if row.status is not coverage.CoverageStatus.SUPPORTED]

    assert {row.milestone for row in supported} >= {
        "GRAPH_COLLECTED", "SYSTEM_ON_HOST", "KRBTGT_DUMPED", "DCSYNC_PARENT", "OBJECTIVE",
        "DA_CHILD", "CROSS_DOMAIN_CONTEXT", "ACCOUNT_KERBEROS_CONTEXT", "MANAGED_LOCAL_ADMIN_SECRET",
        "MANAGED_LOCAL_ADMIN_ACCESS", "ENDPOINT_PROTECTION_STATE", "CA_KEY_EXPORTED",
        "CERTIFICATE_ENROLLED", "HOST_CREDENTIAL_MATERIAL", "RBCD_RIGHTS",
    }
    assert all(row.producer for row in supported)
    assert all(row.producer is None for row in unavailable)
    assert {row.name for row in rows if row.milestone == "DA_CHILD"} == {
        "gpo-controlled-system-exec", "forge-golden-ticket", "adcs-certificate-auth",
        "domain-admin-membership-check", "golden-ticket",
    }

    oracle_rows = coverage.validate_oracle_coverage()
    assert all(row.producer for row in oracle_rows if row.status is coverage.CoverageStatus.SUPPORTED)
    assert all(row.producer is None for row in oracle_rows if row.status is not coverage.CoverageStatus.SUPPORTED)
