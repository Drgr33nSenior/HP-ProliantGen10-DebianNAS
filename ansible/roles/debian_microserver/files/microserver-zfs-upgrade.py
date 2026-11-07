"""Owner-run ZFS package acceptance; never enable pool features or reboot."""
import argparse
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import socket
import stat
import subprocess
import sys

STATE = Path('/var/lib/microserver/zfs-upgrade-2.4')
TARGET = '2.4.4-1~bpo13+1'
MODULE = '2.4.4'
PACKAGES = ('zfs-dkms', 'zfsutils-linux', 'zfs-zed', 'libnvpair3linux',
            'libuutil3linux', 'libzfs7linux', 'libzpool7linux')
OLD_PACKAGES = ('zfs-dkms', 'zfsutils-linux', 'zfs-zed', 'libnvpair3linux',
                'libuutil3linux', 'libzfs6linux', 'libzpool6linux')
SERVICES = ('microserver-ui.service', 'microserver-monitoring.service',
            'prometheus-node-exporter.service', 'microserver-zfs-metrics.timer')


def require(condition, message):
    if not condition:
        raise RuntimeError(message)


def run(argv, timeout=30):
    result = subprocess.run(argv, capture_output=True, text=True, timeout=timeout, check=False,
                            env=dict(os.environ, LC_ALL='C'))
    require(result.returncode == 0, Path(argv[0]).name + ' failed; retain the private owner log')
    require(len(result.stdout) < 1048576, 'Command output exceeded its bound')
    return result.stdout.strip()


def protected(path, directory=False):
    info = path.lstat()
    require(info.st_uid == 0 and not info.st_mode & 0o022
            and (stat.S_ISDIR(info.st_mode) if directory else stat.S_ISREG(info.st_mode)),
            'Unexpected ZFS upgrade path type or permissions')


def features(text):
    result = {}
    for line in text.splitlines():
        name, value = line.split('\t')
        if name.startswith('feature@'):
            require(value in ('disabled', 'enabled', 'active'), 'Unknown pool feature state')
            if value != 'disabled':
                result[name] = True
    require(bool(result), 'Pool feature metadata is incomplete')
    return sorted(result)


def inspect_pool(pool, guid):
    require(run(['/usr/sbin/zpool', 'get', '-H', '-o', 'value', 'guid', pool]) == guid, 'NAS pool identity differs')
    require(run(['/usr/sbin/zpool', 'get', '-H', '-o', 'value', 'health', pool]) == 'ONLINE', 'NAS pool is not ONLINE')
    status = run(['/usr/sbin/zpool', 'status', pool])
    require('in progress' not in status and 'resilvering' not in status, 'Wait for the active pool maintenance operation')
    return features(run(['/usr/sbin/zpool', 'get', '-H', '-o', 'property,value', 'all', pool]))


def mounts(pool):
    result = {}
    rows = run(['/usr/sbin/zfs', 'list', '-H', '-t', 'filesystem', '-o', 'name,mounted,mountpoint', '-r', pool]).splitlines()
    require(bool(rows), 'NAS filesystem metadata is missing')
    for line in rows:
        name, mounted, path = line.split('\t')
        require(mounted in ('yes', 'no'), 'Unknown dataset mounted state')
        if mounted == 'yes':
            require(path.startswith('/'), 'Unexpected dataset mountpoint')
            require(run(['/usr/bin/findmnt', '-n', '-o', 'FSTYPE', '--target', path]) == 'zfs', 'Dataset mount does not refer to ZFS')
            result[name] = path
    # A pool root with mountpoint=none is a valid baseline. Record only actual
    # mounts; later acceptance still requires every recorded mount to remain.
    return result


def nfs_state():
    active = run(['/usr/bin/systemctl', 'show', 'nfs-server.service', '--property=ActiveState', '--value'])
    enabled = run(['/usr/bin/systemctl', 'show', 'nfs-server.service', '--property=UnitFileState', '--value'])
    require(active in ('active', 'inactive') and enabled == 'enabled', 'NFS unit needs review')
    return active


def secure_boot():
    entries = list(Path('/sys/firmware/efi/efivars').glob('SecureBoot-*'))
    if not Path('/sys/firmware/efi').exists():
        return False
    require(len(entries) == 1, 'Cannot determine UEFI Secure Boot state')
    data = entries[0].read_bytes()
    require(len(data) == 5 and data[-1] in (0, 1), 'Unknown Secure Boot state')
    return data[-1] == 1


