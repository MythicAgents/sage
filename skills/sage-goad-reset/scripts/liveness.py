#!/usr/bin/env python3
"""Throwaway (gitignored) — board state before the converged run: active callbacks + liveness."""
import asyncio
import sys

sys.path.insert(0, "/home/john/dev/sage/Payload_Type/sage")
from evals.harness import resolve_password, login_to_mythic  # noqa: E402
from ai.langgraph.mythic_tools import assess_callback_liveness  # noqa: E402
from mythic import mythic  # noqa: E402


async def main():
    client = await login_to_mythic(resolve_password())
    cbs = await mythic.get_all_active_callbacks(client)
    print(f"active callbacks: {len(cbs or [])}")
    for cb in sorted(cbs or [], key=lambda c: c.get("display_id", 0)):
        did = cb.get("display_id")
        host = cb.get("host")
        user = cb.get("user")
        ptype = ((cb.get("payload") or {}).get("payloadtype") or {}).get("name")
        last = cb.get("last_checkin")
        try:
            live = await assess_callback_liveness(client, did)
            status = live.get("status")
            reason = live.get("reason", "")[:60]
        except Exception as e:
            status, reason = "err", str(e)[:60]
        print(f"  cb{did:<4} {str(ptype):<8} {str(user):<20} {str(host):<22} "
              f"last={last}  LIVE={status} ({reason})")


if __name__ == "__main__":
    asyncio.run(main())
