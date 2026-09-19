"""Independent, in-memory certificate-material oracles.

The public entry point consumes a frozen scenario binding, one native Mythic task
and response, and one independently retained artifact.  Every identity is
required: operation, callback, task, response, artifact id, artifact digest,
target, and the task parameter digest which identifies the request when Mythic
has no separate request id.  Missing bytes or bindings return ``None``.

Only command parameter shapes emitted by Sage's current CA-export, ESC
enrollment, and certificate-forge builders are recognized.  The returned
``OracleObservation`` contains hashes and public certificate identity only; key
bytes, artifact bytes, passwords, and task output never leave this module.
"""

from __future__ import annotations

import base64
import hashlib
import json
import re
from collections.abc import Mapping, Sequence
from typing import Any

from cryptography import x509
from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import dsa, ec, ed25519, ed448, padding, rsa
from cryptography.hazmat.primitives.serialization import pkcs12
from cryptography.x509.oid import ExtensionOID, NameOID, ObjectIdentifier

from ai.hillclimb import effect_registry


_TEMPLATE_NAME_OID = ObjectIdentifier("1.3.6.1.4.1.311.20.2")
_TEMPLATE_INFO_OID = ObjectIdentifier("1.3.6.1.4.1.311.21.7")
_UPN_OID = ObjectIdentifier("1.3.6.1.4.1.311.20.2.3")
_HEX_256 = re.compile(r"[0-9a-f]{64}")


def _reject(rejected: list[str] | None, reason: str) -> None:
    if rejected is not None:
        rejected.append(reason)


def _identifier(value: object) -> str:
    if isinstance(value, bool):
        return ""
    if isinstance(value, int):
        return str(value) if value > 0 else ""
    return str(value or "").strip()


def _same_identifier(left: object, right: object) -> bool:
    return bool(_identifier(left)) and _identifier(left) == _identifier(right)


def _normal(value: object) -> str:
    return str(value or "").strip().casefold()


def _canonical_subject(cert: x509.Certificate) -> str:
    return cert.subject.rfc4514_string()


def _single_fields(output: str) -> dict[str, str] | None:
    fields: dict[str, str] = {}
    for raw in output.splitlines():
        line = raw.strip()
        if "=" not in line:
            continue
        name, value = line.split("=", 1)
        key = name.strip().upper()
        if not re.fullmatch(r"[A-Z][A-Z0-9_]*", key):
            continue
        if key in fields:
            return None
        fields[key] = value.strip()
    return fields


def _json_parameters(value: object) -> Mapping[str, Any] | None:
    if isinstance(value, Mapping):
        return value
    if not isinstance(value, str):
        return None
    try:
        parsed = json.loads(value)
    except (TypeError, ValueError):
        return None
    return parsed if isinstance(parsed, Mapping) else None


def _operation_value(parameters: Mapping[str, Any], *names: str) -> Any:
    folded = {str(key).casefold(): value for key, value in parameters.items()}
    for name in names:
        if name.casefold() in folded:
            return folded[name.casefold()]
    return None


def _expected_step(kind: str, parameters: Mapping[str, Any]):
    """Construct one existing execution step without interpreting PowerShell."""
    from ai.langgraph import capabilities

    operations = {
        "CA_KEY_EXPORTED": (
            "adcs-ca-private-key-dpapi-export"
            if _operation_value(parameters, "tool", "dpapi_tool", "output_path", "remote_output_path")
            and not _operation_value(parameters, "pfx_path", "remote_pfx_path")
            else "adcs-ca-private-key-export"
        ),
        "CERTIFICATE_ENROLLED": "adcs-esc-certificate-enroll",
        "CERT_FORGED": "adcs-certificate-forge",
    }
    operation = operations.get(kind)
    if not operation:
        return None
    return capabilities.CapabilityExecutionStep(
        operation=operation,
        parameters=dict(parameters),
        capability={
            "CA_KEY_EXPORTED": "adcs-ca-private-key-export",
            "CERTIFICATE_ENROLLED": "adcs-esc-certificate-enroll",
            "CERT_FORGED": "adcs-certificate-auth",
        }[kind],
        purpose="independent evaluator request reconstruction",
        expected_probe={
            "CA_KEY_EXPORTED": "extract_adcs_ca_private_key_probe",
            "CERTIFICATE_ENROLLED": "extract_adcs_enrolled_certificate_probe",
            "CERT_FORGED": "extract_forged_certificate_probe",
        }[kind],
    )


