#!/usr/bin/env python3
"""Collect bounded Docker statistics for node_exporter's textfile collector."""
import argparse
import http.client
import json
import os
from pathlib import Path
import re
import socket
import sys
import tempfile
import time

OUTPUT = Path('/var/lib/microserver/node-exporter/docker.prom')
MAX_RESPONSE = 1024 * 1024
MAX_CONTAINERS = 32


class CollectionError(Exception):
    """An invalid or unavailable Docker statistics response."""


class DockerConnection(http.client.HTTPConnection):
    def connect(self):
        self.sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        self.sock.settimeout(self.timeout)
        self.sock.connect('/run/docker.sock')


def docker_json(path, deadline):
    remaining = deadline - time.monotonic()
    if remaining <= 0:
        raise CollectionError('Collection deadline exceeded')
    connection = DockerConnection('localhost', timeout=min(2, remaining))
    try:
        connection.request('GET', path)
        response = connection.getresponse()
        if response.status != 200:
            raise CollectionError(f'Docker API returned HTTP {response.status}')
        body = response.read(MAX_RESPONSE + 1)
        if len(body) > MAX_RESPONSE:
            raise CollectionError('Docker response exceeds the collection limit')
        return json.loads(body)
    finally:
        connection.close()


def number(value):
    if type(value) is not int or value < 0:
        raise CollectionError('Invalid numeric statistic')
    return value


def sample(name, value, container=None):
    labels = '' if container is None else '{container="' + container + '"}'
    return f'microserver_docker_{name}{labels} {value}'


def collect():
    deadline = time.monotonic() + 10
    version = docker_json('/version', deadline)['ApiVersion']
    if not isinstance(version, str) or not re.fullmatch(r'1\.[0-9]{1,3}', version):
        raise CollectionError('Invalid Docker API version')
    prefix = '/v' + version
    containers = docker_json(prefix + '/containers/json?all=true', deadline)
    if not isinstance(containers, list) or len(containers) > MAX_CONTAINERS:
        raise CollectionError('Container count exceeds the collection limit')
    lines = [sample('containers', len(containers))]
    seen = set()
    for container in containers:
        identifier = container['Id']
        raw_name = container['Names'][0]
        if not isinstance(raw_name, str):
            raise CollectionError('Invalid container name')
        name = raw_name.removeprefix('/')
        if (not re.fullmatch(r'[a-f0-9]{64}', identifier)
                or not re.fullmatch(r'[a-zA-Z0-9][a-zA-Z0-9_.-]*', name)
                or name in seen):
            raise CollectionError('Invalid or duplicate container identity')
        seen.add(name)
        state = container['State']
        if state not in {'created', 'restarting', 'running', 'removing', 'paused', 'exited', 'dead'}:
            raise CollectionError('Invalid container state')
        running = state == 'running'
        lines.append(sample('container_running', int(running), name))
        if not running:
            continue
        stats = docker_json(prefix + '/containers/' + identifier
                            + '/stats?stream=false&one-shot=true', deadline)
        cpu = number(stats['cpu_stats']['cpu_usage']['total_usage'])
        memory = stats['memory_stats']
        usage = number(memory['usage'])
        limit = number(memory['limit'])
        cache = memory.get('stats', {})
        inactive = number(cache.get('inactive_file', cache.get('total_inactive_file', 0)))
        lines.extend([
            sample('container_cpu_usage_seconds_total', cpu / 1_000_000_000, name),
            sample('container_memory_usage_bytes', usage, name),
            sample('container_memory_working_set_bytes', max(0, usage - inactive), name),
            sample('container_memory_limit_bytes', limit, name),
        ])
        pids = stats.get('pids_stats', {}).get('current')
        if pids is not None:
            lines.append(sample('container_pids', number(pids), name))
    return lines


def render(lines, success, timestamp):
    kinds = {
        'containers': ('gauge', 'Number of containers, including stopped containers.'),
        'container_running': ('gauge', 'One when the container is running; zero otherwise.'),
        'container_cpu_usage_seconds_total': ('counter', 'Cumulative container CPU time in seconds.'),
        'container_memory_usage_bytes': ('gauge', 'Container memory usage including cache in bytes.'),
        'container_memory_working_set_bytes': ('gauge', 'Memory usage minus inactive file cache in bytes.'),
        'container_memory_limit_bytes': ('gauge', 'Container memory limit in bytes.'),
        'container_pids': ('gauge', 'Current number of container tasks.'),
        'collection_success': ('gauge', 'One when the complete collection succeeded; zero otherwise.'),
        'collection_timestamp_seconds': ('gauge', 'Unix time of the latest collection attempt.'),
    }
    headers = []
    for name, (kind, help_text) in kinds.items():
        headers.extend([f'# HELP microserver_docker_{name} {help_text}',
                        f'# TYPE microserver_docker_{name} {kind}'])
    return '\n'.join(headers + lines + [
        sample('collection_success', int(success)),
        sample('collection_timestamp_seconds', timestamp),
    ]) + '\n'


def write_metrics(content):
    # A root-owned directory is provisioned by Ansible. Atomic replacement
    # prevents the exporter from reading a partially written snapshot.
    descriptor, temporary = tempfile.mkstemp(prefix='.docker-', dir=OUTPUT.parent)
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
    parser.add_argument('--stdout', action='store_true', help='Read statistics without writing a file')
    args = parser.parse_args()
    success = True
    try:
        lines = collect()
    except (CollectionError, OSError, ValueError, KeyError, IndexError,
            TypeError, http.client.HTTPException) as error:
        # CollectionError messages are our own fixed diagnostics. Never print
        # Docker response bodies or exception text from JSON/socket libraries.
        detail = str(error) if isinstance(error, CollectionError) else type(error).__name__
        print(f'Docker telemetry collection failed ({detail}).', file=sys.stderr)
        lines, success = [], False
    content = render(lines, success, time.time())
    if args.stdout:
        sys.stdout.write(content)
    else:
        write_metrics(content)
    return 0 if success else 1


if __name__ == '__main__':
    sys.exit(main())
