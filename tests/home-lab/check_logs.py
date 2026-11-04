"""Validate log configuration and exercise an isolated loopback log pipeline."""
import errno
import json
import os
from pathlib import Path
import shutil
import socket
import subprocess
import tempfile
import time
import unittest
import urllib.error
import urllib.parse
import urllib.request

import yaml

from check_microserver import ENV, ROOT, ROLE, read_yaml

LOKI = shutil.which('loki')
VECTOR = shutil.which('vector')
HTTP = urllib.request.build_opener(urllib.request.ProxyHandler({}))


def vector_config(enabled, gateway='192.168.50.1', listener='192.168.50.10'):
    context = dict(microserver_unifi_syslog_enabled=enabled,
                   microserver_unifi_gateway_ip=gateway,
                   microserver_unifi_syslog_listen_ip=listener)
    return yaml.safe_load(ENV.from_string((ROLE / 'templates/vector.yaml.j2').read_text()).render(**context))


def unused_port(kind=socket.SOCK_STREAM):
    with socket.socket(socket.AF_INET, kind) as listener:
        listener.bind(('127.0.0.1', 0))
        return listener.getsockname()[1]


def response(url):
    if urllib.parse.urlsplit(url).hostname != '127.0.0.1':
        raise AssertionError('Fixture requests must remain on loopback')
    with HTTP.open(url, timeout=2) as result:
        return result.read()


