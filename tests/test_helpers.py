"""Local safety and simulated workflow tests; never access a Proxmox node."""
import copy
import importlib.util
import json
from pathlib import Path
import shlex
import tempfile
import unittest
from unittest.mock import patch

SCRIPT = Path(__file__).resolve().parents[1] / 'pve-migrate.py'
spec = importlib.util.spec_from_file_location('migration', SCRIPT)
m = importlib.util.module_from_spec(spec)
spec.loader.exec_module(m)


class Helpers(unittest.TestCase):
    def test_source_encoding_and_compilation(self):
        compile(SCRIPT.read_text(), str(SCRIPT), 'exec')
        for name in ('pve-migrate.py', 'pve-migrate.sh'):
            self.assertNotIn(b'\r', (SCRIPT.parent / name).read_bytes())

    def test_container_volume_prefixes_and_unused(self):
        c = m.parse_config('rootfs: volume=local-lvm:vm-101-disk-0,size=8G\n'
                           'mp0: local-lvm:vm-101-disk-1,mp=/data\n'
                           'unused0: local-lvm:vm-101-disk-2\n')
        self.assertEqual(len(m.volume_list(c, 'lxc', '101', 'local-lvm')), 3)

    def test_vm_disk_types_and_empty_cd(self):
        c = {key: f'local-lvm:vm-101-disk-{n}' for n, key in enumerate(
            ('scsi0', 'virtio1', 'sata0', 'ide0', 'efidisk0', 'tpmstate0', 'unused0'))}
        c['ide2'] = 'local-lvm:vm-101-cloudinit,media=cdrom'
        c['ide3'] = 'none,media=cdrom'
        self.assertEqual(len(m.volume_list(c, 'qemu', '101', 'local-lvm')), 8)

    def test_pending_snapshots_and_duplicate_keys(self):
        for text in ('[PENDING]\nmemory: 512', '[snapshot]\n', 'memory: 512\nmemory: 1024', 'garbage'):
            with self.subTest(text=text), self.assertRaises(m.Refuse):
                m.parse_config(text)

    def test_rejected_dependencies(self):
        for key, value in [('lock', 'backup'), ('template', '1'), ('hostpci0', '0000:01:00'),
                           ('hookscript', 'local:snippets/hook'), ('args', '-device'),
                           ('lxc.mount.entry', '/data'), ('cicustom', 'user=local:snippets/x'),
                           ('virtiofs0', 'dirid=data'), ('numa0', 'hostnodes=0'),
                           ('cpu', 'host'), ('cpu', 'custom-example'), ('cpu', 'kvm64,flags=+aes'),
                           ('cpu', 'cputype=host'),
                           ('serial0', '/dev/ttyS0'), ('memory', 'current=1024,max=2048')]:
            with self.subTest(key=key), self.assertRaises(m.Refuse):
                m.volume_list({'scsi0': 'local-lvm:vm-101-disk-0', key: value}, 'qemu', '101', 'local-lvm')

    def test_wrong_storage_foreign_disks_bind_and_iso(self):
        for disk in ('/data', '/dev/sdb', 'other:vm-101-disk-0', 'local-lvm:vm-102-disk-0',
                     'local:iso/os.iso,media=cdrom', 'local-lvm:vm-101-disk-0;rm -rf /'):
            with self.subTest(disk=disk), self.assertRaises(m.Refuse):
                m.volume_list({'rootfs': disk}, 'lxc', '101', 'local-lvm')

    def test_duplicate_and_missing_disks(self):
        for c in ({}, {'memory': '512'}, {'scsi0': 'local-lvm:vm-101-disk-0',
                                         'unused0': 'local-lvm:vm-101-disk-0'}):
            with self.subTest(c=c), self.assertRaises(m.Refuse):
                m.volume_list(c, 'qemu', '101', 'local-lvm')

    def test_full_size_budget_and_metadata_ceiling(self):
        row = dict(lv_size=100*m.GIB, data_percent='20', metadata_percent='5')
        self.assertEqual(m.pool_budget(row, 65*m.GIB, 10, 85), 65*m.GIB)
        with self.assertRaises(m.Refuse):
            m.pool_budget(row, 66*m.GIB, 10, 85)
        for key, value in [('metadata_percent', '85'), ('data_percent', '90'),
                           ('metadata_percent', 'nan'), ('lv_size', '0')]:
            with self.subTest(key=key), self.assertRaises(m.Refuse):
                m.pool_budget(dict(row, **{key: value}), 0, 10, 85)

    def test_reserve_is_independent_of_percentage_ceiling(self):
        row = dict(lv_size=100*m.GIB, data_percent='20', metadata_percent='5')
        self.assertEqual(m.pool_budget(row, 0, 30, 85), 50*m.GIB)

    def test_guest_agent_failure_and_unfinished(self):
        m.guest_result(json.dumps(dict(exited=1, exitcode=0)))
        for data in ({}, dict(pid=42), dict(exited=0), dict(exited=1, exitcode=1),
                     dict(exited=1, exitcode=0, signal=9)):
            with self.subTest(data=data), self.assertRaises(m.Refuse):
                m.guest_result(json.dumps(data))

    def test_remote_pipeline_uses_pipefail_and_strict_ssh(self):
        command = 'dd if=/dev/pve/test iflag=direct | sha256sum'
        argv = m.Host('192.0.2.1').argv(command)
        self.assertIn('StrictHostKeyChecking=yes', argv)
        self.assertIn('BatchMode=yes', argv)
        self.assertEqual(shlex.split(argv[-1]), ['bash', '-o', 'pipefail', '-c', command])

    def test_ram_budget_ignores_stopped_guests(self):
        obj = m.Migration(m.parser().parse_args(['node2', '192.0.2.1', '101']))
        obj.dst.api = lambda path: dict(memory=dict(free=4096*m.MIB))
        obj.resources([dict(memory=2048, state='running'), dict(memory=65536, state='stopped')])
        with self.assertRaises(m.Refuse):
            obj.resources([dict(memory=2049, state='running')])


