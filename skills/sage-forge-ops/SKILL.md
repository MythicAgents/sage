---
name: sage-forge-ops
description: Repo-local Sage Forge/Codex helper workflow. Use when Codex, Claude Code, or an operator needs to run Sage-specific Forge helper automation or preserve Forge operational scripts outside Plans.
---

# Sage Forge Ops

Use only for Sage development helper workflows. Sage offensive-security code must use the cyber-capable model
configuration documented in `CLAUDE.md`; do not change global Forge defaults from this skill.

## Bundled Scripts

- `sage_forge.sh`

Run from `/home/john/dev/sage`:

```bash
/bin/bash skills/sage-forge-ops/scripts/sage_forge.sh
```
