import json
import multiprocessing
from pathlib import Path

import pytest

from ai import resource_budget


def _write_config(path, *, limit=10, cleanup=2, calendar=100, cleanup_seconds=10, provider=False):
    resources = {"control_requests": {"limit": limit, "cleanup_reserve": cleanup}}
    if provider:
        resources.update({
            "provider_requests": {"limit": 10, "cleanup_reserve": 0},
            "provider_tokens": {"limit": 100, "cleanup_reserve": 0},
        })
    path.write_text(json.dumps({"schema": resource_budget.CONFIG_SCHEMA, "resources": resources,
                                "calendar_seconds": calendar, "cleanup_seconds": cleanup_seconds}))


def _race(config, ledger, gate, results):
    gate.wait()
    try:
        resource_budget.ResourceLedger(config, ledger).reserve({"control_requests": 1}, "race")
        results.put("reserved")
    except resource_budget.ResourceBudgetExceeded:
        results.put("denied")


def _paused_first_reserve(config, ledger, entered, release, results):
    original, count = resource_budget.fcntl.flock, 0
    def delayed(descriptor, operation):
        nonlocal count
        count += 1
        if count == 2:
            entered.set()
            assert release.wait(5)
        return original(descriptor, operation)
    resource_budget.fcntl.flock = delayed
    try:
        resource_budget.ResourceLedger(config, ledger).reserve({"control_requests": 1}, "paused-first")
        results.put("reserved")
    except Exception as exc:
        results.put(type(exc).__name__)


def _ordinary_reserve(config, ledger, done, results):
    try:
        resource_budget.ResourceLedger(config, ledger).reserve({"control_requests": 1}, "second")
        results.put("reserved")
    except Exception as exc:
        results.put(type(exc).__name__)
    finally:
        done.set()


def _ordinary_status(config, ledger, done, results):
    try:
        results.put(resource_budget.ResourceLedger(config, ledger).status()["effective_resource_charges"])
    except Exception as exc:
        results.put(type(exc).__name__)
    finally:
        done.set()


def _episode_task(config, ledger, gate, results):
    gate.wait()
    try:
        resource_budget.ResourceLedger(config, ledger, episode_id="ep").reserve(
            {"payload_task_attempts": 1}, "episode-task"
        )
        results.put("reserved")
    except resource_budget.ResourceBudgetExceeded:
        results.put("denied")


def _episode_provider(config, ledger, gate, results):
    gate.wait()
    try:
        value = resource_budget.ResourceLedger(config, ledger, episode_id="ep").reserve(
            {"provider_requests": 1, "provider_tokens": 40}, "episode-provider"
        )
        results.put(value)
    except resource_budget.ResourceBudgetExceeded:
        results.put("denied")


def _write_episode_config(path, *, episodes=4, calendar=100, cleanup_seconds=10):
    resources = {
        "episodes": {"limit": episodes, "cleanup_reserve": 0},
        "control_requests": {"limit": 20, "cleanup_reserve": 4},
        "provider_requests": {"limit": 20, "cleanup_reserve": 0},
        "provider_tokens": {"limit": 1000, "cleanup_reserve": 0},
        "payload_task_attempts": {"limit": 20, "cleanup_reserve": 0},
    }
    path.write_text(json.dumps({"schema": resource_budget.CONFIG_SCHEMA, "resources": resources,
                                "calendar_seconds": calendar, "cleanup_seconds": cleanup_seconds}))


def _register_episode(budget, episode_id="ep", *, action=20, settlement=10, token_limit=100, reserve=40, tasks=2):
    budget.register_episode(episode_id, action_seconds=action, settlement_seconds=settlement,
                            provider_token_limit=token_limit, per_call_token_reservation=reserve,
                            payload_task_limit=tasks)