def packages():
    rows = run(['/usr/bin/dpkg-query', '-W', '-f=${binary:Package}\t${Version}\t${db:Status-Status}\n',
                *OLD_PACKAGES])
    result = {}
    for row in rows.splitlines():
        name, version, status_value = row.split('\t')
        if status_value == 'installed':
            result[name.split(':')[0]] = version
    require(set(result) >= {'zfs-dkms', 'zfsutils-linux', 'zfs-zed', 'libzfs6linux'}, 'Unexpected installed ZFS package set')
    require(all(value == '2.3.9-0+deb13u1' for value in result.values()), 'Expected commissioned ZFS 2.3.9 packages')
    return result


def validate_plan(text):
    installs = {}
    removals = set()
    for line in text.splitlines():
        if line.startswith('Inst '):
            match = re.match(r'Inst ([a-z0-9+.-]+)(?::amd64)?(?: \[[^\]]+\])? \(([^ ]+) ', line)
            require(match is not None, 'Unrecognized APT installation plan')
            installs[match[1]] = match[2]
        elif line.startswith('Remv '):
            removals.add(line.split()[1].split(':')[0])
    require(installs == dict.fromkeys(PACKAGES, TARGET) and removals <= {'libzfs6linux'},
            'APT changes extend beyond the reviewed ZFS package transition')


def save(data):
    target = STATE / 'state.json'
    temporary = STATE / 'state.json.new'
    require(not temporary.exists() and not temporary.is_symlink(), 'Unexpected upgrade state temporary file')
    with temporary.open('x') as stream:
        json.dump(data, stream, sort_keys=True)
        stream.write('\n')
        stream.flush()
        os.fsync(stream.fileno())
    temporary.replace(target)


