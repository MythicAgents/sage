"""Real PKCS#12 enrollment artifacts for verifier tests.

No issued-certificate capture exists in the retained run records (the archived Certify output is a policy-module
denial), so these build the artifact the enrollment step returns: a CA-signed account certificate with its UPN in the
subject alternative name, a template extension and the exported private key, protected by the step's password.
"""

from __future__ import annotations

import base64
from datetime import datetime, timedelta, timezone
import hashlib

from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.hazmat.primitives.serialization import pkcs12
from cryptography.x509.oid import NameOID

UPN_OID = x509.ObjectIdentifier("1.3.6.1.4.1.311.20.2.3")
TEMPLATE_NAME_OID = x509.ObjectIdentifier("1.3.6.1.4.1.311.20.2")
TEMPLATE_INFO_OID = x509.ObjectIdentifier("1.3.6.1.4.1.311.21.7")
ISSUED_AT = datetime(2026, 11, 7, 12, 0, tzinfo=timezone.utc)


def _utf8(value: str) -> bytes:
    data = value.encode()
    return b"\x0c" + bytes([len(data)]) + data


def _bmp(value: str) -> bytes:
    data = value.encode("utf-16-be")
    return b"\x1e" + bytes([len(data)]) + data


def _template_info(dotted: str) -> bytes:
    first, second, *rest = (int(arc) for arc in dotted.split("."))
    body = bytes([first * 40 + second])
    for arc in rest:
        chunk = [arc & 0x7F]
        arc >>= 7
        while arc:
            chunk.append(0x80 | (arc & 0x7F))
            arc >>= 7
        body += bytes(reversed(chunk))
    oid = b"\x06" + bytes([len(body)]) + body
    versions = b"\x02\x01\x64\x02\x01\x00"
    return b"\x30" + bytes([len(oid) + len(versions)]) + oid + versions


def issue_pfx(
    password: str,
    *,
    upn: str = "administrator@lab.local",
    ca_cn: str = "LAB-CA",
    template: str = "VulnerableUser",
    template_oid: str = "",
    include_key: bool = True,
    issued_at: datetime = ISSUED_AT,
    days: int = 365,
    legacy: bool = False,
) -> bytes:
    ca_key = ec.generate_private_key(ec.SECP256R1())
    ca_name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, ca_cn)])
    leaf_key = ec.generate_private_key(ec.SECP256R1())
    account = upn.split("@", 1)[0]
    builder = (
        x509.CertificateBuilder()
        .subject_name(x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, account)]))
        .issuer_name(ca_name)
        .public_key(leaf_key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(issued_at - timedelta(minutes=5))
        .not_valid_after(issued_at + timedelta(days=days))
        .add_extension(x509.SubjectAlternativeName([x509.OtherName(UPN_OID, _utf8(upn))]), critical=False)
    )
    if template_oid:
        builder = builder.add_extension(x509.UnrecognizedExtension(TEMPLATE_INFO_OID, _template_info(template_oid)), False)
    elif template:
        builder = builder.add_extension(x509.UnrecognizedExtension(TEMPLATE_NAME_OID, _bmp(template)), False)
    certificate = builder.sign(ca_key, hashes.SHA256())
    encryption = (
        serialization.PrivateFormat.PKCS12.encryption_builder().kdf_rounds(2000)
        .key_cert_algorithm(pkcs12.PBES.PBESv1SHA1And3KeyTripleDESCBC).hmac_hash(hashes.SHA1()).build(password.encode())
        if legacy else serialization.BestAvailableEncryption(password.encode())
    )
    return pkcs12.serialize_key_and_certificates(
        b"sage-test", leaf_key if include_key else None, certificate, None, encryption,
    )


def enrollment_output(pfx: bytes, *, status: str = "OK", marker: str = "SAGE_CERT_ENROLL_PROOF_administrator_lab_local_13",
                      sha256: str | None = None) -> str:
    """The enrollment step's own output lines, as its PowerShell writes them."""
    digest = hashlib.sha256(pfx).hexdigest() if sha256 is None else sha256
    return "\n".join([
        marker,
        "CERT_ENROLL_METHOD=native-certreq",
        "CERT_ENROLL_DOMAIN=lab.local",
        "CERT_ENROLL_ACCOUNT=administrator",
        r"CERT_ENROLL_CA=ca01.lab.local\LAB-CA",
        "CERT_ENROLL_TEMPLATE=VulnerableUser",
        "CERTREQ_SUBMIT_OUTPUT_BEGIN",
        "RequestId: 7",
        'Certificate retrieved(Issued) Issued',
        "CERTREQ_SUBMIT_OUTPUT_END",
        "CERT_REQUEST_ID=7",
        f"CERT_ENROLL_STATUS={status}",
        "CERT_SUBJECT=CN=administrator",
        r"CERT_PFX_PATH=C:\Windows\Temp\sage_forged_cert_administrator_lab_local_13.pfx",
        f"PFX_SHA256={digest}",
        f"PFX_BASE64={base64.b64encode(pfx).decode()}",
    ])
