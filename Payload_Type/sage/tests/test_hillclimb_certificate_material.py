from __future__ import annotations

import base64
import copy
import hashlib
import json
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

SAGE_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(SAGE_ROOT))

from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from cryptography.hazmat.primitives.serialization import pkcs12
from cryptography.x509.oid import ExtendedKeyUsageOID, NameOID, ObjectIdentifier

from ai.hillclimb import certificate_material, effect_registry
from ai.langgraph import capabilities, mythic_capability_adapter


CA_SUBJECT = "CN=LAB-CA"
TARGET = "ca01.lab.local"
DOMAIN = "lab.local"
ACCOUNT = "administrator"
TEMPLATE = "VulnerableUser"
PASSWORD = b"SyntheticArtifactPassword"
UPN_OID = ObjectIdentifier("1.3.6.1.4.1.311.20.2.3")
TEMPLATE_OID = ObjectIdentifier("1.3.6.1.4.1.311.20.2")


def _der_string(value: str, tag: int = 0x0C) -> bytes:
    raw = value.encode("utf-16-be" if tag == 0x1E else "utf-8")
    return bytes((tag, len(raw))) + raw


def _ca(common_name: str = "LAB-CA"):
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    subject = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, common_name)])
    now = datetime.now(timezone.utc)
    cert = (
        x509.CertificateBuilder()
        .subject_name(subject).issuer_name(subject).public_key(key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(now - timedelta(minutes=1)).not_valid_after(now + timedelta(days=30))
        .add_extension(x509.BasicConstraints(ca=True, path_length=None), critical=True)
        .add_extension(x509.KeyUsage(
            digital_signature=True, content_commitment=False, key_encipherment=False,
            data_encipherment=False, key_agreement=False, key_cert_sign=True, crl_sign=True,
            encipher_only=False, decipher_only=False,
        ), critical=True)
        .sign(key, hashes.SHA256())
    )
    return key, cert


def _leaf(ca_key, ca_cert, *, principal=f"{ACCOUNT}@{DOMAIN}", template=TEMPLATE):
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    now = datetime.now(timezone.utc)
    builder = (
        x509.CertificateBuilder()
        .subject_name(x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, ACCOUNT)]))
        .issuer_name(ca_cert.subject).public_key(key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(now - timedelta(minutes=1)).not_valid_after(now + timedelta(days=1))
        .add_extension(x509.BasicConstraints(ca=False, path_length=None), critical=True)
        .add_extension(x509.ExtendedKeyUsage([ExtendedKeyUsageOID.CLIENT_AUTH]), critical=False)
        .add_extension(x509.SubjectAlternativeName([x509.OtherName(UPN_OID, _der_string(principal))]), critical=False)
    )
    if template:
        builder = builder.add_extension(
            x509.UnrecognizedExtension(TEMPLATE_OID, _der_string(template, 0x1E)), critical=False,
        )
    return key, builder.sign(ca_key, hashes.SHA256())


def _pfx(key, cert, password=PASSWORD):
    return pkcs12.serialize_key_and_certificates(
        b"material", key, cert, None, serialization.BestAvailableEncryption(password),
    )


def _pem(key, cert):
    return (
        key.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8, serialization.NoEncryption())
        + cert.public_bytes(serialization.Encoding.PEM)
    )


def _task(script, output, *, command="powerpick", params=None):
    response = {"id": 71, "response_text": output}
    request = script if params is None else params
    return {
        "operation_id": 9, "display_id": 41, "completed": True,
        "command_name": command,
        "original_params": json.dumps(request) if isinstance(request, dict) else request,
        "callback": {"display_id": 13, "host": "CASTELBLACK", "domain": "NORTH"},
        "responses": [response],
    }, response


