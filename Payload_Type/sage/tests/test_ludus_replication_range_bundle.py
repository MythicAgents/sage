from pathlib import Path

import yaml

from ai.hillclimb import replication_purpose_range


REPO_ROOT = Path(__file__).resolve().parents[3]
BUNDLE_ROOT = REPO_ROOT / "ludus" / "sage-purpose-ranges"
BLUEPRINT_ROOT = BUNDLE_ROOT / "blueprints" / "sage-replication-range"


def _load_yaml(path: Path):
    with path.open("r", encoding="utf-8") as handle:
        return yaml.safe_load(handle)


def test_replication_range_bundle_matches_eval_contract():
    source = _load_yaml(BUNDLE_ROOT / "source.yml")
    blueprint = _load_yaml(BLUEPRINT_ROOT / "blueprint.yml")
    config = _load_yaml(BLUEPRINT_ROOT / "range-config.yml")

    assert source["manifest_version"] == 1
    assert blueprint["id"] == "sage-replication-range"
    assert blueprint["config"] == "range-config.yml"
    assert replication_purpose_range.REPLICATION_PURPOSE_RANGE.source_dir == (
        "ludus/sage-purpose-ranges/blueprints/sage-replication-range"
    )

    vms = config["ludus"]
    assert [vm["hostname"] for vm in vms] == ["dc01", "srv02", "ws01"]
    assert [vm["domain"]["fqdn"] for vm in vms] == ["replication.local"] * 3
    assert [vm["domain"]["role"] for vm in vms] == ["primary-dc", "member", "member"]


def test_replication_range_generalizes_same_template_windows_hosts():
    config = _load_yaml(BLUEPRINT_ROOT / "range-config.yml")
    windows_vms = [vm for vm in config["ludus"] if "windows" in vm]

    assert {vm["template"] for vm in windows_vms} == {"win2022-server-x64-template"}
    assert [vm["windows"]["sysprep"] for vm in windows_vms] == [True, True, True]


def test_replication_range_bundle_ships_only_the_needed_custom_relationships():
    config = _load_yaml(BLUEPRINT_ROOT / "range-config.yml")
    role_text = (BUNDLE_ROOT / "ansible" / "roles" / "sage_replication_range" / "tasks" / "main.yml").read_text(
        encoding="utf-8"
    )
    serialized = yaml.safe_dump(config)

    assert "DreadGOAD" not in serialized
    assert "GOAD" not in serialized
    assert "sage_replication_range" in serialized
    assert "SRV02-Policy" in serialized
    assert "SageProof" in serialized
    assert "1131f6aa-9c07-11d1-f79f-00c04fc2dcd2" in role_text
    assert "1131f6ad-9c07-11d1-f79f-00c04fc2dcd2" in role_text
    assert "Move-ADObject" in role_text


def test_replication_range_bundle_includes_ludus_206_sysprep_guard_patch():
    patch_text = (
        BUNDLE_ROOT / "patches" / "ludus-2.0.6" / "sysprep-appx-fail-closed.patch"
    ).read_text(encoding="utf-8")
    readme_text = (BUNDLE_ROOT / "README.md").read_text(encoding="utf-8")

    assert "Microsoft.MicrosoftEdge.Stable_8wekyb3d8bbwe" in patch_text
    assert "Sysprep_succeeded.tag" in patch_text
    assert "Fail if sysprep did not generalize the VM" in patch_text
    assert "sysprep-appx-fail-closed.patch" in readme_text
    assert "range deploy -t vm-deploy,network,assign-ip,sysprep" in readme_text
