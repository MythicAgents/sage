import asyncio
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "ai" / "langgraph"))
import mythic_tools  # noqa: E402
from mythic_tools import _compute_liveness  # noqa: E402


NOW = datetime(2026, 6, 2, 12, 0, 0, tzinfo=timezone.utc)


def _checkin(seconds_ago):
    return (NOW - timedelta(seconds=seconds_ago)).isoformat()


def test_alive_when_recent_checkin_within_threshold():
    result = _compute_liveness(
        display_id=17,
        last_checkin=_checkin(6),
        callback_interval=10,
        callback_jitter=23,
        tasks=[],
        now=NOW,
    )

    assert result["status"] == "alive"
    assert result["alive"] is True


def test_dead_when_gap_exceeds_threshold():
    result = _compute_liveness(
        display_id=17,
        last_checkin=_checkin(18000),
        callback_interval=10,
        callback_jitter=23,
        tasks=[],
        now=NOW,
    )

    assert result["status"] == "dead"
    assert result["alive"] is False
    assert result["seconds_since_checkin"] == 18000
    assert result["threshold_seconds"] == 91.5
    assert result["seconds_since_checkin"] > result["threshold_seconds"]
    assert "no checkin for 18000s" in result["reason"]


def test_three_second_sleep_four_hour_gap_is_dead():
    result = _compute_liveness(
        display_id=13,
        last_checkin=_checkin(4 * 60 * 60),
        callback_interval=3,
        callback_jitter=0,
        tasks=[],
        now=NOW,
    )

    assert result["status"] == "dead"
    assert result["alive"] is False
    assert result["threshold_seconds"] == 45
    assert result["seconds_since_checkin"] == 14400


def test_sage_service_callback_is_taskable_despite_stale_timestamp():
    result = _compute_liveness(
        display_id=3,
        last_checkin=_checkin(6 * 60 * 60),
        callback_interval=3,
        callback_jitter=0,
        tasks=[],
        payload_type="sage",
        active=True,
        now=NOW,
    )

    assert result["status"] == "taskable"
    assert result["alive"] is True
    assert result["liveness_mode"] == "service"
    assert result["seconds_since_checkin"] == 21600
    assert "timestamp advances only when a command is sent" in result["reason"]


def test_inactive_sage_service_callback_is_not_taskable():
    result = _compute_liveness(
        display_id=3,
        last_checkin=_checkin(6),
        callback_interval=3,
        callback_jitter=0,
        tasks=[],
        payload_type="sage",
        active=False,
        now=NOW,
    )

    assert result["status"] == "inactive"
    assert result["alive"] is False


def test_assess_callback_liveness_reads_sage_payload_type(monkeypatch):
    seen = {}

    async def fake_query(client, query, variables=None):
        seen["query"] = query
        return {
            "callback": [{
                "display_id": 3,
                "active": True,
                "last_checkin": _checkin(6 * 60 * 60),
                "payload": {"payloadtype": {"name": "sage"}},
                "c2profileparametersinstances": [],
                "tasks": [],
            }]
        }

    monkeypatch.setattr(mythic_tools.mythic, "execute_custom_query", fake_query)
    result = asyncio.run(mythic_tools.assess_callback_liveness(object(), 3, now=NOW))

    assert "payload { payloadtype { name } }" in seen["query"]
    assert result["status"] == "taskable"
    assert result["alive"] is True


def test_sleep_task_interval_overrides_c2_profile_threshold():
    last_checkin = _checkin(240)
    sleep_task = {
        "command_name": "sleep",
        "original_params": {"interval": 300},
        "status": "completed",
        "completed": True,
        "timestamp": (NOW - timedelta(seconds=239)).isoformat(),
    }

    without_sleep = _compute_liveness(
        display_id=17,
        last_checkin=last_checkin,
        callback_interval=10,
        callback_jitter=23,
        tasks=[],
        now=NOW,
    )
    with_sleep = _compute_liveness(
        display_id=17,
        last_checkin=last_checkin,
        callback_interval=10,
        callback_jitter=23,
        tasks=[sleep_task],
        now=NOW,
    )

    assert without_sleep["status"] == "dead"
    assert without_sleep["threshold_seconds"] == 91.5
    assert with_sleep["sleep_source"] == "sleep_task"
    assert with_sleep["effective_sleep_seconds"] == 300
    assert with_sleep["status"] == "alive"
    assert with_sleep["alive"] is True


