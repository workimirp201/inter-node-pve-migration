# Proxmox VM and container migration with direct I/O

Run `bash pve-migrate.sh` on the **source node** for the interactive wizard. Keep `pve-migrate.sh` and `pve-migrate.py` together. This replaces the earlier script's normal Proxmox migration commands with offline `dd` copying over SSH. It combines the earlier VM/container selection, bandwidth control, resource planning and FreePBX workflow with the direct-I/O approach from the working container script.

**Scope: QEMU VMs and LXC containers in the same Proxmox cluster, with all their disks on one non-shared LVM-thin storage ID available on both nodes.** Default storage: `local-lvm`; VG and thin-pool names are read from Proxmox storage configuration. This is not a general replacement for Proxmox migration across ZFS, Ceph, directories, or different clusters.

The R810's successful direct-I/O run is useful evidence for keeping this data path. It does not establish the original crash cause or guarantee that hardware/kernel faults cannot recur.

## Quick start

Copy these two files to the same directory on the source node:

- `pve-migrate.sh`
- `pve-migrate.py`

Python 3 and standard Proxmox tools are required. Install `pv` on **both nodes** if absent, using `apt install pv`. Use `tmux` or `screen` for the real run. The source must have working root SSH access with the target's host key already verified and accepted. This script does not disable host-key verification.

```bash
bash pve-migrate.sh --help
bash pve-migrate.sh --dry-run
bash pve-migrate.sh
```

The wizard lists local guests, accepts selected IDs or `all`, shows cluster node RAM/CPU status, asks for the destination and rate, and lets you review RAM, CPU cores and container swap. Enter `r` at destination selection to refresh node metrics. It defaults to FreePBX preparation for all selected guests; enter `none` for standard guests or enter only the FreePBX IDs for a mixed batch.

Explicit commands also work. Replace the example node/address/IDs with your values:

```bash
# Mixed VM/container batch, normal graceful shutdown, settings preserved.
bash pve-migrate.sh --dry-run node2 192.168.1.12 101 205
bash pve-migrate.sh node2 192.168.1.12 101 205

# FreePBX guests, conservative 10 MiB/s copying AND checksum reads.
bash pve-migrate.sh --dry-run --pbx all node2 192.168.1.12 107 108
bash pve-migrate.sh --pbx all node2 192.168.1.12 107 108

# Only guest 107 is FreePBX; ask for destination resource changes.
bash pve-migrate.sh --pbx 107 --edit-resources node2 192.168.1.12 107 205
```

Explicit command-line mode defaults to **no FreePBX commands** unless `--pbx` is supplied. Originally stopped guests remain stopped. Running guests remain offline for copying plus both complete checksum reads. At 10 MiB/s, a 30 GiB disk takes roughly 154 minutes for these three passes alone. Thin/sparse space savings do not reduce the logical data transmitted or read.

Every real run prints the plan and requires `MIGRATE`. Dry run checks the plan but does not stop guests, set guest locks, create LVs, or read all disk contents. It briefly creates/holds tool lock files on both hosts and may execute read-only FreePBX availability checks. It is not a hardware stress test or proof that a guest will boot on the destination.

## Transfer and safety behavior

1. Check both nodes' cluster identity and quorum, storage configuration/availability, destination package versions, guest state/configuration, existing destination volumes, bridges, and selected guest HA/replication membership.
2. Budget destination RAM and **full logical disk sizes**, not source thin allocation. Keep a default 10 GiB disk reserve and an 85% data/metadata ceiling. Sparse savings are deliberately not assumed. A sparse source disk can allocate more destination space because writes operate in 4 MiB blocks.
3. Shut down FreePBX where selected, then gracefully shut down the guest. Never force-stop it. Acquire a guest migration lock after shutdown and recheck guest state and configuration. Do not perform concurrent guest administration during this interval or the batch.
4. Create fresh target thin LVs and copy serially with source `iflag=direct`, destination `oflag=direct`, sparse writes, and an explicit final `fsync`. No normal `qm migrate`/`pct migrate` path, disk compression, or buffered source disk scan is used.
5. Read both complete volumes with direct I/O and compare SHA-256 checksums. Copying and each checksum pass are separately throttled by `pv`. These limits bound stream throughput, not all disk IOPS, host CPU use, or workload activity.
6. Check target pool headroom and both kernel logs periodically during data pipelines. New segfault/OOM/I/O/hardware/kernel fault messages stop the batch. Polling is best effort, approximately every ten seconds plus command duration; it cannot prevent an abrupt crash or detect every fault.
7. Optionally run a read-only ext2/3/4 filesystem check on container copies. No VM partition is passed blindly to `e2fsck`. No automatic filesystem repair is performed.
8. Deactivate source LVs, move the locked configuration within the cluster filesystem, unlock on the target, apply/verify selected resource changes, and start only guests that were originally running.
9. Retain all source LVs and a protected, durable JSON recovery record with original configuration, LV UUIDs, sizes, verification hashes and progress phase. Pause before the next guest.

The transfer does not write source disk contents. A normal running guest and its graceful shutdown can of course write to its own disks before copying begins. No global cache-dropping command is issued.

Tool locks on both hosts prevent overlapping runs of **this version** involving either node. They do not coordinate other migration scripts, backups, GUI actions or administrators. Suspend overlapping work and do not bypass guest locks. RAM/pool checks are observations, not reservations. Destination thin metadata growth cannot be predicted exactly; runtime ceiling checks add protection but are not a hard reservation.

