"""Offline NAS ingestion scope, drift and optional backend tests."""
import copy
import importlib.util
import re
import unittest

import yaml
import check_microserver as microserver
from check_microserver import ENV, ROLE, ROOT, assertions, read_yaml

spec = importlib.util.spec_from_file_location('telemetry_guard', ROLE / 'files/microserver-telemetry-guard.py')
guard = importlib.util.module_from_spec(spec)
spec.loader.exec_module(guard)
spec = importlib.util.spec_from_file_location('ui_validate', ROLE / 'files/microserver-ui-validate.py')
validate = importlib.util.module_from_spec(spec)
spec.loader.exec_module(validate)


class TelemetryReceiverTests(unittest.TestCase):
    def setUp(self):
        base = microserver.MicroServerTests()
        base.setUp()
        self.context = dict(base.context, microserver_monitoring_enabled=True, microserver_ui_enabled=True,
                            microserver_ui_address='192.168.30.10', microserver_ui_domain='lan.example.test',
                            microserver_ui_allowed_networks=['192.168.2.0/24', '192.168.30.0/24'],
                            microserver_telemetry_enabled=True, microserver_telemetry_client_address='192.168.30.11')
        self.tasks = read_yaml(ROLE / 'tasks/telemetry.yml')

    def render(self, name, **updates):
        return str(ENV.from_string((ROLE / 'templates' / name).read_text()).render(**dict(self.context, **updates)))

    def test_disabled_feature_opens_no_listener_and_adds_no_service(self):
        config = yaml.safe_load(self.render('compose.yaml.j2', microserver_telemetry_enabled=False))
        self.assertNotIn('tempo', config['services'])
        self.assertNotIn(':8080', self.render('ui-nginx.conf.j2', microserver_telemetry_enabled=False))

    def test_only_tempo_is_added_to_compose_and_its_resources_are_bounded(self):
        before = yaml.safe_load(self.render('compose.yaml.j2', microserver_telemetry_enabled=False))
        after = yaml.safe_load(self.render('compose.yaml.j2'))
        guard.require_receiver_services_only(before, after)
        guard.require_receiver_services_only(after, after)
        tempo = after['services']['tempo']
        self.assertRegex(tempo['image'], r'^docker.io/grafana/tempo:3[.]0[.]3@sha256:[a-f0-9]{64}$')
        self.assertEqual(tempo['user'], '10001:10001')
        self.assertTrue(tempo['read_only'])
        self.assertEqual(tempo['cap_drop'], ['ALL'])
        self.assertEqual(tempo['security_opt'], ['no-new-privileges:true'])
        self.assertEqual(tempo['mem_limit'], '768m')
        self.assertEqual(tempo['memswap_limit'], '768m')
        self.assertEqual(tempo['cpus'], 0.25)
        self.assertEqual(len(tempo['volumes']), 2)

    def test_existing_service_changes_and_tempo_drift_are_refused(self):
        before = yaml.safe_load(self.render('compose.yaml.j2', microserver_telemetry_enabled=False))
        after = yaml.safe_load(self.render('compose.yaml.j2'))
        for name, field, value in [('prometheus', 'image', 'different'), ('loki', 'network_mode', 'bridge'),
                                   ('grafana', 'read_only', False)]:
            changed = copy.deepcopy(after)
            changed['services'][name][field] = value
            with self.assertRaises(ValueError):
                guard.require_receiver_services_only(before, changed)
        changed = copy.deepcopy(after)
        changed['services']['tempo']['cpus'] = 1
        with self.assertRaises(ValueError):
            guard.require_receiver_services_only(changed, after)

    def test_proxy_preserves_uis_and_restricts_ingestion_to_the_actual_peer(self):
        before = self.render('ui-nginx.conf.j2', microserver_telemetry_enabled=False)
        after = self.render('ui-nginx.conf.j2')
        guard.require_ingestion_only(before, after)
        guard.require_ingestion_only(after, after)
        block = '\n'.join(guard.split_ingestion(after)[1])
        self.assertIn('listen 192.168.30.10:8080 default_server;', block)
        self.assertEqual(re.findall(r'allow ([^;]+);', block), ['192.168.30.11/32'])
        self.assertIn('deny all;', block)
        self.assertEqual(block.count('limit_except POST { deny all; }'), 4)
        self.assertIn('client_max_body_size 4m;', block)
        self.assertNotIn('real_ip_header', after)
        self.assertNotIn('$http_x_forwarded_for', after)
        self.assertEqual(re.findall(r'location = ([^ ]+) \{', block),
                         ['/ready', '/api/v1/write', '/loki/api/v1/push', '/otlp/v1/logs', '/v1/traces',
                          '/metrics/prometheus', '/metrics/loki', '/metrics/tempo', '/metrics/grafana'])
        self.assertEqual(block.count('limit_except GET { deny all; }'), 5)

    def test_proxy_validator_refuses_ui_changes_changed_receivers_and_bad_markers(self):
        before = self.render('ui-nginx.conf.j2', microserver_telemetry_enabled=False)
        after = self.render('ui-nginx.conf.j2')
        with self.assertRaises(ValueError):
            guard.require_ingestion_only(before, after.replace('127.0.0.1:3000', '127.0.0.1:9999'))
        with self.assertRaises(ValueError):
            guard.require_ingestion_only(after.replace('allow 192.168.30.11/32;', 'allow 192.168.30.12/32;'), after)
        for text in [after.replace(guard.END, ''), after + '\n' + guard.BEGIN,
                     after.replace(guard.BEGIN, 'X').replace(guard.END, guard.BEGIN).replace('X', guard.END)]:
            with self.assertRaises(ValueError):
                guard.require_ingestion_only(before, text)

    def test_backend_listeners_retention_and_datasource_are_private(self):
        tempo = read_yaml(ROOT / 'observability/config/tempo.yaml')
        self.assertEqual(tempo['target'], 'all')
        self.assertEqual(tempo['server']['http_listen_address'], '127.0.0.1')
        self.assertEqual(tempo['server']['grpc_listen_address'], '127.0.0.1')
        self.assertEqual(tempo['server']['grpc_listen_port'], 9096)
        self.assertEqual(tempo['distributor']['receivers']['otlp']['protocols'], {'http': {'endpoint': '127.0.0.1:4318'}})
        self.assertEqual(tempo['overrides']['defaults']['compaction']['block_retention'], '72h')
        datasource = read_yaml(ROOT / 'observability/config/datasources.traces.yaml')['datasources'][0]
        self.assertEqual(datasource['uid'], 'tempo')
        self.assertEqual(datasource['url'], 'http://127.0.0.1:3200')
        self.assertFalse(datasource['editable'])

    def test_receiver_requires_distinct_private_addresses_and_active_ui_scope(self):
        context = dict(self.context, ansible_facts={'all_ipv4_addresses': ['192.168.30.10']})
        self.assertTrue(assertions(self.tasks[0], context))
        for change in [dict(microserver_ui_enabled=False), dict(microserver_telemetry_enabled=False),
                       dict(microserver_telemetry_client_address='192.168.30.10')]:
            self.assertFalse(assertions(self.tasks[0], dict(context, **change)))
        for address in ['0.0.0.0', '203.0.113.1', '192.168.30.11/24', '192.168.30.11; allow all']:
            with self.assertRaises(ValueError):
                validate.validate(dict(address='192.168.30.10', domain='lan.example.test',
                                       networks=['192.168.30.10/32', address + '/32']))

    def test_component_only_recreates_tempo_and_grafana_without_repulling_other_images(self):
        command = next(task['ansible.builtin.command']['argv'] for task in self.tasks
                       if task['name'] == 'Start Tempo and reload only Grafana provisioning')
        self.assertEqual(command[-2:], ['tempo', 'grafana'])
        self.assertIn('--no-deps', command)
        self.assertIn('never', command)
        self.assertNotIn('down', command)
        play = read_yaml(ROOT / 'ansible/owner-telemetry.yml')[0]
        self.assertEqual(play['tasks'][0]['ansible.builtin.import_role']['tasks_from'], 'telemetry')
        port_check = next(task for task in self.tasks if 'ansible.builtin.wait_for' in task)
        self.assertEqual([item['port'] for item in port_check['loop']], [8080, 3200, 4318, 9096])
        self.assertEqual(port_check['ansible.builtin.wait_for']['state'], 'stopped')
        self.assertLess(self.tasks.index(port_check), next(index for index, task in enumerate(self.tasks)
                                                         if 'ansible.builtin.file' in task))

    def test_later_full_applies_cannot_implicitly_drop_the_trace_backend(self):
        task = next(task for task in read_yaml(ROLE / 'tasks/monitoring.yml')
                    if task['name'].startswith('Preserve an installed trace component'))
        installed = {'stat': dict(exists=True, isreg=True, islnk=False, uid=0)}
        self.assertTrue(assertions(task, dict(self.context, microserver_existing_tempo=installed)))
        self.assertFalse(assertions(task, dict(self.context, microserver_existing_tempo=installed,
                                              microserver_telemetry_enabled=False)))
        self.assertFalse(assertions(task, dict(self.context, microserver_existing_tempo=installed,
                                              microserver_ui_enabled=False)))
        self.assertFalse(assertions(task, dict(self.context, microserver_existing_tempo={'stat': dict(exists=False)})))
        for name in ['ui', 'monitoring']:
            tasks = read_yaml(ROLE / ('tasks/' + name + '.yml'))
            source_guard = next(task for task in tasks if task['name'].startswith('Validate the retained exact workstation source'))
            self.assertTrue(assertions(source_guard, self.context))
            for address in ['', '0.0.0.0', '203.0.113.2', '192.168.30.11/24', '192.168.30.11; allow all',
                            '192.168.30.10', '192.168.30.999', '192.168.030.11']:
                self.assertFalse(assertions(source_guard, dict(self.context, microserver_telemetry_client_address=address)))

    def profiles(self, **updates):
        return dict(microserver_telemetry_profiles_enabled=True, **updates)

    def test_profiles_are_off_by_default_and_add_no_service_or_route(self):
        config = yaml.safe_load(self.render('compose.yaml.j2'))
        self.assertNotIn('pyroscope', config['services'])
        nginx = self.render('ui-nginx.conf.j2')
        self.assertNotIn('4040', nginx)
        self.assertNotIn(guard.PROFILES_BEGIN, nginx)

    def test_pyroscope_is_bounded_private_and_only_added(self):
        tempo_only = yaml.safe_load(self.render('compose.yaml.j2'))
        after = yaml.safe_load(self.render('compose.yaml.j2', **self.profiles()))
        guard.require_receiver_services_only(tempo_only, after)
        guard.require_receiver_services_only(after, after)
        pyroscope = after['services']['pyroscope']
        self.assertRegex(pyroscope['image'], r'^docker.io/grafana/pyroscope:2[.]4[.]3@sha256:[a-f0-9]{64}$')
        self.assertEqual(pyroscope['user'], '10001:10001')
        self.assertTrue(pyroscope['read_only'])
        self.assertEqual(pyroscope['cap_drop'], ['ALL'])
        self.assertEqual((pyroscope['mem_limit'], pyroscope['memswap_limit'], pyroscope['cpus']), ('768m', '768m', 0.25))
        self.assertEqual(pyroscope['volumes'], ['/etc/microserver/monitoring/pyroscope.yaml:/etc/pyroscope/pyroscope.yaml:ro',
                                                '/var/lib/microserver/pyroscope:/data'])
        self.assertTrue(all(flag.endswith('=127.0.0.1') for flag in pyroscope['command'][1:]))
        changed = copy.deepcopy(after)
        changed['services']['pyroscope']['cpus'] = 1
        with self.assertRaises(ValueError):
            guard.require_receiver_services_only(after, changed)
        with self.assertRaises(ValueError):
            guard.require_receiver_services_only(after, tempo_only)
        changed = copy.deepcopy(after)
        changed['services']['grafana']['read_only'] = False
        with self.assertRaises(ValueError):
            guard.require_receiver_services_only(tempo_only, changed)

    def test_profiles_routes_are_the_only_change_to_an_installed_ingestion_block(self):
        installed = self.render('ui-nginx.conf.j2')
        after = self.render('ui-nginx.conf.j2', **self.profiles())
        guard.require_ingestion_only(installed, after)
        guard.require_ingestion_only(after, after)
        block = '\n'.join(guard.split_ingestion(after)[1])
        self.assertEqual(re.findall(r'allow ([^;]+);', block), ['192.168.30.11/32'])
        self.assertIn('client_max_body_size 4m;', block)
        _, routes = guard.split_profiles(guard.split_ingestion(after)[1])
        routes = '\n'.join(routes)
        self.assertEqual(re.findall(r'location = ([^ ]+) \{', routes), ['/push.v1.PusherService/Push', '/metrics/pyroscope'])
        self.assertIn('limit_except POST { deny all; }', routes)
        self.assertIn('limit_except GET { deny all; }', routes)
        self.assertEqual(set(re.findall(r'proxy_pass http://([^/;]+)', routes)), {'127.0.0.1:4040'})
        self.assertLess(block.index(guard.PROFILES_END), block.index('location / { return 404; }'))
        with self.assertRaises(ValueError):
            guard.require_ingestion_only(after, installed)
        with self.assertRaises(ValueError):
            guard.require_ingestion_only(installed, after.replace('127.0.0.1:4318/v1/traces', '127.0.0.1:9999/v1/traces'))
        with self.assertRaises(ValueError):
            guard.require_ingestion_only(after, after.replace('127.0.0.1:4040/metrics', '127.0.0.1:4041/metrics'))
        with self.assertRaises(ValueError):
            guard.require_ingestion_only(installed, after.replace(guard.PROFILES_END, ''))

    def test_profiles_backend_listeners_retention_and_datasource_are_private(self):
        config = read_yaml(ROOT / 'observability/config/pyroscope.yaml')
        self.assertEqual((config['target'], config['architecture_storage']), ('all', 'v2'))
        self.assertEqual(config['server']['http_listen_address'], '127.0.0.1')
        self.assertEqual(config['server']['grpc_listen_address'], '127.0.0.1')
        self.assertEqual((config['server']['http_listen_port'], config['server']['grpc_listen_port']), (4040, 9097))
        self.assertEqual(config['metastore']['address'], '127.0.0.1:9097')
        self.assertEqual(config['metastore']['raft']['bind_address'], '127.0.0.1:9099')
        self.assertEqual(config['memberlist']['bind_addr'], ['127.0.0.1'])
        for ring in (config['distributor']['ring'], config['overrides_exporter']['ring'], config['frontend']):
            self.assertEqual(ring['instance_addr'], '127.0.0.1')
        self.assertEqual(config['segment_writer']['lifecycler']['address'], '127.0.0.1')
        self.assertEqual(config['limits']['retention_period'], '72h')
        self.assertLessEqual(config['limits']['ingestion_rate_mb'], 1)
        self.assertEqual(config['storage']['backend'], 'filesystem')
        self.assertTrue(config['storage']['filesystem']['dir'].startswith('/data/'))
        self.assertFalse(config['analytics']['reporting_enabled'])
        self.assertTrue(config['self_profiling']['disable_push'])
        datasource = read_yaml(ROOT / 'observability/config/datasources.profiles.yaml')['datasources'][0]
        self.assertEqual((datasource['uid'], datasource['type'], datasource['url']),
                         ('pyroscope', 'grafana-pyroscope-datasource', 'http://127.0.0.1:4040'))
        self.assertFalse(datasource['editable'])

    def test_profiles_ports_are_checked_and_later_applies_keep_the_backend(self):
        check = next(task for task in self.tasks if task['name'] == 'Require unused profiles ports before first installation')
        self.assertEqual(check['loop'], [4040, 9097, 9099, 7947])
        keep = next(task for task in self.tasks if task['name'] == 'Keep an installed profiles backend unless it is explicitly retained')
        installed = {'results': [{'item': {'path': '/etc/microserver/monitoring/pyroscope.yaml'}, 'stat': {'exists': True}}]}
        self.assertTrue(assertions(keep, dict(self.context, microserver_telemetry_paths=installed, **self.profiles())))
        self.assertFalse(assertions(keep, dict(self.context, microserver_telemetry_paths=installed,
                                               microserver_telemetry_profiles_enabled=False)))
        for name, register in [('ui', 'microserver_ui_existing_pyroscope'), ('monitoring', 'microserver_existing_pyroscope')]:
            task = next(task for task in read_yaml(ROLE / ('tasks/' + name + '.yml'))
                        if task['name'] == 'Preserve an installed profiles backend and its receiver routes')
            exists = {register: {'stat': {'exists': True}}}
            self.assertTrue(assertions(task, dict(self.context, **exists, **self.profiles())))
            self.assertFalse(assertions(task, dict(self.context, **exists, microserver_telemetry_profiles_enabled=False)))
            self.assertFalse(assertions(task, dict(self.context, **{register: {'stat': {'exists': False}}},
                                                   microserver_telemetry_enabled=False, **self.profiles())))


if __name__ == '__main__':
    unittest.main()
