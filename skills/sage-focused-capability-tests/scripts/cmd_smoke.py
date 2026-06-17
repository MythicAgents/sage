#!/usr/bin/env python3
"""Throwaway (gitignored) — command-construction smoke harness (#2).

Validates the deterministic resolver (command_builder.resolve_params) against the LIVE Mythic command
schemas — not hand-written test fakes — using the exact training-prior parameter shapes the agent actually
emitted across runs (assembly_name/assembly_file/assembly_arguments, mimikatz `commands`, etc.).

Seconds, no 25-min solve. Schema-validation only (does NOT issue tasks). Pass --live to also dry-construct
against the live schema for a callback's payloadtype.
"""
import asyncio
import sys

sys.path.insert(0, "/home/john/dev/sage/Payload_Type/sage")
from evals.harness import resolve_password, login_to_mythic  # noqa: E402
from ai.langgraph import command_builder  # noqa: E402
from mythic import mythic  # noqa: E402

# (payloadtype, command, training-prior supplied params the agent really emitted) -> what we expect resolved
CASES = [
    ("apollo", "execute_assembly", {"assembly_name": "Rubeus.exe", "assembly_arguments": "klist /nowrap"}),
    ("apollo", "inline_assembly", {"assembly_name": "SharpHound.exe", "assembly_arguments": "-c All"}),
    ("apollo", "execute_assembly", {"assembly_file": "Rubeus.exe", "assembly_arguments": "triage"}),
    ("apollo", "mimikatz", {"commands": "lsadump::dcsync /domain:essos.local /user:krbtgt"}),
    ("apollo", "powershell", {"command": "Get-Domain"}),
    ("apollo", "ps", {}),
    ("apollo", "whoami", {}),
    ("merlin", "execute-assembly", {"assembly_name": "Rubeus.exe", "assembly_arguments": "klist"}),
    ("merlin", "mimikatz", {"commands": "lsadump::dcsync /domain:essos.local /user:krbtgt"}),
]


async def fetch_schemas(client, payloadtype: str) -> dict[str, list[dict]]:
    q = ("query CMD($pt:String!){ command(where:{payloadtype:{name:{_eq:$pt}}, deleted:{_eq:false}}){ "
         "cmd commandparameters{ cli_name name parameter_group_name type choices required default_value } } }")
    r = await mythic.execute_custom_query(mythic=client, query=q, variables={"pt": payloadtype})
    return {c["cmd"]: (c.get("commandparameters") or []) for c in r.get("command", [])}


def valid_keys_for_group(params: list[dict], group: str | None) -> set[str]:
    keys = set()
    for p in params:
        g = p.get("parameter_group_name")
        if group is None or g == group or g == "Default":
            keys.add(p.get("cli_name") or p.get("name"))
    return keys


async def main():
    client = await login_to_mythic(resolve_password())
    schemas: dict[str, dict] = {}
    for pt in {pt for pt, _, _ in CASES}:
        try:
            schemas[pt] = await fetch_schemas(client, pt)
        except Exception as e:
            schemas[pt] = {}
            print(f"[warn] schema fetch failed for {pt}: {e}")

    print(f"{'payload':<8}{'command':<18}{'supplied keys':<34}{'->':<3}{'OK':<4}{'group':<10}resolved keys / repair")
    npass = nfail = 0
    for pt, cmd, supplied in CASES:
        params = schemas.get(pt, {}).get(cmd)
        if params is None:
            print(f"{pt:<8}{cmd:<18}{'(command not in live schema)':<34}")
            continue
        res = command_builder.resolve_params(params, dict(supplied), command=cmd)
        supplied_k = ",".join(supplied) or "(none)"
        if res.ok:
            valid = valid_keys_for_group(params, res.group)
            stray = [k for k in res.params if k not in valid]
            ok_mark = "OK" if not stray else "OK*"
            detail = ",".join(res.params) + (f"  STRAY={stray}" if stray else "")
            print(f"{pt:<8}{cmd:<18}{supplied_k[:33]:<34}{'':<3}{ok_mark:<4}{str(res.group)[:9]:<10}{detail[:60]}")
            npass += 0 if stray else 1
            nfail += 1 if stray else 0
        else:
            print(f"{pt:<8}{cmd:<18}{supplied_k[:33]:<34}{'':<3}{'NO':<4}{str(res.group)[:9]:<10}repair: {str(res.repair)[:55]}")
            # a clean repair hint on a genuinely-underspecified call is acceptable; flag for review
            nfail += 1
    print(f"\nclean-construct: {npass}/{len([c for c in CASES])}  (NO/OK* rows need a look)")


if __name__ == "__main__":
    asyncio.run(main())