def _expected_target_matches(kind: str, parameters: Mapping[str, Any], target: str, account: str) -> bool:
    domain = effect_registry.normalize_binding(_operation_value(parameters, "target_domain", "domain", "realm"))
    callback = _identifier(_operation_value(parameters, "callback_id", "callback", "callback_display_id"))
    if kind == "CA_KEY_EXPORTED":
        host = effect_registry.normalize_binding(_operation_value(parameters, "target_host", "host", "computer"))
        wanted = effect_registry.normalize_binding(target)
        targets = {host, f"{host}.{domain}" if host and domain else "", f"{host}@{domain}" if host and domain else ""}
        return bool(host and domain and wanted in targets and callback)
    principal = effect_registry.normalize_binding(_operation_value(parameters, "account", "user", "principal"))
    san = effect_registry.normalize_binding(_operation_value(parameters, "subject_alt_name", "san", "upn"))
    return bool(
        principal == account and domain == target and san == f"{account}@{target}"
        and (callback or kind == "CERT_FORGED")
    )


def _forge_options_exact(parameters: object, account: str, domain: str) -> bool:
    values = _request_params_exact(parameters)
    if not isinstance(values, Mapping):
        return False
    arguments = _operation_value(values, "assembly_arguments", "arguments", "args")
    assembly = _normal(_operation_value(values, "assembly_name", "assembly", "pe_name"))
    if not isinstance(arguments, str) or assembly.rsplit("\\", 1)[-1].rsplit("/", 1)[-1] not in {
        "certify.exe", "certify", "forgecert.exe", "forgecert",
    }:
        return False
    found: dict[str, list[str]] = {}
    for flag, raw in re.findall(r'(?<!\S)(--[A-Za-z][A-Za-z-]*)(?:=|\s+)("[^"]*"|\S+)', arguments):
        found.setdefault(flag.casefold(), []).append(raw.strip('"').casefold())
    if any(len(items) != 1 for items in found.values()):
        return False
    principal = f"{account}@{domain}"
    if arguments.casefold().strip().startswith("forge "):
        required = {"--ca-cert", "--ca-pass", "--upn", "--output-path", "--output-pass"}
        allowed = required | {"--subject", "--sid", "--crl"}
        return required <= found.keys() and found.keys() <= allowed and found["--upn"] == [principal]
    required = {"--cacertpath", "--cacertpassword", "--subjectaltname", "--newcertpath", "--newcertpassword"}
    allowed = required | {"--subject"}
    return required <= found.keys() and found.keys() <= allowed and found["--subjectaltname"] == [principal]


def _reconstructed_request(
    task: Mapping[str, Any], kind: str, target: str, account: str,
    expected_operation_parameters: Mapping[str, Any],
    expected_adapter: Mapping[str, Any] | None,
) -> tuple[str, Mapping[str, Any] | str] | None:
    """Match the complete task request against the current pure native adapter."""
    from ai.langgraph import capabilities, mythic_capability_adapter

    if not _expected_target_matches(kind, expected_operation_parameters, target, account):
        return None
    step = _expected_step(kind, expected_operation_parameters)
    if step is None:
        return None
    plan = capabilities.CapabilityExecutionPlan(True, steps=[step])
    commands = mythic_capability_adapter.build_mythic_capability_commands(
        plan, dict(expected_adapter or {}),
    )
    if not commands.ok:
        return None
    actual_command = _normal(task.get("command_name"))
    actual_parameters = _request_params_exact(task.get("original_params"))
    matches = [
        command for command in commands.commands
        if _normal(command.command) == actual_command
        and _request_params_exact(command.parameters) == actual_parameters
        and command.expected_probe == step.expected_probe
    ]
    if len(matches) == 1 and kind == "CERT_FORGED" and not _forge_options_exact(matches[0].parameters, account, target):
        return None
    return (step.operation, matches[0].parameters) if len(matches) == 1 else None


def _request_params_exact(value: object) -> Mapping[str, Any] | str | None:
    if isinstance(value, Mapping):
        return dict(value)
    if not isinstance(value, str):
        return None
    parsed = _json_parameters(value)
    return parsed if parsed is not None else value


def _response_in_task(task: Mapping[str, Any], response: Mapping[str, Any]) -> bool:
    response_id = _identifier(response.get("id"))
    response_text = response.get("response_text")
    rows = task.get("responses")
    if not response_id or not isinstance(response_text, str) or not isinstance(rows, Sequence):
        return False
    return sum(
        1 for row in rows
        if isinstance(row, Mapping)
        and _identifier(row.get("id")) == response_id
        and row.get("response_text") == response_text
    ) == 1


