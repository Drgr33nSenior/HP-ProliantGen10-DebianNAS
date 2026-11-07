"""Safety regressions for owner-authorized isolated storage writes."""
import hashlib
import io
import json
import contextlib
from pathlib import Path
import tempfile
import unittest
from unittest import mock

from check_houston import load

write = load('microserver-zfs-write-test')


class ZFSWriteTests(unittest.TestCase):
    def state(self, mountpoint='/run/fixture/data'):
        return dict(dataset='nas/acceptance-' + 'a' * 32, nonce='a' * 32, guid='123', mountpoint=mountpoint)

    def test_real_exclusive_write_fsync_and_checksum_and_no_symlink_following(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'data.bin'
            write.write_file(path, b'original')
            self.assertEqual(write.digest(path), hashlib.sha256(b'original').hexdigest())
            with self.assertRaises(FileExistsError):
                write.write_file(path, b'collision')
            link = Path(directory) / 'link'
            link.symlink_to(path)
            with self.assertRaises(OSError):
                write.write_file(link, b'bad', replace=True)
            self.assertEqual(path.read_bytes(), b'original')

    def test_wrong_host_or_pool_guid_prevents_dataset_creation(self):
        with mock.patch.object(write.os, 'geteuid', return_value=0), mock.patch.object(write.socket, 'gethostname', return_value='microserver'), mock.patch.object(write, 'run', return_value='999') as run:
            with self.assertRaises(RuntimeError):
                write.accept('nas', '123')
            self.assertEqual(run.call_args_list, [mock.call(write.ZPOOL, 'get', '-H', '-o', 'value', 'guid', 'nas')])

    def test_unowned_name_guid_marker_or_quota_prevents_rollback_and_cleanup(self):
        good = {write.MARKER: 'a' * 32, 'guid': '123', 'mountpoint': '/run/fixture/data', 'quota': str(write.LIMIT), 'refquota': str(write.LIMIT)}
        for field in (write.MARKER, 'guid', 'mountpoint', 'quota', 'refquota'):
            values = dict(good, **{field: 'foreign'})
            with self.subTest(field=field), mock.patch.object(write, 'prop', side_effect=lambda ds, key: values[key]), mock.patch.object(write, 'run') as run:
                with self.assertRaises(RuntimeError):
                    write.owned(self.state())
                run.assert_not_called()
        with self.assertRaises(RuntimeError):
            write.owned(dict(self.state(), dataset='nas/production'))

    def test_unexpected_descendants_prevent_snapshot_cleanup(self):
        state = self.state()
        properties = {write.MARKER: state['nonce'], 'guid': state['guid'], 'mountpoint': state['mountpoint'], 'quota': str(write.LIMIT), 'refquota': str(write.LIMIT)}
        with mock.patch.object(write, 'prop', side_effect=lambda ds, key: properties[key]), mock.patch.object(write, 'inventory', return_value={state['dataset'], state['dataset'] + '@baseline', state['dataset'] + '/foreign'}), mock.patch.object(write, 'run') as run:
            with self.assertRaises(RuntimeError):
                write.owned(state)
            run.assert_not_called()

    def test_feature_acceptance_allows_activation_but_never_enables_features(self):
        with mock.patch.object(write, 'run', return_value='feature@a\tenabled\nfeature@b\tdisabled'):
            before = write.features('nas')
        with mock.patch.object(write, 'run', return_value='feature@a\tactive\nfeature@b\tdisabled'):
            self.assertEqual(write.features('nas'), before)
        with mock.patch.object(write, 'run', return_value='feature@a\tactive\nfeature@b\tenabled'):
            self.assertNotEqual(write.features('nas'), before)

    def test_snapshot_holds_and_clones_prevent_rollback_and_cleanup(self):
        state = self.state()
        properties = {write.MARKER: state['nonce'], 'guid': state['guid'], 'mountpoint': state['mountpoint'], 'quota': str(write.LIMIT), 'refquota': str(write.LIMIT), 'clones': '-'}
        for hold, clones in [('external-hold', '-'), ('', 'nas/foreign-clone')]:
            with self.subTest(hold=hold, clones=clones), mock.patch.object(write, 'prop', side_effect=lambda ds, key: clones if key == 'clones' else properties[key]), mock.patch.object(write, 'inventory', return_value={state['dataset'], state['dataset'] + '@baseline'}), mock.patch.object(write, 'run', return_value=hold) as run:
                with self.assertRaises(RuntimeError):
                    write.owned(state)
                self.assertEqual(run.call_args_list, [mock.call(write.ZFS, 'holds', '-H', state['dataset'] + '@baseline')])

    def test_failed_create_retains_private_state_and_never_attempts_destruction(self):
        with tempfile.TemporaryDirectory() as directory:
            state_dir, runtime = Path(directory) / 'state', Path(directory) / 'runtime'
            state_dir.mkdir()
            runtime.mkdir()
            def command(*args):
                if args[0] == write.ZPOOL:
                    return {'get': '123', 'list': 'ONLINE' if 'health' in args else str(1024 ** 3),
                            'status': 'errors: No known data errors'}[args[1]]
                if args[:2] == (write.ZFS, 'create'):
                    raise RuntimeError('fixture create refused')
                raise AssertionError('unexpected command')
            with mock.patch.object(write.os, 'geteuid', return_value=0), mock.patch.object(write.socket, 'gethostname', return_value='microserver'), mock.patch.object(write, 'run', side_effect=command) as run, mock.patch.object(write, 'inventory', return_value={'nas'}), mock.patch.object(write, 'features', return_value={'feature@fixture'}), mock.patch.object(write, 'trusted_directory'), mock.patch.object(write.tempfile, 'mkdtemp', side_effect=[str(state_dir), str(runtime)]), contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
                with self.assertRaises(RuntimeError):
                    write.accept('nas', '123')
            self.assertEqual(json.loads((state_dir / 'state.json').read_text())['stage'], 'prepared')
            self.assertEqual((state_dir / 'state.json').stat().st_mode & 0o777, 0o600)
            self.assertFalse(any('destroy' in call.args or 'rollback' in call.args for call in run.call_args_list))


if __name__ == '__main__':
    unittest.main()
