"""Opt-in shared resource accounting with no execution or authority surface."""
from __future__ import annotations

from contextlib import contextmanager
import fcntl
import hashlib
import json
import math
import os
import re
import stat
import tempfile
import time
from pathlib import Path
from typing import Any, Callable, Mapping
from uuid import uuid4


CONFIG_SCHEMA = "sage-shared-resource-budget-v1"
STATUS_SCHEMA = "sage-shared-resource-budget-status-v1"
_CONFIG_ENV = "SAGE_RESOURCE_BUDGET_CONFIG"
_LEDGER_ENV = "SAGE_RESOURCE_BUDGET_LEDGER"
_EPISODE_ENV = "SAGE_RESOURCE_EPISODE_ID"
_PHASE_ENV = "SAGE_RESOURCE_BUDGET_PHASE"
_SERVICE_ENV = "SAGE_RESOURCE_SERVICE_PROBE_ID"
_RECOVERY_SERVICE_LIMITS = {
    "max_starts": 1, "active_seconds": 900, "total_seconds": 1200,
    "control_requests": 1024, "active_control_requests": 896, "cleanup_control_requests": 128,
    "allowed_resources": ["control_requests"], "provider_authorized": False,
    "payload_tasks_authorized": False, "range_mutations_authorized": False,
}
_DIAGNOSTIC_SERVICE_LIMITS = {**_RECOVERY_SERVICE_LIMITS, "active_seconds": 2580, "total_seconds": 2700}
_DIAGNOSTIC_SESSION = {"stages": ["A", "B", "C"], "starts_per_stage": 2,
                       "max_stage_starts": 6, "stage_active_seconds": 120, "stage_total_seconds": 180}
_CONTINUATION_SCHEMA = "sage-resource-recovery-amendment-v4"
_CONTINUATION_SESSION = {"stages": ["A", "B", "C"], "max_stage_starts": 24,
    "pooled_active_seconds": 7200, "stage_active_seconds": 600,
    "stage_cleanup_seconds": 120, "stage_total_seconds": 720,
    "calendar_deadlines_inherited": True}
_SLUG = re.compile(r"^[a-z0-9][a-z0-9._-]{0,127}$")


class ResourceBudgetError(ValueError):
    pass


class ResourceBudgetExceeded(ResourceBudgetError):
    pass


def _positive_int(value: Any, name: str, *, allow_zero: bool = False) -> int:
    minimum = 0 if allow_zero else 1
    if type(value) is not int or value < minimum:
        raise ResourceBudgetError(f"{name} must be an integer >= {minimum}")
    return value


def _slug(value: Any, name: str) -> str:
    if not isinstance(value, str) or not _SLUG.fullmatch(value):
        raise ResourceBudgetError(f"{name} must be a simple non-empty slug")
    return value