def _response_output(value: str) -> str:
    if re.search(r"(?m)^\s*[A-Z][A-Z0-9_]*=", value) or any(
        marker in value for marker in ("-----BEGIN", "certificate", "Certificate", "Forged", "forged")
    ):
        return value
    try:
        return base64.b64decode(value, validate=True).decode()
    except (ValueError, UnicodeError):
        return value


def _material(data: bytes, password: str | bytes | None) -> tuple[Any, x509.Certificate] | None:
    raw_password = password.encode() if isinstance(password, str) else password
    try:
        key, cert, _chain = pkcs12.load_key_and_certificates(data, raw_password)
        if key is not None and cert is not None:
            return key, cert
    except (TypeError, ValueError):
        pass
    try:
        key = serialization.load_pem_private_key(data, password=raw_password)
        certs = x509.load_pem_x509_certificates(data)
        if certs:
            return key, certs[0]
    except (TypeError, ValueError):
        return None
    return None


def _key_matches(key: Any, cert: x509.Certificate) -> bool:
    try:
        left = key.public_key().public_bytes(serialization.Encoding.DER, serialization.PublicFormat.SubjectPublicKeyInfo)
        right = cert.public_key().public_bytes(serialization.Encoding.DER, serialization.PublicFormat.SubjectPublicKeyInfo)
        return left == right
    except (TypeError, ValueError):
        return False


def _decode_der_string(value: bytes) -> str:
    if len(value) < 2 or value[0] not in {0x0C, 0x16, 0x1E}:
        return ""
    index, length = 2, value[1]
    if length & 0x80:
        width = length & 0x7F
        if width == 0 or width > 4 or len(value) < 2 + width:
            return ""
        length = int.from_bytes(value[2:2 + width], "big")
        index += width
    if index + length != len(value):
        return ""
    encoding = "utf-16-be" if value[0] == 0x1E else "utf-8"
    try:
        return value[index:].decode(encoding)
    except UnicodeError:
        return ""


def _certificate_sans(cert: x509.Certificate) -> set[str] | None:
    try:
        names = cert.extensions.get_extension_for_oid(ExtensionOID.SUBJECT_ALTERNATIVE_NAME).value
    except x509.ExtensionNotFound:
        return None
    result = {_normal(value) for value in names.get_values_for_type(x509.RFC822Name)}
    result.update(_normal(value) for value in names.get_values_for_type(x509.DNSName))
    for value in names.get_values_for_type(x509.OtherName):
        if value.type_id == _UPN_OID:
            decoded = _normal(_decode_der_string(value.value))
            if decoded:
                result.add(decoded)
    return result


def _certificate_template(cert: x509.Certificate) -> str:
    for oid in (_TEMPLATE_NAME_OID, _TEMPLATE_INFO_OID):
        try:
            raw = cert.extensions.get_extension_for_oid(oid).value
        except x509.ExtensionNotFound:
            continue
        value = getattr(raw, "value", b"")
        if not isinstance(value, bytes):
            continue
        decoded = _decode_der_string(value)
        if decoded:
            return _normal(decoded)
        match = re.search(rb"(?:[A-Za-z0-9][A-Za-z0-9_.-]*)(?:\x00)?", value)
        if match:
            try:
                return _normal(match.group(0).replace(b"\x00", b"").decode("ascii"))
            except UnicodeError:
                pass
    return ""


def _signed_by(cert: x509.Certificate, ca: x509.Certificate) -> bool:
    if cert.issuer != ca.subject:
        return False
    public = ca.public_key()
    try:
        if isinstance(public, rsa.RSAPublicKey):
            public.verify(cert.signature, cert.tbs_certificate_bytes, padding.PKCS1v15(), cert.signature_hash_algorithm)
        elif isinstance(public, ec.EllipticCurvePublicKey):
            public.verify(cert.signature, cert.tbs_certificate_bytes, ec.ECDSA(cert.signature_hash_algorithm))
        elif isinstance(public, dsa.DSAPublicKey):
            public.verify(cert.signature, cert.tbs_certificate_bytes, cert.signature_hash_algorithm)
        elif isinstance(public, (ed25519.Ed25519PublicKey, ed448.Ed448PublicKey)):
            public.verify(cert.signature, cert.tbs_certificate_bytes)
        else:
            return False
    except (InvalidSignature, TypeError, ValueError):
        return False
    return True


def _expected_ca(value: bytes | x509.Certificate | None) -> x509.Certificate | None:
    if isinstance(value, x509.Certificate):
        return value
    if not isinstance(value, bytes) or not value:
        return None
    try:
        return x509.load_pem_x509_certificate(value)
    except ValueError:
        try:
            return x509.load_der_x509_certificate(value)
        except ValueError:
            return None


