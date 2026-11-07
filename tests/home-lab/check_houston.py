"""Loopback receiver, pinned vendor adapter and owner orchestration regressions."""
import copy
from http.client import HTTPConnection
import importlib.util
import json
import os
from pathlib import Path
import socket
import subprocess
import tempfile
import threading
import time
import unittest
import uuid
from unittest import mock
from xml.etree import ElementTree

import yaml
from check_microserver import ENV, ROLE, ROOT, assertions, read_yaml


def load(name):
    spec = importlib.util.spec_from_file_location(name, ROLE / 'files' / (name + '.py'))
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


webhook = load('microserver-houston-webhook')
adapter = load('microserver-houston-dbus')


def combine(base, update, recursive=False):
    output = copy.deepcopy(base)
    for key, value in update.items():
        output[key] = combine(output[key], value, True) if recursive and isinstance(value, dict) and key in output else value
    return output


ENV.filters.update(from_yaml=yaml.safe_load, combine=combine,
                   to_nice_yaml=lambda value, **kw: yaml.safe_dump(value, **kw))


class HoustonTests(unittest.TestCase):
    def payload(self, status='firing'):
        return dict(version='4', status=status, alerts=[dict(status=status,
                    labels=dict(alertname='FixtureAlert', severity='critical'),
                    annotations=dict(summary='fixture summary'), startsAt='2026-11-06T00:00:00Z',
                    endsAt='2026-11-06T00:04:00Z')])

    def test_configuration_preserves_the_existing_grouping_and_adds_only_local_delivery(self):
        template = ENV.from_string((ROLE / 'templates/alertmanager.yaml.j2').read_text())
        render = lambda enabled: yaml.safe_load(str(template.render(role_path=str(ROLE), microserver_houston_alerts_enabled=enabled)))
        baseline = read_yaml(ROLE / 'files/alertmanager.yaml')
        self.assertEqual(render(False), baseline)
        integrated = render(True)
        for key, value in baseline['route'].items():
            if key != 'receiver':
                self.assertEqual(integrated['route'][key], value)
        receiver = integrated['receivers'][-1]
        self.assertEqual(receiver, dict(name='houston-alerts-webhook', webhook_configs=[dict(
            url='http://127.0.0.1:5002/webhook', send_resolved=True, max_alerts=100)]))
        self.assertEqual(integrated['route']['routes'][0]['matchers'], ['alertname="MicroserverHoustonAcceptance"'])

    def test_messages_retain_status_and_map_resolved_severity_to_info(self):
        firing = webhook.messages(self.payload())[0]
        resolved = webhook.messages(self.payload('resolved'))[0]
        self.assertNotEqual(firing[0], resolved[0])
        self.assertEqual(firing[1]['severity'], 'critical')
        self.assertEqual(resolved[1]['severity'], 'info')
        self.assertEqual(json.loads(resolved[1]['event'])['alerts'][0]['status'], 'resolved')
        truncated = webhook.messages(dict(self.payload(), truncatedAlerts=1))
        self.assertEqual(len(truncated), 2)
        self.assertIn('omitted 1 alerts', truncated[1][1]['text'])
        self.assertEqual(truncated[1][1]['severity'], 'warning')
        for value in ({}, dict(version='4', alerts=[]), dict(self.payload(), truncatedAlerts=-1),
                      dict(self.payload(), alerts=self.payload()['alerts'] * 101)):
            with self.assertRaises(ValueError):
                webhook.messages(value)

    def test_native_http_rejects_file_serving_oversize_and_chunked_requests_and_retries_failures(self):
        server = webhook.HTTPServer(('127.0.0.1', 0), webhook.Handler)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        webhook.Handler.seen = {}
        try:
            def request(method, path, body=None, headers=None):
                connection = HTTPConnection(*server.server_address, timeout=3)
                try:
                    connection.request(method, path, body, headers or {})
                    response = connection.getresponse()
                    response.read()
                    return response.status
                finally:
                    connection.close()
            self.assertEqual(request('GET', '/ready'), 200)
            self.assertEqual(request('GET', '/etc/45drives/msmtp_pass'), 404)
            self.assertEqual(request('POST', '/webhook', headers={'Content-Length': str(webhook.MAX_BODY + 1)}), 413)
            self.assertEqual(request('POST', '/webhook', headers={'Transfer-Encoding': 'chunked'}), 400)
            body = json.dumps(self.payload())
            headers = {'Content-Type': 'application/json'}
            with mock.patch.object(webhook, 'deliver', side_effect=RuntimeError('private failure')):
                self.assertEqual(request('POST', '/webhook', body, headers), 503)
            with mock.patch.object(webhook, 'deliver') as delivered:
                self.assertEqual(request('POST', '/webhook', body, headers), 204)
                self.assertEqual(request('POST', '/webhook', body, headers), 204)
                self.assertEqual(delivered.call_count, 1)
        finally:
            server.shutdown()
            server.server_close()
            thread.join(3)

    def test_default_users_cannot_modify_houston_or_its_mail_settings(self):
        source = (ROLE / 'templates/microserver-houston-policy.conf.j2').read_text()
        policy = ElementTree.fromstring(str(ENV.from_string(source).render(microserver_cockpit_admin='nasadmin')))
        self.assertEqual(policy.find("./policy[@context='default']/deny").attrib,
                         {'send_destination': 'org._45drives.Houston'})
        receiver = policy.find("./policy[@user='microserver-alerts']")
        self.assertEqual(len(receiver), 1)
        self.assertEqual(receiver[0].attrib['send_member'], 'ForwardMessage')
        self.assertIn('RestrictAddressFamilies=AF_UNIX', (ROLE / 'files/microserver-houston-dbus.conf').read_text())
        self.assertIn('PrivateNetwork=yes', (ROLE / 'files/microserver-houston-dbus.conf').read_text())
        self.assertEqual(adapter.disabled()['status'], 'disabled')

    def test_vendor_adapter_refuses_changed_code_before_importing_it(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory)
            (path / 'dbus_server.py').write_text('raise AssertionError("must not execute")')
            with mock.patch.object(adapter, 'VENDOR', path), mock.patch.object(adapter, 'HASHES', {'dbus_server.py': '0' * 64}):
                with self.assertRaises(RuntimeError):
                    adapter.safe_paths()

    def test_later_monitoring_render_requires_the_integration_flag(self):
        guard = read_yaml(ROLE / 'tasks/houston-alerts-preserve.yml')[1]
        metadata = {'stat': dict(exists=True, isreg=True, islnk=False, uid=0)}
        self.assertTrue(assertions(guard, dict(microserver_existing_houston=metadata, microserver_houston_alerts_enabled=True)))
        self.assertFalse(assertions(guard, dict(microserver_existing_houston=metadata, microserver_houston_alerts_enabled=False)))

    def test_owner_changes_only_alertmanager_and_restores_the_four_public_files_on_failure(self):
        play = read_yaml(ROOT / 'ansible/owner-houston.yml')[0]
        block = play['tasks'][0]['block']
        apply = next(task for task in block if 'block' in task)
        commands = [task['ansible.builtin.command']['argv'] for task in apply['block'] if 'ansible.builtin.command' in task]
        recreate = next(command for command in commands if command[0] == '/usr/bin/docker-compose')
        self.assertEqual(recreate[-1], 'alertmanager')
        self.assertIn('--no-deps', recreate)
        self.assertIn('--pull', recreate)
        restore = next(task for task in apply['rescue'] if task['name'].startswith('Restore the four'))
        self.assertEqual(restore['loop'], '{{ microserver_houston_public_files }}')
        self.assertEqual(len(play['vars']['microserver_houston_public_files']), 4)

    @unittest.skipUnless(os.environ.get('MICROSERVER_HOUSTON_TEST_IMAGE'), 'Requires the owned native Houston fixture image')
    def test_native_vendor_database_and_unprivileged_receiver_deliver_real_alertmanager_firing_and_resolved(self):
        image = os.environ['MICROSERVER_HOUSTON_TEST_IMAGE']
        self.assertRegex(image, r'^sha256:[a-f0-9]{64}$')
        endpoint = subprocess.check_output(['docker', 'context', 'inspect', '--format', '{{.Endpoints.docker.Host}}'], text=True).strip()
        self.assertTrue(endpoint.startswith('unix://'))
        docker = ['docker', '--host', endpoint]
        name = 'microserver-houston-tst-' + uuid.uuid4().hex
        container = subprocess.check_output(docker + ['run', '--detach', '--pull', 'never', '--platform', 'linux/amd64',
            '--name', name, '--hostname', 'microserver', '--read-only', '--cap-drop', 'ALL',
            '--security-opt', 'no-new-privileges', '--network', 'none', '--memory', '256m', '--pids-limit', '64',
            '--tmpfs', '/run:rw,nosuid,nodev,noexec,size=16m,mode=0755',
            '--tmpfs', '/tmp:rw,nosuid,nodev,noexec,size=16m',
            '--tmpfs', '/var/lib/sqlite:rw,nosuid,nodev,noexec,size=16m,mode=0755', image], text=True).strip()
        try:
            def wait_file(path, seconds):
                deadline = time.monotonic() + seconds
                while time.monotonic() < deadline:
                    result = subprocess.run(docker + ['exec', container, 'test', '-e', path], capture_output=True, timeout=5)
                    if result.returncode == 0:
                        return
                    running = subprocess.check_output(docker + ['inspect', '--format', '{{.State.Running}}', container], text=True).strip()
                    if running != 'true':
                        break
                    time.sleep(1)
                logs = subprocess.run(docker + ['logs', container], capture_output=True, text=True)
                self.fail('Native fixture failed: ' + logs.stdout[-2000:] + logs.stderr[-2000:])
            wait_file('/tmp/bus-ready', 20)
            subprocess.run(docker + ['exec', '--detach', '--user', 'microserver-alerts', container,
                                     '/usr/bin/python3', '/usr/local/libexec/microserver-houston-webhook.py'], check=True, timeout=10)
            wait_file('/tmp/accepted', 85)
            denial = '''import dbus
bus = dbus.SystemBus()
remote = bus.get_object('org._45drives.Houston', '/org/_45drives/Houston', introspect=False)
for member, args in [('GetNotificationCount', ()), ('UpdateSMTPConfig', ('{}',))]:
    try:
        remote.get_dbus_method(member, 'org._45drives.Houston')(*args, timeout=3)
    except dbus.exceptions.DBusException as error:
        assert error.get_dbus_name() == 'org.freedesktop.DBus.Error.AccessDenied'
    else:
        raise AssertionError('Receiver gained unauthorized access')
print('RECEIVER_DBUS_RESTRICTIONS=passed')
'''
            subprocess.run(docker + ['exec', '--user', 'microserver-alerts', container, '/usr/bin/python3', '-c', denial], check=True, timeout=10)
            denied_forward = denial.replace("[('GetNotificationCount', ()), ('UpdateSMTPConfig', ('{}',))]", "[('ForwardMessage', ('{}',))]")
            subprocess.run(docker + ['exec', '--user', 'nobody', container, '/usr/bin/python3', '-c', denied_forward], check=True, timeout=10)
            admin = "import dbus; b=dbus.SystemBus(); o=b.get_object('org._45drives.Houston','/org/_45drives/Houston',introspect=False); assert o.get_dbus_method('GetNotificationCount','org._45drives.Houston')(timeout=3)>=2; print('ADMIN_UI_READ=passed')"
            subprocess.run(docker + ['exec', '--user', 'nasadmin', container, '/usr/bin/python3', '-c', admin], check=True, timeout=10)
            # An unwritable vendor DB must fail delivery so Alertmanager retries.
            database = '/var/lib/sqlite/45drives/notifications.db'
            subprocess.run(docker + ['exec', container, 'chmod', '0400', database], check=True, timeout=5)
            retry = '''import json, urllib.error, urllib.request, uuid
body = {'version':'4','alerts':[{'status':'firing','labels':{'alertname':'RetryFixture','identity':uuid.uuid4().hex},'annotations':{},'startsAt':'2026-11-06T00:00:00Z'}]}
request = urllib.request.Request('http://127.0.0.1:5002/webhook',data=json.dumps(body).encode(),headers={'Content-Type':'application/json'})
try:
    urllib.request.urlopen(request,timeout=5)
except urllib.error.HTTPError as error:
    assert error.code == 503
else:
    raise AssertionError('Failed database write was acknowledged')
print('FAILED_PERSISTENCE_REQUIRES_RETRY=passed')
'''
            try:
                subprocess.run(docker + ['exec', '--user', 'microserver-alerts', container, '/usr/bin/python3', '-c', retry], check=True, timeout=10)
            finally:
                subprocess.run(docker + ['exec', container, 'chmod', '0600', database], check=True, timeout=5)
        finally:
            subprocess.run(docker + ['stop', '--time', '3', container], capture_output=True, timeout=10)
            subprocess.run(docker + ['rm', container], capture_output=True, timeout=10)


if __name__ == '__main__':
    unittest.main()
