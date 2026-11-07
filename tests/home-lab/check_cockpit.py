"""Cockpit management scope, native authentication and proxy regression checks."""
import hashlib
import os
from pathlib import Path
import re
import shutil
import subprocess
import tempfile
import unittest
from unittest import mock

import yaml

import check_ui
from check_microserver import ENV, ROOT, ROLE, assertions, read_yaml

tls = check_ui.load('microserver-ui-tls')
ENV.tests['search'] = lambda value, pattern: re.search(pattern, value) is not None


class CockpitTests(unittest.TestCase):
    def setUp(self):
        base = check_ui.IntranetUITests()
        base.setUp()
        self.context = dict(base.context, microserver_ui_tls_enabled=True, microserver_cockpit_enabled=True,
                            microserver_cockpit_admin='nasadmin', microserver_telemetry_enabled=True,
                            microserver_telemetry_client_address='192.168.30.11')

    def render(self, template, **changes):
        return str(ENV.from_string((ROLE / 'templates' / template).read_text()).render(**dict(self.context, **changes)))

    def test_management_uses_a_separate_loopback_port_and_preserves_ingestion(self):
        before = self.render('ui-nginx.conf.j2', microserver_cockpit_enabled=False)
        after = self.render('ui-nginx.conf.j2')
        self.assertEqual(after.count('proxy_pass http://127.0.0.1:9091;'), 1)
        self.assertEqual(after.count('server_name microserver.lan.example.test;'), 2)
        self.assertIn('return 308 https://microserver.lan.example.test$request_uri;', after)
        self.assertEqual(before.split('# BEGIN workstation ingestion')[1], after.split('# BEGIN workstation ingestion')[1])
        self.assertIn('proxy_set_header X-Forwarded-Proto $scheme;', after)
        self.assertIn('proxy_set_header X-Forwarded-For $remote_addr;', after)
        self.assertIn('proxy_set_header Upgrade $http_upgrade;', after)
        socket = (ROLE / 'files/microserver-cockpit-socket.conf').read_text()
        self.assertEqual(re.findall(r'^ListenStream=(.*)$', socket, re.M), ['', '127.0.0.1:9091'])

    def test_cockpit_preserves_native_authentication_and_limits_the_https_origin(self):
        config = self.render('cockpit.conf.j2')
        self.assertIn('Origins=https://microserver.lan.example.test', config)
        self.assertIn('LoginTo=false', config)
        self.assertIn('AllowMultiHost=false', config)
        self.assertIn('IdleTimeout=15', config)
        self.assertNotIn('AllowUnencrypted=true', config)
        tasks = read_yaml(ROLE / 'tasks/cockpit.yml')
        block = next(task['block'] for task in tasks if 'block' in task)
        pam = next(task['ansible.builtin.blockinfile'] for task in block if 'ansible.builtin.blockinfile' in task)
        self.assertEqual(pam['path'], '/etc/pam.d/cockpit')
        self.assertIn('auth requisite pam_listfile.so', pam['block'])
        self.assertIn('onerr=fail', pam['block'])
        self.assertNotIn('pam_permit', pam['block'])

    @unittest.skipUnless(shutil.which('ansible-playbook'), 'Requires the configured local Ansible environment')
    def test_native_ansible_writes_one_pam_allowlist_user_with_a_real_newline(self):
        block = next(task['block'] for task in read_yaml(ROLE / 'tasks/cockpit.yml') if 'block' in task)
        copy = next(task['ansible.builtin.copy'] for task in block
                    if task.get('ansible.builtin.copy', {}).get('dest') == '/etc/cockpit/microserver-users')
        self.assertEqual((copy['owner'], copy['group'], copy['mode']), ('root', 'root', '0644'))
        with tempfile.TemporaryDirectory(prefix='microserver-cockpit-users.') as directory:
            users = Path(directory) / 'users'
            playbook = Path(directory) / 'fixture.yml'
            # Exercise Ansible's real templating and copy path in an owned local
            # tst directory. Production file ownership is checked above.
            fixture = [dict(name='Local tst Cockpit allowlist fixture', hosts='localhost', gather_facts=False,
                            vars=dict(microserver_environment='tst', microserver_cockpit_admin='nasadmin'), tasks=[
                                {'name': 'Write only the fixture allowlist', 'ansible.builtin.copy':
                                 dict(content=copy['content'], dest=str(users), mode='0600')}])]
            playbook.write_text(yaml.safe_dump(fixture))
            result = subprocess.run(['ansible-playbook', '-i', 'localhost,', '--connection', 'local', '--limit', 'localhost', str(playbook)],
                                    capture_output=True, text=True, timeout=30, env=dict(os.environ, ANSIBLE_NOCOLOR='1'))
            self.assertEqual(result.returncode, 0, result.stderr + result.stdout)
            self.assertEqual(users.read_bytes(), b'nasadmin\n')

    def test_later_renders_require_the_commissioned_management_and_tls_flags(self):
        guard = read_yaml(ROLE / 'tasks/ui-tls-preserve.yml')[3]
        context = dict(self.context, microserver_existing_cockpit={'stat': dict(exists=True, isreg=True, islnk=False, uid=0)})
        self.assertTrue(assertions(guard, context))
        for changes in (dict(microserver_cockpit_enabled=False), dict(microserver_ui_tls_enabled=False), dict(microserver_ui_enabled=False)):
            self.assertFalse(assertions(guard, dict(context, **changes)))

    def test_installer_starts_the_selected_administrator_user_bus_before_the_ui(self):
        block = next(task['block'] for task in read_yaml(ROLE / 'tasks/cockpit.yml') if 'block' in task)
        start = next(task for task in block if task['name'].startswith('Refresh the existing user manager'))
        self.assertLess(next(index for index, task in enumerate(block) if task['name'].startswith('Install native Cockpit')), block.index(start))
        self.assertLess(block.index(start), next(index for index, task in enumerate(block) if task['name'] == 'Enable the loopback Cockpit socket'))
        self.assertEqual(start['loop'], ['dbus.socket', 'dbus.service'])
        self.assertEqual(start['ansible.builtin.systemd_service']['scope'], 'user')
        self.assertEqual(start['ansible.builtin.systemd_service']['state'], 'started')
        self.assertEqual(start['become_user'], '{{ microserver_cockpit_admin }}')
        self.assertEqual(start['environment'], {'XDG_RUNTIME_DIR': '/run/user/{{ microserver_cockpit_admin_uid.stdout }}'})
        task = next(task for task in block if task['name'] == 'Require the administrator D-Bus socket')
        context = dict(microserver_cockpit_user_bus={'stat': dict(issock=True, uid=1000)}, microserver_cockpit_admin_uid={'stdout': '1000'})
        self.assertTrue(assertions(task, context))
        self.assertFalse(assertions(task, dict(context, microserver_cockpit_user_bus={'stat': dict(issock=False, uid=1000)})))
        self.assertFalse(assertions(task, dict(context, microserver_cockpit_user_bus={'stat': dict(issock=True, uid=0)})))

    def test_tls_renewal_checks_the_management_name_and_anonymous_session_denial(self):
        scope = dict(address='192.168.30.10', domain='lan.example.test', cockpit=True)
        with mock.patch.object(tls, 'certificate_member', side_effect=lambda name, **kw: Path('/DUMMY/' + name)), \
                mock.patch.object(tls, 'run', return_value=b'DUMMY_PUBLIC_SPKI') as run:
            tls.verify_certificate(scope)
        hosts = [call.args[0][call.args[0].index('-verify_hostname') + 1] for call in run.call_args_list if '-verify_hostname' in call.args[0]]
        self.assertEqual(hosts[-1], 'microserver.lan.example.test')

        def response(scope, name, path, secure=True):
            if not secure:
                return 308, 'https://' + name + '.' + scope['domain'] + '/'
            return (401 if path in ('/api/user', '/cockpit/login') else 200), None

        with mock.patch.object(tls, 'request', side_effect=response):
            tls.probe(scope)
        with mock.patch.object(tls, 'request', side_effect=lambda s, n, p, **kw: (200, None) if p == '/cockpit/login' else response(s, n, p, **kw)):
            with self.assertRaisesRegex(RuntimeError, 'authentication'):
                tls.probe(scope)
        with self.assertRaises(RuntimeError):
            tls.validate_scope(dict(scope, cockpit='true'))

    def test_package_scope_and_notifications_are_controlled_before_installation(self):
        tasks = read_yaml(ROLE / 'tasks/cockpit.yml')
        block = next(task['block'] for task in tasks if 'block' in task)
        names = [task['name'] for task in block]
        self.assertLess(names.index('Keep the additional vendor notification services inactive'),
                        names.index('Install native Cockpit and the pinned ZFS management plugin'))
        install = next(task['ansible.builtin.apt'] for task in block if 'Install native' in task['name'])
        self.assertEqual(install['policy_rc_d'], 101)
        self.assertTrue(install['fail_on_autoremove'])
        self.assertFalse(install['install_recommends'])
        self.assertEqual(install['name'], ['cockpit-zfs=1.3.0-1trixie', 'python3-libzfs=2.4.1-4trixie', 'cockpit-alerts=4.0.25-2trixie'])
        pin = next(task['ansible.builtin.copy']['content'] for task in block if 'Permit only' in task['name'])
        self.assertIn('Package: *\nPin: origin repo.45drives.com\nPin-Priority: -1', pin)
        self.assertEqual(hashlib.sha256((ROLE / 'files/45drives-archive.asc').read_bytes()).hexdigest(),
                         '47d8f87e63048e90dd0565405d5d1378bb2fbf4abe5def2933f7d12c75931939')
        guard = next(task for task in block if task['name'].startswith('Reject package removals'))
        self.assertTrue(assertions(guard, dict(microserver_cockpit_plan={'stdout': 'Inst cockpit-ws (337 Debian [amd64])'})))
        for bad in ('Remv zfsutils-linux [1]', 'Inst network-manager (1 Debian [amd64])', 'Inst zfs-dkms (2.5 Debian [all])'):
            self.assertFalse(assertions(guard, dict(microserver_cockpit_plan={'stdout': bad})))

    def test_failed_qualification_retains_packages_and_restores_public_proxy_files(self):
        block = next(task for task in read_yaml(ROLE / 'tasks/cockpit.yml') if 'block' in task)
        copies = [task['ansible.builtin.copy'] for task in block['rescue'] if 'ansible.builtin.copy' in task]
        self.assertEqual(len(copies), 1)
        self.assertTrue(copies[0]['remote_src'])
        self.assertFalse(any('ansible.builtin.apt' in task or 'ansible.builtin.file' in task for task in block['rescue']))

    @unittest.skipUnless(os.environ.get('MICROSERVER_COCKPIT_TEST_IMAGE'), 'Requires the owned Debian Cockpit fixture image')
    def test_native_cockpit_behind_tls_preserves_authentication_redirects_and_peer_acl(self):
        image = os.environ['MICROSERVER_COCKPIT_TEST_IMAGE']
        self.assertRegex(image, r'^sha256:[a-f0-9]{64}$')
        endpoint = subprocess.check_output(['docker', 'context', 'inspect', '--format', '{{.Endpoints.docker.Host}}'], text=True).strip()
        self.assertTrue(endpoint.startswith('unix://'))
        with tempfile.TemporaryDirectory(prefix='DUMMY-cockpit-tls.') as directory:
            config = Path(directory) / 'nginx.conf'
            source = self.render('ui-nginx.conf.j2', microserver_telemetry_enabled=False,
                                 microserver_ui_address='127.0.0.1', microserver_ui_allowed_networks=['127.0.0.1/32'])
            config.write_text(source.replace('127.0.0.1:80', '127.0.0.1:18080').replace('127.0.0.1:443', '127.0.0.1:18443'))
            config.chmod(0o644)
            script = '''set -eu
mkdir -p /run/credentials/microserver-ui.service /run/microserver-ui/client-body /run/microserver-ui/proxy /run/microserver-ui/fastcgi /run/microserver-ui/uwsgi /run/microserver-ui/scgi
cert=/run/credentials/microserver-ui.service/ui-fullchain.pem
openssl req -x509 -newkey ec -pkeyopt ec_paramgen_curve:prime256v1 -nodes -keyout /run/credentials/microserver-ui.service/ui-privkey.pem -out "$cert" -subj '/CN=DUMMY TLS FIXTURE' -addext 'subjectAltName=DNS:*.lan.example.test' -days 1 >/dev/null 2>&1
/usr/lib/cockpit/cockpit-ws --no-tls --address 127.0.0.1 --port 9091 &
cockpit=$!
nginx -e stderr -t -c /fixture.conf -g 'pid /run/nginx.pid;'
nginx -e stderr -c /fixture.conf -g 'daemon off; pid /run/nginx.pid;' &
proxy=$!
trap 'kill "$proxy" "$cockpit" 2>/dev/null || true' EXIT
sleep 1
url=https://microserver.lan.example.test:18443
for check in /:200 /cockpit/login:401; do
  path=${check%:*}; expected=${check##*:}
  actual=$(curl --noproxy '*' --silent --show-error --max-time 3 --cacert "$cert" --resolve microserver.lan.example.test:18443:127.0.0.1 --output /dev/null --write-out '%{http_code}' "$url$path")
  [ "$actual" = "$expected" ]
done
[ "$(curl --noproxy '*' --silent --max-time 3 -H 'Host: microserver.lan.example.test' --output /dev/null --write-out '%{http_code} %{redirect_url}' http://127.0.0.1:18080/)" = '308 https://microserver.lan.example.test/' ]
[ "$(curl --noproxy '*' --silent --max-time 3 --interface 127.0.0.2 --cacert "$cert" --resolve microserver.lan.example.test:18443:127.0.0.1 -H 'X-Forwarded-For: 127.0.0.1' --output /dev/null --write-out '%{http_code}' "$url/")" = 403 ]
printf 'NATIVE_COCKPIT_TLS=page, authentication, redirect and actual-peer ACL passed\n'
'''
            result = subprocess.run(['docker', '--host', endpoint, 'run', '--rm', '--pull', 'never', '--platform', 'linux/amd64',
                                     '--user', '1000:1000', '--read-only', '--cap-drop', 'ALL', '--security-opt', 'no-new-privileges',
                                     '--network', 'none', '--memory', '128m', '--pids-limit', '64',
                                     '--tmpfs', '/run:rw,nosuid,nodev,noexec,size=16m,uid=1000,gid=1000,mode=0750',
                                     '--tmpfs', '/tmp:rw,nosuid,nodev,noexec,size=16m',
                                     '--mount', f'type=bind,source={config},target=/fixture.conf,readonly',
                                     '--entrypoint', '/bin/sh', image, '-ec', script], capture_output=True, text=True, timeout=30)
            self.assertEqual(result.returncode, 0, result.stderr)


if __name__ == '__main__':
    unittest.main()
