# Sage Structural Family A — Phase 17R1 Source Candidate

Versioned successor Ludus source blueprint for independent review of the Phase 17R1 runas repair. The accepted
`sage-structural-a` source remains unchanged; this candidate is not accepted, sealed, provisioned, countable, or
authorized for live use.

The source preserves the Phase 16R candidate physical map:

| VM | Hostname | Domain | Role |
|---|---|---|---|
| `{{ range_id }}-DC01` | `marble-dc01` | `marble.local` | root DC |
| `{{ range_id }}-DC02` | `larch-dc01` | `larch.marble.local` | child DC |
| `{{ range_id }}-DC03` | `ivory-dc01` | `ivory.marble.local` | sibling child DC |
| `{{ range_id }}-DC04` | `onyx-dc01` | `onyx.partner.local` | partner forest DC |
| `{{ range_id }}-WS01` | `marble-ws01` | `marble.local` | foothold |
| `{{ range_id }}-SRV01` | `n01` | `larch.marble.local` | GPO branch host |
| `{{ range_id }}-SRV02` | `n02` | `onyx.partner.local` | ADCS branch host |

The low-privileged foothold is `MARBLE\analyst1`. The source creates only the mechanics needed for the candidate
branches: the controlled child-domain GPO `saffron-policy` with machine-account `WriteDacl`, one read-only proof
share, and one partner-forest ESC1 CA path.

This source is defined only. It is not independently accepted, provisioned, callback-ready, countable, or
authorized for live execution.

Runas-bearing placements declare the authentication realm separately from the target realm. The Larch child DC
uses the Marble parent credential realm, while the Onyx primary DC uses its own realm. The shared successor role
validates the effective token before directory or KDC mutation.
