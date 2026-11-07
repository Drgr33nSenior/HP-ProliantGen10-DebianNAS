#!/usr/bin/env python3
"""Read one ZFS pool and publish a bounded, atomic node exporter snapshot."""
import argparse
import json
import os
from pathlib import Path
import re
import subprocess
import sys
import tempfile
import time

OUTPUT = Path('/var/lib/microserver/node-exporter/zfs.prom')
MAX_RESPONSE = 1048576
MAX_VDEVS = 64
METRICS = {
    'pool_healthy': ('gauge', 'One when the selected pool is ONLINE.'),
    'pool_size_bytes': ('gauge', 'ZFS pool size in bytes.'),
    'pool_allocated_bytes': ('gauge', 'ZFS pool allocated space in bytes.'),
    'pool_free_bytes': ('gauge', 'ZFS pool free space in bytes.'),
    'pool_capacity_ratio': ('gauge', 'Allocated pool space divided by total pool space.'),
    'pool_fragmentation_ratio': ('gauge', 'ZFS pool fragmentation as a ratio.'),
    'pool_data_errors': ('gauge', 'Current ZFS pool data error count.'),
    'vdev_healthy': ('gauge', 'One when the vdev is ONLINE, HEALTHY or an available spare.'),
    'vdev_read_errors_total': ('counter', 'Accumulated ZFS vdev read errors.'),
    'vdev_write_errors_total': ('counter', 'Accumulated ZFS vdev write errors.'),
    'vdev_checksum_errors_total': ('counter', 'Accumulated ZFS vdev checksum errors.'),
    'scan_present': ('gauge', 'One when ZFS reports a scan record.'),
    'scan_info': ('gauge', 'Latest ZFS scan function and state.'),
    'scan_start_timestamp_seconds': ('gauge', 'Latest reported scan start time as Unix seconds.'),
    'scan_end_timestamp_seconds': ('gauge', 'Latest reported scan end time as Unix seconds; zero while unfinished.'),
    'scan_examined_bytes': ('gauge', 'Bytes examined in the latest reported scan.'),
    'scan_total_bytes': ('gauge', 'Bytes to examine in the latest reported scan.'),
    'scan_errors': ('gauge', 'Errors in the latest reported scan.'),
    'collection_success': ('gauge', 'One when the complete pool collection succeeded.'),
    'collection_timestamp_seconds': ('gauge', 'Unix time of the latest ZFS collection attempt.'),
}


def number(value):
    if type(value) is not int or value < 0:
        raise ValueError('Invalid ZFS number')
    return value


def command(arguments):
    result = subprocess.run(['/usr/sbin/zpool'] + arguments, check=True, timeout=6,
                            stdout=subprocess.PIPE, stderr=subprocess.DEVNULL)
    if len(result.stdout) > MAX_RESPONSE:
        raise ValueError('ZFS response exceeds the collection bound')
    return result.stdout.decode('utf-8')


def sample(name, value, labels=None):
    suffix = '' if not labels else '{' + ','.join(key + '=' + json.dumps(str(value))
                                                for key, value in sorted(labels.items())) + '}'
    return 'microserver_zfs_' + name + suffix + ' ' + str(value)


