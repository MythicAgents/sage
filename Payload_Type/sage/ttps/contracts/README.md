# Executable contracts (machine-consumed enforcement data)

This directory holds **deterministic executable contracts** (`*.json`), one per enrolled tool,
resolved at runtime by `load_contract_for_artifact` (globbed by SHA-256, filename is **not**
identity). They are **enforcement authority, not prose** — they are never shown to the model.

They live under `ttps/` to sit next to the knowledge-base cards that reference them
(`binary_contract: ttps/contracts/<tool>.json`), but in their own namespace so:

- the knowledge-base loader (`ttp_library.py`, which globs `ttps/*.md` non-recursively) never sweeps
  them into prompt/retrieval context;
- the contract glob stays scoped to `ttps/contracts/*.json`;
- their governance stays distinct: a contract is SHA-256-pinned enforcement derived from inspecting the
  actual binary, not advisory tradecraft guidance.

Do not hand-author these. They are built from inspected `--help` metadata (see
`docs/development/ARTIFACT_INSPECTION.md`). A `.md` card under `ttps/` is the human/model-facing
knowledge; the `.json` here is the machine-facing grammar for the same tool.
