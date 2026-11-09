"""Offline intranet UI scope, rendering and monitoring-drift checks."""
import copy
import importlib.util
import ipaddress
import os
import re
import shlex
import subprocess
import tempfile
import unittest
from pathlib import Path

import yaml

import check_microserver as microserver
from check_microserver import ENV, ROLE, ROOT, assertions, read_yaml


def load(name):
    spec = importlib.util.spec_from_file_location(name, ROLE / 'files' / f'{name}.py')
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


validate = load('microserver-ui-validate').validate
require_ui_only = load('microserver-ui-compose').require_ui_only


class IntranetUITests(unittest.TestCase):
    def setUp(self):
        baseline = microserver.MicroServerTests()
        baseline.setUp()
        self.context = dict(baseline.context,
                            microserver_ui_enabled=True, microserver_monitoring_enabled=True,
                            microserver_ui_address='192.168.30.10', microserver_ui_domain='lan.example.test',
                            microserver_ui_allowed_networks=['192.168.2.0/24', '192.168.30.0/24', '192.168.4.0/24'])
        self.scope = dict(address=self.context['microserver_ui_address'], domain=self.context['microserver_ui_domain'],
                          networks=self.context['microserver_ui_allowed_networks'])
        self.tasks = read_yaml(ROLE / 'tasks/ui.yml')

    def render(self, name, **changes):
        return str(ENV.from_string((ROLE / 'templates' / name).read_text()).render(**dict(self.context, **changes)))

    def test_scope_requires_canonical_private_addresses_and_safe_dns(self):
        validate(self.scope)
        for field, values in {
            'address': ['0.0.0.0', '127.0.0.1', '203.0.113.2', '192.168.30.10:80', '192.168.30.10\n', 3232243210],
            'domain': ['lan.example.test; include /etc/passwd', 'lan.example.test\n', 'LAN.example.test', '', '192.168.30.10'],
            'networks': [[], ['0.0.0.0/0'], ['192.168.0.0/8'], ['10.0.0.0/8'], ['192.168.30.1/24'],
                         ['203.0.113.0/24'], ['192.168.30.0/24; allow all'], ['192.168.2.0/24']],
        }.items():
            for value in values:
                with self.subTest(field=field, value=value), self.assertRaises((ValueError, TypeError)):
                    validate(dict(self.scope, **{field: value}))

    def test_proxy_binds_only_the_private_address_and_restricts_actual_peer(self):
        text = self.render('ui-nginx.conf.j2')
        self.assertEqual(re.findall(r'listen ([^;]+);', text),
                         ['192.168.30.10:80 default_server'] + ['192.168.30.10:80'] * 3)
        self.assertEqual(re.findall(r'proxy_pass ([^;]+);', text),
                         ['http://127.0.0.1:3000', 'http://127.0.0.1:9090', 'http://127.0.0.1:9093'])
        networks = [ipaddress.IPv4Network(value) for value in re.findall(r'^    allow ([^;]+);', text, re.M)]
        for address in ['192.168.2.184', '192.168.30.11', '192.168.4.2']:
            self.assertTrue(any(ipaddress.IPv4Address(address) in network for network in networks))
        for address in ['192.168.20.2', '192.168.3.2', '192.168.1.39', '203.0.113.1']:
            self.assertFalse(any(ipaddress.IPv4Address(address) in network for network in networks))
        self.assertIn('deny all;', text)
        self.assertIn('proxy_set_header X-Forwarded-For $remote_addr;', text)
        self.assertNotIn('real_ip_header', text)
        self.assertIn('return 404;', text)
        self.assertIn('proxy_set_header Upgrade $http_upgrade;', text)
        self.assertNotIn('$request_uri', text)
        for name in ['grafana', 'prometheus', 'alertmanager']:
            self.assertIn(f'server_name {name}.lan.example.test;', text)

    def test_compose_preserves_backend_listeners_and_grafana_authentication(self):
        enabled = yaml.safe_load(self.render('compose.yaml.j2'))
        disabled = yaml.safe_load(self.render('compose.yaml.j2', microserver_ui_enabled=False))
        require_ui_only(disabled, enabled)
        require_ui_only(enabled, disabled)
        services = enabled['services']
        self.assertEqual(services['grafana']['environment']['GF_SERVER_HTTP_ADDR'], '127.0.0.1')
        self.assertEqual(services['grafana']['environment']['GF_AUTH_ANONYMOUS_ENABLED'], 'false')
        self.assertEqual(services['grafana']['environment']['GF_USERS_ALLOW_SIGN_UP'], 'false')
        self.assertEqual(services['grafana']['environment']['GF_SERVER_ROOT_URL'], 'http://grafana.lan.example.test/')
        for name, port in [('prometheus', 9090), ('alertmanager', 9093)]:
            self.assertIn(f'--web.listen-address=127.0.0.1:{port}', services[name]['command'])
            self.assertIn(f'--web.external-url=http://{name}.lan.example.test/', services[name]['command'])
        self.assertNotIn('GF_SERVER_ROOT_URL', disabled['services']['grafana']['environment'])

    def test_ui_apply_rejects_unrelated_monitoring_drift(self):
        baseline = yaml.safe_load(self.render('compose.yaml.j2', microserver_ui_enabled=False))
        proposed = yaml.safe_load(self.render('compose.yaml.j2'))
        mutations = [lambda config: config['services']['grafana']['environment'].update(GF_AUTH_ANONYMOUS_ENABLED='true'),
                     lambda config: config['services']['grafana'].update(image='unexpected-image'),
                     lambda config: config['services']['prometheus']['command'].append('--web.enable-admin-api'),
                     lambda config: config['services']['vector']['command'].append('--unexpected-option')]
        for mutation in mutations:
            changed = copy.deepcopy(proposed)
            mutation(changed)
            with self.assertRaisesRegex(ValueError, 'outside UI'):
                require_ui_only(baseline, changed)
        self.assertNotIn('GF_SERVER_ROOT_URL', baseline['services']['grafana']['environment'])

    def test_activation_guards_and_package_install_do_not_take_over_other_nginx(self):
        task = next(task for task in self.tasks if task['name'].startswith('Require active monitoring'))
        context = dict(self.context, ansible_facts={'all_ipv4_addresses': ['192.168.30.10']})
        self.assertTrue(assertions(task, context))
        self.assertFalse(assertions(task, dict(context, microserver_monitoring_enabled=False)))
        self.assertFalse(assertions(task, dict(context, microserver_ui_address='192.168.30.20')))
        takeover = next(task for task in self.tasks if task['name'].startswith('Refuse to take over'))
        self.assertFalse(assertions(takeover, dict(microserver_distribution_nginx={'rc': 0})))
        self.assertTrue(assertions(takeover, dict(microserver_distribution_nginx={'rc': 3})))
        package = next(task['ansible.builtin.apt'] for task in self.tasks if 'ansible.builtin.apt' in task)
        self.assertEqual(package['name'], 'nginx')
        self.assertEqual(package['policy_rc_d'], 101)
        self.assertFalse(package['install_recommends'])
        unused = next(task['ansible.builtin.systemd_service'] for task in self.tasks
                      if task.get('ansible.builtin.systemd_service', {}).get('name') == 'nginx.service')
        self.assertEqual(unused, dict(name='nginx.service', enabled=False, masked=True, state='stopped'))

    def test_managed_paths_reject_untrusted_files_and_wrong_types(self):
        task = next(task for task in self.tasks if task['name'] == 'Require protected intranet proxy paths')
        good = dict(item='/etc/microserver/ui/nginx.conf', stat=dict(exists=True, islnk=False, isreg=True, isdir=False, uid=0, mode='0644'))
        self.assertTrue(assertions(task, dict(item=good)))
        for changes in [dict(islnk=True), dict(uid=1000), dict(mode='0664'), dict(isreg=False, isdir=True)]:
            bad = copy.deepcopy(good)
            bad['stat'].update(changes)
            self.assertFalse(assertions(task, dict(item=bad)))

    def test_owner_entry_point_requires_explicit_intent_and_only_imports_ui(self):
        play = read_yaml(ROOT / 'ansible/owner-ui.yml')[0]
        guard = play['pre_tasks'][0]
        context = dict(microserver_owner_install=True, ansible_play_hosts_all=['nas'],
                       ansible_facts=dict(distribution='Debian', distribution_major_version='13', architecture='x86_64'))
        self.assertTrue(assertions(guard, context))
        self.assertFalse(assertions(guard, dict(context, microserver_owner_install=False)))
        self.assertFalse(assertions(guard, dict(context, ansible_play_hosts_all=['nas', 'other'])))
        self.assertEqual(play['tasks'][0]['ansible.builtin.import_role']['tasks_from'], 'ui')

    @unittest.skipUnless(os.environ.get('MICROSERVER_COCKPIT_TEST_IMAGE'), 'Requires the owned Debian nginx fixture image')
    def test_native_root_validation_preserves_temp_ownership_and_complete_large_proxy_transfers(self):
        image = os.environ['MICROSERVER_COCKPIT_TEST_IMAGE']
        self.assertRegex(image, r'^sha256:[a-f0-9]{64}$')
        endpoint = subprocess.check_output(['docker', 'context', 'inspect', '--format', '{{.Endpoints.docker.Host}}'], text=True).strip()
        self.assertTrue(endpoint.startswith('unix://'))
        with tempfile.TemporaryDirectory(prefix='microserver-proxy-large.') as directory:
            config = Path(directory) / 'nginx.conf'
            config.write_text(self.render('ui-nginx.conf.j2', microserver_ui_address='127.0.0.1',
                                         microserver_ui_allowed_networks=['127.0.0.1/32']).replace('127.0.0.1:80', '127.0.0.1:18080'))
            config.chmod(0o644)
            template = next(task['ansible.builtin.template'] for task in self.tasks
                            if task.get('ansible.builtin.template', {}).get('src') == 'ui-nginx.conf.j2')
            root_check = shlex.join(shlex.split(template['validate'].replace('%s', '/fixture.conf')))
            old_check = root_check.replace('user www-data; ', '')
            script = '''set -eu
as_www() { setpriv --reuid=33 --regid=33 --clear-groups "$@"; }
as_www mkdir -p /run/microserver-ui/client-body /run/microserver-ui/proxy /run/microserver-ui/fastcgi /run/microserver-ui/uwsgi /run/microserver-ui/scgi
dd if=/dev/zero of=/tmp/payload.bin bs=1048576 count=4 status=none
setpriv --reuid=33 --regid=33 --clear-groups /usr/local/bin/python3 -m http.server 3000 --bind 127.0.0.1 --directory /tmp >/tmp/backend.log 2>&1 &
backend=$!
cleanup() { as_www /bin/sh -c 'kill -QUIT "$1" 2>/dev/null || true; kill "$2" 2>/dev/null || true' signal "$main" "$backend"; }
''' + old_check + '''
test "$(stat -c %u /run/microserver-ui/proxy)" = 65534
setpriv --reuid=33 --regid=33 --clear-groups nginx -e stderr -c /fixture.conf -g 'daemon off; pid /run/microserver-ui/nginx.pid;' >/tmp/nginx.log 2>&1 &
main=$!
trap cleanup EXIT
ready=no
for attempt in $(seq 1 40); do
  if curl --noproxy '*' --fail --silent --max-time 1 --output /dev/null http://127.0.0.1:3000/payload.bin; then ready=yes; break; fi
  sleep 0.1
done
test "$ready" = yes
# A slow client forces a large upstream response to use the proxy temp path.
failed=no
if curl --noproxy '*' --fail --silent --show-error --max-time 15 --limit-rate 1m -H 'Host: grafana.lan.example.test' --output /tmp/before.bin http://127.0.0.1:18080/payload.bin; then failed=no; else failed=yes; fi
test "$failed" = yes
grep -q 'Permission denied' /tmp/nginx.log
''' + root_check + '''
for path in /run/microserver-ui/client-body /run/microserver-ui/proxy /run/microserver-ui/fastcgi /run/microserver-ui/uwsgi /run/microserver-ui/scgi; do test "$(stat -c %u "$path")" = 33; done
# Repair must take effect in the running worker, without a restart or reload.
curl --noproxy '*' --fail --silent --show-error --max-time 15 --limit-rate 1m -H 'Host: grafana.lan.example.test' --output /tmp/after.bin http://127.0.0.1:18080/payload.bin
test "$(wc -c </tmp/after.bin)" = 4194304
cmp /tmp/payload.bin /tmp/after.bin
test "$(cat /run/microserver-ui/nginx.pid)" = "$main"
printf 'NATIVE_PROXY_LARGE_TRANSFER=passed; live ownership repair; full 4 MiB checksum-equivalent readback\n'
cleanup
wait "$main"
trap - EXIT
'''
            result = subprocess.run(['docker', '--host', endpoint, 'run', '--rm', '--pull', 'never', '--platform', 'linux/amd64',
                '--read-only', '--cap-drop', 'ALL', '--cap-add', 'CHOWN', '--cap-add', 'SETUID', '--cap-add', 'SETGID',
                '--cap-add', 'DAC_OVERRIDE', '--security-opt', 'no-new-privileges', '--network', 'none',
                '--memory', '128m', '--pids-limit', '48',
                '--tmpfs', '/run/microserver-ui:rw,nosuid,nodev,noexec,size=16m,uid=33,gid=33,mode=0750',
                '--tmpfs', '/tmp:rw,nosuid,nodev,noexec,size=24m,mode=1777',
                '--mount', f'type=bind,source={config},target=/fixture.conf,readonly',
                '--entrypoint', '/bin/sh', image, '-ec', script], capture_output=True, text=True, timeout=40)
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)

    @unittest.skipUnless(os.environ.get('MICROSERVER_NGINX_TEST_IMAGE'), 'Optional Nginx fixture requires a pre-pulled, digest-pinned test image')
    def test_native_nginx_starts_after_root_validation_without_pid_ownership_conflict(self):
        image = os.environ['MICROSERVER_NGINX_TEST_IMAGE']
        self.assertRegex(image, r'^docker[.]io/library/nginx:[0-9.]+@sha256:[a-f0-9]{64}$')
        endpoint = subprocess.check_output(['docker', 'context', 'inspect', '--format', '{{.Endpoints.docker.Host}}'], text=True).strip()
        self.assertTrue(endpoint.startswith('unix://'), 'Native fixture requires a local Docker socket')
        with tempfile.TemporaryDirectory(prefix='microserver-nginx.') as work:
            config = Path(work) / 'nginx.conf'
            # The isolated namespace has no NAS interface. Use an owned loopback
            # endpoint above privileged ports while checking the same config.
            text = self.render('ui-nginx.conf.j2', microserver_ui_address='127.0.0.1')
            # Exercise the validator's explicit identity; adding a user directive
            # to the fixture would hide the real Debian default-user regression.
            config.write_text(text.replace('127.0.0.1:80', '127.0.0.1:18080'))
            config.chmod(0o644)
            template = next(task['ansible.builtin.template'] for task in self.tasks
                            if task.get('ansible.builtin.template', {}).get('src') == 'ui-nginx.conf.j2')
            root_check = shlex.split(template['validate'].replace('%s', '/fixture.conf'))
            unit = (ROLE / 'files/microserver-ui.service').read_text()
            commands = dict(line.split('=', 1) for line in unit.splitlines() if line.startswith(('ExecStart=', 'ExecStartPre=')))
            preflight = shlex.split(commands['ExecStartPre'].replace('/etc/microserver/ui/nginx.conf', '/fixture.conf'))
            start = shlex.split(commands['ExecStart'].replace('/etc/microserver/ui/nginx.conf', '/fixture.conf'))
            as_www = ['setpriv', '--reuid=33', '--regid=33', '--clear-groups']
            serve = shlex.join(start) + ''' &
main=$!
trap 'kill -QUIT "$main" 2>/dev/null || true' EXIT
ready=no
for attempt in $(seq 1 30); do
    if [ "$(curl --noproxy '*' --silent --max-time 1 --output /dev/null --write-out '%{http_code}' http://127.0.0.1:18080/)" = 404 ]; then
        ready=yes
        break
    fi
    sleep 0.1
done
[ "$ready" = yes ]
kill -QUIT "$main"
wait "$main"
trap - EXIT
'''
            runtime_dirs = ['/run/microserver-ui/' + name for name in ['client-body', 'proxy', 'fastcgi', 'uwsgi', 'scgi']]
            script = '\n'.join([
                'set -eu', shlex.join(as_www + ['mkdir', '-p'] + runtime_dirs),
                shlex.join(root_check), 'test ! -e /run/microserver-ui/nginx.pid',
                'for path in /run/microserver-ui/*; do test "$(stat -c %u "$path")" = 33; done',
                shlex.join(as_www + preflight),
                'test "$(stat -c %u /run/microserver-ui/nginx.pid)" = 33',
                shlex.join(as_www + ['timeout', '15s', '/bin/sh', '-ec', serve]),
            ])
            result = subprocess.run([
                'docker', '--host', endpoint, 'run', '--rm', '--pull', 'never', '--network', 'none',
                '--read-only', '--cap-drop', 'ALL', '--cap-add', 'SETUID', '--cap-add', 'SETGID',
                '--cap-add', 'DAC_OVERRIDE', '--security-opt', 'no-new-privileges',
                '--memory', '128m', '--cpus', '0.25', '--pids-limit', '32',
                '--tmpfs', '/run/microserver-ui:rw,nosuid,nodev,noexec,size=16m,uid=33,gid=33,mode=0750',
                '--mount', f'type=bind,source={config},target=/fixture.conf,readonly',
                '--entrypoint', '/bin/sh', image, '-ec', script],
                capture_output=True, text=True, timeout=30)
            self.assertEqual(result.returncode, 0, result.stderr)


if __name__ == '__main__':
    unittest.main()
