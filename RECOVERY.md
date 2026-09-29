# Inspect before recovery or disk removal

The migration stops on the first error. It does not automatically delete, unlock or restart on failure. Remote `dd`, Proxmox tasks or in-guest FreePBX commands may outlive a disconnected controller. Do not restart another copy until those operations have finished and ownership is clear.

## Establish the actual state

1. Read `/var/lib/pve-safe-migrate/<run>/<ID>.json` and `migration.log` on the source. The JSON contains the original guest configuration, source/target names, guest type, initial running state, volume names/sizes/source UUIDs, created target UUIDs, hashes and last recorded phase. A phase is written **before** its operation; a crash can leave a completed operation with the earlier phase. It is evidence, not the authority for current ownership.
2. Restore/check cluster quorum and inspect Proxmox tasks on both nodes. Check for active `dd`, `pv`, SSH writers, shutdown/start tasks and `fwconsole stop` inside the guest. Stop or wait for the actual task as appropriate; disconnecting the client is not proof it stopped. Inspect kernel/storage errors before using either copy.
3. Locate the only guest configuration under `/etc/pve/nodes/<node>/qemu-server/<ID>.conf` for a VM or `/etc/pve/nodes/<node>/lxc/<ID>.conf` for a container. Confirm both nodes agree. If it is missing, duplicated or cluster state is uncertain, do not unlock or start anything.
4. Inspect `qm status <ID>` or `pct status <ID>` on the owning node and the actual LV UUIDs on each node:

```bash
lvs --units b -o lv_name,lv_uuid,lv_size,lv_attr,pool_lv pve
```

Use the VG recorded in the JSON if it is not `pve`. Do not infer disk identity solely from a reused `vm-<ID>-disk-N` name.

## Configuration is still on the source

The source is the authoritative guest. Original LVs were not overwritten by the transfer, although they might have been deactivated just before handoff. Do not use partial target copies.

After verifying that all transfer/shutdown tasks have ended, the target guest is not running and the configuration still belongs to the source, use the matching CLI there:

```bash
# VM, on the owning source only:
qm unlock <ID>
qm start <ID>

# Container, on the owning source only:
pct unlock <ID>
pct start <ID>
```

Start only if restoring service is intended. For a guest still running after failed shutdown, do not issue another start; inspect FreePBX and any pending stop command before manually restoring phone services. Normal boot should start FreePBX, but check it.

Before retrying migration, inspect/remove each partial target LV individually. Match its UUID to `created` in the journal, confirm that no guest on the target references it and no process is using it, then run `lvremove VG/LV` **on the target only** for that exact LV. Do not use `-y` or a loop. If the crash occurred at `creating-volume`, a new LV can exist without a recorded target UUID; determine ownership from tasks, logs and current storage state instead of guessing. The script will refuse any leftover target LV.

## Configuration is on the target

Treat the target as authoritative even if the last journal phase is `handoff-pending`. The source volumes were deactivated before handoff. Do not copy the configuration back or start the old disks while the target might have run.

Check target disks/UUIDs, resources and task status. If no operation is active and the target configuration still has this migration lock, unlock with the matching `qm`/`pct` CLI on the target. Correct startup compatibility/resource errors there, then start there if appropriate. Resource changes may have been applied partially; compare the original configuration and planned `settings` in the JSON. Keep the source copy until application health and backups are verified.

If the target has written any data, the source disks are stale. Moving back requires a new, deliberate data recovery/migration decision; moving just the configuration back can lose all target-side writes.

## Retiring retained source volumes after successful migration

First verify application health on the current owning node and take/test a new backup. Retain the original disks for an appropriate recovery period. Then, for **each exact source volume**:

1. Confirm the journal says `complete`, but also locate the current guest configuration and confirm it no longer belongs to the original source. If the guest moved again, resolve its full history first.
2. Match the source LV UUID and size to `guest.volumes` in the record. Confirm the volume is inactive, unused and not referenced by another configuration. A matching name alone is insufficient.
3. Confirm the current owning guest uses a different physical copy and that its data is backed up.
4. On the original source only, run `lvremove VG/LV` for that exact confirmed LV and read the confirmation prompt. Record its removal with the migration records. Never remove the target's active disk or use an old cleanup list blindly.

There is intentionally no automated destructive cleanup or forced unlock/resume command. Retained disks can block a later migration back to the original node until their history is resolved.
