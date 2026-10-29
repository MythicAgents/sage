"""Single source of truth for the durable per-engagement hop ledger on disk.

Used by BOTH the LangGraph agent (`ai/langgraph/mythic_tools.py`, which records achieved hops during a solve)
and the operator-facing `state` Mythic command (`container/agent_functions/state.py`, which shows and
edits the ledger). Keeping the path + I/O + edit operations in one place stops the agent and the command from
drifting onto different files or formats. Pure stdlib; never raises on a missing/corrupt file (returns an empty
ledger). Edits operate on the plain ledger dict so they are trivially testable without the agent runtime.
"""
import json
import os
import re

_FILENAME_SAFE = re.compile(r"[^A-Za-z0-9._-]")


def default_engagement_id() -> str:
    return os.environ.get("SAGE_ENGAGEMENT_ID", "").strip() or "default"


def state_dir() -> str:
    """Directory holding the per-engagement ledgers. `SAGE_ENGAGEMENT_STATE_DIR` overrides; default is
    `.sage_engagement` next to the Sage process cwd (same persistence guarantee as the operational db).
    """
    override = os.environ.get("SAGE_ENGAGEMENT_STATE_DIR", "").strip()
    if override:
        return override
    return os.path.join(os.getcwd(), ".sage_engagement")


# Ledger filename prefix. Named `state_` (the operator-facing `state` command) and keyed per Mythic
# OPERATION — e.g. `state_Operation_Chimera_1.json`.
_LEDGER_PREFIX = "state_"


def ledger_path(engagement_id: str | None = None) -> str:
    """Absolute path to the JSON ledger for an engagement/operation key (sanitized to a safe filename)."""
    key = (engagement_id or default_engagement_id() or "default").strip() or "default"
    safe = _FILENAME_SAFE.sub("_", key)[:128]
    return os.path.join(state_dir(), f"{_LEDGER_PREFIX}{safe}.json")


def load(engagement_id: str | None = None) -> dict:
    """Return the raw ledger dict for an engagement; an empty skeleton if missing/corrupt. Never raises.

    This is intentionally a neutral serializer. Runtime consumers that can influence autonomous state must use
    :func:`load_runtime`, which quarantines historical ``achieved`` rows that lack an admissible proof envelope.
    Eval readers and migration tooling need raw historical rows so they can apply their own verifier policy
    without mutating the source ledger as a side effect of reading it.
    """
    path = ledger_path(engagement_id)
    try:
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)
        if isinstance(data, dict):
            data.setdefault("hops", [])
            return data
    except (FileNotFoundError, ValueError, OSError):
        pass
    return {"engagement_id": engagement_id or default_engagement_id(), "hops": []}


def save(data: dict, engagement_id: str | None = None) -> str:
    """Write the raw ledger dict (atomic replace). Creates the directory if needed. Returns the path.

    Runtime callers should use :func:`save_runtime` so unbound achievements cannot be re-persisted as active
    runtime proof. Raw save exists for evaluator fixtures, archival/migration tooling, and other consumers whose
    own verifier policy is separate from the live autonomous ledger.
    """
    path = ledger_path(engagement_id)
    os.makedirs(os.path.dirname(path), exist_ok=True)
    tmp = f"{path}.tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2, sort_keys=True)
    os.replace(tmp, path)
    return path


def load_runtime(engagement_id: str | None = None) -> dict:
    """Load a ledger for live runtime use and quarantine unbound historical achievements in memory."""
    data = load(engagement_id)
    _quarantine_unproven_achievements(data, engagement_id or data.get("engagement_id") or default_engagement_id())
    assign_hop_numbers(data)
    return data


def save_runtime(data: dict, engagement_id: str | None = None) -> str:
    """Persist a live runtime ledger after quarantining unbound historical achievements."""
    _quarantine_unproven_achievements(data, engagement_id or data.get("engagement_id") or default_engagement_id())
    assign_hop_numbers(data)
    return save(data, engagement_id)