def _source_request(kind, *, dpapi=False, adapter=None):
    if kind == "CA_KEY_EXPORTED":
        capability = "adcs-ca-private-key-export"
        inputs = {"target_host": "ca01", "target_domain": DOMAIN, "callback_id": "13",
                  "password": "synthetic-local-password", "pfx_password": PASSWORD.decode()}
        if dpapi:
            inputs.update({"adcs_ca_export_method": "sharpdpapi", "tool_file_uuid": "synthetic-sharpdpapi-file"})
    elif kind == "CERTIFICATE_ENROLLED":
        capability = "adcs-esc-certificate-enroll"
        inputs = {"domain": DOMAIN, "account": ACCOUNT, "callback_id": "13",
                  "ca_host": "ca01", "ca_name": r"ca01.lab.local\LAB-CA", "template": TEMPLATE}
    else:
        capability = "adcs-certificate-auth"
        inputs = {"domain": DOMAIN, "account": ACCOUNT, "callback_id": "13",
                  "ca_pfx_path": r"C:\Windows\Temp\ca.pfx", "ca_pfx_password": PASSWORD.decode()}
    action = capabilities.CapabilityAction(
        name=capability, target="", preconditions=[], effects=[], intent={"capability": capability},
    )
    plan = capabilities.build_capability_execution_plan(action, inputs)
    operation = {
        "CA_KEY_EXPORTED": "adcs-ca-private-key-dpapi-export" if dpapi else "adcs-ca-private-key-export",
        "CERTIFICATE_ENROLLED": "adcs-esc-certificate-enroll",
        "CERT_FORGED": "adcs-certificate-forge",
    }[kind]
    step = next(step for step in plan.steps if step.operation == operation)
    commands = mythic_capability_adapter.build_mythic_capability_commands(
        capabilities.CapabilityExecutionPlan(True, steps=[step]), adapter or {},
    )
    command = next(command for command in commands.commands if command.expected_probe == step.expected_probe)
    return step.parameters, command


def _positive(kind, artifact, *, ca_cert, task=None, response=None, **changes):
    digest = hashlib.sha256(artifact).hexdigest()
    output = {
        "CA_KEY_EXPORTED": f"SAGE_CA_EXPORT_PROOF_ca01_13\nCA_HOST=CA01\nCA_EXPORT_STATUS=OK\nCA_SUBJECT={CA_SUBJECT}",
        "CERTIFICATE_ENROLLED": (
            "SAGE_CERT_ENROLL_PROOF_administrator_lab_local_13\nCERT_ENROLL_METHOD=native-certreq\n"
            "CERT_ENROLL_DOMAIN=lab.local\nCERT_ENROLL_ACCOUNT=administrator\n"
            "CERT_ENROLL_CA=ca01.lab.local\\LAB-CA\nCERT_ENROLL_TEMPLATE=VulnerableUser\nCERT_ENROLL_STATUS=OK"
        ),
        "CERT_FORGED": "[+] Forged certificate written to the requested PFX path",
    }[kind]
    if kind == "CA_KEY_EXPORTED":
        output += f"\nPFX_ARTIFACT_ID=artifact-1\nPFX_SHA256={digest}\nPFX_BASE64={base64.b64encode(artifact).decode()}"
    elif kind == "CERTIFICATE_ENROLLED":
        output += f"\nPFX_SHA256={digest}\nPFX_BASE64={base64.b64encode(artifact).decode()}"
    supplied_operation_parameters = "expected_operation_parameters" in changes
    operation_parameters = changes.pop("expected_operation_parameters", None)
    if task is None:
        operation_parameters, source = _source_request(kind)
        task, response = _task(source.parameters, output, command=source.command)
    elif operation_parameters is None and not supplied_operation_parameters:
        operation_parameters, _source = _source_request(kind)
    request = task["original_params"]
    request_bytes = request.encode() if isinstance(request, str) else json.dumps(
        request, sort_keys=True, separators=(",", ":"),
    ).encode()
    values = {
        "kind": kind, "task": task, "response": response, "artifact_bytes": artifact,
        "artifact_id": "artifact-1", "artifact_sha256": digest,
        "expected_operation_id": 9, "expected_callback_id": 13, "expected_task_id": 41,
        "expected_response_id": 71,
        "expected_request_sha256": hashlib.sha256(request_bytes).hexdigest(),
        "expected_response_sha256": hashlib.sha256(response["response_text"].encode()).hexdigest(),
        "expected_artifact_id": "artifact-1", "expected_artifact_sha256": digest,
        "expected_target": TARGET if kind == "CA_KEY_EXPORTED" else DOMAIN,
        "expected_account": "" if kind == "CA_KEY_EXPORTED" else ACCOUNT,
        "expected_ca_subject": CA_SUBJECT if kind == "CA_KEY_EXPORTED" else "",
        "expected_ca_certificate": ca_cert,
        "artifact_password": PASSWORD if artifact.startswith(b"0") else None,
        "expected_operation_parameters": operation_parameters,
    }
    values.update(changes)
    return certificate_material.certificate_material_observation(**values)