def read_state():
    protected(STATE.parent, directory=True)
    protected(STATE, directory=True)
    protected(STATE / 'state.json')
    return json.loads((STATE / 'state.json').read_text())


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('operation', choices=['preflight', 'prepare', 'plan', 'staged', 'verify'])
    parser.add_argument('--pool', required=True)
    parser.add_argument('--guid', required=True)
    args = parser.parse_args()
    require(os.geteuid() == 0 and socket.gethostname().split('.')[0] == 'microserver', 'Expected owner on the commissioned NAS')
    require(re.fullmatch(r'[A-Za-z][A-Za-z0-9_.-]{0,63}', args.pool) and re.fullmatch(r'[0-9]+', args.guid), 'Invalid pool identity')
    os.umask(0o077)
    enabled = inspect_pool(args.pool, args.guid)
    mounted = mounts(args.pool)
    nfs = nfs_state()
    kernel = os.uname().release
    require(kernel == '6.12.115+deb13.1-amd64', 'Kernel changed since qualification; review the target')
    run(['/usr/bin/systemctl', 'is-active', *SERVICES])
    if args.operation == 'preflight':
        require(not STATE.exists() and not STATE.is_symlink(), 'An existing upgrade needs verification or recovery, not reapplication')
        protected(STATE.parent, directory=True)
        for path in (Path('/etc/apt/sources.list.d/microserver-zfs-backports.list'),
                     Path('/etc/apt/preferences.d/microserver-zfs')):
            protected(path.parent, directory=True)
            require(not path.exists() and not path.is_symlink(), 'An existing ZFS APT policy requires review')
        require(run(['/usr/bin/findmnt', '-n', '-o', 'FSTYPE', '/']) == 'xfs', 'Expected the commissioned XFS root filesystem')
        require(run(['/usr/bin/findmnt', '-n', '-o', 'FSTYPE', '/boot']) == 'ext4', 'Expected the commissioned ext4 boot filesystem')
        require(Path('/lib/modules', kernel, 'build').resolve().is_dir(), 'Current kernel headers are missing')
        require(shutil.disk_usage('/').free >= 2 * 1024**3 and shutil.disk_usage('/boot').free >= 128 * 1024**2, 'Insufficient package or boot space')
        require(run(['/usr/bin/cat', '/sys/module/zfs/version']).startswith('2.3.9-'), 'Expected the commissioned running module')
        signing_key = run(['/usr/sbin/modinfo', '-F', 'sig_key', 'zfs'])
        require(not secure_boot() or bool(signing_key), 'Secure Boot signing requires review')
        print(json.dumps([name + '=' + version for name, version in packages().items()]))
    elif args.operation == 'prepare':
        require(not STATE.exists() and not STATE.is_symlink(), 'Upgrade state already exists')
        installed = packages()
        STATE.mkdir(mode=0o700)
        (STATE / 'debs').mkdir(mode=0o700)
        archives = []
        for name, version in installed.items():
            found = []
            for archive in Path('/var/cache/apt/archives').glob(name + '_*.deb'):
                protected(archive)
                fields = run(['/usr/bin/dpkg-deb', '-f', str(archive), 'Package', 'Version', 'Architecture'])
                if 'Package: ' + name + '\n' in fields + '\n' and 'Version: ' + version + '\n' in fields + '\n':
                    found.append(archive)
            require(len(found) == 1, 'The exact signed rollback package was not retained')
            target = STATE / 'debs' / found[0].name
            shutil.copyfile(found[0], target)
            archives.append({'name': target.name, 'sha256': hashlib.sha256(target.read_bytes()).hexdigest()})
        with (STATE / 'debs/SHA256SUMS').open('x') as stream:
            stream.write(''.join(item['sha256'] + '  ' + item['name'] + '\n' for item in archives))
        save(dict(pool=args.pool, guid=args.guid, kernel=kernel, boot=Path('/proc/sys/kernel/random/boot_id').read_text().strip(),
                  enabled=enabled, mounts=mounted, nfs=nfs, packages=installed, archives=archives, secure_boot=secure_boot(),
                  signing_key=run(['/usr/sbin/modinfo', '-F', 'sig_key', 'zfs']), phase='prepared'))
        print('ZFS_ROLLBACK_PACKAGES=retained; POOL_FEATURES=recorded')
    else:
        data = read_state()
        require((data['pool'], data['guid'], data['kernel']) == (args.pool, args.guid, kernel), 'Upgrade scope differs')
        require(data['enabled'] == enabled, 'Enabled pool feature set changed; retain recovery state')
        require(all(mounted.get(name) == path for name, path in data['mounts'].items()), 'A retained NAS dataset is no longer mounted at its original path')
        require(data['nfs'] != 'active' or nfs == 'active', 'The previously active NFS service is unavailable')
        if args.operation == 'plan':
            validate_plan(run(['/usr/bin/apt-get', '-s', '--no-install-recommends', 'install',
                               *[name + '=' + TARGET for name in PACKAGES]], timeout=120))
            print('ZFS_PACKAGE_PLAN=seven reviewed packages; only old libzfs ABI removal allowed')
        else:
            installed = run(['/usr/bin/dpkg-query', '-W', '-f=${binary:Package}\t${Version}\t${db:Status-Status}\n', *PACKAGES])
            expected = {name: TARGET for name in PACKAGES}
            actual = {}
            for row in installed.splitlines():
                name, version, status_value = row.split('\t')
                require(status_value == 'installed', 'A target ZFS package is not fully configured')
                actual[name.split(':')[0]] = version
            require(actual == expected, 'Installed ZFS packages do not match the reviewed target')
            require(run(['/usr/sbin/modinfo', '-F', 'version', 'zfs']) == TARGET, 'New on-disk ZFS module does not match the target')
            dkms = run(['/usr/sbin/dkms', 'status', '-m', 'zfs', '-v', MODULE])
            require(any(kernel in line and line.endswith(': installed') for line in dkms.splitlines()), 'New DKMS module is not installed for this kernel')
            require(secure_boot() == data['secure_boot'], 'Secure Boot state changed')
            require(not data['secure_boot'] or run(['/usr/sbin/modinfo', '-F', 'sig_key', 'zfs']) == data['signing_key'],
                    'New module signing key differs; review MOK enrollment before reboot')
            if args.operation == 'staged':
                require(data['phase'] == 'prepared', 'Upgrade was already staged')
                data['phase'] = 'staged'
                save(data)
                print('ZFS_PACKAGES=2.4.4; DKMS=installed; OWNER_REBOOT=required; POOL_FEATURES=unchanged')
            else:
                require(data['phase'] in ('staged', 'confirmed'), 'Complete package staging before verification')
                require(data['boot'] != Path('/proc/sys/kernel/random/boot_id').read_text().strip(), 'Owner reboot has not occurred')
                require(run(['/usr/bin/cat', '/sys/module/zfs/version']) == TARGET, 'Running ZFS module does not match the new packages')
                run(['/usr/bin/python3', '/usr/local/libexec/microserver-ui-tls.py', 'probe'])
                data['phase'] = 'confirmed'
                save(data)
                print('ZFS_UPGRADE=verified after reboot; NAS_SERVICES=active; NFS=' + nfs + '; HTTPS=verified; POOL_FEATURES=unchanged')


if __name__ == '__main__':
    try:
        main()
    except (RuntimeError, OSError, ValueError, KeyError, subprocess.SubprocessError) as error:
        print('STOP: ' + (str(error) if isinstance(error, RuntimeError) else type(error).__name__), file=sys.stderr)
        sys.exit(1)
