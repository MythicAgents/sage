"""Engagement State Ledger formatting and historical JSON helpers.

Live ledger persistence is in :mod:`engagement_store` (``sage.db``): sessions read fresh operation snapshots and
write explicit row mutations there. This module keeps what does not touch the live store: the legacy JSON file
location and neutral reader used by migration, archive and evaluation tooling; selectors; per-hop proof
admission; and the one markdown table every ledger view renders. Pure stdlib.
"""
import json
import os
from pathlib import Path
import re

_FILENAME_SAFE = re.compile(r"[^A-Za-z0-9._-]")
# Actor of the one-time schema migration that retracts stored failed/blocked rows as attempt records.
SCHEMA_MIGRATION_ACTOR = "system:schema-migration"
# Actor of the schema-3 revalidation that folds old rows for one typed effect into one row.
REVALIDATION_ACTOR = "system:proof-revalidation"


def default_engagement_id() -> str:
    """The explicit legacy namespace (``SAGE_ENGAGEMENT_ID``), or ``""``. ``default`` is not a namespace."""
    value = os.environ.get("SAGE_ENGAGEMENT_ID", "").strip()
    return "" if value == "default" else value


def state_dir() -> str:
    """Directory of legacy ``state_*.json`` files (``SAGE_ENGAGEMENT_STATE_DIR``; default ``.sage_engagement`` in
    the process cwd). It also holds decision records and artifacts. It is never the live ledger store.
    """
    override = os.environ.get("SAGE_ENGAGEMENT_STATE_DIR", "").strip()
    if override:
        return override
    return os.path.join(os.getcwd(), ".sage_engagement")


_LEDGER_PREFIX = "state_"


def ledger_path(engagement_id: str) -> str:
    """Path of the LEGACY JSON ledger file for a key, as the pre-SQLite runtime named it (migration input)."""
    key = (engagement_id or "").strip()
    if not key:
        raise ValueError("a legacy ledger path needs an explicit engagement key")
    safe = _FILENAME_SAFE.sub("_", key)[:128]
    return os.path.join(state_dir(), f"{_LEDGER_PREFIX}{safe}.json")


def load_file(path) -> dict:
    """Neutral reader for a historical JSON ledger or ESL export. Never applies runtime proof admission.

    Returns an empty skeleton when the file is missing or corrupt. Callers that need runtime eligibility apply
    their own verifier policy; this reader never mutates the source.
    """
    try:
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)
        if isinstance(data, dict):
            data.setdefault("hops", [])
            return data
    except (FileNotFoundError, ValueError, OSError):
        pass
    return {"hops": []}


def load(engagement_id: str) -> dict:
    """Neutral reader for the legacy JSON ledger file of a key (historical/migration use only)."""
    data = load_file(ledger_path(engagement_id))
    data.setdefault("engagement_id", engagement_id)
    return data


def save_file(data: dict, path) -> str:
    """Write a raw ledger/export dict to an explicit file (atomic replace). Never touches the live store."""
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    tmp = target.with_name(f".{target.name}.{os.getpid()}.tmp")
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2, sort_keys=True)
    os.replace(tmp, target)
    return str(target)


def status_label(hop) -> str:
    """A row's status for display: ``achieved``, ``pending (awaiting effect)`` or ``pending (proof missing)``.

    Rows stored before the two-status schema keep their historical status word in history views.
    """
    if isinstance(hop, dict):
        status, reason = hop.get("status"), hop.get("pending_reason")
    else:
        status, reason = getattr(hop, "status", ""), getattr(hop, "pending_reason", "")
    status = str(status or "").strip() or "-"
    if status.casefold() != "pending":
        return status
    reason = str(reason or "").strip().casefold() or "awaiting_effect"
    return f"pending ({reason.replace('_', ' ')})"


def is_attempt_record_retraction(corrections) -> bool:
    """Whether a row's retraction is the schema migration's: a former failed/blocked attempt record."""
    retracted = corrections.get("retracted") if isinstance(corrections, dict) else None
    return isinstance(retracted, dict) and str(retracted.get("actor") or "") == SCHEMA_MIGRATION_ACTOR


