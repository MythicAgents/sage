+++
title = "Security and Boundaries"
chapter = false
weight = 90
+++

Sage is a control-plane agent. These boundaries are structural, not advisory.

{{% notice warning %}}
Use Sage only for activity you are explicitly authorized to perform. It is under active development, and its
behavior in a production environment is not yet well understood: an autonomous agent driving a C2 carries real
risk of unintended impact on a live network. Weigh production use very carefully.
{{% /notice %}}

## Execution boundary

- Target-facing actions execute through **Mythic payload tasks only**. The Sage process may reason over Mythic
  and BloodHound control-plane data, but it must not connect directly to target LDAP, SMB, Kerberos, WinRM, RPC,
  HTTP, or similar services.
- Sage is not an implant and creates no Sage callback. A direct Sage-process connection to a target service is a
  boundary violation even when it is only intended as a verifier or fallback.

## Evidence boundary

- Objective proof comes from Mythic task output or artifacts, Mythic credential-store state, or BloodHound facts
  derived from payload-collected artifacts. Sage-local attack artifacts are not admissible proof.
- No capability effect or objective completion may be recorded from Sage-host target I/O or Sage-local artifact
  generation. See [The Engagement Ledger](/agents/sage/engagement-ledger/) for how proof is enforced.

## Livelock detection

Sage includes structural guards against delegation loops that would otherwise spin indefinitely:

- **No-progress backstop.** Halts after 3 consecutive delegations where a guarded tool was attempted but no
  Mythic task was issued. The halt message distinguishes a genuine stall from operator-denied actions.
- **Neutral-delegation soft cap.** Warns the operator after 6 consecutive delegations that return content but
  neither attempt a guarded tool nor issue a task. Does not halt — the operator decides.
- **Pair-bounce detector.** Warns when the same agent is delegated 3 times in a row without progress,
  catching Supervisor ↔ sub-agent routing loops.

## Handling secrets

- `read_credentials` can place raw secrets into the model context and traces; use it deliberately.
- Never put credentials in tracked files. Use Mythic user secrets, the per-chat configuration, process
  environment variables, or a secret manager.
