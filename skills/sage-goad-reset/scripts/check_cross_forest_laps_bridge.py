#!/usr/bin/env python3
"""Check whether BloodHound exposes a cross-forest managed-secret bridge.

This is a read-only readiness/diagnostic probe for GOAD-style trust-walker labs.
It mirrors the graph shape Sage's reconciler consumes for the managed local-admin
secret hop:

    User -> MemberOf* -> principal -> ReadLAPSPassword -> Computer

Use it after a target-scope SharpHound ingest, or after a staged solve halts with
an empty frontier, to distinguish a real missing graph bridge from a Sage resume
bug. It intentionally does not treat SyncLAPSPassword as equivalent to
ReadLAPSPassword.
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path
import re
import sys
from typing import Any, Callable


DEFAULT_BLOODHOUND_MCP_DIR = Path("/home/john/dev/bloodhound_mcp")
_DOMAIN_RE = re.compile(r"^[a-z0-9.-]+$", re.IGNORECASE)
_MANAGED_SECRET_EDGE_TYPES = ("ReadLAPSPassword", "ReadLAPS", "ReadMSLAPSPassword")


def normalize_domain(value: str) -> str:
    domain = str(value or "").strip().strip(".").casefold()
    if not domain or not _DOMAIN_RE.fullmatch(domain):
        raise ValueError(f"invalid domain: {value!r}")
    return domain


def build_bridge_query(target_domain: str, source_domain: str = "") -> str:
    target = normalize_domain(target_domain)
    source = normalize_domain(source_domain) if source_domain else ""
    edge_labels = "[" + ", ".join(f"'{label}'" for label in _MANAGED_SECRET_EDGE_TYPES) + "]"
    source_filter = (
        f"toLower(u.name) ENDS WITH '@{source}'"
        if source
        else f"NOT toLower(u.name) ENDS WITH '@{target}'"
    )
    return (
        "MATCH (u:User)-[:MemberOf*0..4]->(reader)-[e]->(c:Computer) "
        f"WHERE {source_filter} "
        f"AND toLower(coalesce(c.domain, '')) = '{target}' "
        f"AND type(e) IN {edge_labels} "
        "RETURN DISTINCT u.name + '|' + coalesce(reader.name, '') + '|' + "
        "c.name + '|' + type(e) AS name"
    )


def _get_bloodhound_api(mcp_dir: Path = DEFAULT_BLOODHOUND_MCP_DIR) -> Any:
    sys.path.insert(0, str(mcp_dir))
    from lib.bloodhound_api import BloodhoundAPI

    return BloodhoundAPI()


def _scalar_values(result: dict[str, Any]) -> list[str]:
    data = result.get("data") if isinstance(result, dict) else {}
    literals = data.get("literals") if isinstance(data, dict) else []
    values: list[str] = []
    if not isinstance(literals, list):
        return values
    for item in literals:
        if isinstance(item, dict):
            value = item.get("value")
        else:
            value = item
        if value is not None:
            values.append(str(value))
    return values


def _parse_bridge(value: str) -> dict[str, str] | None:
    parts = [part.strip() for part in str(value or "").split("|")]
    if len(parts) != 4 or not all(parts):
        return None
    return {
        "user": parts[0],
        "reader": parts[1],
        "computer": parts[2],
        "edge_type": parts[3],
    }


def check_bridge(
    target_domain: str,
    *,
    source_domain: str = "",
    query_runner: Callable[[str], dict[str, Any]] | None = None,
    mcp_dir: Path = DEFAULT_BLOODHOUND_MCP_DIR,
) -> dict[str, Any]:
    target = normalize_domain(target_domain)
    source = normalize_domain(source_domain) if source_domain else ""
    query = build_bridge_query(target, source)
    if query_runner is None:
        query_runner = _get_bloodhound_api(mcp_dir).cypher.run_query
    result = query_runner(query)
    bridges = [
        bridge
        for value in _scalar_values(result)
        if (bridge := _parse_bridge(value)) is not None
    ]
    return {
        "ready": bool(bridges),
        "target_domain": target,
        "source_domain": source or None,
        "bridge_count": len(bridges),
        "bridges": bridges,
        "reason": (
            "cross-forest managed-secret bridge is present"
            if bridges
            else "no cross-forest ReadLAPSPassword bridge found in the current BloodHound graph"
        ),
    }


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Check BloodHound for a cross-forest ReadLAPSPassword bridge into a target domain."
    )
    parser.add_argument("--target-domain", required=True)
    parser.add_argument("--source-domain", default="")
    parser.add_argument("--bloodhound-mcp-dir", default=str(DEFAULT_BLOODHOUND_MCP_DIR))
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    result = check_bridge(
        args.target_domain,
        source_domain=args.source_domain,
        mcp_dir=Path(args.bloodhound_mcp_dir),
    )
    print(json.dumps(result, indent=2, sort_keys=True))
    return 0 if result["ready"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