## Supported and rejected configurations

Supported disks include VM IDE/SATA/SCSI/VirtIO disks, EFI and TPM state disks, standard cloud-init volumes, container rootfs and storage-backed mount points, and unused volumes. Each must belong to the selected guest and storage. Volume sizes must be aligned for direct I/O. Guest CPU/machine/OS compatibility and application health still need operator verification; a byte-identical disk does not prove boot compatibility.

The script refuses snapshots/pending sections, thin clones/snapshots, templates, existing locks, HA resources, replication jobs, bind/device mounts, foreign disks, other storage backends, attached ISO/physical CD media, host/custom CPU models or custom CPU flags, device passthrough, raw `lxc.*` lines, hooks, custom VM arguments, custom cloud-init snippets, host NUMA/affinity settings and several other host-dependent options. Resolve these separately before retrying. An empty CD drive (`none,media=cdrom`) is allowed. This deliberately restrictive scope protects the manual configuration handoff from dependencies it cannot copy.

VLAN tags/trunks require `--ack-network`, or acknowledgment in the wizard, after checking the destination network and switch configuration. Bridge existence alone does not prove connectivity. No mappings or network settings are rewritten.

Destination Proxmox manager, QEMU server and container package versions must be at least the source versions. This check is necessary but not sufficient for compatibility. Verify a matching CPU model and supported machine/OS version before selecting a VM. Host/custom CPU configurations are not silently changed.

## Options

| Option | Default | Meaning |
| --- | --- | --- |
| `--dry-run` | Off | Preflight and plan only. |
| `--storage ID` | `local-lvm` | One LVM-thin storage ID for all disks. |
| `--rate-mib N` | 10 | Limit each copy/read stream; range 1–1024 MiB/s. |
| `--pause N` | 30 | Seconds between guests. |
| `--shutdown-timeout N` | 300 | Graceful OS shutdown timeout; range 30–3600 seconds. |
| `--reserve-mib N` | 2048 | Target RAM reserve beyond planned running guests; minimum 512. |
| `--reserve-gib N` | 10 | Target data pool free-space reserve; minimum 1. |
| `--pool-ceiling N` | 85 | Data/metadata percentage ceiling; range 50–95. |
| `--pbx all` / `--pbx 101,205` | None in explicit mode | Stop FreePBX before guest shutdown. |
| `--edit-resources` | Off in explicit mode | Prompt for RAM, cores, and CT swap; otherwise preserve configuration. |
| `--check-fs` | Off | Require clean read-only ext2/3/4 checks on active CT filesystem copies. |
| `--ack-network` | Off | Confirm you have checked VLAN/trunk/switch compatibility. |

VM cores are per socket; sockets are preserved. Container core value 0 in the prompt means keep the current setting. VM swap/pagefile settings live inside the guest OS and are untouched. Container swap is a host-backed allowance, not additional RAM or a reservation. Balloon minimum and hotplug vCPU constraints are checked. Originally stopped guests are excluded from the immediate startup RAM budget; budget their later starts separately.

FreePBX checks require root command access inside running guests. VMs require a configured, working QEMU Guest Agent; containers use `pct exec`. The stop-command timeout is 330 seconds, independent of OS shutdown. Failure or timeout stops the batch; the guest command may still be active. Calls may drop when FreePBX stops. After boot, check trunks, extensions and a test call; guest running state is not a phone-service health check.

## Failures and old disk cleanup

Read [RECOVERY.md](RECOVERY.md). This version deliberately retains partial copies and acquired locks after failure instead of automatically deleting disks or restarting a guest whose ownership may be uncertain. There is no automatic resume. A shutdown failure before lock acquisition may leave the source guest unlocked and running with FreePBX stopped. A failure after target unlock may leave it unlocked there. Earlier successful guests remain on the destination.

Logs and per-guest journals are under `/var/lib/pve-safe-migrate/<run>/`. Keep a copy of completed records with your backups. Records contain guest configuration and should stay root-only. Source LVs become stale as soon as the destination guest starts writing; they are not a synchronized backup or an automatic failback copy. No bulk `lvremove` loop is provided.

## Validation

Run the platform-independent test suite:

```bash
python3 -m unittest discover -s tests -v
python3 -m py_compile pve-migrate.py
bash -n pve-migrate.sh
```

Tests exercise parsing, safety gates, capacity calculations, guest-agent result handling, SSH construction, and simulated migration success/failure ordering. They do not replace a real Proxmox test. Development occurred on Windows without a Proxmox cluster or Bash runtime; actual LVM/SSH transfers and guest startup have not been tested here. Start with a backed-up, noncritical guest and a dry run on the installed Proxmox version.

## References

- [GNU dd](https://www.gnu.org/software/coreutils/manual/html_node/dd-invocation.html): direct I/O, sparse conversion and synchronization.
- [Proxmox VM CLI](https://pve.proxmox.com/pve-docs/qm.1.html) and [container CLI](https://pve.proxmox.com/pve-docs/pct.1.html).
- [Proxmox generated container command reference](https://github.com/proxmox/pve-docs/blob/master/generated/pct.1-synopsis.adoc): container shutdown/set commands differ from VM commands.
- [Proxmox cluster filesystem](https://pve.proxmox.com/pve-docs/chapter-pmxcfs.html).

The prior implementation is retained as `pve-migrate.previous.sh` for reference only. It uses the old transfer path implicated in your crash; the current launcher never calls it.
