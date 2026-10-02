"""Bounded, opt-in retries for completed OpenAI-compatible HTTP responses.

Only a received temporary response can be retried. Transport exceptions and
successful responses remain with the caller, which avoids repeating an
ambiguous model request or a tool action.
"""
from __future__ import annotations

import asyncio
import random
import threading
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
from typing import Any, Callable, Mapping
from uuid import uuid4

import httpx


POLICY_SCHEMA = "sage-provider-retry-policy-v1"
SNAPSHOT_SCHEMA = "sage-provider-retry-state-v1"


@dataclass(frozen=True)
class Policy:
    name: str = "transient-v1"
    max_retries_per_request: int = 3
    max_wait_seconds_per_request: float = 300.0
    max_wait_seconds_per_arm: float = 900.0

    def to_record(self) -> dict[str, Any]:
        return {
            "schema": POLICY_SCHEMA,
            "name": self.name,
            "max_retries_per_request": self.max_retries_per_request,
            "max_wait_seconds_per_request": self.max_wait_seconds_per_request,
            "max_wait_seconds_per_arm": self.max_wait_seconds_per_arm,
        }


_POLICY = Policy()
_SAFE_PROVIDER_CODES = {
    "rate_limit_exceeded", "rate_limit_error", "too_many_requests", "slow_down",
    "server_is_overloaded", "overloaded_error", "service_unavailable",
    "insufficient_quota", "quota_exceeded", "billing_hard_limit_reached",
}


def parse_policy(value: str | Mapping[str, Any] | None) -> Policy | None:
    """Resolve the one supported preset or its exact, frozen contract record."""
    if value is None or value in ("", "off"):
        return None
    if value == _POLICY.name or (
        isinstance(value, Mapping)
        and dict(value) == _POLICY.to_record()
        and all(type(value[key]) is type(expected) for key, expected in _POLICY.to_record().items())
    ):
        return _POLICY
    raise ValueError("SAGE_PROVIDER_RETRY_POLICY must be transient-v1 or its canonical contract record")


def _finite_nonnegative(value: Any) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValueError("provider retry duration must be numeric")
    number = float(value)
    if not (0 <= number < float("inf")):
        raise ValueError("provider retry duration must be finite and nonnegative")
    return number


def _interval_union(intervals: list[tuple[float, float]]) -> float:
    total = 0.0
    last_end = float("-inf")
    for start, end in sorted(intervals):
        if end > max(start, last_end):
            total += end - max(start, last_end)
        last_end = max(last_end, end)
    return total


