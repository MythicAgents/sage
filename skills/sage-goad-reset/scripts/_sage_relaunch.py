"""Relaunch helper: load a NUL-delimited /proc environ snapshot and exec `python -u main.py`.

Used by sage_restart.sh to bring Sage back up with byte-identical environment (incl. RABBITMQ_PASSWORD,
MYTHIC_SERVER_HOST, etc.) after a tmux C-c. Robust to special chars in values (execve, not shell).
argv: <cwd> <python-bin> <env-snapshot-path>
"""
import os, sys

cwd, py, snap = sys.argv[1], sys.argv[2], sys.argv[3]
env: dict[str, str] = {}
for kv in open(snap, "rb").read().split(b"\x00"):
    if b"=" in kv:
        k, v = kv.split(b"=", 1)
        env[k.decode("utf-8", "replace")] = v.decode("utf-8", "replace")
os.chdir(cwd)
os.execve(py, [py, "-u", "main.py"], env)