def is_revalidation_merge(corrections) -> bool:
    """Whether a row's retraction is the revalidation's: an old row folded into the one row for its effect."""
    retracted = corrections.get("retracted") if isinstance(corrections, dict) else None
    return isinstance(retracted, dict) and str(retracted.get("actor") or "") == REVALIDATION_ACTOR


def correction_label(hop: dict) -> str:
    """``achieved — operator retracted`` style presentation of a row's status plus its effective correction."""
    status = status_label(hop) if isinstance(hop, dict) else "-"
    corrections = hop.get("corrections") if isinstance(hop, dict) and isinstance(hop.get("corrections"), dict) else {}
    if is_attempt_record_retraction(corrections):
        return f"{status} — retracted attempt record"
    if is_revalidation_merge(corrections):
        return f"{status} — merged into one row per effect"
    if corrections.get("retracted"):
        return f"{status} — operator retracted"
    marker = corrections.get("no_longer_holds") if isinstance(corrections.get("no_longer_holds"), dict) else {}
    if marker.get("effective"):
        return f"{status} — no longer holds"
    return status


def hop_corrected(hop) -> bool:
    """Whether a hop (dict or Hop) carries an effective retraction or "no longer holds" marker."""
    corrections = hop.get("corrections") if isinstance(hop, dict) else getattr(hop, "corrections", None)
    if not isinstance(corrections, dict):
        return False
    if corrections.get("retracted"):
        return True
    marker = corrections.get("no_longer_holds")
    return isinstance(marker, dict) and bool(marker.get("effective"))


# --- hop selection + edit operations (operate on the ledger dict's "hops" list) ------------------------

def hop_label(hop: dict) -> str:
    """A stable selector for a hop: its `id`, else `technique:target`."""
    if not isinstance(hop, dict):
        return ""
    hid = str(hop.get("id") or "").strip()
    if hid:
        return hid
    technique = str(hop.get("technique") or "").strip()
    target = str(hop.get("target") or "").strip()
    return f"{technique}:{target}".strip(":")


def _hop_matches(hop: dict, selector_cf: str) -> bool:
    """A hop matches if the selector exactly equals its label, effect, or technique (case-insensitive)."""
    if not isinstance(hop, dict) or not selector_cf:
        return False
    return selector_cf in {
        hop_label(hop).casefold(),
        str(hop.get("effect") or "").casefold(),
        str(hop.get("technique") or "").casefold(),
    }


def hop_number(hop: dict) -> int:
    """A hop's permanent display number (the `#` column), or 0 when it has none yet."""
    value = hop.get("number") if isinstance(hop, dict) else None
    if isinstance(value, bool) or not isinstance(value, int) or value < 1:
        return 0
    return value


def _resolve_index(selector: str, hops: list) -> int | None:
    """If `selector` is a hop number (the `#` column of `/state`), return that hop's 0-based index, else None.
    Hop labels are never bare integers, so a numeric selector unambiguously means a hop number.
    """
    s = (selector or "").strip()
    if s.isdigit():
        wanted = int(s)
        for i, hop in enumerate(hops):
            if hop_number(hop) == wanted:
                return i
    return None


