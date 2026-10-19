"""Typed, version-bound contracts for arguments passed to managed executables.

Contract identity is the executable filename; the SHA-1 (read from Mythic's
file metadata, never recomputed by downloading the binary) is advisory build
provenance, and an optional allowlist can make it a strict integrity gate.
Contracts describe only the option grammar that has been enrolled.  Rendering
targets the ``CommandLineToArgvW`` splitter used by Apollo's managed-assembly
runners.
"""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass, field, replace
from pathlib import Path
from types import MappingProxyType
from typing import Annotated, Any, Literal, Mapping

from pydantic import BaseModel, ConfigDict, Field, StrictBool, StrictInt, StrictStr, ValidationError, create_model


_SHA1_RE = re.compile(r"^[0-9a-f]{40}$")
_OPTION_RE = re.compile(r"^[a-z0-9][a-z0-9-]*$")
_INT32_MIN = -(2**31)
_INT32_MAX = 2**31 - 1


class ExecutableContractError(ValueError):
    """Base error for an invalid descriptor or invocation."""


class UnsupportedExecutableError(ExecutableContractError):
    """The artifact or operation has no compatible enrolled contract."""


class UnsupportedExecutableVerbError(UnsupportedExecutableError):
    """A string verb is absent from this artifact's enrolled operations."""


class ExecutableIdentityError(ExecutableContractError):
    """The supplied executable bytes do not match the selected contract."""


class ContractValidationError(ExecutableContractError):
    """The descriptor or invocation violates its declared grammar."""


class _StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)


class BinaryDescriptor(_StrictModel):
    filename: StrictStr
    # The inspected REFERENCE hash (provenance of the build the grammar was derived from), not the
    # identity gate. Optional, because a CI/CD pipeline rebuilds .NET tools on every run so the file
    # hash changes every build while the CLI grammar does not. Contract identity is the filename.
    sha1: StrictStr | None = None
    # Optional integrity allowlist. When NON-EMPTY the contract is strict: the observed binary hash
    # must be one of these (a sealed/eval run that genuinely pins known-good builds). When empty
    # (the default) the filename identifies the contract and the observed hash is advisory provenance.
    accepted_sha1: list[StrictStr] = []
    display_version: StrictStr | None = None
    assembly_version: StrictStr | None = None
    clr_version: StrictStr | None = None


class RegisteredVerbDescriptor(_StrictModel):
    name: StrictStr
    enrolled: StrictBool
    options_type: StrictStr | None = None


class OptionDescriptor(_StrictModel):
    type: Literal["string", "boolean", "integer"]
    value_shape: Literal["scalar", "sequence"] = "scalar"
    default: Any = None
    set: StrictStr | None = None
    requires: list[StrictStr] = []
    enum: list[Any] | None = None


class VerbDescriptor(_StrictModel):
    options: dict[str, OptionDescriptor]
    required: list[StrictStr] = []


class ContractDescriptor(_StrictModel):
    contract_version: StrictInt
    binary: BinaryDescriptor
    registered_verbs: list[RegisteredVerbDescriptor]
    verbs: dict[str, VerbDescriptor]
    global_options: dict[str, OptionDescriptor] = {}
    disabled_registered_verbs: list[StrictStr] = []
    legacy_unregistered_verbs: list[StrictStr] = []
    # Deterministic repair map: a legacy (unregistered) verb -> the enrolled verb it
    # should be rewritten to before the approval card is built. Keys must be declared
    # legacy verbs and values must be enrolled verbs (validated below). Empty by default,
    # so a contract that declares no aliases keeps the strict fail-closed behavior.
    legacy_verb_aliases: dict[StrictStr, StrictStr] = {}
    # Deterministic repair map for RENAMED options: a legacy option name -> the enrolled
    # option name it should be rewritten to (e.g. "altname" -> "upn"). Applied across verbs,
    # but only ever fires when the target names a real option for the resolved verb, so a
    # global alias cannot inject an option a verb does not declare. Keys must not shadow a
    # real option name; targets must be a declared option somewhere (validated below).
    # Generic slash/`=` SYNTAX normalization (`/name:value` -> `--name value`) needs no
    # declaration; this map is only for names that actually changed between builds.
    legacy_option_aliases: dict[StrictStr, StrictStr] = {}
    identity_rule: StrictStr | None = None
    comparison_source: dict[str, StrictStr] | None = None
    evidence: dict[str, StrictStr] | None = None


