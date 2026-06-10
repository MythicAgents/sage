import os
ENGAGEMENT_GATE_ENABLED = os.environ.get("SAGE_ENGAGEMENT_GATE", "").lower() in ("1", "true", "yes")
# Durable cross-run engagement ledger config. The achieved-hops ledger is maintained incrementally in
# code (zero LLM inference); these knobs let it survive across runs/restarts as a per-engagement JSON.
# Key per-ENGAGEMENT (broader than the per-solve ParentTaskID/agent_task_id) so separate solves resume.
SAGE_ENGAGEMENT_ID = os.environ.get("SAGE_ENGAGEMENT_ID", "").strip() or "default"
# Durable-hop TTL (hours). A loaded "achieved" hop older than this is dropped at load so a stale belief
# (e.g. after a GOAD redeploy) cannot suppress a real hop. Default 0 = disabled (no expiry). The gate
# also refuses to SILENTLY hard-SKIP a durable hop unless live footholds corroborate it — TTL is the
# cheap first line; corroboration is the second.
def _engagement_hop_ttl_hours() -> float:
    try:
        return float(os.environ.get("SAGE_ENGAGEMENT_HOP_TTL_HOURS", "0") or 0)
    except (ValueError, TypeError):
        return 0.0
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


def _engagement_state_dir() -> str:
    """Directory for the durable per-engagement ledger (delegated). SAGE_ENGAGEMENT_STATE_DIR overrides."""
    return _engagement_ledger_mod().state_dir()


def _engagement_ledger_file(engagement_id: str | None = None) -> str:
    """Absolute path to the JSON ledger for an engagement key (delegated to the shared module)."""
    return _engagement_ledger_mod().ledger_path(engagement_id or SAGE_ENGAGEMENT_ID)
import asyncio
import base64
from datetime import datetime, timezone
import re
from typing import Annotated, List, Dict, TypedDict
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