def hop_proof_admission(hop: dict, engagement_id: str) -> tuple[bool, str]:
    """Runtime proof admission for one stored hop (the same checks every runtime projection applies)."""
    try:
        try:
            from . import proof_boundary
        except ImportError:
            import proof_boundary
    except Exception:
        return False, "proof admission failed closed"
    evidence = hop.get("evidence") if isinstance(hop.get("evidence"), dict) else {}
    proof = hop.get("proof_envelope") if isinstance(hop.get("proof_envelope"), dict) and hop.get("proof_envelope") \
        else evidence.get("proof_envelope")
    # A typed row's headline proof must cover every effect the row claims (a review-backed proof covers its credential
    # material only); other rows keep the runtime rule.
    admit = proof_boundary.admit_runtime_envelope
    if hop.get("canonical_effect"):
        effects = [str(item) for item in hop.get("satisfied_effects") or []]
        admit = lambda envelope, **kw: proof_boundary.admit_task_observation(  # noqa: E731
            envelope, claimed_effects=proof_boundary.row_headline_claims(envelope, effects), **kw)
    try:
        admission = admit(
            proof_boundary.ProofEnvelope.from_dict(proof),
            current_engagement_id=str(engagement_id or ""),
            expected_callback_id=evidence.get("callback_id") or evidence.get("callback_display_id") or "",
            expected_transaction_id=evidence.get("transaction_id") or "",
            expected_task_id=evidence.get("mythic_task_id") or evidence.get("task_id") or "",
            expected_terminal_status=evidence.get("terminal_task_status") or evidence.get("terminal_status") or "",
            expected_verifier_id=evidence.get("verifier_id") or "",
            expected_verifier_input_sha256=evidence.get("verifier_input_sha256") or "",
            expected_verifier_result_sha256=evidence.get("verifier_result_sha256") or "",
            expected_artifact_id=evidence.get("artifact_id") or "",
            expected_authorization=evidence,
        )
        return admission.admitted, admission.reason
    except Exception:
        return False, "proof admission failed closed"


def quarantine_hop(hop: dict, engagement_id: str) -> dict:
    """In a runtime view only: an ``achieved`` row without admissible proof reads as ``pending`` / ``proof_missing``.

    The stored historical row is unchanged; this never turns a historical row into live proof.
    """
    if not isinstance(hop, dict) or str(hop.get("status") or "").casefold() != "achieved":
        return hop
    admitted, reason = hop_proof_admission(hop, engagement_id)
    if admitted:
        return hop
    hop["status"] = "pending"
    hop["pending_reason"] = "proof_missing"
    evidence = dict(hop.get("evidence") if isinstance(hop.get("evidence"), dict) else {})
    evidence["proof_persistence_state"] = "legacy_unverified"
    evidence["proof_admission_reason"] = reason
    hop["evidence"] = evidence
    return hop


def _quarantine_unproven_achievements(data: dict, engagement_id: str) -> None:
    """Quarantine every unproven ``achieved`` row of a ledger dict in place (historical-reader helper)."""
    for hop in data.get("hops") or []:
        quarantine_hop(hop, engagement_id)


# --- shared ledger view (one table for every surface that shows hops) -----------------------------------

def md_cell(value, limit: int = 40) -> str:
    """Sanitize a value for a SINGLE markdown table cell.

    Ledger evidence is often a bytes-repr (`b'...\\r\\n...'`) or multi-line tool output. A raw CR/LF/tab
    in a cell makes Mythic's markdown renderer split the row into phantom lines. So: unwrap the bytes-repr,
    drop its literal escapes, collapse EVERY real whitespace run to one space, escape pipes, and hard-truncate.
    """
    s = str(value if value is not None else "")
    if len(s) >= 3 and s[0] == "b" and s[1] in ("'", '"') and s[-1] == s[1]:
        s = s[2:-1]                                                      # b'...' / b"..." → inner text
    s = s.replace("\\r", " ").replace("\\n", " ").replace("\\t", " ")    # literal escapes from the repr
    s = " ".join(s.split())                                             # collapse real CR/LF/tab/space runs
    s = s.replace("|", "\\|")                                            # escape the markdown column pipe
    if len(s) > limit:
        s = s[: limit - 1] + "…"
    return s or "-"


def _code(value, limit: int = 60, *, cell: bool = False) -> str:
    """A value as one inline code span: whitespace collapsed, backticks removed, bounded. Markdown inside it is inert.

    In a table cell a pipe is escaped, as GitHub-flavoured tables require even inside code spans.
    """
    text = " ".join(str(value if value is not None else "").replace("`", "").split())
    if len(text) > limit:
        text = text[: limit - 1] + "…"
    if cell:
        text = text.replace("|", "\\|")
    return f"`{text}`" if text else "`-`"


