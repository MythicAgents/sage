"""Secret-free Engagement State Ledger fixtures shaped like the channel 3/4 operation ledger.

The live ledger behind chat channels 3 and 4 held seven hops numbered 23–29 with ``next_hop_number`` 30 after
repeated renumbering. These fixtures keep its row shapes (technique, target, effect, status, satisfied effects
and the presence of a proof envelope) and synthesize admissible envelopes for a test engagement key. No task
output, credential, hash or host evidence from the range is copied.
"""

from __future__ import annotations

import copy
from datetime import datetime, timezone
import json
from pathlib import Path
import sys

LG = Path(__file__).resolve().parents[1] / "ai" / "langgraph"
if str(LG) not in sys.path:
    sys.path.insert(0, str(LG))

import proof_boundary as pb  # noqa: E402

OPERATION_ID = 1
OPERATION_NAME = "Operation Chimera"
MARKER_UUID = "cd2a0da1-0000-4000-8000-000000000034"
ENGAGEMENT_KEY = f"Operation_Chimera_{OPERATION_ID}_{MARKER_UUID}"


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def task_proof(engagement_id: str, *, task_id: str, callback_id: str = "3", command: str = "shell",
               verifier: str = "fixture:task") -> dict:
    return pb.make_runtime_task_envelope(
        engagement_id=engagement_id,
        callback_id=callback_id,
        task_id=task_id,
        terminal_status="completed",
        command=command,
        verifier_id=verifier,
        transaction_id=f"fixture-transaction-{task_id}",
        verifier_input={"task": task_id},
        verifier_result={"verdict": "achieved"},
        captured_at=_now(),
    ).to_dict()


def ingest_proof(engagement_id: str, *, task_id: str, job_id: str, callback_id: str = "3") -> dict:
    return pb.make_runtime_bloodhound_envelope(
        engagement_id=engagement_id,
        callback_id=callback_id,
        task_id=task_id,
        terminal_status="completed",
        command="sharphound",
        ingest_job_id=job_id,
        ingest_status="complete",
        source_artifact_id=f"fixture-artifact-{task_id}",
        source_artifact_sha256="a" * 64,
        verifier_id="fixture:ingest",
        transaction_id=f"fixture-ingest-{task_id}",
        verifier_input={"job": job_id},
        verifier_result={"verdict": "achieved"},
        captured_at=_now(),
    ).to_dict()


def _hop(number, technique, target, effect, status, *, source, proof=None, evidence=None):
    ev = dict(evidence or {})
    if proof:
        ev["proof_envelope"] = proof
    return {
        "number": number,
        "id": f"{technique}:{target}",
        "technique": technique,
        "target": target,
        "effect": effect,
        "status": status,
        "evidence": ev,
        "preconditions": [],
        "satisfied_effects": [effect],
        "source": source,
        "timestamp": _now(),
        "proof_envelope": dict(proof or {}),
    }