_CALLBACK_LIVENESS_QUERY = """
    query cbinfo($ids:[Int!]) {
      callback(where: {display_id: {_in: $ids}}) {
        display_id active last_checkin
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
    now: datetime | None = None,
) -> dict:
    """Compute callback liveness from check-in time, effective sleep, jitter, and recent tasks."""

    now_utc = now or datetime.now(timezone.utc)
    if now_utc.tzinfo is None:
        now_utc = now_utc.replace(tzinfo=timezone.utc)
    else:
        now_utc = now_utc.astimezone(timezone.utc)

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
    "create_payload",
    "delete_payload",
    "download_tool",
    "ingest_collection",
    "sandbox_exec",
    "file_upload",
    "add_credential",
}


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

    def __init__(self, agent_task_id: str):
        """Initialize the MythicTools with the Mythic taskData.Task.AgentTaskID. Call create() to establish connection."""
        logger.debug(f"Initializing MythicAPIClient with task ID: {agent_task_id}")
        self.agent_task_id = agent_task_id
        self.client = None
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
        # Mythic display_id + callback display_id of the most recently issued task — attached to an engagement
        # hop's evidence so the operator (and `state show`) can trace each achieved effect back to the exact
        # task AND the callback that proved it.
        self._last_issued_task_display_id = None
        self._last_issued_callback_id = None
        self._engagement_hops: list = []
        self._pending_engagement_hop = None
        # Forward-planner graph facts: BloodHound ACL edges (GenericWrite on GPOs, WriteDacl on domains,
        # etc.) projected into engagement predicates, cached here and refreshed after each verified ingest
        # so the per-turn injection can DIRECT the operator to the next available hop instead of letting it
        # re-collect (the 2026-06-09 loop). SUGGESTION-ONLY: these feed available_hops / the injected
        # render, NOT the gate's enforcement state — so a reconciliation miss can never newly DEFER a real
        # hop (suggest-on-known; blocking stays conservative / unknown != false).
        self._engagement_graph_facts: list = []
        self._engagement_graph_facts_ts: str | None = None
        # Empirical pre-DCSync rights precheck: per-(technique,domain) count of times we've blocked a DCSync
        # for missing replication rights. CAPPED (see _DCSYNC_PRECHECK_MAX_BLOCKS) so the precheck can redirect
        # the agent to obtain rights first WITHOUT ever becoming a permanent deadlock (the failure mode that
        # got the static gate demoted to advisory).
        self._dcsync_precheck_blocks: dict = {}
        # Access-context keys with a collection currently in-flight (issued, not yet ingested). Gates the
        # parallel-spawn case (agent fires N SharpHounds before the first ingests) until graph-built lands.
        self._collection_in_flight: set = set()
        # Deliberation-drain guards. Command schemas are STATIC per payloadtype -> cache (a 2026-06-09 solve
        # re-fetched + re-dumped them 27×, bloating context). Gate-block counts cap a hop the gate keeps
        # DEFERring (issue_task fired 168× -> 1 task that run): a firm STOP after repeated blocks with no
        # intervening progress; reset whenever a task actually passes the gate.
        self._command_schema_cache: dict = {}
        self._gate_block_counts: dict = {}
        # Credential-store cache: the store is read both by read_credentials (which the agent hit 24× in one
        # solve) and by the gate's durable-hop corroboration probe. Cache the raw rows for a short TTL so
        # neither path re-queries Mythic repeatedly.
        self._cred_cache: list | None = None
        self._cred_cache_ts: str | None = None
        # The durable-ledger key. Defaults to the explicit SAGE_ENGAGEMENT_ID (env/test override); when
        # that is unset ("default") it is resolved lazily from the current Mythic OPERATION the first time
        # the gate fires (client exists by then) -> `state_<OperationName>_<OperationId>.json`. The lock
        # serializes that one-time resolve+reload so two concurrent gate calls can't both reload and stomp
        # an appended hop.
        self._engagement_key: str | None = None
        self._engagement_key_lock = asyncio.Lock()
        # Live footholds cache (populated by the gate after reconcile) so the per-turn state render in
        # model.py can show footholds without an extra network round-trip on every model call.
        self._engagement_footholds: list = []
        # Durable cross-run resume: load the per-engagement hop ledger from disk so a fresh MythicTools
        # (rebuilt per solve) inherits already-achieved hops across runs/restarts. Gate-on only; never
        # raises (fail-open to an empty ledger). With the gate off we never touch disk.
        if ENGAGEMENT_GATE_ENABLED:
            try:
                self._load_engagement_ledger()
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

    async def login(self):
        """Create the Mythic API client connection asynchronously."""
        logger.info(f"Calling MythicRPCAPITokenCreateMessage with: {self.agent_task_id}")

        resp = await SendMythicRPCAPITokenCreate(MythicRPCAPITokenCreateMessage(AgentTaskID=self.agent_task_id))
        if resp.Success:
            api_key = resp.APIToken
        else:
            raise Exception(f"Failed to get API token for AgentTaskID {self.agent_task_id}: {resp.Error}")

        ip = os.environ.get("NGINX_HOST", "127.0.0.1")
        port = int(os.environ.get("NGINX_PORT", 7443))
        ssl = True if os.environ.get("NGINX_SSL", "true").lower() in ['true', '1', 'yes'] else False
        self.client = await mythic.login(apitoken=api_key, server_ip=ip, server_port=port, ssl=ssl)
    
    def get_tools(self, method_names: list[str]) -> list[StructuredTool]:
        """Get Mythic tools by method names and return them as LangChain StructuredTool instances.

        Do not use the LangChain @tool decorator because it will cause a conflict with 'self' argument in class methods
        """

        if "get_task_history_for_callback" in method_names and "list_open_artifacts" not in method_names:
            method_names = [*method_names, "list_open_artifacts"]

        tools = []
        for method_name in method_names:
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
            live = _compute_liveness(
                display_id=cb.get("display_id"),
                last_checkin=cb.get("last_checkin"),
                callback_interval=profile.get("callback_interval"),
                callback_jitter=profile.get("callback_jitter"),
                tasks=[],
            )
            agent = ((cb.get("payload") or {}).get("payloadtype") or {}).get("name")
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
                payloadtype(where: { name: { _neq: "sage" } }) {
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
                payloadtype(where: { name: { _neq: "sage" } }) {
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

    async def issue_task_and_waitfor_task_output(self, command: str, parameters: str|dict, callback_display_id: int, token_id: int | None = None, timeout: int | None = None) -> str:
        """
        Issue a task to execute 'command' on the specified agent and wait for the agent to checkin, execute the task, and return the results.
        **IMPORTANT**: When a command has a parameter type of "File" (e.g., "type": "File"), you must pass in the Mythic file UUID (not the filename).

        Args:
            command: The command name to execute from the "cmd" field from the get_all_commands_for_payloadtype tool. Validate the agent's operating system and the supported_os match.
            parameters: The command's parameters or arguments. Prefer a JSON string that leverages the commandparameters "name" value (e.g. {"arguments": "value"}). Alternatively, use a non-JSON string that has dash with the "cli_name" field (e.g. -path /etc/issue).
            callback_display_id: The callback_display_id of the target agent to run the command on.
            token_id: Optional Mythic identifier for tracked Windows user access tokens to use for impersonation.
            timeout: Optional timeout in seconds for the task to complete.
        Returns:
            str: Command output (binary output coerced to string).
        """
        if ENGAGEMENT_GATE_ENABLED:
            try:
                _gate_result = await self._engagement_gate(command, parameters, callback_display_id)
            except Exception:
                _gate_result = None  # fail-open: any gate error => proceed normally
            if _gate_result is not None:
                # Post-RCA (2026-06-09): missing-precondition DEFER no longer blocks (see _engagement_gate),
                # so the ONLY blocking result left is an already-achieved/collect-once SKIP (genuine dedup).
                # This breaker now caps the narrow case of an agent re-proposing an ALREADY-WON hop: a firm
                # STOP after 3 tries tells it to advance instead of re-issuing a hop the ledger already holds.
                # Counts reset when a task actually passes the gate.
                try:
                    _sig = (
                        command, callback_display_id,
                        json.dumps(parameters, sort_keys=True) if isinstance(parameters, (dict, list)) else str(parameters),
                    )
                    self._gate_block_counts[_sig] = self._gate_block_counts.get(_sig, 0) + 1
                    if self._gate_block_counts[_sig] >= 3:
                        return (
                            f"STOP — the engagement gate has blocked this exact action "
                            f"{self._gate_block_counts[_sig]}× with NO intervening progress.\n{_gate_result}\n"
                            "Re-proposing a blocked hop burns the step budget. Execute one of the NEXT GROUNDED "
                            "ACTIONS shown in the engagement state, or handback_to_supervisor with this blocker. "
                            "Do NOT re-issue this command."
                        )
                except Exception:
                    pass
                return _gate_result

        # A command is about to be issued → new state will exist → start a fresh recon epoch so a single
        # legitimate post-action re-read of history/callbacks is allowed again (the guard only fires on
        # REPEATED reads within one epoch). A task passing the gate is progress, so stale gate-block counts
        # reset here too.
        self._recon_epoch += 1
        self._gate_block_counts = {}

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

        # Circuit breaker: refuse to re-issue a command that has already failed repeatedly
        # with the same (normalized) parameters on the same callback. Without this, a
        # transient "Failed to create task" or a parse error sends the model into an
        # unbounded retry loop (cosmetic param permutations) that explodes context and
        # exhausts the recursion limit.
        fail_key = (
            command,
            callback_display_id,
            json.dumps(parameters, sort_keys=True) if isinstance(parameters, (dict, list)) else str(parameters),
        )
        if self._task_failure_counts.get(fail_key, 0) >= 2:
            return (
                f"STOP — command '{command}' on callback {callback_display_id} with these parameters has already "
                f"failed {self._task_failure_counts[fail_key]} times. Do NOT re-issue it with cosmetically different "
                f"empty parameters ({{}}, '', '\"\"' are all equivalent to 'no arguments'). The parameter format is "
                f"likely wrong or the failure is environmental. Report this to the operator, consult "
                f"get_all_commands_for_payloadtype for the correct parameter schema, or choose a different approach."
            )

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
                        if parameters != original_parameters:
                            logger.debug(
                                f"🛡️ ARGRES command={command} group={resolved.group} "
                                f"params={sorted(parameters.keys())} notes={resolved.notes}"
                            )
                            fail_key = (
                                command,
                                callback_display_id,
                                json.dumps(parameters, sort_keys=True),
                            )
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

                async def _issue_and_wait():
                    task = await mythic.issue_task(
                        mythic=self.client, command_name=command, parameters=parameters,
                        callback_display_id=callback_display_id, wait_for_complete=True, timeout=timeout,
                    )  # token_id=token_id
                    tdid = task.get("display_id") if isinstance(task, dict) else None
                    if tdid is None:
                        raise Exception("Failed to create task")
                    self._last_issued_task_display_id = tdid
                    return await mythic.waitfor_for_task_output(
                        mythic=self.client, task_display_id=tdid, timeout=timeout,
                    )

                results = await asyncio.wait_for(_issue_and_wait(), timeout=timeout + 20)
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
            results_str = str(results)
            # Agent-side execution errors come back in the OUTPUT (not as exceptions); count
            # them toward the circuit breaker so blind retries are still capped.
            result_class = command_builder.classify_result(command, results_str)
            decision = self._apply_task_result_class(fail_key, result_class)
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
            if ENGAGEMENT_GATE_ENABLED:
                try:
                    self._record_engagement_success(results_str)
                except Exception:
                    pass  # fail-open: recording must never break the issue path
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

    async def _engagement_gate(self, command, parameters, callback_display_id) -> str | None:
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
        if classified is None:
            return None

        # Resolve the durable-ledger key from the current Mythic operation (and reload the ledger under
        # it) before the first gated decision — __init__ loaded under 'default' with no client yet.
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

        # collect-graph: deterministic collect-once-per-privilege. Rebind the empty target to the issuing
        # callback's access-context key; SKIP if a collection is in-flight or the graph is already built at
        # this access level. graph-built is recorded by ingest_collection on graph_verified (not on
        # SharpHound success), so we do NOT set a pending hop here.
        if technique == "collect-graph":
            cg_state = engagement_state.EngagementState(
                objective=self._engagement_objective(), footholds=footholds, hops=list(self._engagement_hops),
            )
            fh = next(
                (f for f in footholds if str(getattr(f, "callback_id", "")) == str(callback_display_id)), None
            )
            access_key = engagement_state.access_context_key(cg_state, fh) if fh is not None else ""
            if not access_key:
                return None  # can't key it — fail-open, allow the collection
            if access_key in self._collection_in_flight:
                return ("[engagement-gate] skipped: a collection is already in-flight at this access level "
                        f"({access_key}) — wait for ingest, then analyze; do NOT launch another SharpHound.")
            decision, _reason = engagement_state.gate_decision("collect-graph", access_key, cg_state)
            if decision == engagement_state.GateDecision.SKIP:
                return (f"[engagement-gate] skipped: graph already built at this access level ({access_key}) "
                        "— analyze the existing graph or escalate; do NOT re-collect.")
            self._collection_in_flight.add(access_key)
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
        )
        decision, reason = engagement_state.gate_decision(technique, target_key, state)
        if decision == engagement_state.GateDecision.SKIP:
            self._pending_engagement_hop = None
            return f"[engagement-gate] skipped: {reason}"
        if decision == engagement_state.GateDecision.DEFER:
            # ADVISOR, NOT ENFORCER (2026-06-09 RCA): a missing-precondition DEFER is NO LONGER a veto.
            # The hand-built STRIPS precondition model produced FATAL false-negatives — it refused 167/168
            # correct hops in solve #37 and deadlocked every run after hop 1: `dcsync` DEFERd for
            # `ds-replication-rights:{domain}`, and the StandIn grant that PRODUCES those rights itself
            # DEFERd for `write-dacl:domain:{domain}` — a precondition nothing in the model ever emits from
            # the agent's GPO/SYSTEM control. The agent cannot route around Sage's OWN refusal, so every
            # modeling gap is a fatal deadlock. The live target is a real oracle we already possess: a wrong
            # attack fails with real, reasoned-from feedback; a right attack advances the ledger. Cost
            # asymmetry: a gate false-block = infinite deadlock; a false-allow = one cheap, reset-recoverable
            # task + real signal. So we PROCEED past a would-DEFER and let execution be the judge. The
            # advisory is LOGGED for observability but NOT injected into the agent's context — injecting it
            # is what made the agent mistake Sage's veto for a malformed command and permute --object syntax
            # ~50×. Already-achieved SKIP (dedup, above) is preserved — that is what prevents re-doing won
            # work (the original 156× re-propose thrash). verify-on-record still gates the ledger, so a hop
            # that proceeds-but-fails records `failed`, never a phantom effect.
            # EMPIRICAL pre-DCSync rights precheck (2026-06-10): the demoted gate no longer warns, so the
            # agent fires DCSync without replication rights and burns a step on 8453. Re-block — but ONLY
            # for DCSync, ONLY on EMPIRICAL evidence (the graph is POPULATED and the missing precondition is
            # replication rights), and ONLY up to a per-domain CAP so this can never re-create the static
            # deadlock that demoted the gate. Fail-open on ignorance (no graph facts) and after the cap, where
            # the agent's own 8453 + verify-on-record become the judge.
            dom = self._dcsync_target_domain(technique, target_key)
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
            logger.info("🧭 [engagement-advisor] would-defer (proceeding, advisory only): %s", reason)
            self._pending_engagement_hop = (technique, target_key, now)
            return None

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
        return f"sage-engagement:{self.agent_task_id}" if self.agent_task_id else "sage-engagement"

    _CRED_CACHE_TTL_SECONDS = 60

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
                         "{ id account realm type credential_text comment timestamp } }")
                variables = {"op": op_id}
            else:
                query = ("query SageReadCredentials { credential(where: {deleted: {_eq: false}}, "
                         "order_by: {id: desc}, limit: 200) { id account realm type credential_text comment timestamp } }")
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
        facts: list = []
        seen: set = set()

        def add(pred: str) -> None:
            p = engagement_state._normalize_predicate(pred)
            if p and p not in seen:
                seen.add(p)
                facts.append(engagement_state.GraphFact(
                    predicate=p, source="live-probe", timestamp=now, ttl_seconds=self._GRAPH_FACTS_TTL_SECONDS))

        # 1. Credential store -> creds:{account@realm}, krbtgt-hash:{realm} (the dcsync/lsass artifacts).
        try:
            for c in await self._fetch_credentials_cached(now):
                acct = str(c.get("account") or "").strip().casefold()
                realm = str(c.get("realm") or "").strip().casefold()
                if not str(c.get("credential_text") or "").strip():
                    continue
                if acct and realm:
                    add(f"creds:{acct}@{realm}")
                if acct == "krbtgt" and realm:
                    add(f"krbtgt-hash:{realm}")
        except Exception:
            pass
        # 2. Cached graph ACLs -> gpo-abuse corroboration (you still CONTROL the GPO) + replication passthrough.
        try:
            for gf in (getattr(self, "_engagement_graph_facts", []) or []):
                p = engagement_state._normalize_predicate(getattr(gf, "predicate", ""))
                if p.startswith("generic-write:gpo:"):
                    add("system:" + p[len("generic-write:gpo:"):])
                elif p.startswith("ds-replication-rights:"):
                    add(p)
                elif p.startswith("gpo-domain:"):
                    # Pass the GPO->domain link through so the gate's _expand_implications can chain
                    # gpo-abuse (system:{gpo}) -> ds-replication-rights:{domain} -> dcsync. Without it the
                    # planner names dcsync but the gate DEFERs it (missing ds-replication-rights) -> churn.
                    add(p)
        except Exception:
            pass
        return facts

    _GRAPH_FACTS_TTL_SECONDS = 600                 # facts stay live in the engagement state for 10 min
    _GRAPH_FACTS_REFRESH_INTERVAL_SECONDS = 120    # don't re-run the read-only cypher more than ~every 2 min

    async def _refresh_graph_facts_if_stale(self, now: str, force: bool = False) -> None:
        """Refresh the cached BloodHound graph facts (ACL edges → engagement predicates) that feed the
        forward planner / per-turn injection. TTL-bounded (the read-only cypher runs at most ~every 2 min
        unless forced — e.g. right after a verified ingest). SUGGESTION-ONLY: these are NOT fed into the
        gate's enforcement state, so this can never newly DEFER a real hop. Best-effort, fail-open."""
        try:
            try:
                from . import access_reconciler, engagement_state, graph_reconciler
            except ImportError:
                import access_reconciler
                import engagement_state
                import graph_reconciler

            # Only TTL-skip when we ALREADY HAVE facts. An empty cache means the graph wasn't populated yet
            # (ingest is async ~40s) — keep retrying on each gate call until it lands, else the chain never
            # sees the graph (the 2026-06-09 false-negative: ingest completed late, cache stayed empty).
            if not force and self._engagement_graph_facts and self._engagement_graph_facts_ts:
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
                    if "bloodhound" not in server.lower():
                        continue
                    for tool in MCPManager.get_tools_by_server(server):
                        if getattr(tool, "name", "") == "cypher_query":
                            cypher_tool = tool
            except Exception:
                cypher_tool = None
            if cypher_tool is None:
                logger.info("🧭 [graph-facts] refresh skipped: BloodHound cypher_query tool not resolvable")
                return

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
            )
            principals = graph_reconciler.controlled_principals_from_state(state)
            if not principals:
                logger.info(
                    f"🧭 [graph-facts] refresh skipped: 0 controlled principals "
                    f"(footholds={len(footholds)}, alive={sum(1 for f in footholds if getattr(f, 'alive', False))})"
                )
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

            facts = list(await graph_reconciler.reconcile_graph_position(
                _SingleToolMCP(cypher_tool), principals, state.objective, now,
                self._GRAPH_FACTS_TTL_SECONDS,
            ) or [])
            # Non-clobbering: a pending/empty reconcile (graph not ingested yet) must NOT wipe good facts
            # from a prior refresh. Only overwrite when this reconcile actually returned edges.
            if facts:
                self._engagement_graph_facts = facts
                self._engagement_graph_facts_ts = now
            logger.info(
                f"🧭 [graph-facts] refresh: principals={len(principals)} new_facts={len(facts)} "
                f"cached={len(self._engagement_graph_facts)} "
                f"preds={[getattr(f, 'predicate', '') for f in self._engagement_graph_facts][:6]}"
            )
        except Exception as exc:
            logger.info(f"🧭 [graph-facts] refresh error (fail-open): {exc}")
            # fail-open: planner suggestions are best-effort, never break the issue path

    async def _record_graph_built(self, callback_display_id, verified: bool) -> None:
        """Record the collect-graph effect (graph-built at the resolving callback's access level) on a
        verified ingest, and clear that access key's in-flight marker either way. This is what makes the
        gate SKIP a re-collection at the same privilege. Best-effort, fail-open."""
        try:
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
            )
            access_key = engagement_state.access_context_key(state, fh)
            if not access_key:
                return
            self._collection_in_flight.discard(access_key)
            if not verified:
                return
            updated = engagement_state.record_hop_result(
                state, "collect-graph", access_key, "achieved",
                {"source": "ingest_collection", "provenance": "run", "graph_verified": True}, now,
            )
            self._engagement_hops = updated.hops
            try:
                self._persist_engagement_ledger()
            except Exception:
                pass
        except Exception:
            pass

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

            # Verify-on-record: credential-dump techniques only record `achieved` when the output
            # actually contains a usable secret (a real NTLM/AES/RC4 key) — not merely because the
            # task lacked a known failure signature. An 8439 DS_DRA_BAD_DN DCSync "succeeds" at the
            # Mythic layer but returns no key; recording it `achieved` was the false-achieved ledger
            # bug that made the agent forge with a placeholder key. Non-credential techniques keep the
            # legacy behavior (record `achieved` unless the output is a known failure signature).
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
            elif _is_task_failure_output(results_str):
                # Legacy non-credential path: a known failure signature means no hop is recorded.
                return

            state = engagement_state.EngagementState(
                objective=self._engagement_objective(),
                footholds=[],
                hops=list(self._engagement_hops),
            )
            updated = engagement_state.record_hop_result(
                state,
                technique,
                target_key,
                status,
                {"source": "issue_task", "provenance": "run", "result_preview": str(results_str)[:200],
                 "mythic_task_id": getattr(self, "_last_issued_task_display_id", None),
                 "callback_id": getattr(self, "_last_issued_callback_id", None),
                 **extra},
                now,
            )
            self._engagement_hops = updated.hops
            # Write-through to the durable per-engagement ledger so the hop survives runs/restarts.
            try:
                self._persist_engagement_ledger()
            except Exception:
                pass  # fail-open: persistence must never break the issue path
        finally:
            self._pending_engagement_hop = None

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
                # __init__ loaded under the 'default' key (no client yet); reload under the operation key.
                try:
                    self._load_engagement_ledger()
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

    def _load_engagement_ledger(self) -> None:
        """Load the durable hop ledger from disk into self._engagement_hops. Fail-open: any error
        (missing file, bad JSON, unreadable) leaves the in-memory ledger untouched. NO LLM inference."""
        path = self._engagement_ledger_path()
        if not os.path.exists(path):
            return
        try:
            with open(path, "r", encoding="utf-8") as fh:
                payload = json.load(fh)
        except Exception:
            return
        items = payload.get("hops") if isinstance(payload, dict) else payload
        try:
            from . import engagement_state
        except ImportError:
            import engagement_state
        loaded = engagement_state.hops_from_dicts(items)
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
        if loaded:
            self._engagement_hops = loaded
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
            "updated": datetime.now(timezone.utc).isoformat(),
            "hops": engagement_state.hops_to_dicts(self._engagement_hops),
        }
        tmp = f"{path}.{os.getpid()}.tmp"
        with open(tmp, "w", encoding="utf-8") as fh:
            json.dump(payload, fh, default=str)
        os.replace(tmp, path)  # atomic on POSIX — never leaves a half-written ledger

    async def list_open_artifacts(self) -> str:
        """List artifacts this run has created (files dropped, beacons planted) that have NOT been cleaned up, so you can remove them at sub-goal completion for OPSEC. Returns a JSON list."""
        # HITL: free
        return json.dumps([e for e in self._artifact_ledger if not e.get("cleaned")], default=str)

    async def get_task_history_for_callback(self, callback_display_id: Annotated[int, "The callback_display_id of the target agent to retrieve task history for"]) -> str:
        """Get the task history of commands issued for a specific agent (callback).

        This tool retrieves detailed information about all tasks issued to a specific Mythic agent, including the following fields:

        - **id**: The ID associated with the task.
        - **operator**: The Mythic operator who issued the command.
        - **status**: The status of the task (e.g., success, completed, agent_processing, error).
        - **completed**: Whether the task is completed (True/False).
        - **original_params**: The original parameters or arguments issued with the command.
        - **timestamp**: The timestamp when the command was issued.
        - **command_name**: The name of the command issued to the Mythic agent.

        Args:
            callback_display_id: The callback_display_id of the target agent to retrieve task history for.
        Returns:
            str: JSON string containing the task history for the specified agent.
        """
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
        """Upload a file stored in Mythic to a specific Mythic agent by the Mythic file UUID.

        This tool uploads a file, identified by its Mythic file UUID, to a specified Mythic agent. The file will be transferred to the agent associated with the provided callback_display_id.

        **IMPORTANT**: The command's parameter must be of type "File" (e.g., "type": "File"). DO NOT USE PARAMETER TYPE "STRING" TO UPLOAD FILES.
        For the Merlin agent, use the "upload" command with the "file" parameter set to the Mythic file UUID and the "path" parameter set to the destination path and file name on the target system.

        **IMPORTANT — which UUID:** `file_uuid` must be the Mythic *file* UUID, i.e. the `agent_file_id`
        returned by create_payload (surfaced as the top-level `agent_file_id` field), NOT the payload's
        `uuid` (the Payload UUID). They are different values; passing the Payload UUID will fail because no
        downloadable file has that UUID. For a freshly built payload, also confirm its build_phase is
        "success" before uploading — a payload that errored during build has no file bytes to send.

        Args:
            command: The name of the command for a Mythic agent that has upload functionality, typically the "upload" command.
            parameters: Parameters a Mythic agent's upload command.
            file_uuid: The Mythic file UUID (agent_file_id) to upload to the target agent.
            callback_display_id: The callback_display_id of the target agent to upload the file to.
            token_id: Optional token ID for authentication.
            timeout: Optional timeout for the upload operation.
        Returns:
            str: Command output (binary output coerced to string) after the upload operation."""
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

    async def ingest_collection(
        self,
        file_uuid: Annotated[str, "The Mythic file UUID of the downloaded SharpHound/AzureHound collection to ingest. PREFERRED. Optional if callback_display_id is given."] = "",
        callback_display_id: Annotated[int | None, "If set (and file_uuid empty), resolve the MOST RECENT completed collection download from this callback and ingest that."] = None,
        file_name: Annotated[str, "Optional basename for the collection (e.g. 'collection.zip'). Defaults to the source filename or <uuid>.zip."] = "",
        name_contains: Annotated[str, "When resolving by callback, only match files whose name contains this substring (default 'zip')."] = "zip",
    ) -> str:
        """Ingest a downloaded collection into BloodHound IN-MEMORY (no disk staging, no LLM round-trip).

        This is the programmatic bridge between Mythic and BloodHound: it resolves the Mythic file
        (by UUID, or the latest completed download on a callback), fetches its bytes from Mythic, and
        uploads them DIRECTLY to BloodHound via the BloodHound MCP's `file_upload(info_type=
        'upload_bytes', ...)` — invoked HERE in code so the (potentially large) collection bytes NEVER
        pass through the LLM context and are never written to the Sage host disk.

        This is the in-memory ingest path (no disk). After it returns, VERIFY with
        `domain_info(info_type='list')` that the expected domain(s) appear (BloodHound ingest is
        asynchronous — the count may lag a few seconds).

        Args:
            file_uuid: The Mythic file UUID. Optional if callback_display_id is given.
            callback_display_id: Resolve the most recent completed agent-download from this callback.
            file_name: Optional basename for the collection.
            name_contains: Optional case-insensitive source filename substring for callback resolution.
        Returns:
            str: JSON with status, file_uuid, filename, byte count, and BloodHound's upload response.
        """
        # HITL: guarded
        if self.client is None:
            raise Exception("MythicAPIClient not initialized. Call login() first.")
        logger.debug(f"🛠️ Calling ingest_collection (file_uuid={file_uuid!r}, callback={callback_display_id})")
        source_filename = ""
        # 1. Resolve the Mythic file UUID (by UUID, or the latest completed download on a callback).
        if file_uuid:
            resolved_by = "uuid"
        elif callback_display_id is not None:
            row = await self._latest_download_for_callback(callback_display_id, name_contains)
            if row is None:
                for _ in range(3):
                    await asyncio.sleep(2)
                    row = await self._latest_download_for_callback(callback_display_id, name_contains)
                    if row is not None:
                        break
            if row is None:
                return json.dumps({"status": "error", "callback_display_id": callback_display_id,
                                   "error": "No completed agent-download found on this callback. Run the Mythic `download` command for the collection first."}, sort_keys=True)
            file_uuid = row["agent_file_id"]
            source_filename = row.get("filename_utf8") or ""
            if not file_name:
                file_name = os.path.basename(source_filename)
            resolved_by = "callback:" + str(callback_display_id)
        else:
            return json.dumps({"status": "error", "error": "Provide either file_uuid or callback_display_id."}, sort_keys=True)
        # 2. Fetch the collection bytes from Mythic.
        try:
            file_content = await mythic.download_file(mythic=self.client, file_uuid=file_uuid)
        except Exception as e:
            return json.dumps({"status": "error", "file_uuid": file_uuid, "error": str(e)}, sort_keys=True)
        if not file_content:
            return json.dumps({"status": "error", "file_uuid": file_uuid,
                               "error": "Mythic returned no content for this file UUID."}, sort_keys=True)
        safe_name = os.path.basename(file_name) if file_name else f"{file_uuid}.zip"
        # 3. Resolve the BloodHound MCP's file_upload + domain_info tools (generic; no Mythic knowledge).
        upload_tool = None
        info_tool = None
        try:
            from ai.mcp import MCPManager
            for server in MCPManager.get_connected_servers():
                if "bloodhound" not in server.lower():
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
        def _mcp_data(resp):
            try:
                text = resp
                if isinstance(resp, list) and resp and isinstance(resp[0], dict):
                    text = resp[0].get("text", "")
                parsed = json.loads(text) if isinstance(text, str) else (text or {})
                return parsed.get("data") if isinstance(parsed, dict) else None
            except Exception:
                return None
        up = _mcp_data(result)
        job_id_bh = up.get("job_id") if isinstance(up, dict) else None
        status_msg = None
        if job_id_bh is not None:
            for _ in range(20):  # up to ~120s of async-ingest wait
                await asyncio.sleep(6)
                try:
                    st = _mcp_data(await upload_tool.ainvoke({"info_type": "status", "job_id": job_id_bh}))
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
        # Loop-breaker: once the graph is populated the forward planner can name the next hop. Refresh the
        # cached graph facts so the per-turn injection surfaces NEXT GROUNDED ACTIONS (graph ACL edges →
        # available hops) and the operator advances instead of re-collecting. Fire on EVERY ingest (not
        # just verified): ingest is async, so a collection that reads "pending" here is often Complete
        # seconds later — the refresh is non-clobbering (keeps prior facts if this cypher returns empty).
        try:
            await self._refresh_graph_facts_if_stale(datetime.now(timezone.utc).isoformat(), force=True)
        except Exception:
            pass
        # Record the collect-graph effect (graph-built at this access level) so re-collection is gated, and
        # clear the in-flight marker. Keyed by the resolving callback's foothold. Best-effort, fail-open.
        try:
            await self._record_graph_built(callback_display_id, verified)
        except Exception:
            pass
        return json.dumps({"status": status_out, "file_uuid": file_uuid, "filename": safe_name,
                           "bytes": len(file_content), "resolved_by": resolved_by,
                           "bloodhound_job_id": job_id_bh, "job_status": status_msg,
                           "graph_verified": verified,
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
        """Get C2-agnostic tradecraft guidance for a goal, joined to how the target agent runs it.

        This is the FIRST tool to consult before reaching for a specific offensive tool. It
        matches your goal to Sage's TTP knowledge library and returns the tool's frontmatter
        (including `common_args` and `usage_examples`) plus the prose guidance UP TO the
        "## Full Reference" section, along with an `execution_on_agent` hint describing how the
        callback's Mythic agent runs that binary type.

        Progressive disclosure: rely on `common_args` and `usage_examples` first. Only call
        `get_ttp_full_reference(slug)` when you need an uncommon flag, exact output format, or
        version-specific behavior the guidance doesn't cover.

        Args:
            goal: Plain-language tradecraft goal.
            callback_display_id: Target agent's callback_display_id (tailors execution guidance).
        Returns:
            str: JSON with matched slug, frontmatter, guidance prose, execution_on_agent, and
                 whether a Full Reference is available.
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

    async def ensure_tool_uploaded(
        self,
        binary_filename: Annotated[str, "The tool binary filename, e.g. 'SharpHound.exe' (matches a TTP's binary_filename)."],
    ) -> str:
        """Ensure a tool binary is in Mythic's file store, uploading it from the tools/ drop zone if needed.

        Workflow: (1) check Mythic's file store by name; (2) if absent, look for the file in the
        operator drop zone at Payload_Type/sage/tools/<binary_filename>; (3) if found, register it
        with Mythic via register_file. Returns the Mythic file UUID to pass as the File parameter of
        a subsequent issue_task_and_waitfor_task_output call (e.g. assembly_file for inline_assembly).

        Args:
            binary_filename: The tool binary filename (matches a TTP's binary_filename).
        Returns:
            str: JSON with status and, when available, the Mythic file UUID.
        """
        # HITL: free
        if self.client is None:
            raise Exception("MythicAPIClient not initialized. Call login() first.")
        logger.debug(f"🛠️ Calling ensure_tool_uploaded tool (binary={binary_filename!r})")
        # 1. Already in Mythic's file store?
        try:
            existing = await mythic.get_latest_uploaded_file_by_name(self.client, filename=binary_filename)
            if existing:
                uuid = existing.get("agent_file_id") or existing.get("id")
                if uuid:
                    return json.dumps({"status": "already_present", "binary_filename": binary_filename, "file_uuid": uuid}, sort_keys=True)
        except Exception as e:
            logger.debug(f"get_latest_uploaded_file_by_name failed for {binary_filename}: {e}")
        # 2. Operator drop zone
        local_path = ttp_library.TOOLS_DIR / binary_filename
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
        # 3. Register the local file with Mythic
        try:
            file_uuid = await mythic.register_file(self.client, filename=binary_filename, contents=local_path.read_bytes())
            return json.dumps({"status": "uploaded", "binary_filename": binary_filename, "file_uuid": file_uuid}, sort_keys=True)
        except Exception as e:
            return json.dumps({"status": "error", "binary_filename": binary_filename, "error": str(e)}, sort_keys=True)

    async def download_tool(
        self,
        binary_filename: Annotated[str, "The tool binary filename to fetch from its pinned TTP source, e.g. 'SharpHound.exe' (matches a TTP's binary_filename)."],
    ) -> str:
        """Download a tool binary from its pinned TTP source into the tools/ drop zone.

        Finds the TTP whose frontmatter binary_filename matches, reads its pinned
        `binary_download` block (url + archive_sha256 + extract_member), downloads the
        archive, VERIFIES the sha256 (tamper-evidence) BEFORE extracting, then extracts
        the named member into the tools/ drop zone. Does NOT upload to Mythic — call
        ensure_tool_uploaded(binary_filename) afterward to register it with Mythic.

        REQUIRES PRIOR OPERATOR APPROVAL: this fetches a binary from the internet. The
        agent must ask the operator and receive explicit approval before calling this.

        Args:
            binary_filename: The tool binary filename (matches a TTP's binary_filename).
        Returns:
            str: JSON with a "status" key describing the outcome.
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