class FakeHost:
    def __init__(self, workflow, target=False):
        self.w = workflow
        self.target = target
        self.address = 'node2' if target else None
        self.rows = {} if target else {'vm-101-disk-0': dict(
            lv_uuid='source-uuid', lv_size=str(m.GIB), lv_attr='Vwi-a-tz--')}

    def path(self):
        node = 'node2' if self.target else 'node1'
        return self.w.root / node / self.w.g['directory'] / '101.conf'

    def api(self, path):
        if path.endswith('/config'):
            return dict(m.parse_config(self.path().read_text()), digest='test-digest')
        raise AssertionError(path)

    def lvs(self, vg):
        return self.rows

    def argv(self, cmd):
        return ['remote' if self.target else 'local', cmd]

    def run(self, args, **kwargs):
        args = list(map(str, args))
        self.w.calls.append((self.target, args, self.w.target_path.exists()))
        if args[0] in ('pct', 'qm'):
            action = args[1]
            if action == 'shutdown':
                if self.w.failure == 'shutdown':
                    raise m.Refuse('Shutdown timed out')
                self.w.states[self.target] = 'stopped'
            elif action == 'set':
                conf = m.parse_config(self.path().read_text())
                for index in range(3, len(args), 2):
                    if args[index] != '--digest':
                        conf[args[index][2:]] = args[index + 1]
                self.path().write_text(''.join(f'{k}: {v}\n' for k, v in conf.items()))
            elif action == 'unlock':
                conf = m.parse_config(self.path().read_text())
                conf.pop('lock', None)
                self.path().write_text(''.join(f'{k}: {v}\n' for k, v in conf.items()))
            elif action == 'start':
                if self.w.failure == 'start':
                    raise m.Refuse('Target startup failed')
                self.w.states[self.target] = 'running'
            else:
                raise AssertionError(args)
        elif args[0] == 'lvcreate':
            self.rows[args[-1]] = dict(lv_uuid='target-uuid', lv_size=str(m.GIB), lv_attr='Vwi-a-tz--')
        elif args[0] == 'blockdev':
            return str(m.GIB)
        elif args[0] == 'lvchange':
            if args[1] == '-an' and self.w.failure == 'deactivate':
                raise m.Refuse('Volume busy')
        else:
            raise AssertionError(args)
        return ''


