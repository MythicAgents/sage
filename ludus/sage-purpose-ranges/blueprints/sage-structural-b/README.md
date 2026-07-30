# Sage Structural Family B

Standalone Ludus source blueprint for the Phase 16R candidate family `sealed-family-s2`.

The source preserves the Phase 16R candidate physical map:

| VM | Hostname | Domain | Role |
|---|---|---|---|
| `{{ range_id }}-DC01` | `quartz-dc01` | `quartz.local` | root DC |
| `{{ range_id }}-DC02` | `cedar-dc01` | `cedar.partner.local` | partner forest DC |
| `{{ range_id }}-DC03` | `harbor-dc01` | `harbor.local` | peer forest DC |
| `{{ range_id }}-DC04` | `tide-dc01` | `tide.harbor.local` | nested child DC |
| `{{ range_id }}-WS01` | `quartz-ws01` | `quartz.local` | foothold |
| `{{ range_id }}-SRV01` | `n11` | `cedar.partner.local` | LAPS + CA branch host |
| `{{ range_id }}-SRV02` | `n12` | `tide.harbor.local` | nested-domain branch host |

The low-privileged foothold is `QUARTZ\analyst1`. The source creates only the mechanics needed for the candidate
branches: an object-scoped LAPS read edge to the CA host, one partner-forest CA export path, and exact direct
DCSync rights on the nested child domain.

This source is defined only. It is not independently accepted, provisioned, callback-ready, countable, or
authorized for live execution.
