"""ZFS monitoring fixtures; no writes to or administrative operations on a pool."""
import importlib.util
import hashlib
from datetime import datetime, timezone
import json
from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest import mock

import yaml
import check_microserver as fixtures
from check_workstation import verify

spec = importlib.util.spec_from_file_location('zfs_metrics', fixtures.ROLE / 'files/microserver-zfs-metrics.py')
collector = importlib.util.module_from_spec(spec)
spec.loader.exec_module(collector)


class ZFSMonitoringTests(unittest.TestCase):
    def setUp(self):
        self.pool = {'name': 'nas', 'state': 'ONLINE', 'error_count': 0,
                     'vdevs': {'nas': {'guid': 1, 'state': 'ONLINE', 'read_errors': 0,
                                      'write_errors': 0, 'checksum_errors': 0,
                                      'vdevs': {'disk': {'guid': 2, 'state': 'ONLINE',
                                                        'read_errors': 0, 'write_errors': 0, 'checksum_errors': 0}}}}}

    def collect(self, pool=None, listed=None):
        payload = {'output_version': {'vers_major': 0, 'vers_minor': 1}, 'pools': {'nas': pool or self.pool}}
        with mock.patch.object(collector, 'command', side_effect=[listed or 'nas\t1000\t200\t800\t20\t1\tONLINE\n', json.dumps(payload)]):
            return '\n'.join(collector.collect('nas'))

    def test_healthy_capacity_and_no_scan_are_reported_without_invented_scrub_dates(self):
        text = self.collect()
        self.assertIn('microserver_zfs_pool_healthy{pool="nas"} 1', text)
        self.assertIn('microserver_zfs_pool_capacity_ratio{pool="nas"} 0.2', text)
        self.assertIn('microserver_zfs_scan_present{pool="nas"} 0', text)
        self.assertNotIn('scan_end_timestamp_seconds', text)

    def test_degraded_device_and_errors_are_preserved(self):
        self.pool['state'] = 'DEGRADED'
        disk = self.pool['vdevs']['nas']['vdevs']['disk']
        disk.update(state='FAULTED', read_errors=7, checksum_errors=2)
        text = self.collect(listed='nas\t1000\t200\t800\t20\t1\tDEGRADED\n')
        self.assertIn('pool_healthy{pool="nas"} 0', text)
        self.assertIn('vdev_healthy{pool="nas",vdev_guid="2"} 0', text)
        self.assertIn('vdev_read_errors_total{pool="nas",vdev_guid="2"} 7', text)
        self.assertIn('vdev_checksum_errors_total{pool="nas",vdev_guid="2"} 2', text)

    def test_finished_scrub_and_active_resilver_have_distinct_labels(self):
        for function, state in [('SCRUB', 'FINISHED'), ('RESILVER', 'SCANNING')]:
            self.pool['scan_stats'] = dict(function=function, state=state, start_time=100, end_time=200,
                                           examined=500, to_examine=1000, errors=0)
            text = self.collect()
            self.assertIn('function="' + function + '",pool="nas",state="' + state + '"', text)
            self.assertIn('scan_end_timestamp_seconds', text)

    def test_malformed_or_duplicate_statistics_are_refused(self):
        for changes in [dict(error_count=-1), dict(error_count=True), dict(name='other'), dict(state='DEGRADED')]:
            with self.subTest(changes=changes), self.assertRaises((ValueError, KeyError, TypeError)):
                self.collect(dict(self.pool, **changes))
        self.pool['vdevs']['nas']['vdevs']['disk']['guid'] = 1
        with self.assertRaises(ValueError):
            self.collect()

    def test_command_has_fixed_read_only_binary_timeout_and_bound(self):
        with mock.patch.object(collector.subprocess, 'run', return_value=subprocess.CompletedProcess([], 0, stdout=b'{}')) as run:
            collector.command(['status', '-j', '-p', '--json-int', 'nas'])
        args, kwargs = run.call_args
        self.assertEqual(args[0], ['/usr/sbin/zpool', 'status', '-j', '-p', '--json-int', 'nas'])
        self.assertEqual(kwargs['timeout'], 6)
        with mock.patch.object(collector.subprocess, 'run', return_value=subprocess.CompletedProcess([], 0, stdout=b'x' * (collector.MAX_RESPONSE + 1))):
            with self.assertRaises(ValueError):
                collector.command(['status'])

    def test_failed_collection_replaces_old_healthy_metrics_without_partial_data(self):
        with tempfile.TemporaryDirectory() as work:
            target = Path(work) / 'zfs.prom'
            target.write_text('old healthy sample\n')
            with mock.patch.object(collector, 'OUTPUT', target), mock.patch.object(collector, 'collect', side_effect=OSError), \
                    mock.patch('sys.argv', ['collector', '--pool', 'nas']):
                self.assertEqual(collector.main(), 1)
            self.assertIn('microserver_zfs_collection_success 0', target.read_text())
            self.assertNotIn('old healthy', target.read_text())
            self.assertEqual(target.stat().st_mode & 0o777, 0o644)
            self.assertEqual(list(Path(work).iterdir()), [target])

    def test_invalid_pool_names_cannot_add_options_or_shell_syntax(self):
        for name in ['--help', 'nas; touch bad', '../nas', 'nas\n', '']:
            with self.subTest(name=name), mock.patch.object(collector, 'command') as command:
                with self.assertRaises(ValueError):
                    collector.collect(name)
                command.assert_not_called()

    def test_every_dashboard_query_is_nas_scoped_and_uses_the_existing_datasource(self):
        dashboard = json.loads((fixtures.ROLE / 'files/microserver-zfs-dashboard.json').read_text())
        self.assertEqual(dashboard['uid'], 'microserver-zfs')
        for panel in dashboard['panels']:
            self.assertEqual(panel['datasource']['uid'], 'prometheus')
            for target in panel['targets']:
                self.assertIn('job="microserver-node"', target['expr'])
        self.assertIn('not a complete scrub history', json.dumps(dashboard))

    def test_existing_rules_remain_intact_and_zfs_has_its_own_group(self):
        groups = yaml.safe_load((fixtures.ROLE / 'files/alerts.yaml').read_text())['groups']
        self.assertEqual(groups[0]['name'], 'microserver')
        self.assertEqual(len(groups[0]['rules']), 6)
        self.assertEqual(groups[1]['name'], 'microserver_zfs')
        self.assertEqual(len(groups[1]['rules']), 7)
        self.assertTrue(all('job="microserver-node"' in rule['expr'] for rule in groups[1]['rules']))

    def test_component_changes_no_storage_configuration_and_restarts_only_prometheus(self):
        play = yaml.safe_load((fixtures.ROOT / 'ansible/owner-zfs.yml').read_text())[0]
        self.assertEqual(play['tasks'][2]['ansible.builtin.import_role']['tasks_from'], 'zfs-monitoring')
        text = (fixtures.ROLE / 'tasks/zfs-monitoring.yml').read_text()
        for forbidden in ['zfs create', 'zpool scrub', 'zpool clear', 'zpool import', 'exports', 'nfs-kernel-server', 'compose.yaml.j2']:
            self.assertNotIn(forbidden, text)
        handlers = yaml.safe_load((fixtures.ROLE / 'handlers/main.yml').read_text())
        handler = next(item for item in handlers if item['name'] == 'Reload MicroServer Prometheus rules')
        self.assertEqual(handler['ansible.builtin.command']['argv'][-1], 'prometheus')
        self.assertIn('--no-deps', handler['ansible.builtin.command']['argv'])

    def test_owner_rule_update_accepts_only_the_retained_base_or_current_file(self):
        play = yaml.safe_load((fixtures.ROOT / 'ansible/owner-zfs.yml').read_text())[0]
        task = next(item for item in play['tasks'] if item['name'] == 'Require the existing protected NAS rule file')
        source = (fixtures.ROLE / 'files/alerts.yaml').read_text()
        def valid(content):
            context = {'microserver_zfs_source_rules': source, 'microserver_zfs_rule_path': {'stat': {
                'exists': True, 'isreg': True, 'islnk': False, 'uid': 0, 'mode': '0644',
                'checksum': hashlib.sha256(content.encode()).hexdigest()}}}
            return fixtures.assertions(task, context)
        self.assertTrue(valid(source))
        self.assertTrue(valid(source.split('  - name: microserver_zfs')[0]))
        self.assertFalse(valid(source.replace('MicroServerMemoryLow', 'ChangedByAnotherOperator')))

    def test_zfs_rule_acceptance_preserves_firing_alerts_and_refuses_stale_evaluation(self):
        rules = yaml.safe_load((fixtures.ROLE / 'files/alerts.yaml').read_text())['groups'][1]['rules']
        group = {'name': 'microserver_zfs', 'file': '/etc/prometheus/alerts.yaml',
                 'lastEvaluation': datetime.now(timezone.utc).isoformat(),
                 'rules': [dict(name=rule['alert'], health='ok', state='firing') for rule in rules]}
        payload = {'status': 'success', 'data': {'groups': [group,
                   {'name': 'microserver', 'file': '/etc/prometheus/alerts.yaml', 'rules': [{}] * 6}]}}
        self.assertEqual(verify.verify_zfs_rules(payload), 7)
        group['lastEvaluation'] = '2020-01-01T00:00:00Z'
        with self.assertRaisesRegex(RuntimeError, 'stale'):
            verify.verify_zfs_rules(payload)


if __name__ == '__main__':
    unittest.main()