def wipe(engagement_id: str | None = None) -> bool:
    """Delete the ledger file for an engagement. Returns True if a file was removed."""
    path = ledger_path(engagement_id)
    try:
        os.remove(path)
        return True
    except OSError:
        return False


def list_engagements() -> list[str]:
    """Engagement ids that currently have a ledger on disk."""
    out: list[str] = []
    try:
        for name in os.listdir(state_dir()):
            if name.startswith(_LEDGER_PREFIX) and name.endswith(".json"):
                out.append(name[len(_LEDGER_PREFIX):-len(".json")])
    except OSError:
        pass
    return sorted(out)


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


def assign_hop_numbers(data: dict, previous: dict | None = None) -> dict:
    """Give every hop a permanent number. Idempotent; mutates and returns `data`.

    A number is assigned once and never reused: removing a hop leaves a gap, and `next_hop_number` keeps the
    high-water mark. Numbering is what the operator and the model both refer to as "hop N", so it must not
    shift when rows are removed. Unnumbered hops (older ledgers, or writers that rebuild hop dicts from
    dataclasses) take their number from `previous` (the ledger as last written) by hop label, else the next
    free number in list order. The first hop claiming a number keeps it; a duplicate is renumbered.
    """
    hops = data.get("hops")
    if not isinstance(hops, list):
        return data
    high = 0
    for source in (previous, data):
        if isinstance(source, dict):
            try:
                high = max(high, int(source.get("next_hop_number") or 1) - 1)
            except (TypeError, ValueError):
                pass
    earlier: dict[str, int] = {}
    if isinstance(previous, dict):
        for hop in previous.get("hops") or []:
            number = hop_number(hop)
            if number:
                earlier.setdefault(hop_label(hop), number)
                high = max(high, number)
    claimed: set[int] = set()
    unnumbered: list[dict] = []
    for hop in hops:
        if not isinstance(hop, dict):
            continue
        number = hop_number(hop)
        if number and number not in claimed:
            claimed.add(number)
            high = max(high, number)
        else:
            unnumbered.append(hop)
    for hop in unnumbered:
        number = earlier.get(hop_label(hop), 0)
        if not number or number in claimed:
            high += 1
            number = high
        hop["number"] = number
        claimed.add(number)
    data["next_hop_number"] = high + 1
    return data


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


def remove_hop(data: dict, selector: str) -> tuple[dict, int]:
    """Remove hops matching `selector` — a HOP NUMBER (the `#` column of `/state`) OR a label/effect/technique.
    Returns (data, removed_count). Single-selector convenience over remove_hops.
    """
    return remove_hops(data, [selector])


def remove_hops(data: dict, selectors) -> tuple[dict, int]:
    """Remove every hop matching ANY selector in `selectors` (each a HOP NUMBER OR a label/effect/technique).
    Hop numbers are permanent, so a CSV like `9,10,11` removes those exact hops and leaves the remaining
    hops' numbers unchanged. Returns (data, removed_count).
    """
    assign_hop_numbers(data)
    hops = list(data.get("hops") or [])
    targets: set[int] = set()
    for sel in selectors:
        idx = _resolve_index(sel, hops)
        if idx is not None:
            targets.add(idx)
            continue
        sel_cf = (sel or "").strip().casefold()
        if not sel_cf:
            continue
        for i, hop in enumerate(hops):
            if _hop_matches(hop, sel_cf):
                targets.add(i)
    kept = [hop for i, hop in enumerate(hops) if i not in targets]
    data["hops"] = kept
    return data, len(hops) - len(kept)


def set_hop_status(data: dict, selector: str, status: str) -> tuple[dict, int]:
    """Set the `status` of hops matching `selector` — a HOP NUMBER OR a label/effect/technique.
    Returns (data, changed_count).
    """
    assign_hop_numbers(data)
    hops = data.get("hops") or []
    new_status = (status or "").strip()
    if new_status.casefold() == "achieved":
        return data, 0
    idx = _resolve_index(selector, hops)
    if idx is not None:
        hops[idx]["status"] = new_status
        return data, 1
    sel = (selector or "").strip().casefold()
    changed = 0
    for hop in hops:
        if _hop_matches(hop, sel):
            hop["status"] = new_status
            changed += 1
    return data, changed


