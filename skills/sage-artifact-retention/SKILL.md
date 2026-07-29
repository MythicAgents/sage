---
name: sage-artifact-retention
description: Preserve high-value Sage contracts, handoffs, reviews, transcripts, and evidence in a private project-local history while keeping payloads, credentials, locks, fixtures, clones, and other scratch work temporary.
---

# Sage Artifact Retention

Use this skill whenever a Sage task creates evidence or decision material that must survive a reboot. The durable
root defaults to `.sage_history/` at the repository root and can be overridden for testing with
`SAGE_HISTORY_ROOT`. It is private, gitignored local state; it is not a system backup.

## Retention Classes

- `scratch`: active locks and leases, temporary clones, fixtures, staging, payload downloads, environment
  snapshots, and reproducible intermediate files. Keep these under `/tmp`.
- `durable-private`: contracts, final handoffs, decision-bearing reviews, external panel packets and responses,
  full chat transcripts used for analysis, accepted or rejected evaluation evidence, closed governance receipts,
  and manifests. Store these under `.sage_history/`.
- `published`: a later curated and redacted subset. Never publish directly from the raw private archive.

An artifact is durable when any of these statements is true:

- a final response cites it;
- it approves, rejects, or materially explains a decision;
- it proves an evaluation or live-run result;
- it contains a worker contract or final handoff;
- resuming accurately after reboot requires its exact bytes.

`/tmp` must not be the source of truth for a durable artifact.

## Commands

Initialize or inspect the private history:

```bash
.venv/bin/python skills/sage-artifact-retention/scripts/artifact_retention.py init
.venv/bin/python skills/sage-artifact-retention/scripts/artifact_retention.py manifest
```

Allocate a durable path for a tool that cannot import the helper:

```bash
.venv/bin/python skills/sage-artifact-retention/scripts/artifact_retention.py path --category transcripts/native-chat --name request.json
```

After that tool writes the file or directory, append its hashes to the manifest:

```bash
.venv/bin/python skills/sage-artifact-retention/scripts/artifact_retention.py record --category transcripts/native-chat --artifact-type native-chat-transcript /path/returned/above
```

Write a structured handoff from a JSON file or standard input:

```bash
.venv/bin/python skills/sage-artifact-retention/scripts/artifact_retention.py write-json --category handoffs --name handoff.json --artifact-type task-handoff --input /path/to/handoff.json
```

Copy explicitly reviewed temp artifacts into durable history:

```bash
.venv/bin/python skills/sage-artifact-retention/scripts/artifact_retention.py promote --category migrated/reviews --artifact-type external-review --context 'approved retention migration' /tmp/example-review.md
```

Promotion is copy-only. It refuses symlinks, files larger than 100 MiB, and names shaped like credentials,
environment files, private keys, or payloads. Do not bypass that refusal by renaming sensitive material.

## Manifest

`.sage_history/manifest.jsonl` is an append-only, machine-readable index. Each record binds an artifact or
promoted directory to its relative path, SHA-256, size, type, source path when applicable, and recording time.
Raw history remains operator-sensitive even when obvious auth files are excluded.

## Completion Guard

The project `Stop` and `SubagentStop` hooks run `retention_guard.py`. It scans only a bounded tail of the current
transcript, warns about existing high-value `/tmp` paths that lack a matching manifest record, and never copies
anything automatically. Treat it as a guardrail, not a complete parser or a substitute for the retention rule.

## Scripts

- `scripts/artifact_retention.py`: path allocation, private JSON writes, explicit promotion, and manifest queries.
- `scripts/retention_guard.py`: bounded completion-time warning for unpromoted high-value temp artifacts.
