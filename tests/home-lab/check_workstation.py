"""Workstation rules, narrow component changes and credential-free metadata checks."""
import copy
from datetime import datetime, timezone
import importlib.util
import json
from pathlib import Path
import shutil
import sqlite3
import subprocess
import tempfile
import unittest

import yaml
import check_microserver as fixtures


def load(name):
    spec = importlib.util.spec_from_file_location(name, fixtures.ROLE / 'files' / (name + '.py'))
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


guard = load('microserver-workstation-guard')
verify = load('microserver-workstation-verify')


class WorkstationComponentTests(unittest.TestCase):
    def setUp(self):
        base = fixtures.MicroServerTests()
        base.setUp()
        self.context = dict(base.context, microserver_monitoring_enabled=True, microserver_ui_enabled=True,
                            microserver_ui_address='192.168.30.10', microserver_ui_domain='lan.example.test',
                            microserver_ui_allowed_networks=['192.168.30.0/24'],
                            microserver_telemetry_enabled=True, microserver_telemetry_client_address='192.168.30.11')

    def render(self, name, enabled):
        return str(fixtures.ENV.from_string((fixtures.ROLE / 'templates' / name).read_text()).render(
            **dict(self.context, microserver_workstation_rules_enabled=enabled)))

    def test_only_the_new_rule_reference_changes(self):
        before, after = self.render('prometheus.yaml.j2', False), self.render('prometheus.yaml.j2', True)
        guard.require_rule_only(before, after)
        guard.require_rule_only(after, after)
        for proposed in [before, after.replace('job_name: microserver-node', 'job_name: altered'), after + '\n' + guard.NEW_RULES]:
            with self.assertRaises(ValueError):
                guard.require_rule_only(before, proposed)

    @unittest.skipUnless(shutil.which('docker'), 'Requires local Docker Compose config validation')
    def test_native_compose_adds_only_one_read_only_rule_mount(self):
        with tempfile.TemporaryDirectory() as directory:
            configs = []
            for enabled in [False, True]:
                path = Path(directory) / ('on.yaml' if enabled else 'off.yaml')
                path.write_text(self.render('compose.yaml.j2', enabled))
                result = subprocess.check_output(['docker', 'compose', '--project-name', 'microserver',
                    '--file', str(path), 'config', '--format', 'json'], timeout=20)
                configs.append(json.loads(result))
        before, after = configs
        guard.require_mount_only(before, after)
        guard.require_mount_only(after, after)
        for service, key, value in [('grafana', 'image', 'unreviewed'), ('loki', 'read_only', False)]:
            changed = copy.deepcopy(after)
            changed['services'][service][key] = value
            with self.assertRaises(ValueError):
                guard.require_mount_only(before, changed)
        changed = copy.deepcopy(after)
        mount = next(row for row in changed['services']['prometheus']['volumes'] if row.get('target') == guard.TARGET)
        mount['read_only'] = False
        with self.assertRaises(ValueError):
            guard.require_mount_only(before, changed)

    def test_disabled_profile_has_no_rule_or_mount(self):
        self.assertNotIn('workstation-alerts', self.render('prometheus.yaml.j2', False))
        self.assertNotIn('workstation-alerts', self.render('compose.yaml.j2', False))

    def test_existing_nas_rules_are_separate_and_workstation_data_is_scoped(self):
        path = fixtures.ROOT / 'observability/config/workstation-alerts.yaml'
        rules = yaml.safe_load(path.read_text())['groups'][0]['rules']
        self.assertEqual(len(rules), 16)
        names = {rule.get('alert', rule.get('record')) for rule in rules}
        self.assertEqual(len(names), 16)
        self.assertIn('WorkstationHostAlloyHeartbeatMissing', names)
        for rule in rules:
            if any(name in rule['expr'] for name in ['node_memory_', 'node_pressure_', 'node_filesystem_', 'workstation_gpu_', 'workstation_hardware_']):
                self.assertIn('job="node"', rule['expr'])
        current = yaml.safe_load((fixtures.ROLE / 'files/alerts.yaml').read_text())
        self.assertEqual(current['groups'][0]['name'], 'microserver')
        self.assertEqual(len(current['groups'][0]['rules']), 6)

    def test_owner_component_recreates_only_prometheus_and_keeps_grafana_authentication(self):
        tasks = yaml.safe_load((fixtures.ROLE / 'tasks/workstation.yml').read_text())
        updates = [task['ansible.builtin.command']['argv'] for task in tasks
                   if 'ansible.builtin.command' in task and 'up' in task['ansible.builtin.command']['argv']]
        self.assertEqual(len(updates), 1)
        self.assertEqual(updates[0][-1], 'prometheus')
        self.assertIn('--no-deps', updates[0])
        self.assertIn('never', updates[0])
        self.assertFalse(any('ansible.builtin.systemd_service' in task for task in tasks))
        compose = yaml.safe_load(self.render('compose.yaml.j2', True))
        self.assertEqual(compose['services']['grafana']['environment']['GF_AUTH_ANONYMOUS_ENABLED'], 'false')

    def test_native_validator_requires_a_safe_pinned_prometheus_image(self):
        task = yaml.safe_load((fixtures.ROLE / 'tasks/workstation.yml').read_text())[0]
        context = dict(self.context, microserver_workstation_rules_enabled=True,
                       ansible_facts={'all_ipv4_addresses': ['192.168.30.10']})
        self.assertTrue(fixtures.assertions(task, context))
        for value in ['--privileged unreviewed', 'docker.io/prom/prometheus:latest', '$(unreviewed)', 'image with spaces']:
            self.assertFalse(fixtures.assertions(task, dict(context, microserver_prometheus_image=value)))

    def test_verification_tag_updates_only_validators_and_runs_read_only_checks(self):
        play = yaml.safe_load((fixtures.ROOT / 'ansible/owner-workstation.yml').read_text())[0]
        self.assertIn('always', play['pre_tasks'][0]['tags'])
        tasks = yaml.safe_load((fixtures.ROLE / 'tasks/workstation.yml').read_text())
        selected = [task for task in tasks if 'workstation_verify' in task.get('tags', [])]
        self.assertEqual(len(selected), 10)
        for task in selected:
            if 'ansible.builtin.copy' in task:
                self.assertEqual(task['loop'], ['microserver-workstation-guard.py', 'microserver-workstation-verify.py'])
            if 'ansible.builtin.command' in task:
                self.assertIn(task['ansible.builtin.command']['argv'][0], ['/usr/bin/systemctl', '/usr/bin/python3'])
                if task['ansible.builtin.command']['argv'][0] == '/usr/bin/systemctl':
                    self.assertEqual(task['ansible.builtin.command']['argv'][1], 'is-active')
            self.assertNotIn('ansible.builtin.template', task)
            self.assertNotIn('ansible.builtin.systemd_service', task)

    def test_future_full_apply_cannot_drop_commissioned_rules(self):
        task = next(task for task in yaml.safe_load((fixtures.ROLE / 'tasks/monitoring.yml').read_text())
                    if task['name'] == 'Preserve commissioned workstation rules during later full applies')
        context = dict(self.context, microserver_existing_workstation_rules={'stat': {'exists': True, 'isreg': True, 'islnk': False, 'uid': 0}},
                       microserver_workstation_rules_enabled=True)
        self.assertTrue(fixtures.assertions(task, context))
        self.assertFalse(fixtures.assertions(task, dict(context, microserver_workstation_rules_enabled=False)))
        self.assertFalse(fixtures.assertions(task, dict(context, microserver_telemetry_enabled=False)))


