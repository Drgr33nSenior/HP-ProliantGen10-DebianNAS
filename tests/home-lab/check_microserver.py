"""Exercise the actual installer guards and templates without contacting a host."""
import copy
import configparser
from decimal import Decimal
import importlib.util
import io
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import time
import tomllib
import unittest
import urllib.parse
from unittest import mock
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
ENV.filters['to_json'] = json.dumps
ENV.filters['urlsplit'] = lambda value, key: getattr(urllib.parse.urlsplit(value), key)
ENV.filters['from_yaml_all'] = lambda value: list(yaml.safe_load_all(value))
ENV.filters['dict2items'] = lambda value: [dict(key=key, value=item) for key, item in value.items()]


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

    def test_lightweight_install_keeps_owner_disk_and_account_choices(self):
        entries = {}
        for line in (ROOT / 'installer/media/preseed.cfg').read_text().splitlines():
            if not line.strip() or line.startswith('#'):
                continue
            owner, key, kind, *value = line.split(None, 3)
            self.assertIn(owner, ['d-i', 'tasksel', 'popularity-contest'])
            entries[key] = value[0] if value else ''
        self.assertFalse(any(key.startswith(('partman', 'grub-installer')) for key in entries))
        self.assertFalse(any('password' in key for key in entries))
        self.assertNotIn('preseed/early_command', entries)
        self.assertEqual(entries['tasksel/first'], '')
        self.assertEqual(entries['base-installer/install-recommends'], 'false')
        self.assertEqual(entries['hw-detect/load_firmware'], 'true')
        self.assertEqual(entries['apt-setup/non-free-firmware'], 'true')
        packages = set(entries['pkgsel/include'].split())
        self.assertTrue({'amd64-microcode', 'firmware-misc-nonfree', 'firmware-amd-graphics', 'openssh-server', 'sudo', 'python3-apt', 'apparmor', 'ethtool', 'pciutils', 'systemd-timesyncd'} <= packages)
        self.assertFalse({'k3s', 'kubelet', 'task-desktop', 'xorg', 'gnome', 'kde-plasma-desktop', 'docker.io'} & packages)
        for name in ['grub-entry.cfg', 'isolinux-entry.cfg']:
            boot = (ROOT / 'installer/media' / name).read_text()
            self.assertIn('preseed/file=/cdrom/preseed.cfg', boot)
            self.assertNotIn('/gtk/', boot)
        package_tasks = read_yaml(ROLE / 'tasks/packages.yml')
        apt = next(task['ansible.builtin.apt'] for task in package_tasks if 'ansible.builtin.apt' in task)
        self.assertFalse(apt['install_recommends'])
        self.assertEqual(apt['policy_rc_d'], 101)
        self.assertTrue({'docker.io', 'docker-cli', 'docker-compose'} <= set(apt['name']))
        self.assertTrue({'apparmor', 'zfs-zed', 'amd64-microcode', 'firmware-misc-nonfree', 'firmware-amd-graphics', 'ethtool', 'pciutils', 'lm-sensors', 'iperf3', 'nftables', 'systemd-timesyncd', 'irqbalance', 'linux-cpupower', 'powertop', 'tuned'} <= set(apt['name']))

    def test_iommu_workaround_is_in_installer_and_persistent_boot_options(self):
        for name, command in [('grub-entry.cfg', 'linux '), ('isolinux-entry.cfg', 'append ')]:
            lines = (ROOT / 'installer/media' / name).read_text().splitlines()
            args = next(line.strip().split()[1:] for line in lines if line.strip().startswith(command))
            self.assertEqual(args.count('iommu=pt'), 1)
            self.assertIn('iommu=pt', args[args.index('---') + 1:])
            self.assertFalse(any(arg.startswith('iommu=') and arg != 'iommu=pt' for arg in args))

    def test_host_tools_keep_benchmark_and_powertop_inactive(self):
        tasks = read_yaml(ROLE / 'tasks/packages.yml')
        selection = next(task['ansible.builtin.debconf'] for task in tasks if 'ansible.builtin.debconf' in task)
        self.assertEqual(selection, dict(name='iperf3', question='iperf3/start_daemon', value='false', vtype='boolean'))
        services = next(task for task in tasks if 'ansible.builtin.systemd_service' in task)
        self.assertEqual(set(services['loop']), {'iperf3', 'powertop'})
        self.assertFalse(services['ansible.builtin.systemd_service']['enabled'])
        self.assertEqual(services['ansible.builtin.systemd_service']['state'], 'stopped')
        host_tasks = read_yaml(ROLE / 'tasks/host.yml')
        ntp, irq = [task['ansible.builtin.systemd_service'] for task in host_tasks]
        self.assertEqual(ntp, dict(name='systemd-timesyncd', enabled=True, state='started'))
        self.assertTrue(self.context['microserver_irqbalance_enabled'])
        for enabled, state in [(True, 'started'), (False, 'stopped')]:
            context = dict(self.context, microserver_irqbalance_enabled=enabled)
            self.assertEqual(ENV.from_string(irq['enabled']).render(**context), enabled)
            self.assertEqual(ENV.from_string(irq['state']).render(**context), state)
        exporter = next(task for task in self.monitoring if task['name'] == 'Bind the Debian node exporter to loopback')
        args = exporter['ansible.builtin.copy']['content']
        for collector in ['hwmon', 'netdev', 'netclass']:
            self.assertIn(f'--collector.{collector}', args)
        self.assertIn('--web.listen-address=127.0.0.1:9100', args)

    def test_tuned_profile_scope_and_runtime_qualification(self):
        profile = configparser.ConfigParser(interpolation=None)
        profile.read(ROLE / 'files/tuned-performance.conf')
        self.assertEqual(set(profile.sections()), {'main', 'cpu'})
        self.assertEqual(dict(profile['cpu']), {'governor': 'performance'})
        self.assertNotIn('include', profile['main'])
        tasks = read_yaml(ROLE / 'tasks/tuning.yml')
        apt = tasks[0]['ansible.builtin.apt']
        self.assertEqual(set(apt['name']), {'tuned', 'linux-cpupower'})
        self.assertFalse(apt['install_recommends'])
        self.assertEqual(apt['policy_rc_d'], 101)
        tuning_import = next(task for task in read_yaml(ROLE / 'tasks/main.yml')
                             if task['ansible.builtin.import_tasks'] == 'tuning.yml')
        self.assertEqual(set(tuning_import['tags']), {'configure', 'tuning'})
        competing = next(task for task in tasks if task['name'].startswith('Give TuneD sole'))
        self.assertNotIn('irqbalance.service', competing['loop'])
        for name in ['cpupower.service', 'tlp.service', 'power-profiles-daemon.service', 'powertop.service']:
            self.assertIn(name, competing['loop'])
        self.assertFalse(competing['ansible.builtin.systemd_service']['enabled'])
        self.assertEqual(competing['ansible.builtin.systemd_service']['state'], 'stopped')
        self.assertFalse(ENV.compile_expression(competing['when'])(
            item='tlp.service', ansible_facts={'services': {}}))
        self.assertFalse(ENV.compile_expression(competing['when'])(
            item='tlp.service', ansible_facts={'services': {'tlp.service': {'status': 'not-found'}}}))
        self.assertTrue(ENV.compile_expression(competing['when'])(
            item='tlp.service', ansible_facts={'services': {'tlp.service': {'status': 'enabled'}}}))
        protected = next(task for task in tasks if task['name'].startswith('Require protected TuneD'))
        item = dict(item={'kind': 'directory'}, stat=dict(exists=True, isdir=True,
                                                       islnk=False, uid=0, mode='0755'))
        self.assertTrue(assertions(protected, {'item': item}))
        for change in [dict(islnk=True), dict(uid=1000), dict(mode='0777')]:
            bad = copy.deepcopy(item)
            bad['stat'].update(change)
            self.assertFalse(assertions(protected, {'item': bad}))
        no_driver = next(task for task in tasks if task['name'].startswith('Require a CPU frequency'))
        self.assertFalse(assertions(no_driver, {'microserver_cpufreq_policies': {'matched': 0}}))
        self.assertTrue(assertions(no_driver, {'microserver_cpufreq_policies': {'matched': 2}}))
        governor = next(task for task in tasks if task['name'].startswith('Require performance on every'))
        self.assertTrue(assertions(governor, {'item': {'stdout': 'performance\n'}}))
        self.assertFalse(assertions(governor, {'item': {'stdout': 'schedutil\n'}}))
        profile_check = next(task for task in tasks if task['name'].startswith('Require the selected profile'))
        active = {'stdout': 'Current active profile: microserver-performance\n'}
        self.assertTrue(assertions(profile_check, {'microserver_tuned_active': active}))
        self.assertFalse(assertions(profile_check, {'microserver_tuned_active': {'stdout': 'Current active profile: balanced'}}))
        verify = tasks[-1]
        self.assertEqual(verify['ansible.builtin.command']['argv'], ['/usr/sbin/tuned-adm', 'verify'])
        self.assertNotIn('failed_when', verify)
        self.assertEqual(verify['when'], 'not ansible_check_mode')

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

    @unittest.skipUnless(shutil.which('ansible-playbook'), 'Ansible is required to inspect task selection')
    def test_independent_commissioning_does_not_select_storage_tasks(self):
        command = ['ansible-playbook', '-i', 'ansible/microserver.inventory.example.ini',
                   '--list-tasks', 'ansible/microserver.yml', '--tags']
        environment = dict(os.environ, ANSIBLE_NOCOLOR='1')
        selected = subprocess.run(command + ['packages,host,tuning,monitoring'],
                                  cwd=ROOT, env=environment,
                                  capture_output=True, text=True, timeout=30)
        self.assertEqual(selected.returncode, 0, selected.stderr)
        for task in self.storage:
            self.assertNotIn(task['name'], selected.stdout)
        for name in ['packages', 'host', 'sensors', 'tuning', 'monitoring']:
            for task in read_yaml(ROLE / f'tasks/{name}.yml'):
                self.assertIn(task['name'], selected.stdout)
        configured = subprocess.run(command + ['configure'],
                                    cwd=ROOT, env=environment,
                                    capture_output=True, text=True, timeout=30)
        self.assertEqual(configured.returncode, 0, configured.stderr)
        for task in self.storage:
            self.assertIn(task['name'], configured.stdout)

    def test_owner_production_entry_requires_explicit_intent(self):
        task = read_yaml(ROOT / 'ansible/owner-install.yml')[0]['pre_tasks'][0]
        valid = dict(microserver_environment='prd', microserver_owner_install=True,
                     ansible_play_hosts_all=['nas'], ansible_facts=dict(
                         distribution='Debian', distribution_major_version='13', architecture='x86_64'))
        self.assertTrue(assertions(task, valid))
        for change in [dict(microserver_owner_install=False), dict(microserver_environment='unmapped'),
                       dict(microserver_environment='tst'), dict(ansible_play_hosts_all=['nas', 'other'])]:
            self.assertFalse(assertions(task, dict(valid, **change)))
        missing = dict(valid)
        del missing['microserver_owner_install']
        self.assertFalse(assertions(task, missing))
        for field, value in [('distribution', 'Ubuntu'), ('distribution_major_version', '12'),
                             ('architecture', 'aarch64')]:
            invalid = copy.deepcopy(valid)
            invalid['ansible_facts'][field] = value
            self.assertFalse(assertions(task, invalid))

    def test_dimm_limits_require_qualified_hardware_and_effective_readback(self):
        self.assertFalse(self.context['microserver_dimm_limits_enabled'])
        tasks = read_yaml(ROLE / 'tasks/sensors.yml')
        qualification = next(task for task in tasks if task['name'].startswith('Require the qualified Micron'))
        inventory = {'stdout_lines': ['\tManufacturer: Micron', '\tManufacturer: Micron',
                                     '\tPart Number: 9ASF1G72AZ-2G3B1    ',
                                     '\tPart Number: 18ASF2G72AZ-2G6D1   ']}
        self.assertTrue(assertions(qualification, {'microserver_dimm_inventory': inventory}))
        wrong = copy.deepcopy(inventory)
        wrong['stdout_lines'][-1] = '\tPart Number: UNQUALIFIED-DIMM'
        self.assertFalse(assertions(qualification, {'microserver_dimm_inventory': wrong}))
        adapter_check = next(task for task in tasks if task['name'].startswith('Require both DIMM sensors on'))
        readings = {'Adapter': 'SMBus PIIX4 adapter port 0 at 0b00', 'temp1': dict(
            temp1_input=30, temp1_max=75, temp1_crit=85, temp1_crit_hyst=82,
            temp1_max_alarm=0, temp1_crit_alarm=0)}
        hardware = {f'jc42-i2c-7-{address}': copy.deepcopy(readings) for address in ['18', '1a']}
        ctx = {'microserver_dimm_before': {'stdout': json.dumps(hardware)}}
        expr = adapter_check['vars']['microserver_jc42_before']
        sensors = ENV.from_string(expr).render(**ctx)
        self.assertTrue(assertions(adapter_check, {'microserver_jc42_before': sensors}))
        self.assertFalse(assertions(adapter_check, {'microserver_jc42_before': sensors[:1]}))
        wrong_adapter = copy.deepcopy(sensors)
        wrong_adapter[0]['value']['Adapter'] = 'Another adapter'
        self.assertFalse(assertions(adapter_check, {'microserver_jc42_before': wrong_adapter}))
        validation = tasks[-1]
        self.assertTrue(assertions(validation, {'item': sensors[0]}))
        for change in [dict(temp1_max=0), dict(temp1_crit=95), dict(temp1_crit_hyst=0),
                       dict(temp1_input=80, temp1_max_alarm=1), dict(temp1_crit_alarm=1)]:
            bad = copy.deepcopy(sensors[0])
            bad['value']['temp1'].update(change)
            self.assertFalse(assertions(validation, {'item': bad}))
        profile = (ROLE / 'files/microserver-jc42.conf').read_text()
        self.assertIn('bus "i2c-0" "SMBus PIIX4 adapter port 0 at 0b00"', profile)
        self.assertIn('chip "jc42-i2c-0-18" "jc42-i2c-0-1a"', profile)
        self.assertNotIn('ignore ', profile)
        parser = next(task['ansible.builtin.copy'] for task in tasks if 'ansible.builtin.copy' in task)
        self.assertEqual(parser['validate'], '/usr/bin/sensors -c %s -j')
        self.assertNotIn('-s', parser['validate'].split())

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
                    storage_read = argv[:2] in [['zpool', 'get'], ['zpool', 'list'], ['zfs', 'list'], ['zfs', 'get']]
                    hardware_read = argv in [['/usr/sbin/dmidecode', '--type', '17'],
                                             ['/usr/bin/sensors', '-c', '/dev/null', '-j']]
                    self.assertTrue(storage_read or hardware_read, argv)

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
        self.assertLessEqual(sum(Decimal(str(service['cpus'])) for service in config['services'].values()), Decimal('1.4'))
        self.assertLessEqual(sum(int(service['mem_limit'].removesuffix('m')) for service in config['services'].values()), 5632)
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
            for mount in service.get('tmpfs', []):
                self.assertTrue(mount.split(':', 1)[0].startswith('/'), mount)
        grafana_tmpfs = config['services']['grafana']['tmpfs']
        self.assertEqual(len(grafana_tmpfs), 1)
        target, options = grafana_tmpfs[0].split(':', 1)
        self.assertEqual(target, '/tmp')
        self.assertEqual(dict(option.split('=', 1) for option in options.split(',')),
                         dict(size='64m', mode='1777'))
        for name in ['prometheus', 'grafana', 'alertmanager', 'loki', 'vector']:
            self.assertEqual(config['services'][name]['image'], self.context[f'microserver_{name}_image'])
        self.assertIn('--web.listen-address=127.0.0.1:9090', config['services']['prometheus']['command'])
        self.assertIn('--query.max-concurrency=2', config['services']['prometheus']['command'])
        self.assertIn('--web.listen-address=127.0.0.1:9093', config['services']['alertmanager']['command'])
        self.assertEqual(config['services']['grafana']['environment']['GF_SERVER_HTTP_ADDR'], '127.0.0.1')
        grafana = config['services']['grafana']['environment']
        self.assertEqual(grafana['GF_PLUGINS_PREINSTALL_DISABLED'], 'true')
        self.assertEqual(grafana['GF_PLUGINS_PREINSTALL_AUTO_UPDATE'], 'false')
        self.assertNotIn('GF_PLUGINS_ALLOW_LOADING_UNSIGNED_PLUGINS', grafana)
        if len(sys.argv) > 1:
            output = Path(sys.argv[1])
            output.mkdir(parents=True, exist_ok=True)
            (output / 'compose.yaml').write_text(str(compose))
            (output / 'prometheus.yaml').write_text(str(ENV.from_string((ROLE / 'templates/prometheus.yaml.j2').read_text()).render(**self.context)))

    def test_syslog_sender_and_bind_guards(self):
        endpoint = next(task for task in self.monitoring if task['name'].startswith('Require exact private IPv4 syslog'))
        binding = next(task for task in self.monitoring if task['name'].startswith('Require the syslog bind'))
        for address in ['192.168.50.1', '10.0.0.1', '172.20.0.1']:
            self.assertTrue(assertions(endpoint, dict(item=address)))
        for address in ['', '0.0.0.0', '*', '192.168.50.0/24', '203.0.113.1',
                        '127.0.0.1', '192.168.050.1', '192.168.50.1\n', None]:
            self.assertFalse(assertions(endpoint, dict(item=address)))
        context = dict(microserver_unifi_gateway_ip='192.168.50.1',
                       microserver_unifi_syslog_listen_ip='192.168.50.10',
                       ansible_facts=dict(all_ipv4_addresses=['192.168.50.10']))
        self.assertTrue(assertions(binding, context))
        self.assertFalse(assertions(binding, dict(context, microserver_unifi_syslog_listen_ip='192.168.50.11')))
        self.assertFalse(assertions(binding, dict(context, microserver_unifi_gateway_ip='192.168.50.10')))
        vector = yaml.safe_load(ENV.from_string((ROLE / 'templates/vector.yaml.j2').read_text()).render(**self.context))
        self.assertNotIn('unifi', vector['sources'])
        self.assertEqual(vector['sinks']['metrics']['address'], '127.0.0.1:8687')
        enabled = dict(self.context, **context, microserver_unifi_syslog_enabled=True)
        vector = yaml.safe_load(ENV.from_string((ROLE / 'templates/vector.yaml.j2').read_text()).render(**enabled))
        self.assertEqual(vector['sources']['unifi']['address'], '192.168.50.10:1514')
        self.assertEqual(vector['sources']['unifi']['mode'], 'udp')
        self.assertEqual(set(vector['sinks']['loki']['labels']), {'job', 'product', 'severity'})
        self.assertEqual(vector['sinks']['loki']['buffer']['max_size'], 536870912)
        for name in ['metrics', 'logs']:
            datasource = read_yaml(ROOT / f'observability/config/datasources.{name}.yaml')['datasources'][0]
            self.assertIn(datasource['uid'], ['prometheus', 'loki'])

    def test_network_folder_keeps_provider_and_uids_without_legacy_duplicates(self):
        provider = read_yaml(ROOT / 'observability/config/dashboards.yaml')['providers'][0]
        self.assertEqual(provider['name'], 'workstation')
        self.assertNotIn('folder', provider)
        self.assertNotIn('folderUid', provider)
        self.assertTrue(provider['disableDeletion'])
        self.assertTrue(provider['options']['foldersFromFilesStructure'])
        self.assertGreater(provider['updateIntervalSeconds'], 10)
        compose = yaml.safe_load(ENV.from_string((ROLE / 'templates/compose.yaml.j2').read_text()).render(**self.context))
        mounts = compose['services']['grafana']['volumes']
        self.assertIn('/etc/microserver/monitoring/dashboard-folders:/var/lib/grafana/dashboards:ro', mounts)
        self.assertFalse(any(value.startswith('/etc/microserver/monitoring/dashboards:') for value in mounts))
        destinations = {task['ansible.builtin.copy'].get('src'): task['ansible.builtin.copy']['dest']
                        for task in self.monitoring if 'ansible.builtin.copy' in task and 'src' in task['ansible.builtin.copy']}
        self.assertIn('/Infrastructure/', destinations['microserver-dashboard.json'])
        self.assertIn('/Network/', destinations['unifi-logs-dashboard.json'])
        self.assertIn('/Network/', destinations['network-dashboard.json'])
        self.assertEqual(json.loads((ROLE / 'files/unifi-logs-dashboard.json').read_text())['uid'], 'unifi-logs')

    def test_unifi_poller_is_opt_in_with_private_files_verified_tls_and_bounded_metrics(self):
        template = ENV.from_string((ROLE / 'templates/unpoller.conf.j2').read_text())
        endpoint = next(task for task in self.monitoring if task['name'].startswith('Require explicit HTTPS controller'))
        context = dict(self.context, microserver_unifi_metrics_enabled=True,
                       microserver_unifi_controller_url='https://gateway.home.arpa',
                       microserver_unifi_username='monitoring')
        self.assertTrue(assertions(endpoint, context))
        for url in ['', 'http://gateway.home.arpa', 'https://user:password@gateway.home.arpa',
                    'https://gateway.home.arpa/path', 'https://gateway.home.arpa\n']:
            self.assertFalse(assertions(endpoint, dict(context, microserver_unifi_controller_url=url)))
        self.assertFalse(assertions(endpoint, dict(context, microserver_unifi_auth_method='unsupported')))
        self.assertFalse(assertions(endpoint, dict(context, microserver_unifi_auth_method='password', microserver_unifi_username='')))
        for method in ['api_key', 'password']:
            context['microserver_unifi_auth_method'] = method
            config = tomllib.loads(str(template.render(**context)))
            self.assertTrue(config['unifi']['defaults']['verify_ssl'])
            field = 'api_key' if method == 'api_key' else 'pass'
            self.assertEqual(config['unifi']['defaults'][field], 'file:///run/secrets/unifi-credential')
            self.assertEqual(config['prometheus']['http_listen'], '127.0.0.1:9130')
            self.assertEqual(config['prometheus']['interval'], '60s')
            self.assertTrue(config['unifi']['defaults']['skip_device_tags'])
            self.assertFalse(config['unifi']['defaults']['save_dpi'])
            self.assertTrue(config['influxdb']['disable'])
            self.assertTrue(config['loki']['disable'])
        compose = yaml.safe_load(ENV.from_string((ROLE / 'templates/compose.yaml.j2').read_text()).render(**context))
        self.assertEqual(compose['services']['unpoller']['image'], self.context['microserver_unpoller_image'])
        self.assertLessEqual(sum(Decimal(str(service['cpus'])) for service in compose['services'].values()), Decimal('1.5'))
        self.assertLessEqual(sum(int(service['mem_limit'].removesuffix('m')) for service in compose['services'].values()), 5888)
        self.assertEqual(compose['services']['unpoller']['mem_limit'], '256m')
        self.assertTrue(compose['services']['unpoller']['read_only'])
        self.assertEqual(compose['services']['unpoller']['cap_drop'], ['ALL'])
        self.assertEqual(compose['services']['unpoller']['security_opt'], ['no-new-privileges:true'])
        self.assertNotIn('unifi-credential', str(compose['services']['unpoller']['environment']))
        custom = tomllib.loads(str(template.render(**dict(context, microserver_unifi_custom_ca=True))))
        self.assertNotIn('ssl_cert_paths', custom['unifi']['defaults'])
        custom_compose = yaml.safe_load(ENV.from_string((ROLE / 'templates/compose.yaml.j2').read_text()).render(
            **dict(context, microserver_unifi_custom_ca=True)))
        self.assertEqual(custom_compose['services']['unpoller']['environment']['SSL_CERT_FILE'], '/run/secrets/unifi-ca.pem')
        self.assertIn('/etc/microserver/private/unifi-ca.pem:/run/secrets/unifi-ca.pem:ro', custom_compose['services']['unpoller']['volumes'])
        alias_context = dict(context, microserver_unifi_controller_ip='192.168.50.1')
        alias = next(task for task in self.monitoring if task['name'].startswith('Validate an optional private controller'))
        self.assertTrue(assertions(alias, alias_context))
        for address in ['0.0.0.0', '203.0.113.1', '192.168.50.1\n', '192.168.50.256', '192.168.050.1']:
            self.assertFalse(assertions(alias, dict(context, microserver_unifi_controller_ip=address)))
        alias_compose = yaml.safe_load(ENV.from_string((ROLE / 'templates/compose.yaml.j2').read_text()).render(**alias_context))
        self.assertEqual(alias_compose['services']['unpoller']['extra_hosts'], ['gateway.home.arpa:192.168.50.1'])
        for name in ['Inspect only owner-prepared UniFi credential metadata', 'Inspect only optional UniFi CA metadata']:
            task = next(task for task in self.monitoring if task['name'] == name)
            self.assertFalse(task['ansible.builtin.stat']['get_checksum'])
            self.assertFalse(task['ansible.builtin.stat']['follow'])
        scrape = yaml.safe_load(ENV.from_string((ROLE / 'templates/prometheus.yaml.j2').read_text()).render(**context))
        job = next(job for job in scrape['scrape_configs'] if job['job_name'] == 'microserver-unifi')
        policy = job['metric_relabel_configs'][0]
        self.assertEqual(policy['action'], 'keep')
        self.assertIsNotNone(re.fullmatch(policy['regex'], 'unifipoller_device_wan_receive_bytes_total'))
        self.assertIsNotNone(re.fullmatch(policy['regex'], 'unifipoller_client_receive_bytes_total'))
        self.assertIsNotNone(re.fullmatch(policy['regex'], 'unifipoller_device_info'))
        self.assertIsNone(re.fullmatch(policy['regex'], 'unifipoller_dpi_application_present'))
        self.assertIsNone(re.fullmatch(policy['regex'], 'unifipoller_firewall_rule_enabled'))
        default = yaml.safe_load(ENV.from_string((ROLE / 'templates/compose.yaml.j2').read_text()).render(**self.context))
        self.assertNotIn('unpoller', default['services'])
        stop = next(task for task in self.monitoring if task['name'].startswith('Stop the optional collector'))
        self.assertIn('not microserver_unifi_metrics_enabled', stop['when'])
        if len(sys.argv) > 1:
            output = Path(sys.argv[1])
            (output / 'compose-unifi.yaml').write_text(str(ENV.from_string((ROLE / 'templates/compose.yaml.j2').read_text()).render(**context)))
            (output / 'prometheus-unifi.yaml').write_text(str(ENV.from_string((ROLE / 'templates/prometheus.yaml.j2').read_text()).render(**context)))

    def test_installer_payload_contains_current_runtime_sources(self):
        payload = set(line for line in (ROOT / 'installer/media/payload.files').read_text().splitlines()
                      if line and not line.startswith('#'))
        for path in ROLE.rglob('*'):
            if path.is_file() and '__pycache__' not in path.parts:
                self.assertIn(str(path.relative_to(ROOT)), payload)
        for name in ['datasources.metrics.yaml', 'datasources.logs.yaml', 'dashboards.yaml']:
            self.assertIn('observability/config/' + name, payload)

    def test_network_queries_have_data_units_and_no_fabricated_health(self):
        dashboard = json.loads((ROLE / 'files/network-dashboard.json').read_text())
        expected = {(1, 'A'): 1, (2, 'A'): 45, (3, 'A'): 1, (4, 'A'): 86400,
                    (5, 'A'): 8000, (5, 'B'): 1600, (6, 'A'): .012,
                    (7, 'A'): 25, (8, 'A'): 40, (9, 'A'): 45,
                    (10, 'A'): 1_000_000_000, (11, 'A'): 8, (11, 'B'): 16,
                    (12, 'A'): 0, (12, 'B'): .1}
        queries = []
        covered = set()
        for panel in dashboard['panels']:
            self.assertEqual(panel['datasource']['uid'], 'prometheus')
            for target in panel['targets']:
                key = (panel['id'], target['refId'])
                covered.add(key)
                self.assertNotIn('vector(0)', target['expr'])
                self.assertEqual(target['instant'], panel['type'] == 'stat')
                queries.append(dict(expr=f"sum({target['expr']})", eval_time='6m',
                                    exp_samples=[dict(labels='{}', value=expected[key])]))
        self.assertEqual(covered, set(expected))
        for number, unit in [(5, 'bps'), (6, 's'), (7, 'percent'), (8, 'percent'), (9, 'celsius'), (10, 'bps')]:
            self.assertEqual(next(panel for panel in dashboard['panels'] if panel['id'] == number)['fieldConfig']['defaults']['unit'], unit)
        if len(sys.argv) > 1:
            test = dict(rule_files=[], evaluation_interval='1m', tests=[
                dict(interval='1m', input_series=read_yaml(ROOT / 'tests/fixtures/microserver/network_series.yaml'),
                     promql_expr_test=queries)])
            (Path(sys.argv[1]) / 'network_test.yaml').write_text(yaml.safe_dump(test))

    def test_dashboard_queries_have_data_and_correct_units(self):
        dashboard = json.loads((ROLE / 'files/microserver-dashboard.json').read_text())
        datasource = read_yaml(ROOT / 'observability/config/datasources.metrics.yaml')['datasources'][0]
        expected = {(1, 'A'): 25, (2, 'A'): 75, (3, 'A'): 80,
                    (4, 'A'): 10, (4, 'B'): 20, (5, 'A'): 82,
                    (6, 'A'): 61, (6, 'B'): 150, (6, 'C'): 170,
                    (7, 'A'): 20, (8, 'A'): 50, (9, 'A'): 1,
                    (10, 'A'): 1, (11, 'A'): 50, (12, 'A'): 1, (12, 'B'): 2}
        queries = []
        covered = set()
        for panel in dashboard['panels']:
            self.assertEqual(panel['datasource']['uid'], datasource['uid'])
            for target in panel['targets']:
                key = (panel['id'], target['refId'])
                covered.add(key)
                self.assertEqual(target['datasource']['uid'], datasource['uid'])
                self.assertEqual(target['range'], panel['type'] == 'timeseries')
                self.assertEqual(target['instant'], panel['type'] == 'stat')
                count = 2 if panel['id'] in [5, 6] else 1
                for aggregation, value in [('sum', expected[key]), ('count', count)]:
                    queries.append(dict(expr=f"{aggregation}({target['expr']})", eval_time='6m',
                                        exp_samples=[dict(labels='{}', value=value)]))
        self.assertEqual(covered, set(expected))
        if len(sys.argv) > 1:
            series = read_yaml(ROOT / 'tests/fixtures/microserver/dashboard_series.yaml')
            test = dict(rule_files=[], evaluation_interval='1m', tests=[
                dict(interval='1m', input_series=series, promql_expr_test=queries)])
            (Path(sys.argv[1]) / 'dashboard_test.yaml').write_text(yaml.safe_dump(test))

    def test_docker_collection_activation_and_plugin_readiness_order(self):
        services = [task for task in self.monitoring if 'ansible.builtin.systemd_service' in task]
        for task in services:
            self.assertFalse(ENV.compile_expression(task['when'])(microserver_monitoring_enabled=False))
            self.assertTrue(ENV.compile_expression(task['when'])(microserver_monitoring_enabled=True))
        readiness = [task for task in self.monitoring if 'ansible.builtin.uri' in task]
        for task in readiness:
            self.assertGreater(self.monitoring.index(task), max(map(self.monitoring.index, services)))
        protected = next(task for task in self.monitoring if task['name'].startswith('Require a protected Docker'))
        metadata = dict(exists=True, isreg=True, islnk=False, uid=0, mode='0644')
        self.assertTrue(assertions(protected, dict(microserver_docker_program=dict(stat=metadata))))
        for change in [dict(islnk=True), dict(uid=1000), dict(mode='0666')]:
            self.assertFalse(assertions(protected, dict(microserver_docker_program=dict(stat=dict(metadata, **change)))))

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


