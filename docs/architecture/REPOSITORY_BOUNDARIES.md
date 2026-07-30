# Sage Repository Boundaries

Sage is maintained as a monorepo, but the repository is not one deployable unit. The Mythic chat capability,
developer experiments, evaluation machinery, range definitions, operator workflows, and generated evidence have
different trust and release boundaries.

The governing dependency direction is:

```text
range definitions ─┐
evaluation tools ──┼──> public Sage interfaces
operator skills ───┘
                          │
                          v
                 Mythic Sage runtime
```

The arrow does not reverse. Product runtime code must not import range definitions, evaluation packages,
operator skills, private plans, or generated evidence.

## Ownership map

| Location | Ownership | Shipped in Sage image | May import product code |
|---|---|---:|---:|
| `Payload_Type/sage/main.py` | Mythic container entry point | Yes | Yes |
| `Payload_Type/sage/sage_chat/` | Native Mythic v4 chat integration | Yes | Yes |
| `Payload_Type/sage/ai/langgraph/` | Product orchestration, capabilities, execution, and verification | Yes | Yes |
| `Payload_Type/sage/ai/trajectory/` | Runtime failure recording and repair-policy bridge | Yes | Yes |
| `Payload_Type/sage/prompts/` | Product prompts | Yes | N/A |
| `Payload_Type/sage/ttps/` and `tools/` | Product tradecraft and tool metadata | Yes | N/A |
| `Payload_Type/sage/ai/hillclimb/` | Development and policy experimentation | No | Yes |
| `Payload_Type/sage/evals/` | Evaluation harnesses and fixtures | No | Yes |
| `Payload_Type/sage/tests/` | Offline verification | No | Yes |
| `skills/` | Operator and developer workflows | No | Yes |
| `ludus/` | Range definitions and provisioning | No | No product import |
| `Plans/` | Private, temporary planning and handoff state | No | No product import |
| `.phoenix/`, `.trajectory/`, `.sage_engagement/`, `.hillclimb/` | Generated runtime or evaluation evidence | No | Runtime data only |

`ai/trajectory` is intentionally on the product side of the boundary today because `execute_capability` uses its
runtime bridge. Offline corpus construction, replay experiments, and promotion research should migrate out of
that package if they can be separated without creating a product-to-development import.

## Packaging enforcement

Mythic builds Sage with `Payload_Type/sage/` as the Docker context. The Dockerfile may continue to use `COPY . .`
only because `.dockerignore` excludes development-only code, tests, local configuration, runtime databases,
retained evidence, and nested repository metadata.

Adding a new top-level directory beneath `Payload_Type/sage/` therefore requires an explicit decision:

1. If Sage imports it at runtime, document it in the ownership map and allow it into the image.
2. If it supports development or evaluation, add it to `.dockerignore` and keep imports one-way.
3. If it contains generated state or secrets, gitignore it and docker-ignore it.

The boundary tests in `Payload_Type/sage/tests/test_repository_boundaries.py` verify the current contract.

## Why this remains one repository

The product, capability contracts, operator workflows, and evaluators often need atomic changes. Keeping them in
one repository preserves reproducibility and makes interface drift visible in one review. A repository split is
justified only if at least one of these becomes true:

- the components have independent release owners or incompatible release cadences;
- access-control or licensing requirements prohibit co-location;
- dependency isolation cannot be enforced by packaging and tests;
- evaluation history makes ordinary product review or distribution operationally impractical.

Until then, enforced boundaries provide the useful properties of separate repositories without cross-repository
version pinning and synchronized pull requests.

## Incremental migration

Do not perform a wholesale move while the runtime and demo workflow are changing. Use three stages:

1. Enforce import, packaging, documentation, and test-tier boundaries in the existing tree.
2. Move development-only packages to top-level `evaluation/` or `development/` directories in small,
   mechanically verified changes.
3. Reassess a repository split using the criteria above after the physical migration is stable.

Historical sealed or rejected evaluation bundles are append-only evidence. Moving or rewriting them merely to
produce a cleaner tree is not an acceptable migration.
