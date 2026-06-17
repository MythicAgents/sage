#!/usr/bin/env python3
"""Create and inspect short-lived Sage architecture gate tokens."""

from __future__ import annotations

import argparse
from datetime import datetime, timezone
import json
from pathlib import Path
import tempfile
import time

from arch_governor_common import (
    HIGH_RISK_PATTERNS,
    TOKEN_VERSION,
    approval_status,
    repo_hash,
    repo_root,
    token_path,
    write_json,
)


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def open_token(args) -> int:
    root = repo_root()
    scope = args.files or list(HIGH_RISK_PATTERNS)
    expires_at = time.time() + max(1, int(args.minutes)) * 60
    payload = {
        "version": TOKEN_VERSION,
        "repo_root": str(root),
        "repo_hash": repo_hash(root),
        "created_at": _now_iso(),
        "expires_at": expires_at,
        "expires_at_iso": datetime.fromtimestamp(expires_at, tz=timezone.utc).isoformat(),
        "approved_by": args.approved_by,
        "approval_source": args.approval_source,
        "reason": args.reason,
        "scope": scope,
    }
    write_json(token_path(root), payload)
    print(json.dumps({"ok": True, "token": str(token_path(root)), "scope": scope, "expires_at": payload["expires_at_iso"]}, indent=2))
    return 0


def status_token(_args) -> int:
    root = repo_root()
    path = token_path(root)
    if not path.exists():
        print(json.dumps({"ok": False, "token": str(path), "reason": "missing"}, indent=2))
        return 1
    data = json.loads(path.read_text(encoding="utf-8"))
    ok, reason = approval_status(root, data.get("scope") or [])
    data["valid"] = ok
    data["validation"] = reason
    data["token"] = str(path)
    print(json.dumps(data, indent=2, sort_keys=True))
    return 0 if ok else 1


def close_token(_args) -> int:
    path = token_path(repo_root())
    if path.exists():
        path.unlink()
    print(json.dumps({"ok": True, "closed": str(path)}, indent=2))
    return 0


def self_test() -> int:
    with tempfile.TemporaryDirectory() as tmp:
        old_dir = Path.cwd()
        try:
            Path(tmp, ".git").mkdir()
            import os

            os.chdir(tmp)
            args = argparse.Namespace(
                files=["Payload_Type/sage/ai/langgraph/model.py"],
                minutes=1,
                approved_by="self-test",
                approval_source="self-test approval",
                reason="self-test",
            )
            open_token(args)
            ok, reason = approval_status(repo_root(), ["Payload_Type/sage/ai/langgraph/model.py"])
            if not ok:
                print(f"self-test failed: token invalid: {reason}")
                return 1
            close_token(argparse.Namespace())
        finally:
            os.chdir(old_dir)
    print("open_gate self-test passed")
    return 0


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--self-test", action="store_true")
    sub = parser.add_subparsers(dest="cmd")

    open_p = sub.add_parser("open")
    open_p.add_argument("--reason", required=True)
    open_p.add_argument("--approved-by", required=True)
    open_p.add_argument("--approval-source", required=True)
    open_p.add_argument("--minutes", type=int, default=90)
    open_p.add_argument("--files", action="append", default=[])
    open_p.set_defaults(func=open_token)

    status_p = sub.add_parser("status")
    status_p.set_defaults(func=status_token)

    close_p = sub.add_parser("close")
    close_p.set_defaults(func=close_token)

    args = parser.parse_args()
    if args.self_test:
        return self_test()
    if not hasattr(args, "func"):
        parser.print_help()
        return 2
    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main())
