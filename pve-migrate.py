#!/usr/bin/env python3
"""Offline, direct-I/O migration for a deliberately restricted LVM-thin layout.

No disk contents pass through Python. GNU dd/pv/ssh do bounded, throttled I/O.
Failures preserve both copies and any acquired guest lock for operator-led recovery.
"""
import argparse
import datetime as dt
import json
import math
import os
from pathlib import Path
import re
import shlex
import signal
import subprocess as sp
import sys
import time

GIB = 2**30
MIB = 2**20
LOCK = '/run/lock/pve-safe-migrate.lock'
ROOT = Path('/etc/pve/nodes')
DISK = re.compile(r'(?:rootfs|mp\d+|unused\d+|(?:ide|sata|scsi|virtio)\d+|efidisk0|tpmstate0)\Z')
FW = 'PATH=/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin; export PATH; test "$(id -u)" = 0 && command -v fwconsole >/dev/null'


class Refuse(RuntimeError):
    pass


def require(ok, message):
    if not ok:
        raise Refuse(message)


def parse_config(text):
    result = {}
    for line in text.splitlines():
        line = line.strip()
        if not line or line.startswith('#'):
            continue
        require(not line.startswith('['), 'Snapshots/pending sections are unsupported.')
        require(': ' in line, 'Unrecognized configuration line: ' + line)
        key, value = line.split(': ', 1)
        require(key not in result, 'Repeated configuration key: ' + key)
        result[key] = value
    return result


def volume_list(config, kind, guest_id, storage):
    require(config.get('template', '0') == '0', 'Templates are unsupported.')
    require('lock' not in config, 'Guest is already locked.')
    forbidden = r'(?:hookscript|args|hostpci\d+|usb\d+|dev\d+|lxc\..*|cicustom|vmstate|ivshmem|virtiofs\d+|numa\d+|affinity|rng0|amd-sev|intel-tdx)'
    volumes = []
    for key, value in config.items():
        require(not re.fullmatch(forbidden, key), 'Host-specific/unsupported setting: ' + key)
        if re.fullmatch(r'(?:serial|parallel)\d+', key):
            require(value == 'socket', 'Host device: ' + key)
        if key == 'memory':
            require(value.isdigit(), 'Advanced memory format unsupported; use integer MiB.')
        if key == 'cpu':
            model = value.split(',')[0].removeprefix('cputype=')
            require(not model.startswith(('host', 'custom-')) and 'flags=' not in value,
                    'Use a compatible named VM CPU model without custom flags before migrating.')
        if not DISK.fullmatch(key):
            continue
        first = value.split(',')[0]
        if first.startswith('file=') or first.startswith('volume='):
            first = first.split('=', 1)[1]
        if first == 'none' and 'media=cdrom' in value and kind == 'qemu':
            continue
        require('media=cdrom' not in value or first.endswith('-cloudinit'),
                'Eject ISO/physical CD media before migration: ' + key)
        require(first.startswith(storage + ':'), 'Unsupported storage/bind mount: ' + first)
        lv = first.split(':', 1)[1]
        require(re.fullmatch(r'vm-' + str(guest_id) + r'-(?:disk-\d+|cloudinit)', lv),
                'Unexpected/foreign volume name: ' + lv)
        require(lv not in [v['lv'] for v in volumes], 'Duplicate volume reference: ' + lv)
        volumes.append(dict(key=key, lv=lv, volid=first))
    require(volumes, 'No supported volumes found.')
    if kind == 'lxc':
        require('rootfs' in config, 'Container rootfs missing.')
    return volumes


def number(text, label, minimum=0):
    require(str(text).isdigit(), label + ' must be a whole number.')
    value = int(text)
    require(value >= minimum, label + ' is too small.')
    return value


def pool_budget(row, logical_bytes, reserve_gib, ceiling):
    size = float(row['lv_size'])
    data = float(row['data_percent'])
    meta = float(row['metadata_percent'])
    require(all(math.isfinite(x) for x in (size, data, meta)), 'Invalid thin-pool metrics.')
    require(size > 0 and 0 <= data < ceiling and 0 <= meta < ceiling,
            'Thin pool data/metadata at safety ceiling or invalid.')
    # Worst case: every logical byte allocated, plus reserve AND a utilization ceiling.
    free_budget = min(size * (100 - data) / 100 - reserve_gib * GIB,
                      size * (ceiling - data) / 100)
    require(logical_bytes <= free_budget,
            f'Target pool budget {free_budget/GIB:.1f} GiB is below worst-case '
            f'{logical_bytes/GIB:.1f} GiB. Sparse savings are not assumed.')
    return free_budget


