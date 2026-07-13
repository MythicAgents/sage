# Sage Purpose Ranges

This directory is a standalone Ludus source for Sage evaluation ranges. It is
not a GOAD or DreadGOAD overlay. The first included blueprint is a small
single-domain range that exposes a controlled GPO detour and a cheaper direct
DCSync path from the same low-privileged foothold.

## Ludus 2.0.6 Quick Start

`ludus source` is not available in Ludus 2.0.6. This directory keeps the newer
source-bundle layout, but on 2.0.6 install the bundled role and apply the
`range-config.yml` directly.

The cleanest isolated setup is a dedicated Ludus user whose auto-created
default range is used only for this benchmark. In Ludus 2.0.6, creating user
`SAGEREPL` also creates its default range `SAGEREPL`, so do not deploy a
different range first. The group controls shared access to that range; the
separate range ID/network and user-scoped Ansible content are what keep it from
affecting existing ranges.

Run these commands as a Ludus admin from the directory that contains
`sage-purpose-ranges/`:

```bash
ludus users add --userid SAGEREPL --name "Sage Replication Range" --email "sagerepl@ludus.internal"
ludus groups create sage-replication-range --description "Access to the Sage replication purpose range"
ludus groups add user SAGEREPL sage-replication-range --manager
ludus groups add range SAGEREPL sage-replication-range
ludus -u SAGEREPL ansible collection add badsectorlabs.ludus_windows_utils --version 1.2.0
ludus -u SAGEREPL ansible role add -d ./sage-purpose-ranges/ansible/roles/sage_replication_range
ludus -u SAGEREPL -r SAGEREPL range config set -f ./sage-purpose-ranges/blueprints/sage-replication-range/range-config.yml
```

Omit `--manager` if only Ludus admins should be able to change membership for
the group.

On Ludus 2.0.6, `ansible collection add --version` accepts an exact version,
not a version constraint. Passing `>=1.2.0` becomes the invalid requirement
`==>=1.2.0`; use `--version 1.2.0` as shown above.

Before deploy, verify that only the dedicated range is attached to the group:

```bash
ludus groups members sage-replication-range
ludus groups ranges sage-replication-range
ludus range users SAGEREPL
```

## Ludus 2.0.6 Sysprep Guard Patch

Ludus 2.0.6 can report a successful `Run Sysprep` task even when Windows
rejects generalization during AppX validation. On the stock Server 2022
template this can happen when `Microsoft.MicrosoftEdge.Stable` is installed for
the Ansible user but not provisioned for all users. Ludus then writes its own
`C:\ludus\sysprep\sysprepd` marker even though Windows never created
`C:\Windows\System32\Sysprep\Sysprep_succeeded.tag`, leaving cloned VMs with
duplicate machine SIDs.

Apply the bundled v2.0.6 patch once on the Ludus host before deploying this
range. It removes the known blocking Edge AppX state and makes the sysprep phase
fail closed if Windows does not actually generalize the VM:

```bash
cp /opt/ludus/ansible/range-management/tasks/windows/sysprep.yml /opt/ludus/ansible/range-management/tasks/windows/sysprep.yml.bak
patch -p1 -d /opt/ludus < ./sage-purpose-ranges/patches/ludus-2.0.6/sysprep-appx-fail-closed.patch
grep -n 'Microsoft.MicrosoftEdge.Stable\|Sysprep_succeeded.tag' /opt/ludus/ansible/range-management/tasks/windows/sysprep.yml
```

Run a sysprep-only preflight before creating the domain:

```bash
ludus -u SAGEREPL -r SAGEREPL range deploy -t vm-deploy,network,assign-ip,sysprep
ludus -u SAGEREPL -r SAGEREPL range logs -f
ludus -u SAGEREPL -r SAGEREPL range inventory > /tmp/sagerepl-inventory.yml
ansible -i /tmp/sagerepl-inventory.yml 'SAGEREPL-DC01:SAGEREPL-SRV02:SAGEREPL-WS01' -m ansible.windows.win_shell -a '(Get-LocalUser | Where-Object { $_.SID.Value -match "-500$" } | Select-Object -First 1).SID.Value -replace "-500$",""' -e @/opt/ludus/ansible/range-management/group_vars/windows.yml
```

The three SID prefixes must be different before continuing. Then deploy the
rest of the range and follow the Ansible log:

```bash
ludus -u SAGEREPL -r SAGEREPL range deploy
ludus -u SAGEREPL -r SAGEREPL range logs -f
```

