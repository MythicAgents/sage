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
    UnsupportedExecutableVerbError,
    load_contract_for_artifact,
    load_contract_for_filename,
    load_executable_contract,
    parse_windows_command_line,
    render_windows_command_line,
    windows_quote_argument,
)


SAGE_ROOT = Path(__file__).resolve().parents[1]
CERTIFY_CONTRACT = SAGE_ROOT / "ttps" / "contracts" / "certify.json"
CERTIFY_SHA1 = "7f949a895c51e747fa334b380a9511c4442ad5d0"


@pytest.fixture
def certify():
    return load_executable_contract(CERTIFY_CONTRACT, CERTIFY_SHA1)


def _synthetic_descriptor(sha1: str) -> dict:
    return {
        "contract_version": 7,
        "binary": {"filename": "unrelated-cli.exe", "sha1": sha1},
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
    assert load_executable_contract(renamed, CERTIFY_SHA1).filename == "Certify.exe"

    # Non-pinned identity: the contract loads for any well-formed observed hash (a CI-rebuilt
    # binary), since identity is the filename and the inspected hash is advisory provenance.
    assert load_executable_contract(CERTIFY_CONTRACT, "0" * 40).filename == "Certify.exe"
    # A malformed (non-SHA-1) observed hash is still rejected on shape.
    _assert_rejected(
        lambda: load_executable_contract(CERTIFY_CONTRACT, "Certify.exe"), "lowercase sha-1"
    )


def test_pinned_allowlist_enforces_byte_integrity_while_non_pinned_accepts_any_build(tmp_path):
    # A contract that pins an integrity allowlist (sealed/eval) accepts only the listed builds;
    # a non-pinned contract (the default) accepts any build of the named tool.
    pinned_hash = hashlib.sha1(b"the one trusted build").hexdigest()
    other_hash = hashlib.sha1(b"a different build").hexdigest()
    descriptor = _synthetic_descriptor(pinned_hash)
    descriptor["binary"]["accepted_sha1"] = [pinned_hash]
    path = _write_descriptor(tmp_path, descriptor)
    # In-allowlist build loads; off-allowlist build is rejected on identity.
    assert load_executable_contract(path, pinned_hash).filename == "unrelated-cli.exe"
    _assert_rejected(
        lambda: load_executable_contract(path, other_hash), "allowlist"
    )
    # By-filename resolution honors the same allowlist.
    assert load_contract_for_filename(tmp_path, "unrelated-cli.exe", pinned_hash) is not None
    _assert_rejected(
        lambda: load_contract_for_filename(tmp_path, "unrelated-cli.exe", other_hash), "allowlist"
    )


def test_hash_lookup_never_uses_a_filename_or_returns_unknown_artifacts(tmp_path):
    artifact_sha1 = hashlib.sha1(b"unrelated binary bytes").hexdigest()
    _write_descriptor(tmp_path, _synthetic_descriptor(artifact_sha1), "renamed-description.json")
    contract = load_contract_for_artifact(tmp_path, artifact_sha1)
    assert contract.filename == "unrelated-cli.exe"
    _assert_rejected(lambda: load_contract_for_artifact(tmp_path, hashlib.sha1(b"other").hexdigest()), "no enrolled")


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
    prepared = certify.prepare(verb, supplied, artifact_sha1=CERTIFY_SHA1)
    assert prepared.argument_string == expected
    assert parse_windows_command_line(prepared.argument_string) == prepared.argv
    assert certify.revalidate(prepared.as_binding(), artifact_sha1=CERTIFY_SHA1) == prepared


@pytest.mark.parametrize("verb", ["forge", "request-agent", "anything", "enumcas"])
def test_unenrolled_verbs_without_a_declared_alias_fail_closed(certify, verb):
    # Legacy verbs WITH a declared alias (find/cas/download) are repaired instead —
    # see test_declared_legacy_aliases_repair_deterministically. Everything else,
    # including a near-miss like "enumcas", still fails closed with the guard intact.
    _assert_rejected(lambda: certify.prepare(verb, {}, artifact_sha1=CERTIFY_SHA1), "not enrolled")


@pytest.mark.parametrize(
    "legacy, supplied, expected_verb, expected_args",
    [
        ("cas", {"filter-vulnerable": True}, "enum-cas", "enum-cas --filter-vulnerable"),
        ("find", {}, "enum-templates", "enum-templates"),
        ("download", {"ca": r"SERVER\CA", "id": 7}, "request-download",
         r"request-download --ca SERVER\CA --id 7"),
    ],
)
def test_declared_legacy_aliases_repair_deterministically(
    certify, legacy, supplied, expected_verb, expected_args
):
    prepared = certify.prepare(legacy, supplied, artifact_sha1=CERTIFY_SHA1)
    assert prepared.verb == expected_verb
    assert prepared.argument_string == expected_args
    assert prepared.repaired_from == legacy
    # The repair is advisory provenance only: canonical invocation identity is the
    # enrolled form, so as_binding() carries no repaired_from and revalidate round-trips.
    assert "repaired_from" not in prepared.as_binding()
    assert certify.revalidate(prepared.as_binding(), artifact_sha1=CERTIFY_SHA1) == prepared


def test_legacy_alias_repairs_through_the_raw_string_parser(certify):
    prepared = certify.parse_and_prepare("cas --filter-vulnerable", artifact_sha1=CERTIFY_SHA1)
    assert prepared.verb == "enum-cas"
    assert prepared.repaired_from == "cas"
    assert prepared.argument_string == "enum-cas --filter-vulnerable"


def test_repaired_verb_still_enforces_target_option_grammar(certify):
    # Repair rewrites the verb, not the options: request-download requires ca+id, so a
    # bare `download` with no options surfaces a recoverable grammar error, not a loop.
    _assert_rejected(
        lambda: certify.prepare("download", {}, artifact_sha1=CERTIFY_SHA1),
        "required",
    )


def test_unsupported_verb_lists_only_this_artifacts_enrolled_verbs(certify, tmp_path):
    with pytest.raises(UnsupportedExecutableVerbError) as caught:
        certify.prepare("forge", {}, artifact_sha1=CERTIFY_SHA1)
    assert str(caught.value) == (
        "executable verb 'forge' is not enrolled; enrolled verbs for this artifact: "
        "enum-cas, enum-templates, request, request-download"
    )

    artifact_sha1 = hashlib.sha1(b"other exact executable").hexdigest()
    descriptor = _synthetic_descriptor(artifact_sha1)
    descriptor["legacy_unregistered_verbs"] = ["find"]
    path = _write_descriptor(tmp_path, descriptor)
    other = load_executable_contract(path, artifact_sha1)
    with pytest.raises(UnsupportedExecutableVerbError) as caught:
        other.prepare("compiled-out", {}, artifact_sha1=artifact_sha1)
    assert str(caught.value) == (
        "executable verb 'compiled-out' is not enrolled; "
        "enrolled verbs for this artifact: inspect"
    )

    with pytest.raises(UnsupportedExecutableError) as caught:
        certify.prepare(7, {}, artifact_sha1=CERTIFY_SHA1)
    assert type(caught.value) is UnsupportedExecutableError


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
    _assert_rejected(lambda: certify.prepare(verb, supplied, artifact_sha1=CERTIFY_SHA1), *needles)


def test_sequence_fields_render_one_option_followed_by_the_declared_values(certify):
    prepared = certify.prepare(
        "request",
        {
            "ca": r"SERVER\CA",
            "template": "T",
            "upn": ["one@example.test", "two@example.test"],
            "dns": ["one.example.test", "two.example.test"],
        },
        artifact_sha1=CERTIFY_SHA1,
    )
    assert prepared.argv == (
        "request", "--ca", r"SERVER\CA", "--template", "T", "--upn",
        "one@example.test", "two@example.test", "--dns", "one.example.test",
        "two.example.test", "--key-size", "2048",
    )
    reparsed = certify.parse_and_prepare(prepared.argument_string, artifact_sha1=CERTIFY_SHA1)
    assert reparsed.argv == prepared.argv


@pytest.mark.parametrize("value", [[], "one@example.test", ["good", 3], ["good", True]])
def test_sequence_fields_reject_empty_scalar_and_mixed_values(certify, value):
    _assert_rejected(
        lambda: certify.prepare(
            "request", {"ca": r"S\C", "template": "T", "upn": value},
            artifact_sha1=CERTIFY_SHA1,
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
    _assert_rejected(lambda: certify.prepare("request", supplied, artifact_sha1=CERTIFY_SHA1), needle)


def test_legacy_raw_form_parses_and_normalizes_through_same_validator(certify):
    prepared = certify.parse_and_prepare(
        'request --output-pem --template "Template One" --ca SERVER\\CA --upn one@test two@test',
        artifact_sha1=CERTIFY_SHA1,
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
        ("request --ca SERVER\\CA --template T --output-pem true", "unexpected"),
        ('request --ca "unterminated', "unterminated"),
        ("request-agent --ca SERVER\\CA", "not enrolled"),
        ("request --ca SERVER\\CA --template T --upn --fake-value", "no values"),
    ],
)
def test_legacy_mixed_unknown_and_ambiguous_forms_fail_closed(certify, raw, needle):
    _assert_rejected(lambda: certify.parse_and_prepare(raw, artifact_sha1=CERTIFY_SHA1), needle)


_CANONICAL_REQUEST = "request --ca SERVER\\CA --template T --key-size 2048"


@pytest.mark.parametrize(
    "raw",
    [
        "request /ca:SERVER\\CA /template:T",        # legacy slash option syntax (the live residual)
        "request --ca=SERVER\\CA --template=T",      # `=`-joined values
        "request /ca:SERVER\\CA --template T",        # mixed legacy + enrolled
        "request --ca=SERVER\\CA /template:T",        # the other mix
    ],
)
def test_legacy_option_syntax_is_normalized_to_the_enrolled_grammar(certify, raw):
    prepared = certify.parse_and_prepare(raw, artifact_sha1=CERTIFY_SHA1)
    # Deterministically canonicalized to the enrolled double-dash grammar...
    assert prepared.argument_string == _CANONICAL_REQUEST
    assert prepared.verb == "request"
    assert prepared.options["ca"] == "SERVER\\CA"
    assert prepared.options["template"] == "T"
    # ...with auditable provenance recording the exact raw string it came from.
    assert prepared.normalized_from == raw
    # Canonical identity is the enrolled form; provenance never leaks into the binding.
    assert "normalized_from" not in prepared.as_binding()
    assert prepared.as_binding()["argument_string"] == _CANONICAL_REQUEST


def test_legacy_slash_flag_and_case_insensitive_option_normalize(certify):
    flag = certify.parse_and_prepare("enum-cas /filter-vulnerable", artifact_sha1=CERTIFY_SHA1)
    assert flag.argument_string == "enum-cas --filter-vulnerable"
    assert flag.normalized_from == "enum-cas /filter-vulnerable"

    cased = certify.parse_and_prepare("enum-cas --Filter-Vulnerable", artifact_sha1=CERTIFY_SHA1)
    assert cased.argument_string == "enum-cas --filter-vulnerable"
    assert cased.normalized_from == "enum-cas --Filter-Vulnerable"


def test_already_canonical_invocation_is_not_marked_normalized(certify):
    prepared = certify.parse_and_prepare(_CANONICAL_REQUEST, artifact_sha1=CERTIFY_SHA1)
    assert prepared.argument_string == _CANONICAL_REQUEST
    assert prepared.normalized_from is None  # no-op: nothing was rewritten


@pytest.mark.parametrize(
    "raw, needle",
    [
        ("enum-cas /bogus:value", "unknown"),          # unknown slash option still fails closed
        ("enum-cas --nope", "unknown"),                # unknown dash option still fails closed
        ("enum-cas /filter-vulnerable:yes", "takes no value"),  # boolean cannot carry an inline value
    ],
)
def test_normalizer_never_fabricates_an_unknown_option(certify, raw, needle):
    _assert_rejected(lambda: certify.parse_and_prepare(raw, artifact_sha1=CERTIFY_SHA1), needle)


def _descriptor_with_option_aliases(sha1, aliases):
    descriptor = _synthetic_descriptor(sha1)
    descriptor["legacy_option_aliases"] = aliases
    return descriptor


def test_declared_legacy_option_alias_repairs_a_renamed_option(tmp_path):
    sha1 = hashlib.sha1(b"aliased cli").hexdigest()
    contract = load_executable_contract(
        _write_descriptor(tmp_path, _descriptor_with_option_aliases(sha1, {"src": "source"})),
        sha1,
    )
    # `/src:cache` -> the declared rename `source`, rendered in the enrolled grammar.
    prepared = contract.parse_and_prepare("inspect --mode brief /src:cache", artifact_sha1=sha1)
    assert prepared.options["source"] == "cache"
    assert "--source cache" in prepared.argument_string
    assert prepared.normalized_from == "inspect --mode brief /src:cache"


@pytest.mark.parametrize(
    "aliases, needle",
    [
        ({"mode": "source"}, "shadow"),          # key must not shadow a real option
        ({"src": "nonexistent"}, "declared option"),  # target must be a real option
    ],
)
def test_legacy_option_alias_descriptor_is_validated(tmp_path, aliases, needle):
    sha1 = hashlib.sha1(b"bad aliases cli").hexdigest()
    _assert_rejected(
        lambda: load_executable_contract(
            _write_descriptor(tmp_path, _descriptor_with_option_aliases(sha1, aliases)), sha1
        ),
        needle,
    )


def test_prepared_binding_detects_contract_artifact_argument_and_argv_drift(certify):
    binding = certify.prepare(
        "request-download", {"ca": r"S\C", "id": 4}, artifact_sha1=CERTIFY_SHA1
    ).as_binding()
    for key, value, needle in (
        ("artifact_sha1", "0" * 40, "artifact identity"),
        ("contract_sha256", "0" * 64, "contract identity"),
        ("argument_string", binding["argument_string"] + " --output-pem", "changed"),
        ("argv", [*binding["argv"], "--output-pem"], "argv changed"),
    ):
        changed = dict(binding)
        changed[key] = value
        _assert_rejected(lambda changed=changed: certify.revalidate(changed, artifact_sha1=CERTIFY_SHA1), needle)


def test_unrelated_contract_proves_generic_enums_dependencies_defaults_and_disabled_verbs(tmp_path):
    artifact_sha1 = hashlib.sha1(b"unrelated cli").hexdigest()
    contract = load_executable_contract(
        _write_descriptor(tmp_path, _synthetic_descriptor(artifact_sha1)), artifact_sha1
    )
    prepared = contract.prepare(
        "inspect", {"mode": "brief", "tag": ["one", "two"], "source": "cache", "confirm": True},
        artifact_sha1=artifact_sha1,
    )
    assert prepared.argument_string == "inspect --mode brief --tag one two --limit 10 --source cache --confirm"
    _assert_rejected(
        lambda: contract.prepare("inspect", {"mode": "other"}, artifact_sha1=artifact_sha1), "mode"
    )
    _assert_rejected(
        lambda: contract.prepare("inspect", {"mode": "brief", "confirm": True}, artifact_sha1=artifact_sha1),
        "requires", "source",
    )
    _assert_rejected(
        lambda: contract.prepare("compiled-out", {}, artifact_sha1=artifact_sha1), "not enrolled"
    )


def test_descriptor_with_duplicate_or_ambiguous_hash_entries_fails_closed(tmp_path):
    artifact_sha1 = hashlib.sha1(b"same binary").hexdigest()
    descriptor = _synthetic_descriptor(artifact_sha1)
    _write_descriptor(tmp_path, descriptor, "one.json")
    _write_descriptor(tmp_path, descriptor, "two.json")
    _assert_rejected(lambda: load_contract_for_artifact(tmp_path, artifact_sha1), "multiple")

    duplicate = tmp_path / "duplicate.json"
    duplicate.write_text(
        '{"contract_version":7,"contract_version":8,"binary":{"filename":"x","sha1":"'
        + artifact_sha1 + '"},"registered_verbs":[],"verbs":{}}',
        encoding="utf-8",
    )
    _assert_rejected(lambda: load_executable_contract(duplicate, artifact_sha1), "duplicate")


def _aliased_descriptor(sha1: str, aliases: dict) -> dict:
    descriptor = _synthetic_descriptor(sha1)
    descriptor["legacy_unregistered_verbs"] = ["old-inspect"]
    descriptor["legacy_verb_aliases"] = aliases
    return descriptor


def test_valid_legacy_alias_loads_and_repairs_on_the_synthetic_contract(tmp_path):
    artifact_sha1 = hashlib.sha1(b"aliased synthetic cli").hexdigest()
    path = _write_descriptor(tmp_path, _aliased_descriptor(artifact_sha1, {"old-inspect": "inspect"}))
    contract = load_executable_contract(path, artifact_sha1)
    prepared = contract.prepare("old-inspect", {"mode": "brief"}, artifact_sha1=artifact_sha1)
    assert prepared.verb == "inspect"
    assert prepared.repaired_from == "old-inspect"


def test_legacy_alias_key_must_be_a_declared_legacy_verb(tmp_path):
    artifact_sha1 = hashlib.sha1(b"bad alias key").hexdigest()
    path = _write_descriptor(tmp_path, _aliased_descriptor(artifact_sha1, {"ghost": "inspect"}))
    _assert_rejected(
        lambda: load_executable_contract(path, artifact_sha1),
        "legacy verb alias", "declared legacy verb",
    )


def test_legacy_alias_target_must_be_enrolled(tmp_path):
    artifact_sha1 = hashlib.sha1(b"bad alias target").hexdigest()
    path = _write_descriptor(tmp_path, _aliased_descriptor(artifact_sha1, {"old-inspect": "compiled-out"}))
    _assert_rejected(
        lambda: load_executable_contract(path, artifact_sha1),
        "must map to an enrolled verb",
    )