def test_ca_pkcs12_material_is_request_bound_and_redacted():
    ca_key, ca_cert = _ca()
    artifact = _pfx(ca_key, ca_cert)
    observation = _positive("CA_KEY_EXPORTED", artifact, ca_cert=ca_cert)
    assert observation is not None
    assert observation.origin == effect_registry.ProofOrigin.MYTHIC_ARTIFACT
    assert observation.proof_kind == "ca_private_key_material"
    assert observation.subject == CA_SUBJECT
    assert effect_registry.admit_observation("CA_KEY_EXPORTED", observation, expected_target=TARGET).proven
    rendered = json.dumps(observation.to_dict())
    assert base64.b64encode(artifact).decode() not in rendered
    assert PASSWORD.decode() not in rendered
    assert "PRIVATE KEY" not in rendered


def test_dpapi_pem_material_is_supported_only_with_current_readback_shape():
    ca_key, ca_cert = _ca()
    artifact = _pem(ca_key, ca_cert)
    operation_parameters, source = _source_request("CA_KEY_EXPORTED", dpapi=True)
    task, response = _task(source.parameters, artifact.decode(), command=source.command)
    observation = _positive("CA_KEY_EXPORTED", artifact, ca_cert=ca_cert, task=task, response=response,
                            expected_ca_subject=CA_SUBJECT,
                            expected_operation_parameters=operation_parameters)
    assert observation is not None
    without_material = copy.deepcopy(task)
    without_material["responses"][0]["response_text"] = "SharpDPAPI certificates /machine /nowrap"
    assert _positive("CA_KEY_EXPORTED", artifact, ca_cert=ca_cert, task=without_material,
                     response=without_material["responses"][0],
                     expected_operation_parameters=operation_parameters) is None


def test_enrollment_pkcs12_proves_exact_san_issuer_template_and_key():
    ca_key, ca_cert = _ca()
    leaf_key, leaf_cert = _leaf(ca_key, ca_cert)
    observation = _positive("CERTIFICATE_ENROLLED", _pfx(leaf_key, leaf_cert), ca_cert=ca_cert)
    assert observation is not None
    assert observation.proof_kind == "certificate_enrollment"
    assert observation.subject == f"{ACCOUNT}@{DOMAIN}"
    assert effect_registry.admit_observation(
        "CERTIFICATE_ENROLLED", observation, expected_target=DOMAIN,
        expected_subject=f"{ACCOUNT}@{DOMAIN}",
    ).proven


def test_forge_requires_an_actual_forge_request_and_expected_ca_signature():
    ca_key, ca_cert = _ca()
    leaf_key, leaf_cert = _leaf(ca_key, ca_cert, template="")
    artifact = _pfx(leaf_key, leaf_cert)
    observation = _positive("CERT_FORGED", artifact, ca_cert=ca_cert)
    assert observation is not None
    assert observation.proof_kind == "certificate_material"
    assert effect_registry.admit_observation("CERT_FORGED", observation, expected_target=DOMAIN).proven

    enroll_parameters, enroll_source = _source_request("CERTIFICATE_ENROLLED")
    enroll_task, enroll_response = _task(enroll_source.parameters, "CERT_FORGE_STATUS=OK",
                                         command=enroll_source.command)
    assert _positive("CERT_FORGED", artifact, ca_cert=ca_cert,
                     task=enroll_task, response=enroll_response,
                     expected_operation_parameters=_source_request("CERT_FORGED")[0]) is None