class RetryState:
    """One arm's retry waits, with recoverable completed and pending intervals."""

    def __init__(
        self,
        policy: Policy,
        event_sink: Callable[[dict[str, Any]], None] | None = None,
        snapshot: Mapping[str, Any] | None = None,
        allow_pending: bool = False,
    ) -> None:
        self.policy = parse_policy(policy.to_record())
        assert self.policy is not None
        self.event_sink = event_sink
        self._lock = threading.RLock()
        self._completed: list[dict[str, Any]] = []
        self._pending: dict[str, dict[str, Any]] = {}
        self._sleep_started_epochs: dict[str, float] = {}
        self._event_records: list[dict[str, Any]] = []
        self._active_deadline_epoch: float | None = None
        self._async_event_sink: Callable[[dict[str, Any]], Any] | None = None
        self._sink_loop: asyncio.AbstractEventLoop | None = None
        if snapshot is not None:
            if not allow_pending and snapshot.get("wait_pending"):
                raise ValueError("provider retry wait unresolved in checkpoint")
            self._restore(snapshot)

    @classmethod
    def from_snapshot(
        cls,
        policy: Policy,
        snapshot: Mapping[str, Any],
        event_sink: Callable[[dict[str, Any]], None] | None = None,
        allow_pending: bool = False,
    ) -> "RetryState":
        return cls(policy, event_sink=event_sink, snapshot=snapshot, allow_pending=allow_pending)

    def _restore(self, record: Mapping[str, Any]) -> None:
        if set(record) != {"schema", "policy", "wait_completed_seconds", "wait_pending", "events"}:
            raise ValueError("provider retry state fields changed")
        if record["schema"] != SNAPSHOT_SCHEMA or record["policy"] != self.policy.to_record():
            raise ValueError("provider retry state policy changed")
        events = record["events"]
        pending = record["wait_pending"]
        if not isinstance(events, list) or not isinstance(pending, list):
            raise ValueError("provider retry state events are malformed")
        completed: list[dict[str, Any]] = []
        active: dict[str, dict[str, Any]] = {}
        ids: set[str] = set()
        for item in events:
            if not isinstance(item, dict) or item.get("event") not in {
                "retry_wait_started", "retry_wait_completed", "provider_response_observed"
            }:
                raise ValueError("provider retry event malformed")
            event_id = item.get("event_id")
            if not isinstance(event_id, str) or len(event_id) != 32 or any(c not in "0123456789abcdef" for c in event_id):
                raise ValueError("provider retry event ID malformed")
            if item["event"] == "provider_response_observed":
                if event_id in ids or set(item) != {
                    "event", "event_id", "request_id", "attempt_index", "status_code",
                    "provider_code", "observed_at_epoch"
                }:
                    raise ValueError("provider response event changed")
                ids.add(event_id)
                if (
                    not isinstance(item["request_id"], str)
                    or len(item["request_id"]) != 32
                    or any(c not in "0123456789abcdef" for c in item["request_id"])
                    or type(item["attempt_index"]) is not int
                    or not 1 <= item["attempt_index"] <= self.policy.max_retries_per_request + 1
                    or type(item["status_code"]) is not int
                    or not 100 <= item["status_code"] <= 599
                    or not isinstance(item["provider_code"], str)
                    or item["provider_code"] not in (_SAFE_PROVIDER_CODES | {""})
                ):
                    raise ValueError("provider response event malformed")
                _finite_nonnegative(item["observed_at_epoch"])
            elif item["event"] == "retry_wait_started":
                if event_id in ids or set(item) != {"event", "event_id", "started_at_epoch", "planned_seconds"}:
                    raise ValueError("provider retry wait start changed")
                ids.add(event_id)
                start = _finite_nonnegative(item["started_at_epoch"])
                planned = _finite_nonnegative(item["planned_seconds"])
                if planned > self.policy.max_wait_seconds_per_request:
                    raise ValueError("provider retry wait exceeded per-request cap")
                active[event_id] = dict(item)
            else:
                if event_id not in active or set(item) != {"event", "event_id", "finished_at_epoch", "actual_seconds"}:
                    raise ValueError("provider retry wait completion changed")
                finish = _finite_nonnegative(item["finished_at_epoch"])
                actual = _finite_nonnegative(item["actual_seconds"])
                start_item = active.pop(event_id)
                if (
                    finish < start_item["started_at_epoch"]
                    or actual > start_item["planned_seconds"] + 1e-5
                    or actual > finish - start_item["started_at_epoch"] + 1.0
                ):
                    raise ValueError("provider retry wait duration inconsistent")
                completed.append({**start_item, **item})
        if len(pending) != len(active):
            raise ValueError("provider retry pending waits changed")
        activated: dict[str, float] = {}
        for item, original in zip(pending, active.values()):
            if not isinstance(item, dict) or set(item) != set(original) | {"sleep_started_at_epoch"}:
                raise ValueError("provider retry pending wait fields changed")
            if {k: v for k, v in item.items() if k != "sleep_started_at_epoch"} != original:
                raise ValueError("provider retry pending waits changed")
            began = item["sleep_started_at_epoch"]
            if began is not None:
                began = _finite_nonnegative(began)
                if began < original["started_at_epoch"]:
                    raise ValueError("provider retry sleep activation precedes intent")
                activated[original["event_id"]] = began
        total = sum(item["actual_seconds"] for item in completed)
        if total > self.policy.max_wait_seconds_per_arm + 1e-6:
            raise ValueError("provider retry arm wait cap exceeded")
        if abs(_finite_nonnegative(record["wait_completed_seconds"]) - total) > 1e-5:
            raise ValueError("provider retry completed wait total changed")
        self._completed = completed
        self._pending = active
        self._sleep_started_epochs = activated
        self._event_records = [dict(item) for item in events]

    def restore(self, snapshot: Mapping[str, Any]) -> None:
        """Replace state from a clean checkpoint before another request is sent."""
        with self._lock:
            if snapshot.get("wait_pending"):
                raise ValueError("provider retry wait unresolved in checkpoint")
            self._restore(snapshot)

    def reset_for_new_request(self) -> None:
        """Reset a reused Chat session in place; transports keep this state object."""
        with self._lock:
            if self._pending:
                raise ValueError("cannot reset an in-flight provider retry")
            self._completed = []
            self._event_records = []
            self._sleep_started_epochs = {}

    def _emit(self, event: dict[str, Any]) -> None:
        self._event_records.append(event)
        if self.event_sink is not None:
            self.event_sink({**event, "provider_retry": self.snapshot()})

    def bind_async_event_sink(
        self, sink: Callable[[dict[str, Any]], Any], loop: asyncio.AbstractEventLoop
    ) -> None:
        self._async_event_sink = sink
        self._sink_loop = loop

    async def _publish_async(self, event: dict[str, Any]) -> None:
        if self._async_event_sink is not None:
            await self._async_event_sink({**event, "provider_retry": self.snapshot()})

    def _publish_sync(self, event: dict[str, Any]) -> None:
        if self._async_event_sink is None:
            return
        loop = self._sink_loop
        if loop is None or not loop.is_running():
            raise RuntimeError("provider retry event sink unavailable")
        try:
            current = asyncio.get_running_loop()
        except RuntimeError:
            current = None
        if current is loop:
            raise RuntimeError("synchronous provider retry cannot publish on its own event loop")
        future = asyncio.run_coroutine_threadsafe(
            self._async_event_sink({**event, "provider_retry": self.snapshot()}), loop
        )
        future.result(timeout=30)

    def snapshot(self) -> dict[str, Any]:
        with self._lock:
            return {
                "schema": SNAPSHOT_SCHEMA,
                "policy": self.policy.to_record(),
                "wait_completed_seconds": sum(item["actual_seconds"] for item in self._completed),
                "wait_pending": [
                    {**item, "sleep_started_at_epoch": self._sleep_started_epochs.get(event_id)}
                    for event_id, item in self._pending.items()
                ],
                "events": list(self._event_records),
            }

    def credited_wait_seconds(self, now_epoch: float | None = None, *, started_epoch: float | None = None) -> float:
        now = time.time() if now_epoch is None else _finite_nonnegative(now_epoch)
        with self._lock:
            intervals = [
                (item["finished_at_epoch"] - item["actual_seconds"], item["finished_at_epoch"])
                for item in self._completed
            ]
            intervals.extend(
                (began, min(now, began + self._pending[event_id]["planned_seconds"]))
                for event_id, began in self._sleep_started_epochs.items()
                if event_id in self._pending and now > began
            )
            if started_epoch is not None:
                started = _finite_nonnegative(started_epoch)
                intervals = [(max(start, started), min(end, now)) for start, end in intervals if end > started and start < now]
            return min(self.policy.max_wait_seconds_per_arm, _interval_union(intervals))

    def completed_wait_seconds(
        self, now_epoch: float | None = None, *, started_epoch: float | None = None
    ) -> float:
        """Final proof credit from completed sleep intervals only."""
        now = time.time() if now_epoch is None else _finite_nonnegative(now_epoch)
        started = 0.0 if started_epoch is None else _finite_nonnegative(started_epoch)
        with self._lock:
            intervals = [
                (max(started, item["finished_at_epoch"] - item["actual_seconds"]),
                 min(now, item["finished_at_epoch"]))
                for item in self._completed
                if item["finished_at_epoch"] > started
                and item["finished_at_epoch"] <= now
                and item["finished_at_epoch"] - item["actual_seconds"] < now
            ]
        return min(self.policy.max_wait_seconds_per_arm, _interval_union(intervals))

    def active_elapsed_seconds(self, started_epoch: float, now_epoch: float | None = None) -> float:
        now = time.time() if now_epoch is None else _finite_nonnegative(now_epoch)
        started = _finite_nonnegative(started_epoch)
        return max(0.0, now - started - self.credited_wait_seconds(now, started_epoch=started))

    def bind_active_deadline(self, deadline_epoch: float) -> None:
        """Bind an original arm deadline; only actual retry waits can extend it."""
        deadline = _finite_nonnegative(deadline_epoch)
        with self._lock:
            if self._active_deadline_epoch is not None and self._active_deadline_epoch != deadline:
                raise ValueError("provider retry arm deadline changed")
            self._active_deadline_epoch = deadline

    def active_deadline_reached(self, *, completed_only: bool = False) -> bool:
        with self._lock:
            deadline = self._active_deadline_epoch
            if deadline is None:
                return False
            now = time.time()
            credit = self.completed_wait_seconds(now) if completed_only else self.credited_wait_seconds(now)
            return now >= deadline + credit

    def _reserve_wait(self, seconds: float) -> str | None:
        with self._lock:
            reserved = sum(item["actual_seconds"] for item in self._completed)
            reserved += sum(item["planned_seconds"] for item in self._pending.values())
            if reserved + seconds > self.policy.max_wait_seconds_per_arm:
                return None
            event_id = uuid4().hex
            item = {
                "event": "retry_wait_started",
                "event_id": event_id,
                "started_at_epoch": time.time(),
                "planned_seconds": seconds,
            }
            self._pending[event_id] = item
            self._emit(item)
            return event_id

    def record_response(self, response: httpx.Response, request_id: str, attempt_index: int) -> None:
        code = ""
        if response.status_code in {429, 503}:
            try:
                data = response.json()
                error = data.get("error") if isinstance(data, dict) else None
                if isinstance(error, dict):
                    for field in ("code", "type"):
                        candidate = str(error.get(field) or "").casefold()
                        if candidate in _SAFE_PROVIDER_CODES:
                            code = candidate
                            break
            except (ValueError, UnicodeDecodeError):
                pass
        with self._lock:
            event = {
                "event": "provider_response_observed",
                "event_id": uuid4().hex,
                "request_id": request_id,
                "attempt_index": attempt_index,
                "status_code": response.status_code,
                "provider_code": code,
                "observed_at_epoch": time.time(),
            }
            self._event_records.append(event)
            if self.event_sink is not None:
                self.event_sink(dict(event))

    def _complete_wait(self, event_id: str, started_monotonic: float) -> None:
        with self._lock:
            start_item = self._pending.pop(event_id)
            self._sleep_started_epochs.pop(event_id, None)
            event = {
                "event": "retry_wait_completed",
                "event_id": event_id,
                "finished_at_epoch": time.time(),
                "actual_seconds": min(
                    max(0.0, time.monotonic() - started_monotonic),
                    start_item["planned_seconds"],
                ),
            }
            self._completed.append({**start_item, **event})
            self._emit(event)

    def _begin_wait(self, event_id: str) -> None:
        with self._lock:
            if event_id not in self._pending:
                raise ValueError("provider retry wait no longer pending")
            if self.event_sink is not None:
                self.event_sink({
                    "event": "retry_wait_activated",
                    "event_id": event_id,
                    "planned_seconds": self._pending[event_id]["planned_seconds"],
                })
            self._sleep_started_epochs[event_id] = time.time()

    def _publish_activation(self, event_id: str) -> Any:
        if self._async_event_sink is None:
            return
        loop = self._sink_loop
        if loop is None or not loop.is_running():
            raise RuntimeError("provider retry event sink unavailable")
        event = {
            "event": "retry_wait_activated",
            "event_id": event_id,
            "sleep_started_at_epoch": self._sleep_started_epochs[event_id],
        }
        try:
            current = asyncio.get_running_loop()
        except RuntimeError:
            current = None
        if current is loop:
            return loop.create_task(self._publish_async(event))
        return asyncio.run_coroutine_threadsafe(self._publish_async(event), loop)


