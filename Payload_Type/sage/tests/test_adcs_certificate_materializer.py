import sys
import base64
from datetime import datetime, timedelta, timezone
import hashlib
import os
from pathlib import Path


sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "ai" / "langgraph"))

import adcs_certificate_materializer as materializer  # noqa: E402


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
    path.write_bytes(
        key.private_bytes(
            serialization.Encoding.PEM,
            serialization.PrivateFormat.PKCS8,
            serialization.NoEncryption(),
        )
        + cert.public_bytes(serialization.Encoding.PEM)
    )
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
    path.write_bytes(_pfx_from_ca_artifact(source, password))
    return path


def _ledger(path: Path) -> dict:
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
                },
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


def test_materializer_can_resolve_artifact_dir_after_verified_effect(tmp_path):
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

    assert result.ok is True
    assert result.evidence["ca_artifact_path"] == str(ca_artifact)


def test_materializer_skips_newer_unusable_pfx_and_uses_valid_pfx_fallback(tmp_path):
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

    assert result.ok is True
    assert result.evidence["ca_artifact_path"] == str(ca_artifact)
    assert result.evidence["ca_artifact_path"] != str(bad_pfx)


def test_persist_verified_ca_pfx_artifact_writes_only_sha_bound_provenance(tmp_path):
    artifact_dir = tmp_path / "artifacts"
    source_ca = _write_ca_artifact(tmp_path / "source" / "adcs_ca_source_ca01_lab.local.pem.txt")
    pfx = _pfx_from_ca_artifact(source_ca, "CA Secret!")
    sha256 = hashlib.sha256(pfx).hexdigest()
    output = "\n".join([
        "CA_EXPORT_STATUS=OK",
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
    assert evidence["pfx_sha256"] == sha256
    assert hashlib.sha256(path.read_bytes()).hexdigest() == sha256
    assert "pfx_base64" not in evidence


def test_materializer_uses_current_hop_sha_instead_of_newer_stale_artifact(tmp_path):
    artifact_dir = tmp_path / "artifacts"
    fresh = _write_ca_pfx(artifact_dir / "adcs_ca_signing_test_ca01_lab.local_fresh.pfx")
    stale = _write_ca_pfx(artifact_dir / "adcs_ca_signing_test_ca01_lab.local_stale.pfx")
    os.utime(fresh, (1, 1))
    os.utime(stale, (2, 2))
    ledger = _ledger(Path("/missing/adcs-ca.pfx"))
    ledger["hops"][0]["evidence"] = {
        "artifact_present": True,
        "verify_verdict": "achieved",
        "pfx_sha256": hashlib.sha256(fresh.read_bytes()).hexdigest(),
    }

    result = materializer.materialize_adcs_certificate_auth(
        ledger=ledger,
        artifact_dir=artifact_dir,
        engagement_key="test-op",
        domain="lab.local",
        account="administrator",
        ca_host="ca01",
        callback_id="13",
    )

    assert result.ok is True
    assert result.evidence["ca_artifact_path"] == str(fresh)
    assert result.evidence["ca_artifact_path"] != str(stale)


def test_materializer_fails_closed_when_current_hop_sha_has_no_matching_artifact(tmp_path):
    artifact_dir = tmp_path / "artifacts"
    _write_ca_pfx(artifact_dir / "adcs_ca_signing_test_ca01_lab.local_stale.pfx")
    ledger = _ledger(Path("/missing/adcs-ca.pfx"))
    ledger["hops"][0]["evidence"] = {
        "artifact_present": True,
        "verify_verdict": "achieved",
        "pfx_sha256": "a" * 64,
    }

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


def test_materializer_can_use_verified_probe_pfx_base64(tmp_path):
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

    assert result.ok is True
    selected = Path(result.evidence["ca_artifact_path"])
    assert selected.is_file()
    assert selected.parent == artifact_dir
    assert selected.name.startswith("adcs_ca_signing_")
    assert hashlib.sha256(selected.read_bytes()).hexdigest() == pfx_sha256