def test_output_claims_and_fake_asn1_prefix_do_not_prove_material():
    _ca_key, ca_cert = _ca()
    fake = b"0" + b"output-self-report" * 20
    assert _positive("CA_KEY_EXPORTED", fake, ca_cert=ca_cert) is None
    assert _positive("CA_KEY_EXPORTED", b"", ca_cert=ca_cert) is None
    operation_parameters, source = _source_request("CA_KEY_EXPORTED")
    task, response = _task(source.parameters, "CA_EXPORT_STATUS=OK", command=source.command)
    assert _positive("CA_KEY_EXPORTED", _pfx(_ca_key, ca_cert), ca_cert=ca_cert,
                     task=task, response=response, expected_operation_parameters=None) is None


def test_corrupt_key_and_certificate_mismatch_are_rejected():
    ca_key, ca_cert = _ca()
    leaf_key, leaf_cert = _leaf(ca_key, ca_cert, template="")
    artifact = _pfx(leaf_key, leaf_cert)
    assert _positive("CERT_FORGED", artifact[:-5] + b"abcde", ca_cert=ca_cert) is None
    wrong_key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    mismatch = _pem(wrong_key, leaf_cert)
    assert _positive("CERT_FORGED", mismatch, ca_cert=ca_cert, artifact_password=None) is None


def test_wrong_san_issuer_and_template_are_rejected():
    ca_key, ca_cert = _ca()
    wrong_ca_key, wrong_ca_cert = _ca("OTHER-CA")
    wrong_san_key, wrong_san = _leaf(ca_key, ca_cert, principal="operator@lab.local", template="")
    assert _positive("CERT_FORGED", _pfx(wrong_san_key, wrong_san), ca_cert=ca_cert) is None
    wrong_issuer_key, wrong_issuer = _leaf(wrong_ca_key, wrong_ca_cert, template="")
    assert _positive("CERT_FORGED", _pfx(wrong_issuer_key, wrong_issuer), ca_cert=ca_cert) is None
    wrong_template_key, wrong_template = _leaf(ca_key, ca_cert, template="SafeUser")
    assert _positive("CERTIFICATE_ENROLLED", _pfx(wrong_template_key, wrong_template), ca_cert=ca_cert) is None


def test_every_native_and_artifact_binding_is_required():
    ca_key, ca_cert = _ca()
    artifact = _pfx(ca_key, ca_cert)
    assert _positive("CA_KEY_EXPORTED", artifact, ca_cert=ca_cert, expected_operation_id=10) is None
    assert _positive("CA_KEY_EXPORTED", artifact, ca_cert=ca_cert, expected_callback_id=14) is None
    assert _positive("CA_KEY_EXPORTED", artifact, ca_cert=ca_cert, expected_task_id=42) is None
    assert _positive("CA_KEY_EXPORTED", artifact, ca_cert=ca_cert, expected_response_id=72) is None
    assert _positive("CA_KEY_EXPORTED", artifact, ca_cert=ca_cert, expected_request_sha256="0" * 64) is None
    assert _positive("CA_KEY_EXPORTED", artifact, ca_cert=ca_cert, expected_response_sha256="0" * 64) is None
    assert _positive("CA_KEY_EXPORTED", artifact, ca_cert=ca_cert, expected_artifact_id="artifact-2") is None
    assert _positive("CA_KEY_EXPORTED", artifact, ca_cert=ca_cert, expected_artifact_sha256="0" * 64) is None
    _other_key, other_ca = _ca("LAB-CA")
    assert _positive("CA_KEY_EXPORTED", artifact, ca_cert=ca_cert,
                     expected_ca_certificate=other_ca) is None
    op_parameters, source = _source_request("CA_KEY_EXPORTED")
    unfinished_task, response = _task(source.parameters, "CA_EXPORT_STATUS=OK", command=source.command)
    unfinished_task["completed"] = False
    assert _positive("CA_KEY_EXPORTED", artifact, ca_cert=ca_cert,
                     task=unfinished_task, response=response) is None


