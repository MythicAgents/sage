"""Export normalized transition records from historical Sage artifacts."""

from __future__ import annotations

import json
import os
from pathlib import Path
import re
import sqlite3
from typing import Any, Iterable
from urllib.parse import quote

from .corpus import artifact_kind, build_manifest
from .labeler import classify_observation, repair_for_label
from .schema import (
    SourceArtifact,
    TransitionCommand,
    TransitionObservation,
    TransitionRecord,
    TransitionRepair,
    TransitionVerifier,
    redact_text,
)


def export_transitions(roots: Iterable[str | Path]) -> list[TransitionRecord]:
    records: list[TransitionRecord] = []
    for artifact in build_manifest(roots):
        if not artifact.readable:
            continue
        path = Path(artifact.path)
        if artifact.kind in {"solve_log", "run_log"}:
            records.extend(export_text_artifact(path, artifact))
        elif artifact.kind == "phoenix_db":
            records.extend(export_phoenix_artifact(path, artifact))
        elif artifact.kind == "sage_db":
            records.extend(export_sage_artifact(path, artifact))
        elif artifact.kind == "engagement_ledger":
            records.extend(export_ledger_artifact(path, artifact))
    return records


def export_text_artifact(path: Path, artifact: SourceArtifact | None = None) -> list[TransitionRecord]:
    text = _read_text(path)
    records: list[TransitionRecord] = []
    for label_hint, match in _interesting_windows(text):
        record = _record_from_observation(
            observation=match,
            source_path=str(path),
            run_id=_run_id_from_path(path),
            source_kind=(artifact.kind if artifact else artifact_kind(path)) or "text",
            label_hint=label_hint,
        )
        if record:
            records.append(record)
    return records


def export_phoenix_artifact(path: Path, artifact: SourceArtifact | None = None) -> list[TransitionRecord]:
    records: list[TransitionRecord] = []
    uri = f"file:{quote(str(path.resolve()), safe='/:')}?mode=ro"
    try:
        con = sqlite3.connect(uri, uri=True)
        columns = {row[1] for row in con.execute("PRAGMA table_info(spans)").fetchall()}
        select_cols = [col for col in ("name", "attributes", "status_code", "status_message", "start_time") if col in columns]
        if not select_cols:
            return []
        sql = f"SELECT {', '.join(select_cols)} FROM spans"
        for row in con.execute(sql).fetchall():
            item = dict(zip(select_cols, row, strict=False))
            observation = "\n".join(str(item.get(key) or "") for key in select_cols)
            record = _record_from_observation(
                observation=observation,
                source_path=str(path),
                run_id=_run_id_from_path(path),
                source_kind=(artifact.kind if artifact else "phoenix_db"),
            )
            if record:
                records.append(record)
    except sqlite3.Error:
        return records
    finally:
        try:
            con.close()  # type: ignore[name-defined]
        except Exception:
            pass
    return records


def export_sage_artifact(path: Path, artifact: SourceArtifact | None = None) -> list[TransitionRecord]:
    """Extract labeled transitions from a Sage checkpoint SQLite DB in read-only mode."""
    records: list[TransitionRecord] = []
    uri = f"file:{quote(str(path.resolve()), safe='/:')}?mode=ro"
    try:
        limit = int(os.environ.get("SAGE_TRAJECTORY_SAGE_DB_ROW_LIMIT", "5000") or 5000)
    except (TypeError, ValueError):
        limit = 5000
    try:
        con = sqlite3.connect(uri, uri=True)
        tables = [
            row[0]
            for row in con.execute(
                "SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'"
            ).fetchall()
        ]
        for table in tables:
            columns = con.execute(f"PRAGMA table_info({_quote_identifier(table)})").fetchall()
            names = [row[1] for row in columns if row[1]]
            if not names:
                continue
            selected = ", ".join(_quote_identifier(name) for name in names)
            sql = f"SELECT {selected} FROM {_quote_identifier(table)} LIMIT ?"
            for row in con.execute(sql, (limit,)).fetchall():
                chunks = []
                for name, value in zip(names, row, strict=False):
                    if isinstance(value, bytes):
                        continue
                    if value is None:
                        continue
                    chunks.append(f"{name}={str(value)[:4000]}")
                if not chunks:
                    continue
                observation = "\n".join(chunks)
                record = _record_from_observation(
                    observation=observation,
                    source_path=str(path),
                    run_id=_run_id_from_path(path),
                    source_kind=(artifact.kind if artifact else "sage_db"),
                )
                if record:
                    records.append(record)
    except sqlite3.Error:
        return records
    finally:
        try:
            con.close()  # type: ignore[name-defined]
        except Exception:
            pass
    return records