def test_cross_process_reservation_race_cannot_exceed_limit(tmp_path):
    config, ledger = tmp_path / "config.json", tmp_path / "ledger.jsonl"
    _write_config(config, limit=3, cleanup=0)
    context = multiprocessing.get_context("fork")
    gate, results = context.Event(), context.Queue()
    processes = [context.Process(target=_race, args=(config, ledger, gate, results)) for _ in range(8)]
    for process in processes:
        process.start()
    gate.set()
    outcomes = [results.get(timeout=10) for _ in processes]
    for process in processes:
        process.join(10)
        assert process.exitcode == 0
    assert outcomes.count("reserved") == 3
    assert outcomes.count("denied") == 5
    assert resource_budget.ResourceLedger(config, ledger).status()["effective_resource_charges"] == {
        "control_requests": 3
    }


def test_first_use_sidecar_lock_hides_transient_empty_ledger_from_reserve_and_status(tmp_path):
    config, ledger = tmp_path / "config.json", tmp_path / "ledger.jsonl"
    _write_config(config, limit=3, cleanup=0)
    context = multiprocessing.get_context("fork")

    entered, release, second_done, results = context.Event(), context.Event(), context.Event(), context.Queue()
    first = context.Process(target=_paused_first_reserve, args=(config, ledger, entered, release, results))
    first.start(); assert entered.wait(3)
    assert ledger.exists() and ledger.stat().st_size == 0
    second = context.Process(target=_ordinary_reserve, args=(config, ledger, second_done, results))
    second.start(); assert second_done.wait(0.2) is False
    release.set(); first.join(5); second.join(5)
    assert first.exitcode == second.exitcode == 0
    assert sorted([results.get(timeout=2), results.get(timeout=2)]) == ["reserved", "reserved"]

    entered, release, status_done, results = context.Event(), context.Event(), context.Event(), context.Queue()
    ledger2 = tmp_path / "ledger-status.jsonl"
    first = context.Process(target=_paused_first_reserve, args=(config, ledger2, entered, release, results))
    first.start(); assert entered.wait(3)
    reader = context.Process(target=_ordinary_status, args=(config, ledger2, status_done, results))
    reader.start(); assert status_done.wait(0.2) is False
    release.set(); first.join(5); reader.join(5)
    observed = [results.get(timeout=2), results.get(timeout=2)]
    assert "reserved" in observed
    assert {"control_requests": 1} in observed


def test_genuinely_preexisting_empty_ledger_remains_invalid(tmp_path):
    config, ledger = tmp_path / "config.json", tmp_path / "ledger.jsonl"
    _write_config(config)
    ledger.write_text("")
    with pytest.raises(resource_budget.ResourceBudgetError, match="empty"):
        resource_budget.ResourceLedger(config, ledger).status()


def test_exact_caps_cleanup_reserve_and_time_windows(tmp_path):
    config, ledger = tmp_path / "config.json", tmp_path / "ledger.jsonl"
    _write_config(config, limit=10, cleanup=3)
    clock = [0]
    budget = resource_budget.ResourceLedger(config, ledger, now=lambda: clock[0])
    budget.reserve({"control_requests": 7}, "active")
    with pytest.raises(resource_budget.ResourceBudgetExceeded):
        budget.reserve({"control_requests": 1}, "active")
    budget.reserve({"control_requests": 3}, "cleanup", cleanup=True)
    with pytest.raises(resource_budget.ResourceBudgetExceeded):
        budget.reserve({"control_requests": 1}, "cleanup", cleanup=True)

    config2, ledger2 = tmp_path / "config2.json", tmp_path / "ledger2.jsonl"
    _write_config(config2, calendar=100, cleanup_seconds=10, limit=10, cleanup=2)
    clock[0] = 0
    budget = resource_budget.ResourceLedger(config2, ledger2, now=lambda: clock[0])
    budget.reserve({"control_requests": 1}, "first")
    clock[0] = 90
    with pytest.raises(resource_budget.ResourceBudgetExceeded):
        budget.reserve({"control_requests": 1}, "late-active")
    budget.reserve({"control_requests": 1}, "within-cleanup", cleanup=True)
    clock[0] = 100
    with pytest.raises(resource_budget.ResourceBudgetExceeded):
        budget.reserve({"control_requests": 1}, "late-cleanup", cleanup=True)


