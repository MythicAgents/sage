import sys
from pathlib import Path

import pytest


sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "ai" / "langgraph"))
import footprint  # noqa: E402


def test_whoami_minimal_schema_scores_zero():
    result = footprint.footprint("whoami", {}, [])

    assert result["axes"] == {axis: 0 for axis in footprint.AXES}
    assert result["total"] == 0
    assert isinstance(result["rationale"], list)


def test_jump_wmi_payload_selector_and_host_scores_beacon_lateral_network():
    schema = [
        {"name": "Payload", "type": "ChooseOne"},
        {"name": "host", "type": "String"},
    ]

    result = footprint.footprint("jump_wmi", {"host": "workstation01"}, schema)

    assert result["axes"]["new_beacon"] == 3
    assert result["axes"]["lateral_hop"] == 3
    assert result["axes"]["network_signature"] >= 2
    assert result["axes"]["reversibility"] >= 2


def test_inline_assembly_with_assembly_file_and_sharphound_scores_artifact_tool_process():
    schema = [
        {
            "name": "assembly_file",
            "cli_name": "assembly_file",
            "type": "File",
            "parameter_group_name": "New-Assembly",
        }
    ]
    params = {"assembly_file": "C:\\Temp\\SharpHound.exe"}

    result = footprint.footprint("inline_assembly", params, schema)

    assert result["axes"]["disk_artifact"] >= 2
    assert result["axes"]["flagged_tool"] >= 1
    assert result["axes"]["new_process"] >= 2


def test_registry_persistence_scores_max_reversibility():
    result = footprint.footprint(
        "registry_persist",
        {"run_key": "HKCU\\Software\\Microsoft\\Windows\\CurrentVersion\\Run"},
        [{"name": "run_key", "type": "String"}],
    )

    assert result["axes"]["reversibility"] == 3


def test_make_token_reversibility_zero_low_total():
    result = footprint.footprint("make_token", {"username": "alice"}, [])

    assert result["axes"]["reversibility"] == 0
    assert result["total"] <= 1


def test_load_technique_map_guardrails_and_clean_entry():
    with pytest.raises(ValueError):
        footprint.load_technique_map({"WINTERFELL": {"axes": {}, "note": "invalid key"}})

    with pytest.raises(ValueError):
        footprint.load_technique_map(
            {
                "t1003": {
                    "axes": {"disk_artifact": 1},
                    "note": "north.sevenkingdoms.local",
                }
            }
        )

    with pytest.raises(ValueError):
        footprint.load_technique_map({"apollo": {"axes": {}, "note": "agent key"}})

    clean = {
        "T1003.001": {
            "axes": {"disk_artifact": 1},
            "note": "credential dumping",
        }
    }
    assert footprint.load_technique_map(clean) == clean


def test_footprint_fail_soft_on_malformed_schema():
    result = footprint.footprint("whoami", {}, ["not a schema dict"])

    assert result["axes"] == {axis: 0 for axis in footprint.AXES}
    assert result["total"] == 0
    assert result["rationale"]
