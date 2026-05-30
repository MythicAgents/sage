---
name: SharpClipboard
category: collection
subcategories: [clipboard-monitoring, credential-harvest, data-collection]
tradecraft_tags: [clipboard, collection, credential-harvest, monitoring, dotnet, apollo-runnable]
mitre_attack:
  - id: T1115
    name: Clipboard Data
source:
  url: https://github.com/bing0o/SharpClipboard
  license: Unknown
  maintained: false
binary_type: .net-assembly
binary_filename: SharpClipboard.exe
supported_os: [windows]
architecture: [x64]
privilege_required: user
network_required: false
detection_signal: |
  Clipboard access from non-standard processes is detectable by behavioral EDR.
  Clipboard monitoring (polling GetClipboardData) is suspicious when done by
  non-UI applications. Sysmon doesn't directly log clipboard access.
usage_examples:
  - description: Read current clipboard contents once
    args: "SharpClipboard.exe"
  - description: Monitor clipboard for a period (capture password manager pastes)
    args: "SharpClipboard.exe monitor 60"
opsec_notes: |
  Clipboard monitoring is valuable for capturing password manager pastes (users copy
  passwords from their vault to paste into applications). A brief monitoring window
  (30-60 seconds) during active user session can capture credentials passively.
  Best used during interactive user sessions (RDP, active workstation).
gotchas: |
  Clipboard contents are session-specific — must run in the same Windows session
  as the target user. From a service account or SYSTEM context, the clipboard may
  not contain the user's data. Clipboard access from a background process is detectable
  by some EDR vendors. Run briefly and stop rather than persistent monitoring.
related_ttps: [seatbelt, sharpchromium, credential-hunting-checklist]
alternatives: [seatbelt-clipboard, manual-powershell-clipboard]
common_args:
  monitor:
    description: Monitor clipboard for specified seconds instead of single read
    typical_values: [30, 60]
last_updated: 2026-05-29
---

# SharpClipboard

A .NET assembly for reading or monitoring Windows clipboard contents. Most useful
during active user sessions to capture password manager pastes — when a user copies
a password from their KeePass/Bitwarden and pastes it into an application, a brief
monitoring window captures the credential in plaintext.

## Typical use cases
- Capture password manager credentials during active user sessions
- One-time clipboard snapshot for credentials recently copied

## How Sage uses this
SharpClipboard is a targeted, brief-duration credential collection tool. Sage uses it
when an interactive user session is active and credential capture is needed. Run briefly
(30-60 seconds), capture, then stop — persistent clipboard monitoring is noisier.

## Output
Text content of the clipboard at time of read, or continuous output with timestamps
in monitor mode.
