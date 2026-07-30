import sys
import base64
from datetime import datetime, timedelta, timezone
import hashlib
import json
import os
from pathlib import Path
import pytest


sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "ai" / "langgraph"))

import adcs_certificate_materializer as materializer  # noqa: E402
import artifact_secrets  # noqa: E402
import capabilities  # noqa: E402
import proof_boundary  # noqa: E402


def _write_ca_artifact(path: Path) -> Path:
    from cryptography import x509
    from cryptography.hazmat.primitives import hashes, serialization
    from cryptography.hazmat.primitives.asymmetric import rsa
    from cryptography.x509.oid import NameOID

    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    subject = issuer = x509.Name([
        x509.NameAttribute(NameOID.COMMON_NAME, "LAB-CA"),
    ])
    now = datetime.now(timezone.utc)
    cert = (
        x509.CertificateBuilder()
        .subject_name(subject)
        .issuer_name(issuer)
        .public_key(key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(now - timedelta(days=1))
        .not_valid_after(now + timedelta(days=3650))
        .add_extension(x509.BasicConstraints(ca=True, path_length=None), critical=True)
        .add_extension(x509.SubjectKeyIdentifier.from_public_key(key.public_key()), critical=False)
        .add_extension(
            x509.KeyUsage(
                digital_signature=True,
                content_commitment=False,
                key_encipherment=False,
                data_encipherment=False,
                key_agreement=False,
                key_cert_sign=True,
                crl_sign=True,
                encipher_only=False,
                decipher_only=False,
            ),
            critical=False,
        )
        .sign(key, hashes.SHA256())
    )
    path.parent.mkdir(parents=True, exist_ok=True)
    path.parent.chmod(0o700)
    path.write_bytes(
        key.private_bytes(
            serialization.Encoding.PEM,
            serialization.PrivateFormat.PKCS8,
            serialization.NoEncryption(),
        )
        + cert.public_bytes(serialization.Encoding.PEM)
    )
    path.chmod(0o600)
    return path


def _pfx_from_ca_artifact(path: Path, password: str) -> bytes:
    from cryptography.hazmat.primitives import serialization
    from cryptography.hazmat.primitives.serialization import pkcs12

    key, cert, _subject = materializer.load_ca_key_cert_from_artifact(path, "", "lab-ca")
    return pkcs12.serialize_key_and_certificates(
        name=b"lab-ca",
        key=key,
        cert=cert,
        cas=None,
        encryption_algorithm=(
            serialization.BestAvailableEncryption(password.encode())
            if password else
            serialization.NoEncryption()
        ),
    )


def _write_ca_pfx(path: Path, password: str = "") -> Path:
    source = _write_ca_artifact(path.parent / f"{path.stem}.pem.txt")
    path.parent.chmod(0o700)
    path.write_bytes(_pfx_from_ca_artifact(source, password))
    path.chmod(0o600)
    return path


def _ledger(path: Path) -> dict:
    sha256 = hashlib.sha256(path.read_bytes()).hexdigest() if path.is_file() else "a" * 64
    envelope = proof_boundary.make_runtime_artifact_envelope(
        engagement_id="test-op",
        callback_id="13",
        task_id="450",
        terminal_status="completed",
        command="execute_assembly",
        artifact_id="artifact-ca-1",
        artifact_sha256=sha256,
        verifier_id="capability:adcs-ca-private-key-export",
        transaction_id="fixture:450",
        verifier_input={"artifact_id": "artifact-ca-1", "task_id": "450"},
        verifier_result={"verdict": "achieved", "artifact_present": True},
        captured_at="2026-07-14T00:00:00+00:00",
    ).to_dict()
    return {
        "hops": [
            {
                "id": "capability:adcs-ca-private-key-export:target=ca01;target_domain=lab.local;callback=13",
                "effect": "adcs-ca-private-key:ca01@lab.local",
                "status": "achieved",
                "satisfied_effects": ["adcs-ca-private-key:ca01@lab.local"],
                "evidence": {
                    "artifact_present": True,
                    "verify_verdict": "achieved",
                    "pfx_artifact_path": str(path),
                    "pfx_artifact_id": "artifact-ca-1",
                    "pfx_artifact_sha256": sha256,
                    "proof_envelope": envelope,
                },
                "proof_envelope": envelope,
            }
        ]
    }

def test_materializer_stages_verified_ca_pfx_for_payload_side_forge(tmp_path):
    artifact_dir = tmp_path / "artifacts"
    ca_artifact = _write_ca_pfx(artifact_dir / "adcs_ca_test_ca01_lab.local.pfx")

    result = materializer.materialize_adcs_certificate_auth(
        ledger=_ledger(ca_artifact),
        artifact_dir=artifact_dir,
        engagement_key="test-op",
        domain="LAB.LOCAL",
        account="Administrator",
        ca_host="CA01.lab.local",
        callback_id="cb13",
    )

    assert result.ok is True
    assert "certificate_already_forged" not in result.inputs
    assert result.inputs["ca_pfx_path"] == r"C:\Windows\Temp\sage_ca_signing_administrator_lab_local_13.pfx"
    assert result.inputs["ca_pfx_password"] == ""
    assert result.inputs["forged_pfx_path"] == r"C:\Windows\Temp\sage_forged_cert_administrator_lab_local_13.pfx"
    assert Path(result.inputs["_local_ca_pfx_path"]) == ca_artifact
    assert result.evidence["materialization_mode"] == "stage-ca-pfx-for-payload-forge"
    assert result.evidence["ca_artifact_path"] == str(ca_artifact)
    assert result.evidence["remote_ca_pfx_path"] == result.inputs["ca_pfx_path"]


def test_materializer_forwards_account_sid_for_payload_side_forge(tmp_path):
    artifact_dir = tmp_path / "artifacts"
    ca_artifact = _write_ca_pfx(artifact_dir / "adcs_ca_test_ca01_lab.local.pfx")
    account_sid = "S-1-5-21-111-222-333-500"

    result = materializer.materialize_adcs_certificate_auth(
        ledger=_ledger(ca_artifact),
        artifact_dir=artifact_dir,
        engagement_key="test-op",
        domain="lab.local",
        account="administrator",
        ca_host="ca01",
        callback_id="13",
        account_sid=account_sid,
    )

    assert result.ok is True
    assert result.inputs["account_sid"] == account_sid
    assert result.evidence["account_sid"] == account_sid


def test_materializer_rejects_pem_only_material_until_payload_forge_can_consume_it(tmp_path):
    artifact_dir = tmp_path / "artifacts"
    ca_artifact = _write_ca_artifact(artifact_dir / "adcs_ca_test_ca01_lab.local.pem.txt")

    result = materializer.materialize_adcs_certificate_auth(
        ledger=_ledger(ca_artifact),
        artifact_dir=artifact_dir,
        engagement_key="test-op",
        domain="lab.local",
        account="administrator",
        ca_host="ca01",
        callback_id="13",
    )

    assert result.ok is False
    assert result.missing == ["adcs_ca_private_key_pfx_artifact"]
    assert "payload-side certificate forge adapter requires a usable PFX artifact" in result.reason


def test_materializer_requires_verified_ca_private_key_effect(tmp_path):
    artifact_dir = tmp_path / "artifacts"
    _write_ca_artifact(artifact_dir / "adcs_ca_test_ca01_lab.local.pem.txt")

    result = materializer.materialize_adcs_certificate_auth(
        ledger={"hops": []},
        artifact_dir=artifact_dir,
        engagement_key="test-op",
        domain="lab.local",
        account="administrator",
        ca_host="ca01",
        callback_id="13",
    )

    assert result.ok is False
    assert result.missing == ["adcs_ca_private_key_artifact"]


def test_materializer_does_not_search_artifact_dir_after_verified_effect(tmp_path):
    artifact_dir = tmp_path / "artifacts"
    ca_artifact = _write_ca_pfx(artifact_dir / "adcs_ca_test_ca01_lab.local.pfx")
    ledger = _ledger(Path("/missing/adcs-ca.pem.txt"))
    ledger["hops"][0]["evidence"].pop("pfx_artifact_path")

    result = materializer.materialize_adcs_certificate_auth(
        ledger=ledger,
        artifact_dir=artifact_dir,
        engagement_key="test-op",
        domain="lab.local",
        account="administrator",
        ca_host="ca01",
        callback_id="13",
    )

    assert result.ok is False
    assert result.missing == ["adcs_ca_private_key_artifact"]


def test_materializer_does_not_fallback_to_another_pfx(tmp_path):
    artifact_dir = tmp_path / "artifacts"
    ca_artifact = _write_ca_pfx(artifact_dir / "adcs_ca_test_ca01_lab.local.pfx")
    bad_pfx = artifact_dir / "adcs_ca_signing_test_ca01_lab.local.pfx"
    bad_pfx.write_bytes(b"not-a-pkcs12")
    os.utime(ca_artifact, (1, 1))
    os.utime(bad_pfx, (2, 2))
    ledger = _ledger(Path("/missing/adcs-ca.pem.txt"))
    ledger["hops"][0]["evidence"].pop("pfx_artifact_path")

    result = materializer.materialize_adcs_certificate_auth(
        ledger=ledger,
        artifact_dir=artifact_dir,
        engagement_key="test-op",
        domain="lab.local",
        account="administrator",
        ca_host="ca01",
        callback_id="13",
    )

    assert result.ok is False
    assert result.missing == ["adcs_ca_private_key_artifact"]


def test_persist_verified_ca_pfx_artifact_writes_only_sha_bound_provenance(tmp_path):
    artifact_dir = tmp_path / "artifacts"
    artifact_dir.mkdir(mode=0o755)
    source_ca = _write_ca_artifact(tmp_path / "source" / "adcs_ca_source_ca01_lab.local.pem.txt")
    password = artifact_secrets.derive_password(tmp_path, engagement_id="test-op", purpose=artifact_secrets.ADCS_CA_EXPORT_PFX_PURPOSE, canonical_host="ca01", domain="lab.local")
    pfx = _pfx_from_ca_artifact(source_ca, password)
    sha256 = hashlib.sha256(pfx).hexdigest()
    output = "\n".join([
        "CA_EXPORT_STATUS=OK",
        "PFX_ARTIFACT_ID=artifact-ca-1",
        f"PFX_SHA256={sha256}",
        f"PFX_BASE64={base64.b64encode(pfx).decode()}",
    ])

    evidence = materializer.persist_verified_ca_pfx_artifact(
        output,
        artifact_dir,
        engagement_key="test-op",
        ca_host="ca01",
        domain="lab.local",
    )

    path = Path(evidence["pfx_artifact_path"])
    assert path.is_file()
    assert path.parent == artifact_dir
    assert evidence["pfx_artifact_sha256"] == sha256
    assert evidence["pfx_artifact_id"] == "artifact-ca-1"
    assert evidence["pfx_sha256"] == sha256
    assert hashlib.sha256(path.read_bytes()).hexdigest() == sha256
    assert oct(path.parent.stat().st_mode & 0o777) == "0o700"
    assert oct(path.stat().st_mode & 0o777) == "0o600"
    assert "pfx_base64" not in evidence
    os.chmod(path, 0o644)
    assert materializer.persist_verified_ca_pfx_artifact(output, artifact_dir, engagement_key="test-op", ca_host="ca01", domain="lab.local")["pfx_artifact_path"] == str(path)
    assert oct(path.stat().st_mode & 0o777) == "0o600"
    serialized = json.dumps({"evidence": evidence, "ledger": _ledger(path)}, sort_keys=True)
    key = next((tmp_path / "secrets").glob("artifact_master_*.key")).read_bytes()
    assert password not in serialized
    assert key.hex() not in serialized
    assert base64.b64encode(key).decode() not in serialized


def test_persist_verified_ca_pfx_artifact_rejects_symlink_artifact_dir(tmp_path):
    source = _write_ca_artifact(tmp_path / "source" / "ca.pem.txt")
    pfx = _pfx_from_ca_artifact(source, "CA Secret!")
    real = tmp_path / "real"
    real.mkdir()
    (tmp_path / "artifacts").symlink_to(real, target_is_directory=True)
    output = f"PFX_ARTIFACT_ID=artifact-ca-1\nPFX_BASE64={base64.b64encode(pfx).decode()}"
    with pytest.raises(ValueError):
        materializer.persist_verified_ca_pfx_artifact(output, tmp_path / "artifacts", engagement_key="test-op", ca_host="ca01", domain="lab.local")


def test_materializer_reopens_durable_exported_pfx_in_independent_process_context(tmp_path):
    state_dir = tmp_path / "state"
    artifact_dir = state_dir / "artifacts"
    canonical_host = capabilities.canonical_host_for_domain("CA01.lab.local", "lab.local")
    password = artifact_secrets.derive_password(
        state_dir,
        engagement_id="test-op",
        purpose=artifact_secrets.ADCS_CA_EXPORT_PFX_PURPOSE,
        canonical_host=canonical_host,
        domain="lab.local",
    )
    source_ca = _write_ca_artifact(tmp_path / "source" / "ca.pem.txt")
    pfx = _pfx_from_ca_artifact(source_ca, password)
    output = "\n".join([
        "CA_EXPORT_STATUS=OK",
        "PFX_ARTIFACT_ID=artifact-ca-1",
        f"PFX_SHA256={hashlib.sha256(pfx).hexdigest()}",
        f"PFX_BASE64={base64.b64encode(pfx).decode()}",
    ])
    evidence = materializer.persist_verified_ca_pfx_artifact(
        output,
        artifact_dir,
        engagement_key="test-op",
        ca_host="ca01.lab.local",
        domain="lab.local",
    )
    ledger = _ledger(Path(evidence["pfx_artifact_path"]))

    result = materializer.materialize_adcs_certificate_auth(
        ledger=ledger,
        artifact_dir=artifact_dir,
        engagement_key="test-op",
        domain="lab.local",
        account="administrator",
        ca_host="ca01",
        callback_id="13",
        ca_pfx_password_resolver=lambda host, domain: artifact_secrets.derive_password(
            state_dir,
            engagement_id="test-op",
            purpose=artifact_secrets.ADCS_CA_EXPORT_PFX_PURPOSE,
            canonical_host=host,
            domain=domain,
        ),
    )

    assert result.ok is True
    assert result.inputs["ca_pfx_password"] == password


def test_materializer_rejects_wrong_domain_or_multilabel_ca_host(tmp_path):
    artifact_dir = tmp_path / "artifacts"
    ca_artifact = _write_ca_pfx(artifact_dir / "adcs_ca_test_ca01_lab.local.pfx")

    for ca_host in ("ca01.other.local", "x.ca01.lab.local", "ca01..lab.local"):
        result = materializer.materialize_adcs_certificate_auth(
            ledger=_ledger(ca_artifact),
            artifact_dir=artifact_dir,
            engagement_key="test-op",
            domain="lab.local",
            account="administrator",
            ca_host=ca_host,
            callback_id="13",
        )
        assert result.ok is False
        assert "ca_host" in result.missing


@pytest.mark.parametrize("mutator", ["root_mode", "file_mode", "root_symlink"])
def test_materializer_rejects_unsafe_artifact_path_state(tmp_path, mutator):
    root = tmp_path / "artifacts"
    path = _write_ca_pfx(root / "adcs_ca_test_ca01_lab.local.pfx")
    if mutator == "root_mode":
        root.chmod(0o755)
    elif mutator == "file_mode":
        path.chmod(0o644)
    else:
        alias = tmp_path / "alias"
        alias.symlink_to(root, target_is_directory=True)
        root, path = alias, alias / path.name
    result = materializer.materialize_adcs_certificate_auth(ledger=_ledger(path), artifact_dir=root, engagement_key="test-op", domain="lab.local", account="administrator", ca_host="ca01", callback_id="13")
    assert result.ok is False
    assert result.missing == ["adcs_ca_private_key_artifact"]


def test_materializer_requires_explicit_current_hop_path(tmp_path):
    artifact_dir = tmp_path / "artifacts"
    fresh = _write_ca_pfx(artifact_dir / "adcs_ca_signing_test_ca01_lab.local_fresh.pfx")
    stale = _write_ca_pfx(artifact_dir / "adcs_ca_signing_test_ca01_lab.local_stale.pfx")
    os.utime(fresh, (1, 1))
    os.utime(stale, (2, 2))
    ledger = _ledger(Path("/missing/adcs-ca.pfx"))
    ledger["hops"][0]["evidence"].pop("pfx_artifact_path")

    result = materializer.materialize_adcs_certificate_auth(
        ledger=ledger,
        artifact_dir=artifact_dir,
        engagement_key="test-op",
        domain="lab.local",
        account="administrator",
        ca_host="ca01",
        callback_id="13",
    )

    assert result.ok is False
    assert result.missing == ["adcs_ca_private_key_artifact"]


def test_materializer_fails_closed_when_current_hop_sha_has_no_matching_artifact(tmp_path):
    artifact_dir = tmp_path / "artifacts"
    _write_ca_pfx(artifact_dir / "adcs_ca_signing_test_ca01_lab.local_stale.pfx")
    ledger = _ledger(Path("/missing/adcs-ca.pfx"))
    ledger["hops"][0]["evidence"]["pfx_artifact_sha256"] = "a" * 64

    result = materializer.materialize_adcs_certificate_auth(
        ledger=ledger,
        artifact_dir=artifact_dir,
        engagement_key="test-op",
        domain="lab.local",
        account="administrator",
        ca_host="ca01",
        callback_id="13",
    )

    assert result.ok is False
    assert result.missing == ["adcs_ca_private_key_artifact"]


def test_materializer_rejects_embedded_probe_pfx_base64_without_artifact_lineage(tmp_path):
    artifact_dir = tmp_path / "artifacts"
    source_ca = _write_ca_artifact(tmp_path / "source" / "adcs_ca_source_ca01_lab.local.pem.txt")
    password = "CA Secret!"
    pfx = _pfx_from_ca_artifact(source_ca, password)
    pfx_sha256 = hashlib.sha256(pfx).hexdigest()
    ledger = {
        "hops": [
            {
                "id": "capability:adcs-ca-private-key-export:target=ca01;target_domain=lab.local;callback=13",
                "effect": "adcs-ca-private-key:ca01@lab.local",
                "status": "achieved",
                "satisfied_effects": ["adcs-ca-private-key:ca01@lab.local"],
                "evidence": {
                    "artifact_present": True,
                    "verify_verdict": "achieved",
                    "probe": {
                        "callback_id": "13",
                        "pfx_base64": base64.b64encode(pfx).decode(),
                        "pfx_sha256": pfx_sha256,
                    },
                },
            }
        ]
    }

    result = materializer.materialize_adcs_certificate_auth(
        ledger=ledger,
        artifact_dir=artifact_dir,
        engagement_key="test-op",
        domain="lab.local",
        account="administrator",
        ca_host="ca01",
        callback_id="13",
        ca_pfx_password=password,
    )

    assert result.ok is False
    assert result.missing == ["adcs_ca_private_key_artifact"]
