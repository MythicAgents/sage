# sage_replication_range

Local Ludus role for the Sage replication purpose range.

## Modes

- `domain`: runs on the DC after the base AD content and GPO exist. It moves
  `SRV02` into the GPO-linked OU, sets the GPO foreground sync baseline, and
  grants the foothold user the two domain-root extended rights that BloodHound
  projects as direct `DCSync`.
- `gpo-target`: runs on `SRV02`. It configures a one-minute Group Policy refresh
  interval and the read-only `SageProof` share used by Sage's remote GPO proof
  verifier.

The role is intentionally narrow. Base AD content, the controlled GPO, firewall
settings, and the foothold local group membership stay in
`badsectorlabs.ludus_windows_utils`.