@dataclass(frozen=True)
class PreparedInvocation:
    """Canonical invocation material suitable for exact approval and recheck."""

    artifact_sha1: str
    contract_sha256: str
    contract_version: int
    verb: str
    options: Mapping[str, Any]
    argv: tuple[str, ...]
    argument_string: str
    # Advisory provenance only: the legacy verb this invocation was deterministically
    # rewritten from (e.g. "cas" when the enrolled verb is "enum-cas"), else None.
    # Excluded from equality/hash and from as_binding()/revalidate so canonical
    # invocation identity stays the enrolled form and round-trips unchanged after approval.
    repaired_from: str | None = field(default=None, compare=False)
    # Advisory provenance only: the original raw argument string this invocation was
    # deterministically normalized from (legacy slash/`=` option syntax or a declared renamed
    # option rewritten to the enrolled double-dash grammar), else None. Excluded from
    # equality/hash and from as_binding()/revalidate for the same reason as repaired_from —
    # canonical identity is always the enrolled argv.
    normalized_from: str | None = field(default=None, compare=False)

    def as_binding(self) -> dict[str, Any]:
        return {
            "artifact_sha1": self.artifact_sha1,
            "contract_sha256": self.contract_sha256,
            "contract_version": self.contract_version,
            "verb": self.verb,
            "options": _copy_options(self.options),
            "argv": list(self.argv),
            "argument_string": self.argument_string,
        }