class GrafanaMetadataTests(unittest.TestCase):
    def setUp(self):
        self.connection = sqlite3.connect(':memory:')
        self.addCleanup(self.connection.close)
        self.connection.execute('CREATE TABLE dashboard(uid TEXT,title TEXT,org_id INTEGER,deleted TEXT,is_folder INTEGER DEFAULT 0,data TEXT)')
        self.connection.execute('CREATE TABLE resource("group" TEXT,resource TEXT,namespace TEXT,name TEXT,action INTEGER,value BLOB)')
        self.connection.execute('CREATE TABLE data_source(uid TEXT,type TEXT,url TEXT,org_id INTEGER,password TEXT,secure_json_data TEXT)')
        self.expected = {'workstation-overview': 'Workstation / Overview'}
        self.document = {'kind': 'Dashboard', 'apiVersion': 'dashboard.grafana.app/v0alpha1',
                         'metadata': {'name': 'workstation-overview', 'namespace': 'default'},
                         'spec': {'title': self.expected['workstation-overview'], 'protected_payload': 'DUMMY_FIXTURE_SECRET'}}
        self.connection.execute('INSERT INTO resource VALUES (?,?,?,?,?,?)',
                                ('dashboard.grafana.app', 'dashboards', 'default', 'workstation-overview', 1,
                                 json.dumps(self.document).encode()))
        self.connection.executemany('INSERT INTO data_source VALUES (?,?,?,1,?,?)',
            [(uid, kind, url, 'DUMMY_FIXTURE_SECRET', 'DUMMY_FIXTURE_SECRET') for uid, (kind, url) in verify.SOURCES.items()])

    def test_metadata_queries_succeed_and_sensitive_columns_are_denied(self):
        verify.verify_catalog(self.connection, self.expected)
        for query in ['SELECT password FROM data_source', 'SELECT secure_json_data FROM data_source',
                      'SELECT data FROM dashboard', 'SELECT * FROM data_source', 'UPDATE dashboard SET title="changed"',
                      'SELECT value FROM resource', "SELECT json_extract(value,'$.spec') FROM resource",
                      'SELECT * FROM resource', 'DROP VIEW workstation_dashboard_metadata',
                      'CREATE TEMP VIEW leaked AS SELECT value FROM resource']:
            with self.subTest(query=query), self.assertRaises(sqlite3.DatabaseError):
                self.connection.execute(query)

    def test_missing_dashboard_is_refused(self):
        with self.assertRaisesRegex(RuntimeError, 'dashboard metadata'):
            verify.verify_catalog(self.connection, dict(self.expected, missing='Missing'))

    def test_non_loopback_datasource_is_refused(self):
        self.connection.execute('UPDATE data_source SET url="http://other:9090" WHERE uid="prometheus"')
        with self.assertRaisesRegex(RuntimeError, 'loopback datasource'):
            verify.verify_catalog(self.connection, self.expected)

    def test_soft_deleted_dashboard_is_not_counted(self):
        self.document['metadata']['deletionTimestamp'] = '2026-11-05T17:00:00Z'
        self.connection.execute('UPDATE resource SET value=?', (json.dumps(self.document).encode(),))
        with self.assertRaisesRegex(RuntimeError, 'dashboard metadata'):
            verify.verify_catalog(self.connection, self.expected)

    def test_trashed_dashboard_is_not_counted(self):
        self.document['metadata']['labels'] = {'grafana.app/get-trash': 'true'}
        self.connection.execute('UPDATE resource SET value=?', (json.dumps(self.document).encode(),))
        with self.assertRaisesRegex(RuntimeError, 'dashboard metadata'):
            verify.verify_catalog(self.connection, self.expected)

    def test_other_organization_is_not_counted(self):
        self.connection.execute('UPDATE resource SET namespace="org-2"')
        with self.assertRaisesRegex(RuntimeError, 'dashboard metadata'):
            verify.verify_catalog(self.connection, self.expected)

    def test_wrong_title_is_refused(self):
        with self.assertRaisesRegex(RuntimeError, 'dashboard metadata'):
            verify.verify_catalog(self.connection, {'workstation-overview': 'Unexpected title'})

    def test_stale_legacy_row_cannot_replace_missing_current_resource(self):
        self.connection.execute('INSERT INTO dashboard(uid,title,org_id) VALUES (?,?,1)', list(self.expected.items())[0])
        self.connection.execute('DELETE FROM resource')
        with self.assertRaisesRegex(RuntimeError, 'dashboard metadata'):
            verify.verify_catalog(self.connection, self.expected)

    def test_modified_resource_is_supported(self):
        self.connection.execute('UPDATE resource SET action=2')
        verify.verify_catalog(self.connection, self.expected)

    def test_zfs_catalog_requires_its_resource_but_only_the_prometheus_datasource(self):
        self.document['metadata']['name'] = 'microserver-zfs'
        self.document['spec']['title'] = 'MicroServer / ZFS'
        self.connection.execute('UPDATE resource SET name=?,value=?',
                                ('microserver-zfs', json.dumps(self.document).encode()))
        self.connection.execute('DELETE FROM data_source WHERE uid != "prometheus"')
        verify.verify_catalog(self.connection, {'microserver-zfs': 'MicroServer / ZFS'},
                              {'prometheus': verify.SOURCES['prometheus']})

    def test_rule_health_requires_fresh_evaluation_but_preserves_firing_alerts(self):
        rules = yaml.safe_load((fixtures.ROOT / 'observability/config/workstation-alerts.yaml').read_text())['groups'][0]['rules']
        payload = {'status': 'success', 'data': {'groups': [
            {'name': 'workstation', 'file': '/etc/prometheus/workstation-alerts.yaml',
             'lastEvaluation': datetime.now(timezone.utc).isoformat(),
             'rules': [dict(name=row.get('alert', row.get('record')), health='ok',
                            **({'state': 'firing'} if 'alert' in row else {})) for row in rules]},
            {'name': 'microserver', 'file': '/etc/prometheus/alerts.yaml', 'rules': [{}] * 6}]}}
        self.assertEqual(verify.verify_rule_group(payload), 14)
        changed = copy.deepcopy(payload)
        changed['data']['groups'][0]['rules'][0]['health'] = 'err'
        with self.assertRaisesRegex(RuntimeError, 'evaluation is not healthy'):
            verify.verify_rule_group(changed)


if __name__ == '__main__':
    unittest.main()