def _task_link(task: str, text: str = "", *, cell: bool = False) -> str:
    """A Mythic task-page link for a numeric display ID; anything else is shown as inert text, never a link."""
    if task.isascii() and task.isdigit():
        return f"[{text or task}](/new/task/{task})"
    return f"{text + ' ' if text else ''}{_code(task, 24, cell=cell)}"


def task_ids(hop) -> list[str]:
    """Distinct Mythic task display IDs a row names: every contribution of a typed row, else its evidence task."""
    get = hop.get if isinstance(hop, dict) else (lambda key, default=None: getattr(hop, key, default))
    tasks: list[str] = []
    contributions = get("contributions")
    if get("canonical_effect") and isinstance(contributions, list):
        candidates = [c.get("task_id") for c in contributions if isinstance(c, dict)]
    else:
        evidence = get("evidence") if isinstance(get("evidence"), dict) else {}
        candidates = [evidence.get("mythic_task_id") or evidence.get("task_id")]
    for task in candidates:
        text = str(task if task is not None else "").strip()
        if text and text not in tasks:
            tasks.append(text)
    return tasks


def render_hop_table(hops, *, limit: int = 0) -> list[str]:
    """Render hops as markdown table lines keyed by permanent hop number, ascending.

    Every surface that shows hops (`/state`, the turn-end summary, and the model's copy of either) uses this,
    so "hop N" names the same row everywhere. `limit` keeps the N highest-numbered (newest) hops. The Task column
    links each Mythic task the row names; ``evidence_notes`` explains each one. No task output is rendered.
    """
    rows = sorted((h for h in (hops or []) if isinstance(h, dict)), key=hop_number)
    if limit and len(rows) > limit:
        rows = rows[-limit:]
    lines = ["| # | Hop | Effect | Status | Task | CB |", "|---|---|---|---|---|---|"]
    for hop in rows:
        ev = hop.get("evidence") if isinstance(hop.get("evidence"), dict) else {}
        cb_id = ev.get("callback_id")
        number = hop_number(hop)
        tasks = ", ".join(_task_link(task, cell=True) for task in task_ids(hop)) or "-"
        lines.append(
            f"| {number or '-'} | `{md_cell(hop_label(hop), 48)}` | {md_cell(hop.get('effect'))} | "
            f"`{md_cell(correction_label(hop), 40)}` | {tasks} | "
            f"{md_cell(cb_id, 12) if cb_id not in (None, '') else '-'} |"
        )
    return lines


_CLASS_LABELS = {
    "terminal_success": "terminal success",
    "postprocessing_error_with_output": "completion-function error with output",
    "agent_error_or_failed": "agent error or failed",
    "not_agent_processed": "not processed by the agent",
    "unknown": "unknown",
}
_WRITER_LABELS = (
    ("raw", "raw"), ("capability", "capability"), ("reconcile", "task history"),
    ("credential_store", "credential store"), ("review", "credential review"), ("operator", "operator review"),
    ("legacy", "earlier record"),
)


def _writer_label(writer: str) -> str:
    kind = str(writer or "").split(":", 1)[0]
    return next((label for name, label in _WRITER_LABELS if name == kind), _code(writer, 32))


def _writer_labels(writers) -> list[str]:
    order = {name: index for index, (name, _label) in enumerate(_WRITER_LABELS)}
    labels: list[str] = []
    for writer in sorted(writers, key=lambda w: order.get(str(w).split(":", 1)[0], len(order))):
        label = _writer_label(writer)
        if label not in labels:
            labels.append(label)
    return labels


def _class_label(value) -> str:
    text = str(value or "").strip()
    return _CLASS_LABELS.get(text, _code(text, 40)) if text else ""


def _observations(contribution: dict):
    for writer, slots in (contribution.get("writers") or {}).items():
        if isinstance(slots, dict):
            for slot in ("admitted", "rejected"):
                if isinstance(slots.get(slot), dict):
                    yield str(writer), slots[slot]