def _quarantine_unproven_achievements(data: dict, engagement_id: str) -> None:
    """Keep legacy ledger rows visible without letting them represent runtime proof.

    Old ledgers predate proof envelopes. They remain useful operator context, but an
    unbound ``achieved`` row must not survive a load as runtime achievement.
    """
    try:
        try:
            from . import proof_boundary
        except ImportError:
            import proof_boundary
    except Exception:
        proof_boundary = None
    for hop in data.get("hops") or []:
        if not isinstance(hop, dict) or str(hop.get("status") or "").casefold() != "achieved":
            continue
        evidence = hop.get("evidence") if isinstance(hop.get("evidence"), dict) else {}
        proof = hop.get("proof_envelope") if isinstance(hop.get("proof_envelope"), dict) else evidence.get("proof_envelope")
        admitted = False
        reason = "missing proof envelope"
        if proof_boundary is not None:
            try:
                admission = proof_boundary.admit_runtime_envelope(
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
                admitted = admission.admitted
                reason = admission.reason
            except Exception:
                admitted = False
                reason = "proof admission failed closed"
        if admitted:
            continue
        hop["status"] = "legacy_unverified"
        evidence = dict(evidence)
        evidence["proof_persistence_state"] = "legacy_unverified"
        evidence["proof_admission_reason"] = reason
        hop["evidence"] = evidence


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


def render_hop_table(hops, *, include_evidence: bool = True, limit: int = 0) -> list[str]:
    """Render hops as markdown table lines keyed by permanent hop number, ascending.

    Every surface that shows hops (`/state`, the turn-end summary, and the model's copy of either) uses this,
    so "hop N" names the same row everywhere. `limit` keeps the N highest-numbered (newest) hops. The model's
    copy omits Evidence: those previews are target-influenced task output, which the model reads through its
    tools rather than as operator-role text.
    """
    rows = sorted((h for h in (hops or []) if isinstance(h, dict)), key=hop_number)
    if limit and len(rows) > limit:
        rows = rows[-limit:]
    header = "| # | Hop | Effect | Status | Task | CB |" + (" Evidence |" if include_evidence else "")
    divider = "|---|---|---|---|---|---|" + ("---|" if include_evidence else "")
    lines = [header, divider]
    for hop in rows:
        ev = hop.get("evidence") if isinstance(hop.get("evidence"), dict) else {}
        task_id = ev.get("mythic_task_id")
        cb_id = ev.get("callback_id")
        number = hop_number(hop)
        line = (
            f"| {number or '-'} | `{md_cell(hop_label(hop), 48)}` | {md_cell(hop.get('effect'))} | "
            f"`{md_cell(hop.get('status') or '-', 16)}` | {task_id if task_id is not None else '-'} | "
            f"{cb_id if cb_id is not None else '-'} |"
        )
        if include_evidence:
            line += f" {md_cell(ev.get('result_preview') or ev.get('source') or '', 60)} |"
        lines.append(line)
    return lines


# --- operator-shown views handed to the model ----------------------------------------------------------
# Sage shows the operator the ledger in `/state` and at the end of a turn, but neither reaches the model's
# conversation, so "why is hop 1 pending?" had nothing to resolve against. The latest view shown on a channel
# waits here, process-local, until that channel's next model request records it in the conversation.
# Read-only: this hands the model a description of the ledger, never a way to change it.

_OPERATOR_VIEWS: dict[str, str] = {}


def operator_view_text(data: dict, shown_via: str, *, limit: int = 0) -> str:
    """Model-facing copy of a ledger view the operator was just shown (no evidence previews)."""
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
        lines.extend(render_hop_table(hops, include_evidence=False, limit=limit))
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