def export_ledger_artifact(path: Path, artifact: SourceArtifact | None = None) -> list[TransitionRecord]:
    try:
        data = json.loads(_read_text(path))
    except json.JSONDecodeError:
        return []
    rows = _ledger_rows(data)
    records: list[TransitionRecord] = []
    for row in rows:
        status = str(row.get("status") or row.get("verdict") or "").casefold()
        if status not in {"achieved", "failed", "blocked"}:
            continue
        technique = str(row.get("technique") or row.get("capability") or "")
        target = str(row.get("target") or "")
        labels = ("ledger_" + status,)
        record = TransitionRecord(
            run_id=_run_id_from_path(path),
            source_files=(str(path),),
            objective=str(data.get("objective") or ""),
            capability=technique,
            inputs={"target": target} if target else {},
            observations=(
                TransitionObservation(
                    kind="ledger_row",
                    labels=labels,
                    excerpt=redact_text(row.get("evidence") or row)[:600],
                    source=str(path),
                ),
            ),
            verifier=TransitionVerifier(status=status, labels=labels),
            failure_label="" if status == "achieved" else "ledger_" + status,
            repair=None,
            state_after={"effects_added": row.get("effects") or row.get("effect") or []},
        )
        records.append(record)
    return records


def _record_from_observation(
    observation: str,
    source_path: str,
    run_id: str,
    source_kind: str,
    label_hint: str | None = None,
) -> TransitionRecord | None:
    classification = classify_observation(observation)
    if classification.label == "unclassified":
        return None
    primary_label = label_hint if label_hint in classification.labels else classification.label
    repair_data = repair_for_label(primary_label)
    repair = (
        TransitionRepair(kind=repair_data[0], retry_budget=repair_data[1], notes=repair_data[2])
        if repair_data
        else None
    )
    capability, command = _infer_capability_and_command(observation, classification.label)
    labels = tuple(label for label in classification.labels if label)
    return TransitionRecord(
        run_id=run_id,
        source_files=(source_path,),
        objective="administrative-control:target-domain",
        capability=capability,
        observations=(
            TransitionObservation(
                kind=source_kind,
                labels=labels,
                excerpt=redact_text(_squash(observation))[:1000],
                source=source_path,
            ),
        ),
        verifier=TransitionVerifier(status="failed", labels=labels, evidence={"classification": primary_label}),
        failure_label=primary_label,
        repair=repair,
        commands=(command,) if command else (),
        env_fingerprint={"source_kind": source_kind},
        state_before={"features": _state_features_for_label(primary_label)},
        state_after={"effects_added": [], "effects_failed": [capability]},
    )


def _infer_capability_and_command(observation: str, label: str) -> tuple[str, TransitionCommand | None]:
    lower = observation.casefold()
    if label == "command_size_limit":
        if "gpo" in lower or "scheduledtasks.xml" in lower or "group policy" in lower:
            return "gpo-controlled-system-exec", None
        return "issue-mythic-command", None
    if label == "unresolved_gpo_identity":
        return "gpo-controlled-system-exec", None
    if label == "command_template_error":
        if "gpo" in lower or "scheduledtasks.xml" in lower or "group policy" in lower:
            return "gpo-controlled-system-exec", TransitionCommand("gpo-task", adapter="mythic", constructed_from_builder=True)
        return "issue-mythic-command", None
    if label == "directory_bind_error":
        if "gpo" in lower or "scheduledtasks.xml" in lower or "group policy" in lower:
            return "gpo-controlled-system-exec", TransitionCommand("gpo-task", adapter="mythic", constructed_from_builder=True)
        return "directory-write", None
    if "dcsync" in lower or "drsr" in lower or "cracknames" in lower:
        return "dcsync-account", TransitionCommand("dcsync", adapter="mythic", constructed_from_builder=False)
    if "gpo" in lower or "scheduledtasks.xml" in lower or "group policy" in lower:
        return "gpo-controlled-system-exec", TransitionCommand("gpo-task", adapter="mythic", constructed_from_builder=False)
    if "parameter group" in lower or "invalid parameters" in lower:
        return "issue-mythic-command", None
    if "kdc_err_client_not_trusted" in lower or "certificate" in lower:
        return "adcs-certificate-auth", None
    if label == "wrong_security_context":
        return "ensure-kerberos-context", None
    return "unknown-capability", None