def _typed_notes(hop: dict, number) -> list[str]:
    """One line per task on a typed row, from the stored structured fields only."""
    try:
        try:
            from . import task_evidence
        except ImportError:
            import task_evidence  # type: ignore
        canonical = task_evidence.canonical_from_identity(hop.get("canonical_effect"))
    except Exception:
        canonical = None
    subject = _code(canonical.subject if canonical is not None else hop.get("canonical_effect"), 80)
    lines = []
    for contribution in hop.get("contributions") or []:
        if not isinstance(contribution, dict) or not str(contribution.get("task_id") or "").strip():
            continue
        task = str(contribution["task_id"]).strip()
        observations = list(_observations(contribution))
        writers = list(dict.fromkeys(writer for writer, _obs in observations))
        if "admitted_writers" in contribution:
            admitted = [w for w in contribution.get("admitted_writers") or [] if w in writers]
        elif str(hop.get("status") or "").casefold() != "achieved":  # stored before views needed it
            admitted = []
        else:
            admitted = [w for w in writers if isinstance((contribution["writers"].get(w) or {}).get("admitted"), dict)]
        reviews = [r for r in (contribution.get("reviews") or {}).values() if isinstance(r, dict)]
        review = next((r for r in reviews if r.get("verdict") == "valid"), reviews[-1] if reviews else None)
        if admitted:
            strengths = {str((contribution["writers"][w].get("admitted") or {}).get("strength") or "") for w in admitted}
            if "working" in strengths:
                claim = f"proves working credential for {subject}"
            else:
                candidate = review.get("candidate") if isinstance(review, dict) and isinstance(
                    review.get("candidate"), dict) else {}
                kind = candidate.get("type") if isinstance(review, dict) and review.get("verdict") == "valid" else ""
                if not kind:
                    kind = next((str((contribution["writers"][w].get("admitted") or {}).get("material_kind") or "")
                                 for w in admitted), "")
                claim = f"proves {_code(kind or 'unknown', 24)} material for {subject}"
        else:
            claim = f"does not prove {subject}"
        parts = [f"- hop {number or '-'} · {_task_link(task, f'task {task}' if task.isascii() and task.isdigit() else 'task')}: {claim}"]
        task_class = next((obs.get("evidence", {}).get("task_classification") for _w, obs in observations
                           if isinstance(obs.get("evidence"), dict) and obs["evidence"].get("task_classification")), "")
        if task_class:
            parts.append(f"Mythic: {_class_label(task_class)}")
        if admitted:
            parts.append("admitted: " + ", ".join(_writer_labels(admitted)))
        rejected = [w for w in writers if w not in admitted]
        if rejected:
            parts.append("not admitted: " + ", ".join(_writer_labels(rejected)))
        if isinstance(review, dict):
            reviewer = _code(review.get("reviewer_kind") or "unknown", 24)[1:-1]
            who = review.get("model") if review.get("reviewer_kind") == "llm" else (
                review.get("reviewer_id") if review.get("reviewer_kind") == "operator" else "")
            reviewer += f" {_code(who, 48)}" if who else ""
            parts.append(f"reviewer: {reviewer} ({_code(review.get('verdict') or 'unknown', 16)[1:-1]})")
        lines.append(" · ".join(parts))
    return lines


def evidence_notes(hops, *, limit: int = 0) -> list[str]:
    """One line per (row, Mythic task), built only from structured fields (no output, candidate or model text).

    Corrected rows are history and are explained by ``correction_notes`` instead.
    """
    rows = sorted((h for h in (hops or []) if isinstance(h, dict)), key=hop_number)
    if limit and len(rows) > limit:
        rows = rows[-limit:]
    lines: list[str] = []
    for hop in rows:
        if hop_corrected(hop):
            continue
        number = hop_number(hop)
        if hop.get("canonical_effect") and isinstance(hop.get("contributions"), list):
            lines.extend(_typed_notes(hop, number))
            continue
        tasks = task_ids(hop)
        if not tasks:
            continue
        evidence = hop.get("evidence") if isinstance(hop.get("evidence"), dict) else {}
        verb = "proves" if str(hop.get("status") or "").casefold() == "achieved" else "does not prove"
        task = tasks[0]
        parts = [f"- hop {number or '-'} · {_task_link(task, f'task {task}' if task.isascii() and task.isdigit() else 'task')}: "
                 f"{verb} {_code(hop.get('effect'), 80)}"]
        if evidence.get("task_classification"):
            parts.append(f"Mythic: {_class_label(evidence.get('task_classification'))}")
        try:
            try:
                from . import task_evidence
            except ImportError:
                import task_evidence  # type: ignore
            writer = task_evidence.writer_kind(str(hop.get("technique") or ""), evidence)
        except Exception:
            writer = str(evidence.get("source") or "")
        if writer and writer != "writer":
            parts.append(f"source: {_writer_label(writer)}")
        lines.append(" · ".join(parts))
    return lines


