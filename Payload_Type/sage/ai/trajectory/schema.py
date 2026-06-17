"""Schema and redaction helpers for trajectory transition records."""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
import hashlib
import json
import re
from typing import Any

SCHEMA_VERSION = 1

_NTLM_RE = re.compile(r"(?<![0-9a-fA-F])[0-9a-fA-F]{32}(?![0-9a-fA-F])")
_AES_RE = re.compile(r"(?<![0-9a-fA-F])[0-9a-fA-F]{64}(?![0-9a-fA-F])")
_PASSWORD_KV_RE = re.compile(r"(?i)\b(password|passwd|pwd|secret)\s*[:=]\s*([^\s,;]+)")
_SECRET_JSON_KEY_RE = re.compile(
    r"(?i)((?:\\?[\"'])(?:[A-Za-z0-9_.-]*"
    r"(?:password|passwd|pwd|secret|credential)"
    r"[A-Za-z0-9_.-]*)(?:\\?[\"'])\s*:\s*(?:\\?[\"']))([^\"'\\\r\n]+)"
)
_SECRET_CONTEXT_RE = re.compile(
    r"(?i)(\b(?:password|passwd|pwd|secret|credential|plaintext)\b[^`'\"\r\n]{0,80}[`'\"])([^`'\"\r\n]{4,})([`'\"])"
)
_SECRET_COLON_QUOTED_RE = re.compile(
    r"(?i)(\b(?:password|passwd|pwd|secret|credential|plaintext)\b[^`'\":\r\n]{0,80}:\s*[`'\"])([^`'\"\r\n]{4,})([`'\"])"
)
_SECRET_FOR_QUOTED_TARGET_RE = re.compile(
    r"(?i)(\b(?:password|passwd|pwd|secret|credential|plaintext)\b[^`'\"\r\n]{0,80}[`'\"][^`'\"\r\n]{1,120}[`'\"]\s*:\s*[`'\"])([^`'\"\r\n]{4,})([`'\"])"
)
_SAGE_GENERATED_SECRET_RE = re.compile(r"\bSage(?:Pfx|Cert)-[A-Za-z0-9_.-]+")
_PFX_BASE64_RE = re.compile(r"(?i)(\bPFX_BASE64=)([A-Za-z0-9+/=]{40,})")


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def secret_handle(value: str, kind: str = "secret") -> str:
    digest = hashlib.sha256(value.encode("utf-8", errors="replace")).hexdigest()[:16]
    return f"<{kind}:sha256:{digest}>"


def redact_text(value: Any) -> str:
    """Return text with common credential material replaced by stable handles."""
    text = "" if value is None else str(value)

    def repl_aes(match: re.Match[str]) -> str:
        return secret_handle(match.group(0).lower(), "aes256")

    def repl_ntlm(match: re.Match[str]) -> str:
        return secret_handle(match.group(0).lower(), "ntlm")

    def repl_password(match: re.Match[str]) -> str:
        key = match.group(1)
        return f"{key}=<password:redacted>"

    def repl_secret_json(match: re.Match[str]) -> str:
        return f"{match.group(1)}<password:redacted>"

    def repl_secret_context(match: re.Match[str]) -> str:
        return f"{match.group(1)}<password:redacted>{match.group(3)}"

    def repl_secret_colon_quoted(match: re.Match[str]) -> str:
        return f"{match.group(1)}<password:redacted>{match.group(3)}"

    def repl_sage_secret(match: re.Match[str]) -> str:
        return secret_handle(match.group(0), "sage-secret")

    def repl_pfx_base64(match: re.Match[str]) -> str:
        return f"{match.group(1)}<base64_blob>"

    text = _AES_RE.sub(repl_aes, text)
    text = _NTLM_RE.sub(repl_ntlm, text)
    text = _PASSWORD_KV_RE.sub(repl_password, text)
    text = _SAGE_GENERATED_SECRET_RE.sub(repl_sage_secret, text)
    text = _PFX_BASE64_RE.sub(repl_pfx_base64, text)
    text = _SECRET_JSON_KEY_RE.sub(repl_secret_json, text)
    text = _SECRET_FOR_QUOTED_TARGET_RE.sub(repl_secret_colon_quoted, text)
    text = _SECRET_COLON_QUOTED_RE.sub(repl_secret_colon_quoted, text)
    text = _SECRET_CONTEXT_RE.sub(repl_secret_context, text)
    return text


