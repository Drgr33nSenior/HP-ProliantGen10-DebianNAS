"""Offline checks for the bounded ZFS package transition and retained recovery state."""
import importlib.util
import os
from pathlib import Path
import re
import shutil
import subprocess
import tempfile
from types import SimpleNamespace
import unittest
from unittest import mock

from check_microserver import ENV, ROOT, ROLE, assertions, read_yaml, yaml

spec = importlib.util.spec_from_file_location('zfs_upgrade', ROLE / 'files/microserver-zfs-upgrade.py')
upgrade = importlib.util.module_from_spec(spec)
spec.loader.exec_module(upgrade)
ENV.filters['regex_search'] = lambda value, pattern: match.group(0) if (match := re.search(pattern, value)) else None


class ZfsUpgradeTests(unittest.TestCase):
    def plan(self):
        return '\n'.join('Inst ' + name + ' [2.3.9-0+deb13u1] (' + upgrade.TARGET + ' Debian Backports [amd64])'
                         for name in upgrade.PACKAGES) + '\nRemv libzfs6linux [2.3.9-0+deb13u1]'

    def test_apt_plan_accepts_only_the_reviewed_abi_transition(self):
        upgrade.validate_plan(self.plan())
        for bad in (self.plan() + '\nRemv nfs-kernel-server [1]',
                    self.plan() + '\nInst linux-image-amd64 (7.0 Debian [amd64])',
                    self.plan().replace(upgrade.TARGET, '2.5.0-1'),
                    self.plan().replace('Inst zfs-dkms ', 'Conf zfs-dkms '),
                    self.plan() + '\nInst unknown syntax'):
            with self.subTest(bad=bad), self.assertRaises(RuntimeError):
                upgrade.validate_plan(bad)

    def test_feature_comparison_preserves_enabled_features_and_ignores_new_disabled_features(self):
        before = 'health\tONLINE\nfeature@async_destroy\tenabled\nfeature@lz4_compress\tactive'
        after = before.replace('async_destroy\tenabled', 'async_destroy\tactive') + '\nfeature@new_feature\tdisabled'
        self.assertEqual(upgrade.features(before), upgrade.features(after))
        self.assertNotEqual(upgrade.features(before), upgrade.features(after.replace('new_feature\tdisabled', 'new_feature\tenabled')))
        with self.assertRaises(RuntimeError):
            upgrade.features('feature@unknown\tunknown')

    def test_pool_identity_health_and_active_maintenance_are_required(self):
        good = ['123', 'ONLINE', 'scan: scrub repaired 0B', 'feature@async_destroy\tenabled']
        with mock.patch.object(upgrade, 'run', side_effect=good):
            self.assertEqual(upgrade.inspect_pool('nas', '123'), ['feature@async_destroy'])
        for bad in (['456'], ['123', 'DEGRADED'], ['123', 'ONLINE', 'scan: resilver in progress']):
            with self.subTest(bad=bad), mock.patch.object(upgrade, 'run', side_effect=bad), self.assertRaises(RuntimeError):
                upgrade.inspect_pool('nas', '123')

    def test_mount_metadata_excludes_unmounted_datasets_and_requires_real_zfs_mounts(self):
        listing = 'nas\tyes\t/nas\nnas/data\tyes\t/nas/data\nnas/backup\tno\t/nas/backup'
        with mock.patch.object(upgrade, 'run', side_effect=[listing, 'zfs', 'zfs']):
            self.assertEqual(upgrade.mounts('nas'), {'nas': '/nas', 'nas/data': '/nas/data'})
        with mock.patch.object(upgrade, 'run', side_effect=[listing, 'xfs']), self.assertRaises(RuntimeError):
            upgrade.mounts('nas')

    def test_unmounted_pool_root_is_a_valid_empty_mount_baseline(self):
        with mock.patch.object(upgrade, 'run', return_value='nas\tno\tnone') as run:
            self.assertEqual(upgrade.mounts('nas'), {})
        self.assertEqual(run.call_count, 1)
        for listing in ('', 'nas\tunknown\tnone'):
            with self.subTest(listing=listing), mock.patch.object(upgrade, 'run', return_value=listing), self.assertRaises(RuntimeError):
                upgrade.mounts('nas')

    def test_loss_of_a_previously_mounted_dataset_still_blocks_the_upgrade(self):
        data = dict(pool='nas', guid='123', kernel='6.12.115+deb13.1-amd64',
                    enabled=['feature@async_destroy'], mounts={'nas/data': '/nas/data'}, nfs='inactive')
        with mock.patch('sys.argv', ['helper', 'plan', '--pool', 'nas', '--guid', '123']), \
                mock.patch.object(upgrade.os, 'geteuid', return_value=0), \
                mock.patch.object(upgrade.os, 'uname', return_value=SimpleNamespace(release=data['kernel'])), \
                mock.patch.object(upgrade.socket, 'gethostname', return_value='microserver'), \
                mock.patch.object(upgrade, 'inspect_pool', return_value=data['enabled']), \
                mock.patch.object(upgrade, 'mounts', return_value={}), \
                mock.patch.object(upgrade, 'nfs_state', return_value='inactive'), \
                mock.patch.object(upgrade, 'read_state', return_value=data), mock.patch.object(upgrade, 'run') as run:
            with self.assertRaisesRegex(RuntimeError, 'no longer mounted'):
                upgrade.main()
        self.assertEqual(run.call_count, 1)
        self.assertEqual(run.call_args.args[0][:2], ['/usr/bin/systemctl', 'is-active'])

    def test_preflight_reports_only_a_bounded_helper_reason_before_any_apt_changes(self):
        tasks = read_yaml(ROOT / 'ansible/owner-zfs-upgrade.yml')[0]['tasks']
        command = next(task for task in tasks if task['name'].startswith('Read the commissioned package baseline'))
        self.assertTrue(command['no_log'])
        self.assertFalse(command['failed_when'])
        guard = next(task for task in tasks if task['name'].startswith('Require preflight acceptance'))
        self.assertLess(tasks.index(guard), next(index for index, task in enumerate(tasks) if 'block' in task))
        context = {'microserver_zfs_old_packages': {'rc': 1, 'stderr': 'STOP: NAS filesystem metadata is missing\n',
                                                 'stdout': 'DUMMY_PROTECTED_PACKAGE_OUTPUT'}}
        self.assertFalse(assertions(guard, context))
        rendered = str(ENV.from_string(guard['ansible.builtin.assert']['fail_msg']).render(**context))
        self.assertEqual(rendered, 'STOP: NAS filesystem metadata is missing')
        for stderr in ('Traceback: DUMMY_PROTECTED_TRACEBACK', 'STOP: <DUMMY_PROTECTED_OUTPUT>', 'STOP: ' + 'x' * 181):
            context['microserver_zfs_old_packages']['stderr'] = stderr
            rendered = str(ENV.from_string(guard['ansible.builtin.assert']['fail_msg']).render(**context))
            self.assertEqual(rendered, 'STOP: ZFS preflight failed; protected diagnostics omitted')

    @unittest.skipUnless(shutil.which('ansible-playbook'), 'Requires the configured local Ansible environment')
    def test_native_ansible_reports_the_public_failure_without_protected_command_output(self):
        guard = next(task for task in read_yaml(ROOT / 'ansible/owner-zfs-upgrade.yml')[0]['tasks']
                     if task['name'].startswith('Require preflight acceptance'))
        fixture = [dict(name='Local tst fixture for preflight failure reporting', hosts='localhost', gather_facts=False,
                        vars=dict(microserver_environment='tst', microserver_zfs_upgrade_operation='stage'), tasks=[
                            {'name': 'Synthetic protected preflight failure', 'ansible.builtin.command': {
                                'argv': ['/bin/sh', '-c', 'printf "%s\\n" "STOP: NAS filesystem metadata is missing" >&2\nprintf "%s\\n" "DUMMY_PROTECTED_OUTPUT"\nexit 1']},
                             'register': 'microserver_zfs_old_packages', 'changed_when': False, 'failed_when': False, 'no_log': True}, guard])]
        with tempfile.TemporaryDirectory(prefix='microserver-preflight-report.') as directory:
            playbook = Path(directory) / 'fixture.yml'
            playbook.write_text(yaml.safe_dump(fixture))
            result = subprocess.run(['ansible-playbook', '-i', 'localhost,', '--connection', 'local', '--limit', 'localhost', str(playbook)],
                                    capture_output=True, text=True, timeout=30, env=dict(os.environ, ANSIBLE_NOCOLOR='1'))
        self.assertEqual(result.returncode, 2, result.stderr)
        self.assertIn('STOP: NAS filesystem metadata is missing', result.stdout)
        self.assertNotIn('DUMMY_PROTECTED_OUTPUT', result.stdout + result.stderr)

    def test_nfs_baseline_distinguishes_inactive_from_failed_units(self):
        for active in ('active', 'inactive'):
            with mock.patch.object(upgrade, 'run', side_effect=[active, 'enabled']):
                self.assertEqual(upgrade.nfs_state(), active)
        with mock.patch.object(upgrade, 'run', side_effect=['failed', 'enabled']), self.assertRaises(RuntimeError):
            upgrade.nfs_state()

    def test_upgrade_is_owner_only_and_does_not_reboot_or_change_pool_features(self):
        play = read_yaml(ROOT / 'ansible/owner-zfs-upgrade.yml')[0]
        context = dict(microserver_environment='prd', microserver_owner_install=True,
                       ansible_play_hosts_all=['nas'], ansible_check_mode=False, microserver_zfs_upgrade_operation='stage',
                       ansible_facts=dict(hostname='microserver', distribution='Debian', distribution_major_version='13', architecture='x86_64'))
        guard = play['pre_tasks'][0]
        self.assertTrue(assertions(guard, context))
        for changes in (dict(microserver_owner_install=False), dict(microserver_environment='unmapped'),
                        dict(ansible_play_hosts_all=['nas', 'other']), dict(ansible_check_mode=True),
                        dict(microserver_zfs_upgrade_operation='reboot')):
            self.assertFalse(assertions(guard, dict(context, **changes)))
        text = (ROOT / 'ansible/owner-zfs-upgrade.yml').read_text() + (ROLE / 'files/microserver-zfs-upgrade.py').read_text()
        for forbidden in ('ansible.builtin.reboot', "'zpool', 'upgrade'", "'zfs', 'upgrade'", "'zpool', 'set'", "'zfs', 'set'", 'allow_unauthenticated: true'):
            self.assertNotIn(forbidden, text)
        stage = next(task['block'] for task in play['tasks'] if 'block' in task)
        install = next(task['ansible.builtin.apt'] for task in stage if 'Install only' in task['name'])
        self.assertEqual(set(install['name']), {name + '=' + upgrade.TARGET for name in upgrade.PACKAGES})
        self.assertEqual(install['policy_rc_d'], 101)
        self.assertFalse(install['install_recommends'])
        self.assertNotIn('autoremove', install)


if __name__ == '__main__':
    unittest.main()