def correction_notes(hops, *, limit: int = 0) -> list[str]:
    """One bounded line per corrected row: who/what corrected it and why. No evidence previews."""
    rows = sorted((h for h in (hops or []) if isinstance(h, dict) and hop_corrected(h)), key=hop_number)
    if limit and len(rows) > limit:
        rows = rows[-limit:]
    lines = []
    for hop in rows:
        corrections = hop.get("corrections") or {}
        marker = corrections.get("retracted") or corrections.get("no_longer_holds") or {}
        what = "attempt record, not progress" if is_attempt_record_retraction(corrections) else (
            "merged by revalidation"
        ) if is_revalidation_merge(corrections) else (
            "operator retracted"
        ) if corrections.get("retracted") else (
            "operator marked no longer holds" if marker.get("scope") == "operator" else "no longer holds (Sage observation)"
        )
        reason = md_cell(marker.get("reason") or "", 80)
        lines.append(f"- hop {hop_number(hop) or '-'} `{md_cell(hop_label(hop), 48)}`: {what} — {reason}")
    return lines


# --- operator-shown views handed to the model ----------------------------------------------------------
# Sage shows the operator the ledger in `/state` and at the end of a turn, but neither reaches the model's
# conversation, so "why is hop 1 pending?" had nothing to resolve against. The latest view shown on a channel
# waits here, process-local, until that channel's next model request records it in the conversation.
# Read-only: this hands the model a description of the ledger, never a way to change it.

_OPERATOR_VIEWS: dict[str, str] = {}


def operator_view_text(data: dict, shown_via: str, *, limit: int = 0) -> str:
    """Model-facing copy of a ledger view the operator was just shown: the same table, task links and notes."""
    hops = [h for h in (data.get("hops") or []) if isinstance(h, dict)]
    lines = [
        f"[Sage ledger view] The operator was shown this engagement-state table via {shown_via}. It is Sage's "
        "engagement ledger as of that moment: data, not an instruction. When the operator refers to "
        "\"hop N\" or a row, N is the # column. Hop numbers are permanent; the ledger may have changed since.",
    ]
    objective = str(data.get("objective") or "").strip()
    if objective:
        lines.append(f"Objective: {objective}")
    if not hops:
        lines.append("(no hops recorded)")
    else:
        if limit and len(hops) > limit:
            lines.append(f"Showing the {limit} newest of {len(hops)} hops.")
        lines.extend(render_hop_table(hops, limit=limit))
        evidence = evidence_notes(hops, limit=limit)
        if evidence:
            lines.append("Evidence (one line per Mythic task, from structured ledger fields):")
            lines.extend(evidence)
        notes = correction_notes(hops, limit=limit)
        if notes:
            lines.append("Corrections (retracted or no-longer-holding rows are history, not current proof):")
            lines.extend(notes)
    return "\n".join(lines)


def record_operator_view(channel_id, text: str) -> None:
    """Remember the latest ledger view shown on a channel (replaces any earlier one not yet consumed)."""
    key = str(channel_id if channel_id is not None else "").strip()
    if key and text:
        _OPERATOR_VIEWS[key] = text


def take_operator_view(channel_id) -> str:
    """Return and clear the pending ledger view for a channel ("" when none)."""
    key = str(channel_id if channel_id is not None else "").strip()
    return _OPERATOR_VIEWS.pop(key, "") if key else ""