def test_first_denied_reservation_does_not_create_or_start_the_ledger(tmp_path):
    config, ledger = tmp_path / "config.json", tmp_path / "ledger.jsonl"
    _write_config(config, limit=10, cleanup=3)
    budget = resource_budget.ResourceLedger(config, ledger)
    with pytest.raises(resource_budget.ResourceBudgetExceeded):
        budget.reserve({"control_requests": 8}, "first-over-cap")
    assert not ledger.exists()
    budget.reserve({"control_requests": 7}, "first-legal")
    assert budget.status()["effective_resource_charges"] == {"control_requests": 7}


def test_config_drift_truncation_and_counters_fail_closed_across_instances(tmp_path):
    config, ledger = tmp_path / "config.json", tmp_path / "ledger.jsonl"
    _write_config(config)
    resource_budget.ResourceLedger(config, ledger).reserve({"control_requests": 4}, "first")
    assert resource_budget.ResourceLedger(config, ledger).status()["effective_resource_charges"]["control_requests"] == 4
    _write_config(config, limit=11)
    with pytest.raises(resource_budget.ResourceBudgetError, match="drifted"):
        resource_budget.ResourceLedger(config, ledger).reserve({"control_requests": 1}, "second")

    config2, ledger2 = tmp_path / "config2.json", tmp_path / "ledger2.jsonl"
    _write_config(config2)
    resource_budget.ResourceLedger(config2, ledger2).reserve({"control_requests": 1}, "first")
    with ledger2.open("a") as handle:
        handle.write("{")
    with pytest.raises(resource_budget.ResourceBudgetError, match="truncated"):
        resource_budget.ResourceLedger(config2, ledger2).reserve({"control_requests": 1}, "second")


def test_provider_known_unknown_overage_and_double_settlement_are_durable(tmp_path):
    config, ledger = tmp_path / "config.json", tmp_path / "ledger.jsonl"
    _write_config(config, limit=10, cleanup=2, provider=True)
    budget = resource_budget.ResourceLedger(config, ledger)
    known = budget.reserve({"provider_requests": 1, "provider_tokens": 50}, "provider-known")
    budget.settle_provider(known, 20)
    status = resource_budget.ResourceLedger(config, ledger).status()
    assert status["effective_resource_charges"]["provider_requests"] == 1
    assert status["effective_resource_charges"]["provider_tokens"] == 20
    with pytest.raises(resource_budget.ResourceBudgetError, match="already settled"):
        budget.settle_provider(known, 20)

    unknown = budget.reserve({"provider_requests": 1, "provider_tokens": 40}, "provider-unknown")
    budget.settle_provider(unknown, None)
    assert budget.status()["effective_resource_charges"]["provider_tokens"] == 60
    with pytest.raises(resource_budget.ResourceBudgetExceeded, match="unknown provider"):
        budget.reserve({"provider_requests": 1, "provider_tokens": 1}, "blocked-provider")
    budget.reserve({"control_requests": 1}, "cleanup-control", cleanup=True)

    config2, ledger2 = tmp_path / "config2.json", tmp_path / "ledger2.jsonl"
    _write_config(config2, limit=10, cleanup=2, provider=True)
    over = resource_budget.ResourceLedger(config2, ledger2).reserve(
        {"provider_requests": 1, "provider_tokens": 80}, "provider-overage"
    )
    with pytest.raises(resource_budget.ResourceBudgetExceeded, match="total token"):
        resource_budget.ResourceLedger(config2, ledger2).settle_provider(over, 150)
    assert resource_budget.ResourceLedger(config2, ledger2).status()["effective_resource_charges"]["provider_tokens"] == 150