def guest_result(text):
    value = json.loads(text)
    require(value.get('exited') == 1 and value.get('exitcode') == 0
            and value.get('signal') is None, 'Guest command failed or remains active: ' + text)


class Host:
    def __init__(self, address=None):
        self.address = address

    def argv(self, command):
        if not self.address:
            return ['bash', '-o', 'pipefail', '-c', command]
        return ['ssh', '-T', '-o', 'BatchMode=yes', '-o', 'StrictHostKeyChecking=yes',
                '-o', 'ConnectTimeout=10', '-o', 'ServerAliveInterval=10',
                '-o', 'ServerAliveCountMax=3', 'root@' + self.address,
                'bash -o pipefail -c ' + shlex.quote(command)]

    def run(self, args, timeout=90, check=True):
        return self.shell(shlex.join([str(a) for a in args]), timeout, check)

    def shell(self, command, timeout=90, check=True):
        result = sp.run(self.argv('export LC_ALL=C LVM_SUPPRESS_FD_WARNINGS=1; ' + command),
                        stdin=sp.DEVNULL, stdout=sp.PIPE, stderr=sp.PIPE,
                        text=True, timeout=timeout)
        if check and result.returncode:
            raise Refuse(f'{self.address or "source"}: {command}\n{result.stderr.strip()}\n{result.stdout.strip()}')
        return result.stdout.strip() if check else result

    def api(self, path, *args):
        return json.loads(self.run(['pvesh', 'get', path, *args, '--output-format', 'json']))

    def lvs(self, vg):
        result = json.loads(self.run(['lvs', '--reportformat', 'json', '--units', 'b',
                                     '--nosuffix', '-o', 'lv_name,lv_uuid,lv_size,lv_attr,pool_lv,origin,data_percent,metadata_percent', vg]))
        return {r['lv_name'].strip(): {k: v.strip() for k, v in r.items()}
                for r in result['report'][0]['lv']}


