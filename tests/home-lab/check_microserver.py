"""Exercise the actual installer guards and templates without contacting a host."""
import copy
import json
import re
import sys
import unittest
from pathlib import Path

import jinja2
from jinja2.nativetypes import NativeEnvironment
import yaml

ROOT = Path(__file__).resolve().parents[2]
ROLE = ROOT / 'ansible/roles/debian_microserver'
ENV = NativeEnvironment(undefined=jinja2.StrictUndefined)
ENV.tests['match'] = lambda value, pattern: re.match(pattern, str(value)) is not None
ENV.filters['type_debug'] = lambda value: type(value).__name__
ENV.filters['bool'] = lambda value: value is True or str(value).lower() in ('true', 'yes', '1')
ENV.filters['from_json'] = json.loads
ENV.filters['from_yaml_all'] = lambda value: list(yaml.safe_load_all(value))


def read_yaml(path):
    return yaml.safe_load(path.read_text())


def lookup(plugin, path):
    assert plugin == 'ansible.builtin.file'
    resolved = Path(path).resolve()
    assert ROOT in resolved.parents
    return resolved.read_text()


ENV.globals['lookup'] = lookup


def assertions(task, context):
    return all(ENV.compile_expression(expr)(**context)
               for expr in task['ansible.builtin.assert']['that'])


