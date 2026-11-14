"""Offline checks for the per-user sandbox home datasets, exports and snapshots."""
import configparser
import copy
import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

import yaml

import check_microserver as microserver
from check_microserver import ENV, ROLE, ROOT, assertions, read_yaml
from check_registry import task

HOMES = [{'name': 'alice', 'quota': '20G'}, {'name': 'bob-2', 'quota': '50G'}]
# Stands in for zfs and zpool: state lives in one JSON file and every changing
# call is logged, so the play's real task flow runs without a pool.
STUB = '''#!{python}
import json, sys
from pathlib import Path
state_path = Path({state!r})
state = json.loads(state_path.read_text())
tool, verb, args = Path(sys.argv[0]).name, sys.argv[1], sys.argv[2:]
if tool == 'zpool':
    print('1234567890123456789\\nONLINE')
elif verb == 'list':
    if args[-1] not in state['datasets']:
        sys.exit(1)
    print(args[-1])
elif verb == 'get':
    for name in args[3].split(','):
        print(state['datasets'][args[-1]][name])
else:
    state['calls'].append([verb] + args)
    name, values = args[-1], dict(a.split('=', 1) for a in args[:-1] if '=' in a)
    if verb == 'create':
        parent = state['datasets'][name.rsplit('/', 1)[0]]
        values = dict({{'type': 'filesystem', 'canmount': 'on', 'refquota': 'none',
                       'mountpoint': parent['mountpoint'] + '/' + name.rsplit('/', 1)[1]}}, **values)
        values['mounted'] = 'no' if values['canmount'] == 'off' else 'yes'
        state['datasets'][name] = values
    elif verb == 'set':
        state['datasets'][name].update(values)
    else:
        sys.exit('unexpected ' + verb)
    state_path.write_text(json.dumps(state))
'''


