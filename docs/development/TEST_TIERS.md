# Sage Test Tiers

There is one tier.

Run the offline suite from the repository root:

```bash
.venv/bin/python skills/sage-focused-capability-tests/scripts/run_offline_suite.py
```

No suite is excluded. A green run means the tree is green — which is the point of removing the split described
below. The runner still accepts a trailing `supported` argument and ignores it, so older handoff docs and muscle
memory keep working.

## What changed, and why

This file previously documented two tiers. `supported` ran the tree minus four append-only rejected
successor-portfolio suites; `retired` ran only those four and "is not expected to be green."

Those suites froze source hashes of an older product surface, so they could never pass against current code. The
exclusion was therefore permanent, and the default command's name — "supported" — quietly meant "the parts we
still expect to work."

The portfolios are rejected evaluation evidence. `AGENTS.md` § Durable Artifact Retention names `.sage_history/`
as the home for "accepted or rejected evaluation evidence," so they now live at:

```
.sage_history/evaluation/architecture-policy/rejected-successor-portfolios/
```

They are preserved append-only there, as the doctrine requires. They are not deleted and must not be rewritten or
resealed. They are simply not product source: 28k lines of rejected candidates were roughly five times the weight
of the working instruments they were candidates for.

`test_repository_boundaries.py` asserts both halves of this — that the runner carries no exclusion mechanism, and
that no `*successor*portfolio*.py` reappears under `Payload_Type/`.

## Sealed evaluation evidence

Sealed evidence is durable-private state and belongs under `.sage_history/`, never `Plans/` — the maintainer's own
documents, which happen to be gitignored for an unrelated reason. Reading evidence from `Plans/` is what made
three tracked tests pass only on the maintainer's laptop and fail in every clone.

The Phase 16R/17 campaign's evidence and source are archived at `.sage_history/evaluation/architecture-policy/`
(`campaign-source/` for the modules and tests, `rejected-successor-portfolios/` for the rejected candidates).

**Known gap:** six modules still write outputs under `Plans/` — `phase10_evidence_bundle`, `phase12`, `phase13`,
`phase14` (bundle + validator), and `phase15`. Their tests build hermetic `tmp_path/"Plans"` fixtures, so
migrating them means changing both sides together. Until then, do not add new `Plans/` write anchors.

## Scope

For a small change, run the directly affected modules first, then the full suite. Live range checks are a separate
lifecycle and never substitute for offline tests.