def collect(pool):
    if not re.fullmatch(r'[A-Za-z][A-Za-z0-9_.-]{0,63}', pool):
        raise ValueError('Invalid pool name')
    columns = command(['list', '-Hp', '-o', 'name,size,alloc,free,cap,frag,health', pool]).strip().split('\t')
    if len(columns) != 7 or columns[0] != pool:
        raise ValueError('Unexpected pool list response')
    size, allocated, free, capacity, fragmentation = [number(int(value)) for value in columns[1:6]]
    if not size or allocated > size or free > size or capacity > 100 or fragmentation > 100:
        raise ValueError('Invalid pool capacity')
    payload = json.loads(command(['status', '-j', '-p', '--json-int', pool]))
    if payload['output_version']['vers_major'] != 0 or set(payload['pools']) != {pool}:
        raise ValueError('Unsupported ZFS status format or pool identity')
    status = payload['pools'][pool]
    if status['name'] != pool or status['state'] != columns[6]:
        raise ValueError('Pool changed during collection')
    labels = {'pool': pool}
    lines = [sample('pool_healthy', int(status['state'] == 'ONLINE'), labels),
             sample('pool_size_bytes', size, labels), sample('pool_allocated_bytes', allocated, labels),
             sample('pool_free_bytes', free, labels), sample('pool_capacity_ratio', allocated / size, labels),
             sample('pool_fragmentation_ratio', fragmentation / 100, labels),
             sample('pool_data_errors', number(status['error_count']), labels)]
    seen = set()

    def vdevs(children, depth=0):
        if not isinstance(children, dict) or depth > 8:
            raise ValueError('Invalid vdev hierarchy')
        for item in children.values():
            guid = str(item['guid'])
            if not re.fullmatch(r'[0-9]{1,20}', guid) or guid in seen or len(seen) >= MAX_VDEVS:
                raise ValueError('Invalid, duplicate or excessive vdev identity')
            seen.add(guid)
            identity = dict(labels, vdev_guid=guid)
            lines.append(sample('vdev_healthy', int(item['state'] in {'ONLINE', 'HEALTHY', 'AVAIL'}), identity))
            for field in ['read_errors', 'write_errors', 'checksum_errors']:
                lines.append(sample('vdev_' + field + '_total', number(item[field]), identity))
            vdevs(item.get('vdevs', {}), depth + 1)

    vdevs(status['vdevs'])
    if not seen:
        raise ValueError('Pool has no vdev statistics')
    scan = status.get('scan_stats')
    lines.append(sample('scan_present', int(bool(scan)), labels))
    if scan:
        if scan['function'] not in {'NONE', 'SCRUB', 'RESILVER', 'ERRORSCRUB'} or scan['state'] not in {
                'NONE', 'SCANNING', 'FINISHED', 'CANCELED', 'ERRORSCRUBBING'}:
            raise ValueError('Unsupported scan state')
        identity = dict(labels, function=scan['function'], state=scan['state'])
        lines.append(sample('scan_info', 1, identity))
        for field, metric in [('start_time', 'start_timestamp_seconds'), ('end_time', 'end_timestamp_seconds'),
                              ('examined', 'examined_bytes'), ('to_examine', 'total_bytes'), ('errors', 'errors')]:
            lines.append(sample('scan_' + metric, number(scan[field]), identity))
    return lines


def render(lines, success, timestamp):
    headers = []
    for name, (kind, description) in METRICS.items():
        headers += ['# HELP microserver_zfs_' + name + ' ' + description,
                    '# TYPE microserver_zfs_' + name + ' ' + kind]
    return '\n'.join(headers + lines + [sample('collection_success', int(success)),
                              sample('collection_timestamp_seconds', timestamp)]) + '\n'


def write_metrics(content):
    descriptor, temporary = tempfile.mkstemp(prefix='.zfs-', dir=OUTPUT.parent)
    try:
        with os.fdopen(descriptor, 'w', encoding='utf-8') as output:
            os.fchmod(output.fileno(), 0o644)
            output.write(content)
        os.replace(temporary, OUTPUT)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--pool', required=True)
    parser.add_argument('--stdout', action='store_true', help='Read metrics without writing a file')
    args = parser.parse_args()
    success = True
    try:
        lines = collect(args.pool)
    except (OSError, ValueError, KeyError, TypeError, subprocess.SubprocessError) as error:
        # Never print zpool response bodies, device paths or subprocess arguments.
        print('ZFS metric collection failed (' + type(error).__name__ + ').', file=sys.stderr)
        lines, success = [], False
    content = render(lines, success, time.time())
    if args.stdout:
        sys.stdout.write(content)
    else:
        write_metrics(content)
    return 0 if success else 1


if __name__ == '__main__':
    sys.exit(main())
