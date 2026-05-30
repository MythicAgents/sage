---
name: SharpCloud
category: recon
subcategories: [cloud-credential-discovery, azure-cred, aws-cred, gcp-cred]
tradecraft_tags: [cloud, azure, aws, gcp, credentials, dotnet, metadata-service, apollo-runnable]
mitre_attack:
  - id: T1552.005
    name: Unsecured Credentials — Cloud Instance Metadata API
source:
  url: https://github.com/chrismaddalena/SharpCloud
  license: Unknown
  maintained: false
binary_type: .net-assembly
binary_filename: SharpCloud.exe
supported_os: [windows]
architecture: [x64]
privilege_required: user
network_required: true
detection_signal: |
  Cloud metadata API calls (169.254.169.254 or equivalent per-provider) from unexpected
  processes are detectable. AWS/Azure/GCP metadata API access is logged on the cloud
  provider side. Local credential file reads (AWS ~/.aws/credentials, Azure ~/.azure/)
  generate file access events.
usage_examples:
  - description: Check all cloud provider credentials on the host
    args: "SharpCloud.exe"
  - description: Check Azure instance metadata service
    args: "SharpCloud.exe azure"
  - description: Check AWS credentials and metadata
    args: "SharpCloud.exe aws"
  - description: Check for GCP metadata and credentials
    args: "SharpCloud.exe gcp"
opsec_notes: |
  Cloud metadata API access is logged by cloud providers (AWS CloudTrail, Azure Monitor,
  GCP Cloud Logging). If the compromised host is a cloud VM, querying the instance
  metadata API will generate access logs in the cloud tenant. For AWS, SSRF via the
  metadata API (169.254.169.254) is the primary attack path; for Azure managed identities,
  the IMDS endpoint (169.254.169.254/metadata/identity/oauth2/token) provides tokens.
gotchas: |
  SharpCloud is not actively maintained (~2019). Cloud credential locations and metadata
  API formats have evolved. For AWS/Azure/GCP, check for:
  1. Instance metadata service credentials (IAM role, managed identity)
  2. Credential files on disk (~/.aws/credentials, service account JSON files)
  3. Environment variables with cloud API keys
  Seatbelt's EnvironmentVariables check covers env-var credentials. SharpCloud handles
  the metadata API path.
related_ttps: [seatbelt, roadtools, azurehound, credential-hunting-checklist]
alternatives: [manual-curl-imds, seatbelt-env, cloud-provider-cli]
common_args:
  azure:
    description: Check Azure instance metadata service and credential files
    typical_values: [flag-only]
  aws:
    description: Check AWS instance metadata and ~/.aws/credentials
    typical_values: [flag-only]
  gcp:
    description: Check GCP metadata service and service account JSON files
    typical_values: [flag-only]
last_updated: 2026-05-29
---

# SharpCloud

A .NET assembly that discovers cloud provider credentials on a Windows host —
both from local credential files and from the cloud instance metadata service (IMDS).
When a compromised Windows host is a cloud VM (Azure, AWS, GCP), SharpCloud extracts
cloud credentials for lateral movement into the cloud tenant.

## Cloud Credential Sources

### AWS
- Instance metadata: `http://169.254.169.254/latest/meta-data/iam/security-credentials/`
- Credential file: `C:\Users\<user>\.aws\credentials`
- Environment: `AWS_ACCESS_KEY_ID`, `AWS_SECRET_ACCESS_KEY`

### Azure
- Instance metadata: `http://169.254.169.254/metadata/instance`
- Managed identity token: `http://169.254.169.254/metadata/identity/oauth2/token?resource=https://management.azure.com/`
- Credential files: `C:\Users\<user>\.azure\`

### GCP
- Instance metadata: `http://metadata.google.internal/computeMetadata/v1/instance/service-accounts/default/token`
- Service account JSON: typically in `C:\Users\<user>\.config\gcloud\`

## How Sage uses this
When a compromised host appears to be a cloud VM (check for cloud metadata routes
in netstat output or the presence of cloud agent processes), SharpCloud is run to
discover cloud credentials for cloud tenant enumeration (AzureHound, ROADtools for Azure;
AWS CLI for AWS).

## Apollo-specific note
.NET assembly — Apollo inline_assembly compatible.
