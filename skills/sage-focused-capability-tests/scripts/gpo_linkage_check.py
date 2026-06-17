#!/usr/bin/env python3
"""(c-1) STARKWALLPAPER GPO linkage check — inference-free, direct BloodHound cypher.

Answers the structural question behind the dcsync-rights-grant wall: does the won GPO route to
something with domain-write (a DC under its scope, or a path to Domain Admins / domain-root DACL),
or does SYSTEM-on-its-targets have no domain rights (making any self-grant denied regardless of
delivery)? Run via the MCP env:
  uv --directory /home/john/dev/bloodhound_mcp run python /home/john/dev/sage/skills/sage-focused-capability-tests/scripts/gpo_linkage_check.py
"""
import sys
sys.path.insert(0, "/home/john/dev/bloodhound_mcp")
from lib.bloodhound_api import BloodhoundBaseClient, CypherClient  # noqa: E402

cy = CypherClient(BloodhoundBaseClient())


def nodes_of(res):
    d = (res or {}).get("data", {}) or {}
    out = []
    for nid, n in (d.get("nodes", {}) or {}).items():
        props = n.get("properties", {}) or {}
        out.append((n.get("kind") or n.get("kinds"), n.get("label") or props.get("name"), props.get("objectid") or n.get("objectId")))
    return out, (d.get("nodes", {}) or {}), (d.get("edges", []) or [])


def run(title, query, show_edges=False):
    print(f"\n### {title}")
    print(f"    Q: {query}")
    try:
        res = cy.run_query(query)
    except Exception as e:
        print(f"    !! error: {e}")
        return
    if not res.get("success"):
        print("    (no results / 404)")
        return
    rows, nmap, edges = nodes_of(res)
    if not rows:
        print("    (empty)")
        return
    for kind, name, oid in rows:
        print(f"    - [{kind}] {name}  {oid or ''}")
    if show_edges and edges:
        print("    edges:")
        for e in edges:
            s = (nmap.get(str(e.get('source')), {}) or {}); t = (nmap.get(str(e.get('target')), {}) or {})
            sn = s.get('label') or (s.get('properties', {}) or {}).get('name') or e.get('source')
            tn = t.get('label') or (t.get('properties', {}) or {}).get('name') or e.get('target')
            print(f"      {sn} -[{e.get('kind') or e.get('label')}]-> {tn}")


run("Q1 STARKWALLPAPER GPO node",
    "MATCH (g:GPO) WHERE toUpper(g.name) CONTAINS 'STARKWALLPAPER' RETURN g")

run("Q2 What STARKWALLPAPER GPLinks to (OU/Domain scope)",
    "MATCH p=(g:GPO)-[:GPLink]->(n) WHERE toUpper(g.name) CONTAINS 'STARKWALLPAPER' RETURN p", show_edges=True)

run("Q3 Computers in STARKWALLPAPER scope (GPLink->container->Contains*->Computer)",
    "MATCH (g:GPO)-[:GPLink]->(c1)-[:Contains*1..4]->(comp:Computer) "
    "WHERE toUpper(g.name) CONTAINS 'STARKWALLPAPER' RETURN DISTINCT comp")

run("Q4 Domain Controllers (members of -516) — to intersect with Q3",
    "MATCH (c:Computer)-[:MemberOf*1..]->(g:Group) WHERE g.objectid ENDS WITH '-516' RETURN DISTINCT c")

run("Q5 Path STARKWALLPAPER -> Domain Admins (-512)",
    "MATCH p=shortestPath((g:GPO)-[*1..5]->(grp:Group)) "
    "WHERE toUpper(g.name) CONTAINS 'STARKWALLPAPER' AND grp.objectid ENDS WITH '-512' RETURN p",
    show_edges=True)

run("Q6 Who holds domain-root control on NORTH (WriteDacl/GenericAll/Owns/DCSync/GetChanges*)",
    "MATCH p=(n)-[r:WriteDacl|GenericAll|Owns|WriteOwner|AllExtendedRights|DCSync|GetChanges|GetChangesAll]->(d:Domain) "
    "WHERE toUpper(d.name)='NORTH.SEVENKINGDOMS.LOCAL' RETURN p", show_edges=True)

run("Q7 samwell -> STARKWALLPAPER control edge",
    "MATCH p=(u:User)-[r]->(g:GPO) WHERE toUpper(u.name) CONTAINS 'SAMWELL' "
    "AND toUpper(g.name) CONTAINS 'STARKWALLPAPER' RETURN p", show_edges=True)