@unittest.skipUnless(LOKI and VECTOR, 'Pinned Loki and Vector validation binaries are required')
class LogPipelineTests(unittest.TestCase):
    def run_checked(self, command):
        result = subprocess.run(command, capture_output=True, text=True, timeout=45)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)

    def test_native_configuration_validation(self):
        self.run_checked([LOKI, '-verify-config=true', '-config.file=' + str(ROLE / 'files/loki.yaml')])
        with tempfile.TemporaryDirectory() as directory:
            for enabled in [False, True]:
                path = Path(directory) / f'vector-{enabled}.yaml'
                path.write_text(yaml.safe_dump(vector_config(enabled)))
                self.run_checked([VECTOR, 'validate', '--no-environment', str(path)])

    def test_real_cef_parser_and_sender_filter(self):
        with tempfile.TemporaryDirectory() as directory:
            config = vector_config(True)
            config.update(read_yaml(ROOT / 'tests/fixtures/microserver/unifi-vector-tests.yaml'))
            path = Path(directory) / 'vector.yaml'
            path.write_text(yaml.safe_dump(config))
            self.run_checked([VECTOR, 'test', str(path)])

    def wait_http(self, url, process):
        deadline = time.monotonic() + 45
        while time.monotonic() < deadline:
            self.assertIsNone(process.poll(), 'Fixture process exited before readiness')
            try:
                return response(url)
            except (OSError, urllib.error.URLError):
                time.sleep(0.1)
        self.fail('Fixture HTTP readiness timed out')

    def wait_udp(self, port, process):
        deadline = time.monotonic() + 15
        while time.monotonic() < deadline:
            self.assertIsNone(process.poll(), 'Vector exited before binding UDP')
            with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as probe:
                try:
                    probe.bind(('127.0.0.1', port))
                except OSError as error:
                    if error.errno == errno.EADDRINUSE:
                        return
                    raise
            time.sleep(0.05)
        self.fail('Fixture UDP listener did not bind')

    def test_udp_cef_reaches_loki_and_dashboard_queries(self):
        # Use an existing unprivileged tmpfs on Linux when it has enough space.
        # Loki's disk-pressure protection remains enabled in every fixture.
        parent = None
        if (os.path.isdir('/dev/shm') and os.access('/dev/shm', os.W_OK)
                and shutil.disk_usage('/dev/shm').free > 1024 ** 3):
            parent = '/dev/shm'
        with tempfile.TemporaryDirectory(dir=parent) as directory:
            work = Path(directory)
            usage = shutil.disk_usage(work)
            self.assertLess(usage.used / usage.total, 0.9,
                            'The fixture filesystem is over 90% used; Loki protects its WAL. '
                            'Choose TMPDIR on a filesystem with space or run the full CI suite.')
            loki = read_yaml(ROLE / 'files/loki.yaml')
            http_port, grpc_port = unused_port(), unused_port()
            while grpc_port == http_port:
                grpc_port = unused_port()
            loki['server'].update(http_listen_port=http_port, grpc_listen_port=grpc_port)
            loki['common']['path_prefix'] = str(work / 'loki')
            loki['common']['storage']['filesystem'] = dict(
                chunks_directory=str(work / 'loki/chunks'), rules_directory=str(work / 'loki/rules'))
            loki['ingester']['wal']['dir'] = str(work / 'loki/wal')
            loki['storage_config']['tsdb_shipper'] = dict(
                active_index_directory=str(work / 'loki/index'), cache_location=str(work / 'loki/index-cache'))
            loki['compactor']['working_directory'] = str(work / 'loki/compactor')
            loki_path = work / 'loki.yaml'
            loki_path.write_text(yaml.safe_dump(loki))
            vector = vector_config(True, gateway='127.0.0.1', listener='127.0.0.1')
            udp_port = unused_port(socket.SOCK_DGRAM)
            metrics_port = unused_port()
            while metrics_port in [http_port, grpc_port]:
                metrics_port = unused_port()
            vector['sources']['unifi']['address'] = f'127.0.0.1:{udp_port}'
            vector['sinks']['metrics']['address'] = f'127.0.0.1:{metrics_port}'
            vector['sinks']['loki']['endpoint'] = f'http://127.0.0.1:{http_port}'
            vector['data_dir'] = str(work / 'vector')
            (work / 'vector').mkdir()
            vector_path = work / 'vector.yaml'
            vector_path.write_text(yaml.safe_dump(vector))
            processes = []
            with (work / 'processes.log').open('w+') as log:
                try:
                    processes.append(subprocess.Popen(
                        [LOKI, '-config.file=' + str(loki_path)], stdout=log, stderr=log))
                    self.wait_http(f'http://127.0.0.1:{http_port}/ready', processes[0])
                    environment = dict(os.environ, VECTOR_LOG='warn')
                    processes.append(subprocess.Popen(
                        [VECTOR, '--threads', '1', '--config', str(vector_path)],
                        stdout=log, stderr=log, env=environment))
                    self.wait_http(f'http://127.0.0.1:{metrics_port}/metrics', processes[1])
                    self.wait_udp(udp_port, processes[1])
                    messages = [
                        'CEF:0|Ubiquiti|UniFi Network|10.0.0|400|Synthetic gateway event|5|msg=fixture',
                        '<134>Nov 4 12:00:00 gateway CEF:0|Ubiquiti|UniFi OS|5.0.0|101|Synthetic system event|3|msg=fixture',
                        'Synthetic plain syslog event',
                    ]
                    start = time.time_ns() - 60_000_000_000
                    with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as sender:
                        for message in messages:
                            sender.sendto(message.encode(), ('127.0.0.1', udp_port))
                    deadline = time.monotonic() + 20
                    entries = []
                    while time.monotonic() < deadline:
                        params = urllib.parse.urlencode(dict(query='{job="unifi"}', start=start,
                                                             end=time.time_ns(), limit=100))
                        result = json.loads(response(f'http://127.0.0.1:{http_port}/loki/api/v1/query_range?' + params))
                        entries = [json.loads(value) for stream in result['data']['result']
                                   for _, value in stream.get('values', [])]
                        if len(entries) >= 3:
                            break
                        time.sleep(0.1)
                    self.assertEqual(len(entries), 3)
                    self.assertEqual({entry['message'] for entry in entries}, set(messages))
                    self.assertEqual(sum(entry['cef_parsed'] for entry in entries), 2)
                    self.assertEqual({entry['severity'] for entry in entries}, {'3', '5', 'unknown'})
                    for stream in result['data']['result']:
                        self.assertLessEqual(set(stream['stream']), {'job', 'product', 'severity', 'service_name'})
                    dashboard = json.loads((ROLE / 'files/unifi-logs-dashboard.json').read_text())
                    for panel in dashboard['panels']:
                        for target in panel['targets']:
                            params = urllib.parse.urlencode(dict(query=target['expr'], start=start,
                                                                 end=time.time_ns(), step=1, limit=100))
                            queried = json.loads(response(f'http://127.0.0.1:{http_port}/loki/api/v1/query_range?' + params))
                            self.assertEqual(queried['status'], 'success')
                            self.assertTrue(queried['data']['result'], panel['title'])
                except Exception:
                    log.flush()
                    log.seek(0)
                    print(log.read()[-12000:])
                    raise
                finally:
                    for process in reversed(processes):
                        if process.poll() is None:
                            process.terminate()
                        try:
                            process.wait(timeout=8)
                        except subprocess.TimeoutExpired:
                            process.kill()
                            process.wait(timeout=5)


if __name__ == '__main__':
    unittest.main()
