# sage_structural_range_phase17r1

Versioned Phase 17R1 source-repair candidate for the Phase 16 structural families. It preserves the accepted
`sage_structural_range` role unchanged and is not accepted, sealed, or authorized for live use.

The role keeps family-specific naming in range configs and implements only the reusable mechanics that are not
already covered by `badsectorlabs.ludus_windows_utils` or `badsectorlabs.ludus_adcs`:

- `directory`: optional foothold creation, controlled GPO setup, machine-account `WriteDacl`, exact DCSync ACEs,
  and object-scoped LAPS read edges.
- `directory-kdc-cert`: run the directory mechanics and then the KDC certificate enrollment on the same DC.
- `proof-target`: fast Group Policy refresh plus a read-only `SageProof` share used by the existing remote GPO
  verifier.
- `kdc-cert`: enterprise CA trust refresh and KDC certificate enrollment on a DC after ADCS deployment.

This is not a strategy layer. It does not encode branch order, objective answers, or GOAD-specific names.

For `directory`, `directory-kdc-cert`, and `kdc-cert`, callers must supply a bare
`sage_structural_domain_admin` plus explicit `sage_structural_credential_realm_netbios` and
`sage_structural_credential_realm_fqdn` values. The role constructs one down-level principal and requires an
exact `ansible.windows.win_whoami` account/domain/logon-realm match before either mutating block executes.
