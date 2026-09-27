from __future__ import annotations

import base64
import importlib.util
import json
import os
from pathlib import Path
import stat
import subprocess
import sys
import threading

import pytest


ROOT = Path(__file__).resolve().parents[3]
SOURCE = ROOT / "skills/sage-eval-gauge/scripts/capture_redaction.py"
CHANNEL = ROOT / "Payload_Type/sage/ai/observation_channel.py"
RECEIVER = ROOT / "skills/sage-eval-gauge/scripts/observation_receiver.py"


def _module(path: Path, name: str):
    spec = importlib.util.spec_from_file_location(name, path)
    assert spec and spec.loader
    value = importlib.util.module_from_spec(spec); spec.loader.exec_module(value)
    return value


redaction = _module(SOURCE, "m3_capture_redaction")
receiver_module = _module(RECEIVER, "m3_redaction_receiver")


def test_all_exact_encodings_report_provenance_and_no_raw_values():
    value = 'Op+/"é'.encode()
    index = redaction.SecretIndex(); index.register("opaque-a", value, "operator")
    variants = {
        "raw_utf8": value,
        "utf16le": value.decode().encode("utf-16le"),
        "percent_encoded": b"Op%2B%2F%22%C3%A9",
        "json_escaped": b'Op+/\\"\\u00e9',
        "base64_standard_padded": base64.b64encode(value),
        "base64_urlsafe_padded": base64.urlsafe_b64encode(value),
        "base64_standard_unpadded": base64.b64encode(value).rstrip(b"="),
    }
    for expected, content in variants.items():
        matches = index.scan(b"before:" + content + b":after")
        assert any(identity == {"handle": "opaque-a", "category": "operator", "encoding": expected}
                   for match in matches for identity in match["identities"])
        result = index.redact_bytes(content)
        assert value not in repr(result).encode() and result["redacted"] == b"<SAGE_REDACTED>"


def test_duplicate_overlapping_and_split_values_are_union_redacted():
    index = redaction.SecretIndex()
    index.register("handle-long", b"secret-value", "runtime")
    index.register("handle-overlap", b"value", "operator")
    index.register("handle-duplicate", b"secret-value", "operator")
    content = b"x" + b"secret-" + b"value" + b"y"
    result = index.redact_bytes(content)
    assert result["redacted"] == b"x<SAGE_REDACTED>y"
    handles = {identity["handle"] for match in result["metadata"]["matches"]
               for identity in match["identities"]}
    assert handles == {"handle-long", "handle-overlap", "handle-duplicate"}
    assert "secret-value" not in repr(index) and "secret-value" not in repr(result)


def test_near_match_and_unregistered_material_are_explicitly_unchanged():
    index = redaction.SecretIndex(); index.register("known", "CaseSensitive", "runtime")
    content = b"casesensitive CaseSensitivf UNREGISTERED"
    result = index.redact_bytes(content)
    assert result["redacted"] == content and result["metadata"]["matches"] == []


def test_recursive_keys_values_bytes_and_collisions():
    index = redaction.SecretIndex(); index.register("known", "TOKEN", "runtime")
    result = index.redact_json({"TOKEN-key": [b"TOKEN", {b"TOKEN": "xTOKEN"}]})
    encoded = json.dumps(result, sort_keys=True)
    assert "TOKEN" not in encoded and "sage-redacted-bytes-json-v1" in encoded
    with pytest.raises(redaction.RedactionError, match="collision"):
        index.redact_json({"TOKEN": 1, "<SAGE_REDACTED>": 2})


@pytest.mark.parametrize("handle,value,category", [
    ("", "RAW_SENTINEL", "runtime"), ("bad handle", "RAW_SENTINEL", "runtime"),
    ("ok", "", "runtime"), ("ok", b"", "operator"), ("ok", "secret", "other"),
])
def test_malformed_registrations_reject_without_raw_errors(handle, value, category):
    with pytest.raises(redaction.RedactionError, match="registration is invalid") as error:
        redaction.SecretIndex().register(handle, value, category)
    assert "RAW_SENTINEL" not in repr(error.value)


def test_actual_receiver_redacts_nested_capture_before_fsync(tmp_path):
    os.chmod(tmp_path, 0o700)
    records = tmp_path / "capture.jsonl"
    index = redaction.SecretIndex()
    index.register("operator-secret", "SYNTHETIC_OPERATOR_SECRET", "operator")
    index.register("runtime-secret", "SYNTHETIC/RUNTIME+SECRET", "runtime")
    with receiver_module.ObservationReceiver(
        records, lambda event: index.redact_json(event["payload"]), temporary_parent=tmp_path,
    ) as receiver:
        served = []
        def serve():
            for _ in range(4):
                served.append(receiver.serve_one(5))
        thread = threading.Thread(target=serve); thread.start()
        environment = dict(os.environ, SAGE_OBSERVATION_DIRECTORY=str(receiver.directory))
        program = (
            "import base64,os,sys;sys.path.insert(0,sys.argv[1]);from ai.observation_channel import emit;"
            "a='SYNTHETIC_OPERATOR_SECRET';b='SYNTHETIC/RUNTIME+SECRET';"
            "payload={'nested':{'raw':a,'split':a[:10]+a[10:],"
            "'percent':'SYNTHETIC%2FRUNTIME%2BSECRET',"
            "'base64':base64.b64encode(a.encode()).decode()},'tool':a};"
            "[emit(kind,payload) for kind in ('model_request','control_request','log_record','tool_event')]"
        )
        subprocess.run([sys.executable, "-c", program, str(ROOT / "Payload_Type/sage")], env=environment,
                       check=True, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                       stderr=subprocess.DEVNULL, timeout=60)
        thread.join(timeout=10)
    durable = records.read_bytes()
    assert len(served) == 4 and stat.S_IMODE(records.stat().st_mode) == 0o600
    forbidden = [b"SYNTHETIC_OPERATOR_SECRET", b"SYNTHETIC/RUNTIME+SECRET",
                 b"SYNTHETIC%2FRUNTIME%2BSECRET", base64.b64encode(b"SYNTHETIC_OPERATOR_SECRET")]
    assert all(value not in durable for value in forbidden)
    assert durable.count(b"<SAGE_REDACTED>") >= 20