class ResourceLedger:
    def __init__(self, config_path: str | Path, ledger_path: str | Path, now: Callable[[], float] = time.time,
                 *, episode_id: str | None = None, default_cleanup: bool = False,
                 service_probe_id: str | None = None):
        self.config_path = Path(config_path)
        self.ledger_path = Path(ledger_path)
        self._now = now
        if not callable(now):
            raise ResourceBudgetError("now must be callable")
        self.episode_id = None if episode_id is None else _slug(episode_id, "episode_id")
        self.service_probe_id = None if service_probe_id is None else _slug(service_probe_id, "service_probe_id")
        if self.episode_id is not None and self.service_probe_id is not None:
            raise ResourceBudgetError("service probe cannot select an episode")
        if type(default_cleanup) is not bool or (default_cleanup and self.episode_id is not None):
            raise ResourceBudgetError("cleanup configuration is invalid")
        self.default_cleanup = default_cleanup

    def _lock_path(self) -> Path:
        directory = Path(tempfile.gettempdir()) / f"sage-resource-budget-{os.getuid()}"
        try:
            directory.mkdir(mode=0o700, parents=True, exist_ok=True)
            metadata = os.stat(directory, follow_symlinks=False)
        except OSError as exc:
            raise ResourceBudgetError("resource ledger lock directory is invalid") from exc
        if not stat.S_ISDIR(metadata.st_mode) or metadata.st_uid != os.getuid() \
                or stat.S_IMODE(metadata.st_mode) != 0o700:
            raise ResourceBudgetError("resource ledger lock directory is invalid")
        identity = hashlib.sha256(str(self.ledger_path.resolve(strict=False)).encode("utf-8")).hexdigest()
        return directory / f"{identity}.lock"

    @contextmanager
    def _outer_lock(self):
        try:
            descriptor = os.open(self._lock_path(), os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW, 0o600)
            metadata = os.fstat(descriptor)
        except OSError as exc:
            raise ResourceBudgetError("resource ledger lock is invalid") from exc
        if not stat.S_ISREG(metadata.st_mode) or metadata.st_uid != os.getuid() \
                or stat.S_IMODE(metadata.st_mode) != 0o600:
            os.close(descriptor)
            raise ResourceBudgetError("resource ledger lock is invalid")
        try:
            fcntl.flock(descriptor, fcntl.LOCK_EX)
            yield
        finally:
            os.close(descriptor)

    def _config(self) -> tuple[dict[str, Any], str]:
        try:
            raw = self.config_path.read_bytes()
        except OSError as exc:
            raise ResourceBudgetError("resource budget config cannot be read") from exc
        try:
            config = json.loads(raw)
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise ResourceBudgetError("resource budget config is malformed") from exc
        if not isinstance(config, dict) or set(config) != {
            "schema", "resources", "calendar_seconds", "cleanup_seconds"
        } or config["schema"] != CONFIG_SCHEMA:
            raise ResourceBudgetError("resource budget config fields differ from schema")
        resources = config["resources"]
        if not isinstance(resources, dict) or not resources:
            raise ResourceBudgetError("resources must be a non-empty mapping")
        validated = {}
        for name, limits in resources.items():
            name = _slug(name, "resource name")
            if not isinstance(limits, dict) or set(limits) != {"limit", "cleanup_reserve"}:
                raise ResourceBudgetError(f"resource {name} fields differ from schema")
            limit = _positive_int(limits["limit"], f"{name}.limit")
            cleanup = _positive_int(limits["cleanup_reserve"], f"{name}.cleanup_reserve", allow_zero=True)
            if cleanup >= limit:
                raise ResourceBudgetError(f"{name}.cleanup_reserve must be smaller than limit")
            validated[name] = {"limit": limit, "cleanup_reserve": cleanup}
        calendar = _positive_int(config["calendar_seconds"], "calendar_seconds")
        cleanup_seconds = _positive_int(config["cleanup_seconds"], "cleanup_seconds")
        if cleanup_seconds >= calendar:
            raise ResourceBudgetError("cleanup_seconds must be smaller than calendar_seconds")
        return {
            "schema": CONFIG_SCHEMA,
            "resources": validated,
            "calendar_seconds": calendar,
            "cleanup_seconds": cleanup_seconds,
        }, hashlib.sha256(raw).hexdigest()

    def _read_events(self, handle, config: Mapping[str, Any], config_sha256: str) -> list[dict[str, Any]]:
        handle.seek(0)
        raw = handle.read()
        if not raw:
            return []
        if not raw.endswith("\n"):
            raise ResourceBudgetError("resource ledger is truncated")
        events = []
        for expected, line in enumerate(raw.splitlines()):
            try:
                event = json.loads(line)
            except json.JSONDecodeError as exc:
                raise ResourceBudgetError("resource ledger contains malformed JSON") from exc
            if not isinstance(event, dict) or event.get("seq") != expected:
                raise ResourceBudgetError("resource ledger sequence is invalid")
            events.append(event)
        prefixes = raw.splitlines(keepends=True)
        for event in events:
            if event.get("type") == "budget_amendment":
                approval = self._amendment_approval(event, config, config_sha256)
                prefix = "".join(prefixes[:event["seq"]]).encode("utf-8")
                if hashlib.sha256(prefix).hexdigest() != approval["ledger_prefix_sha256"]:
                    raise ResourceBudgetError("amendment ledger prefix differs")
        self._validate_events(events, config, config_sha256)
        return events

    @staticmethod
    def _bound_bytes(path: Any, sha256: Any) -> bytes:
        if not isinstance(path, str) or not Path(path).is_absolute() or not isinstance(sha256, str):
            raise ResourceBudgetError("amendment file binding is invalid")
        try:
            raw = Path(path).read_bytes()
        except OSError as exc:
            raise ResourceBudgetError("amendment bound file is unavailable") from exc
        if hashlib.sha256(raw).hexdigest() != sha256:
            raise ResourceBudgetError("amendment bound file drifted")
        return raw

    def _amendment_approval(self, event: Mapping[str, Any], config: Mapping[str, Any],
                            config_sha256: str) -> dict[str, Any]:
        try:
            value = json.loads(self._bound_bytes(event.get("approval_path"), event.get("approval_sha256")))
            if (value["schema"] not in {"sage-resource-recovery-amendment-v1", "sage-resource-recovery-amendment-v2", "sage-resource-recovery-amendment-v3", _CONTINUATION_SCHEMA}
                    or value["status"] != "approved"
                    or not isinstance(value["user_approval"], str) or not value["user_approval"].strip()
                    or value["amendment_id"] != event["amendment_id"]
                    or value["base_config_sha256"] != config_sha256
                    or value["ledger_prefix_records"] != event["seq"]
                    or value["next_sequence"] != event["seq"]
                    or value["original_aggregate_limits_unchanged"] is not True):
                raise ResourceBudgetError("amendment approval does not match the ledger")
            self._bound_bytes(**value["document"])
            parent = json.loads(self._bound_bytes(**value["parent_authority"]))
            if parent.get("status") != "active" or parent.get("revoked_at") is not None:
                raise ResourceBudgetError("amendment parent authority is inactive")
            changes = value["limit_changes"]
            if not isinstance(changes, dict) or set(changes) != {"capture_correction_cycles"}:
                raise ResourceBudgetError("amendment may change only capture correction allowance")
            change = changes["capture_correction_cycles"]
            replacement = value["schema"] == "sage-resource-recovery-amendment-v2"
            diagnostic = value["schema"] == "sage-resource-recovery-amendment-v3"
            continuation = value["schema"] == _CONTINUATION_SCHEMA
            expected_change = ({"old": 6, "new": 6} if continuation else
                {"old": 5, "new": 6} if diagnostic else ({"old": 4, "new": 5} if replacement else {"old": 2, "new": 4}))
            if (change != expected_change or config["resources"]["capture_correction_cycles"]["limit"] != 2):
                raise ResourceBudgetError("amendment correction limits differ")
            if replacement:
                prior = json.loads(self._bound_bytes(**value["prior_amendment"]))
                if (value.get("amendment_ordinal") != 2 or value.get("replacement_service_probe") is not True
                        or value.get("engineering_seconds") != 7200
                        or value.get("correction_engineering_seconds") != 1500
                        or prior.get("schema") != "sage-resource-recovery-amendment-v1"
                        or prior.get("status") != "approved"
                        or prior.get("base_config_sha256") != config_sha256
                        or prior.get("parent_authority") != value["parent_authority"]
                        or prior.get("limit_changes") != {"capture_correction_cycles": {"old": 2, "new": 4}}):
                    raise ResourceBudgetError("replacement amendment lineage differs")
            if diagnostic:
                prior = json.loads(self._bound_bytes(**value["prior_amendment"]))
                if (value.get("amendment_ordinal") != 3
                        or value.get("engineering_seconds") != 7200
                        or value.get("correction_engineering_seconds") != 1500
                        or value.get("diagnostic_session") != _DIAGNOSTIC_SESSION
                        or prior.get("schema") != "sage-resource-recovery-amendment-v2"
                        or prior.get("status") != "approved"
                        or prior.get("base_config_sha256") != config_sha256
                        or prior.get("parent_authority") != value["parent_authority"]
                        or prior.get("limit_changes") != {"capture_correction_cycles": {"old": 4, "new": 5}}):
                    raise ResourceBudgetError("diagnostic amendment lineage differs")
            service = value["service_probe"]
            if continuation:
                prior = json.loads(self._bound_bytes(**value["prior_amendment"]))
                if (value.get("amendment_ordinal") != 4 or value.get("engineering_seconds") != 43200
                        or value.get("engineering_cleanup_seconds") != 900
                        or value.get("continuation_cycle") != 6
                        or value.get("diagnostic_session") != _CONTINUATION_SESSION
                        or value.get("capture_recovery") != {"max_attempts": 6, "max_before_first_composite": 4,
                            "consume_episode_and_setup_retry": True, "interpretable_outcome_replacement": False}
                        or prior.get("schema") != "sage-resource-recovery-amendment-v3"
                        or prior.get("status") != "approved"
                        or prior.get("base_config_sha256") != config_sha256
                        or prior.get("parent_authority") != value["parent_authority"]
                        or prior.get("limit_changes") != {"capture_correction_cycles": {"old": 5, "new": 6}}):
                    raise ResourceBudgetError("continuation amendment lineage differs")
                self._engineering_usage(value, value["engineering_started_epoch"])
            expected_service = ({**_RECOVERY_SERVICE_LIMITS, "max_starts": 24,
                "active_seconds": config["calendar_seconds"] - config["cleanup_seconds"],
                "total_seconds": config["calendar_seconds"], "control_requests": 8192,
                "active_control_requests": 7168, "cleanup_control_requests": 1024}
                if continuation else _DIAGNOSTIC_SERVICE_LIMITS if diagnostic else _RECOVERY_SERVICE_LIMITS)
            for key in ("active_seconds", "total_seconds", "control_requests", "active_control_requests", "cleanup_control_requests"):
                _positive_int(service[key], key)
            if (service != expected_service
                    or service["allowed_resources"] != ["control_requests"]
                    or service["provider_authorized"] is not False or service["payload_tasks_authorized"] is not False
                    or service["range_mutations_authorized"] is not False
                    or service["active_seconds"] >= service["total_seconds"]
                    or service["active_control_requests"] + service["cleanup_control_requests"] != service["control_requests"]):
                raise ResourceBudgetError("amendment service limits are invalid")
            return value
        except (KeyError, TypeError, ValueError) as exc:
            if isinstance(exc, ResourceBudgetError):
                raise
            raise ResourceBudgetError("amendment approval is malformed") from exc

    @staticmethod
    def _engineering_usage(approval: Mapping[str, Any], now: float) -> float:
        """Read the existing append-only union-time accounting, including open work."""
        try:
            raw = Path(approval["engineering_intervals_path"]).read_bytes()
            lines = raw.splitlines(keepends=True)
            prefix = approval["engineering_prefix_records"]
            if (type(prefix) is not int or prefix < 0 or len(lines) < prefix
                    or hashlib.sha256(b"".join(lines[:prefix])).hexdigest() != approval["engineering_prefix_sha256"]):
                raise ResourceBudgetError("engineering accounting prefix differs")
            opened, intervals = {}, []
            for line in lines[prefix:]:
                row = json.loads(line); identifier = row["interval_id"]; epoch = row["epoch"]
                if type(epoch) not in (int, float) or epoch < approval["engineering_started_epoch"]:
                    raise ResourceBudgetError("engineering interval time differs")
                if row["event"] == "start" and identifier not in opened:
                    opened[identifier] = epoch
                elif row["event"] == "end" and identifier in opened and epoch >= opened[identifier]:
                    intervals.append((opened.pop(identifier), epoch))
                else:
                    raise ResourceBudgetError("engineering interval sequence differs")
            intervals.extend((start, max(start, now)) for start in opened.values())
            used = float(approval["engineering_previously_used_seconds"])
            if not 0 <= used <= 7200:
                raise ResourceBudgetError("engineering prior charge differs")
            finish = float("-inf")
            for begin, end in sorted(intervals):
                used += max(0, end - max(begin, finish)); finish = max(finish, end)
            return used
        except (OSError, KeyError, TypeError, ValueError) as exc:
            if isinstance(exc, ResourceBudgetError):
                raise
            raise ResourceBudgetError("engineering accounting is unavailable") from exc

    def _check_engineering(self, approvals: Mapping[str, Any], *, cleanup: bool = False) -> None:
        if not approvals:
            return
        approval = next(reversed(approvals.values()))
        if approval["schema"] == _CONTINUATION_SCHEMA:
            cap = approval["engineering_seconds"] - (0 if cleanup else approval["engineering_cleanup_seconds"])
            if self._engineering_usage(approval, float(self._now())) >= cap:
                raise ResourceBudgetExceeded("shared engineering time is exhausted")

    def apply_amendment(self, approval_path: str | Path, expected_sha256: str) -> None:
        """Append one approved correction allowance without replacing the base ledger."""
        if self.episode_id is not None or self.service_probe_id is not None:
            raise ResourceBudgetError("amendment requires the global ledger")
        config, digest = self._config()
        with self._outer_lock():
            if not self.ledger_path.exists():
                raise ResourceBudgetError("amendment requires an existing ledger")
            with self.ledger_path.open("a+", encoding="utf-8") as handle:
                fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
                events = self._read_events(handle, config, digest)
                if not events:
                    raise ResourceBudgetError("amendment ledger is empty")
                try:
                    approval = json.loads(self._bound_bytes(str(Path(approval_path).resolve()), expected_sha256))
                except (ValueError, TypeError) as exc:
                    raise ResourceBudgetError("amendment approval is malformed") from exc
                event = {"seq": len(events), "type": "budget_amendment",
                         "approval_path": str(Path(approval_path).resolve()), "approval_sha256": expected_sha256,
                         "amendment_id": approval.get("amendment_id"), "applied_epoch": float(self._now())}
                if any(e["type"] == "budget_amendment" and e["amendment_id"] == event["amendment_id"] for e in events):
                    raise ResourceBudgetError("amendment is duplicate")
                approval = self._amendment_approval(event, config, digest)
                handle.seek(0)
                if hashlib.sha256(handle.read().encode("utf-8")).hexdigest() != approval["ledger_prefix_sha256"]:
                    raise ResourceBudgetError("amendment ledger prefix differs")
                appended = [event]
                if approval["schema"] == _CONTINUATION_SCHEMA:
                    if self._state(events, config)["charges"]["capture_correction_cycles"] != 5:
                        raise ResourceBudgetError("continuation requires five charged corrections")
                    self._check_engineering({approval["amendment_id"]: approval})
                    appended.append({"seq": len(events) + 1, "type": "reservation",
                        "reservation_id": f"r-{uuid4().hex}", "operation": "m3-continuation-cycle-six",
                        "cleanup": False, "amounts": {"capture_correction_cycles": 1},
                        "reserved_epoch": float(self._now())})
                self._validate_events([*events, *appended], config, digest)
                self._append(handle, appended)

    @staticmethod
    def _probe_state(event, approval, config, started):
        limits = approval["service_probe"]
        continuation = approval["schema"] == _CONTINUATION_SCHEMA
        origin = started if continuation else event["started_epoch"]
        value = {"amendment_id": event["amendment_id"], "started_epoch": event["started_epoch"],
            "closed_epoch": None, "active_deadline_epoch": origin + limits["active_seconds"],
            "total_deadline_epoch": origin + limits["total_seconds"], "limits": limits,
            "active_control_requests": 0, "cleanup_control_requests": 0}
        if "diagnostic_session" in approval:
            value.update(stages=[], allocation=approval["diagnostic_session"], continuation=continuation)
        return value

    def _service_state(self, events: list[dict[str, Any]], config: Mapping[str, Any],
                       digest: str) -> tuple[dict[str, Any], dict[str, Any]]:
        amendments, probes = {}, {}
        for event in events:
            kind = event["type"]
            if kind == "budget_amendment":
                amendments[event["amendment_id"]] = self._amendment_approval(event, config, digest)
            elif kind == "service_probe_start":
                probes[event["probe_id"]] = self._probe_state(event, amendments[event["amendment_id"]], config, events[0]["started_epoch"])
            elif kind == "diagnostic_stage_start":
                self._apply_stage_start(probes, event)
            elif kind == "diagnostic_stage_active_end":
                self._apply_stage_active_end(probes, event)
            elif kind == "diagnostic_stage_close":
                self._apply_stage_close(probes, event)
            elif kind == "reservation" and event.get("service_probe_id") is not None:
                key = "cleanup_control_requests" if event["cleanup"] else "active_control_requests"
                probes[event["service_probe_id"]][key] += event["amounts"]["control_requests"]
            elif kind == "service_probe_close":
                probes[event["probe_id"]]["closed_epoch"] = event["closed_epoch"]
        return amendments, probes

    def _apply_stage_start(self, probes: dict[str, Any], event: Mapping[str, Any]) -> None:
        if set(event) != {"seq", "type", "probe_id", "stage", "started_epoch", "retry_evidence"}:
            raise ResourceBudgetError("diagnostic stage fields differ")
        probe = probes.get(event["probe_id"])
        if not probe or probe["closed_epoch"] is not None or "stages" not in probe:
            raise ResourceBudgetError("diagnostic session is unavailable")
        if probe.get("continuation"):
            self._apply_continuation_stage_start(probes, probe, event)
            return
        stages = probe["stages"]; stage = event["stage"]; now = event["started_epoch"]
        if (stage not in _DIAGNOSTIC_SESSION["stages"] or type(now) not in (int, float)
                or now >= probe["active_deadline_epoch"] or len(stages) >= 6
                or now < probe["started_epoch"]
                or any(s["closed_epoch"] is None or not s["contained"]
                       or s["containment_epoch"] > s["total_deadline_epoch"] for s in stages)):
            raise ResourceBudgetError("diagnostic stage is unavailable or overlaps")
        earlier = _DIAGNOSTIC_SESSION["stages"][:_DIAGNOSTIC_SESSION["stages"].index(stage)]
        if any(not any(s["stage"] == name and s["passed"] for s in stages) for name in earlier):
            raise ResourceBudgetError("diagnostic stages are out of order")
        same = [s for s in stages if s["stage"] == stage]
        if len(same) >= 2 or any(s["passed"] for s in same):
            raise ResourceBudgetError("diagnostic stage starts are consumed")
        ref = event["retry_evidence"]
        if same:
            if not isinstance(ref, dict) or set(ref) != {"path", "sha256"}:
                raise ResourceBudgetError("diagnostic retry needs bound evidence")
            evidence = json.loads(self._bound_bytes(**ref))
            if (evidence.get("stage") != stage or evidence.get("prior_result") != same[-1]["evidence"]
                    or evidence.get("cleanup_complete") is not True
                    or evidence.get("focused_probe_passed") is not True
                    or not isinstance(evidence.get("cause"), str) or not evidence["cause"].strip()
                    or not re.fullmatch(r"[a-f0-9]{64}", str(evidence.get("source_identity", "")))):
                raise ResourceBudgetError("diagnostic retry evidence differs")
        elif ref is not None:
            raise ResourceBudgetError("first diagnostic start cannot be a retry")
        stages.append({"stage": stage, "attempt": len(same) + 1, "started_epoch": now,
            "active_deadline_epoch": min(now + 120, probe["active_deadline_epoch"]),
            "total_deadline_epoch": min(now + 180, probe["total_deadline_epoch"]),
            "closed_epoch": None, "containment_epoch": None,
            "passed": False, "contained": False, "evidence": None})

    def _apply_continuation_stage_start(self, probes, probe, event):
        allocation = probe["allocation"]; stage = event["stage"]; now = event["started_epoch"]
        siblings = [p for p in probes.values() if p["amendment_id"] == probe["amendment_id"]]
        stages = [s for p in siblings for s in p["stages"]]
        if (stage not in allocation["stages"] or type(now) not in (int, float)
                or now < probe["started_epoch"] or now >= probe["active_deadline_epoch"]
                or len(stages) >= allocation["max_stage_starts"]
                or any(s["closed_epoch"] is None or not s["contained"]
                    or s["containment_epoch"] > s["total_deadline_epoch"] for s in stages)):
            raise ResourceBudgetError("continuation diagnostic start is unavailable")
        used = sum(s["active_ended_epoch"] - s["started_epoch"] for s in stages)
        remaining = allocation["pooled_active_seconds"] - used
        if remaining < allocation["stage_active_seconds"] or any(sum(p[key] for p in siblings) >= probe["limits"][key]
                for key in ("active_control_requests", "cleanup_control_requests")):
            raise ResourceBudgetExceeded("continuation diagnostic pool is exhausted")
        earlier = allocation["stages"][:allocation["stages"].index(stage)]
        prior_close = float("-inf")
        for name in earlier:
            latest = next((s for s in reversed(stages) if s["stage"] == name), None)
            if latest is None or not latest["passed"] or latest["started_epoch"] < prior_close:
                raise ResourceBudgetError("continuation prerequisites are not current")
            prior_close = latest["containment_epoch"]
        same = [s for s in stages if s["stage"] == stage]
        ref = event["retry_evidence"]
        if same:
            if not isinstance(ref, dict) or set(ref) != {"path", "sha256"}:
                raise ResourceBudgetError("continuation retry needs bound evidence")
            evidence = json.loads(self._bound_bytes(**ref))
            if (evidence.get("stage") != stage or evidence.get("prior_result") != same[-1]["evidence"]
                    or evidence.get("cleanup_complete") is not True or evidence.get("focused_probe_passed") is not True
                    or not isinstance(evidence.get("cause"), str) or not evidence["cause"].strip()
                    or not re.fullmatch(r"[a-f0-9]{64}", str(evidence.get("source_identity", "")))
                    or not isinstance(evidence.get("discriminating_probe"), dict)):
                raise ResourceBudgetError("continuation retry evidence differs")
            self._bound_bytes(**evidence["discriminating_probe"])
            identity = (evidence["cause"], evidence["discriminating_probe"]["sha256"])
            for previous in stages:
                if previous.get("retry_evidence"):
                    prior = json.loads(self._bound_bytes(**previous["retry_evidence"]))
                    if identity == (prior["cause"], prior["discriminating_probe"]["sha256"]):
                        raise ResourceBudgetError("continuation repeats an unchanged diagnostic probe")
        elif ref is not None:
            raise ResourceBudgetError("first diagnostic start cannot be a retry")
        active_deadline = min(now + allocation["stage_active_seconds"], now + remaining, probe["active_deadline_epoch"])
        total_deadline = active_deadline + allocation["stage_cleanup_seconds"]
        if total_deadline > probe["total_deadline_epoch"]:
            raise ResourceBudgetExceeded("diagnostic cleanup time is unavailable")
        probe["stages"].append({"stage": stage, "attempt": len(same) + 1, "started_epoch": now,
            "active_deadline_epoch": active_deadline, "total_deadline_epoch": total_deadline,
            "active_ended_epoch": None, "closed_epoch": None, "passed": False,
            "containment_epoch": None, "contained": False, "evidence": None, "retry_evidence": ref})

    @staticmethod
    def _apply_stage_active_end(probes, event):
        if set(event) != {"seq", "type", "probe_id", "active_ended_epoch"}:
            raise ResourceBudgetError("diagnostic active-end fields differ")
        probe = probes.get(event["probe_id"])
        if not probe or not probe.get("continuation") or probe["closed_epoch"] is not None or not probe["stages"]:
            raise ResourceBudgetError("diagnostic active-end scope is unavailable")
        stage = probe["stages"][-1]; now = event["active_ended_epoch"]
        if (stage["closed_epoch"] is not None or stage["active_ended_epoch"] is not None
                or type(now) not in (int, float) or now < stage["started_epoch"]):
            raise ResourceBudgetError("diagnostic active-end is invalid")
        # Time spent after the active deadline is cleanup; the active allocation
        # remains consumed in full and no active dispatch is permitted there.
        stage["active_ended_epoch"] = min(now, stage["active_deadline_epoch"])
        stage["total_deadline_epoch"] = min(stage["total_deadline_epoch"], now + 120)

    def end_diagnostic_active(self, probe_id: str) -> dict[str, Any]:
        return self._append_diagnostic({"type": "diagnostic_stage_active_end", "probe_id": probe_id,
            "active_ended_epoch": float(self._now())})

    def _apply_stage_close(self, probes: dict[str, Any], event: Mapping[str, Any]) -> None:
        if set(event) != {"seq", "type", "probe_id", "closed_epoch", "passed", "contained", "evidence"}:
            raise ResourceBudgetError("diagnostic stage close fields differ")
        probe = probes.get(event["probe_id"])
        if not probe or not probe.get("stages") or probe["stages"][-1]["closed_epoch"] is not None:
            raise ResourceBudgetError("diagnostic stage close is unavailable")
        stage = probe["stages"][-1]
        if probe.get("continuation") and stage["active_ended_epoch"] is None:
            raise ResourceBudgetError("diagnostic active phase has not ended")
        if (type(event["passed"]) is not bool or type(event["contained"]) is not bool
                or type(event["closed_epoch"]) not in (int, float)
                or event["closed_epoch"] < stage["started_epoch"]
                or not isinstance(event["evidence"], dict)):
            raise ResourceBudgetError("diagnostic stage close is invalid")
        result = json.loads(self._bound_bytes(**event["evidence"]))
        explicit = "containment_epoch" in result or "contained_epoch" in result
        containment_epoch = result.get("containment_epoch", result.get("contained_epoch", event["closed_epoch"]))
        if explicit and (event["contained"] is not True or type(containment_epoch) not in (int, float)
                or not math.isfinite(containment_epoch) or containment_epoch < stage["started_epoch"]
                or containment_epoch > event["closed_epoch"]):
            raise ResourceBudgetError("diagnostic containment evidence differs")
        if "containment_addendum" in result:
            ref = result["containment_addendum"]
            if not explicit or not isinstance(ref, dict) or set(ref) != {"path", "sha256"}:
                raise ResourceBudgetError("diagnostic containment evidence differs")
            addendum = json.loads(self._bound_bytes(**ref))
            if (addendum.get("contained_epoch") != containment_epoch
                    or addendum.get("owned_process_dead") is not True
                    or addendum.get("stage") != stage["stage"]):
                raise ResourceBudgetError("diagnostic containment evidence differs")
        if (result.get("stage") != stage["stage"] or result.get("passed") is not event["passed"]
                or result.get("contained") is not event["contained"]
                or (event["passed"] and (not event["contained"] or containment_epoch > stage["total_deadline_epoch"]))):
            raise ResourceBudgetError("diagnostic stage result differs")
        stage.update({key: event[key] for key in ("closed_epoch", "passed", "contained", "evidence")})
        stage["containment_epoch"] = containment_epoch

    def _append_diagnostic(self, event: dict[str, Any]) -> dict[str, Any]:
        config, digest = self._config()
        with self._outer_lock():
            with self.ledger_path.open("a+", encoding="utf-8") as handle:
                fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
                events = self._read_events(handle, config, digest)
                if event["type"] == "diagnostic_stage_start":
                    self._check_engineering(self._service_state(events, config, digest)[0])
                event = {"seq": len(events), **event}
                self._validate_events([*events, event], config, digest)
                self._append(handle, [event])
                return self._service_state([*events, event], config, digest)[1][event["probe_id"]]["stages"][-1]

    def start_diagnostic_stage(self, probe_id: str, stage: str, retry_evidence: dict[str, str] | None = None) -> dict[str, Any]:
        return self._append_diagnostic({"type": "diagnostic_stage_start", "probe_id": probe_id,
            "stage": stage, "started_epoch": float(self._now()), "retry_evidence": retry_evidence})

    def close_diagnostic_stage(self, probe_id: str, passed: bool, contained: bool,
                               evidence_path: str, evidence_sha256: str) -> dict[str, Any]:
        return self._append_diagnostic({"type": "diagnostic_stage_close", "probe_id": probe_id,
            "closed_epoch": float(self._now()), "passed": passed, "contained": contained,
            "evidence": {"path": str(Path(evidence_path).resolve()), "sha256": evidence_sha256}})

    def start_service_probe(self, probe_id: str, amendment_id: str) -> dict[str, Any]:
        probe_id, amendment_id = _slug(probe_id, "probe_id"), _slug(amendment_id, "amendment_id")
        config, digest = self._config()
        with self._outer_lock():
            if not self.ledger_path.exists():
                raise ResourceBudgetError("service probe requires the amended ledger")
            with self.ledger_path.open("a+", encoding="utf-8") as handle:
                fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
                events = self._read_events(handle, config, digest)
                amendments, probes = self._service_state(events, config, digest)
                if (amendment_id not in amendments or amendment_id != next(reversed(amendments))
                        or probe_id in probes or any(p["closed_epoch"] is None for p in probes.values())
                        or sum(p["amendment_id"] == amendment_id for p in probes.values()) >= amendments[amendment_id]["service_probe"]["max_starts"]):
                    raise ResourceBudgetError("service probe is unavailable or already consumed")
                self._check_engineering(amendments)
                now = float(self._now())
                if now >= events[0]["started_epoch"] + config["calendar_seconds"] - config["cleanup_seconds"]:
                    raise ResourceBudgetExceeded("resource budget active window is exhausted")
                if any(ep["closed_epoch"] is None for ep in self._state(events, config)["episodes"].values()):
                    raise ResourceBudgetError("service probe cannot overlap an episode")
                event = {"seq": len(events), "type": "service_probe_start", "probe_id": probe_id,
                         "amendment_id": amendment_id, "started_epoch": now}
                self._validate_events([*events, event], config, digest)
                self._append(handle, [event])
                return self._service_state([*events, event], config, digest)[1][probe_id]

    def close_service_probe(self, probe_id: str) -> None:
        probe_id = _slug(probe_id, "probe_id")
        config, digest = self._config()
        with self._outer_lock():
            if not self.ledger_path.exists():
                raise ResourceBudgetError("service probe is not started")
            with self.ledger_path.open("a+", encoding="utf-8") as handle:
                fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
                events = self._read_events(handle, config, digest)
                probe = self._service_state(events, config, digest)[1].get(probe_id)
                if probe is None:
                    raise ResourceBudgetError("service probe is not started")
                if probe["closed_epoch"] is None:
                    event = {"seq": len(events), "type": "service_probe_close",
                             "probe_id": probe_id, "closed_epoch": float(self._now())}
                    self._validate_events([*events, event], config, digest)
                    self._append(handle, [event])

    def _validate_events(self, events: list[dict[str, Any]], config: Mapping[str, Any], config_sha256: str) -> None:
        if not events:
            return
        start = events[0]
        if set(start) != {"seq", "type", "config_sha256", "started_epoch"} or start["type"] != "start":
            raise ResourceBudgetError("resource ledger start record is invalid")
        if start["config_sha256"] != config_sha256:
            raise ResourceBudgetError("resource budget config drifted after the ledger started")
        if type(start["started_epoch"]) not in (int, float):
            raise ResourceBudgetError("resource ledger start time is invalid")
        reservations, settlements, episodes = {}, set(), {}
        scope_charges, scope_unknown, scope_overrun = {}, {}, {}
        amendments, probes = {}, {}
        amendment_events = {}
        activation_count = 0
        recovery_episodes: set[str] = set()
        recovery_sources: set[str] = set()
        recovery_attempts = 0
        recovery_before_first = 0
        first_capture_evidence: dict[str, str] | None = None
        for event in events[1:]:
            if event.get("type") == "reservation":
                unscoped = {"seq", "type", "reservation_id", "operation", "cleanup", "amounts", "reserved_epoch"}
                if set(event) not in (unscoped, unscoped | {"episode_id"}, unscoped | {"service_probe_id"}):
                    raise ResourceBudgetError("resource reservation record is invalid")
                reservation_id = _slug(event["reservation_id"], "reservation_id")
                if reservation_id in reservations or type(event["cleanup"]) is not bool:
                    raise ResourceBudgetError("resource reservation contradicts the ledger")
                if type(event["reserved_epoch"]) not in (int, float):
                    raise ResourceBudgetError("resource reservation time is invalid")
                _slug(event["operation"], "operation")
                self._validate_amounts(event["amounts"], config)
                service_id = event.get("service_probe_id")
                active_probes = [key for key, probe in probes.items() if probe["closed_epoch"] is None]
                if active_probes and service_id not in active_probes:
                    raise ResourceBudgetError("unscoped request during service probe")
                if service_id is not None:
                    probe = probes.get(service_id)
                    if probe is None or probe["closed_epoch"] is not None or set(event["amounts"]) != {"control_requests"}:
                        raise ResourceBudgetError("service probe permits only scoped control requests")
                    key = "cleanup_control_requests" if event["cleanup"] else "active_control_requests"
                    deadline_key = "total_deadline_epoch" if event["cleanup"] else "active_deadline_epoch"
                    if event["reserved_epoch"] >= probe[deadline_key]:
                        raise ResourceBudgetError("service probe deadline exceeded")
                    if "stages" in probe:
                        stages = probe["stages"]
                        if (not stages or stages[-1]["closed_epoch"] is not None
                                or event["reserved_epoch"] >= stages[-1][deadline_key]
                                or (not event["cleanup"] and stages[-1].get("active_ended_epoch") is not None)
                                or (probe.get("continuation") and event["cleanup"]
                                    and stages[-1].get("active_ended_epoch") is None)):
                            raise ResourceBudgetError("diagnostic stage is unavailable or expired")
                    probe[key] += event["amounts"]["control_requests"]
                    charge = (sum(p[key] for p in probes.values() if p["amendment_id"] == probe["amendment_id"])
                              if probe.get("continuation") else probe[key])
                    if charge > probe["limits"][key]:
                        raise ResourceBudgetError("service probe control limit exceeded")
                episode_id = event.get("episode_id")
                if episode_id is not None:
                    episode_id = _slug(episode_id, "episode_id")
                    if event["cleanup"] or episode_id not in episodes or episodes[episode_id]["closed_epoch"] is not None:
                        raise ResourceBudgetError("episode reservation contradicts the ledger")
                    definition = episodes[episode_id]
                    amounts = event["amounts"]
                    activated = definition["activated_epoch"]
                    if set(amounts) == {"control_requests"}:
                        if activated is not None and event["reserved_epoch"] >= (
                            activated + definition["action_seconds"] + definition["settlement_seconds"]
                        ):
                            raise ResourceBudgetError("episode settlement window was exceeded")
                    elif set(amounts) == {"payload_task_attempts"}:
                        if activated is None or event["reserved_epoch"] >= activated + definition["action_seconds"]:
                            raise ResourceBudgetError("episode action window was exceeded")
                        if scope_charges[episode_id]["payload_task_attempts"] + amounts["payload_task_attempts"] \
                                > definition["payload_task_limit"]:
                            raise ResourceBudgetError("episode task limit was exceeded")
                    elif set(amounts) == {"provider_requests", "provider_tokens"}:
                        if activated is None or event["reserved_epoch"] >= activated + definition["action_seconds"]:
                            raise ResourceBudgetError("episode action window was exceeded")
                        if amounts["provider_tokens"] != definition["per_call_token_reservation"]:
                            raise ResourceBudgetError("episode provider reservation differs from its binding")
                        effective = scope_charges[episode_id]["provider_tokens"]
                        if scope_unknown[episode_id] or scope_overrun[episode_id] or effective >= definition["provider_token_limit"] \
                                or effective + amounts["provider_tokens"] > definition["provider_token_limit"] + definition["per_call_token_reservation"]:
                            raise ResourceBudgetError("episode provider limit was exceeded")
                    else:
                        raise ResourceBudgetError("episode reservation contains an unsupported resource")
                    for name, amount in amounts.items():
                        scope_charges[episode_id][name] += amount
                reservations[reservation_id] = event
            elif event.get("type") == "provider_settlement":
                if set(event) != {"seq", "type", "reservation_id", "actual_tokens"}:
                    raise ResourceBudgetError("provider settlement record is invalid")
                reservation_id = event["reservation_id"]
                actual = event["actual_tokens"]
                if reservation_id not in reservations or reservation_id in settlements:
                    raise ResourceBudgetError("provider settlement contradicts the ledger")
                self._provider_reservation(reservations[reservation_id])
                if actual is not None:
                    _positive_int(actual, "actual_tokens", allow_zero=True)
                reservation = reservations[reservation_id]
                episode_id = reservation.get("episode_id")
                if episode_id is not None:
                    reserve = reservation["amounts"]["provider_tokens"]
                    if actual is None:
                        scope_unknown[episode_id] = True
                    else:
                        scope_charges[episode_id]["provider_tokens"] += actual - reserve
                        if actual > reserve:
                            scope_overrun[episode_id] = True
                settlements.add(reservation_id)
            elif event.get("type") == "episode_register":
                if any(probe["closed_epoch"] is None for probe in probes.values()):
                    raise ResourceBudgetError("episode registration overlaps service probe")
                required = {"seq", "type", "episode_id", "action_seconds", "settlement_seconds",
                            "provider_token_limit", "per_call_token_reservation", "payload_task_limit"}
                if set(event) != required:
                    raise ResourceBudgetError("episode registration record is invalid")
                episode_id = _slug(event["episode_id"], "episode_id")
                if episode_id in episodes:
                    raise ResourceBudgetError("episode registration contradicts the ledger")
                definition = {name: _positive_int(event[name], name) for name in required - {"seq", "type", "episode_id"}}
                definition.update({"activated_epoch": None, "closed_epoch": None})
                episodes[episode_id] = definition
                scope_charges[episode_id] = {name: 0 for name in config["resources"]}
                scope_unknown[episode_id] = scope_overrun[episode_id] = False
            elif event.get("type") == "episode_activate":
                if set(event) != {"seq", "type", "episode_id", "activated_epoch"} or \
                        type(event["activated_epoch"]) not in (int, float):
                    raise ResourceBudgetError("episode activation record is invalid")
                episode_id = _slug(event["episode_id"], "episode_id")
                if episode_id not in episodes or episodes[episode_id]["activated_epoch"] is not None \
                        or episodes[episode_id]["closed_epoch"] is not None:
                    raise ResourceBudgetError("episode activation contradicts the ledger")
                if episode_id not in recovery_episodes:
                    activation_count += 1
                if "episodes" not in config["resources"] or activation_count + len(recovery_episodes) > (
                    config["resources"]["episodes"]["limit"] - config["resources"]["episodes"]["cleanup_reserve"]
                ):
                    raise ResourceBudgetError("episode activation exceeded the global limit")
                episodes[episode_id]["activated_epoch"] = event["activated_epoch"]
            elif event.get("type") == "episode_close":
                if set(event) != {"seq", "type", "episode_id", "closed_epoch"} or \
                        type(event["closed_epoch"]) not in (int, float):
                    raise ResourceBudgetError("episode close record is invalid")
                episode_id = _slug(event["episode_id"], "episode_id")
                if episode_id not in episodes or episodes[episode_id]["closed_epoch"] is not None:
                    raise ResourceBudgetError("episode close contradicts the ledger")
                episodes[episode_id]["closed_epoch"] = event["closed_epoch"]
            elif event.get("type") == "capture_recovery_reservation":
                required = {"seq", "type", "episode_id", "amendment_id", "amounts", "reserved_epoch",
                            "prior_disposition_path", "prior_disposition_sha256"}
                if set(event) != required or event.get("amounts") != {
                    "episodes": 1, "burned_setup_retries": 1,
                } or type(event.get("reserved_epoch")) not in (int, float):
                    raise ResourceBudgetError("capture recovery reservation is invalid")
                episode_id = _slug(event["episode_id"], "episode_id")
                approval = amendments.get(event["amendment_id"])
                if (approval is None or approval["schema"] != _CONTINUATION_SCHEMA
                        or event["amendment_id"] != next(reversed(amendments))
                        or episode_id not in episodes or episodes[episode_id]["activated_epoch"] is not None
                        or episodes[episode_id]["closed_epoch"] is not None
                        or episode_id in recovery_episodes or any(p["closed_epoch"] is None for p in probes.values())
                        or any(key != episode_id and value["closed_epoch"] is None for key, value in episodes.items())
                        or event["reserved_epoch"] >= start["started_epoch"] + config["calendar_seconds"]
                        - config["cleanup_seconds"]):
                    raise ResourceBudgetError("capture recovery scope is unavailable")
                try:
                    disposition = json.loads(self._bound_bytes(
                        event["prior_disposition_path"], event["prior_disposition_sha256"]
                    ))
                    source = _slug(disposition.get("episode_id"), "episode_id")
                    evidence = disposition.get("source_evidence")
                    if (source == episode_id or source not in episodes
                            or episodes[source]["closed_epoch"] is None or source in recovery_sources
                            or disposition.get("protocol_valid") is not False
                            or disposition.get("interpretable") is not False
                            or disposition.get("contained") is not True
                            or not isinstance(evidence, list) or not evidence
                            or any(not isinstance(item, dict) or set(item) != {"path", "sha256"}
                                   for item in evidence)):
                        raise ResourceBudgetError("capture recovery disposition differs")
                    for item in evidence:
                        self._bound_bytes(**item)
                except (json.JSONDecodeError, TypeError, ValueError) as exc:
                    if isinstance(exc, ResourceBudgetError): raise
                    raise ResourceBudgetError("capture recovery disposition differs") from exc
                limit = approval["capture_recovery"]
                if (recovery_attempts >= limit["max_attempts"]
                        or first_capture_evidence is None
                        and recovery_before_first >= limit["max_before_first_composite"]):
                    raise ResourceBudgetError("capture recovery pool is exhausted")
                standard_episode_charges = sum(
                    row["amounts"].get("episodes", 0) for row in reservations.values()
                )
                standard_setup_charges = sum(
                    row["amounts"].get("burned_setup_retries", 0) for row in reservations.values()
                )
                for name, used in (("episodes", activation_count + len(recovery_episodes)
                                    + standard_episode_charges),
                                   ("burned_setup_retries", len(recovery_episodes)
                                    + standard_setup_charges)):
                    if name not in config["resources"] or used + 1 > (
                        config["resources"][name]["limit"] - config["resources"][name]["cleanup_reserve"]
                    ):
                        raise ResourceBudgetError("capture recovery global resource is exhausted")
                recovery_episodes.add(episode_id); recovery_sources.add(source); recovery_attempts += 1
                if first_capture_evidence is None: recovery_before_first += 1
            elif event.get("type") == "capture_first_composite":
                required = {"seq", "type", "episode_id", "amendment_id", "evidence_path",
                            "evidence_sha256", "recorded_epoch"}
                episode_id = _slug(event.get("episode_id"), "episode_id")
                approval = amendments.get(event.get("amendment_id"))
                if (set(event) != required or first_capture_evidence is not None
                        or approval is None or approval["schema"] != _CONTINUATION_SCHEMA
                        or event["amendment_id"] != next(reversed(amendments))
                        or type(event["recorded_epoch"]) not in (int, float)
                        or episode_id not in episodes or episodes[episode_id]["activated_epoch"] is None
                        or episodes[episode_id]["closed_epoch"] is None
                        or any(p["closed_epoch"] is None for p in probes.values())
                        or any(value["closed_epoch"] is None for value in episodes.values())):
                    raise ResourceBudgetError("first capture composite is invalid")
                try:
                    evidence = json.loads(self._bound_bytes(event["evidence_path"], event["evidence_sha256"]))
                    if (not isinstance(evidence, dict) or set(evidence) != {"schema", "episode_id",
                            "fixture_only", "episode_source", "bridge_result"}
                            or evidence.get("schema") != "sage-m3-first-capture-composite-v1"
                            or evidence.get("fixture_only") is not False
                            or evidence.get("episode_id") != episode_id):
                        raise ResourceBudgetError("first capture composite evidence differs")
                    episode_raw = self._bound_bytes(**evidence["episode_source"])
                    episode_source = json.loads(episode_raw)
                    bridge = json.loads(self._bound_bytes(**evidence["bridge_result"]))
                    bridge_fields = {"schema", "fixture_only", "admission", "episode_sha256",
                                     "observer_sha256", "risk_values_complete"}
                    if (not isinstance(episode_source, dict)
                            or episode_source.get("schema") != "sage-measurement-capture-episode-v1"
                            or episode_source.get("episode_id") != episode_id
                            or not isinstance(episode_source.get("requested_identity"), dict)
                            or episode_source["requested_identity"].get("measurement_kind") != "capture"
                            or episode_source["requested_identity"].get("side") != "harness"
                            or episode_source["requested_identity"].get("resource_episode_id") != episode_id
                            or episode_source.get("raw_result") is not None
                            or not isinstance(episode_source.get("native_solver_result"), dict)
                            or not isinstance(bridge, dict) or set(bridge) != bridge_fields
                            or bridge.get("schema") != "sage-capture-composite-bridge-result-v1"
                            or bridge.get("fixture_only") is not False
                            or bridge.get("risk_values_complete") is not True
                            or bridge.get("episode_sha256") != hashlib.sha256(episode_raw).hexdigest()
                            or not isinstance(bridge.get("observer_sha256"), str)
                            or not re.fullmatch(r"[a-f0-9]{64}", bridge["observer_sha256"])):
                        raise ResourceBudgetError("first capture composite evidence differs")
                    admission = bridge["admission"]
                    admission_fields = {"schema", "mechanism_id", "status", "reason_codes",
                        "composite_record", "composite_row", "authorization", "result_hash"}
                    authorization = {"promotion_authorized": False,
                        "runtime_application_authorized": False, "release_authorized": False,
                        "publication_authorized": False, "external_claim_authorized": False,
                        "phase2_authorized": False}
                    if (not isinstance(admission, dict) or set(admission) != admission_fields
                            or admission.get("schema") != "composite-source-admission-result-v2"
                            or admission.get("mechanism_id") != "composite-source-admission-v2"
                            or admission.get("status") != "admitted" or admission.get("reason_codes") != []
                            or admission.get("authorization") != authorization):
                        raise ResourceBudgetError("first capture composite evidence differs")
                    result_payload = {key: value for key, value in admission.items() if key != "result_hash"}
                    result_hash = "sha256:" + hashlib.sha256(json.dumps(result_payload, sort_keys=True,
                        separators=(",", ":"), ensure_ascii=True).encode()).hexdigest()
                    record, row = admission.get("composite_record"), admission.get("composite_row")
                    scorecard = episode_source.get("scorecard")
                    identity = (scorecard.get("measurement_identity")
                                if isinstance(scorecard, dict) else None)
                    requested_arm = episode_source["requested_identity"].get("measurement_arm")
                    if (admission.get("result_hash") != result_hash
                            or not isinstance(record, dict)
                            or set(record) != {"canonical_row", "evidence_projection", "identity"}
                            or not isinstance(row, dict)
                            or row.get("schema") != "canonical-safe-success-composite-row-v2"
                            or not isinstance(identity, dict)
                            or not isinstance(identity.get("policy_arm"), str) or not identity["policy_arm"]
                            or not isinstance(requested_arm, str) or not requested_arm
                            or identity["policy_arm"] != requested_arm
                            or record.get("identity") != identity or row.get("identity") != identity):
                        raise ResourceBudgetError("first capture composite evidence differs")
                except (json.JSONDecodeError, TypeError, ValueError) as exc:
                    if isinstance(exc, ResourceBudgetError): raise
                    raise ResourceBudgetError("first capture composite evidence differs") from exc
                first_capture_evidence = {"path": event["evidence_path"],
                                          "sha256": event["evidence_sha256"]}
            elif event.get("type") == "budget_amendment":
                if (set(event) != {"seq", "type", "approval_path", "approval_sha256", "amendment_id", "applied_epoch"}
                        or event["amendment_id"] in amendments
                        or type(event["applied_epoch"]) not in (int, float)):
                    raise ResourceBudgetError("budget amendment is duplicate or malformed")
                _slug(event["amendment_id"], "amendment_id")
                approval = self._amendment_approval(event, config, config_sha256)
                if approval["schema"] == "sage-resource-recovery-amendment-v1":
                    if amendments:
                        raise ResourceBudgetError("budget amendment is duplicate or reordered")
                else:
                    prior_count = (3 if approval["schema"] == _CONTINUATION_SCHEMA else
                                   2 if approval["schema"] == "sage-resource-recovery-amendment-v3" else 1)
                    if len(amendments) != prior_count or len(probes) != prior_count:
                        raise ResourceBudgetError("replacement amendment requires the consumed prior probe")
                    prior_id = next(reversed(amendments)); prior_event = amendment_events[prior_id]
                    if (approval["prior_amendment"] != {"path": prior_event["approval_path"],
                                                        "sha256": prior_event["approval_sha256"]}
                            or any(p["closed_epoch"] is None for p in probes.values())
                            or set(p["amendment_id"] for p in probes.values()) != set(amendments)
                            or any(ep["closed_epoch"] is None for ep in episodes.values())):
                        raise ResourceBudgetError("replacement amendment lineage or closed scope differs")
                amendments[event["amendment_id"]] = approval
                amendment_events[event["amendment_id"]] = event
            elif event.get("type") == "service_probe_start":
                if (set(event) != {"seq", "type", "probe_id", "amendment_id", "started_epoch"}
                        or event["amendment_id"] not in amendments
                        or event["amendment_id"] != next(reversed(amendments))
                        or event["probe_id"] in probes
                        or any(p["closed_epoch"] is None for p in probes.values())
                        or sum(p["amendment_id"] == event["amendment_id"] for p in probes.values()) >=
                            amendments[event["amendment_id"]]["service_probe"]["max_starts"]
                        or type(event["started_epoch"]) not in (int, float)
                        or any(ep["closed_epoch"] is None for ep in episodes.values())):
                    raise ResourceBudgetError("service probe start is duplicate or invalid")
                _slug(event["probe_id"], "probe_id")
                probes[event["probe_id"]] = self._probe_state(event, amendments[event["amendment_id"]], config, start["started_epoch"])
            elif event.get("type") == "diagnostic_stage_start":
                self._apply_stage_start(probes, event)
            elif event.get("type") == "diagnostic_stage_active_end":
                self._apply_stage_active_end(probes, event)
            elif event.get("type") == "diagnostic_stage_close":
                self._apply_stage_close(probes, event)
            elif event.get("type") == "service_probe_close":
                if (set(event) != {"seq", "type", "probe_id", "closed_epoch"}
                        or type(event["closed_epoch"]) not in (int, float)
                        or event["probe_id"] not in probes or probes[event["probe_id"]]["closed_epoch"] is not None):
                    raise ResourceBudgetError("service probe close is invalid")
                if any(s["closed_epoch"] is None for s in probes[event["probe_id"]].get("stages", [])):
                    raise ResourceBudgetError("service probe cannot close an active diagnostic stage")
                probes[event["probe_id"]]["closed_epoch"] = event["closed_epoch"]
            else:
                raise ResourceBudgetError("resource ledger contains an unknown event")

    def _validate_amounts(self, amounts: Any, config: Mapping[str, Any]) -> dict[str, int]:
        if not isinstance(amounts, dict) or not amounts:
            raise ResourceBudgetError("amounts must be a non-empty mapping")
        cleaned = {}
        for name, amount in amounts.items():
            if name not in config["resources"]:
                raise ResourceBudgetError(f"unknown resource: {name}")
            cleaned[name] = _positive_int(amount, f"amounts.{name}")
        provider_fields = {"provider_requests", "provider_tokens"}.intersection(cleaned)
        if provider_fields and not (
            cleaned.get("provider_requests") == 1 and cleaned.get("provider_tokens", 0) > 0
            and {"provider_requests", "provider_tokens"}.issubset(config["resources"])
        ):
            raise ResourceBudgetError("provider reservation requires one request and a positive token reserve")
        return cleaned

    @staticmethod
    def _provider_reservation(event: Mapping[str, Any]) -> None:
        amounts = event["amounts"]
        if amounts.get("provider_requests") != 1 or type(amounts.get("provider_tokens")) is not int \
                or amounts["provider_tokens"] <= 0:
            raise ResourceBudgetError("reservation is not a provider reservation")

    def _state(self, events: list[dict[str, Any]], config: Mapping[str, Any]) -> dict[str, Any]:
        charges = {name: 0 for name in config["resources"]}
        reservations, settled, unknown, overrun, episodes = {}, {}, False, False, {}
        recovery_episodes: set[str] = set()
        capture_recovery = {"total_attempts": 0, "before_first_composite": 0,
                            "first_composite_evidence": None}
        for event in events:
            if event.get("type") == "episode_register":
                episodes[event["episode_id"]] = {
                    name: event[name] for name in (
                        "action_seconds", "settlement_seconds", "provider_token_limit",
                        "per_call_token_reservation", "payload_task_limit"
                    )
                }
                episodes[event["episode_id"]].update({
                    "activated_epoch": None, "closed_epoch": None,
                    "effective_resource_charges": {name: 0 for name in config["resources"]},
                    "unknown_provider_usage": False, "provider_overrun": False,
                    "outstanding_reservations": [],
                })
            elif event.get("type") == "episode_activate":
                episodes[event["episode_id"]]["activated_epoch"] = event["activated_epoch"]
                if event["episode_id"] not in recovery_episodes:
                    charges["episodes"] += 1
            elif event.get("type") == "episode_close":
                episodes[event["episode_id"]]["closed_epoch"] = event["closed_epoch"]
            elif event.get("type") == "reservation":
                reservations[event["reservation_id"]] = event
                for name, amount in event["amounts"].items():
                    charges[name] += amount
                    if event.get("episode_id") is not None:
                        episodes[event["episode_id"]]["effective_resource_charges"][name] += amount
            elif event.get("type") == "provider_settlement":
                reservation = reservations[event["reservation_id"]]
                settled[event["reservation_id"]] = event["actual_tokens"]
                if event["actual_tokens"] is None:
                    unknown = True
                    if reservation.get("episode_id") is not None:
                        episodes[reservation["episode_id"]]["unknown_provider_usage"] = True
                else:
                    charges["provider_tokens"] += event["actual_tokens"] - reservation["amounts"]["provider_tokens"]
                    if event["actual_tokens"] > reservation["amounts"]["provider_tokens"]:
                        overrun = True
                    if reservation.get("episode_id") is not None:
                        episode = episodes[reservation["episode_id"]]
                        episode["effective_resource_charges"]["provider_tokens"] += (
                            event["actual_tokens"] - reservation["amounts"]["provider_tokens"]
                        )
                        if event["actual_tokens"] > reservation["amounts"]["provider_tokens"]:
                            episode["provider_overrun"] = True
            elif event.get("type") == "capture_recovery_reservation":
                recovery_episodes.add(event["episode_id"])
                capture_recovery["total_attempts"] += 1
                if capture_recovery["first_composite_evidence"] is None:
                    capture_recovery["before_first_composite"] += 1
                for name, amount in event["amounts"].items():
                    charges[name] += amount
                    episodes[event["episode_id"]]["effective_resource_charges"][name] += amount
            elif event.get("type") == "capture_first_composite":
                capture_recovery["first_composite_evidence"] = {
                    "path": event["evidence_path"], "sha256": event["evidence_sha256"]}
        outstanding = sorted(
            reservation_id for reservation_id, event in reservations.items()
            if "provider_requests" in event["amounts"] and reservation_id not in settled
        )
        for reservation_id in outstanding:
            reservation = reservations[reservation_id]
            if reservation.get("episode_id") is not None:
                episodes[reservation["episode_id"]]["outstanding_reservations"].append(reservation_id)
        return {"charges": charges, "reservations": reservations, "settled": settled,
                "unknown_provider_usage": unknown, "provider_overrun": overrun,
                "outstanding": outstanding, "episodes": episodes,
                "capture_recovery": capture_recovery,
                "capture_recovery_episodes": recovery_episodes}

    @staticmethod
    def _append(handle, events: list[dict[str, Any]]) -> None:
        handle.seek(0, os.SEEK_END)
        for event in events:
            handle.write(json.dumps(event, sort_keys=True, separators=(",", ":")) + "\n")
        handle.flush()
        os.fsync(handle.fileno())

    def register_episode(self, episode_id: str, *, action_seconds: int, settlement_seconds: int,
                         provider_token_limit: int, per_call_token_reservation: int,
                         payload_task_limit: int) -> None:
        config, config_sha256 = self._config()
        episode_id = _slug(episode_id, "episode_id")
        values = {
            "action_seconds": _positive_int(action_seconds, "action_seconds"),
            "settlement_seconds": _positive_int(settlement_seconds, "settlement_seconds"),
            "provider_token_limit": _positive_int(provider_token_limit, "provider_token_limit"),
            "per_call_token_reservation": _positive_int(per_call_token_reservation, "per_call_token_reservation"),
            "payload_task_limit": _positive_int(payload_task_limit, "payload_task_limit"),
        }
        with self._outer_lock():
            self.ledger_path.parent.mkdir(parents=True, exist_ok=True)
            existed = self.ledger_path.exists()
            with self.ledger_path.open("a+", encoding="utf-8") as handle:
                fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
                events = self._read_events(handle, config, config_sha256)
                if existed and not events:
                    raise ResourceBudgetError("existing resource ledger is empty")
                if any(probe["closed_epoch"] is None for probe in self._service_state(events, config, config_sha256)[1].values()):
                    raise ResourceBudgetExceeded("episode registration overlaps service probe")
                now = float(self._now())
                started = events[0]["started_epoch"] if events else now
                if now >= started + config["calendar_seconds"]:
                    raise ResourceBudgetExceeded("resource budget time window is exhausted")
                if episode_id in self._state(events, config)["episodes"]:
                    raise ResourceBudgetError("episode is already registered")
                appended = []
                if not events:
                    appended.append({"seq": 0, "type": "start", "config_sha256": config_sha256,
                                     "started_epoch": started})
                appended.append({"seq": len(events) + len(appended), "type": "episode_register",
                                 "episode_id": episode_id, **values})
                self._append(handle, appended)

    def activate_episode(self, episode_id: str) -> None:
        config, config_sha256 = self._config()
        episode_id = _slug(episode_id, "episode_id")
        with self._outer_lock():
            if not self.ledger_path.exists():
                raise ResourceBudgetError("resource ledger has not started")
            with self.ledger_path.open("a+", encoding="utf-8") as handle:
                fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
                events = self._read_events(handle, config, config_sha256)
                if not events:
                    raise ResourceBudgetError("existing resource ledger is empty")
                state = self._state(events, config)
                episode = state["episodes"].get(episode_id)
                if episode is None or episode["activated_epoch"] is not None or episode["closed_epoch"] is not None:
                    raise ResourceBudgetError("episode cannot be activated")
                now = float(self._now())
                active_deadline = events[0]["started_epoch"] + config["calendar_seconds"] - config["cleanup_seconds"]
                if now >= active_deadline:
                    raise ResourceBudgetExceeded("resource budget active window is exhausted")
                episode_charge = 0 if episode_id in state["capture_recovery_episodes"] else 1
                if "episodes" not in config["resources"] or state["charges"].get("episodes", 0) + episode_charge > (
                    config["resources"]["episodes"]["limit"] - config["resources"]["episodes"]["cleanup_reserve"]
                ):
                    raise ResourceBudgetExceeded("episode budget is exhausted")
                self._append(handle, [{"seq": len(events), "type": "episode_activate",
                                      "episode_id": episode_id, "activated_epoch": now}])

    def reserve_capture_recovery(
        self, episode_id: str, *, prior_disposition_path: str | Path,
        prior_disposition_sha256: str,
    ) -> None:
        """Charge and bind one approved continuation capture replacement."""
        if self.episode_id is not None or self.service_probe_id is not None:
            raise ResourceBudgetError("capture recovery requires the global ledger")
        config, digest = self._config(); episode_id = _slug(episode_id, "episode_id")
        disposition_path = str(Path(prior_disposition_path).resolve())
        with self._outer_lock():
            if not self.ledger_path.exists():
                raise ResourceBudgetError("capture recovery requires an existing ledger")
            with self.ledger_path.open("a+", encoding="utf-8") as handle:
                fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
                events = self._read_events(handle, config, digest)
                if not events:
                    raise ResourceBudgetError("capture recovery requires an existing ledger")
                amendments, _ = self._service_state(events, config, digest)
                self._check_engineering(amendments)
                event = {"seq": len(events), "type": "capture_recovery_reservation",
                    "episode_id": episode_id, "amendment_id": next(reversed(amendments), None),
                    "amounts": {"episodes": 1, "burned_setup_retries": 1},
                    "reserved_epoch": float(self._now()),
                    "prior_disposition_path": disposition_path,
                    "prior_disposition_sha256": prior_disposition_sha256}
                self._validate_events([*events, event], config, digest)
                self._append(handle, [event])

    def record_first_capture_composite(
        self, episode_id: str, evidence_path: str | Path, evidence_sha256: str,
    ) -> None:
        """Latch the first admitted non-fixture composite for recovery accounting."""
        if self.episode_id is not None or self.service_probe_id is not None:
            raise ResourceBudgetError("first capture composite requires the global ledger")
        config, digest = self._config(); episode_id = _slug(episode_id, "episode_id")
        evidence = str(Path(evidence_path).resolve())
        with self._outer_lock():
            if not self.ledger_path.exists():
                raise ResourceBudgetError("first capture composite requires an existing ledger")
            with self.ledger_path.open("a+", encoding="utf-8") as handle:
                fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
                events = self._read_events(handle, config, digest)
                if not events:
                    raise ResourceBudgetError("first capture composite requires an existing ledger")
                amendments, _ = self._service_state(events, config, digest)
                self._check_engineering(amendments)
                event = {"seq": len(events), "type": "capture_first_composite",
                    "episode_id": episode_id, "amendment_id": next(reversed(amendments), None),
                    "evidence_path": evidence, "evidence_sha256": evidence_sha256,
                    "recorded_epoch": float(self._now())}
                self._validate_events([*events, event], config, digest)
                self._append(handle, [event])

    def close_episode(self, episode_id: str) -> None:
        config, config_sha256 = self._config()
        episode_id = _slug(episode_id, "episode_id")
        with self._outer_lock():
            if not self.ledger_path.exists():
                raise ResourceBudgetError("resource ledger has not started")
            with self.ledger_path.open("a+", encoding="utf-8") as handle:
                fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
                events = self._read_events(handle, config, config_sha256)
                if not events:
                    raise ResourceBudgetError("existing resource ledger is empty")
                episode = self._state(events, config)["episodes"].get(episode_id)
                if episode is None:
                    raise ResourceBudgetError("episode is not registered")
                if episode["closed_epoch"] is not None:
                    return
                self._append(handle, [{"seq": len(events), "type": "episode_close",
                                      "episode_id": episode_id, "closed_epoch": float(self._now())}])

    def reserve(self, amounts: dict[str, int], operation: str, cleanup: bool | None = None) -> str:
        config, config_sha256 = self._config()
        operation = _slug(operation, "operation")
        if cleanup is None:
            cleanup = self.default_cleanup
        if type(cleanup) is not bool:
            raise ResourceBudgetError("cleanup must be Boolean")
        if cleanup and self.episode_id is not None:
            raise ResourceBudgetError("cleanup reservations cannot select an episode")
        amounts = self._validate_amounts(amounts, config)
        if cleanup and any(config["resources"][name]["cleanup_reserve"] == 0 for name in amounts):
            raise ResourceBudgetExceeded("resource has no cleanup reserve")
        for name, amount in amounts.items():
            cap = config["resources"][name]["limit"]
            if not cleanup:
                cap -= config["resources"][name]["cleanup_reserve"]
            if amount > cap:
                raise ResourceBudgetExceeded(f"resource budget exceeded: {name}")
        with self._outer_lock():
            if self.episode_id is not None and not self.ledger_path.exists():
                raise ResourceBudgetExceeded("episode is not registered")
            self.ledger_path.parent.mkdir(parents=True, exist_ok=True)
            existed = self.ledger_path.exists()
            with self.ledger_path.open("a+", encoding="utf-8") as handle:
                fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
                events = self._read_events(handle, config, config_sha256)
                if existed and not events:
                    raise ResourceBudgetError("existing resource ledger is empty")
                now = float(self._now())
                started = events[0]["started_epoch"] if events else now
                active_deadline = started + config["calendar_seconds"] - config["cleanup_seconds"]
                total_deadline = started + config["calendar_seconds"]
                state = self._state(events, config)
                amendments, probes = self._service_state(events, config, config_sha256)
                self._check_engineering(amendments, cleanup=cleanup)
                active_probes = [key for key, probe in probes.items() if probe["closed_epoch"] is None]
                if active_probes and self.service_probe_id not in active_probes:
                    raise ResourceBudgetExceeded("unscoped request during service probe")
                if self.service_probe_id is not None:
                    probe = probes.get(self.service_probe_id)
                    if probe is None or probe["closed_epoch"] is not None or set(amounts) != {"control_requests"}:
                        raise ResourceBudgetExceeded("service probe permits only scoped control requests")
                    key = "cleanup_control_requests" if cleanup else "active_control_requests"
                    deadline_key = "total_deadline_epoch" if cleanup else "active_deadline_epoch"
                    charge = (sum(p[key] for p in probes.values() if p["amendment_id"] == probe["amendment_id"])
                              if probe.get("continuation") else probe[key])
                    if now >= probe[deadline_key] or charge + amounts["control_requests"] > probe["limits"][key]:
                        raise ResourceBudgetExceeded("service probe limit or deadline exhausted")
                    if "stages" in probe:
                        stages = probe["stages"]
                        if (not stages or stages[-1]["closed_epoch"] is not None
                                or now >= stages[-1][deadline_key]
                                or (not cleanup and stages[-1].get("active_ended_epoch") is not None)
                                or (probe.get("continuation") and cleanup
                                    and stages[-1].get("active_ended_epoch") is None)):
                            raise ResourceBudgetExceeded("diagnostic stage is unavailable or expired")
                episode_control = False
                if self.episode_id is not None:
                    episode = state["episodes"].get(self.episode_id)
                    if episode is None:
                        raise ResourceBudgetExceeded("episode is not registered")
                    if episode["closed_epoch"] is not None:
                        raise ResourceBudgetExceeded("episode is closed")
                    episode_control = set(amounts) == {"control_requests"}
                    activated = episode["activated_epoch"]
                    if episode_control:
                        if activated is not None and now >= (
                            activated + episode["action_seconds"] + episode["settlement_seconds"]
                        ):
                            raise ResourceBudgetExceeded("episode settlement window is exhausted")
                    elif set(amounts) == {"payload_task_attempts"}:
                        if activated is None:
                            raise ResourceBudgetExceeded("episode is pending")
                        if now >= activated + episode["action_seconds"]:
                            raise ResourceBudgetExceeded("episode action window is exhausted")
                        if episode["effective_resource_charges"].get("payload_task_attempts", 0) \
                                + amounts["payload_task_attempts"] > episode["payload_task_limit"]:
                            raise ResourceBudgetExceeded("episode task limit is exhausted")
                    elif set(amounts) == {"provider_requests", "provider_tokens"}:
                        if activated is None:
                            raise ResourceBudgetExceeded("episode is pending")
                        if now >= activated + episode["action_seconds"]:
                            raise ResourceBudgetExceeded("episode action window is exhausted")
                        reserve = episode["per_call_token_reservation"]
                        if amounts["provider_tokens"] != reserve:
                            raise ResourceBudgetExceeded("provider reservation differs from episode binding")
                        effective = episode["effective_resource_charges"].get("provider_tokens", 0)
                        if episode["unknown_provider_usage"] or episode["provider_overrun"] \
                                or effective >= episode["provider_token_limit"] \
                                or effective + reserve > episode["provider_token_limit"] + reserve:
                            raise ResourceBudgetExceeded("episode provider limit is exhausted")
                    else:
                        raise ResourceBudgetExceeded("resource is unavailable in an episode scope")
                if now >= total_deadline or (not cleanup and now >= active_deadline):
                    raise ResourceBudgetExceeded("resource budget time window is exhausted")
                if "provider_requests" in amounts and state["unknown_provider_usage"]:
                    raise ResourceBudgetExceeded("unknown provider usage blocks later provider reservations")
                if "provider_requests" in amounts and state["provider_overrun"]:
                    raise ResourceBudgetExceeded("provider overrun blocks later provider reservations")
                if "payload_task_attempts" in amounts and state["unknown_provider_usage"]:
                    raise ResourceBudgetExceeded("unknown provider usage blocks later payload task reservations")
                if "payload_task_attempts" in amounts and state["provider_overrun"]:
                    raise ResourceBudgetExceeded("provider overrun blocks later payload task reservations")
                for name, amount in amounts.items():
                    cap = config["resources"][name]["limit"]
                    if name == "capture_correction_cycles" and amendments:
                        cap = amendments[next(reversed(amendments))]["limit_changes"][name]["new"]
                    if not cleanup:
                        cap -= config["resources"][name]["cleanup_reserve"]
                    if state["charges"][name] + amount > cap:
                        raise ResourceBudgetExceeded(f"resource budget exceeded: {name}")
                reservation_id = f"r-{uuid4().hex}"
                appended = []
                if not events:
                    appended.append({"seq": 0, "type": "start", "config_sha256": config_sha256,
                                     "started_epoch": started})
                reservation = {"seq": len(events) + len(appended), "type": "reservation",
                               "reservation_id": reservation_id, "operation": operation,
                               "cleanup": cleanup, "amounts": amounts, "reserved_epoch": now}
                if self.episode_id is not None:
                    reservation["episode_id"] = self.episode_id
                if self.service_probe_id is not None:
                    reservation["service_probe_id"] = self.service_probe_id
                appended.append(reservation)
                self._append(handle, appended)
                return reservation_id

    def settle_provider(self, reservation_id: str, actual_tokens: int | None) -> None:
        config, config_sha256 = self._config()
        reservation_id = _slug(reservation_id, "reservation_id")
        if actual_tokens is not None:
            _positive_int(actual_tokens, "actual_tokens", allow_zero=True)
        with self._outer_lock():
            if not self.ledger_path.exists():
                raise ResourceBudgetError("resource ledger has not started")
            with self.ledger_path.open("a+", encoding="utf-8") as handle:
                fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
                events = self._read_events(handle, config, config_sha256)
                state = self._state(events, config)
                reservation = state["reservations"].get(reservation_id)
                if reservation is None or reservation_id in state["settled"]:
                    raise ResourceBudgetError("provider reservation is missing or already settled")
                self._provider_reservation(reservation)
                settlement = {"seq": len(events), "type": "provider_settlement",
                              "reservation_id": reservation_id, "actual_tokens": actual_tokens}
                self._append(handle, [settlement])
                if actual_tokens is not None:
                    current = state["charges"]["provider_tokens"]
                    effective = current - reservation["amounts"]["provider_tokens"] + actual_tokens
                    if reservation.get("episode_id") is not None and \
                            actual_tokens > reservation["amounts"]["provider_tokens"]:
                        raise ResourceBudgetExceeded("provider usage exceeded its episode reservation")
                    if effective > config["resources"]["provider_tokens"]["limit"]:
                        raise ResourceBudgetExceeded("actual provider usage exceeded the total token limit")

    def status(self) -> dict[str, Any]:
        config, config_sha256 = self._config()
        with self._outer_lock():
            if not self.ledger_path.exists():
                state = self._state([], config)
                events = []
            else:
                with self.ledger_path.open("a+", encoding="utf-8") as handle:
                    fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
                    events = self._read_events(handle, config, config_sha256)
                    if not events:
                        raise ResourceBudgetError("existing resource ledger is empty")
                    state = self._state(events, config)
        started = events[0]["started_epoch"] if events else None
        amendments, probes = self._service_state(events, config, config_sha256)
        effective_limits = {name: resource["limit"] for name, resource in config["resources"].items()}
        if amendments:
            effective_limits["capture_correction_cycles"] = amendments[next(reversed(amendments))]["limit_changes"]["capture_correction_cycles"]["new"]
        return {"schema": STATUS_SCHEMA, "frozen_config_sha256": config_sha256,
                "started_epoch": started,
                "active_deadline_epoch": None if started is None else started + config["calendar_seconds"] - config["cleanup_seconds"],
                "total_deadline_epoch": None if started is None else started + config["calendar_seconds"],
                "effective_resource_charges": state["charges"],
                "outstanding_reservations": state["outstanding"],
                "unknown_provider_usage": state["unknown_provider_usage"],
                "provider_overrun": state["provider_overrun"],
                "episodes": state["episodes"],
                "effective_limits": effective_limits,
                "amendments": [{"amendment_id": event["amendment_id"], "approval_path": event["approval_path"],
                                "approval_sha256": event["approval_sha256"]}
                               for event in events if event["type"] == "budget_amendment"],
                "service_probes": probes,
                "capture_recovery": state["capture_recovery"],
                "next_sequence": len(events), "live_authorized": False}


