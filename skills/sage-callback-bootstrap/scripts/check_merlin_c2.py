#!/usr/bin/env python3
"""Throwaway (gitignored) — confirm Merlin supports an http C2 profile (ISC-44)."""
import asyncio
import json
import sys

sys.path.insert(0, "/home/john/dev/sage/Payload_Type/sage")
from evals.harness import resolve_password, login_to_mythic  # noqa: E402
from mythic import mythic  # noqa: E402

Q = """
query MerlinC2 {
  payloadtypec2profile(where: {payloadtype: {name: {_eq: "merlin"}}}) {
    c2profile { name is_p2p }
  }
}
"""


async def main():
    client = await login_to_mythic(resolve_password())
    r = await mythic.execute_custom_query(client, Q)
    profiles = [row["c2profile"]["name"] for row in r.get("payloadtypec2profile", [])]
    print("Merlin C2 profiles:", json.dumps(profiles))
    print("HTTP SUPPORTED:", any(p.lower() == "http" for p in profiles))


if __name__ == "__main__":
    asyncio.run(main())
