import importlib.util
import json
from pathlib import Path

import pytest


SCRIPTS = Path(__file__).resolve().parents[3] / "skills/sage-eval-gauge/scripts"


def load(name):
    spec = importlib.util.spec_from_file_location(name, SCRIPTS / (name + ".py"))
    value = importlib.util.module_from_spec(spec); spec.loader.exec_module(value)
    return value


registration = load("capture_secret_registration")
native = load("native_inventory")


def snapshot(secret=None):
    value = {"schema": "sage-m3-native-inventory-v1", "operation_id": 1, "complete": True,
        "failures": [], "snapshot_consistency": "single_operation_root_query",
        "query_hashes": {"operation_snapshot": native.SNAPSHOT_QUERY_SHA256},
        "rows": {table: [] for table in native.TABLE_FIELDS}}
    if secret is not None:
        row = {field: None for field in native.TABLE_FIELDS["credential"]}
        row.update(id=2, operation_id=1, credential=secret)
        value["rows"]["credential"].append(row)
    return value


def test_exact_origins_remain_distinct_and_late_native_secret_is_redacted():
    index = registration.register_secrets({"auth": "operator-example"}, snapshot(), snapshot("runtime-example"))
    result = index.redact_bytes(b"operator-example runtime-example")
    assert result["redacted"] == b"<SAGE_REDACTED> <SAGE_REDACTED>"
    assert "operator-example" not in json.dumps(result["metadata"])
    overlap = registration.register_secrets({"auth": "shared-example"}, snapshot(), snapshot("shared-example"))
    assert {item["category"] for item in overlap.scan(b"shared-example")[0]["identities"]} == {"operator", "runtime"}


def test_missing_inventory_wrong_operation_and_unavailable_native_value_refuse():
    with pytest.raises(registration.SecretRegistrationError):
        registration.register_secrets({}, snapshot(), snapshot())
    end = snapshot(); end["operation_id"] = 2
    with pytest.raises(registration.SecretRegistrationError):
        registration.register_secrets({"auth": "operator-example"}, snapshot(), end)
    with pytest.raises(registration.SecretRegistrationError):
        registration.register_secrets({"auth": "operator-example"}, snapshot(), snapshot(""))
