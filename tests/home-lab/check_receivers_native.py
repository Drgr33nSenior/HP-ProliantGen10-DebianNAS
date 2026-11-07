"""Real Nginx/Tempo in owned local Docker namespaces, with synthetic data only."""
import json
import os
from pathlib import Path
import re
import subprocess
import tempfile
import time
import unittest
import uuid

import check_microserver as fixtures


@unittest.skipUnless(os.environ.get('MICROSERVER_NATIVE_RECEIVERS') == 'true', 'Optional pre-pulled native receiver fixture')
class NativeReceiverTests(unittest.TestCase):
    def test_private_ingestion_acl_routes_and_trace_readback(self):
        endpoint = subprocess.check_output(['docker', 'context', 'inspect', '--format', '{{.Endpoints.docker.Host}}'], text=True).strip()
        self.assertTrue(endpoint.startswith('unix://'), 'Fixture requires a local Docker socket')
        docker = ['docker', '--host', endpoint]
        nginx = os.environ['MICROSERVER_NGINX_TEST_IMAGE']
        self.assertRegex(nginx, r'^docker[.]io/library/nginx:[0-9.]+@sha256:[a-f0-9]{64}$')
        tempo = next(row['image'] for row in json.loads((fixtures.ROOT / 'observability/versions.json').read_text())['images']
                     if row['component'] == 'tempo')
        prometheus = next(row['image'] for row in json.loads((fixtures.ROOT / 'observability/versions.json').read_text())['images']
                          if row['component'] == 'prometheus')
        owned = []

        def start(arguments):
            result = subprocess.check_output(docker + ['run', '--detach', '--pull', 'never'] + arguments, text=True, timeout=30).strip()
            self.assertRegex(result, r'^[a-f0-9]{64}$')
            owned.append(result)
            return result

        def request(namespace, path, *, method='GET', body=None, source='127.0.0.1', headers=(), port=8080,
                    content_type='application/json'):
            command = docker + ['run', '--rm', '--pull', 'never', '--interactive', '--network', 'container:' + namespace,
                                '--read-only', '--cap-drop', 'ALL', '--security-opt', 'no-new-privileges',
                                '--memory', '64m', '--pids-limit', '16', '--entrypoint', '/usr/bin/curl', nginx,
                                '--noproxy', '*', '--silent', '--show-error', '--connect-timeout', '2', '--max-time', '5',
                                '--interface', source, '--request', method, '--write-out', '\n%{http_code}']
            for header in headers:
                command += ['--header', header]
            if body is not None:
                command += ['--header', 'Content-Type: ' + content_type, '--data-binary', '@-']
            result = subprocess.run(command + [f'http://127.0.0.1:{port}{path}'], input=body or '',
                                    capture_output=True, text=True, timeout=15)
            data, _, code = result.stdout.rpartition('\n')
            return int(code) if code.isdigit() else 0, data

        try:
            with tempfile.TemporaryDirectory(prefix='microserver-receivers.') as work:
                base = fixtures.MicroServerTests()
                base.setUp()
                context = dict(base.context, microserver_telemetry_enabled=True,
                               microserver_telemetry_client_address='127.0.0.1', microserver_ui_enabled=True,
                               microserver_ui_address='127.0.0.1', microserver_ui_domain='lan.example.test',
                               microserver_ui_allowed_networks=['127.0.0.1/32'])
                config = str(fixtures.ENV.from_string((fixtures.ROLE / 'templates/ui-nginx.conf.j2').read_text()).render(**context))
                config = config.replace('127.0.0.1:80 ', '127.0.0.1:18080 ').replace('127.0.0.1:80;', '127.0.0.1:18080;')
                # Distinct fixture statuses verify the exact forwarded paths.
                stubs = '''
    server { listen 127.0.0.1:3100;
        location = /loki/api/v1/push { return 202; }
        location = /otlp/v1/logs { return 204; }
        location = /metrics { return 206; }
    }
    server { listen 127.0.0.1:3000; location = /metrics { return 207; } }
'''
                before, closing = config.rsplit('\n}', 1)
                config_path = Path(work) / 'nginx.conf'
                config_path.write_text(before + stubs + '\n}' + closing)
                config_path.chmod(0o644)
                namespace = start([
                    '--platform', 'linux/amd64', '--network', 'none', '--read-only', '--user', '10001:10001',
                    '--cap-drop', 'ALL', '--security-opt', 'no-new-privileges', '--memory', '768m', '--cpus', '0.25',
                    '--env', 'GOMEMLIMIT=614MiB',
                    '--pids-limit', '128', '--tmpfs', '/var/tempo:rw,nosuid,nodev,noexec,size=64m,uid=10001,gid=10001,mode=0700',
                    '--tmpfs', '/tmp:rw,nosuid,nodev,noexec,size=16m,mode=1777',
                    '--mount', f'type=bind,source={fixtures.ROOT / "observability/config/tempo.yaml"},target=/config.yaml,readonly',
                    tempo, '-config.file=/config.yaml'])
                prom_config = Path(work) / 'prometheus.yaml'
                prom_config.write_text('global: {scrape_interval: 30s}\nscrape_configs: []\n')
                prom_config.chmod(0o644)
                start([
                    '--platform', 'linux/amd64', '--network', 'container:' + namespace, '--read-only', '--user', '65534:65534',
                    '--cap-drop', 'ALL', '--security-opt', 'no-new-privileges', '--memory', '256m', '--cpus', '0.25',
                    '--pids-limit', '64', '--tmpfs', '/prometheus:rw,nosuid,nodev,noexec,size=64m,uid=65534,gid=65534,mode=0700',
                    '--mount', f'type=bind,source={prom_config},target=/fixture.yaml,readonly',
                    prometheus, '--config.file=/fixture.yaml', '--web.listen-address=127.0.0.1:9090',
                    '--web.enable-remote-write-receiver', '--storage.tsdb.path=/prometheus'])
                start([
                    '--network', 'container:' + namespace, '--read-only', '--user', '33:33', '--cap-drop', 'ALL',
                    '--security-opt', 'no-new-privileges', '--memory', '128m', '--cpus', '0.25', '--pids-limit', '32',
                    '--tmpfs', '/run/microserver-ui:rw,nosuid,nodev,noexec,size=16m,uid=33,gid=33,mode=0750',
                    '--mount', f'type=bind,source={config_path},target=/fixture.conf,readonly',
                    '--entrypoint', '/usr/sbin/nginx', nginx, '-e', 'stderr', '-c', '/fixture.conf',
                    '-g', 'daemon off; pid /run/microserver-ui/nginx.pid;'])
                for _ in range(20):
                    if request(namespace, '/ready')[0] == 200 and request(namespace, '/metrics/prometheus')[0] == 200:
                        break
                    time.sleep(0.5)
                else:
                    self.fail('Native Tempo/proxy did not become ready')
                self.assertEqual(request(namespace, '/ready', source='127.0.0.2')[0], 403)
                self.assertEqual(request(namespace, '/ready', source='127.0.0.2', headers=['X-Forwarded-For: 127.0.0.1'])[0], 403)
                # Raw Snappy for an empty protobuf WriteRequest: validates the
                # protocol endpoint without manufacturing production series.
                self.assertEqual(request(namespace, '/api/v1/write', method='POST', body='\x00',
                                         content_type='application/x-protobuf', headers=[
                                             'Content-Encoding: snappy', 'X-Prometheus-Remote-Write-Version: 0.1.0'])[0], 204)
                self.assertEqual(request(namespace, '/api/v1/write')[0], 403)
                self.assertEqual(request(namespace, '/loki/api/v1/push', method='POST', body='{}')[0], 202)
                self.assertEqual(request(namespace, '/otlp/v1/logs', method='POST', body='{}')[0], 204)
                self.assertEqual(request(namespace, '/admin', method='POST', body='{}')[0], 404)
                self.assertEqual(request(namespace, '/metrics/prometheus')[0], 200)
                self.assertEqual(request(namespace, '/metrics/loki')[0], 206)
                self.assertEqual(request(namespace, '/metrics/tempo')[0], 200)
                self.assertEqual(request(namespace, '/metrics/grafana')[0], 207)
                self.assertEqual(request(namespace, '/metrics/tempo', method='POST', body='{}')[0], 403)
                trace_id = uuid.uuid4().hex
                now = time.time_ns()
                payload = {'resourceSpans': [{'resource': {'attributes': [
                    {'key': 'service.name', 'value': {'stringValue': 'microserver-cutover-fixture'}}]},
                    'scopeSpans': [{'spans': [{'traceId': trace_id, 'spanId': uuid.uuid4().hex[:16],
                        'name': 'receiver-fixture', 'startTimeUnixNano': str(now), 'endTimeUnixNano': str(now + 1000000)}]}]}]}
                self.assertEqual(request(namespace, '/v1/traces', method='POST', body=json.dumps(payload))[0], 200)
                for _ in range(15):
                    code, result = request(namespace, '/api/traces/' + trace_id,
                                           headers=['Accept: application/json'], port=3200)
                    if code == 200 and 'receiver-fixture' in result:
                        break
                    time.sleep(0.5)
                else:
                    self.fail('The synthetic trace could not be read back')
        finally:
            for container in reversed(owned):
                subprocess.run(docker + ['rm', '--force', container], capture_output=True, timeout=20, check=True)


if __name__ == '__main__':
    unittest.main()
