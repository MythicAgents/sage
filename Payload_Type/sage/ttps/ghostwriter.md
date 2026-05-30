---
name: Ghostwriter
category: command-and-control
subcategories: [red-team-operations, project-management, reporting, c2-integration]
tradecraft_tags: [red-team, project-management, reporting, c2-integration, infrastructure, specterops]
mitre_attack: []
source:
  url: https://github.com/GhostManager/Ghostwriter
  license: BSD-3-Clause
  maintained: true
binary_type: none
binary_filename: ""
supported_os: [linux]
architecture: [x64]
privilege_required: none
network_required: false
detection_signal: |
  Operator-side infrastructure — no target detection signal.
usage_examples:
  - description: Ghostwriter is operator-side infrastructure
    args: "(operator platform — not a tool deployed against targets)"
opsec_notes: |
  Ghostwriter is the red team operations management platform developed by SpecterOps
  researcher Christopher Maddalena (@chrismaddalena). It provides campaign management,
  client database, report generation, and importantly — C2 integration for automatic
  activity logging from Cobalt Strike, Mythic, and other C2s.
gotchas: |
  Infrastructure-side tool. Not relevant to target-side operations. Documented for
  completeness as it's a key part of professional red team infrastructure.
  The Mythic integration is relevant: Ghostwriter can auto-log activity from Mythic
  agents, providing a complete operation log for report generation.
related_ttps: [post-exploitation-playbook, opsec-checklist]
alternatives: [dradis, faraday, manual-reporting]
common_args: {}
last_updated: 2026-05-29
---

# Ghostwriter

Christopher Maddalena's (@chrismaddalena) red team operations management platform.
Provides project management, client database, findings management, and report generation
for professional penetration testing and red team engagements.

## Key Features Relevant to Sage/Mythic Operations

### C2 Integration
Ghostwriter has native integration with:
- Cobalt Strike (automatic event logging)
- Mythic (via webhook — automatic agent activity logging)
- Custom C2 frameworks via webhook API

When configured with Mythic, Ghostwriter automatically logs:
- Agent callbacks
- Task execution
- File uploads/downloads
- Operator commands

### Report Generation
- DOCX/PPTX report templates
- Automated finding severity classification
- Evidence management (screenshots, command output)

### Operational Relevance
For Sage engagements:
- Ghostwriter + Mythic integration provides automatic operation timeline
- All Apollo/Athena agent activity logged without manual note-taking
- Evidence artifacts (downloaded files, screenshots) auto-attached to findings

## Architecture
- Django + PostgreSQL backend
- Docker-compose deployment
- Role-based access control for team operations
- API for programmatic integration