class MicroServerTests(unittest.TestCase):
    def setUp(self):
        self.storage = read_yaml(ROLE / 'tasks/storage.yml')
        self.monitoring = read_yaml(ROLE / 'tasks/monitoring.yml')
        self.context = read_yaml(ROLE / 'defaults/main.yml')
        self.context.update(
            playbook_dir=str(ROOT / 'ansible'),
            microserver_pool_guid='1234567890123456789',
            microserver_nfs_clients=['192.168.50.20'],
            microserver_share_uid=1000, microserver_share_gid=1000,
        )
        for key, value in read_yaml(ROLE / 'vars/main.yml').items():
            self.context[key] = ENV.from_string(value).render(**self.context)

    def test_target_boundary(self):
        task = read_yaml(ROOT / 'ansible/microserver.yml')[0]['pre_tasks'][0]
        valid = dict(microserver_environment='tst', ansible_play_hosts_all=['nas'],
                     ansible_facts=dict(distribution='Debian', distribution_major_version='13',
                                        architecture='x86_64'))
        self.assertTrue(assertions(task, valid))
        for field, value in [('distribution', 'Ubuntu'), ('distribution_major_version', '12'),
                             ('architecture', 'aarch64')]:
            invalid = copy.deepcopy(valid)
            invalid['ansible_facts'][field] = value
            self.assertFalse(assertions(task, invalid))
        for change in [dict(microserver_environment='prd'), dict(microserver_environment='unmapped'),
                       dict(ansible_play_hosts_all=['nas', 'workstation'])]:
            self.assertFalse(assertions(task, dict(valid, **change)))

    def test_required_identity_and_clients(self):
        self.assertTrue(assertions(self.storage[0], self.context))
        for change in [dict(microserver_pool_guid=''), dict(microserver_pool_guid='0'),
                       dict(microserver_pool_name='nas; reboot'), dict(microserver_share_uid=0),
                       dict(microserver_share_gid=0), dict(microserver_nfs_clients=[]),
                       dict(microserver_nfs_clients='192.168.50.20')]:
            self.assertFalse(assertions(self.storage[0], dict(self.context, **change)))
        for client in ['10.0.0.1', '172.16.0.2', '172.31.255.254', '192.168.50.20']:
            self.assertTrue(assertions(self.storage[1], dict(item=client)))
        for client in ['*', '192.168.50.0/24', '203.0.113.10', '172.32.0.1',
                       '192.168.256.1', '192.168.050.20', '192.168.50.20(no_root_squash)',
                       '192.168.50.20\n10.0.0.2', None, 123]:
            self.assertFalse(assertions(self.storage[1], dict(item=client)))

    def test_guid_and_mounted_dataset_are_required(self):
        guid_task = next(task for task in self.storage if task['name'] == 'Require the exact owner-selected pool')
        self.assertTrue(assertions(guid_task, dict(self.context, microserver_actual_pool_guid={'stdout': self.context['microserver_pool_guid']})))
        self.assertFalse(assertions(guid_task, dict(self.context, microserver_actual_pool_guid={'stdout': '987654321'})))
        mounted_task = next(task for task in self.storage if task['name'].startswith('Require the mounted'))
        for properties, accepted in [(['filesystem', '/srv/microserver/shared', 'yes'], True),
                                     (['filesystem', '/srv/microserver/shared', 'no'], False),
                                     (['filesystem', '/srv/other', 'yes'], False)]:
            self.assertEqual(assertions(mounted_task, {'microserver_dataset_properties': {'stdout_lines': properties}}), accepted)
        # No pool creation/import/destruction, partitioning or shell execution.
        for source in (ROLE / 'tasks').glob('*.yml'):
            for task in read_yaml(source):
                self.assertNotIn('ansible.builtin.shell', task)
                argv = task.get('ansible.builtin.command', {}).get('argv', [])
                if argv[:1] == ['zpool']:
                    self.assertIn(argv[1], ['get', 'list'])
                if argv[:2] == ['zfs', 'create']:
                    self.assertEqual(task['when'], 'microserver_dataset.rc == 1')
                    self.assertNotIn('-f', argv)
                if task.get('check_mode') is False:
                    self.assertIn(argv[:2], [['zpool', 'get'], ['zpool', 'list'], ['zfs', 'list'], ['zfs', 'get']])

    def test_path_guards(self):
        task = next(task for task in self.storage if task['name'].startswith('Refuse untrusted'))
        item = dict(item='/srv/microserver/shared', stat=dict(exists=True, isdir=True, islnk=False, mode='0750', uid=1000))
        ctx = dict(self.context, item=item, microserver_dataset=dict(rc=0))
        self.assertTrue(assertions(task, ctx))
        self.assertFalse(assertions(task, dict(ctx, microserver_dataset=dict(rc=1))))
        for change in [dict(islnk=True), dict(mode='0777'), dict(uid=1234), dict(isdir=False)]:
            bad = copy.deepcopy(item)
            bad['stat'].update(change)
            self.assertFalse(assertions(task, dict(ctx, item=bad)))
        self.assertTrue(assertions(task, dict(ctx, item=dict(item=item['item'], stat=dict(exists=False)), microserver_dataset=dict(rc=1))))

    def test_rendered_exports(self):
        text = str(ENV.from_string((ROLE / 'templates/exports.j2').read_text()).render(**self.context))
        self.assertIn('192.168.50.20(rw,sync,root_squash,no_subtree_check,mountpoint)', text)
        self.assertNotIn('no_root_squash', text)
        self.assertNotIn('*(', text)

    def test_compose_security_and_canonical_images(self):
        compose = ENV.from_string((ROLE / 'templates/compose.yaml.j2').read_text()).render(**self.context)
        config = yaml.safe_load(compose)
        for service in config['services'].values():
            self.assertRegex(service['image'], r'@sha256:[a-f0-9]{64}$')
            self.assertTrue(service['read_only'])
            self.assertEqual(service['cap_drop'], ['ALL'])
            self.assertEqual(service['security_opt'], ['no-new-privileges:true'])
            self.assertEqual(service['network_mode'], 'host')
            self.assertNotIn('ports', service)
            self.assertNotIn('privileged', service)
            self.assertNotEqual(service['user'].split(':')[0], '0')
            self.assertEqual(service['mem_limit'], service['memswap_limit'])
            self.assertGreater(service['pids_limit'], 0)
            self.assertLessEqual(service['cpus'], 1)
            self.assertEqual(service['logging']['options']['max-size'], '10m')
            self.assertFalse(any('docker.sock' in value for value in service['volumes']))
        for name in ['prometheus', 'grafana', 'alertmanager']:
            self.assertEqual(config['services'][name]['image'], self.context[f'microserver_{name}_image'])
        self.assertIn('--web.listen-address=127.0.0.1:9090', config['services']['prometheus']['command'])
        self.assertIn('--web.listen-address=127.0.0.1:9093', config['services']['alertmanager']['command'])
        self.assertEqual(config['services']['grafana']['environment']['GF_SERVER_HTTP_ADDR'], '127.0.0.1')
        if len(sys.argv) > 1:
            output = Path(sys.argv[1])
            output.mkdir(parents=True, exist_ok=True)
            (output / 'compose.yaml').write_text(str(compose))
            (output / 'prometheus.yaml').write_text(str(ENV.from_string((ROLE / 'templates/prometheus.yaml.j2').read_text()).render(**self.context)))

    def test_secret_metadata_and_defaults(self):
        self.assertFalse(read_yaml(ROLE / 'defaults/main.yml')['microserver_monitoring_enabled'])
        task = next(task for task in self.monitoring if task['name'].startswith('Require a private owner-prepared'))
        stat = dict(exists=True, isreg=True, islnk=False, uid=0, gid=472, mode='0440', size=32)
        self.assertTrue(assertions(task, dict(microserver_grafana_credential=dict(stat=stat))))
        for change in [dict(exists=False), dict(islnk=True), dict(uid=1000), dict(mode='0644'), dict(size=0), dict(gid=0)]:
            self.assertFalse(assertions(task, dict(microserver_grafana_credential=dict(stat=dict(stat, **change)))))
        inspect_task = next(task for task in self.monitoring if task['name'].startswith('Inspect only Grafana'))
        self.assertEqual(inspect_task['ansible.builtin.stat']['get_checksum'], False)
        self.assertEqual(inspect_task['ansible.builtin.stat']['follow'], False)
        self.assertEqual(inspect_task['when'], 'microserver_monitoring_enabled | bool')


if __name__ == '__main__':
    unittest.main(argv=[sys.argv[0]])