def test_configured_helpers_are_noop_only_when_both_variables_are_absent(tmp_path, monkeypatch):
    monkeypatch.delenv(resource_budget._CONFIG_ENV, raising=False)
    monkeypatch.delenv(resource_budget._LEDGER_ENV, raising=False)
    assert resource_budget.configured_ledger() is None
    assert resource_budget.reserve_configured({"control_requests": 1}, "absent") is None
    monkeypatch.setenv(resource_budget._CONFIG_ENV, str(tmp_path / "missing.json"))
    with pytest.raises(resource_budget.ResourceBudgetError, match="configured together"):
        resource_budget.configured_ledger()
    monkeypatch.setenv(resource_budget._LEDGER_ENV, str(tmp_path / "ledger.jsonl"))
    with pytest.raises(resource_budget.ResourceBudgetError, match="cannot be read"):
        resource_budget.configured_ledger()
    malformed = tmp_path / "config.json"
    malformed.write_text("{}")
    monkeypatch.setenv(resource_budget._CONFIG_ENV, str(malformed))
    with pytest.raises(resource_budget.ResourceBudgetError, match="schema"):
        resource_budget.configured_ledger()


def test_episode_pending_activation_close_and_late_settlement_are_durable(tmp_path):
    config, ledger = tmp_path / "config.json", tmp_path / "ledger.jsonl"
    _write_episode_config(config)
    clock = [0.0]
    operator = resource_budget.ResourceLedger(config, ledger, now=lambda: clock[0])
    _register_episode(operator)
    scoped = resource_budget.ResourceLedger(config, ledger, now=lambda: clock[0], episode_id="ep")
    scoped.reserve({"control_requests": 1}, "startup")
    with pytest.raises(resource_budget.ResourceBudgetExceeded, match="pending"):
        scoped.reserve({"payload_task_attempts": 1}, "premature-task")
    with pytest.raises(resource_budget.ResourceBudgetExceeded, match="pending"):
        scoped.reserve({"provider_requests": 1, "provider_tokens": 40}, "premature-provider")
    operator.activate_episode("ep")
    with pytest.raises(resource_budget.ResourceBudgetError):
        operator.activate_episode("ep")
    reservation = scoped.reserve({"provider_requests": 1, "provider_tokens": 40}, "provider")
    assert scoped.reserve({"payload_task_attempts": 2}, "tasks")
    operator.close_episode("ep"); operator.close_episode("ep")
    next_sequence = operator.status()["next_sequence"]
    clock[0] = 1000
    scoped.settle_provider(reservation, 20)
    status = operator.status()
    assert status["next_sequence"] == next_sequence + 1
    assert status["effective_resource_charges"]["episodes"] == 1
    assert status["episodes"]["ep"]["activated_epoch"] == 0.0
    assert status["episodes"]["ep"]["closed_epoch"] == 0.0
    assert status["episodes"]["ep"]["effective_resource_charges"]["provider_tokens"] == 20
    with pytest.raises(resource_budget.ResourceBudgetExceeded, match="closed"):
        scoped.reserve({"control_requests": 1}, "after-close")


def test_episode_concurrent_task_and_provider_caps_share_global_lock(tmp_path):
    config, ledger = tmp_path / "config.json", tmp_path / "ledger.jsonl"
    _write_episode_config(config)
    operator = resource_budget.ResourceLedger(config, ledger); _register_episode(operator, token_limit=50, tasks=2)
    operator.activate_episode("ep")
    context = multiprocessing.get_context("fork")
    gate, results = context.Event(), context.Queue()
    tasks = [context.Process(target=_episode_task, args=(config, ledger, gate, results)) for _ in range(4)]
    for process in tasks: process.start()
    gate.set(); outcomes = [results.get(timeout=5) for _ in tasks]
    for process in tasks: process.join(5); assert process.exitcode == 0
    assert outcomes.count("reserved") == 2 and outcomes.count("denied") == 2

    gate, results = context.Event(), context.Queue()
    providers = [context.Process(target=_episode_provider, args=(config, ledger, gate, results)) for _ in range(3)]
    for process in providers: process.start()
    gate.set(); outcomes = [results.get(timeout=5) for _ in providers]
    for process in providers: process.join(5); assert process.exitcode == 0
    assert outcomes.count("denied") == 1 and len(set(outcomes) - {"denied"}) == 2
    assert operator.status()["episodes"]["ep"]["effective_resource_charges"]["provider_tokens"] == 80