class Workflow(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.root_patch = patch.object(m, 'ROOT', self.root)
        self.root_patch.start()
        self.addCleanup(self.root_patch.stop)
        self.sleep_patch = patch.object(m.time, 'sleep', lambda duration: None)
        self.sleep_patch.start()
        self.addCleanup(self.sleep_patch.stop)

    def prepare(self, kind='qemu', running=True, failure=None, settings=None):
        self.failure = failure
        self.calls, self.phases, self.pipelines = [], [], []
        directory = 'qemu-server' if kind == 'qemu' else 'lxc'
        key = 'scsi0' if kind == 'qemu' else 'rootfs'
        text = f'{key}: local-lvm:vm-101-disk-0\nmemory: 512\n'
        for node in ('node1', 'node2'):
            (self.root / node / directory).mkdir(parents=True, exist_ok=True)
        self.source_path = self.root / 'node1' / directory / '101.conf'
        self.target_path = self.root / 'node2' / directory / '101.conf'
        self.source_path.write_text(text)
        self.states = {False: 'running' if running else 'stopped', True: 'stopped'}
        self.g = dict(id='101', kind=kind, cli='qm' if kind == 'qemu' else 'pct',
                      directory=directory, original=text, config=m.parse_config(text),
                      volumes=[dict(lv='vm-101-disk-0', key=key, size=m.GIB, source_uuid='source-uuid')],
                      state=self.states[False], pbx=False, memory=512, settings=settings or {})
        obj = m.Migration(m.parser().parse_args(['node2', '192.0.2.1', '101']))
        obj.source, obj.vg, obj.pool = 'node1', 'pve', 'data'
        obj.run_dir = self.root
        obj.src, obj.dst = FakeHost(self), FakeHost(self, target=True)
        obj.cluster = lambda: None
        obj.exclusions = lambda ids: None
        obj.resources = lambda guests: None
        obj.capacity = lambda size: None
        obj.state = lambda host, guest: self.states[host.target]
        obj.health = lambda: None
        obj.log = lambda message: None
        def phase(name, **changes):
            obj.record.update(changes, phase=name)
            self.phases.append((name, copy.deepcopy(obj.record)))
        obj.phase = phase
        def pipeline(commands, **kwargs):
            self.pipelines.append(commands)
            if self.failure == 'copy':
                raise m.Refuse('Copy interrupted')
        obj.pipeline = pipeline
        obj.checksum = lambda host, path: ('b' if self.failure == 'checksum' and host.target else 'a') * 64
        self.obj = obj

    def test_vm_success_handoff_order_and_original_retention(self):
        self.prepare(settings={'memory': 1024, 'cores': 2})
        self.obj.migrate(self.g, [self.g])
        self.assertFalse(self.source_path.exists())
        self.assertTrue(self.target_path.exists())
        self.assertEqual(self.states[True], 'running')
        self.assertEqual(self.phases[-1][0], 'complete')
        self.assertEqual(self.obj.record['created'][0]['uuid'], 'target-uuid')
        self.assertEqual(len(self.obj.record['checksums']), 1)
        self.assertTrue(all(not target_exists for target, args, target_exists in self.calls
                            if args[:2] == ['lvchange', '-an']))
        self.assertTrue(all(target_exists for target, args, target_exists in self.calls
                            if args[:2] in (['qm', 'unlock'], ['qm', 'start'])))
        self.assertFalse(any(args[0] == 'lvremove' for _, args, _ in self.calls))
        data_commands = str(self.pipelines)
        for option in ('iflag=direct', 'oflag=direct', 'conv=sparse,fsync'):
            self.assertIn(option, data_commands)

    def test_container_shutdown_before_lock_and_no_invalid_skiplock(self):
        self.prepare(kind='lxc', settings={'memory': 1024, 'swap': 0})
        self.obj.migrate(self.g, [self.g])
        commands = [args for _, args, _ in self.calls]
        shutdown = next(i for i, c in enumerate(commands) if c[:2] == ['pct', 'shutdown'])
        lock = next(i for i, c in enumerate(commands) if '--lock' in c)
        self.assertLess(shutdown, lock)
        self.assertFalse(any('--skiplock' in c for c in commands))
        self.assertEqual(m.parse_config(self.target_path.read_text())['swap'], '0')

    def test_stopped_guest_stays_stopped(self):
        self.prepare(running=False)
        self.obj.migrate(self.g, [self.g])
        self.assertFalse(any(args[1] in ('start', 'shutdown') for _, args, _ in self.calls if args[0] == 'qm'))

    def test_copy_failure_retains_locked_source_and_partial_target(self):
        self.prepare(failure='copy')
        with self.assertRaises(m.Refuse):
            self.obj.migrate(self.g, [self.g])
        self.assertEqual(m.parse_config(self.source_path.read_text())['lock'], 'migrate')
        self.assertFalse(self.target_path.exists())
        self.assertIn('vm-101-disk-0', self.obj.dst.rows)
        self.assertFalse(any('unlock' in args or 'start' in args for _, args, _ in self.calls))

    def test_checksum_failure_never_hands_off(self):
        self.prepare(failure='checksum')
        with self.assertRaises(m.Refuse):
            self.obj.migrate(self.g, [self.g])
        self.assertTrue(self.source_path.exists())
        self.assertNotIn('handoff-pending', [p[0] for p in self.phases])

    def test_deactivation_failure_never_hands_off(self):
        self.prepare(failure='deactivate')
        with self.assertRaises(m.Refuse):
            self.obj.migrate(self.g, [self.g])
        self.assertTrue(self.source_path.exists())
        self.assertFalse(self.target_path.exists())

    def test_start_failure_never_moves_back_or_restarts_source(self):
        self.prepare(failure='start')
        with self.assertRaises(m.Refuse):
            self.obj.migrate(self.g, [self.g])
        self.assertTrue(self.target_path.exists())
        self.assertFalse(self.source_path.exists())
        self.assertEqual(self.phases[-1][0], 'starting-target')
        self.assertFalse(any(not target and args[:2] == ['qm', 'start'] for target, args, _ in self.calls))

    def test_shutdown_failure_leaves_disks_alone(self):
        self.prepare(failure='shutdown')
        with self.assertRaises(m.Refuse):
            self.obj.migrate(self.g, [self.g])
        self.assertEqual(self.obj.dst.rows, {})
        self.assertNotIn('lock', m.parse_config(self.source_path.read_text()))

    def test_changed_config_blocks_mutation(self):
        self.prepare()
        self.source_path.write_text(self.g['original'] + 'cores: 4\n')
        with self.assertRaises(m.Refuse):
            self.obj.migrate(self.g, [self.g])
        self.assertEqual(self.calls, [])

    def test_changed_source_uuid_blocks_creation(self):
        self.prepare()
        self.obj.src.rows['vm-101-disk-0']['lv_uuid'] = 'reused-name'
        with self.assertRaises(m.Refuse):
            self.obj.migrate(self.g, [self.g])
        self.assertEqual(self.obj.dst.rows, {})

    def test_target_lv_collision_is_not_overwritten(self):
        self.prepare()
        self.obj.dst.rows['vm-101-disk-0'] = dict(lv_uuid='somebody-elses')
        with self.assertRaises(m.Refuse):
            self.obj.migrate(self.g, [self.g])
        self.assertEqual(self.obj.dst.rows['vm-101-disk-0']['lv_uuid'], 'somebody-elses')
        self.assertFalse(self.pipelines)

    def test_freepbx_failure_prevents_shutdown_and_disk_creation(self):
        self.prepare()
        self.g['pbx'] = True
        self.obj.guest_command = lambda *args: m.require(False, 'fwconsole stop timed out')
        with self.assertRaises(m.Refuse):
            self.obj.migrate(self.g, [self.g])
        self.assertEqual(self.calls, [])
        self.assertEqual(self.phases[-1][0], 'stopping-freepbx')

    def test_handoff_failure_leaves_source_locked_and_no_target_start(self):
        self.prepare()
        with patch.object(Path, 'rename', side_effect=OSError('quorum lost')):
            with self.assertRaises(OSError):
                self.obj.migrate(self.g, [self.g])
        self.assertEqual(self.phases[-1][0], 'handoff-pending')
        self.assertTrue(self.source_path.exists())
        self.assertFalse(any(args[:2] == ['qm', 'start'] for _, args, _ in self.calls))


if __name__ == '__main__':
    unittest.main()