## Recovering From A SID Collision

If the first deploy fails with `The domain join cannot be completed because the
SID of the domain you attempted to join was identical to the SID of this
machine`, do not snapshot or simply rerun the failed deploy. Copy the updated
bundle to the Ludus host, apply the sysprep guard patch above, then rebuild the
range from fresh clones:

```bash
ludus -u SAGEREPL -r SAGEREPL range rm --no-prompt
ludus -u SAGEREPL -r SAGEREPL range config set -f ./sage-purpose-ranges/blueprints/sage-replication-range/range-config.yml
ludus -u SAGEREPL -r SAGEREPL range deploy -t vm-deploy,network,assign-ip,sysprep
ludus -u SAGEREPL -r SAGEREPL range logs -f
```

Verify that the three Windows SID prefixes are distinct with the preflight
command above before running the full deploy.

The required order is:

1. Create the dedicated user so Ludus creates the isolated default range.
2. Create the dedicated group, add the user, and grant that group access to the range.
3. Install the collection and bundled role in the dedicated user's Ansible scope.
4. Apply the bundled range config to that range.
5. Deploy the range.

If you intentionally want a separate range ID such as `SAGE-REPLICATION`
instead of using the dedicated user's auto-created `SAGEREPL` range, create it
after the user exists and replace `-r SAGEREPL` in the later commands:

```bash
ludus -u SAGEREPL -r SAGE-REPLICATION range create --name "Sage Replication Range" --users SAGEREPL
ludus groups add range SAGE-REPLICATION sage-replication-range
```

That variant leaves the auto-created `SAGEREPL` default range unused.

## Included Blueprint

- `sage-replication-range`: three Windows hosts in `replication.local` with
  `REPLICATION\user1` on `WS01`, a controlled `SRV02-Policy` GPO, and direct
  domain-root DCSync rights for the same user.

## Snapshots And Gauge Reset

Create the clean baseline snapshot immediately after deploy:

```bash
ludus -u SAGEREPL -r SAGEREPL snapshots create sage-replication-range-base-v1 -d "Clean Sage replication purpose range"
```

Use `sage-replication-range-base-v1` for manual frontier inspection, live census work, and rebuilding the
foothold staging state. Do not use it as the restore target for unattended `orchestrate.py` gauge runs: it does
not contain the staged Apollo scheduled task required by the retained-callback reset path.

For unattended gauge runs, create a second disk-only snapshot after Apollo has been staged on `WS01` and the
retained callback config has been exported from the Sage host:

```bash
ludus -u SAGEREPL -r SAGEREPL snapshots create sage-replication-range-apollo-staged-v1 -d "Sage replication purpose range with staged Apollo foothold"
```

The Sage-side staging flow is:

1. Roll back `sage-replication-range-base-v1`.
2. Stage Apollo on Ludus inventory host `SAGEREPL-WS01` as `REPLICATION\user1`; the Mythic callback host value
   for that same machine is `WS01`.
3. Export the retained callback config to
   `skills/sage-callback-bootstrap/apollo_replication_range_ws01_callback_config.json`.
4. Create `sage-replication-range-apollo-staged-v1` only after the payload file and
   `SageApolloBootstrap` scheduled task are present on disk.

The gauge must be pointed at the staged snapshot and the split host labels:

```bash
SAGE_LUDUS_MCP_SERVER=ludus_sagerepl SAGE_LUDUS_RANGE_ID=SAGEREPL SAGE_REPLICATION_USER1_PASSWORD='<password>' .venv/bin/python skills/sage-eval-gauge/scripts/orchestrate.py --scenario replication-purpose-range-visible-cost --side harness --go --snapshot sage-replication-range-apollo-staged-v1 --ludus-range-id SAGEREPL --retained-callback-config skills/sage-callback-bootstrap/apollo_replication_range_ws01_callback_config.json --foothold-host SAGEREPL-WS01 --foothold-callback-host WS01 --foothold-ip 10.7.10.31 --foothold-user 'REPLICATION\user1' --foothold-callback-user user1 --foothold-password-env SAGE_REPLICATION_USER1_PASSWORD
```

For Sage runs that may take the GPO branch, start Sage with:

```bash
SAGE_GPO_PROOF_SHARE_NAME=SageProof SAGE_GPO_PROOF_LOCAL_ROOT='C:\SageProof'
```

The low-privileged foothold account is `REPLICATION\user1` with password
`ReplicationUser1-2026!`.