@dataclass(frozen=True)
class SourceArtifact:
    path: str
    kind: str
    size: int
    mtime: str
    readable: bool
    sensitive: bool = True
    sha256: str | None = None
    note: str = ""


@dataclass(frozen=True)
class TransitionCommand:
    payload_command: str
    adapter: str = ""
    constructed_from_builder: bool = False
    argument_features: tuple[str, ...] = field(default_factory=tuple)
    parameters_redacted: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class TransitionObservation:
    kind: str
    labels: tuple[str, ...] = field(default_factory=tuple)
    excerpt: str = ""
    source: str = ""


@dataclass(frozen=True)
class TransitionVerifier:
    status: str
    labels: tuple[str, ...] = field(default_factory=tuple)
    evidence: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class TransitionRepair:
    kind: str
    retry_budget: int = 0
    notes: str = ""


@dataclass(frozen=True)
class TransitionRecord:
    run_id: str
    source_files: tuple[str, ...]
    objective: str
    capability: str
    observations: tuple[TransitionObservation, ...]
    verifier: TransitionVerifier
    failure_label: str
    repair: TransitionRepair | None = None
    env_fingerprint: dict[str, Any] = field(default_factory=dict)
    state_before: dict[str, Any] = field(default_factory=dict)
    inputs: dict[str, Any] = field(default_factory=dict)
    commands: tuple[TransitionCommand, ...] = field(default_factory=tuple)
    state_after: dict[str, Any] = field(default_factory=dict)
    schema_version: int = SCHEMA_VERSION
    created_at: str = field(default_factory=utc_now)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    def to_json_line(self) -> str:
        return json.dumps(self.to_dict(), sort_keys=True, separators=(",", ":"))

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "TransitionRecord":
        observations = tuple(TransitionObservation(**item) for item in data.get("observations", ()))
        commands = tuple(TransitionCommand(**item) for item in data.get("commands", ()))
        verifier = TransitionVerifier(**data.get("verifier", {"status": "unknown"}))
        repair_data = data.get("repair")
        repair = TransitionRepair(**repair_data) if isinstance(repair_data, dict) else None
        return cls(
            run_id=str(data.get("run_id", "")),
            source_files=tuple(data.get("source_files") or ()),
            objective=str(data.get("objective", "")),
            capability=str(data.get("capability", "")),
            observations=observations,
            verifier=verifier,
            failure_label=str(data.get("failure_label", "")),
            repair=repair,
            env_fingerprint=dict(data.get("env_fingerprint") or {}),
            state_before=dict(data.get("state_before") or {}),
            inputs=dict(data.get("inputs") or {}),
            commands=commands,
            state_after=dict(data.get("state_after") or {}),
            schema_version=int(data.get("schema_version", SCHEMA_VERSION)),
            created_at=str(data.get("created_at") or utc_now()),
        )


def load_jsonl(path: str) -> list[TransitionRecord]:
    records: list[TransitionRecord] = []
    with open(path, "r", encoding="utf-8") as handle:
        for line in handle:
            line = line.strip()
            if not line:
                continue
            records.append(TransitionRecord.from_dict(json.loads(line)))
    return records


def write_jsonl(path: str, records: list[TransitionRecord], append: bool = False) -> None:
    mode = "a" if append else "w"
    with open(path, mode, encoding="utf-8") as handle:
        for record in records:
            handle.write(record.to_json_line())
            handle.write("\n")
