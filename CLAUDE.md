# Sage Assistant Compatibility Guide

@AGENTS.md

`AGENTS.md` is the canonical Sage guide for every assistant. Its authority, target-I/O, secret isolation,
database archival, testing, and live-budget rules apply here.

Read `skills/README.md` to locate a workflow, then the operational skill before executing it.
Claude discovers the committed `.claude/skills/` symlinks. Keep links rather than copied skills to prevent drift.
`.claude/settings.json` carries the project prompt-edit and advisory architecture/retention hooks.
Personal settings belong in the gitignored local configuration and must not be published.
