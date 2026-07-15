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


# The engagement key the live Sage process resolved/froze for THIS run (operation-resolved or explicit
# override). `SAGE_ENGAGEMENT_ID` lives on the harness, NOT inside the persistent Sage process, so diagnostics
# that need to attribute records to a specific seed must read this published value rather than the env. Set by
# `mythic_tools._ensure_engagement_key` the first time the key is frozen; read by `_trace_rights_decision`.
_ACTIVE_ENGAGEMENT_ID: str = ""


def set_active_engagement_id(engagement_id: str | None) -> None:
    """Publish the live process's frozen engagement key for diagnostics. Best-effort; never raises. An empty
    or 'default' key is ignored so a real key, once set, is not clobbered by a later 'default' resolution."""
    global _ACTIVE_ENGAGEMENT_ID
    key = (engagement_id or "").strip()
    if key and key != "default":
        _ACTIVE_ENGAGEMENT_ID = key


def active_engagement_id() -> str:
    """The frozen engagement key published by the live Sage process (empty string if never resolved)."""
    return _ACTIVE_ENGAGEMENT_ID


def state_dir() -> str:
    """Directory holding the per-engagement ledgers. `SAGE_ENGAGEMENT_STATE_DIR` overrides; default is
    `.sage_engagement` next to the Sage process cwd (same persistence guarantee as the operational db)."""
    override = os.environ.get("SAGE_ENGAGEMENT_STATE_DIR", "").strip()
    if override:
        return override
    return os.path.join(os.getcwd(), ".sage_engagement")


# Ledger filename prefix. Named `state_` (the operator-facing `state` command) and keyed per Mythic
# OPERATION — e.g. `state_Operation_Chimera_1.json`. (Was `engagement_<SAGE_ENGAGEMENT_ID>` pre-2026-06-08;
# clean break, old files orphaned/untouched.)
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
    return data


def save_runtime(data: dict, engagement_id: str | None = None) -> str:
    """Persist a live runtime ledger after quarantining unbound historical achievements."""
    _quarantine_unproven_achievements(data, engagement_id or data.get("engagement_id") or default_engagement_id())
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


def _resolve_index(selector: str, hops: list) -> int | None:
    """If `selector` is a 1-based row number (as shown by `state show`), return its 0-based index, else None.
    Hop labels are never bare integers, so a numeric selector unambiguously means a row."""
    s = (selector or "").strip()
    if s.isdigit():
        i = int(s) - 1
        if 0 <= i < len(hops):
            return i
    return None


def remove_hop(data: dict, selector: str) -> tuple[dict, int]:
    """Remove hops matching `selector` — a 1-based ROW NUMBER (from `state show`) OR a label/effect/technique.
    Returns (data, removed_count). Single-selector convenience over remove_hops."""
    return remove_hops(data, [selector])


def remove_hops(data: dict, selectors) -> tuple[dict, int]:
    """Remove every hop matching ANY selector in `selectors` (each a 1-based ROW NUMBER OR a
    label/effect/technique). Row numbers are resolved against the ORIGINAL indexing, so a CSV like
    `9,10,11` removes those exact rows with no index-shift surprises. Returns (data, removed_count)."""
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
    """Set the `status` of hops matching `selector` — a 1-based ROW NUMBER OR a label/effect/technique.
    Returns (data, changed_count)."""
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