_TEMP_CODES = {"rate_limit_exceeded", "rate_limit_error", "too_many_requests", "slow_down", "server_is_overloaded", "overloaded_error", "service_unavailable"}
_PERMANENT_CODES = {"insufficient_quota", "quota_exceeded", "billing_hard_limit_reached", "invalid_api_key", "invalid_request_error"}


def retryable_response(response: httpx.Response) -> bool:
    if response.status_code not in {429, 503}:
        return False
    try:
        data = response.json()
    except (ValueError, UnicodeDecodeError):
        return response.status_code == 503
    if not isinstance(data, dict) or not isinstance(data.get("error"), dict):
        return response.status_code == 503
    error = data["error"]
    codes = {str(error.get(key) or "").casefold() for key in ("code", "type")}
    if codes & _PERMANENT_CODES:
        return False
    if response.status_code == 503:
        return True
    if codes & _TEMP_CODES:
        return True
    # Some OpenAI-compatible routes omit a code. A bare status is ambiguous.
    return False


def retry_delay_seconds(response: httpx.Response, retry_number: int) -> float | None:
    """Return provider delay or jittered backoff; invalid Retry-After fails closed."""
    header = response.headers.get("retry-after")
    if header is not None:
        try:
            value = float(header)
        except ValueError:
            try:
                date = parsedate_to_datetime(header)
                if date.tzinfo is None:
                    date = date.replace(tzinfo=timezone.utc)
                value = (date - datetime.now(timezone.utc)).total_seconds()
            except (TypeError, ValueError, OverflowError):
                return None
        if not (0 <= value < float("inf")):
            return None
        return value
    return min(60.0, 2.0 ** max(0, retry_number - 1)) * random.uniform(0.75, 1.0)


