"""Relaunch helper: load a NUL-delimited /proc environ snapshot and exec `python -u main.py`.

Used by sage_restart.sh to bring Sage back up with byte-identical environment (incl. RABBITMQ_PASSWORD,
MYTHIC_SERVER_HOST, etc.) after a tmux C-c. Robust to special chars in values (execve, not shell).
argv: <cwd> <python-bin> <env-snapshot-path>
"""
import importlib.util
from datetime import datetime, timezone
import os
import hashlib
import json
from pathlib import Path
import stat
import sys


OBSERVER_CONFIG_ENV = "SAGE_PROCESS_OBSERVER_CONFIG"
OBSERVER_CONFIG_SHA256_ENV = "SAGE_PROCESS_OBSERVER_CONFIG_SHA256"

def _load_module(path: Path, name: str):
    spec = importlib.util.spec_from_file_location(name, path)
    if spec is None or spec.loader is None:
        return None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def identity_env(exec_env: dict[str, str], sage_dir: str) -> dict[str, str]:
    """Exec env plus whatever `main.py` will load from `.env` — for identity recording only.

    The recorder used to see the exec environment alone, but `main.py` calls `load_sage_dotenv()`
    AFTER this process execs it. So `provider`, `model` and `API_ENDPOINT` set in
    `Payload_Type/sage/.env` — the documented way to configure a local Sage — were absent by
    construction, and readiness reported "provider is not recorded" on every such run. A gate that
    cannot observe the thing it gates is not evidence, and one that cries wolf trains you past it.

    Returns a COPY. The exec environment is deliberately left byte-identical: `main.py` does its own
    loading with the same precedence, so injecting the values here would change nothing except our
    ability to tell the two paths apart if they ever diverge.

    Fails soft on purpose. Identity bookkeeping must never be the reason Sage does not come back up,
    so any problem reading `.env` degrades to the old behaviour rather than raising.
    """
    merged = dict(exec_env)
    try:
        bootstrap = _load_module(Path(sage_dir) / "dotenv_bootstrap.py", "sage_dotenv_relaunch")
        if bootstrap is None:
            return merged
        # Reuse Sage's own search order and apply_dotenv rather than reimplementing "already set
        # wins", "empty is skipped", or the .env.local-before-.env precedence. A second copy of any
        # of those is a second thing to drift, and drift here means the gate lies again.
        for path in bootstrap.dotenv_paths(sage_dir):
            bootstrap.apply_dotenv(bootstrap.dotenv_values(path), merged)
    except Exception:
        return dict(exec_env)
    return merged


def optional_observer_command(env: dict[str, str], cwd: str, py: str) -> list[str] | None:
    """Return the explicitly source-bound observer exec, or preserve the original launch."""
    config_value = str(env.get(OBSERVER_CONFIG_ENV) or "")
    digest = str(env.get(OBSERVER_CONFIG_SHA256_ENV) or "")
    if not config_value and not digest:
        return None
    if (not config_value or len(digest) != 64
            or any(character not in "0123456789abcdef" for character in digest)):
        raise SystemExit("process observer configuration is invalid")
    config_path = Path(config_value)
    if not config_path.is_absolute():
        raise SystemExit("process observer configuration is invalid")
    try:
        descriptor = os.open(config_path, os.O_RDONLY | os.O_NOFOLLOW)
        metadata = os.fstat(descriptor)
        raw = os.read(descriptor, 1_048_577)
        os.close(descriptor)
    except OSError:
        raise SystemExit("process observer configuration is invalid") from None
    if (not stat.S_ISREG(metadata.st_mode) or metadata.st_uid != os.getuid()
            or stat.S_IMODE(metadata.st_mode) != 0o600 or not raw or len(raw) > 1_048_576
            or hashlib.sha256(raw).hexdigest() != digest):
        raise SystemExit("process observer configuration is invalid")
    try:
        config = json.loads(raw)
        observer_path = Path(config["observer_path"])
        observer_sha256 = config["observer_sha256"]
    except (KeyError, TypeError, UnicodeError, json.JSONDecodeError):
        raise SystemExit("process observer configuration is invalid") from None
    try:
        observer_metadata = os.stat(observer_path, follow_symlinks=False)
        observer_raw = observer_path.read_bytes()
    except OSError:
        raise SystemExit("process observer configuration is invalid") from None
    if (not observer_path.is_absolute() or not stat.S_ISREG(observer_metadata.st_mode)
            or observer_metadata.st_uid != os.getuid()
            or hashlib.sha256(observer_raw).hexdigest() != observer_sha256):
        raise SystemExit("process observer configuration is invalid")
    return [py, str(observer_path), "--exec-config", str(config_path),
            "--exec-config-sha256", digest, "--cwd", cwd, "--python", py]


def main() -> None:
    cwd, py, snap = sys.argv[1], sys.argv[2], sys.argv[3]
    env: dict[str, str] = {}
    for kv in open(snap, "rb").read().split(b"\x00"):
        if b"=" in kv:
            k, v = kv.split(b"=", 1)
            env[k.decode("utf-8", "replace")] = v.decode("utf-8", "replace")
    module = _load_module(
        Path(__file__).resolve().with_name("readiness_contract.py"),
        "sage_readiness_contract_relaunch",
    )
    if module is not None:
        module.write_startup_identity(
            module.DEFAULT_STARTUP_IDENTITY_PATH,
            identity_env(env, cwd),
            pid=os.getpid(),
            cwd=cwd,
            recorded_at=datetime.now(timezone.utc).isoformat(),
        )
    os.chdir(cwd)
    observer_command = optional_observer_command(env, cwd, py)
    os.execve(py, observer_command or [py, "-u", "main.py"], env)


# Guarded so `identity_env` is importable by tests. sage_restart.sh invokes this as a script, so
# __main__ holds there and behaviour is unchanged.
if __name__ == "__main__":
    main()