class DockerMetricsTests(unittest.TestCase):
    def setUp(self):
        spec = importlib.util.spec_from_file_location('docker_metrics', ROLE / 'files/microserver-docker-metrics.py')
        self.collector = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(self.collector)
        self.running = dict(Id='a' * 64, Names=['/running'], State='running')
        self.stopped = dict(Id='b' * 64, Names=['/stopped'], State='exited')
        self.stats = dict(cpu_stats=dict(cpu_usage=dict(total_usage=2_000_000_000)),
                          memory_stats=dict(usage=100, limit=512, stats=dict(inactive_file=40)),
                          pids_stats=dict(current=7))

    def test_running_and_stopped_containers_and_working_set(self):
        with mock.patch.object(self.collector, 'docker_json', side_effect=[
                {'ApiVersion': '1.45'}, [self.running, self.stopped], self.stats]) as request:
            lines = self.collector.collect()
        content = self.collector.render(lines, True, 1234)
        self.assertIn('container_running{container="stopped"} 0\n', content)
        self.assertNotIn('container_cpu_usage_seconds_total{container="stopped"}', content)
        self.assertIn('container_cpu_usage_seconds_total{container="running"} 2.0\n', content)
        self.assertIn('container_memory_working_set_bytes{container="running"} 60\n', content)
        self.assertIn('container_memory_limit_bytes{container="running"} 512\n', content)
        self.assertIn('container_pids{container="running"} 7\n', content)
        self.assertEqual([call.args[0] for call in request.call_args_list], [
            '/version', '/v1.45/containers/json?all=true',
            '/v1.45/containers/' + 'a' * 64 + '/stats?stream=false&one-shot=true'])
        if len(sys.argv) > 1:
            (Path(sys.argv[1]) / 'docker_metrics.prom').write_text(content)

    def test_cgroup_v1_cache_and_counter_reset(self):
        self.stats['memory_stats']['stats'] = dict(total_inactive_file=200)
        self.stats['cpu_stats']['cpu_usage']['total_usage'] = 100_000_000
        with mock.patch.object(self.collector, 'docker_json', side_effect=[
                {'ApiVersion': '1.45'}, [self.running], self.stats]):
            content = '\n'.join(self.collector.collect())
        self.assertIn('container_memory_working_set_bytes{container="running"} 0', content)
        self.assertIn('container_cpu_usage_seconds_total{container="running"} 0.1', content)

    def test_invalid_identity_and_container_limit_stop_before_stats(self):
        for containers in [[dict(self.running, Id='../inspect')],
                           [dict(self.running, Names=['/bad"label'])],
                           [self.running] * (self.collector.MAX_CONTAINERS + 1)]:
            with self.subTest(containers=len(containers)):
                with mock.patch.object(self.collector, 'docker_json', side_effect=[
                        {'ApiVersion': '1.45'}, containers]) as request:
                    with self.assertRaises(self.collector.CollectionError):
                        self.collector.collect()
                self.assertEqual(request.call_count, 2)

    def test_empty_daemon_is_successful_and_does_not_invent_containers(self):
        with mock.patch.object(self.collector, 'docker_json', side_effect=[
                {'ApiVersion': '1.45'}, []]):
            self.assertEqual(self.collector.collect(), ['microserver_docker_containers 0'])

    def test_stdout_failure_redacts_error_and_writes_no_file(self):
        output, error = io.StringIO(), io.StringIO()
        with mock.patch.object(self.collector, 'collect', side_effect=ValueError('private diagnostic detail')):
            with mock.patch.object(self.collector, 'write_metrics') as writer:
                with mock.patch.object(sys, 'argv', ['collector', '--stdout']), mock.patch.object(sys, 'stdout', output), mock.patch.object(sys, 'stderr', error):
                    self.assertEqual(self.collector.main(), 1)
                writer.assert_not_called()
        self.assertIn('microserver_docker_collection_success 0\n', output.getvalue())
        self.assertNotIn('container_running{', output.getvalue())
        self.assertNotIn('private diagnostic detail', error.getvalue())

    def test_atomic_write_preserves_previous_snapshot_on_failure(self):
        with tempfile.TemporaryDirectory() as directory:
            destination = Path(directory) / 'docker.prom'
            with mock.patch.object(self.collector, 'OUTPUT', destination):
                self.collector.write_metrics('previous\n')
                self.assertEqual(destination.stat().st_mode & 0o777, 0o644)
                with mock.patch.object(self.collector.os, 'replace', side_effect=OSError('replace failed')):
                    with self.assertRaises(OSError):
                        self.collector.write_metrics('next\n')
                self.assertEqual(destination.read_text(), 'previous\n')
                self.assertEqual(list(Path(directory).iterdir()), [destination])
                self.collector.write_metrics('next\n')
                self.assertEqual(destination.read_text(), 'next\n')

    def test_http_is_get_only_bounded_and_closes_on_error(self):
        for status, body in [(200, b'{"ApiVersion":"1.45"}'),
                             (500, b'private response body'),
                             (200, b'x' * (self.collector.MAX_RESPONSE + 1))]:
            connection = mock.Mock()
            connection.getresponse.return_value.status = status
            connection.getresponse.return_value.read.return_value = body
            with mock.patch.object(self.collector, 'DockerConnection', return_value=connection):
                if status == 200 and len(body) < self.collector.MAX_RESPONSE:
                    self.assertEqual(self.collector.docker_json('/version', time.monotonic() + 5), {'ApiVersion': '1.45'})
                else:
                    with self.assertRaises(self.collector.CollectionError):
                        self.collector.docker_json('/version', time.monotonic() + 5)
            connection.request.assert_called_once_with('GET', '/version')
            if status == 200:
                connection.getresponse.return_value.read.assert_called_once_with(self.collector.MAX_RESPONSE + 1)
            connection.close.assert_called_once()
        with self.assertRaises(self.collector.CollectionError):
            self.collector.docker_json('/version', time.monotonic() - 1)


if __name__ == '__main__':
    unittest.main(argv=[sys.argv[0]])