class RetryingHTTPTransport(httpx.BaseTransport):
    def __init__(self, transport: httpx.BaseTransport, state: RetryState) -> None:
        self.transport = transport
        self.state = state

    def handle_request(self, request: httpx.Request) -> httpx.Response:
        request.read()
        total_wait = 0.0
        request_id = uuid4().hex
        last_response: httpx.Response | None = None
        for retry_number in range(self.state.policy.max_retries_per_request + 1):
            if self.state.active_deadline_reached():
                if last_response is not None:
                    return last_response
                raise TimeoutError("provider retry arm active deadline reached")
            response = self.transport.handle_request(request)
            last_response = response
            if response.status_code in {429, 503}:
                response.read()
            self.state.record_response(response, request_id, retry_number + 1)
            if retry_number == self.state.policy.max_retries_per_request or not retryable_response(response):
                return response
            if self.state.active_deadline_reached():
                return response
            delay = retry_delay_seconds(response, retry_number + 1)
            if delay is None or total_wait + delay > self.state.policy.max_wait_seconds_per_request:
                return response
            event_id = self.state._reserve_wait(delay)
            if event_id is None:
                return response
            start_event = self.state._pending[event_id]
            try:
                self.state._publish_sync(start_event)
            except BaseException:
                self.state._complete_wait(event_id, time.monotonic())
                raise
            if self.state.active_deadline_reached(completed_only=True):
                self.state._complete_wait(event_id, time.monotonic())
                self.state._publish_sync(self.state._event_records[-1])
                return response
            response.close()
            started = time.monotonic()
            activation = None
            try:
                self.state._begin_wait(event_id)
                activation = self.state._publish_activation(event_id)
                time.sleep(delay)
            finally:
                self.state._complete_wait(event_id, started)
                if activation is not None:
                    activation.result(timeout=30)
                self.state._publish_sync(self.state._event_records[-1])
            total_wait += delay
        raise AssertionError("unreachable retry loop")

    def close(self) -> None:
        self.transport.close()


