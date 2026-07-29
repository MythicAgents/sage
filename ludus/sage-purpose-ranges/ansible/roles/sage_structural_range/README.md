# sage_structural_range

Narrow local Ludus role for the sealed Phase 16 structural families.

The role keeps family-specific naming in range configs and implements only the reusable mechanics that are not
already covered by `badsectorlabs.ludus_windows_utils` or `badsectorlabs.ludus_adcs`:

- `directory`: optional foothold creation, controlled GPO setup, machine-account `WriteDacl`, exact DCSync ACEs,
  and object-scoped LAPS read edges.
- `directory-kdc-cert`: run the directory mechanics and then the KDC certificate enrollment on the same DC.
- `proof-target`: fast Group Policy refresh plus a read-only `SageProof` share used by the existing remote GPO
  verifier.
- `kdc-cert`: enterprise CA trust refresh and KDC certificate enrollment on a DC after ADCS deployment.

This is not a strategy layer. It does not encode branch order, objective answers, or GOAD-specific names.