def channel34_ledger(engagement_id: str = ENGAGEMENT_KEY) -> dict:
    """Seven hops numbered 23–29 with high-water 30, plus a few graph facts, in the legacy JSON shape."""
    hops = [
        _hop(23, "collect-graph", "north|baseline", "graph-built:north|baseline", "achieved",
             source="ingest_collection", proof=ingest_proof(engagement_id, task_id="101", job_id="11"),
             evidence={"graph_verified": True, "covered_domains": ["north.sevenkingdoms.local"],
                       "mythic_task_id": "101", "callback_id": "3"}),
        _hop(24, "gpo-abuse", "starkwallpaper", "system:starkwallpaper", "achieved",
             source="issue_task", proof=task_proof(engagement_id, task_id="102"),
             evidence={"artifact_present": True, "mythic_task_id": "102", "callback_id": "3"}),
        _hop(25, "domain-admin-membership-check", "north", "da:north", "achieved",
             source="issue_task", proof=task_proof(engagement_id, task_id="103"),
             evidence={"artifact_present": True, "mythic_task_id": "103", "callback_id": "3"}),
        _hop(26, "capability:gpo-controlled-system-exec",
             "gpo=starkwallpaper;domain=north.sevenkingdoms.local",
             "system-exec:gpo:starkwallpaper@north.sevenkingdoms.local", "achieved",
             source="execute_capability", proof=task_proof(engagement_id, task_id="104"),
             evidence={"artifact_present": True, "mythic_task_id": "104", "callback_id": "3"}),
        _hop(27, "capability:dcsync-krbtgt", "domain=north.sevenkingdoms.local;account=krbtgt",
             "krbtgt-hash:north.sevenkingdoms.local", "blocked", source="execute_capability",
             evidence={"failure_class": "fixture", "transaction_status": "blocked"}),
        _hop(28, "dcsync", "north.sevenkingdoms.local", "krbtgt-hash:north.sevenkingdoms.local",
             "legacy_unverified", source="issue_task",
             evidence={"proof_admission_reason": "missing proof envelope", "verify_verdict": "achieved"}),
        _hop(29, "collect-graph", "north|da:north|scope:essos.local",
             "graph-built:north|da:north|scope:essos.local", "achieved", source="ingest_collection",
             proof=ingest_proof(engagement_id, task_id="105", job_id="12"),
             evidence={"graph_verified": True, "covered_domains": ["essos.local"],
                       "mythic_task_id": "105", "callback_id": "3"}),
    ]
    facts = [
        {"predicate": predicate, "source": "bloodhound:cypher", "timestamp": _now(), "ttl_seconds": 600,
         "proof_envelope": ingest_proof(engagement_id, task_id="101", job_id="11")}
        for predicate in (
            "generic-write:gpo:starkwallpaper",
            "gpo-domain:starkwallpaper:north.sevenkingdoms.local",
            "trust-reachable:north.sevenkingdoms.local:sevenkingdoms.local",
        )
    ]
    return {
        "engagement_id": engagement_id,
        "objective": "sage-engagement",
        "objective_source": None,
        "updated": _now(),
        "graph_facts_updated": _now(),
        "hops": hops,
        "graph_facts": facts,
        "next_hop_number": 30,
    }


def write_legacy_ledger(state_dir: str | Path, data: dict) -> Path:
    """Write a legacy ``state_<key>.json`` file exactly as the pre-SQLite runtime did (unchanged bytes shape)."""
    directory = Path(state_dir)
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / f"state_{data['engagement_id']}.json"
    path.write_text(json.dumps(copy.deepcopy(data), indent=2, sort_keys=True), encoding="utf-8")
    return path


def _seed_proof(engagement_id: str, hop: dict) -> dict:
    """A structurally admissible envelope matching the hop's own evidence lineage (test seeding only)."""
    evidence = hop.get("evidence") if isinstance(hop.get("evidence"), dict) else {}
    return pb.make_runtime_task_envelope(
        engagement_id=engagement_id,
        callback_id=evidence.get("callback_id") or evidence.get("callback_display_id") or "1",
        task_id=evidence.get("mythic_task_id") or evidence.get("task_id") or "1",
        terminal_status=evidence.get("terminal_task_status") or evidence.get("terminal_status") or "completed",
        command=str(evidence.get("command") or hop.get("technique") or "seed"),
        verifier_id=evidence.get("verifier_id") or "test:seed",
        transaction_id=evidence.get("transaction_id") or f"seed:{hop.get('technique')}:{hop.get('target')}",
        verifier_input={"seed": hop.get("id")},
        verifier_result={"verdict": "achieved"},
        captured_at=_now(),
    ).to_dict()


def seed_ledger(tools, *, hops=(), graph_facts=(), prove: bool = True, own: bool = True):
    """Write rows into a session's bound operation ledger through the store's mutation API.

    Replaces tests that assigned ``tools._engagement_hops`` directly. ``prove`` gives an ``achieved`` hop that
    has no proof envelope (and no pinned verifier commitments) an admissible test envelope; ``own`` marks the
    rows as produced by this session (run provenance), as the old in-memory assignment implied.
    """
    import dataclasses

    from ai.langgraph import engagement_store as store_mod

    binding = tools._require_ledger_binding()
    store = tools._ledger_store()

    def as_dict(row, fields):
        if dataclasses.is_dataclass(row):
            return dataclasses.asdict(row)
        if isinstance(row, dict):
            return dict(row)
        return {name: getattr(row, name) for name in fields if hasattr(row, name)}

    hop_fields = ("id", "technique", "target", "effect", "status", "evidence", "preconditions",
                  "satisfied_effects", "source", "timestamp", "proof_envelope")
    fact_fields = ("predicate", "source", "timestamp", "ttl_seconds", "proof_envelope")
    hop_dicts = []
    for hop in hops or ():
        data = as_dict(hop, hop_fields)
        evidence = data.get("evidence") if isinstance(data.get("evidence"), dict) else {}
        if (
            prove
            and str(data.get("status") or "").casefold() == "achieved"
            and not data.get("proof_envelope")
            and not evidence.get("proof_envelope")
            and not evidence.get("verifier_input_sha256")
            and not evidence.get("verifier_result_sha256")
            and not evidence.get("artifact_id")
        ):
            data["proof_envelope"] = _seed_proof(binding.engagement_key, data)
        hop_dicts.append(data)
    mutations = store_mod.hop_mutations([], hop_dicts)
    if mutations:
        result = store.apply(binding, mutations)
        if own:
            if getattr(tools, "_ledger_own_rows", None) is None:
                tools._ledger_own_rows = set()
            tools._ledger_own_rows.update(result.row_revisions)
    facts = [as_dict(fact, fact_fields) for fact in graph_facts or ()]
    if facts:
        store.apply(binding, store_mod.fact_mutations(facts))
    return tools


