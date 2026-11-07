#!/usr/bin/python3
"""Owner-run, quota-limited ZFS acceptance. Failed fixtures are retained."""
import argparse
import hashlib
import json
import os
from pathlib import Path
import re
import socket
import stat
import subprocess
import sys
import tempfile
import uuid

ZFS = '/usr/sbin/zfs'
ZPOOL = '/usr/sbin/zpool'
MARKER = 'org.thespry:acceptance-id'
LIMIT = 64 * 1024 * 1024


def require(condition, message):
    if not condition:
        raise RuntimeError(message)


def run(*args):
    result = subprocess.run(args, capture_output=True, text=True, timeout=30,
                            env=dict(os.environ, LC_ALL='C'))
    require(result.returncode == 0, 'bounded ZFS command failed; retain fixture state for review')
    return result.stdout.strip()


def trusted_directory(path):
    metadata = path.lstat()
    require(stat.S_ISDIR(metadata.st_mode) and metadata.st_uid == 0
            and metadata.st_mode & 0o022 == 0, 'untrusted transaction parent')


def inventory(pool):
    return set(run(ZFS, 'list', '-H', '-r', '-t', 'all', '-o', 'name', pool).splitlines())


def features(pool):
    # A write can move an enabled feature to active. It must not enable features.
    return {line.split('\t')[0] for line in run(ZPOOL, 'get', '-H', '-o', 'property,value', 'all', pool).splitlines()
            if line.startswith('feature@') and line.split('\t')[1] != 'disabled'}


def prop(dataset, name):
    return run(ZFS, 'get', '-H', '-p', '-o', 'value', name, dataset)


def owned(state):
    dataset = state['dataset']
    require(re.fullmatch(r'nas/acceptance-[0-9a-f]{32}', dataset) is not None,
            'fixture name outside acceptance namespace')
    require(prop(dataset, MARKER) == state['nonce'] and prop(dataset, 'guid') == state['guid'],
            'fixture ownership changed; retain it')
    require(prop(dataset, 'mountpoint') == state['mountpoint'], 'fixture mountpoint changed')
    require(prop(dataset, 'quota') == str(LIMIT) and prop(dataset, 'refquota') == str(LIMIT),
            'fixture quota changed')
    require(inventory(dataset) == {dataset, dataset + '@baseline'}, 'unexpected fixture descendants')
    require(run(ZFS, 'holds', '-H', dataset + '@baseline') == '', 'snapshot holds present')
    require(prop(dataset + '@baseline', 'clones') == '-', 'snapshot clones present')
    require(set(os.listdir(state['mountpoint'])) == {'data.bin'}, 'unexpected fixture files')
    mounted = json.loads(run('/usr/bin/findmnt', '--json', '--mountpoint', state['mountpoint'],
                            '--output', 'SOURCE,TARGET,FSTYPE'))['filesystems']
    require(len(mounted) == 1 and mounted[0] == dict(source=dataset, target=state['mountpoint'], fstype='zfs'),
            'fixture is not mounted at the expected ZFS target')