class RetryingAsyncHTTPTransport(httpx.AsyncBaseTransport):
    def __init__(self, transport: httpx.AsyncBaseTransport, state: RetryState) -> None:
        self.transport = transport
        self.state = state

    async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
        await request.aread()
        total_wait = 0.0
        request_id = uuid4().hex
        last_response: httpx.Response | None = None
        for retry_number in range(self.state.policy.max_retries_per_request + 1):
            if self.state.active_deadline_reached():
                if last_response is not None:
                    return last_response
                raise TimeoutError("provider retry arm active deadline reached")
            response = await self.transport.handle_async_request(request)
            last_response = response
            if response.status_code in {429, 503}:
                await response.aread()
            self.state.record_response(response, request_id, retry_number + 1)
            if retry_number == self.state.policy.max_retries_per_request or not retryable_response(response):
                return response
            if self.state.active_deadline_reached():
                return response
            delay = retry_delay_seconds(response, retry_number + 1)
            if delay is None or total_wait + delay > self.state.policy.max_wait_seconds_per_request:
                return response
            event_id = self.state._reserve_wait(delay)
            if event_id is None:
                return response
            start_event = self.state._pending[event_id]
            try:
                await self.state._publish_async(start_event)
            except BaseException:
                self.state._complete_wait(event_id, time.monotonic())
                raise
            if self.state.active_deadline_reached(completed_only=True):
                self.state._complete_wait(event_id, time.monotonic())
                await self.state._publish_async(self.state._event_records[-1])
                return response
            await response.aclose()
            started = time.monotonic()
            activation = None
            try:
                self.state._begin_wait(event_id)
                activation = self.state._publish_activation(event_id)
                await asyncio.sleep(delay)
            finally:
                self.state._complete_wait(event_id, started)
                if activation is not None:
                    await activation
                await self.state._publish_async(self.state._event_records[-1])
            total_wait += delay
        raise AssertionError("unreachable retry loop")

    async def aclose(self) -> None:
        await self.transport.aclose()