def test_unknown_and_echo_requests_are_unmeasured():
    ca_key, ca_cert = _ca()
    artifact = _pfx(ca_key, ca_cert)
    for command, params in (("shell", "echo CA_EXPORT_STATUS=OK PFX_BASE64="),
                            ("custom", _source_request("CA_KEY_EXPORTED")[1].parameters)):
        task, response = _task(params, "CA_EXPORT_STATUS=OK", command=command)
        assert _positive("CA_KEY_EXPORTED", artifact, ca_cert=ca_cert, task=task, response=response) is None


@pytest.mark.parametrize("kind", ["CA_KEY_EXPORTED", "CERTIFICATE_ENROLLED"])
def test_same_native_command_here_string_echo_of_real_request_is_rejected(kind):
    ca_key, ca_cert = _ca()
    key, cert = (ca_key, ca_cert) if kind == "CA_KEY_EXPORTED" else _leaf(ca_key, ca_cert)
    artifact = _pfx(key, cert)
    adapter = ({"adcs_ca_export_command": "powerpick", "adcs_ca_export_raw_script": True,
                "adcs_ca_export_use_current_context": True} if kind == "CA_KEY_EXPORTED" else {})
    operation_parameters, source = _source_request(kind, adapter=adapter)
    copied = source.parameters
    assert isinstance(copied, str)
    echo = f"Write-Output @'\n{copied}\n'@"
    digest = hashlib.sha256(artifact).hexdigest()
    output = ("CA_EXPORT_STATUS=OK\nPFX_ARTIFACT_ID=artifact-1" if kind == "CA_KEY_EXPORTED"
              else "CERT_ENROLL_STATUS=OK")
    output += f"\nPFX_SHA256={digest}\nPFX_BASE64={base64.b64encode(artifact).decode()}"
    task, response = _task(echo, output, command=source.command)
    assert _positive(kind, artifact, ca_cert=ca_cert, task=task, response=response,
                     expected_operation_parameters=operation_parameters,
                     expected_adapter=adapter) is None


@pytest.mark.parametrize("mutation", ["duplicate-upn", "wrong-upn", "contradictory-output"])
def test_forge_flags_require_exact_builder_equality(mutation):
    ca_key, ca_cert = _ca()
    leaf_key, leaf_cert = _leaf(ca_key, ca_cert, template="")
    artifact = _pfx(leaf_key, leaf_cert)
    operation_parameters, source = _source_request("CERT_FORGED")
    params = copy.deepcopy(source.parameters)
    if mutation == "duplicate-upn":
        params["assembly_arguments"] += " --upn operator@lab.local"
    elif mutation == "wrong-upn":
        params["assembly_arguments"] = params["assembly_arguments"].replace(
            "administrator@lab.local", "administrator@sub.lab.local",
        )
    else:
        params["assembly_arguments"] += " --output-path C:\\other.pfx"
    task, response = _task(params, "[+] Forged certificate", command=source.command)
    assert _positive("CERT_FORGED", artifact, ca_cert=ca_cert, task=task, response=response,
                     expected_operation_parameters=operation_parameters) is None


def test_native_base64_response_text_is_bound_before_decoding():
    ca_key, ca_cert = _ca()
    artifact = _pfx(ca_key, ca_cert)
    digest = hashlib.sha256(artifact).hexdigest()
    output = (
        f"CA_EXPORT_STATUS=OK\nPFX_ARTIFACT_ID=artifact-1\nPFX_SHA256={digest}\n"
        f"PFX_BASE64={base64.b64encode(artifact).decode()}"
    )
    encoded = base64.b64encode(output.encode()).decode()
    operation_parameters, source = _source_request("CA_KEY_EXPORTED")
    task, response = _task(source.parameters, encoded, command=source.command)
    assert _positive("CA_KEY_EXPORTED", artifact, ca_cert=ca_cert, task=task, response=response) is not None