def write_file(path, content, replace=False):
    flags = os.O_WRONLY | os.O_NOFOLLOW | (os.O_TRUNC if replace else os.O_CREAT | os.O_EXCL)
    with os.fdopen(os.open(path, flags, 0o600), 'wb') as stream:
        stream.write(content)
        stream.flush()
        os.fsync(stream.fileno())
    descriptor = os.open(path.parent, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def digest(path):
    with os.fdopen(os.open(path, os.O_RDONLY | os.O_NOFOLLOW), 'rb') as stream:
        return hashlib.file_digest(stream, 'sha256').hexdigest()


def accept(pool, guid):
    require(os.geteuid() == 0 and socket.gethostname().split('.')[0] == 'microserver',
            'requires the owner NAS administrator session')
    require(pool == 'nas' and re.fullmatch(r'[0-9]{1,20}', guid) is not None, 'unexpected pool identity')
    require(run(ZPOOL, 'get', '-H', '-o', 'value', 'guid', pool) == guid, 'pool GUID mismatch')
    require(run(ZPOOL, 'list', '-H', '-o', 'health', pool) == 'ONLINE', 'pool is not ONLINE')
    require(int(run(ZPOOL, 'list', '-H', '-p', '-o', 'free', pool)) >= 256 * 1024 * 1024, 'insufficient pool space')
    status = run(ZPOOL, 'status', pool)
    require('in progress' not in status and 'resilvering' not in status, 'scrub or resilver in progress')
    require('errors: No known data errors' in status, 'pool reports data errors')
    before, before_features = inventory(pool), features(pool)
    base = Path('/var/lib/microserver')
    for parent in (Path('/var'), Path('/var/lib'), base, Path('/run')):
        trusted_directory(parent)
    nonce = uuid.uuid4().hex
    dataset = pool + '/acceptance-' + nonce
    require(dataset not in before, 'fixture already exists')
    state_dir = Path(tempfile.mkdtemp(prefix='zfs-write-test.', dir=base))
    runtime = Path(tempfile.mkdtemp(prefix='microserver-zfs-test.', dir='/run'))
    mountpoint = runtime / 'data'
    state = dict(nonce=nonce, dataset=dataset, mountpoint=str(mountpoint), guid=None, stage='prepared')
    state_file = state_dir / 'state.json'

    def record(stage):
        state['stage'] = stage
        state_file.write_text(json.dumps(state, sort_keys=True) + '\n')
        os.chmod(state_file, 0o600)

    record('prepared')
    print('ZFS_WRITE_TEST_STATE=' + str(state_dir), flush=True)
    try:
        run(ZFS, 'create', '-o', 'quota=64M', '-o', 'refquota=64M', '-o', 'reservation=none',
            '-o', 'refreservation=none', '-o', 'compression=lz4', '-o', 'atime=off',
            '-o', 'sharenfs=off', '-o', 'sharesmb=off', '-o', 'exec=off', '-o', 'setuid=off',
            '-o', 'devices=off', '-o', 'snapdir=hidden', '-o', 'canmount=on',
            '-o', 'readonly=off', '-o', MARKER + '=' + nonce, '-o', 'mountpoint=' + str(mountpoint), dataset)
        state['guid'] = prop(dataset, 'guid')
        record('created')
        require(prop(dataset, MARKER) == nonce, 'new fixture marker mismatch')
        os.chmod(mountpoint, 0o700)
        mounted = json.loads(run('/usr/bin/findmnt', '--json', '--mountpoint', str(mountpoint),
                                '--output', 'SOURCE,TARGET,FSTYPE'))['filesystems']
        require(mounted == [dict(source=dataset, target=str(mountpoint), fstype='zfs')], 'new fixture mount mismatch')
        path = mountpoint / 'data.bin'
        original = os.urandom(4 * 1024 * 1024)
        expected = hashlib.sha256(original).hexdigest()
        write_file(path, original)
        require(digest(path) == expected, 'initial write checksum mismatch')
        run(ZFS, 'snapshot', dataset + '@baseline')
        record('snapshot-created')
        owned(state)
        write_file(path, os.urandom(len(original)), replace=True)
        require(digest(path) != expected, 'replacement did not change data')
        require(digest(mountpoint / '.zfs/snapshot/baseline/data.bin') == expected, 'snapshot checksum mismatch')
        owned(state)
        run(ZFS, 'rollback', dataset + '@baseline')
        require(digest(path) == expected, 'rollback checksum mismatch')
        record('write-snapshot-rollback-passed')
        owned(state)
        # No recursive/forced destruction: ZFS refuses unexpected children or holds.
        run(ZFS, 'destroy', dataset + '@baseline')
        require(prop(dataset, 'guid') == state['guid'] and prop(dataset, MARKER) == nonce,
                'fixture identity changed before cleanup')
        require(inventory(dataset) == {dataset}, 'unexpected fixture child before cleanup')
        run(ZFS, 'destroy', dataset)
        require(inventory(pool) == before, 'dataset inventory changed during acceptance')
        require(features(pool) == before_features, 'enabled pool features changed during acceptance')
        require(run(ZPOOL, 'status', '-x', pool) == "pool 'nas' is healthy", 'pool health check failed')
        if mountpoint.exists():
            mountpoint.rmdir()
        runtime.rmdir()
        record('passed-and-cleaned')
        print('ZFS_WRITE_TEST=passed; WRITE_SHA256=passed; SNAPSHOT=passed; ROLLBACK=passed; FIXTURE_CLEANUP=passed')
    except Exception:
        print('STOP: Retain the isolated fixture and state for review: ' + str(state_dir), file=sys.stderr)
        raise


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--pool', required=True)
    parser.add_argument('--guid', required=True)
    args = parser.parse_args()
    os.umask(0o077)
    try:
        accept(args.pool, args.guid)
    except RuntimeError as error:
        print('STOP: ' + str(error), file=sys.stderr)
        return 1
    except (OSError, ValueError, subprocess.SubprocessError, KeyError) as error:
        print('STOP: ZFS write acceptance failed (' + type(error).__name__ + '); retain fixture state', file=sys.stderr)
        return 1
    return 0


if __name__ == '__main__':
    sys.exit(main())