class SandboxHomeTests(unittest.TestCase):
    def setUp(self):
        baseline = microserver.MicroServerTests()
        baseline.setUp()
        self.context = dict(baseline.context, microserver_k8s_storage_enabled=True,
                            microserver_sandbox_homes_enabled=True,
                            microserver_k8s_nfs_client='192.168.30.11',
                            microserver_sandbox_homes=copy.deepcopy(HOMES))
        self.tasks = read_yaml(ROLE / 'tasks/sandbox-homes.yml')

    def render(self, name, **changes):
        return str(ENV.from_string((ROLE / 'templates' / name).read_text()).render(**dict(self.context, **changes)))

    def test_each_home_is_one_single_client_all_squash_export(self):
        lines = [line for line in self.render('sandbox-exports.j2').splitlines() if line and not line.startswith('#')]
        options = '192.168.30.11(rw,sync,all_squash,anonuid=1000,anongid=1000,no_subtree_check,mountpoint,sec=sys)'
        self.assertEqual(lines, ['/srv/microserver/sandbox/alice ' + options,
                                 '/srv/microserver/sandbox/bob-2 ' + options])
        self.assertEqual([line for line in self.render('sandbox-exports.j2', microserver_sandbox_homes=[]).splitlines()
                          if line and not line.startswith('#')], [])
        # Homes stay outside the nas-nfs export the CSI provisioner can write to.
        self.assertNotIn('/srv/microserver/k8s', self.render('sandbox-exports.j2'))

    def test_scope_accepts_only_reviewed_names_quotas_and_one_client(self):
        scope = task(self.tasks, 'Require the sandbox home scope')
        self.assertTrue(assertions(scope, self.context))
        self.assertTrue(assertions(scope, dict(self.context, microserver_sandbox_homes=[])))

        def homes(**change):
            return [dict(HOMES[0], **change), HOMES[1]]
        for field, value in [('microserver_k8s_nfs_client', '192.168.30.0/24'),
                             ('microserver_k8s_nfs_client', '*'),
                             ('microserver_sandbox_uid', 0),
                             ('microserver_sandbox_gid', 65534),
                             ('microserver_sandbox_quota', 'none'),
                             ('microserver_sandbox_snapshots_hourly', 0),
                             ('microserver_sandbox_snapshots_daily', 400),
                             ('microserver_sandbox_homes', {'alice': '20G'}),
                             ('microserver_sandbox_homes', 'alice'),
                             ('microserver_sandbox_homes', homes(name='bob-2')),
                             ('microserver_sandbox_homes', homes(name='Alice')),
                             ('microserver_sandbox_homes', homes(name='alice\n')),
                             ('microserver_sandbox_homes', homes(name='../k8s')),
                             ('microserver_sandbox_homes', homes(name='a b(rw)')),
                             ('microserver_sandbox_homes', homes(name='-alice')),
                             ('microserver_sandbox_homes', homes(quota='0G')),
                             ('microserver_sandbox_homes', homes(quota='501G')),
                             ('microserver_sandbox_homes', homes(quota='1T')),
                             ('microserver_sandbox_homes', homes(quota='20G\n')),
                             ('microserver_sandbox_homes', homes(quota='none'))]:
            with self.subTest(field=field, value=value):
                self.assertFalse(assertions(scope, dict(self.context, **{field: value})))

    def test_datasets_are_created_only_when_missing_and_nothing_is_destroyed(self):
        parent = task(self.tasks, 'Create only the missing sandbox dataset')
        self.assertEqual(parent['when'], 'microserver_sandbox_dataset.rc == 1')
        argv = parent['ansible.builtin.command']['argv']
        for expected in ['canmount=off', 'mountpoint=/srv/microserver/sandbox', 'compression=lz4', 'atime=off',
                         'setuid=off', 'devices=off', 'quota={{ microserver_sandbox_quota }}']:
            self.assertIn(expected, argv)
        home = task(self.tasks, 'Create only the missing sandbox home datasets')
        self.assertEqual(home['when'], 'item.rc == 1')
        self.assertEqual(home['loop'], '{{ microserver_sandbox_home_datasets.results }}')
        self.assertIn('refquota={{ item.item.quota }}', home['ansible.builtin.command']['argv'])
        refuse = task(self.tasks, 'Refuse to mount a new home over an existing path')
        self.assertIn('item.0.rc == 0 or not item.1.stat.exists', refuse['ansible.builtin.assert']['that'])
        mounted = task(self.tasks, 'Require every mounted sandbox home dataset')
        item = {'item': {'item': {'name': 'alice'}}}
        for lines, accepted in [(['filesystem', '/srv/microserver/sandbox/alice', 'yes', '20G', '40G'], True),
                                (['filesystem', '/srv/microserver/sandbox/alice', 'no', '20G', '40G'], False),
                                (['filesystem', '/srv/microserver/k8s/alice', 'yes', '20G', '40G'], False)]:
            self.assertEqual(assertions(mounted, {'item': dict(item, stdout_lines=lines)}), accepted)
        for item in self.tasks:
            argv = item.get('ansible.builtin.command', {}).get('argv', [])
            if argv[:1] == ['zfs']:
                self.assertIn(argv[1], ['list', 'get', 'create', 'set'], item['name'])

    def test_exports_reload_without_restarting_the_nfs_server(self):
        export = task(self.tasks, 'Export each sandbox home to the K3s node only')
        self.assertEqual(export['notify'], 'Reload MicroServer NFS exports')
        self.assertEqual(export['ansible.builtin.template']['dest'], '/etc/exports.d/microserver-sandbox.exports')
        self.assertNotIn('Restart MicroServer NFS', (ROLE / 'tasks/sandbox-homes.yml').read_text())
        handler = task(read_yaml(ROLE / 'handlers/main.yml'), 'Reload MicroServer NFS exports')
        self.assertEqual(handler['ansible.builtin.command']['argv'], ['exportfs', '-ra'])
        root = task(self.tasks, 'Set ownership on each home root only')['ansible.builtin.file']
        self.assertEqual((root['owner'], root['group'], root['mode'], root['follow']),
                         ('{{ microserver_sandbox_uid }}', '{{ microserver_sandbox_gid }}', '2770', False))
        self.assertNotIn('recurse', root)

    def test_active_export_verification_matches_exportfs_output(self):
        verify = task(self.tasks, 'Require every active sandbox home export')
        ENV.tests['search'] = lambda value, pattern: __import__('re').search(pattern, value) is not None
        ENV.filters['regex_escape'] = __import__('re').escape
        self.addCleanup(ENV.tests.pop, 'search')
        self.addCleanup(ENV.filters.pop, 'regex_escape')
        live = ('/srv/microserver/k8s\n\t\t192.168.30.11(sync,wdelay,hide,no_subtree_check,mountpoint,anonuid=2000,'
                'anongid=2000,sec=sys,rw,secure,root_squash,all_squash)\n/srv/microserver/sandbox/alice\n\t\t'
                '192.168.30.11(sync,wdelay,hide,no_subtree_check,mountpoint,anonuid=1000,anongid=1000,sec=sys,rw,'
                'secure,root_squash,all_squash)\n')
        context = dict(self.context, item=HOMES[0], microserver_sandbox_exports={'stdout': live})
        self.assertTrue(assertions(verify, context))
        self.assertFalse(assertions(verify, dict(context, item=HOMES[1])))
        for wrong in [live.replace('anonuid=1000,anongid=1000', 'anonuid=2000,anongid=2000'),
                      live.replace('sandbox/alice\n\t\t192.168.30.11', 'sandbox/alice\n\t\t192.168.30.12'),
                      live.replace('sandbox/alice', 'sandbox/alice2')]:
            self.assertFalse(assertions(verify, dict(context, microserver_sandbox_exports={'stdout': wrong})))

    def test_snapshots_cover_every_home_with_bounded_retention(self):
        config = configparser.ConfigParser()
        config.read_string(self.render('sanoid.conf.j2'))
        self.assertEqual(config.sections(), ['nas/k8s/sandbox', 'template_sandbox_home'])
        self.assertEqual(dict(config['nas/k8s/sandbox']), {'use_template': 'sandbox_home', 'recursive': 'yes',
                                                           'process_children_only': 'yes'})
        self.assertEqual(dict(config['template_sandbox_home']),
                         {'frequently': '0', 'hourly': '24', 'daily': '14', 'weekly': '4', 'monthly': '0',
                          'yearly': '0', 'autosnap': 'yes', 'autoprune': 'yes'})
        names = [item['name'] for item in self.tasks]
        # The configuration precedes the package, whose timer may start on installation.
        self.assertLess(names.index('Schedule sandbox home snapshots with retention'),
                        names.index('Install sanoid from the configured signed Debian repositories'))
        install = task(self.tasks, 'Install sanoid from the configured signed Debian repositories')['ansible.builtin.apt']
        self.assertEqual((install['name'], install['install_recommends']), ('sanoid', False))

    def test_owner_play_requires_both_components_and_the_nas(self):
        play = read_yaml(ROOT / 'ansible/owner-sandbox-homes.yml')[0]
        guard = play['pre_tasks'][0]['ansible.builtin.assert']['that']
        for expected in ['microserver_owner_install | default(false) | bool',
                         'microserver_k8s_storage_enabled | default(false) | bool',
                         'microserver_sandbox_homes_enabled | default(false) | bool',
                         'ansible_play_hosts_all | length == 1']:
            self.assertIn(expected, guard)
        self.assertIs(play['force_handlers'], True)
        self.assertEqual([step['ansible.builtin.import_role']['tasks_from'] for step in play['tasks']],
                         ['sandbox-homes'])
        defaults = read_yaml(ROLE / 'defaults/main.yml')
        self.assertIs(defaults['microserver_sandbox_homes_enabled'], False)
        self.assertEqual(defaults['microserver_sandbox_homes'], [])

    def run_dataset_tasks(self, work, datasets, check):
        """Run the dataset tasks (up to, not including, host file changes) against the stub."""
        state = work / 'state.json'
        state.write_text(json.dumps({'datasets': datasets, 'calls': []}))
        tools = work / 'bin'
        tools.mkdir(exist_ok=True)
        for name in ('zfs', 'zpool'):
            (tools / name).write_text(STUB.format(python=sys.executable, state=str(state)))
            (tools / name).chmod(0o755)
        end = next(index for index, item in enumerate(self.tasks)
                   if item['name'] == 'Set ownership on each home root only')
        tasks = copy.deepcopy(self.tasks[:end])
        for item in tasks:
            if 'ansible.builtin.import_tasks' in item:
                item['ansible.builtin.import_tasks'] = str(ROLE / 'tasks' / item['ansible.builtin.import_tasks'])
            if item.get('ansible.builtin.stat', {}).get('path', '').startswith('/srv/'):
                item['ansible.builtin.stat']['path'] = str(work) + item['ansible.builtin.stat']['path']
        variables = dict(read_yaml(ROLE / 'defaults/main.yml'), **read_yaml(ROLE / 'vars/main.yml'),
                         microserver_pool_guid='1234567890123456789', microserver_k8s_nfs_client='192.168.30.11',
                         microserver_sandbox_homes=HOMES)
        playbook = work / 'sandbox-fixture.yml'
        playbook.write_text(yaml.safe_dump([{'hosts': 'localhost', 'connection': 'local', 'gather_facts': False,
                                             'vars': variables, 'tasks': tasks}], sort_keys=False))
        result = subprocess.run(
            ['ansible-playbook', '-i', 'localhost,', str(playbook)] + (['--check'] if check else []),
            cwd=ROOT, capture_output=True, text=True, timeout=180,
            env=dict(os.environ, PATH=str(tools) + os.pathsep + os.environ['PATH'],
                     ANSIBLE_CONFIG=str(ROOT / 'ansible.cfg'), ANSIBLE_NOCOLOR='1'))
        return result, json.loads(state.read_text())

    @unittest.skipUnless(shutil.which('ansible-playbook'), 'Ansible is required to run the dataset task flow')
    def test_dataset_flow_creates_missing_homes_adjusts_quotas_and_is_idempotent(self):
        k8s = {'nas/k8s': {'type': 'filesystem', 'mountpoint': '/srv/microserver/k8s', 'mounted': 'yes'}}
        with tempfile.TemporaryDirectory() as directory:
            work = Path(directory)
            # Check mode on a NAS without any sandbox dataset reads only.
            result, state = self.run_dataset_tasks(work, k8s, check=True)
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
            self.assertEqual(state['calls'], [])
            # The first apply creates the parent and both homes with their quotas.
            result, state = self.run_dataset_tasks(work, k8s, check=False)
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
            self.assertEqual([call[0] for call in state['calls']], ['create', 'create', 'create'])
            self.assertEqual(state['calls'][1][1:], ['-o', 'refquota=20G', '-o', 'quota=40G', 'nas/k8s/sandbox/alice'])
            self.assertEqual(state['calls'][2][1:], ['-o', 'refquota=50G', '-o', 'quota=100G', 'nas/k8s/sandbox/bob-2'])
            self.assertEqual(state['datasets']['nas/k8s/sandbox/alice']['mountpoint'], '/srv/microserver/sandbox/alice')
            # A rerun changes nothing; a changed quota is the only later write.
            datasets = state['datasets']
            for check in (True, False):
                result, state = self.run_dataset_tasks(work, datasets, check=check)
                self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
                self.assertEqual(state['calls'], [])
            datasets['nas/k8s/sandbox/alice'].update(refquota='10G', quota='20G')
            result, state = self.run_dataset_tasks(work, datasets, check=False)
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
            self.assertEqual(state['calls'], [['set', 'refquota=20G', 'quota=40G', 'nas/k8s/sandbox/alice']])
            # Without the Kubernetes dataset, or with an unmounted home, nothing is changed.
            result, state = self.run_dataset_tasks(work, {}, check=False)
            self.assertNotEqual(result.returncode, 0)
            self.assertIn('owner-k8s-storage.yml first', result.stdout)
            self.assertEqual(state['calls'], [])
            datasets['nas/k8s/sandbox/bob-2']['mounted'] = 'no'
            result, state = self.run_dataset_tasks(work, datasets, check=False)
            self.assertNotEqual(result.returncode, 0)
            self.assertIn('never fall back to the OS SSD', result.stdout)
            self.assertEqual([call for call in state['calls'] if 'bob-2' in call[-1]], [])


if __name__ == '__main__':
    unittest.main()