def configured_ledger(now: Callable[[], float] = time.time) -> ResourceLedger | None:
    config, ledger = os.environ.get(_CONFIG_ENV), os.environ.get(_LEDGER_ENV)
    episode_id, phase = os.environ.get(_EPISODE_ENV), os.environ.get(_PHASE_ENV)
    service_id = os.environ.get(_SERVICE_ENV)
    if not config and not ledger and not episode_id and not phase and not service_id:
        return None
    if not config or not ledger:
        raise ResourceBudgetError(f"{_CONFIG_ENV} and {_LEDGER_ENV} must be configured together")
    if phase not in (None, "", "active", "cleanup"):
        raise ResourceBudgetError(f"{_PHASE_ENV} must be active or cleanup")
    cleanup = phase == "cleanup"
    if cleanup and episode_id:
        raise ResourceBudgetError("cleanup configuration cannot select an episode")
    instance = ResourceLedger(config, ledger, now=now, episode_id=episode_id or None, default_cleanup=cleanup,
                              service_probe_id=service_id or None)
    instance._config()
    return instance


def reserve_configured(amounts: dict[str, int], operation: str, cleanup: bool | None = None) -> str | None:
    ledger = configured_ledger()
    return None if ledger is None else ledger.reserve(amounts, operation, cleanup=cleanup)
