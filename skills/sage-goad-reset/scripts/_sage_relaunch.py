"""Relaunch helper: load a NUL-delimited /proc environ snapshot and exec `python -u main.py`.

Used by sage_restart.sh to bring Sage back up with byte-identical environment (incl. RABBITMQ_PASSWORD,
MYTHIC_SERVER_HOST, etc.) after a tmux C-c. Robust to special chars in values (execve, not shell).
argv: <cwd> <python-bin> <env-snapshot-path>
"""
import importlib.util
from datetime import datetime, timezone
import os
from pathlib import Path
import sys

cwd, py, snap = sys.argv[1], sys.argv[2], sys.argv[3]
env: dict[str, str] = {}
for kv in open(snap, "rb").read().split(b"\x00"):
    if b"=" in kv:
        k, v = kv.split(b"=", 1)
        env[k.decode("utf-8", "replace")] = v.decode("utf-8", "replace")
readiness_path = Path(__file__).resolve().with_name("readiness_contract.py")
spec = importlib.util.spec_from_file_location("sage_readiness_contract_relaunch", readiness_path)
if spec is not None and spec.loader is not None:
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    module.write_startup_identity(
        module.DEFAULT_STARTUP_IDENTITY_PATH,
        env,
        pid=os.getpid(),
        cwd=cwd,
        recorded_at=datetime.now(timezone.utc).isoformat(),
    )
os.chdir(cwd)
os.execve(py, [py, "-u", "main.py"], env)
