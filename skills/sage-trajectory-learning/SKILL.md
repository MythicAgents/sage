---
name: sage-trajectory-learning
description: Repo-local Sage trajectory learning workflow. Use when Codex, Claude Code, or an operator needs to inventory retained Sage/Phoenix/run-log artifacts, export normalized state-action-observation-verifier-repair transition records, label repeated blocker classes, replay repair-policy decisions, or process historical DB archives without writing tools into Plans.
---

# Sage Trajectory Learning

Use this skill to convert retained experience into reusable decision data. Historical `sage.db`,
`.phoenix/phoenix.db`, ledger JSON, Mythic task history, and run logs are read-only corpus artifacts.

## CLI

The trajectory tooling is product code, not a `Plans` script:

```bash
MANIFEST_PATH=$(.venv/bin/python skills/sage-artifact-retention/scripts/artifact_retention.py path --category trajectory/manifests --name corpus-manifest.json)
TRANSITIONS_PATH=$(.venv/bin/python skills/sage-artifact-retention/scripts/artifact_retention.py path --category trajectory/transitions --name transitions.jsonl)
.venv/bin/python -m Payload_Type.sage.ai.trajectory manifest --corpus-root "$(git rev-parse --show-toplevel)" --output "$MANIFEST_PATH"
.venv/bin/python -m Payload_Type.sage.ai.trajectory export --corpus-root "$(git rev-parse --show-toplevel)" --output "$TRANSITIONS_PATH"
.venv/bin/python skills/sage-artifact-retention/scripts/artifact_retention.py record --category trajectory/manifests --artifact-type trajectory-corpus-manifest "$MANIFEST_PATH"
.venv/bin/python skills/sage-artifact-retention/scripts/artifact_retention.py record --category trajectory/transitions --artifact-type trajectory-transition-export "$TRANSITIONS_PATH"
.venv/bin/python -m Payload_Type.sage.ai.trajectory replay --train "$TRANSITIONS_PATH" --eval "$TRANSITIONS_PATH"
```

Use `--corpus-root` repeatedly for off-host archives after mounting/copying them locally. The exporter redacts
common credential material into stable handles. It reads `sage*.db`, `.phoenix/phoenix.db`, ledger JSON, and run
logs read-only; it never mutates source DBs. Path allocation alone does not append output hashes to the manifest;
run `record` after the producer closes each file.

## Runtime Bridge

`execute_capability` failure responses include `trajectory_repair` when the runtime bridge is enabled. The bridge
classifies the observed failure, appends a redacted transition record, and returns the highest-frequency repair
seen for that failure label.

Default runtime store:

```bash
Payload_Type/sage/.trajectory/transitions.jsonl
```

Useful environment knobs:

```bash
SAGE_TRAJECTORY_STORE=/path/to/transitions.jsonl
SAGE_TRAJECTORY_ENABLED=0
SAGE_TRAJECTORY_DISABLE=1
SAGE_TRAJECTORY_SAGE_DB_ROW_LIMIT=5000
```

The bridge is advisory in this slice: it tells Sage/operator which repair to apply, but deterministic capability
builders, Mythic adapters, and verifiers still perform exact command construction and proof.

## Build Order

1. Build a corpus manifest.
2. Export transition JSONL.
3. Label repeated failure classes.
4. Replay repair decisions offline.
5. Use runtime `trajectory_repair` annotations to apply/replay repairs, then promote high-confidence repeated
   repairs into deterministic capability code only after verifier-backed evidence supports them.

## Reference

Read `Plans/TRAJECTORY_LEARNING_RUNTIME.md` for architecture and acceptance criteria.