class Migration:
    def __init__(self, args):
        self.a = args
        self.src = Host()
        self.dst = Host(args.target_ip)
        self.source = Path('/etc/pve/local').resolve().name
        self.guests = []
        self.lease = None
        self.journal = None
        self.record = {}
        self.logfile = None

    def log(self, text):
        line = f'[{dt.datetime.now().isoformat(timespec="seconds")}] {text}'
        print(line, flush=True)
        if self.logfile:
            self.logfile.write(line + '\n')
            self.logfile.flush()

    def phase(self, phase, **changes):
        self.record.update(changes, phase=phase)
        temporary = self.journal.with_suffix('.tmp')
        with temporary.open('w', encoding='utf-8') as f:
            json.dump(self.record, f, indent=2)
            f.flush()
            os.fsync(f.fileno())
        os.replace(temporary, self.journal)
        fd = os.open(str(self.journal.parent), os.O_RDONLY)
        try:
            os.fsync(fd)
        finally:
            os.close(fd)
        self.log(phase)

    def cluster(self):
        for host in (self.src, self.dst):
            data = host.api('/cluster/status')
            require(any(r.get('type') == 'cluster' and r.get('quorate') == 1 for r in data),
                    'Both nodes must see a quorate cluster.')
        require(self.dst.run(['cat', '/etc/pve/corosync.conf']) ==
                self.src.run(['cat', '/etc/pve/corosync.conf']), 'Different cluster configurations.')

    def exclusions(self, ids):
        for r in self.src.api('/cluster/ha/resources'):
            require(r.get('sid', '').split(':')[-1] not in ids, 'Selected guest is HA-managed: ' + str(r))
        for r in self.src.api('/cluster/replication'):
            require(str(r.get('guest', str(r.get('id', '')).split('-')[0])) not in ids,
                    'Remove selected guest replication jobs before migrating.')

    def state(self, host, g):
        node = self.source if host is self.src else self.a.target_node
        return host.api(f'/nodes/{node}/{g["kind"]}/{g["id"]}/status/current')['status']

    def resources(self, remaining):
        status = self.dst.api(f'/nodes/{self.a.target_node}/status')
        need = sum(g['memory'] for g in remaining if g['state'] == 'running') + self.a.reserve_mib
        free = int(status['memory']['free']) // MIB
        require(free >= need, f'Target free RAM {free} MiB; need {need} MiB including host reserve.')
        return status

    def capacity(self, remaining_bytes):
        pool = self.dst.lvs(self.vg).get(self.pool)
        require(pool is not None and pool['lv_attr'].startswith('t'), 'Target thin pool missing.')
        pool_budget(pool, remaining_bytes, self.a.reserve_gib, self.a.pool_ceiling)

    def guest_command(self, g, command, seconds=330):
        if g['kind'] == 'lxc':
            self.src.run(['timeout', '--kill-after=10s', str(seconds), 'pct', 'exec', g['id'],
                          '--', '/bin/sh', '-c', command], timeout=seconds + 30)
        else:
            result = self.src.run(['qm', 'guest', 'exec', g['id'], '--synchronous', '1',
                                   '--timeout', str(seconds), '--', '/bin/sh', '-c', command],
                                  timeout=seconds + 45)
            guest_result(result)

    def preflight(self):
        require(self.source != self.a.target_node and (ROOT / self.source).is_dir(), 'Invalid source/target.')
        require(self.dst.run(['readlink', '-f', '/etc/pve/local']).split('/')[-1] == self.a.target_node,
                'Target IP does not match target node name.')
        self.cluster()
        commands = ['bash', 'dd', 'pv', 'sha256sum', 'lvs', 'lvcreate', 'lvchange', 'blockdev',
                    'pvesh', 'pvesm', 'qm', 'pct', 'flock', 'journalctl', 'timeout', 'ip', 'dpkg']
        if self.a.check_fs:
            commands += ['blkid', 'e2fsck']
        for host in (self.src, self.dst):
            host.shell('command -v ' + ' '.join(commands))
        for package in ('pve-manager', 'qemu-server', 'pve-container'):
            versions = [h.run(['dpkg-query', '-W', '-f=${Version}', package]) for h in (self.src, self.dst)]
            self.src.run(['dpkg', '--compare-versions', versions[1], 'ge', versions[0]])
        storage = self.src.api('/storage/' + self.a.storage)
        require(storage.get('type') == 'lvmthin' and not storage.get('shared') and not storage.get('disable'),
                'Storage must be enabled, non-shared LVM-thin.')
        self.vg, self.pool = storage['vgname'], storage['thinpool']
        require(all(re.fullmatch(r'[A-Za-z0-9_+.-]+', v) and not v.startswith('-') for v in (self.vg, self.pool)),
                'Unsupported VG/pool name.')
        nodes = storage.get('nodes', '').split(',')
        require(nodes == [''] or {self.source, self.a.target_node}.issubset(nodes), 'Storage node restriction.')
        for host, node in ((self.src, self.source), (self.dst, self.a.target_node)):
            s = host.api(f'/nodes/{node}/storage/{self.a.storage}/status')
            require(s.get('active') == 1 and s.get('enabled') == 1, 'Storage is not active/enabled on ' + node)
        available = {str(g['vmid']): g for g in self.src.api('/cluster/resources', '--type', 'vm')
                     if g.get('node') == self.source and g.get('type') in ('qemu', 'lxc')}
        ids = sorted(available, key=int) if self.a.ids == ['all'] else list(dict.fromkeys(self.a.ids))
        require(ids and all(i in available for i in ids), 'Select IDs hosted on the source, or all.')
        self.exclusions(ids)
        pbx = set(ids) if self.a.pbx == 'all' else set(filter(None, self.a.pbx.split(',')))
        require(pbx.issubset(ids), '--pbx contains an ID not selected for migration.')
        src_lvs, dst_lvs = self.src.lvs(self.vg), self.dst.lvs(self.vg)
        for guest_id in ids:
            kind = available[guest_id]['type']
            directory = 'lxc' if kind == 'lxc' else 'qemu-server'
            path = ROOT / self.source / directory / (guest_id + '.conf')
            text = path.read_text()
            config = parse_config(text)
            volumes = volume_list(config, kind, guest_id, self.a.storage)
            require(not (ROOT / self.a.target_node / directory / path.name).exists(), 'Target config exists.')
            for v in volumes:
                row = src_lvs.get(v['lv'])
                require(row and row['lv_attr'].startswith('V') and row['pool_lv'] == self.pool,
                        'Volume is not in the selected source thin pool: ' + v['lv'])
                require(not row['origin'] and not any(r['origin'] == v['lv'] for r in src_lvs.values()),
                        'LVM snapshots/clones are unsupported: ' + v['lv'])
                require(v['lv'] not in dst_lvs, 'Target LV already exists: ' + v['lv'])
                v.update(size=int(float(row['lv_size'])), source_uuid=row['lv_uuid'])
                require(v['size'] > 0 and v['size'] % 4096 == 0, 'Volume size is not direct-I/O aligned.')
                # Resolve through Proxmox too: do not assume the storage ID maps to /dev/pve.
                resolved = self.src.run(['pvesm', 'path', v['volid']])
                expected = f'/dev/{self.vg}/{v["lv"]}'
                require(self.src.run(['readlink', '-f', resolved]) == self.src.run(['readlink', '-f', expected]),
                        'Proxmox volume path mismatch: ' + v['volid'])
            for key, value in config.items():
                if re.fullmatch(r'net\d+', key):
                    bridge = re.search(r'(?:^|,)bridge=([^,]+)', value)
                    if bridge:
                        self.dst.run(['ip', 'link', 'show', 'dev', bridge[1]])
                    require(not re.search(r'(?:^|,)(?:tag|trunks)=', value) or self.a.ack_network,
                            'VLAN present: verify destination switch/VLANs and use --ack-network.')
            g = dict(id=guest_id, kind=kind, cli='pct' if kind == 'lxc' else 'qm',
                     directory=directory, config=config, original=text, volumes=volumes,
                     memory=number(config.get('memory', '512'), 'memory', 16),
                     settings={}, pbx=guest_id in pbx, name=config.get('hostname', config.get('name', 'unnamed')))
            g['state'] = self.state(self.src, g)
            require(g['state'] in ('running', 'stopped'), 'Unsupported guest state.')
            self.guests.append(g)
        target_status = self.dst.api(f'/nodes/{self.a.target_node}/status')
        print(f'Target: {target_status["memory"]["free"]/GIB:.1f} GiB free RAM; '
              f'{target_status["cpuinfo"]["cpus"]} logical CPUs; CPU {target_status.get("cpu", 0):.0%}')
        for g in self.guests:
            c = g['config']
            if self.a.edit_resources:
                for key, default, minimum in [('memory', g['memory'], 16), ('cores', c.get('cores', '0' if g['kind'] == 'lxc' else '1'), 0 if g['kind'] == 'lxc' else 1)]:
                    value = number(input(f'{g["id"]} {key} [{default}]: ') or str(default), key, minimum)
                    if key != 'cores' or value != 0:
                        g['settings'][key] = value
                if g['kind'] == 'lxc':
                    g['settings']['swap'] = number(input(f'{g["id"]} swap MiB [{c.get("swap", "512")}]: ') or c.get('swap', '512'), 'swap')
                g['memory'] = g['settings']['memory']
            cores = int(g['settings'].get('cores', c.get('cores', 0 if g['kind'] == 'lxc' else 1)))
            sockets = int(c.get('sockets', 1)) if g['kind'] == 'qemu' else 1
            require(cores * sockets <= int(target_status['cpuinfo']['cpus']), 'Guest CPU count exceeds target CPUs.')
            require(g['memory'] >= int(c.get('balloon', 0)), 'Memory below balloon minimum.')
            require(cores * sockets >= int(c.get('vcpus', 0)), 'Cores below configured hotplug vCPUs.')
            if g['pbx'] and g['state'] == 'running':
                self.guest_command(g, FW, 30)
        self.capacity(sum(v['size'] for g in self.guests for v in g['volumes']))
        self.resources(self.guests)
        print('\nPlan (offline throughout transfer and verification):')
        for g in self.guests:
            print(f'  {g["kind"]} {g["id"]} {g["name"]}: {g["state"]}, '
                  f'{sum(v["size"] for v in g["volumes"])/GIB:.2f} GiB logical, '
                  f'{g["memory"]} MiB RAM, FreePBX={g["pbx"]}, changes={g["settings"]}')
        print(f'Rate {self.a.rate_mib} MiB/s for copying and each checksum read. Originals retained.')

    def lease_start(self):
        command = f'exec 9>{LOCK}; flock -n 9 || exit 73; echo READY; cat >/dev/null'
        self.lease = sp.Popen(self.dst.argv(command), stdin=sp.PIPE, stdout=sp.PIPE, stderr=sp.PIPE, text=True)
        # Startup has an explicit deadline even if authentication/server startup stalls.
        import select
        require(select.select([self.lease.stdout], [], [], 30)[0], 'Target lock acquisition timed out.')
        require(self.lease.stdout.readline().strip() == 'READY', 'Target migration lock unavailable.')

    def health(self):
        require(self.lease is not None and self.lease.poll() is None, 'Target lock connection lost.')
        self.capacity(0)
        for host in (self.src, self.dst):
            result = host.run(['journalctl', '-k', '--since', self.since, '--no-pager', '-o', 'cat'])
            require(not re.search(r'segfault|out of memory|oom-kill|I/O error|hardware error|machine check|BUG:|kernel panic', result, re.I),
                    'New kernel fault detected on ' + (host.address or 'source') + '. Inspect journalctl -k.')

    def pipeline(self, commands, output=False):
        """Bounded OS pipes, checked status of EVERY process, periodic fault checks."""
        processes = []
        previous = None
        try:
            for n, command in enumerate(commands):
                last = n == len(commands) - 1
                p = sp.Popen(command, stdin=previous if previous else sp.DEVNULL,
                             stdout=sp.PIPE if not last or output else sp.DEVNULL,
                             stderr=self.logfile, start_new_session=True)
                if previous:
                    previous.close()
                previous = p.stdout if not last else None
                processes.append(p)
            next_check = 0
            while any(p.poll() is None for p in processes):
                require(not any(p.poll() not in (None, 0) for p in processes), 'Transfer/checksum process failed.')
                if time.monotonic() >= next_check:
                    self.health()
                    next_check = time.monotonic() + 10
                time.sleep(.25)
            require(all(p.returncode == 0 for p in processes), 'Transfer/checksum pipeline failed.')
            return processes[-1].stdout.read().decode().strip() if output else ''
        finally:
            if previous:
                previous.close()
            for p in processes:
                if p.poll() is None:
                    os.killpg(p.pid, signal.SIGTERM)
            for p in processes:
                try:
                    p.wait(timeout=10)
                except sp.TimeoutExpired:
                    os.killpg(p.pid, signal.SIGKILL)
                    p.wait()
                if p.stdout:
                    p.stdout.close()

    def checksum(self, host, path):
        command = shlex.join(['dd', 'if=' + path, 'bs=4M', 'iflag=direct', 'status=none'])
        command += f' | pv -q -L {self.a.rate_mib * MIB} -B 4M | sha256sum'
        result = self.pipeline([host.argv(command)], output=True)
        require(re.fullmatch(r'[0-9a-f]{64}\s+-', result), 'Malformed checksum output.')
        return result.split()[0]

    def migrate(self, g, remaining):
        path = ROOT / self.source / g['directory'] / (g['id'] + '.conf')
        target = ROOT / self.a.target_node / g['directory'] / path.name
        self.cluster()
        self.exclusions([g['id']])
        require(path.read_text() == g['original'] and not target.exists(), 'Guest config/location changed.')
        require(self.state(self.src, g) == g['state'], 'Guest state changed since plan.')
        self.resources(remaining)
        self.capacity(sum(v['size'] for x in remaining for v in x['volumes']))
        self.since = dt.datetime.now(dt.timezone.utc).strftime('%Y-%m-%d %H:%M:%S UTC')
        self.journal = self.run_dir / (g['id'] + '.json')
        self.record = dict(source=self.source, target=self.a.target_node, target_ip=self.a.target_ip,
                           storage=self.a.storage, vg=self.vg, pool=self.pool, guest=g,
                           created=[], checksums={})
        self.phase('prepared')
        if g['state'] == 'running':
            if g['pbx']:
                self.phase('stopping-freepbx')
                self.guest_command(g, FW + ' && exec fwconsole stop')
            self.phase('shutting-down')
            self.src.run([g['cli'], 'shutdown', g['id'], '--timeout', self.a.shutdown_timeout,
                          '--forceStop', '0'], timeout=self.a.shutdown_timeout + 60)
        require(self.state(self.src, g) == 'stopped', 'Guest did not stop; never force-stopping.')
        # pct shutdown has no skiplock option. Acquire the migration lock AFTER
        # graceful shutdown, then recheck config/state before reading any disk.
        require(path.read_text() == g['original'], 'Configuration changed during shutdown.')
        digest = self.src.api(f'/nodes/{self.source}/{g["kind"]}/{g["id"]}/config')['digest']
        require(path.read_text() == g['original'], 'Configuration changed before locking.')
        self.phase('locking')
        self.src.run([g['cli'], 'set', g['id'], '--lock', 'migrate', '--digest', digest])
        locked = parse_config(path.read_text())
        require(locked.pop('lock', None) == 'migrate' and locked == g['config'], 'Config changed while acquiring lock.')
        require(self.state(self.src, g) == 'stopped', 'Guest restarted while acquiring lock.')
        self.phase('copying')
        for v in g['volumes']:
            lv = v['lv']
            device = f'/dev/{self.vg}/{lv}'
            self.src.run(['lvchange', '-ay', f'{self.vg}/{lv}'])
            source_row = self.src.lvs(self.vg)[lv]
            require(source_row['lv_uuid'] == v['source_uuid'] and int(float(source_row['lv_size'])) == v['size'],
                    'Source LV identity/size changed.')
            require(len(source_row['lv_attr']) > 5 and source_row['lv_attr'][5] == '-', 'Source volume is in use.')
            require(lv not in self.dst.lvs(self.vg), 'Target LV appeared since preflight.')
            self.phase('creating-volume', creating=lv)
            self.dst.run(['lvcreate', '-q', '-V', str(v['size']) + 'B', '-T', f'{self.vg}/{self.pool}', '-n', lv])
            row = self.dst.lvs(self.vg)[lv]
            self.record['created'].append(dict(lv=lv, uuid=row['lv_uuid']))
            self.phase('copying-volume', creating=None)
            self.dst.run(['lvchange', '-ay', f'{self.vg}/{lv}'])
            require(int(self.dst.run(['blockdev', '--getsize64', device])) == v['size'], 'Target size differs.')
            self.log(f'{g["id"]}: copying {lv}, {v["size"]/GIB:.2f} GiB')
            # No compression, buffered disk reads, or normal Proxmox migration path.
            self.pipeline([
                ['dd', 'if=' + device, 'bs=4M', 'iflag=direct', 'status=progress'],
                ['pv', '-q', '-L', str(self.a.rate_mib * MIB), '-B', '4M'],
                self.dst.argv(shlex.join(['dd', 'of=' + device, 'bs=4M', 'iflag=fullblock',
                                         'oflag=direct', 'conv=sparse,fsync', 'status=none']))])
            self.phase('verifying-volume')
            source_sum = self.checksum(self.src, device)
            require(source_sum == self.checksum(self.dst, device), 'Checksum mismatch: ' + lv)
            self.record['checksums'][lv] = source_sum
            self.phase('verified-volume')
            if self.a.check_fs and g['kind'] == 'lxc' and not v['key'].startswith('unused'):
                fs = self.dst.run(['blkid', '-o', 'value', '-s', 'TYPE', device])
                require(fs in ('ext2', 'ext3', 'ext4'), '--check-fs supports ext2/3/4 containers only.')
                # No automatic repair: a text heuristic cannot guarantee safe repairs.
                self.dst.run(['e2fsck', '-fn', device], timeout=3600)
        self.health()
        self.cluster()
        self.exclusions([g['id']])
        now = parse_config(path.read_text())
        require(now.pop('lock', None) == 'migrate' and now == g['config'], 'Locked configuration changed.')
        require(self.state(self.src, g) == 'stopped' and not target.exists(), 'Unsafe handoff state.')
        # Recheck both identities after verification, before changing ownership.
        source_rows, target_rows = self.src.lvs(self.vg), self.dst.lvs(self.vg)
        for v, created in zip(g['volumes'], self.record['created']):
            require(source_rows[v['lv']]['lv_uuid'] == v['source_uuid'] and
                    target_rows[v['lv']]['lv_uuid'] == created['uuid'], 'Volume identity changed before handoff.')
        # Deactivate originals BEFORE ownership changes, with the guest still locked.
        for v in g['volumes']:
            self.src.run(['lvchange', '-an', f'{self.vg}/{v["lv"]}'])
        self.phase('handoff-pending')
        path.rename(target)
        require(target.exists() and not path.exists(), 'Configuration handoff could not be verified.')
        self.phase('on-target')
        require(self.state(self.dst, g) == 'stopped', 'Target unexpectedly running.')
        self.phase('unlocking-target')
        self.dst.run([g['cli'], 'unlock', g['id']])
        if g['settings']:
            self.phase('applying-resources')
            config = self.dst.api(f'/nodes/{self.a.target_node}/{g["kind"]}/{g["id"]}/config')
            args = [g['cli'], 'set', g['id'], '--digest', config['digest']]
            for key, value in g['settings'].items():
                args += ['--' + key, value]
            self.dst.run(args)
            config = self.dst.api(f'/nodes/{self.a.target_node}/{g["kind"]}/{g["id"]}/config')
            require(all(str(config.get(k)) == str(v) for k, v in g['settings'].items()), 'Resource verification failed.')
        if g['state'] == 'running':
            self.resources(remaining)
            self.phase('starting-target')
            self.dst.run([g['cli'], 'start', g['id']], timeout=600)
            time.sleep(5)
            require(self.state(self.dst, g) == 'running', 'Target did not remain running.')
        self.phase('complete')
        self.log(f'{g["id"]} complete. Original LVs retained. Check application health on target.')

    def run(self):
        import fcntl
        require(os.geteuid() == 0, 'Run as root on the source Proxmox node.')
        os.umask(0o077)
        with open(LOCK, 'w') as local_lock:
            fcntl.flock(local_lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            try:
                self.lease_start()
                self.preflight()
                if self.a.dry_run:
                    self.log('Dry run passed. No guest/storage changes; no journal created.')
                    return
                require(sys.stdin.isatty(), 'Interactive terminal required for confirmation.')
                require(input('Type MIGRATE to start: ') == 'MIGRATE', 'Cancelled.')
                self.run_dir = Path('/var/lib/pve-safe-migrate') / dt.datetime.now().strftime('%Y%m%d-%H%M%S-%f')
                self.run_dir.mkdir(parents=True, mode=0o700)
                self.logfile = (self.run_dir / 'migration.log').open('a', buffering=1)
                self.log('Recovery records: ' + str(self.run_dir))
                for index, g in enumerate(self.guests):
                    self.migrate(g, self.guests[index:])
                    if index + 1 < len(self.guests):
                        time.sleep(self.a.pause)
                self.log('Batch complete.')
            finally:
                if self.lease:
                    if self.lease.stdin:
                        self.lease.stdin.close()
                    try:
                        self.lease.wait(timeout=10)
                    except sp.TimeoutExpired:
                        self.lease.kill()
                        self.lease.wait()
                if self.logfile:
                    self.logfile.close()


def parser():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument('--dry-run', action='store_true')
    p.add_argument('--storage', default='local-lvm')
    p.add_argument('--rate-mib', type=int, default=10)
    p.add_argument('--pause', type=int, default=30)
    p.add_argument('--shutdown-timeout', type=int, default=300)
    p.add_argument('--reserve-mib', type=int, default=2048)
    p.add_argument('--reserve-gib', type=int, default=10)
    p.add_argument('--pool-ceiling', type=int, default=85)
    p.add_argument('--pbx', default='', metavar='all|ID,ID', help='Explicit FreePBX IDs; VMs need QEMU Guest Agent.')
    p.add_argument('--edit-resources', action='store_true', help='Prompt for RAM/cores and container swap.')
    p.add_argument('--check-fs', action='store_true', help='Read-only ext filesystem check for CT copies.')
    p.add_argument('--ack-network', action='store_true', help='Acknowledge VLAN/switch configuration was checked.')
    p.add_argument('target_node', nargs='?')
    p.add_argument('target_ip', nargs='?')
    p.add_argument('ids', nargs='*')
    return p


def wizard(a):
    require(sys.stdin.isatty(), 'Interactive terminal required, or supply target node, address and IDs.')
    host = Host()
    source = Path('/etc/pve/local').resolve().name
    guests = host.api('/cluster/resources', '--type', 'vm')
    print('Local guests:')
    for g in sorted(guests, key=lambda x: int(x['vmid'])):
        if g.get('node') == source and g.get('type') in ('qemu', 'lxc'):
            print(f'  {g["vmid"]:<9} {g["type"]:<6} {g.get("status", "unknown"):<10} {g.get("name", "unnamed")}')
    a.ids = input('Guest IDs (spaces/commas) or all: ').replace(',', ' ').split()
    while True:
        nodes = host.api('/nodes')
        print('\nNODE                     STATUS    RAM used/total GiB   CPUs  CPU%')
        for n in sorted(nodes, key=lambda x: x['node']):
            online = n.get('status') == 'online'
            ram = f'{n.get("mem", 0)/GIB:.1f}/{n.get("maxmem", 0)/GIB:.1f}' if online else 'N/A'
            cpus = n.get('maxcpu', '?') if online else 'N/A'
            cpu = f'{n.get("cpu", 0):.0%}' if online else 'N/A'
            print(f'{n["node"]:<24} {n.get("status", "unknown"):<9} {ram:<20} {cpus!s:<5} {cpu}')
        choice = input('Destination node (r = refresh): ').strip()
        if choice == 'r':
            continue
        require(any(n['node'] == choice and n.get('status') == 'online' for n in nodes), 'Choose an online node.')
        a.target_node = choice
        break
    a.target_ip = input(f'Destination SSH IP/hostname [{a.target_node}]: ').strip() or a.target_node
    for key, label in [('rate_mib', 'Transfer/read rate MiB/s'), ('pause', 'Pause seconds'),
                       ('shutdown_timeout', 'Graceful shutdown timeout seconds'),
                       ('reserve_mib', 'Destination host RAM reserve MiB')]:
        default = getattr(a, key)
        setattr(a, key, number(input(f'{label} [{default}]: ') or str(default), label))
    a.pbx = input('FreePBX IDs separated by commas, all, or none [all]: ').strip() or 'all'
    if a.pbx == 'none':
        a.pbx = ''
    a.edit_resources = (input('Review/edit RAM, CPU cores and container swap? [Y/n]: ').lower() != 'n')
    a.ack_network = input('Have you checked destination bridges, VLANs and switch ports? [y/N]: ').lower() == 'y'
    return a


def main():
    a = parser().parse_args()
    require(sys.platform.startswith('linux'), 'Execution requires Linux/Proxmox; --help works elsewhere.')
    require(os.geteuid() == 0, 'Run as root on the source Proxmox node.')
    if a.target_node is None and a.target_ip is None and not a.ids:
        a = wizard(a)
    require(a.target_node and a.target_ip and a.ids, 'Supply target node, target address and guest IDs, or no positional arguments for the wizard.')
    require(re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9.-]*', a.target_node), 'Invalid target node.')
    require(re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9.:-]*', a.target_ip), 'Invalid target address.')
    require(re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9_-]*', a.storage), 'Invalid storage ID.')
    require(a.ids == ['all'] or all(re.fullmatch(r'[1-9][0-9]{2,8}', i) for i in a.ids), 'Invalid guest IDs.')
    require(1 <= a.rate_mib <= 1024 and 0 <= a.pause <= 86400 and 30 <= a.shutdown_timeout <= 3600,
            'Invalid rate (1..1024), pause (0..86400), or timeout (30..3600).')
    require(a.reserve_mib >= 512 and a.reserve_gib >= 1 and 50 <= a.pool_ceiling <= 95, 'Invalid safety reserves.')
    def interrupted(signum, frame):
        raise Refuse('Interrupted. Remote/server-side tasks may still be active.')
    for sig in (signal.SIGINT, signal.SIGTERM, signal.SIGHUP):
        signal.signal(sig, interrupted)
    Migration(a).run()


if __name__ == '__main__':
    try:
        main()
    except (Refuse, OSError, ValueError, KeyError, EOFError, sp.SubprocessError) as exc:
        print(f'\nSTOPPED: {exc}\nNo automatic deletion, unlock, or restart on failure. '
              'Inspect /var/lib/pve-safe-migrate and RECOVERY.md before acting.', file=sys.stderr)
        sys.exit(1)