def stub_mythic_operation(monkeypatch, *, operation_id: int = 1) -> dict:
    """Stub only the Mythic transport readers that operation reconciliation and the callback verifier use.

    The readers serve an operation's recorded tasks, outputs, credential-store entries and callbacks with the same
    selection the real GraphQL queries make: operation scope, display IDs above the cursor plus held tasks in
    ascending order, at most the pass limit, credentials by producing task. The store, reducers, proof boundary,
    verifiers and request contract stay real. Set ``records["fail"]`` to make Mythic unreachable.
    """
    from ai.langgraph import mythic_tools as mythic_tools_module
    from ai.langgraph import state_reconcile as sr

    records: dict = {"tasks": [], "outputs": {}, "credentials": [], "callbacks": [], "fail": False,
                     "operation_id": operation_id, "queries": 0}

    def reachable():
        records["queries"] += 1
        if records["fail"]:
            raise RuntimeError("Mythic unreachable")

    def in_operation(row: dict) -> bool:
        return row.get("operation_id", operation_id) == records["operation_id"]

    def key(row: dict) -> int:
        return int(row["display_id"])

    async def operation_tasks(client, op, cursor, held, limit=sr.PASS_LIMIT):
        reachable()
        rows = [task for task in records["tasks"] if in_operation(task)]
        held = {int(item) for item in held or ()}
        above = sorted((task for task in rows if key(task) > cursor), key=key)[:limit]
        return sorted([task for task in rows if key(task) in held] + above, key=key)

    async def cited_task(client, op, display_id):
        reachable()
        rows = [task for task in records["tasks"] if key(task) == int(display_id)]
        mine = [task for task in rows if in_operation(task)]
        return (mine[0], "") if mine else ({}, "not in this operation" if rows else "no such task")

    async def task_credentials(client, op, task_ids):
        reachable()
        wanted = {int(item) for item in task_ids}
        return [row for row in records["credentials"] if in_operation(row)
                and (row.get("task") or {}).get("display_id") in wanted
                and in_operation(row.get("task") or {})]

    async def cited_credential(client, op, credential_id):
        reachable()
        rows = [row for row in records["credentials"] if row["id"] == int(credential_id)]
        mine = [row for row in rows if in_operation(row)]
        return (mine[0], "") if mine else ({}, "not in this operation" if rows else "no such credential")

    async def credit_tasks(client, op, since, until, callback_ids):
        reachable()
        return [{"display_id": task["display_id"], "status": task.get("status"), "completed": task.get("completed"),
                 "command_name": task.get("command_name"), "timestamp": task.get("timestamp", ""),
                 "callback": {"display_id": (task.get("callback") or {}).get("display_id")}}
                for task in sorted(records["tasks"], key=key) if in_operation(task)]

    async def output(client, task_id):
        reachable()
        return records["outputs"].get(int(task_id), "")

    async def callback_rows(self):
        reachable()
        return [dict(row) for row in records["callbacks"]]

    monkeypatch.setattr(sr, "fetch_operation_tasks", operation_tasks)
    monkeypatch.setattr(sr, "fetch_cited_task", cited_task)
    monkeypatch.setattr(sr, "fetch_task_credentials", task_credentials)
    monkeypatch.setattr(sr, "fetch_cited_credential", cited_credential)
    monkeypatch.setattr(sr, "fetch_callback_credit_tasks", credit_tasks)
    monkeypatch.setattr(sr, "_fetch_task_output_text", output)
    monkeypatch.setattr(mythic_tools_module.MythicTools, "_step_callback_rows", callback_rows)
    return records
