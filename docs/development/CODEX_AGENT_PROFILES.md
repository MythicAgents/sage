# Optional Codex Agent Profiles

Sage can use project-local Codex profiles to separate cybersecurity execution from evaluation review. These
profiles are development infrastructure; they are not required by the Sage Mythic container and are not copied
into its image.

The current optional package is atomic:

```text
.codex/config.toml
.codex/agents/sage_cyber_executor.toml
.codex/agents/sage_eval_reviewer.toml
skills/sage-cyber-runner/
AGENTS.md
skills/README.md
```

If `.codex/config.toml` enables a named profile, the referenced profile file must be present in the same change.
If `AGENTS.md` documents the process-runner fallback, that skill must also be present. Do not publish only the
configuration references or only the model-pinned profiles.

The profiles name models that may not be enabled for every contributor. This is acceptable for optional
development infrastructure as long as the base Sage runtime and offline tests do not depend on those models.
Contributors without access can omit the entire package locally or provide an approved equivalent profile; they
must not silently run a generic agent under a privileged specialist name.

`test_repository_boundaries.py` checks that enabled profile references resolve. It does not assert model
availability, which remains an account/runtime concern.

## Review Lifecycle

Use `sage_eval_reviewer` for both evaluation lifecycle reviews and high-risk conversation/runtime-authority
reviews. Do not add a second conversation reviewer with the same authority.

Every source-candidate review is bound to an active architecture-governor review lease. The supervisor freezes
the exact staged candidate and named protected paths with `review_lease.py freeze`; the reviewer verifies that
lease at review start, after its declared commands, and before disposition. Candidate, protected-path, complete
index, or HEAD drift yields `INVALIDATED_CANDIDATE_DRIFT`, not ACCEPT or REJECT. Unrelated unstaged worktree
changes are outside the lease unless named as protected paths.

Review contracts must name an `independence_class`:

- `internal_subagent`
- `independent_top_level_session`
- `human_external`

They must report source/tests, artifact acceptance, phase exit, development/live authority, countability, and
promotion separately. An internal source review does not imply any later transition.
