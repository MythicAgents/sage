"""Focused offline coverage for version-bound executable argument contracts."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path

import pytest

from ai.langgraph.executable_contracts import (
    ContractValidationError,
    ExecutableIdentityError,
    UnsupportedExecutableError,
    load_contract_for_artifact,
    load_executable_contract,
    parse_windows_command_line,
    render_windows_command_line,
    windows_quote_argument,
)


SAGE_ROOT = Path(__file__).resolve().parents[1]
CERTIFY_CONTRACT = SAGE_ROOT / "tool_contracts" / "certify.json"
CERTIFY_SHA256 = "1383e8bca90e4be70ff2224b7e2af465a8f3d5bede1fde60f1ece71e3b02d346"


@pytest.fixture
def certify():
    return load_executable_contract(CERTIFY_CONTRACT, CERTIFY_SHA256)


def _synthetic_descriptor(sha256: str) -> dict:
    return {
        "contract_version": 7,
        "binary": {"filename": "unrelated-cli.exe", "sha256": sha256},
        "registered_verbs": [
            {"name": "inspect", "options_type": "Other.Options", "enrolled": True},
            {"name": "compiled-out", "enrolled": True},
        ],
        "disabled_registered_verbs": ["compiled-out"],
        "legacy_unregistered_verbs": [],
        "global_options": {"quiet": {"type": "boolean"}},
        "verbs": {
            "inspect": {
                "required": ["mode"],
                "options": {
                    "mode": {"type": "string", "enum": ["brief", "full"]},
                    "tag": {"type": "string", "value_shape": "sequence"},
                    "limit": {"type": "integer", "default": 10},
                    "source": {"type": "string"},
                    "confirm": {"type": "boolean", "requires": ["source"]},
                },
            }
        },
    }


def _write_descriptor(tmp_path: Path, descriptor: dict, name: str = "contract.json") -> Path:
    path = tmp_path / name
    path.write_text(json.dumps(descriptor), encoding="utf-8")
    return path


def _assert_rejected(callback, *needles: str):
    with pytest.raises((ContractValidationError, UnsupportedExecutableError, ExecutableIdentityError)) as caught:
        callback()
    message = str(caught.value).lower()
    for needle in needles:
        assert needle.lower() in message


def test_loads_only_the_exact_certify_identity_and_four_enrolled_verbs(certify, tmp_path):
    assert certify.filename == "Certify.exe"
    assert certify.enrolled_verbs == ("enum-cas", "enum-templates", "request", "request-download")
    renamed = _write_descriptor(tmp_path, json.loads(CERTIFY_CONTRACT.read_text(encoding="utf-8")))
    assert load_executable_contract(renamed, CERTIFY_SHA256).filename == "Certify.exe"

    _assert_rejected(
        lambda: load_executable_contract(CERTIFY_CONTRACT, "0" * 64), "sha-256", "does not match"
    )
    _assert_rejected(
        lambda: load_executable_contract(CERTIFY_CONTRACT, "Certify.exe"), "lowercase sha-256"
    )


def test_hash_lookup_never_uses_a_filename_or_returns_unknown_artifacts(tmp_path):
    artifact_sha256 = hashlib.sha256(b"unrelated binary bytes").hexdigest()
    _write_descriptor(tmp_path, _synthetic_descriptor(artifact_sha256), "renamed-description.json")
    contract = load_contract_for_artifact(tmp_path, artifact_sha256)
    assert contract.filename == "unrelated-cli.exe"
    _assert_rejected(lambda: load_contract_for_artifact(tmp_path, hashlib.sha256(b"other").hexdigest()), "no enrolled")


def test_per_verb_models_and_json_schemas_have_explicit_typed_options(certify):
    schema = certify.schema_for_verb("request-download")
    assert schema["additionalProperties"] is False
    assert set(schema["properties"]) == {"verb", "options"}
    assert schema["properties"]["verb"]["const"] == "request-download"
    options_name = schema["properties"]["options"]["$ref"].rsplit("/", 1)[-1]
    options = schema["$defs"][options_name]
    assert options["additionalProperties"] is False
    assert options["properties"]["id"] == {
        "maximum": 2147483647,
        "minimum": -2147483648,
        "title": "Id",
        "type": "integer",
    }
    assert options["properties"]["install-machine"]["type"] == "boolean"
    assert "additionalProperties" not in options["properties"]
    assert "arbitrary" not in json.dumps(schema).lower()


@pytest.mark.parametrize(
    "verb, supplied, expected",
    [
        ("enum-cas", {"filter-vulnerable": True}, "enum-cas --filter-vulnerable"),
        (
            "enum-templates",
            {"filter-enabled": True, "quiet": True},
            "enum-templates --quiet --filter-enabled",
        ),
        (
            "request",
            {"ca": r"SERVER\CA", "template": "User", "output-pem": True},
            r"request --ca SERVER\CA --template User --key-size 2048 --output-pem",
        ),
        (
            "request-download",
            {"ca": r"SERVER\CA", "id": 42, "install-user": True},
            r"request-download --ca SERVER\CA --id 42 --install-user",
        ),
    ],
)
def test_prepares_each_enrolled_verb_and_applies_declared_defaults(certify, verb, supplied, expected):
    prepared = certify.prepare(verb, supplied, artifact_sha256=CERTIFY_SHA256)
    assert prepared.argument_string == expected
    assert parse_windows_command_line(prepared.argument_string) == prepared.argv
    assert certify.revalidate(prepared.as_binding(), artifact_sha256=CERTIFY_SHA256) == prepared


@pytest.mark.parametrize("verb", ["find", "cas", "download", "forge", "request-agent", "anything"])
def test_unregistered_and_unenrolled_verbs_fail_closed(certify, verb):
    _assert_rejected(lambda: certify.prepare(verb, {}, artifact_sha256=CERTIFY_SHA256), "not enrolled")


def test_unsupported_verb_lists_only_this_artifacts_enrolled_verbs(certify, tmp_path):
    with pytest.raises(UnsupportedExecutableError) as caught:
        certify.prepare("find", {}, artifact_sha256=CERTIFY_SHA256)
    assert str(caught.value) == (
        "executable verb 'find' is not enrolled; enrolled verbs for this artifact: "
        "enum-cas, enum-templates, request, request-download"
    )

    artifact_sha256 = hashlib.sha256(b"other exact executable").hexdigest()
    descriptor = _synthetic_descriptor(artifact_sha256)
    descriptor["legacy_unregistered_verbs"] = ["find"]
    path = _write_descriptor(tmp_path, descriptor)
    other = load_executable_contract(path, artifact_sha256)
    with pytest.raises(UnsupportedExecutableError) as caught:
        other.prepare("compiled-out", {}, artifact_sha256=artifact_sha256)
    assert str(caught.value) == (
        "executable verb 'compiled-out' is not enrolled; "
        "enrolled verbs for this artifact: inspect"
    )


@pytest.mark.parametrize(
    "verb, supplied, needles",
    [
        ("request", {"ca": r"S\C"}, ("template", "required")),
        ("request", {"ca": r"S\C", "template": "T", "extra": 1}, ("extra", "not permitted")),
        ("request", {"ca": r"S\C", "template": "T", "machine": 1}, ("machine", "boolean")),
        ("request-download", {"ca": r"S\C", "id": True}, ("id", "integer")),
        ("request-download", {"ca": r"S\C", "id": 2147483648}, ("id", "2147483647")),
        ("request-download", {"ca": r"S\C", "id": -2147483649}, ("id", "-2147483648")),
        (
            "request-download",
            {"ca": r"S\C", "id": 4, "install-machine": True, "install-user": True},
            ("mutually exclusive",),
        ),
    ],
)
def test_required_extra_type_int32_and_set_constraints(certify, verb, supplied, needles):
    _assert_rejected(lambda: certify.prepare(verb, supplied, artifact_sha256=CERTIFY_SHA256), *needles)


def test_sequence_fields_render_one_option_followed_by_the_declared_values(certify):
    prepared = certify.prepare(
        "request",
        {
            "ca": r"SERVER\CA",
            "template": "T",
            "upn": ["one@example.test", "two@example.test"],
            "dns": ["one.example.test", "two.example.test"],
        },
        artifact_sha256=CERTIFY_SHA256,
    )
    assert prepared.argv == (
        "request", "--ca", r"SERVER\CA", "--template", "T", "--upn",
        "one@example.test", "two@example.test", "--dns", "one.example.test",
        "two.example.test", "--key-size", "2048",
    )
    reparsed = certify.parse_and_prepare(prepared.argument_string, artifact_sha256=CERTIFY_SHA256)
    assert reparsed.argv == prepared.argv


@pytest.mark.parametrize("value", [[], "one@example.test", ["good", 3], ["good", True]])
def test_sequence_fields_reject_empty_scalar_and_mixed_values(certify, value):
    _assert_rejected(
        lambda: certify.prepare(
            "request", {"ca": r"S\C", "template": "T", "upn": value},
            artifact_sha256=CERTIFY_SHA256,
        ),
        "upn",
    )


@pytest.mark.parametrize(
    "value",
    [
        "value with spaces",
        "quoted \"value\"",
        "space and trailing" + "\\",
        "C:\\one\\two\\",
        "",
    ],
)
def test_windows_quoting_round_trips_quotes_and_trailing_backslashes(value):
    rendered = windows_quote_argument(value)
    assert parse_windows_command_line(rendered) == (value,)


def test_windows_rendering_preserves_multiple_argv_items():
    argv = ("verb", "", "a b", "three", "backslash quote \\\\ \" end\\")
    assert parse_windows_command_line(render_windows_command_line(argv)) == argv


@pytest.mark.parametrize(
    "supplied, needle",
    [
        ({"ca": "--filter-vulnerable", "template": "T"}, "ambiguous"),
        ({"ca": r"S\C", "template": "line\nbreak"}, "control"),
        ({"ca": r"S\C", "template": "nul\x00byte"}, "control"),
        ({"ca": r"S\C", "template": ""}, "at least 1"),
    ],
)
def test_strings_forbid_option_shaped_empty_and_control_character_values(certify, supplied, needle):
    _assert_rejected(lambda: certify.prepare("request", supplied, artifact_sha256=CERTIFY_SHA256), needle)


def test_legacy_raw_form_parses_and_normalizes_through_same_validator(certify):
    prepared = certify.parse_and_prepare(
        'request --output-pem --template "Template One" --ca SERVER\\CA --upn one@test two@test',
        artifact_sha256=CERTIFY_SHA256,
    )
    assert prepared.argument_string == (
        'request --ca SERVER\\CA --template "Template One" --upn one@test two@test '
        "--key-size 2048 --output-pem"
    )


@pytest.mark.parametrize(
    "raw, needle",
    [
        ("request --ca SERVER\\CA --template", "missing"),
        ("request --ca SERVER\\CA --template T --unknown", "unknown"),
        ("request --ca SERVER\\CA --ca OTHER\\CA --template T", "duplicate"),
        ("request /ca:SERVER\\CA /template:T", "unexpected"),
        ("request --ca=SERVER\\CA --template=T", "unexpected"),
        ("request --ca SERVER\\CA --template T --output-pem true", "unexpected"),
        ('request --ca "unterminated', "unterminated"),
        ("request-agent --ca SERVER\\CA", "not enrolled"),
        ("request --ca SERVER\\CA --template T --upn --fake-value", "no values"),
    ],
)
def test_legacy_mixed_unknown_and_ambiguous_forms_fail_closed(certify, raw, needle):
    _assert_rejected(lambda: certify.parse_and_prepare(raw, artifact_sha256=CERTIFY_SHA256), needle)


def test_prepared_binding_detects_contract_artifact_argument_and_argv_drift(certify):
    binding = certify.prepare(
        "request-download", {"ca": r"S\C", "id": 4}, artifact_sha256=CERTIFY_SHA256
    ).as_binding()
    for key, value, needle in (
        ("artifact_sha256", "0" * 64, "artifact identity"),
        ("contract_sha256", "0" * 64, "contract identity"),
        ("argument_string", binding["argument_string"] + " --output-pem", "changed"),
        ("argv", [*binding["argv"], "--output-pem"], "argv changed"),
    ):
        changed = dict(binding)
        changed[key] = value
        _assert_rejected(lambda changed=changed: certify.revalidate(changed, artifact_sha256=CERTIFY_SHA256), needle)


def test_unrelated_contract_proves_generic_enums_dependencies_defaults_and_disabled_verbs(tmp_path):
    artifact_sha256 = hashlib.sha256(b"unrelated cli").hexdigest()
    contract = load_executable_contract(
        _write_descriptor(tmp_path, _synthetic_descriptor(artifact_sha256)), artifact_sha256
    )
    prepared = contract.prepare(
        "inspect", {"mode": "brief", "tag": ["one", "two"], "source": "cache", "confirm": True},
        artifact_sha256=artifact_sha256,
    )
    assert prepared.argument_string == "inspect --mode brief --tag one two --limit 10 --source cache --confirm"
    _assert_rejected(
        lambda: contract.prepare("inspect", {"mode": "other"}, artifact_sha256=artifact_sha256), "mode"
    )
    _assert_rejected(
        lambda: contract.prepare("inspect", {"mode": "brief", "confirm": True}, artifact_sha256=artifact_sha256),
        "requires", "source",
    )
    _assert_rejected(
        lambda: contract.prepare("compiled-out", {}, artifact_sha256=artifact_sha256), "not enrolled"
    )


def test_descriptor_with_duplicate_or_ambiguous_hash_entries_fails_closed(tmp_path):
    artifact_sha256 = hashlib.sha256(b"same binary").hexdigest()
    descriptor = _synthetic_descriptor(artifact_sha256)
    _write_descriptor(tmp_path, descriptor, "one.json")
    _write_descriptor(tmp_path, descriptor, "two.json")
    _assert_rejected(lambda: load_contract_for_artifact(tmp_path, artifact_sha256), "multiple")

    duplicate = tmp_path / "duplicate.json"
    duplicate.write_text(
        '{"contract_version":7,"contract_version":8,"binary":{"filename":"x","sha256":"'
        + artifact_sha256 + '"},"registered_verbs":[],"verbs":{}}',
        encoding="utf-8",
    )
    _assert_rejected(lambda: load_executable_contract(duplicate, artifact_sha256), "duplicate")
