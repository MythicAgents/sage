---
name: SharpWMIEvent
category: persistence
subcategories: [wmi-event-subscription, persistence, wmi-exec]
tradecraft_tags: [wmi, event-subscription, persistence, dotnet, apollo-runnable, everlasting]
mitre_attack:
  - id: T1546.003
    name: Event Triggered Execution — Windows Management Instrumentation Event Subscription
source:
  url: https://github.com/mdsecactivebreach/SharpSploit
  license: BSD-3-Clause
  maintained: false
binary_type: .net-assembly
binary_filename: SharpSploit.dll
supported_os: [windows]
architecture: [x64]
privilege_required: local-admin
network_required: false
detection_signal: |
  WMI event subscription creation generates Event 5861 (__FilterToConsumerBinding created)
  in the Microsoft-Windows-WMI-Activity operational log. Autoruns detects WMI subscriptions.
  Subscription names appear in: Get-WMIObject __EventFilter, __EventConsumer, __FilterToConsumerBinding.
usage_examples:
  - description: Create a WMI event subscription for persistence (SharpSploit approach)
    args: "(via SharpSploit.Persistence.WMI.CreateFilterConsumerBinding())"
  - description: SharPersist WMI persistence (preferred)
    args: "SharPersist.exe -t wmi -c 'C:\\Windows\\Temp\\payload.exe' -n 'WinUpdate' -m add"
  - description: Enumerate existing WMI subscriptions
    args: "Get-WmiObject -Namespace root\\subscription -Class __EventFilter"
  - description: Remove WMI subscription (cleanup)
    args: "SharPersist.exe -t wmi -n 'WinUpdate' -m remove"
opsec_notes: |
  WMI event subscriptions are persistent across reboots — they survive until explicitly
  removed. Subscriptions are stored in the WMI repository on disk, making them a
  persistent forensic artifact. Autoruns (Sysinternals) enumerates them; IR teams
  specifically check WMI subscriptions during investigations.
  SharPersist provides managed WMI persistence with cleanup. Prefer scheduled tasks
  for shorter-term persistence; use WMI only when scheduled tasks are monitored.
gotchas: |
  WMI subscriptions are VERY persistent — they survive reboots, user logoffs, and
  agent deaths. Always clean up with the `remove` action. The subscription fires based
  on the WMI event filter (e.g. every 60 minutes, or at logon) — choose the trigger
  carefully. WMI repository corruption from failed subscriptions can cause system issues.
related_ttps: [sharpersist, seatbelt]
alternatives: [sharpersist-wmi, powersploit-wmi-persist]
common_args: {}
last_updated: 2026-05-29
---

# SharpWMIEvent / WMI Event Subscription Persistence

WMI event subscription persistence creates a permanent event handler in the WMI
repository that fires on a specified condition (time, system event, user logon).
This executes arbitrary code each time the trigger condition is met — surviving
reboots indefinitely until explicitly removed.

## How WMI Persistence Works

WMI persistence requires three objects:
1. **EventFilter** — condition that triggers (e.g. every 60 seconds, at logon)
2. **EventConsumer** — action to take (execute command/script)
3. **FilterToConsumerBinding** — connects filter to consumer

All three must be created and are all detectable.

## SharPersist Implementation (Preferred)

SharPersist handles all three objects:
```
SharPersist.exe -t wmi -c 'C:\Windows\Temp\payload.exe' -n 'WinUpdate' -m add
SharPersist.exe -t wmi -n 'WinUpdate' -m remove   (cleanup)
```

## Cleanup

```powershell
# Enumerate:
Get-WmiObject -Namespace root\subscription -Class __EventFilter
Get-WmiObject -Namespace root\subscription -Class __EventConsumer  
Get-WmiObject -Namespace root\subscription -Class __FilterToConsumerBinding

# Remove all three:
Get-WmiObject -Namespace root\subscription -Class __EventFilter -Filter "Name='WinUpdate'" | Remove-WmiObject
# Repeat for __EventConsumer and __FilterToConsumerBinding
```

## Comparison with Other Persistence Methods

| Method | Persistence duration | Detection profile | Requires |
|--------|---------------------|------------------|---------|
| Registry Run key | Until deleted | High — Autoruns | user |
| Scheduled task | Until deleted | High — Autoruns, Events | user/admin |
| WMI subscription | Until deleted | Very high — Autoruns, Events, IR | admin |
| Print processor DLL | Until deleted | Medium — rare to check | admin |