def _reject_duplicate_keys(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise ContractValidationError(f"descriptor contains duplicate key {key!r}")
        result[key] = value
    return result


def _split_option_token(token: str) -> tuple[str | None, str | None]:
    """Split one OPTION-position token into (name, inline_value) across the enrolled and legacy
    syntaxes, WITHOUT resolving it against any contract:

      --name          -> ("name", None)
      --name=value    -> ("name", "value")      # `=`-joined value
      /name           -> ("name", None)         # legacy GhostPack/.NET slash flag
      /name:value     -> ("name", "value")      # legacy slash option

    A token in none of these option shapes returns (None, None) so the caller fails closed exactly
    as before (an unexpected positional argument). This is a pure syntax split; the name still has
    to resolve to a real enrolled option (or a declared rename) or it is rejected as unknown."""
    if token.startswith("--") and token != "--":
        body = token[2:]
        if "=" in body:
            name, value = body.split("=", 1)
            return (name or None), value
        return (body or None), None
    if token.startswith("/") and len(token) > 1 and not token.startswith("//"):
        body = token[1:]
        if ":" in body:
            name, value = body.split(":", 1)
            return (name or None), value
        return (body or None), None
    return None, None


def _looks_like_option_token(token: str) -> bool:
    """Whether a token begins a new option (so it terminates a preceding sequence option). Covers
    both the enrolled double-dash form and the legacy slash form; enrolled sequence VALUES never
    start with either, so this does not truncate a legitimate value."""
    return token.startswith("--") or (
        token.startswith("/") and len(token) > 1 and not token.startswith("//")
    )


def _read_descriptor(path: Path) -> tuple[dict[str, Any], str]:
    try:
        data = path.read_bytes()
        raw = json.loads(data, object_pairs_hook=_reject_duplicate_keys)
    except ContractValidationError:
        raise
    except (OSError, TypeError, UnicodeError, json.JSONDecodeError) as exc:
        raise ContractValidationError(f"cannot load executable contract {path.name!r}: {exc}") from exc
    if not isinstance(raw, dict):
        raise ContractValidationError("executable contract must be a JSON object")
    return raw, hashlib.sha256(data).hexdigest()


def _checked_sha1(value: Any, *, label: str) -> str:
    if type(value) is not str or _SHA1_RE.fullmatch(value) is None:
        raise ExecutableIdentityError(f"{label} must be a lowercase SHA-1")
    return value


class ExecutableContract:
    """Compiled descriptor with strict, per-verb Pydantic tool models."""

    def __init__(self, descriptor: ContractDescriptor, descriptor_sha256: str):
        self.descriptor = descriptor
        self.descriptor_sha256 = descriptor_sha256
        # Reference hash (provenance of the inspected build), may be None. NOT the identity gate;
        # identity is the filename. The integrity allowlist (when set) is the only hash gate.
        self.reference_sha1 = descriptor.binary.sha1
        self._accepted_sha1 = frozenset(descriptor.binary.accepted_sha1)
        self._options_models: dict[str, type[BaseModel]] = {}
        self._invocation_models: dict[str, type[BaseModel]] = {}
        self._validate_descriptor()

    @property
    def filename(self) -> str:
        return self.descriptor.binary.filename

    @property
    def artifact_sha1(self) -> str | None:
        """The inspected REFERENCE hash (provenance), or None. Kept for callers that need a concrete
        hash for this contract; identity is the filename, so this is not the identity gate."""
        return self.reference_sha1

    @property
    def enrolled_verbs(self) -> tuple[str, ...]:
        disabled = set(self.descriptor.disabled_registered_verbs)
        return tuple(
            item.name for item in self.descriptor.registered_verbs
            if item.enrolled and item.name not in disabled
        )

    def _validate_descriptor(self) -> None:
        if self.descriptor.contract_version < 1:
            raise ContractValidationError("contract_version must be positive")
        if self.descriptor.binary.sha1 is not None:
            _checked_sha1(self.descriptor.binary.sha1, label="descriptor binary.sha1")
        for pinned in self.descriptor.binary.accepted_sha1:
            _checked_sha1(pinned, label="descriptor binary.accepted_sha1 entry")
        if not self.descriptor.binary.filename or self.descriptor.binary.filename != self.descriptor.binary.filename.strip():
            raise ContractValidationError("descriptor binary filename must be non-empty and unambiguous")
        registered: set[str] = set()
        for item in self.descriptor.registered_verbs:
            _validate_name(item.name, "registered verb")
            if item.name in registered:
                raise ContractValidationError(f"duplicate registered verb {item.name!r}")
            registered.add(item.name)
        disabled = set(self.descriptor.disabled_registered_verbs)
        if len(disabled) != len(self.descriptor.disabled_registered_verbs) or not disabled <= registered:
            raise ContractValidationError("disabled_registered_verbs must name unique registered verbs")
        legacy = set()
        for verb in self.descriptor.legacy_unregistered_verbs:
            _validate_name(verb, "legacy verb")
            if verb in registered:
                raise ContractValidationError(f"legacy verb {verb!r} is also registered")
            legacy.add(verb)
        enrolled = set(self.enrolled_verbs)
        for alias_verb, target_verb in self.descriptor.legacy_verb_aliases.items():
            _validate_name(alias_verb, "legacy verb alias")
            _validate_name(target_verb, "legacy verb alias target")
            if alias_verb not in legacy:
                raise ContractValidationError(
                    f"legacy verb alias {alias_verb!r} must name a declared legacy verb"
                )
            if alias_verb in enrolled:
                raise ContractValidationError(
                    f"legacy verb alias {alias_verb!r} must not shadow an enrolled verb"
                )
            if target_verb not in enrolled:
                raise ContractValidationError(
                    f"legacy verb alias {alias_verb!r} must map to an enrolled verb, not {target_verb!r}"
                )
        for name, specification in self.descriptor.verbs.items():
            _validate_name(name, "verb")
            if name not in registered:
                raise ContractValidationError(f"verb specification {name!r} is not registered")
            self._validate_verb_specification(name, specification)
        for name in self.enrolled_verbs:
            if name not in self.descriptor.verbs:
                raise ContractValidationError(f"enrolled verb {name!r} has no specification")
        for name, specification in self.descriptor.global_options.items():
            _validate_name(name, "global option")
            self._validate_option(name, specification)
        declared_options = set(self.descriptor.global_options)
        for specification in self.descriptor.verbs.values():
            declared_options |= set(specification.options)
        for legacy_option, target_option in self.descriptor.legacy_option_aliases.items():
            _validate_name(legacy_option, "legacy option alias")
            _validate_name(target_option, "legacy option alias target")
            if legacy_option in declared_options:
                raise ContractValidationError(
                    f"legacy option alias {legacy_option!r} must not shadow a declared option"
                )
            if target_option not in declared_options:
                raise ContractValidationError(
                    f"legacy option alias {legacy_option!r} must map to a declared option, not {target_option!r}"
                )

    def _validate_verb_specification(self, verb: str, specification: VerbDescriptor) -> None:
        names = set(self.descriptor.global_options)
        for name, option in specification.options.items():
            _validate_name(name, "option")
            if name in names:
                raise ContractValidationError(f"verb {verb!r} duplicates global option {name!r}")
            names.add(name)
            self._validate_option(name, option)
        if len(set(specification.required)) != len(specification.required) or not set(specification.required) <= names:
            raise ContractValidationError(f"verb {verb!r} required fields must name unique declared options")
        for name, option in {**self.descriptor.global_options, **specification.options}.items():
            if len(set(option.requires)) != len(option.requires) or not set(option.requires) <= names - {name}:
                raise ContractValidationError(f"option {name!r} has invalid requires constraints")

    @staticmethod
    def _validate_option(name: str, option: OptionDescriptor) -> None:
        if option.value_shape == "sequence" and option.type != "string":
            raise ContractValidationError(f"sequence option {name!r} must have string type")
        if option.set is not None and not option.set.strip():
            raise ContractValidationError(f"option {name!r} has an empty set name")
        if option.enum is not None:
            if not option.enum:
                raise ContractValidationError(f"option {name!r} has an empty enum")
            for value in option.enum:
                _validate_scalar(option, value, name=name, allow_sequence_item=True)
            if len({(type(value), repr(value)) for value in option.enum}) != len(option.enum):
                raise ContractValidationError(f"option {name!r} enum contains duplicates")
        if option.default is not None:
            _validate_option_value(name, option, option.default)

    def _alias_for(self, verb: Any) -> str | None:
        """The enrolled verb a declared legacy alias rewrites to, else None.

        Only fires for a verb that is not itself enrolled and has an unambiguous
        contract-declared replacement; never guesses a near match.
        """
        if type(verb) is not str:
            return None
        if verb in self.enrolled_verbs:
            return None
        return self.descriptor.legacy_verb_aliases.get(verb)

    def _canonical_option(
        self, name: str, option_specs: Mapping[str, "OptionDescriptor"]
    ) -> str | None:
        """Resolve a supplied option name to its enrolled spelling for the resolved verb, else None.

        Exact match wins; then a case-insensitive match against the verb's declared options; then a
        contract-declared legacy rename whose target is a real option for THIS verb. Never guesses a
        near match, so an unknown option still fails closed. A global rename only fires when its
        target is actually declared for the current verb, so it can never inject a foreign option.
        """
        if name in option_specs:
            return name
        folded = name.casefold()
        for declared in option_specs:
            if declared.casefold() == folded:
                return declared
        target = self.descriptor.legacy_option_aliases.get(name)
        if target is None:
            for legacy, mapped in self.descriptor.legacy_option_aliases.items():
                if legacy.casefold() == folded:
                    target = mapped
                    break
        if target is not None and target in option_specs:
            return target
        return None

    def _require_verb(self, verb: Any) -> tuple[str, dict[str, OptionDescriptor], VerbDescriptor]:
        if type(verb) is not str:
            raise UnsupportedExecutableError("executable verb must be a string")
        if verb not in self.enrolled_verbs:
            enrolled = ", ".join(self.enrolled_verbs)
            raise UnsupportedExecutableVerbError(
                f"executable verb {verb!r} is not enrolled; "
                f"enrolled verbs for this artifact: {enrolled}"
            )
        specification = self.descriptor.verbs[verb]
        return verb, {**self.descriptor.global_options, **specification.options}, specification

    def model_for_verb(self, verb: str) -> type[BaseModel]:
        verb, options, specification = self._require_verb(verb)
        if verb not in self._invocation_models:
            option_fields: dict[str, tuple[Any, Any]] = {}
            for option_name, option in options.items():
                internal_name = option_name.replace("-", "_")
                annotation = _annotation_for(option, alias=option_name)
                if option_name in specification.required:
                    default: Any = ...
                elif option.default is not None:
                    default = _copy_option_value(option.default)
                elif option.type == "boolean":
                    default = False
                else:
                    default = None
                option_fields[internal_name] = (annotation, default)
            options_model = create_model(
                f"ExecutableOptionsV{self.descriptor.contract_version}_{_model_name(verb)}",
                __config__=ConfigDict(extra="forbid", strict=True, populate_by_name=False),
                **option_fields,
            )
            invocation_model = create_model(
                f"ExecutableInvocationV{self.descriptor.contract_version}_{_model_name(verb)}",
                __config__=ConfigDict(extra="forbid", strict=True),
                verb=(Literal[verb], ...),  # type: ignore[valid-type]
                options=(options_model, ...),
            )
            self._options_models[verb] = options_model
            self._invocation_models[verb] = invocation_model
        return self._invocation_models[verb]

    def schema_for_verb(self, verb: str) -> dict[str, Any]:
        schema = self.model_for_verb(verb).model_json_schema(by_alias=True)
        return _strengthen_string_schema(schema)

    def strict_schema_for_verb(self, verb: str) -> dict[str, Any]:
        """Return an inline strict-tool schema; optional options are required nullable fields."""
        schema = self.schema_for_verb(verb)
        reference = schema["properties"]["options"]["$ref"].rsplit("/", 1)[-1]
        options_schema = schema["$defs"][reference]
        required_options = set(self.descriptor.verbs[verb].required)
        properties = {}
        for name, value in options_schema["properties"].items():
            current = dict(value)
            current.pop("default", None)
            if name not in required_options:
                current = {"anyOf": [current, {"type": "null"}]}
            properties[name] = current
        return {
            "type": "object",
            "properties": {
                "verb": {"type": "string", "enum": [verb]},
                "options": {
                    "type": "object",
                    "properties": properties,
                    "required": list(properties),
                    "additionalProperties": False,
                },
            },
            "required": ["verb", "options"],
            "additionalProperties": False,
        }

    def prepare(
        self,
        verb: str,
        options: Mapping[str, Any] | BaseModel,
        *,
        artifact_sha1: str,
    ) -> PreparedInvocation:
        repaired_from: str | None = None
        alias = self._alias_for(verb)
        if alias is not None:
            repaired_from = verb
            verb = alias
        verb, option_specs, specification = self._require_verb(verb)
        self.assert_artifact_identity(artifact_sha1)
        source: Any = options.model_dump(by_alias=True) if isinstance(options, BaseModel) else options
        if not isinstance(source, Mapping):
            raise ContractValidationError("executable options must be a typed object")
        optional = set(option_specs) - set(specification.required)
        source = {
            name: value for name, value in source.items()
            if value is not None or name not in optional
        }
        try:
            invocation = self.model_for_verb(verb).model_validate(
                {"verb": verb, "options": dict(source)}, by_alias=True, by_name=False
            )
        except ValidationError as exc:
            raise ContractValidationError(_pydantic_error(exc)) from exc
        normalized = invocation.options.model_dump(by_alias=True, exclude_none=True)
        canonical: dict[str, Any] = {}
        for name, value in normalized.items():
            option = option_specs[name]
            _validate_option_value(name, option, value)
            if option.type == "boolean" and value is False:
                continue
            canonical[name] = _copy_option_value(value)
        missing = [name for name in specification.required if name not in canonical]
        if missing:
            raise ContractValidationError("missing required executable option(s): " + ", ".join(sorted(missing)))
        for name, value in canonical.items():
            requires = option_specs[name].requires
            if requires and any(required not in canonical for required in requires):
                raise ContractValidationError(f"option {name!r} requires " + ", ".join(requires))
        selected_set: tuple[str, str] | None = None
        for name in canonical:
            set_name = option_specs[name].set
            if set_name:
                if selected_set is not None and selected_set[0] != set_name:
                    raise ContractValidationError(
                        f"options {selected_set[1]!r} and {name!r} select mutually exclusive sets"
                    )
                selected_set = (set_name, name)
        argv = _render_argv(verb, canonical, option_specs)
        argument_string = render_windows_command_line(argv)
        reparsed = parse_windows_command_line(argument_string)
        if reparsed != argv:
            raise ContractValidationError("rendered executable arguments do not round-trip through CommandLineToArgvW")
        return PreparedInvocation(
            # The OBSERVED artifact hash (the actual uploaded binary), so the approval card binds to
            # the real bytes and revalidate enforces "same binary approved == executed", regardless of
            # whether the contract pins a reference hash.
            artifact_sha1=artifact_sha1,
            contract_sha256=self.descriptor_sha256,
            contract_version=self.descriptor.contract_version,
            verb=verb,
            options=MappingProxyType(canonical),
            argv=argv,
            argument_string=argument_string,
            repaired_from=repaired_from,
        )

    def parse_and_prepare(self, raw: Any, *, artifact_sha1: str) -> PreparedInvocation:
        self.assert_artifact_identity(artifact_sha1)
        if type(raw) is not str or not raw:
            raise ContractValidationError("raw executable arguments must be a non-empty string")
        _validate_text(raw, label="raw executable arguments", allow_option=False)
        argv = parse_windows_command_line(raw)
        if not argv:
            raise ContractValidationError("raw executable arguments are empty")
        # Resolve a declared legacy alias for the option-spec lookup, but hand the raw
        # verb to prepare() so it performs the rewrite once and records repaired_from.
        raw_verb = argv[0]
        effective_verb = self._alias_for(raw_verb) or raw_verb
        _, option_specs, _ = self._require_verb(effective_verb)
        supplied: dict[str, Any] = {}
        normalized = False
        index = 1
        while index < len(argv):
            token = argv[index]
            index += 1
            # Normalizer (contract-driven, position-aware): canonicalize this OPTION-position token
            # from the enrolled or legacy syntax into the enrolled double-dash grammar. Values are
            # consumed inside the branches below and are never re-canonicalized, so a value that
            # happens to begin with `/` is untouched. Unknown names still fail closed.
            raw_name, inline_value = _split_option_token(token)
            if raw_name is None:
                raise ContractValidationError(f"unexpected executable argument {token!r}")
            name = self._canonical_option(raw_name, option_specs)
            if name is None:
                raise ContractValidationError(f"unknown executable option {raw_name!r}")
            if token != f"--{name}":
                normalized = True
            if name in supplied:
                raise ContractValidationError(f"duplicate executable option {name!r}")
            option = option_specs[name]
            if option.type == "boolean":
                if inline_value is not None:
                    raise ContractValidationError(f"boolean option {name!r} takes no value")
                supplied[name] = True
                continue
            if option.value_shape == "sequence":
                values: list[str] = []
                if inline_value is not None:
                    values.append(inline_value)
                while index < len(argv) and not _looks_like_option_token(argv[index]):
                    values.append(argv[index])
                    index += 1
                if not values:
                    raise ContractValidationError(f"sequence option {name!r} has no values")
                supplied[name] = values
            else:
                if inline_value is not None:
                    value = inline_value
                else:
                    if index >= len(argv):
                        raise ContractValidationError(f"option {name!r} is missing its value")
                    value = argv[index]
                    index += 1
                supplied[name] = _raw_scalar(name, option, value)
        prepared = self.prepare(raw_verb, supplied, artifact_sha1=artifact_sha1)
        if normalized:
            prepared = replace(prepared, normalized_from=raw)
        return prepared

    def revalidate(
        self, prepared: PreparedInvocation | Mapping[str, Any], *, artifact_sha1: str
    ) -> PreparedInvocation:
        self.assert_artifact_identity(artifact_sha1)
        source = prepared.as_binding() if isinstance(prepared, PreparedInvocation) else dict(prepared)
        # The binding records the OBSERVED hash at prepare time; require the same binary bytes at
        # execute time (approved binary == executed binary), independent of any reference hash.
        if source.get("artifact_sha1") != artifact_sha1:
            raise ExecutableIdentityError("prepared invocation artifact identity changed")
        if source.get("contract_sha256") != self.descriptor_sha256:
            raise ExecutableIdentityError("prepared invocation contract identity changed")
        if source.get("contract_version") != self.descriptor.contract_version:
            raise ExecutableIdentityError("prepared invocation contract version changed")
        rebuilt = self.parse_and_prepare(source.get("argument_string"), artifact_sha1=artifact_sha1)
        if source.get("verb") != rebuilt.verb or source.get("options") != _copy_options(rebuilt.options):
            raise ContractValidationError("prepared executable invocation changed after approval")
        if source.get("argv") != list(rebuilt.argv):
            raise ContractValidationError("prepared executable argv changed after approval")
        return rebuilt

    def assert_artifact_identity(self, artifact_sha1: Any) -> None:
        # Contract identity is the filename; the observed hash is validated for shape and, only when
        # the contract pins an integrity allowlist (sealed/eval), required to be one of the pinned
        # builds. A non-pinned contract accepts any build of the named tool (the CI-rebuild case),
        # recording the observed hash as provenance in the prepared binding.
        actual = _checked_sha1(artifact_sha1, label="actual artifact SHA-1")
        if self._accepted_sha1 and actual not in self._accepted_sha1:
            raise ExecutableIdentityError(
                "actual artifact SHA-1 is not in this contract's accepted-hash allowlist"
            )


def load_executable_contract(path: str | Path, artifact_sha1: str) -> ExecutableContract:
    """Load one descriptor and assert its integrity policy against the observed artifact hash.

    The hash is validated for shape and, only when the contract pins an allowlist, required to be one
    of the pinned builds. A non-pinned contract loads for any build of the named tool."""
    raw, descriptor_sha256 = _read_descriptor(Path(path))
    try:
        descriptor = ContractDescriptor.model_validate(raw)
    except ValidationError as exc:
        raise ContractValidationError(_pydantic_error(exc, prefix="invalid executable contract")) from exc
    contract = ExecutableContract(descriptor, descriptor_sha256)
    contract.assert_artifact_identity(artifact_sha1)
    return contract


def load_contract_for_filename(
    directory: str | Path, filename: Any, artifact_sha1: str
) -> ExecutableContract | None:
    """Resolve the single contract that claims ``filename`` (identity is the filename, not the bytes).

    This is the runtime resolver. A CI-rebuilt binary (same name, new hash) resolves its contract
    instead of being hard-blocked. Returns None when no contract claims the filename (an un-contracted
    executable, handled open by the caller). Raises ContractValidationError when more than one contract
    claims the same filename, and ExecutableIdentityError when a contract that pins an allowlist rejects
    the observed bytes."""
    if type(filename) is not str or not filename.strip():
        return None
    wanted = filename.strip().casefold()
    expected = _checked_sha1(artifact_sha1, label="actual artifact SHA-1")
    matches: list[Path] = []
    for path in sorted(Path(directory).glob("*.json")):
        try:
            raw, _ = _read_descriptor(path)
            descriptor = ContractDescriptor.model_validate(raw)
        except (ContractValidationError, ValidationError):
            continue
        if descriptor.binary.filename.casefold() == wanted:
            matches.append(path)
    if not matches:
        return None
    if len(matches) != 1:
        raise ContractValidationError("filename matches multiple executable contracts")
    return load_executable_contract(matches[0], expected)


def load_contract_for_artifact(directory: str | Path, artifact_sha1: str) -> ExecutableContract:
    """Find exactly one contract whose reference hash or pinned allowlist contains the artifact hash.

    By-hash lookup retained for strict/eval callers; the runtime uses ``load_contract_for_filename``."""
    expected = _checked_sha1(artifact_sha1, label="actual artifact SHA-1")
    matches: list[ExecutableContract] = []
    for path in sorted(Path(directory).glob("*.json")):
        try:
            raw, _ = _read_descriptor(path)
        except ContractValidationError:
            continue
        binary = raw.get("binary")
        if not isinstance(binary, dict):
            continue
        if binary.get("sha1") == expected or expected in (binary.get("accepted_sha1") or []):
            matches.append(load_executable_contract(path, expected))
    if not matches:
        raise UnsupportedExecutableError("artifact SHA-1 has no enrolled executable contract")
    if len(matches) != 1:
        raise ContractValidationError("artifact SHA-1 matches multiple executable contracts")
    return matches[0]


def recognizes_executable_filename(directory: str | Path, filename: Any) -> bool:
    """Whether a valid descriptor claims a filename, without treating that name as identity."""
    if type(filename) is not str or not filename.strip():
        return False
    wanted = filename.strip().casefold()
    for path in sorted(Path(directory).glob("*.json")):
        try:
            raw, descriptor_sha256 = _read_descriptor(path)
            descriptor = ContractDescriptor.model_validate(raw)
            ExecutableContract(descriptor, descriptor_sha256)
        except (ContractValidationError, ValidationError):
            continue
        if descriptor.binary.filename.casefold() == wanted:
            return True
    return False


def windows_quote_argument(value: str) -> str:
    """Encode one argv item for ``CommandLineToArgvW`` without shell semantics."""
    if type(value) is not str:
        raise ContractValidationError("argv values must be strings")
    _validate_text(value, label="argv value", allow_option=True)
    if value and not any(character in " \t\"" for character in value):
        return value
    output = ['"']
    backslashes = 0
    for character in value:
        if character == "\\":
            backslashes += 1
        elif character == '"':
            output.append("\\" * (backslashes * 2 + 1))
            output.append('"')
            backslashes = 0
        else:
            output.append("\\" * backslashes)
            output.append(character)
            backslashes = 0
    output.append("\\" * (backslashes * 2))
    output.append('"')
    return "".join(output)


def render_windows_command_line(argv: tuple[str, ...] | list[str]) -> str:
    if not argv:
        raise ContractValidationError("cannot render empty argv")
    return " ".join(windows_quote_argument(value) for value in argv)


def parse_windows_command_line(command_line: str) -> tuple[str, ...]:
    """Parse the supported ``CommandLineToArgvW`` grammar, including quote/backslash rules."""
    if type(command_line) is not str:
        raise ContractValidationError("command line must be a string")
    _validate_text(command_line, label="command line", allow_option=True)
    arguments: list[str] = []
    index = 0
    length = len(command_line)
    while index < length:
        while index < length and command_line[index] in " \t":
            index += 1
        if index == length:
            break
        output: list[str] = []
        in_quotes = False
        while index < length:
            if command_line[index] in " \t" and not in_quotes:
                break
            backslashes = 0
            while index < length and command_line[index] == "\\":
                backslashes += 1
                index += 1
            if index < length and command_line[index] == '"':
                output.append("\\" * (backslashes // 2))
                if backslashes % 2:
                    output.append('"')
                    index += 1
                elif in_quotes and index + 1 < length and command_line[index + 1] == '"':
                    output.append('"')
                    index += 2
                else:
                    in_quotes = not in_quotes
                    index += 1
                continue
            output.append("\\" * backslashes)
            if index == length or (command_line[index] in " \t" and not in_quotes):
                break
            output.append(command_line[index])
            index += 1
        if in_quotes:
            raise ContractValidationError("command line contains an unterminated quote")
        arguments.append("".join(output))
        while index < length and command_line[index] in " \t":
            index += 1
    return tuple(arguments)


def _render_argv(
    verb: str, options: Mapping[str, Any], specifications: Mapping[str, OptionDescriptor]
) -> tuple[str, ...]:
    argv = [verb]
    for name, value in options.items():
        option = specifications[name]
        argv.append("--" + name)
        if option.type == "boolean":
            continue
        if option.value_shape == "sequence":
            argv.extend(value)
        else:
            argv.append(str(value))
    return tuple(argv)


def _annotation_for(option: OptionDescriptor, *, alias: str) -> Any:
    field: dict[str, Any] = {"alias": alias}
    if option.type == "string":
        field["min_length"] = 1
        base: Any = StrictStr
    elif option.type == "integer":
        field.update(ge=_INT32_MIN, le=_INT32_MAX)
        base = StrictInt
    else:
        base = StrictBool
    if option.enum is not None:
        base = Literal.__getitem__(tuple(option.enum))
    if option.value_shape == "sequence":
        base = list[base]
        field["min_length"] = 1
    return Annotated[base, Field(**field)]


def _validate_option_value(name: str, option: OptionDescriptor, value: Any) -> None:
    if option.value_shape == "sequence":
        if not isinstance(value, (tuple, list)) or not value:
            raise ContractValidationError(f"option {name!r} needs a non-empty sequence")
        for item in value:
            _validate_scalar(option, item, name=name, allow_sequence_item=True)
            _validate_cli_value(item, name=name)
        return
    _validate_scalar(option, value, name=name, allow_sequence_item=False)
    if option.type == "string":
        _validate_cli_value(value, name=name)


def _validate_scalar(option: OptionDescriptor, value: Any, *, name: str, allow_sequence_item: bool) -> None:
    expected: type[Any]
    if option.type == "string":
        expected = str
    elif option.type == "integer":
        expected = int
    else:
        expected = bool
    if type(value) is not expected:
        raise ContractValidationError(f"option {name!r} must have {option.type} values")
    if expected is int and not _INT32_MIN <= value <= _INT32_MAX:
        raise ContractValidationError(f"option {name!r} must fit the CLR Int32 range")
    if option.enum is not None and not any(type(value) is type(item) and value == item for item in option.enum):
        raise ContractValidationError(f"option {name!r} must be one of its declared enum values")
    if expected is str:
        _validate_text(value, label=f"option {name!r}", allow_option=True)


def _validate_cli_value(value: str, *, name: str) -> None:
    if not value:
        raise ContractValidationError(f"option {name!r} cannot be empty")
    if value.startswith("-"):
        raise ContractValidationError(f"option {name!r} value is ambiguous with an option token")


def _raw_scalar(name: str, option: OptionDescriptor, value: str) -> Any:
    if option.type != "integer":
        return value
    if re.fullmatch(r"-?(?:0|[1-9][0-9]*)", value) is None:
        raise ContractValidationError(f"option {name!r} must have a canonical decimal integer")
    try:
        parsed = int(value)
    except ValueError as exc:
        raise ContractValidationError(f"option {name!r} must have an integer value") from exc
    return parsed


def _validate_text(value: str, *, label: str, allow_option: bool) -> None:
    if any(ord(character) < 32 or ord(character) == 127 for character in value):
        raise ContractValidationError(f"{label} contains a control character")


def _validate_name(value: str, label: str) -> None:
    if _OPTION_RE.fullmatch(value) is None:
        raise ContractValidationError(f"{label} {value!r} is invalid")


def _strengthen_string_schema(value: Any) -> Any:
    if isinstance(value, dict):
        strengthened = {key: _strengthen_string_schema(item) for key, item in value.items()}
        if strengthened.get("type") == "string" and "enum" not in strengthened:
            strengthened.setdefault("minLength", 1)
        return strengthened
    if isinstance(value, list):
        return [_strengthen_string_schema(item) for item in value]
    return value


def _copy_option_value(value: Any) -> Any:
    return list(value) if isinstance(value, (tuple, list)) else value


def _copy_options(options: Mapping[str, Any]) -> dict[str, Any]:
    return {name: _copy_option_value(value) for name, value in options.items()}


def _model_name(value: str) -> str:
    return "".join(part[:1].upper() + part[1:] for part in value.split("-"))


def _pydantic_error(exc: ValidationError, *, prefix: str = "executable invocation is invalid") -> str:
    details = []
    for error in exc.errors(include_url=False, include_input=False):
        location = ".".join(str(part) for part in error.get("loc", ())) or "object"
        details.append(f"{location}: {error.get('msg', 'invalid value')}")
    return prefix + ": " + "; ".join(details)
