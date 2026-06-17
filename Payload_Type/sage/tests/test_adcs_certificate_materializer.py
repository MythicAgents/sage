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
        encryption_algorithm=serialization.BestAvailableEncryption(password.encode()),
    )


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


def test_materializer_forges_windows_pkinit_pfx_from_verified_ca_artifact(tmp_path):
    from cryptography import x509
    from cryptography.hazmat.primitives.serialization import pkcs12

    artifact_dir = tmp_path / "artifacts"
    ca_artifact = _write_ca_artifact(artifact_dir / "adcs_ca_test_ca01_lab.local.pem.txt")

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
    assert result.inputs["certificate_already_forged"] is True
    assert result.inputs["forged_pfx_path"] == r"C:\Windows\Temp\sage_forged_cert_administrator_lab_local_13.pfx"
    assert "ca_pfx_path" not in result.inputs
    pfx_path = Path(result.inputs["_local_forged_pfx_path"])
    assert pfx_path.is_file()

    key, cert, cas = pkcs12.load_key_and_certificates(
        pfx_path.read_bytes(),
        result.inputs["forged_pfx_password"].encode(),
    )
    assert key is not None
    assert cas
    assert cert.subject.rfc4514_string() == "CN=administrator"
    san = cert.extensions.get_extension_for_class(x509.SubjectAlternativeName).value
    assert any(
        isinstance(name, x509.OtherName)
        and name.type_id.dotted_string == "1.3.6.1.4.1.311.20.2.3"
        and b"administrator@lab.local" in name.value
        for name in san
    )
    assert cert.extensions.get_extension_for_class(x509.BasicConstraints).value.ca is False
    assert cert.extensions.get_extension_for_class(x509.SubjectKeyIdentifier).value.digest
    assert cert.extensions.get_extension_for_class(x509.AuthorityKeyIdentifier).value.key_identifier
    cdp = cert.extensions.get_extension_for_class(x509.CRLDistributionPoints).value
    assert "ldap:///" in repr(cdp)
    eku = cert.extensions.get_extension_for_class(x509.ExtendedKeyUsage).value
    assert {oid.dotted_string for oid in eku} >= {
        "1.3.6.1.5.5.7.3.2",
        "1.3.6.1.4.1.311.20.2.2",
    }


def test_materializer_can_embed_ntds_ca_security_sid_extension(tmp_path):
    from cryptography import x509
    from cryptography.hazmat.primitives.serialization import pkcs12
    from cryptography.x509.oid import ObjectIdentifier

    artifact_dir = tmp_path / "artifacts"
    ca_artifact = _write_ca_artifact(artifact_dir / "adcs_ca_test_ca01_lab.local.pem.txt")
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
    _key, cert, _cas = pkcs12.load_key_and_certificates(
        Path(result.inputs["_local_forged_pfx_path"]).read_bytes(),
        result.inputs["forged_pfx_password"].encode(),
    )
    extension = cert.extensions.get_extension_for_oid(ObjectIdentifier("1.3.6.1.4.1.311.25.2")).value
    assert isinstance(extension, x509.UnrecognizedExtension)
    assert account_sid.encode() in extension.value


def test_materializer_can_embed_binary_sid_ntds_ca_security_extension(tmp_path):
    from cryptography.hazmat.primitives.serialization import pkcs12
    from cryptography.x509.oid import ObjectIdentifier

    artifact_dir = tmp_path / "artifacts"
    ca_artifact = _write_ca_artifact(artifact_dir / "adcs_ca_test_ca01_lab.local.pem.txt")
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
        sid_extension_encoding="binary",
    )

    assert result.ok is True
    _key, cert, _cas = pkcs12.load_key_and_certificates(
        Path(result.inputs["_local_forged_pfx_path"]).read_bytes(),
        result.inputs["forged_pfx_password"].encode(),
    )
    extension = cert.extensions.get_extension_for_oid(ObjectIdentifier("1.3.6.1.4.1.311.25.2")).value
    assert account_sid.encode() not in extension.value
    assert b"\x01\x05\x00\x00\x00\x00\x00\x05" in extension.value


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
    ca_artifact = _write_ca_artifact(artifact_dir / "adcs_ca_test_ca01_lab.local.pem.txt")
    ledger = _ledger(Path("/missing/adcs-ca.pem.txt"))

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


def test_materializer_skips_newer_unusable_pfx_and_uses_valid_pem_fallback(tmp_path):
    artifact_dir = tmp_path / "artifacts"
    ca_artifact = _write_ca_artifact(artifact_dir / "adcs_ca_test_ca01_lab.local.pem.txt")
    bad_pfx = artifact_dir / "adcs_ca_signing_test_ca01_lab.local.pfx"
    bad_pfx.write_bytes(b"not-a-pkcs12")
    os.utime(ca_artifact, (1, 1))
    os.utime(bad_pfx, (2, 2))
    ledger = _ledger(Path("/missing/adcs-ca.pem.txt"))

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
