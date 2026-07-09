import importlib.util
from pathlib import Path

import pytest


SCRIPT = (
    Path(__file__).resolve().parents[3]
    / "skills"
    / "sage-goad-reset"
    / "scripts"
    / "check_cross_forest_laps_bridge.py"
)
SPEC = importlib.util.spec_from_file_location("check_cross_forest_laps_bridge", SCRIPT)
check_cross_forest_laps_bridge = importlib.util.module_from_spec(SPEC)
assert SPEC and SPEC.loader
SPEC.loader.exec_module(check_cross_forest_laps_bridge)


def test_build_bridge_query_matches_reconciler_shape_without_sync_laps():
    query = check_cross_forest_laps_bridge.build_bridge_query(
        "MATRIXGROUP.LOCAL",
        "VERTEXVENTURES.LOCAL",
    )

    assert "MATCH (u:User)-[:MemberOf*0..4]->(reader)-[e]->(c:Computer)" in query
    assert "toLower(u.name) ENDS WITH '@vertexventures.local'" in query
    assert "toLower(coalesce(c.domain, '')) = 'matrixgroup.local'" in query
    assert "'ReadLAPSPassword'" in query
    assert "'ReadLAPS'" in query
    assert "'ReadMSLAPSPassword'" in query
    assert "SyncLAPSPassword" not in query


def test_check_bridge_reports_cross_forest_managed_secret_path():
    result = check_cross_forest_laps_bridge.check_bridge(
        "matrixgroup.local",
        source_domain="vertexventures.local",
        query_runner=lambda query: {
            "data": {
                "literals": [
                    {
                        "key": "name",
                        "value": (
                            "DOROTHY.GONZALEZ@VERTEXVENTURES.LOCAL|"
                            "PLATFORM@MATRIXGROUP.LOCAL|"
                            "CITADEL.MATRIXGROUP.LOCAL|"
                            "ReadLAPSPassword"
                        ),
                    }
                ]
            }
        },
    )

    assert result["ready"] is True
    assert result["bridge_count"] == 1
    assert result["bridges"] == [
        {
            "user": "DOROTHY.GONZALEZ@VERTEXVENTURES.LOCAL",
            "reader": "PLATFORM@MATRIXGROUP.LOCAL",
            "computer": "CITADEL.MATRIXGROUP.LOCAL",
            "edge_type": "ReadLAPSPassword",
        }
    ]


def test_check_bridge_fails_closed_when_graph_has_no_matching_path():
    result = check_cross_forest_laps_bridge.check_bridge(
        "matrixgroup.local",
        query_runner=lambda query: {"data": {"literals": []}},
    )

    assert result["ready"] is False
    assert result["bridge_count"] == 0
    assert "no cross-forest ReadLAPSPassword bridge" in result["reason"]


def test_normalize_domain_rejects_cypher_injection_characters():
    with pytest.raises(ValueError, match="invalid domain"):
        check_cross_forest_laps_bridge.normalize_domain("matrixgroup.local' OR 1=1")