def test_zero_interval_uses_180_second_threshold():
    stale = _compute_liveness(
        display_id=17,
        last_checkin=_checkin(200),
        callback_interval=0,
        callback_jitter=23,
        tasks=[],
        now=NOW,
    )
    fresh = _compute_liveness(
        display_id=17,
        last_checkin=_checkin(120),
        callback_interval=0,
        callback_jitter=23,
        tasks=[],
        now=NOW,
    )

    assert stale["threshold_seconds"] == 180
    assert stale["status"] == "dead"
    assert fresh["threshold_seconds"] == 180
    assert fresh["status"] == "alive"


def test_jitter_threshold_math_uses_interval_jitter_and_floor():
    result = _compute_liveness(
        display_id=17,
        last_checkin=_checkin(6),
        callback_interval=10,
        callback_jitter=23,
        tasks=[],
        now=NOW,
    )

    assert result["threshold_seconds"] == 91.5


def test_uncertain_without_usable_last_checkin():
    result = _compute_liveness(
        display_id=17,
        last_checkin=None,
        callback_interval=10,
        callback_jitter=23,
        tasks=[],
        now=NOW,
    )

    assert result["status"] == "uncertain"
    assert result["alive"] is False


def test_queued_since_checkin_counts_only_pending_tasks_after_checkin():
    last_checkin_dt = NOW - timedelta(seconds=60)
    last_checkin = last_checkin_dt.isoformat()
    tasks = [
        {
            "command_name": "whoami",
            "status": "submitted",
            "timestamp": (last_checkin_dt + timedelta(seconds=1)).isoformat(),
        },
        {
            "command_name": "ps",
            "status": "processing",
            "timestamp": (last_checkin_dt + timedelta(seconds=2)).isoformat(),
        },
        {
            "command_name": "pwd",
            "status": "completed",
            "completed": True,
            "timestamp": (last_checkin_dt + timedelta(seconds=3)).isoformat(),
        },
        {
            "command_name": "ls",
            "status": "submitted",
            "timestamp": (last_checkin_dt - timedelta(seconds=1)).isoformat(),
        },
    ]

    result = _compute_liveness(
        display_id=17,
        last_checkin=last_checkin,
        callback_interval=10,
        callback_jitter=23,
        tasks=tasks,
        now=NOW,
    )

    assert result["queued_since_checkin"] == 2


def test_likely_crashed_when_stale_after_completed_task_and_queue():
    last_checkin_dt = NOW - timedelta(seconds=18000)
    last_checkin = last_checkin_dt.isoformat()
    tasks = [
        {
            "command_name": "shell",
            "status": "success",
            "completed": True,
            "timestamp": (last_checkin_dt + timedelta(seconds=2)).isoformat(),
        },
        {
            "command_name": "whoami",
            "status": "submitted",
            "timestamp": (last_checkin_dt + timedelta(seconds=10)).isoformat(),
        },
        {
            "command_name": "ps",
            "status": "processing",
            "timestamp": (last_checkin_dt + timedelta(seconds=20)).isoformat(),
        },
    ]

    result = _compute_liveness(
        display_id=17,
        last_checkin=last_checkin,
        callback_interval=10,
        callback_jitter=23,
        tasks=tasks,
        now=NOW,
    )

    assert result["status"] == "likely_crashed"
    assert result["alive"] is False
    assert result["queued_since_checkin"] == 2
    assert result["suspect_crash_task"] == "shell"


def test_stale_checkin_is_dead_based_on_gap():
    result = _compute_liveness(
        display_id=17,
        last_checkin=_checkin(18000),
        callback_interval=10,
        callback_jitter=23,
        tasks=[],
        now=NOW,
    )

    assert result["status"] == "dead"
    assert result["alive"] is False


def test_fresh_checkin_is_alive_based_on_gap():
    result = _compute_liveness(
        display_id=17,
        last_checkin=_checkin(6),
        callback_interval=10,
        callback_jitter=23,
        tasks=[],
        now=NOW,
    )

    assert result["status"] == "alive"
    assert result["alive"] is True