def _state_features_for_label(label: str) -> list[str]:
    return {
        "ambiguous_account_name": ["directory_operation", "unqualified_principal"],
        "delayed_effect": ["delayed_control_plane", "proof_absent"],
        "unresolved_gpo_identity": ["gpo_identity_unresolved", "exact_guid_required"],
        "dcsync_bad_dn_or_context": ["drsuapi_reached", "dcsync_target_or_context_unresolved"],
        "access_denied": ["privileged_operation", "access_denied"],
        "wrong_security_context": ["credential_or_ticket_available", "context_not_applied"],
        "command_size_limit": ["constructed_command_too_large", "staging_or_shortening_required"],
        "command_template_error": ["deterministic_builder_template", "escaping_or_runtime_syntax"],
        "directory_bind_error": ["directory_write", "writable_dc_bind_required"],
        "schema_or_argument_mismatch": ["payload_schema_required"],
    }.get(label, [label])


def _interesting_windows(text: str, window: int = 700) -> list[tuple[str | None, str]]:
    starts: list[tuple[str, int]] = []
    for label, pattern in (
        ("ambiguous_account_name", r"ERROR_NOT_UNIQUE"),
        ("ambiguous_account_name", r"CrackNames\s+\(name status\):\s*0x0*3\b"),
        ("command_template_error", r"specified wildcard character pattern is not valid"),
        ("command_template_error", r"cannot call a method on a null-valued expression"),
        ("directory_bind_error", r"retrieving member \"Put\".*?(operations error|referral)"),
        ("directory_bind_error", r"A referral was returned from the server"),
        ("unresolved_gpo_identity", r"GPO not found in SYSVOL policy root"),
        ("unresolved_gpo_identity", r"GPO identity unresolved"),
        ("delayed_effect", r"GPO setup pending"),
        ("delayed_effect", r"ScheduledTasks\.xml"),
        ("access_denied", r"DS_DRA_ACCESS_DENIED"),
        ("schema_or_argument_mismatch", r"Supplied Arguments.*?parameter group"),
        ("wrong_security_context", r"KDC_ERR_CLIENT_NOT_TRUSTED"),
        ("access_denied", r"Access is denied"),
    ):
        for match in re.finditer(pattern, text, re.I | re.S):
            starts.append((label, match.start()))
    if not starts:
        return [(None, text[:window])] if classify_observation(text).label != "unclassified" else []
    chunks: list[tuple[str | None, str]] = []
    for label, start in sorted(set(starts), key=lambda item: item[1]):
        lo = max(0, start - window // 2)
        hi = min(len(text), start + window // 2)
        chunks.append((label, text[lo:hi]))
    return chunks


def _ledger_rows(data: Any) -> list[dict[str, Any]]:
    if isinstance(data, dict):
        for key in ("hops", "rows", "records", "ledger", "history"):
            value = data.get(key)
            if isinstance(value, list):
                return [item for item in value if isinstance(item, dict)]
        if all(key in data for key in ("technique", "target", "status")):
            return [data]
    if isinstance(data, list):
        return [item for item in data if isinstance(item, dict)]
    return []


def _read_text(path: Path) -> str:
    return path.read_text(encoding="utf-8", errors="replace")


def _run_id_from_path(path: Path) -> str:
    name = path.name
    for suffix in (".phoenix.db", ".sage.db", ".db", ".json", ".out", ".log", ".txt"):
        if name.endswith(suffix):
            return name[: -len(suffix)]
    return path.stem


def _squash(text: str) -> str:
    return " ".join(str(text).split())


def _quote_identifier(value: str) -> str:
    return '"' + str(value).replace('"', '""') + '"'
