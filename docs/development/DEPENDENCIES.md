# Dependencies and reproducibility

Sage pins its Python environment with three files under `Payload_Type/sage/`, each with a distinct job:

| File | Role |
|---|---|
| `requirements.txt` | **Intent** — the packages Sage depends on directly. This is what the container image installs. |
| `requirements-dev.txt` | Pulls in `requirements.txt` and adds test dependencies. Use this for development. |
| `constraints.txt` | **Resolution** — every transitive package pinned to a version a green suite was observed against. |

Install with the constraints applied:

```bash
python3 -m venv .venv
.venv/bin/pip install -r Payload_Type/sage/requirements-dev.txt -c Payload_Type/sage/constraints.txt
```

## Why the `-c` flag matters

The `-c` flag is what makes your environment match the one the tests passed on, and match the image. Without it,
pip resolves transitive dependencies fresh every install, so two clones a week apart can end up with different
transitive versions — and a transitive bump can remove or rename a module Sage imports.

A single transitive bump is enough: `mcp` 2.0.0 removed a module Sage imports, which breaks test collection
even though no manifest changed. Pinning transitives with `constraints.txt` is what prevents that class of silent
breakage.

## Regenerating `constraints.txt`

Regenerate after any intentional dependency change, and only from a venv whose test suite is green — otherwise you
pin a broken resolution. The exact regeneration command lives in the header of `constraints.txt` itself.
