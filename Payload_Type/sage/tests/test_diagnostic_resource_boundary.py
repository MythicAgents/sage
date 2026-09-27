from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
import fcntl
import importlib.util
import json
import os
from pathlib import Path
import select
import subprocess
import sys
import threading
import time

from ai.resource_budget import ResourceBudgetExceeded, ResourceLedger


ROOT = Path(__file__).resolve().parents[3]
ISOLATION_SOURCE = ROOT / "skills/sage-eval-gauge/scripts/diagnostic_isolation.py"
SPEC = importlib.util.spec_from_file_location("m3_diagnostic_isolation", ISOLATION_SOURCE)
assert SPEC and SPEC.loader
isolation = importlib.util.module_from_spec(SPEC); SPEC.loader.exec_module(isolation)


def _reservation(budget: ResourceLedger, operation: str) -> str:
    try:
        budget.reserve({"control_requests": 1}, operation)
        return "reserved"
    except ResourceBudgetExceeded:
        return "exceeded"


def test_isolated_child_and_host_share_resource_lock_and_cap(tmp_path):
    shared = tmp_path / "shared"; shared.mkdir(mode=0o700)
    config, ledger = shared / "budget.json", shared / "ledger.jsonl"
    config.write_text(json.dumps({
        "schema": "sage-shared-resource-budget-v1",
        "resources": {"control_requests": {"limit": 2, "cleanup_reserve": 0}},
        "calendar_seconds": 300, "cleanup_seconds": 30,
    }))
    host = ResourceLedger(config, ledger)
    host.reserve({"control_requests": 1}, "prefill")
    lock_path = host._lock_path(); lock_path.touch(mode=0o600)
    expected = os.stat(lock_path, follow_symlinks=False)

    child_source = r'''
import json,os
from ai.resource_budget import ResourceBudgetExceeded,ResourceLedger
b=ResourceLedger(os.environ["CONFIG"],os.environ["LEDGER"])
s=os.stat(b._lock_path(),follow_symlinks=False)
print(json.dumps({"device":s.st_dev,"inode":s.st_ino,"uid":s.st_uid}),flush=True)
try:b.reserve({"control_requests":1},"child"); outcome="reserved"
except ResourceBudgetExceeded:outcome="exceeded"
print(outcome,flush=True)
'''
    command = isolation.isolated_command(
        [sys.executable, "-P", "-c", child_source],
        writable_paths=[shared, lock_path.parent],
        read_only_paths=[ROOT],
        environment={"CONFIG": str(config), "LEDGER": str(ledger),
                     "PYTHONPATH": str(ROOT / "Payload_Type/sage"),
                     "PYTHONDONTWRITEBYTECODE": "1"},
        cwd=ROOT / "Payload_Type/sage",
    )
    descriptor = os.open(lock_path, os.O_RDWR | os.O_NOFOLLOW)
    wrapper = None
    pool = ThreadPoolExecutor(max_workers=1); started = threading.Event()
    try:
        fcntl.flock(descriptor, fcntl.LOCK_EX)
        wrapper = subprocess.Popen(command, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
                                   stderr=subprocess.STDOUT, text=True)
        assert wrapper.stdout is not None
        readable, _, _ = select.select([wrapper.stdout], [], [], 5)
        assert readable
        observed = json.loads(wrapper.stdout.readline())
        assert observed == {"device": expected.st_dev, "inode": expected.st_ino,
                            "uid": expected.st_uid}

        def host_reservation():
            started.set()
            return _reservation(host, "host")

        future = pool.submit(host_reservation); assert started.wait(2)
        time.sleep(.05)
        assert not future.done() and wrapper.poll() is None
    finally:
        fcntl.flock(descriptor, fcntl.LOCK_UN); os.close(descriptor)

    try:
        host_outcome = future.result(timeout=5)
        remaining, _ = wrapper.communicate(timeout=5)
        child_outcome = remaining.strip()
        assert wrapper.returncode == 0
        assert sorted((host_outcome, child_outcome)) == ["exceeded", "reserved"]
        assert host.status()["effective_resource_charges"] == {"control_requests": 2}
    finally:
        pool.shutdown(wait=True)
        if wrapper.poll() is None:
            wrapper.terminate(); wrapper.wait(timeout=5)
