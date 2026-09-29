# Proxmox sequential VM and container migration

Interactive Bash script for moving QEMU virtual machines and LXC containers between nodes **in the same Proxmox VE cluster**. Run it on the node currently hosting the guests.

Designed for hosts under pressure: one guest migrates at a time, with an explicit bandwidth limit and a configurable pause between guests. This reduces concurrent migration work, but cannot prevent crashes or cap all CPU, RAM, disk IOPS, or storage-backend activity.

## What it does

1. Lists local VMs and containers, including ID, name, and state. Templates are excluded.
2. Lets you select IDs separated by spaces or commas, or `all`. Your entered order is preserved; duplicates are removed. `all` uses ascending ID order.
3. Shows all cluster nodes with online/offline status, RAM used/total/free, RAM usage percentage, logical CPU count, CPU utilization, and busy CPU equivalents. Asks for one online destination for the batch; enter `r` to refresh the table.
4. Asks for transfer bandwidth, time between guests, shutdown timeout, shutdown preparation mode (FreePBX by default), and destination host RAM reserve.
5. Asks for destination RAM and CPU cores **for each guest**, plus swap allowance **for each LXC container**. These values are collected before confirmation and applied **after migration**, before restarting the guest.
6. Displays the full plan and requires you to type `MIGRATE`.
7. For each running guest in FreePBX mode, runs `fwconsole stop` inside it first, then shuts down the OS gracefully. Migrates the guest, applies and verifies resource settings, then starts it if it was originally running. Originally stopped guests remain stopped.
8. Waits for completion and the configured pause before processing the next guest. Stops the batch on the first failure. Refreshes the cluster status table when the batch completes.

**This uses offline migration and requires downtime for the entire transfer.** It does not perform live migration. Shutdown is never forced. A guest that does not shut down within the timeout stops the batch.

## FreePBX 17 shutdown

Use **mode 1 (the default): `fwconsole stop` followed by graceful OS shutdown**. This lets FreePBX stop its services before the OS shutdown that has been hanging on these PBXs. Forced power-off bypasses application shutdown and risks losing in-flight writes. If FreePBX or OS shutdown fails, this script stops for inspection; it does not automatically escalate to forced shutdown.

- **LXC containers:** executes `fwconsole stop` through `pct exec` as container root.
- **QEMU VMs:** executes it through `qm guest exec`. The QEMU Guest Agent must already be installed and running inside the VM, enabled in its Proxmox options, and permit guest command execution as root. No SSH passwords are needed or collected.
- **Preflight:** checks guest root access and the presence of `fwconsole` in every selected running guest before confirmation. These checks do not stop services. If a VM lacks guest-agent access, fix that first, or stop FreePBX and shut down the VM manually from its console, then migrate the already-stopped VM. A guest that starts the batch stopped remains stopped afterward.
- **Timeouts:** `fwconsole stop` gets its own configurable timeout, default 300 seconds, followed by the separate OS shutdown timeout. A VM command that returns a PID without a completed successful exit is treated as unfinished. A timeout may leave the command running inside the guest; inspect it before retrying.
- **Phone service interruption:** active calls can be disconnected as soon as `fwconsole stop` runs. Use a maintenance window. The script does not wait for calls to drain.
- **After boot:** normal guest boot services must start FreePBX/Asterisk. Verify service status, trunk/extension registration and a test call. The script verifies the guest is running, not that phone services are healthy; it does not blindly issue a second `fwconsole start` during boot.

Mode 2 skips the FreePBX command for generic guests and still uses graceful shutdown. The selected mode applies to the entire batch; migrate mixed FreePBX/non-FreePBX guests in separate batches. Stopped guests never receive guest commands.