def test_episode_provider_unknown_and_per_call_overage_block_later_calls(tmp_path):
    config, ledger = tmp_path / "config.json", tmp_path / "ledger.jsonl"
    _write_episode_config(config)
    operator = resource_budget.ResourceLedger(config, ledger); _register_episode(operator); operator.activate_episode("ep")
    scoped = resource_budget.ResourceLedger(config, ledger, episode_id="ep")
    with pytest.raises(resource_budget.ResourceBudgetExceeded, match="binding"):
        scoped.reserve({"provider_requests": 1, "provider_tokens": 39}, "wrong-reserve")
    first = scoped.reserve({"provider_requests": 1, "provider_tokens": 40}, "overage")
    with pytest.raises(resource_budget.ResourceBudgetExceeded, match="episode reservation"):
        scoped.settle_provider(first, 41)
    assert scoped.status()["episodes"]["ep"]["provider_overrun"] is True
    assert scoped.status()["provider_overrun"] is True
    with pytest.raises(resource_budget.ResourceBudgetExceeded, match="provider limit"):
        scoped.reserve({"provider_requests": 1, "provider_tokens": 40}, "after-overage")
    with pytest.raises(resource_budget.ResourceBudgetExceeded, match="provider overrun"):
        resource_budget.ResourceLedger(config, ledger).reserve(
            {"provider_requests": 1, "provider_tokens": 40}, "global-after-overage"
        )
    with pytest.raises(resource_budget.ResourceBudgetExceeded, match="payload task"):
        resource_budget.ResourceLedger(config, ledger).reserve(
            {"payload_task_attempts": 1}, "task-after-overage"
        )
    assert resource_budget.ResourceLedger(config, ledger).reserve(
        {"control_requests": 1}, "observe-overage"
    )

    config2, ledger2 = tmp_path / "config2.json", tmp_path / "ledger2.jsonl"
    _write_episode_config(config2)
    operator = resource_budget.ResourceLedger(config2, ledger2); _register_episode(operator); operator.activate_episode("ep")
    scoped = resource_budget.ResourceLedger(config2, ledger2, episode_id="ep")
    unknown = scoped.reserve({"provider_requests": 1, "provider_tokens": 40}, "unknown")
    scoped.settle_provider(unknown, None)
    assert scoped.status()["episodes"]["ep"]["effective_resource_charges"]["provider_tokens"] == 40
    with pytest.raises(resource_budget.ResourceBudgetExceeded, match="provider limit"):
        scoped.reserve({"provider_requests": 1, "provider_tokens": 40}, "after-unknown")
    with pytest.raises(resource_budget.ResourceBudgetExceeded, match="payload task"):
        scoped.reserve({"payload_task_attempts": 1}, "task-after-unknown")
    assert resource_budget.ResourceLedger(config2, ledger2, default_cleanup=True).reserve(
        {"control_requests": 1}, "cleanup-after-unknown"
    )


