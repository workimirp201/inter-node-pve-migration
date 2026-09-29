#!/usr/bin/env bash
# Interactive sequential migration within a Proxmox VE cluster.
set -Eeuo pipefail
umask 077
LOG_FILE=/var/log/pve-migrate.log
ACTIVE_GUEST=none
COMPLETED=()
# Fixed command, executed inside the guest only. Guest agents may have a minimal PATH.
FREEPBX_CHECK='PATH=/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin; export PATH; [ "$(id -u)" -eq 0 ] || { echo "Guest command must run as root" >&2; exit 1; }; command -v fwconsole >/dev/null || { echo "fwconsole not found in guest" >&2; exit 127; }'
log() { printf '[%s] %s\n' "$(date '+%F %T')" "$*" | tee -a "$LOG_FILE"; }
die() { log "ERROR: $*" >&2; exit 1; }
on_exit() {
    local rc=$?
    if (( rc != 0 )); then
        printf '\nStopped. Current guest: %s. Completed: %s\n' "$ACTIVE_GUEST" "${COMPLETED[*]:-none}" >&2
        printf 'Check Proxmox tasks and guest location/state before retrying. A guest may be stopped; a server task may still be running. No automatic rollback.\n' >&2
        printf 'In FreePBX mode, phone services may be stopped even if the guest is still running. Check inside the guest before restoring service.\n' >&2
    fi
}
trap on_exit EXIT
trap 'exit 130' INT
trap 'exit 143' TERM
trap 'printf "Command failed at line %s. See %s and Proxmox tasks.\n" "$LINENO" "$LOG_FILE" >&2' ERR
field() { python3 -c 'import json,sys; print(json.load(sys.stdin).get(sys.argv[1], sys.argv[2]))' "$1" "${2:-}"; }
api() { pvesh get "$@" --output-format json; }
show_nodes() {
    printf '\nCluster status snapshot (%s); cluster metrics may lag slightly.\n' "$(date '+%F %T')"
    python3 -c 'import json,sys
rows=json.load(sys.stdin)
header=("NODE", "STATUS", "RAM USED/TOTAL GiB", "RAM %", "FREE GiB", "CPUs", "CPU %", "BUSY CPU EQ.")
fmt="{:<24} {:<9} {:>19} {:>7} {:>9} {:>6} {:>7} {:>12}"
print(fmt.format(*header))
for n in sorted(rows,key=lambda n:n["node"]):
    name=n["node"]+(" (source)" if n["node"]==sys.argv[1] else "")
    status=n.get("status","unknown")
    values=["N/A"]*6
    if status=="online":
        used,total=n.get("mem"),n.get("maxmem")
        cpus,cpu=n.get("maxcpu"),n.get("cpu")
        if used is not None and total is not None and total>0:
            values[:3]=[f"{used/2**30:.1f}/{total/2**30:.1f}",f"{100*used/total:.1f}%",f"{max(0,total-used)/2**30:.1f}"]
        if cpus is not None and cpus>0: values[3]=str(cpus)
        if cpu is not None:
            values[4]=f"{100*cpu:.1f}%"
            if cpus is not None and cpus>0: values[5]=f"{cpu*cpus:.1f}"
    print(fmt.format(name,status,*values))
print("CPUs = logical CPUs (hardware threads). Busy CPU equivalents = utilization x CPU count.")
print("CPU utilization is sampled load, not reserved/free cores or a forecast of guest demand.")' "$SOURCE_NODE" <<< "$1"
}
ask_number() {
    local prompt=$1 default=$2 min=$3 max=$4 answer
    while true; do
        read -r -p "$prompt [$default]: " answer || die 'Input closed.'
        answer=${answer:-$default}
        if [[ $answer =~ ^[0-9]{1,9}$ ]] && (( 10#$answer >= min && 10#$answer <= max )); then
            REPLY=$((10#$answer)); return
        fi
        printf 'Enter a whole number from %s to %s.\n' "$min" "$max"
    done
}
cluster_check() {
    local data
    data=$(api /cluster/status)
    python3 -c 'import json,sys
if not any(x.get("type")=="cluster" and x.get("quorate")==1 for x in json.load(sys.stdin)):
    sys.exit("Cluster is not quorate; refusing migration.")' <<< "$data"
}
ha_check() {
    local data
    data=$(api /cluster/ha/resources)
    python3 -c 'import json,sys
ids=set(sys.argv[1:])
for r in json.load(sys.stdin):
    if r.get("sid", "").split(":")[-1] in ids:
        sys.exit("Selected guest is HA-managed: "+r["sid"]+". Use the HA workflow instead.")' "${SELECTED[@]}" <<< "$data"
}
guest_config() { api "/nodes/$SOURCE_NODE/${TYPES[$1]}/$1/config"; }
guest_state() { api "/nodes/$1/${TYPES[$2]}/$2/status/current" | field status; }
check_guest_result() {
    python3 -c 'import json,sys
# The host qm command can succeed while the guest command fails or is still running.
d=json.load(sys.stdin)
for key in ("out-data", "err-data"):
    if d.get(key): print(d[key])
if d.get("exited") != 1:
    print("Guest command did not finish; it may still be running. PID: " + str(d.get("pid", "unknown")), file=sys.stderr)
    sys.exit(1)
if d.get("signal") is not None or d.get("exitcode") != 0:
    print("Guest command failed: " + json.dumps(d), file=sys.stderr)
    sys.exit(1)' <<< "$1"
}
run_guest_command() {
    local id=$1 wait_seconds=$2 command_text=$3 result
    if [[ ${TYPES[$id]} == lxc ]]; then
        # Killing the CLI on timeout does not guarantee the in-guest process stopped.
        timeout --foreground --kill-after=10s "$wait_seconds" \
            pct exec "$id" -- /bin/sh -c "$command_text" 2>&1 | tee -a "$LOG_FILE" || return 1
    else
        result=$(timeout --foreground --kill-after=10s "$((wait_seconds + 45))" \
            qm guest exec "$id" --synchronous 1 --timeout "$wait_seconds" \
            -- /bin/sh -c "$command_text") || return 1
        check_guest_result "$result" 2>&1 | tee -a "$LOG_FILE" || return 1
    fi
}
check_config() {
    local id=$1 config=$2
    [[ $(field template 0 <<< "$config") == 0 ]] || die "$id is a template."
    [[ -z $(field lock <<< "$config") ]] || die "$id is locked; finish the existing task first."
    if grep -q '^\[PENDING\]' "/etc/pve/nodes/$SOURCE_NODE/${DIRS[$id]}/$id.conf"; then
        die "$id has pending changes. Apply or revert them first."
    fi
}
target_check() {
    local required=$1 data
    data=$(api "/nodes/$TARGET_NODE/status")
    python3 -c 'import json,sys
d=json.load(sys.stdin)
free=int(d["memory"]["free"])//1048576
planned,reserve=int(sys.argv[1]),int(sys.argv[2])
need=planned+reserve
print(f"Destination free RAM: {free} MiB; remaining startup RAM: {planned} MiB; host reserve: {reserve} MiB")
print(f"RAM budget beyond remaining plan and reserve: {free-need} MiB ({(free-need)/1024:.2f} GiB)")
cpu=d.get("cpu"); cpus=d.get("cpuinfo",{}).get("cpus")
if cpu is not None and cpus:
    print(f"Destination CPU now: {100*cpu:.1f}% of {cpus} logical CPUs ({cpu*cpus:.1f} busy CPU equivalents)")
if free < need: sys.exit("Insufficient destination RAM; batch stopped.")' "$required" "$RESERVE_MB" <<< "$data"
}
wait_task() {
    local upid=$1 data state started=$SECONDS
    [[ $upid == UPID:* ]] || die 'Destination did not return a task ID.'
    log "Waiting for destination task: $upid"
    while true; do
        data=$(api "/nodes/$TARGET_NODE/tasks/$upid/status")
        state=$(field status <<< "$data")
        if [[ $state == stopped ]]; then
            [[ $(field exitstatus <<< "$data") == OK ]] || die "Destination task failed: $data"
            return
        fi
        [[ $state == running ]] || die "Unexpected task status: $data"
        (( SECONDS - started < 600 )) || die 'Task wait exceeded 10 minutes. Check Proxmox; the task was not cancelled.'
        sleep 5
    done
}
if [[ ${1:-} == --help || ${1:-} == -h ]]; then
    printf 'Usage: bash pve-migrate.sh\nRun interactively as root on the source Proxmox cluster node. See README.md.\n'
    exit 0
fi
(( $# == 0 )) || { printf 'Unknown argument. Use --help.\n' >&2; exit 1; }
(( EUID == 0 )) || { printf 'Run as root on the source Proxmox node.\n' >&2; exit 1; }
[[ -t 0 ]] || { printf 'An interactive terminal is required.\n' >&2; exit 1; }
for command in qm pct pvesh python3 flock tee readlink grep timeout; do
    command -v "$command" >/dev/null || { printf 'Required command missing: %s\n' "$command" >&2; exit 1; }
done
[[ -d /etc/pve/nodes ]] || { printf 'Proxmox cluster filesystem unavailable.\n' >&2; exit 1; }
touch "$LOG_FILE"
exec 9>/run/lock/pve-migrate.lock
flock -n 9 || die 'Another copy of this script is running on this source node.'
SOURCE_NODE=$(basename "$(readlink -f /etc/pve/local)")
[[ -d /etc/pve/nodes/$SOURCE_NODE ]] || die 'Cannot identify the local Proxmox node.'
cluster_check
printf '\nProxmox VM and container migration - source: %s\n' "$SOURCE_NODE"
printf 'Guests move ONE AT A TIME. Running guests shut down cleanly, migrate, then restart.\n'
printf 'This requires downtime. Bandwidth limiting cannot guarantee against host overload.\n\n'
declare -A TYPES DIRS NAMES STATES RAM SWAP CORES SOCKETS DIGESTS SEEN
declare -a ALL_IDS SELECTED NODES
resources=$(api /cluster/resources --type vm)
rows=$(python3 -c 'import json,sys
for g in sorted(json.load(sys.stdin), key=lambda x:int(x["vmid"])):
    if g.get("node")==sys.argv[1] and g.get("type") in ("qemu","lxc") and not g.get("template"):
        name=" ".join(str(g.get("name") or "unnamed").split())
        print(g["vmid"],g["type"],g.get("status","unknown"),name,sep="\t")' "$SOURCE_NODE" <<< "$resources")
[[ -n $rows ]] || die 'No VMs or containers on this node.'
printf '%-10s %-8s %-12s %s\n' ID TYPE STATUS NAME
while IFS=$'\t' read -r id kind state name; do
    ALL_IDS+=("$id"); TYPES[$id]=$kind; NAMES[$id]=$name; STATES[$id]=$state
    if [[ $kind == qemu ]]; then DIRS[$id]=qemu-server; else DIRS[$id]=lxc; fi
    printf '%-10s %-8s %-12s %s\n' "$id" "$kind" "$state" "$name"
done <<< "$rows"
while true; do
    read -r -p 'Guest IDs in migration order (spaces/commas), or all: ' selection || die 'Input closed.'
    SELECTED=(); SEEN=(); valid=1
    if [[ $selection == all ]]; then
        SELECTED=("${ALL_IDS[@]}")
    else
        read -r -a candidates <<< "${selection//,/ }"
        for id in "${candidates[@]}"; do
            if [[ ! $id =~ ^[1-9][0-9]{2,8}$ ]] || [[ -z ${TYPES[$id]:-} ]]; then
                printf 'Not a listed guest ID: %s\n' "$id"; valid=0; break
            fi
            if [[ -z ${SEEN[$id]:-} ]]; then SELECTED+=("$id"); SEEN[$id]=1; fi
        done
    fi
    if (( valid && ${#SELECTED[@]} > 0 )); then break; fi
    printf 'Select at least one listed guest.\n'
done
ha_check
while true; do
    nodes_json=$(api /nodes)
    show_nodes "$nodes_json"
    node_rows=$(python3 -c 'import json,sys
for n in json.load(sys.stdin):
    if n["node"]!=sys.argv[1] and n.get("status")=="online": print(n["node"])' "$SOURCE_NODE" <<< "$nodes_json")
    [[ -n $node_rows ]] || die 'No other online cluster nodes.'
    mapfile -t NODES <<< "$node_rows"
    printf '\nOnline destination nodes:\n'
    printf '  %s\n' "${NODES[@]}"
    read -r -p 'Destination node name (or r to refresh status): ' TARGET_NODE || die 'Input closed.'
    [[ $TARGET_NODE != r ]] || continue
    valid=0
    for node in "${NODES[@]}"; do [[ $node != "$TARGET_NODE" ]] || valid=1; done
    (( valid )) && break
    printf 'Choose a node from the list.\n'
done
target_info=$(api "/nodes/$TARGET_NODE/status")
target_cpus=$(python3 -c 'import json,sys; print(json.load(sys.stdin)["cpuinfo"]["cpus"])' <<< "$target_info")
printf '\nDestination: %s logical CPUs.\n' "$target_cpus"
ask_number 'Bandwidth per migration in MiB/s (1 MiB/s = 1024 KiB/s)' 10 1 1048576
BW_MIB=$REPLY; BW_KIB=$((BW_MIB * 1024))
ask_number 'Pause between guests, seconds' 30 0 86400
PAUSE=$REPLY
ask_number 'Graceful shutdown timeout, seconds (never force-stop)' 180 30 3600
SHUTDOWN_TIMEOUT=$REPLY
printf '\nShutdown preparation:\n  1) FreePBX: fwconsole stop, then graceful OS shutdown (recommended for these PBXs)\n  2) Standard: graceful OS shutdown only (other guests)\n'
ask_number 'Shutdown preparation mode' 1 1 2
FREEPBX_MODE=$REPLY
FREEPBX_TIMEOUT=300
if (( FREEPBX_MODE == 1 )); then
    printf 'FreePBX mode interrupts active calls. VMs require a working QEMU Guest Agent.\n'
    ask_number 'Time allowed for fwconsole stop, seconds' 300 30 3600
    FREEPBX_TIMEOUT=$REPLY
fi
ask_number 'Free RAM to leave for the destination host, MiB' 1024 512 999999999
RESERVE_MB=$REPLY
required_ram=0
for id in "${SELECTED[@]}"; do
    config=$(guest_config "$id")
    check_config "$id" "$config"
    DIGESTS[$id]=$(field digest <<< "$config")
    [[ -n ${DIGESTS[$id]} ]] || die "Missing configuration digest for $id."
    STATES[$id]=$(guest_state "$SOURCE_NODE" "$id")
    [[ ${STATES[$id]} == running || ${STATES[$id]} == stopped ]] || die "Unsupported state for $id: ${STATES[$id]}"
    current_ram=$(field memory 512 <<< "$config")
    current_cores=$(field cores <<< "$config")
    SOCKETS[$id]=$(field sockets 1 <<< "$config")
    printf '\n%s %s (%s), %s\n' "${TYPES[$id]}" "$id" "${NAMES[$id]}" "${STATES[$id]}"
    target_check "$required_ram"
    printf 'The RAM budget above excludes this guest. Stopped guests do not consume the startup budget.\n'
    [[ $current_ram =~ ^[0-9]+$ ]] || die "Unsupported memory format on $id; configure it manually first."
    ask_number 'Destination RAM in MiB' "$current_ram" 16 999999999
    RAM[$id]=$REPLY
    if [[ ${TYPES[$id]} == qemu ]]; then
        printf 'VM swap/pagefile is managed inside the guest OS; this script leaves it unchanged.\n'
        printf 'VM cores are PER SOCKET; keeping %s socket(s).\n' "${SOCKETS[$id]}"
        max_cores=$((target_cpus / SOCKETS[$id]))
        (( max_cores > 0 )) || die "$id has more sockets than destination logical CPUs."
        ask_number 'Destination cores per socket' "${current_cores:-1}" 1 "$max_cores"
        CORES[$id]=$REPLY
        balloon=$(field balloon 0 <<< "$config")
        (( RAM[$id] >= balloon )) || die "$id: RAM must be at least its balloon minimum ($balloon MiB)."
        vcpus=$(field vcpus 0 <<< "$config")
        (( CORES[$id] * SOCKETS[$id] >= vcpus )) || die "$id: requested CPU total is below its configured vcpus ($vcpus). Adjust vcpus first."
    else
        current_swap=$(field swap 512 <<< "$config")
        printf 'Container swap is a limit on host-backed swap, separate from physical RAM.\n'
        ask_number 'Destination container swap in MiB (0 = no swap allowance)' "$current_swap" 0 999999999
        SWAP[$id]=$REPLY
        printf 'Current container cores: %s. Enter 0 to keep this setting.\n' "${current_cores:-unlimited}"
        ask_number 'Destination CPU cores (0 = unchanged)' 0 0 "$target_cpus"
        CORES[$id]=$REPLY
    fi
    if [[ ${STATES[$id]} == running ]]; then required_ram=$((required_ram + RAM[$id])); fi
done
target_check "$required_ram"
if (( FREEPBX_MODE == 1 )); then
    # Check every running guest before stopping services on ANY guest in the batch.
    for id in "${SELECTED[@]}"; do
        if [[ ${STATES[$id]} == running ]]; then
            log "Checking guest command access and fwconsole availability for $id (read-only)."
            run_guest_command "$id" 30 "$FREEPBX_CHECK" || die "FreePBX preflight failed for $id. Check guest root access, fwconsole, and (for VMs) QEMU Guest Agent. No stop command was issued."
        fi
    done
fi
printf '\nMigration plan: %s -> %s\n' "$SOURCE_NODE" "$TARGET_NODE"
printf 'Bandwidth: %s MiB/s; pause: %ss; shutdown timeout: %ss; host RAM reserve: %s MiB\n' "$BW_MIB" "$PAUSE" "$SHUTDOWN_TIMEOUT" "$RESERVE_MB"
for id in "${SELECTED[@]}"; do
    core_label=${CORES[$id]}
    if [[ ${TYPES[$id]} == qemu ]]; then core_label="$core_label per socket x ${SOCKETS[$id]} socket(s)";
    elif (( CORES[$id] == 0 )); then core_label=unchanged; fi
    printf '  %s %s (%s): RAM %s MiB, cores %s, restore state %s\n' "${TYPES[$id]}" "$id" "${NAMES[$id]}" "${RAM[$id]}" "$core_label" "${STATES[$id]}"
    if [[ ${TYPES[$id]} == lxc ]]; then
        printf '    Container swap: %s MiB\n' "${SWAP[$id]}"
    fi
done
printf 'Running guests will be OFFLINE for their entire transfer. Settings apply after migration.\n'
if (( FREEPBX_MODE == 1 )); then
    printf 'FreePBX: fwconsole stop first (up to %ss), then graceful shutdown. Active calls will be interrupted.\n' "$FREEPBX_TIMEOUT"
    printf 'Failure or timeout stops the batch; there is NO automatic forced shutdown.\n'
else
    printf 'Shutdown preparation: standard OS shutdown only.\n'
fi
printf 'Storage IDs, bridges and required devices must work on the destination.\n'
read -r -p 'Type MIGRATE to begin, or anything else to cancel: ' confirm || die 'Input closed.'
[[ $confirm == MIGRATE ]] || { printf 'Cancelled. No guest changes made.\n'; exit 0; }
for id in "${SELECTED[@]}"; do
    ACTIVE_GUEST=$id
    cluster_check
    ha_check
    [[ -f /etc/pve/nodes/$SOURCE_NODE/${DIRS[$id]}/$id.conf ]] || die "$id is no longer on the source."
    config=$(guest_config "$id")
    check_config "$id" "$config"
    [[ $(field digest <<< "$config") == "${DIGESTS[$id]}" ]] || die "$id configuration changed since confirmation. Re-run to review."
    [[ $(guest_state "$SOURCE_NODE" "$id") == "${STATES[$id]}" ]] || die "$id state changed since confirmation. Re-run to review."
    target_check "$required_ram"
    if [[ ${TYPES[$id]} == qemu ]]; then cli=qm; else cli=pct; fi
    if [[ ${STATES[$id]} == running ]]; then
        if (( FREEPBX_MODE == 1 )); then
            log "Stopping FreePBX services in $id with fwconsole stop (timeout ${FREEPBX_TIMEOUT}s)."
            run_guest_command "$id" "$FREEPBX_TIMEOUT" "$FREEPBX_CHECK; exec fwconsole stop" \
                || die "fwconsole stop failed or timed out for $id. No OS shutdown issued. Inspect phone services and any guest command still running before retrying."
        fi
        log "Gracefully shutting down $id (timeout ${SHUTDOWN_TIMEOUT}s)."
        "$cli" shutdown "$id" --timeout "$SHUTDOWN_TIMEOUT" --forceStop 0 2>&1 | tee -a "$LOG_FILE"
    fi
    [[ $(guest_state "$SOURCE_NODE" "$id") == stopped ]] || die "$id is not stopped."
    log "Migrating ${TYPES[$id]} $id to $TARGET_NODE at $BW_MIB MiB/s."
    "$cli" migrate "$id" "$TARGET_NODE" --bwlimit "$BW_KIB" 2>&1 | tee -a "$LOG_FILE"
    [[ -f /etc/pve/nodes/$TARGET_NODE/${DIRS[$id]}/$id.conf && ! -f /etc/pve/nodes/$SOURCE_NODE/${DIRS[$id]}/$id.conf ]] || die "Cannot verify migration location for $id."
    [[ $(guest_state "$TARGET_NODE" "$id") == stopped ]] || die "$id unexpectedly running on destination."
    dest_config=$(api "/nodes/$TARGET_NODE/${TYPES[$id]}/$id/config")
    dest_digest=$(field digest <<< "$dest_config")
    [[ -n $dest_digest ]] || die "Missing destination configuration digest for $id."
    args=(--memory "${RAM[$id]}" --digest "$dest_digest")
    if [[ ${TYPES[$id]} == lxc ]]; then
        args+=(--swap "${SWAP[$id]}")
        log "Setting container $id swap allowance to ${SWAP[$id]} MiB."
    fi
    if (( CORES[$id] > 0 )); then args+=(--cores "${CORES[$id]}"); fi
    log "Applying destination resources to $id: RAM ${RAM[$id]} MiB, cores ${CORES[$id]} (0 means unchanged; VM cores are per socket)."
    pvesh set "/nodes/$TARGET_NODE/${TYPES[$id]}/$id/config" "${args[@]}" 2>&1 | tee -a "$LOG_FILE"
    dest_config=$(api "/nodes/$TARGET_NODE/${TYPES[$id]}/$id/config")
    [[ $(field memory <<< "$dest_config") == "${RAM[$id]}" ]] || die "RAM verification failed for $id."
    if [[ ${TYPES[$id]} == lxc ]]; then
        [[ $(field swap 512 <<< "$dest_config") == "${SWAP[$id]}" ]] || die "Swap verification failed for $id."
    fi
    if (( CORES[$id] > 0 )); then
        [[ $(field cores <<< "$dest_config") == "${CORES[$id]}" ]] || die "CPU verification failed for $id."
    fi
    if [[ ${STATES[$id]} == running ]]; then
        target_check "$required_ram"
        log "Starting $id on $TARGET_NODE."
        task_json=$(pvesh create "/nodes/$TARGET_NODE/${TYPES[$id]}/$id/status/start" --output-format json)
        upid=$(python3 -c 'import json,sys; print(json.load(sys.stdin))' <<< "$task_json")
        wait_task "$upid"
        [[ $(guest_state "$TARGET_NODE" "$id") == running ]] || die "$id did not reach running state."
        required_ram=$((required_ram - RAM[$id]))
    fi
    COMPLETED+=("$id")
    log "SUCCESS: $id migrated; resources verified; state ${STATES[$id]}."
    if (( FREEPBX_MODE == 1 )) && [[ ${STATES[$id]} == running ]]; then
        log "Verify FreePBX/Asterisk service startup, trunks and test calls on $id; guest running status alone does not verify telephony."
    fi
    ACTIVE_GUEST=none
    if (( ${#COMPLETED[@]} < ${#SELECTED[@]} && PAUSE > 0 )); then
        log "Pausing ${PAUSE}s before the next guest."
        sleep "$PAUSE"
    fi
done
log "Batch complete. Guests: ${COMPLETED[*]}. Log: $LOG_FILE"
# Reporting failure must not turn completed migrations into a failed batch.
if nodes_json=$(api /nodes); then
    show_nodes "$nodes_json" || printf 'Could not display final cluster status. Check the Proxmox dashboard.\n' >&2
else
    printf 'Could not refresh final cluster status. Check the Proxmox dashboard.\n' >&2
fi