Command references: [Sangoma fwconsole documentation](https://sangomakb.atlassian.net/wiki/spaces/PG/pages/41779247) and [Proxmox qm guest exec](https://pve.proxmox.com/pve-docs/qm.1.html).

## Requirements and preparation

- Root access to the source Proxmox VE node, Bash 4.3+, `qm`, `pct`, `pvesh`, Python 3, `flock`, and GNU `timeout` (normally available on Proxmox).
- A healthy, quorate cluster with working inter-node connectivity and migration permissions.
- Compatible destination storage, sufficient disk space, network bridges, CPU features, and any required device mappings. Storage IDs are preserved; the script does not remap storage or bridges. Proxmox performs storage-specific migration checks, which can fail after shutdown.
- Review local disks, mounted ISOs, PCI/USB passthrough, container bind mounts and device mounts. Host directory/device contents are not automatically copied by this script. Do not force a migration past a Proxmox compatibility error.
- Resolve guest locks and pending configuration changes first. HA-managed guests are refused: use the Proxmox HA workflow for those guests.
- Have current backups. Use a maintenance window and a persistent terminal such as `tmux` if available. Test with one noncritical guest first.

Avoid simultaneous backups, replication bursts, manual migrations, or other migration scripts while using this tool. Its lock prevents another copy on the **same source node**, not migrations launched elsewhere in the cluster. Do not edit guest configuration during the batch.

## Run

Copy `pve-migrate.sh` onto the source node, then run from its directory:

```bash
bash -n pve-migrate.sh
bash pve-migrate.sh
```

Alternatively:

```bash
chmod +x pve-migrate.sh
./pve-migrate.sh
```

Use `bash pve-migrate.sh --help` for a brief usage message. Do not run with `sh` or pipe the script into Bash: it needs an interactive terminal.

Example inputs:

```text
Guest IDs:                    101, 205, 110
Destination:                  pve02
Bandwidth in MiB/s:           10
Pause between guests:         30
Shutdown timeout:             180
Shutdown preparation mode:    1 (FreePBX)
fwconsole stop timeout:       300
Destination RAM reserve:      1024
Destination RAM for guest 101: 2048
Destination swap (LXC only):   512
Destination cores:            2
...resource prompts repeat for each guest...
Confirmation:                 MIGRATE
```

## Settings

Before each guest's resource prompts, the script reads the selected destination's current RAM and CPU status. It shows a RAM budget calculated as **current free RAM minus the host reserve minus RAM planned for earlier selected guests that will restart**. Before confirmation, the budget includes the entire planned batch. During migration it includes only guests still waiting to start. This estimate helps choose RAM allocations; it is not a reservation or a guarantee of future free memory.

CPU utilization and CPU allocation are different. For example, 25% utilization on 16 logical CPUs is about 4 busy CPU equivalents at that sample, not 12 dedicated cores available to assign. Guest vCPUs share host CPU time, and workloads may become busier after migration. The table does not forecast post-migration CPU load or recommend an exact safe vCPU count. Offline nodes and missing metrics show `N/A`; cluster-wide statistics may lag. Refresh before selecting a destination. The final snapshot may precede guests reaching their normal workload.

| Prompt | Default | Meaning |
| --- | --- | --- |
| Bandwidth | 10 MiB/s | Positive whole number, converted to KiB/s for Proxmox `--bwlimit`; 10 means 10240 KiB/s. Unlimited is intentionally unavailable. |
| Pause | 30 seconds | Time after completing one guest before beginning the next; zero disables the pause. |
| Shutdown timeout | 180 seconds | Graceful shutdown wait; never force-stops a guest. |
| Shutdown preparation | 1 (FreePBX) | Stops FreePBX services before OS shutdown. Choose 2 for standard guests. |
| FreePBX stop timeout | 300 seconds | Maximum wait for `fwconsole stop`; failure/timeout stops the batch. |
| Host RAM reserve | 1024 MiB | Minimum free RAM to leave beyond the planned RAM for guests that will start; minimum permitted reserve is 512 MiB. Increase for your host's needs. |
| Guest RAM | Current setting | MiB; 1024 MiB = 1 GiB. Enter accepts the displayed value. |
| Container swap | Current setting (Proxmox default: 512 MiB if unset) | Swap allowance in MiB. Enter keeps the displayed value; 0 sets no swap allowance. Applied and verified after migration. |
| VM cores | Current setting | Cores **per socket**. Socket count is preserved and displayed; two sockets times two cores means four virtual CPUs. |
| Container cores | 0 (unchanged) | Enter a positive count to set it, or 0 to preserve the existing limit, including unlimited. |

**RAM reserve is not swap.** The reserve is physical RAM left outside the migration budget for the host. Container swap is an additional allowance to use swap backed by the destination host; it does not create or reserve swap space. For example, 2048 MiB RAM plus 512 MiB swap means a 2 GiB RAM limit and a separate 512 MiB swap allowance. Swap is not added to the script's physical RAM budget. The destination must already have usable swap for containers to use it; the script does not check or change host swap capacity. Heavy swapping can add disk load on an overloaded host.

QEMU VM swap (or a Windows pagefile) is configured inside the guest OS. The script does not prompt for or alter VM swap. See the official [container memory documentation](https://pve.proxmox.com/pve-docs/chapter-pct.html#pct_memory) for container RAM and swap settings.

VM balloon minimum and hotplug `vcpus` are preserved; incompatible reductions are rejected before migration. Adjust those settings separately if needed. CPU counts entered here cannot exceed the destination logical CPU count (including sockets for VMs). CPU allocation is not a host-load guarantee; unchanged unlimited containers can still use substantial CPU.

The RAM check uses destination-reported free memory and the configured RAM of selected guests that will be restarted, plus the reserve. It is conservative and repeats during the batch, but does not reserve memory or predict later demand. Stopped guests are not included in startup RAM requirements. Confirm there is capacity before starting them later. The script does not measure application health after startup.

## Logs and recovery

Migration output and progress are appended to `/var/log/pve-migrate.log`. Also inspect the task log in the Proxmox web UI.

If shutdown, migration, configuration, or startup fails, the batch stops. Earlier completed migrations remain in place. The current guest may be stopped on the source or destination, and destination settings may already have changed. There is no automatic rollback or restart on failure.

If FreePBX preparation succeeded or partially ran before an error, the guest may still be running **with phone services stopped**. After verifying no shutdown/migration/stop command remains active and locating the authoritative guest, use its console to inspect services and run `fwconsole start` if you are abandoning migration and need to restore service. Do not start a second copy of the PBX.

After an error or Ctrl+C:

1. Check the guest's actual node and state in Proxmox and inspect its task log. Server-side tasks may still be running; interrupting this script does not reliably cancel them.
2. Wait for active tasks to finish. Do not blindly unlock, delete disks, or rerun migration.
3. Correct the reported issue, verify storage and configuration, and start the guest on its actual node if appropriate.
4. Run the script on the node hosting any remaining guests and select those guests again. There is no automatic resume.

Destination startup tasks are polled every five seconds for up to ten minutes. A polling timeout stops the batch without cancelling the task.

## Repository files

- `pve-migrate.sh`: executable workflow.
- `README.md`: usage and operational notes.
- `.gitattributes`: keeps shell scripts and Markdown in LF format when using Git on Windows.
- `tests/test_helpers.py`: local checks for the embedded JSON processing and capacity display.

These files are ready to add to a separate Git repository. No repository has been created or published by the script. Do not commit server logs or private cluster configuration.

## Validation and references

Local JSON-helper checks cover node status formatting, missing/offline metrics, CPU arithmetic, RAM budgets, guest/node filtering, quorum, HA exclusion, and guest-command success/failure/timeout responses. Run `python tests/test_helpers.py` to repeat the helper tests without a cluster. These tests compile the Python embedded in the Bash script and exercise it with sample API responses; they do not execute the Bash migration workflow. Bash execution and real migrations were not tested: this Windows workspace has no Bash runtime or Proxmox cluster. Run the syntax check above and verify with a noncritical guest on your installation. Check the installed `qm help migrate`, `pct help migrate`, and `pvesh` API help when using a different release.

Official Proxmox CLI references: [qm](https://pve.proxmox.com/pve-docs/qm.1.html), [pct](https://pve.proxmox.com/pve-docs/pct.1.html), and [pvesh](https://pve.proxmox.com/pve-docs/pvesh.1.html).