def test_episode_exact_action_settlement_and_global_activation_deadlines(tmp_path):
    config, ledger = tmp_path / "config.json", tmp_path / "ledger.jsonl"
    _write_episode_config(config, calendar=100, cleanup_seconds=10)
    clock = [0.0]
    operator = resource_budget.ResourceLedger(config, ledger, now=lambda: clock[0]); _register_episode(operator, action=5, settlement=5)
    clock[0] = 2.0; operator.activate_episode("ep")
    scoped = resource_budget.ResourceLedger(config, ledger, now=lambda: clock[0], episode_id="ep")
    clock[0] = 6.999
    assert scoped.reserve({"payload_task_attempts": 1}, "last-task")
    clock[0] = 7.0
    with pytest.raises(resource_budget.ResourceBudgetExceeded, match="action window"):
        scoped.reserve({"provider_requests": 1, "provider_tokens": 40}, "at-action-deadline")
    clock[0] = 11.999
    assert scoped.reserve({"control_requests": 1}, "last-settlement-control")
    clock[0] = 12.0
    with pytest.raises(resource_budget.ResourceBudgetExceeded, match="settlement window"):
        scoped.reserve({"control_requests": 1}, "at-settlement-deadline")

    _register_episode(operator, "ep2", action=1, settlement=1, token_limit=1, reserve=1, tasks=1)
    clock[0] = 90.0
    with pytest.raises(resource_budget.ResourceBudgetExceeded, match="active window"):
        operator.activate_episode("ep2")

    config2, ledger2 = tmp_path / "config-global.json", tmp_path / "ledger-global.jsonl"
    _write_episode_config(config2, calendar=100, cleanup_seconds=10)
    clock[0] = 0.0
    operator = resource_budget.ResourceLedger(config2, ledger2, now=lambda: clock[0])
    _register_episode(operator, action=10, settlement=100); operator.activate_episode("ep")
    scoped = resource_budget.ResourceLedger(config2, ledger2, now=lambda: clock[0], episode_id="ep")
    clock[0] = 90.0
    with pytest.raises(resource_budget.ResourceBudgetExceeded, match="time window"):
        scoped.reserve({"control_requests": 1}, "globally-late-scoped-settlement")
    cleanup = resource_budget.ResourceLedger(config2, ledger2, now=lambda: clock[0], default_cleanup=True)
    assert cleanup.reserve({"control_requests": 1}, "approved-unscoped-cleanup")


def test_cleanup_phase_uses_only_reserved_resources_and_cannot_select_episode(tmp_path, monkeypatch):
    config, ledger = tmp_path / "config.json", tmp_path / "ledger.jsonl"
    _write_episode_config(config)
    monkeypatch.setenv(resource_budget._CONFIG_ENV, str(config)); monkeypatch.setenv(resource_budget._LEDGER_ENV, str(ledger))
    monkeypatch.setenv(resource_budget._PHASE_ENV, "cleanup")
    assert resource_budget.reserve_configured({"control_requests": 1}, "cleanup-control")
    event = json.loads(ledger.read_text().splitlines()[-1]); assert event["cleanup"] is True
    with pytest.raises(resource_budget.ResourceBudgetExceeded, match="cleanup reserve"):
        resource_budget.reserve_configured({"provider_requests": 1, "provider_tokens": 40}, "cleanup-provider")
    monkeypatch.setenv(resource_budget._EPISODE_ENV, "ep")
    with pytest.raises(resource_budget.ResourceBudgetError, match="cannot select"):
        resource_budget.configured_ledger()
    monkeypatch.setenv(resource_budget._PHASE_ENV, "invalid")
    with pytest.raises(resource_budget.ResourceBudgetError, match="active or cleanup"):
        resource_budget.configured_ledger()


def test_missing_scope_and_corrupted_scope_fields_fail_closed(tmp_path):
    config, ledger = tmp_path / "config.json", tmp_path / "ledger.jsonl"
    _write_episode_config(config)
    scoped = resource_budget.ResourceLedger(config, ledger, episode_id="missing")
    with pytest.raises(resource_budget.ResourceBudgetExceeded, match="not registered"):
        scoped.reserve({"control_requests": 1}, "missing")
    assert not ledger.exists()

    operator = resource_budget.ResourceLedger(config, ledger); _register_episode(operator)
    records = [json.loads(line) for line in ledger.read_text().splitlines()]
    records[-1]["payload_task_limit"] = False
    ledger.write_text("".join(json.dumps(record) + "\n" for record in records))
    with pytest.raises(resource_budget.ResourceBudgetError):
        operator.status()