def certificate_material_observation(
    kind: str,
    *,
    task: Mapping[str, Any],
    response: Mapping[str, Any],
    artifact_bytes: bytes,
    artifact_id: str,
    artifact_sha256: str,
    expected_operation_id: int,
    expected_callback_id: int | str,
    expected_task_id: int,
    expected_response_id: int | str,
    expected_request_sha256: str,
    expected_response_sha256: str,
    expected_artifact_id: str,
    expected_artifact_sha256: str,
    expected_target: str,
    expected_operation_parameters: Mapping[str, Any] | None = None,
    expected_adapter: Mapping[str, Any] | None = None,
    expected_account: str = "",
    expected_ca_subject: str = "",
    expected_ca_certificate: bytes | x509.Certificate | None = None,
    artifact_password: str | bytes | None = None,
    rejected: list[str] | None = None,
) -> effect_registry.OracleObservation | None:
    """Return a request-bound material observation, or ``None`` when unavailable.

    ``task`` is an operation-bound Mythic task row and ``response`` is one of its
    native ``responses { id response_text }`` rows.  The expected values are the
    frozen scenario/source-artifact contract.  ``original_params`` is hashed as
    the request identity because current Mythic task rows have no independent
    request id.  The artifact digest is recomputed before parsing.
    """
    milestone = str(getattr(kind, "name", kind)).strip().upper()
    if not isinstance(task, Mapping) or not isinstance(response, Mapping):
        _reject(rejected, "native task/response bindings are unavailable")
        return None
    callback = task.get("callback") if isinstance(task.get("callback"), Mapping) else {}
    if (
        type(task.get("operation_id")) is not int
        or task["operation_id"] != expected_operation_id
        or type(task.get("display_id")) is not int
        or task["display_id"] != expected_task_id
        or task.get("completed") is not True
        or not _same_identifier(callback.get("display_id"), expected_callback_id)
    ):
        _reject(rejected, "operation, completed-task, or callback binding failed")
        return None
    if not _response_in_task(task, response) or not _same_identifier(response.get("id"), expected_response_id):
        _reject(rejected, "response id/text is absent from the bound task")
        return None
    target = effect_registry.normalize_binding(expected_target)
    account = effect_registry.normalize_binding(expected_account)
    if (
        not target or (milestone != "CA_KEY_EXPORTED" and not account)
        or not isinstance(expected_operation_parameters, Mapping)
    ):
        _reject(rejected, "expected target/account identity is unavailable")
        return None
    operation_callback = _operation_value(
        expected_operation_parameters, "callback_id", "callback", "callback_display_id",
    )
    if operation_callback not in {None, ""} and not _same_identifier(operation_callback, expected_callback_id):
        _reject(rejected, "source-bound operation callback does not match the frozen callback")
        return None

    raw_request = task.get("original_params")
    if not isinstance(raw_request, (str, Mapping)):
        _reject(rejected, "native request parameters are unavailable")
        return None
    request = _reconstructed_request(
        task, milestone, target, account, expected_operation_parameters, expected_adapter,
    )
    if request is None:
        _reject(rejected, "task parameters are not a recognized builder-emitted certificate request")
        return None
    operation, _expected_request = request
    raw_response = response.get("response_text")
    output = _response_output(raw_response) if isinstance(raw_response, str) else raw_response
    fields = _single_fields(output)
    if not isinstance(output, str) or fields is None:
        _reject(rejected, "response output is malformed or ambiguous")
        return None
    failed = output.casefold()
    if (
        (milestone == "CA_KEY_EXPORTED" and operation != "adcs-ca-private-key-dpapi-export" and fields.get("CA_EXPORT_STATUS", "").casefold() != "ok")
        or (milestone == "CERTIFICATE_ENROLLED" and fields.get("CERT_ENROLL_STATUS", "").casefold() != "ok")
        or (milestone == "CERTIFICATE_ENROLLED" and any(marker in failed for marker in (
            "cert_enroll_error=", "certificate request was denied", "certsrv_e_template_denied",
            "rpc server is unavailable", "access denied", "access is denied",
        )))
        or "cert_forge_status=failed" in failed or "certificate forge failed" in failed
    ):
        _reject(rejected, "bound certificate operation did not report terminal completion")
        return None

    digest = hashlib.sha256(artifact_bytes).hexdigest() if isinstance(artifact_bytes, bytes) and artifact_bytes else ""
    declared_digest = _normal(artifact_sha256)
    if (
        not digest or not _HEX_256.fullmatch(digest)
        or declared_digest != digest
        or _normal(expected_artifact_sha256) != digest
        or not artifact_id or artifact_id != expected_artifact_id
    ):
        _reject(rejected, "artifact id or sha256 binding failed")
        return None
    output_digest = _normal(fields.get("PFX_SHA256"))
    if output_digest and output_digest != digest:
        _reject(rejected, "response/artifact sha256 binding failed")
        return None
    output_artifact = fields.get("PFX_ARTIFACT_ID", "")
    if output_artifact and output_artifact != artifact_id:
        _reject(rejected, "response/artifact id binding failed")
        return None
    embedded = fields.get("PFX_BASE64")
    if embedded:
        try:
            if base64.b64decode(embedded, validate=True) != artifact_bytes:
                _reject(rejected, "response/artifact bytes binding failed")
                return None
        except ValueError:
            _reject(rejected, "response artifact encoding is malformed")
            return None

    request_sha = hashlib.sha256(
        raw_request.encode() if isinstance(raw_request, str)
        else json.dumps(raw_request, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()
    response_sha = hashlib.sha256(raw_response.encode()).hexdigest()
    if _normal(expected_request_sha256) != request_sha or _normal(expected_response_sha256) != response_sha:
        _reject(rejected, "request or response sha256 binding failed")
        return None
    if operation == "adcs-ca-private-key-dpapi-export" and artifact_bytes not in output.encode():
        _reject(rejected, "DPAPI response does not contain the retained PEM artifact")
        return None

    pair = _material(artifact_bytes, artifact_password)
    if pair is None:
        _reject(rejected, "artifact is neither supported PKCS#12 nor PEM key/certificate material")
        return None
    key, cert = pair
    if not _key_matches(key, cert):
        _reject(rejected, "artifact private key does not match its certificate")
        return None

    if milestone == "CA_KEY_EXPORTED":
        try:
            is_ca = cert.extensions.get_extension_for_oid(ExtensionOID.BASIC_CONSTRAINTS).value.ca
        except x509.ExtensionNotFound:
            is_ca = False
        known_ca = _expected_ca(expected_ca_certificate)
        if (
            not is_ca or not expected_ca_subject
            or _normal(_canonical_subject(cert)) != _normal(expected_ca_subject)
            or (known_ca is not None and cert.public_bytes(serialization.Encoding.DER) != known_ca.public_bytes(serialization.Encoding.DER))
        ):
            _reject(rejected, "CA BasicConstraints or expected subject check failed")
            return None
    else:
        expected_principal = f"{account}@{target}"
        sans = _certificate_sans(cert)
        ca = _expected_ca(expected_ca_certificate)
        common_names = [_normal(value.value) for value in cert.subject.get_attributes_for_oid(NameOID.COMMON_NAME)]
        if sans != {expected_principal} or common_names != [account] or ca is None or not _signed_by(cert, ca):
            _reject(rejected, "certificate SAN or independently supplied CA signature check failed")
            return None
        template = _normal(_operation_value(expected_operation_parameters, "template", "certificate_template", "adcs_template")) if milestone == "CERTIFICATE_ENROLLED" else ""
        if milestone == "CERTIFICATE_ENROLLED" and (not template or _certificate_template(cert) != template):
            _reject(rejected, "issued certificate template does not match the request")
            return None

    try:
        contract = effect_registry.contract_for(milestone)
    except (KeyError, ValueError):
        _reject(rejected, f"effect registry contract for {milestone} is unavailable")
        return None
    task_id = _identifier(task.get("display_id"))
    response_id = _identifier(response.get("id"))
    callback_id = _identifier(callback.get("display_id"))
    envelope = (
        f"operation-{expected_operation_id}-callback-{callback_id}-task-{task_id}-request-{request_sha}"
        f"-response-{response_id}-{response_sha}-artifact-{artifact_id}-{digest}"
    )
    return effect_registry.OracleObservation(
        registry_version=effect_registry.REGISTRY_VERSION,
        oracle_id=contract.oracle_id,
        origin=effect_registry.ProofOrigin.MYTHIC_ARTIFACT,
        proof_kind={
            "CA_KEY_EXPORTED": "ca_private_key_material",
            "CERTIFICATE_ENROLLED": "certificate_enrollment",
            "CERT_FORGED": "certificate_material",
        }[milestone],
        outcome=True,
        observation_id=f"artifact-{artifact_id}-{digest}",
        target=target,
        subject=_canonical_subject(cert) if milestone == "CA_KEY_EXPORTED" else f"{account}@{target}",
        envelope_id=envelope,
    )
