"""Private HTTPS source, credential boundaries, validation and isolated nginx."""
import json
import os
from pathlib import Path
import shlex
import shutil
import stat
import subprocess
import tempfile
from types import SimpleNamespace
import unittest
from unittest import mock

import yaml
import check_ui
from check_microserver import ENV, ROLE, ROOT, assertions, read_yaml

tls = check_ui.load('microserver-ui-tls')
guard = check_ui.load('microserver-ui-compose')


class TLSTests(unittest.TestCase):
    def setUp(self):
        base = check_ui.IntranetUITests()
        base.setUp()
        self.context = dict(base.context, microserver_ui_tls_enabled=True)
        self.scope = {'address': '192.168.30.10', 'domain': 'lan.example.test'}

    def render(self, name, **changes):
        return str(ENV.from_string((ROLE / 'templates' / name).read_text()).render(**dict(self.context, **changes)))

    def test_https_changes_only_managed_urls_and_adds_secure_grafana_cookies(self):
        before = yaml.safe_load(self.render('compose.yaml.j2', microserver_ui_tls_enabled=False))
        after = yaml.safe_load(self.render('compose.yaml.j2'))
        guard.require_ui_only(before, after)
        self.assertEqual(after['services']['grafana']['environment']['GF_SERVER_ROOT_URL'], 'https://grafana.lan.example.test/')
        self.assertEqual(after['services']['grafana']['environment']['GF_SECURITY_COOKIE_SECURE'], 'true')
        after['services']['grafana']['environment']['GF_SECURITY_COOKIE_SECURE'] = 'false'
        with self.assertRaisesRegex(ValueError, 'secure cookies'):
            guard.require_ui_only(before, after)

    def test_ingestion_scope_and_transport_are_preserved(self):
        changes = {'microserver_telemetry_enabled': True, 'microserver_telemetry_client_address': '192.168.30.11'}
        before = self.render('ui-nginx.conf.j2', microserver_ui_tls_enabled=False, **changes)
        after = self.render('ui-nginx.conf.j2', **changes)
        section = lambda text: text.split('# BEGIN workstation ingestion')[1].split('# END workstation ingestion')[0]
        self.assertEqual(section(before), section(after))
        self.assertIn('proxy_set_header X-Forwarded-Proto $scheme;', after)
        self.assertIn('ssl_reject_handshake on;', after)
        self.assertIn('if ($ui_redirect_allowed = 0) { return 403; }', after)

    def test_full_apply_preserves_commissioned_https_and_requires_scoped_commissioning(self):
        task = read_yaml(ROLE / 'tasks/ui-tls-preserve.yml')[1]
        context = dict(self.context, microserver_existing_tls={'stat': {'exists': True, 'isreg': True, 'islnk': False, 'uid': 0}})
        self.assertTrue(assertions(task, context))
        self.assertFalse(assertions(task, dict(context, microserver_ui_tls_enabled=False)))
        self.assertFalse(assertions(task, dict(context, microserver_existing_tls={'stat': {'exists': False}})))

    def test_scope_rejects_public_addresses_and_unsafe_dns(self):
        tls.validate_scope(self.scope)
        for changes in [{'address': '0.0.0.0'}, {'address': '127.0.0.1'}, {'address': '203.0.113.4'},
                        {'domain': 'lan.example.test;include bad'}, {'domain': '*.example.test'}, {'domain': 'LAN.example.test'}]:
            with self.subTest(changes=changes), self.assertRaises((RuntimeError, ValueError, TypeError)):
                tls.validate_scope(dict(self.scope, **changes))

    def test_private_keys_require_root_owner_regular_file_and_private_permissions(self):
        path = mock.Mock()
        path.lstat.return_value = SimpleNamespace(st_uid=0, st_mode=stat.S_IFREG | 0o600)
        tls.protected(path, private=True)
        for owner, mode in [(1000, stat.S_IFREG | 0o600), (0, stat.S_IFREG | 0o644), (0, stat.S_IFLNK | 0o600)]:
            path.lstat.return_value = SimpleNamespace(st_uid=owner, st_mode=mode)
            with self.assertRaises(RuntimeError):
                tls.protected(path, private=True)

    def test_certificate_links_cannot_leave_the_expected_archive(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            live, archive = root / 'live/lineage', root / 'archive/lineage'
            live.mkdir(parents=True)
            archive.mkdir(parents=True)
            (archive / 'privkey1.pem').write_text('DUMMY_FIXTURE_ONLY')
            (live / 'privkey.pem').symlink_to(archive / 'privkey1.pem')
            with mock.patch.object(tls, 'LIVE', live), mock.patch.object(tls, 'ARCHIVE', archive), \
                    mock.patch.object(tls, 'protected', side_effect=lambda path, **kwargs: path):
                self.assertEqual(tls.certificate_member('privkey', private=True), archive / 'privkey1.pem')
                (live / 'privkey.pem').unlink()
                (live / 'privkey.pem').symlink_to(root / 'outside.pem')
                (root / 'outside.pem').write_text('DUMMY_FIXTURE_ONLY')
                with self.assertRaises(RuntimeError):
                    tls.certificate_member('privkey', private=True)

    def test_certificate_checks_use_trusted_hostname_verification_and_compare_public_keys(self):
        with mock.patch.object(tls, 'certificate_member', side_effect=lambda name, **kw: Path('/DUMMY/' + name)), \
                mock.patch.object(tls, 'run', return_value=b'DUMMY_PUBLIC_SPKI') as run:
            tls.verify_certificate(self.scope)
        commands = [call.args[0] for call in run.call_args_list]
        checked = [args[args.index('-verify_hostname') + 1] for args in commands if '-verify_hostname' in args]
        self.assertEqual(checked, [name + '.lan.example.test' for name in tls.NAMES])
        self.assertTrue(any('-CApath' in args for args in commands))
        self.assertTrue(any('-checkend' in args and '604800' in args for args in commands))
        self.assertTrue(any('-passin' in args and 'pass:' in args and '-pubout' in args for args in commands))

    def test_mismatched_key_pair_is_refused_without_printing_material(self):
        def run(arguments, *args):
            return b'DUMMY_OTHER_PUBLIC_KEY' if '-in' in arguments and arguments[arguments.index('-in') + 1].endswith('/privkey') else b'DUMMY_PUBLIC_KEY'
        with mock.patch.object(tls, 'certificate_member', side_effect=lambda name, **kw: Path('/DUMMY/' + name)), mock.patch.object(tls, 'run', side_effect=run):
            with self.assertRaisesRegex(RuntimeError, 'do not match'):
                tls.verify_certificate(self.scope)

    @unittest.skipUnless(shutil.which('openssl'), 'Requires local OpenSSL for disposable certificate fixtures')
    def test_native_certificate_key_pairing_accepts_unencrypted_keys_and_rejects_mismatches_and_encryption(self):
        openssl = shutil.which('openssl')
        real_run = tls.run
        with tempfile.TemporaryDirectory(prefix='DUMMY-certificate-pairing.') as directory:
            root = Path(directory)
            other = root / 'dummy-other-key.pem'
            real_run([openssl, 'genpkey', '-algorithm', 'EC', '-pkeyopt', 'ec_paramgen_curve:prime256v1', '-out', str(other)])
            for algorithm, options in [('EC', ['-newkey', 'ec', '-pkeyopt', 'ec_paramgen_curve:prime256v1']),
                                       ('RSA', ['-newkey', 'rsa:2048'])]:
                with self.subTest(algorithm=algorithm):
                    key, cert, encrypted = [root / (algorithm + suffix) for suffix in ['-key.pem', '-cert.pem', '-encrypted.pem']]
                    real_run([openssl, 'req', '-x509', *options, '-nodes', '-keyout', str(key), '-out', str(cert),
                              '-subj', '/CN=DUMMY TLS FIXTURE', '-addext', 'subjectAltName=DNS:*.lan.example.test', '-days', '30'])
                    real_run([openssl, 'pkey', '-in', str(key), '-aes-256-cbc', '-passout', 'stdin', '-out', str(encrypted)],
                             b'DUMMY_FIXTURE_ONLY\n')
                    members = {'cert': cert, 'fullchain': cert, 'privkey': key}

                    def fixture_run(arguments, data=None, timeout=15):
                        arguments = [openssl, *arguments[1:]]
                        if arguments[1] == 'verify':
                            # Trust only this disposable certificate; execute the real
                            # hostname, expiry and public-key checks unchanged.
                            index = arguments.index('-CApath')
                            arguments[index:index + 2] = ['-CAfile', str(cert)]
                        return real_run(arguments, data, timeout)

                    with mock.patch.object(tls, 'certificate_member', side_effect=lambda name, **kwargs: members[name]), \
                            mock.patch.object(tls, 'run', side_effect=fixture_run):
                        tls.verify_certificate(self.scope)
                        members['privkey'] = other
                        with self.assertRaisesRegex(RuntimeError, 'do not match'):
                            tls.verify_certificate(self.scope)
                        members['privkey'] = encrypted
                        with self.assertRaisesRegex(RuntimeError, 'openssl pkey failed'):
                            tls.verify_certificate(self.scope)

    def test_command_failures_and_oversized_output_do_not_expose_payloads(self):
        for status, body in [(1, b'DUMMY_PROTECTED_OUTPUT'), (0, b'x' * 65537)]:
            with mock.patch.object(tls.subprocess, 'run', return_value=subprocess.CompletedProcess([], status, stdout=body, stderr=b'')):
                with self.assertRaises(RuntimeError) as result:
                    tls.run(['/DUMMY/program'])
                self.assertNotIn('DUMMY_PROTECTED_OUTPUT', str(result.exception))

    def test_nginx_preflight_keeps_runtime_pid_safe_and_uses_certbot_paths(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'candidate'
            path.write_text(self.render('ui-nginx.conf.j2'))
            real_temporary = tempfile.TemporaryDirectory
            def check(arguments, **kwargs):
                text = Path(arguments[arguments.index('-c') + 1]).read_text()
                self.assertIn('/etc/letsencrypt/live/microserver-ui/privkey.pem', text)
                self.assertNotIn('/run/credentials/', text)
                self.assertEqual(arguments[-2:], ['-g', 'user www-data; pid /dev/null;'])
                return b''
            with mock.patch.object(tls, 'verify_certificate'), mock.patch.object(tls, 'run', side_effect=check), \
                    mock.patch.object(tls.tempfile, 'TemporaryDirectory', side_effect=lambda **kwargs: real_temporary(dir=directory)):
                tls.check_nginx(path, self.scope)

    def test_probe_requires_redirects_and_keeps_grafana_api_authentication(self):
        def response(scope, name, path, secure=True):
            if not secure:
                return 308, 'https://' + name + '.' + scope['domain'] + '/'
            return (401 if path == '/api/user' else 200), None
        with mock.patch.object(tls, 'request', side_effect=response):
            tls.probe(self.scope)
        with mock.patch.object(tls, 'request', return_value=(200, None)):
            with self.assertRaises(RuntimeError):
                tls.probe(self.scope)

    def test_peer_certificate_must_match_the_current_lineage(self):
        connection = mock.MagicMock()
        connection.getpeercert.return_value = b'DUMMY_OLD_CERTIFICATE'
        context = mock.Mock()
        context.wrap_socket.return_value = connection
        with mock.patch.object(tls.socket, 'create_connection', return_value=connection), \
                mock.patch.object(tls.ssl, 'create_default_context', return_value=context), \
                mock.patch.object(tls, 'certificate_member', return_value=Path('/DUMMY/cert')), \
                mock.patch.object(tls, 'run', return_value=b'DUMMY_NEW_CERTIFICATE'):
            with self.assertRaisesRegex(RuntimeError, 'current Certbot certificate'):
                tls.request(self.scope, 'grafana', '/api/health')
        context.wrap_socket.assert_called_once_with(connection, server_hostname='grafana.lan.example.test')
        connection.close.assert_called_once()

    def test_owner_rollback_contains_only_public_config_and_owned_dropin(self):
        play = read_yaml(ROOT / 'ansible/owner-tls.yml')[0]
        block = next(task for task in play['tasks'] if 'block' in task)
        deletions = [task['ansible.builtin.file']['path'] for task in block['rescue'] if 'ansible.builtin.file' in task]
        self.assertEqual(deletions, ['/etc/systemd/system/microserver-ui.service.d/tls.conf'])
        commands = [task['ansible.builtin.command']['argv'] for task in block['block'] if 'ansible.builtin.command' in task]
        update = next(args for args in commands if args[0] == '/usr/bin/docker-compose')
        self.assertEqual(update[-3:], ['prometheus', 'grafana', 'alertmanager'])
        self.assertIn('--no-deps', update)
        renewal = next(task for task in block['block'] if 'renewal and its real' in task['name'])
        self.assertTrue(renewal['no_log'])
        self.assertIn('--dry-run', renewal['ansible.builtin.command']['argv'])
        self.assertIn('--run-deploy-hooks', renewal['ansible.builtin.command']['argv'])

    def test_owner_preflight_refuses_a_changed_proxy_user_or_privilege_policy(self):
        play = read_yaml(ROOT / 'ansible/owner-tls.yml')[0]
        task = next(item for item in play['pre_tasks'] if item['name'] == 'Preserve the rootless proxy identity and privilege policy')
        good = ['User=www-data', 'Group=www-data', 'NoNewPrivileges=yes']
        self.assertTrue(assertions(task, {'microserver_tls_identity': {'stdout_lines': good}}))
        for replacement in [['User=root'] + good[1:], good[:2] + ['NoNewPrivileges=no']]:
            self.assertFalse(assertions(task, {'microserver_tls_identity': {'stdout_lines': replacement}}))

    def test_renewal_hook_is_lineage_scoped_and_credentials_are_service_local(self):
        hook = (ROLE / 'files/microserver-ui-tls-renew').read_text()
        self.assertIn('"${RENEWED_LINEAGE:-}" = /etc/letsencrypt/live/microserver-ui', hook)
        dropin = (ROLE / 'files/microserver-ui-tls.conf').read_text()
        self.assertIn('LoadCredential=ui-privkey.pem:/etc/letsencrypt/live/microserver-ui/privkey.pem', dropin)
        unit = (ROLE / 'files/microserver-ui.service').read_text()
        self.assertIn('User=www-data', unit)
        self.assertIn('NoNewPrivileges=yes', unit)
        self.assertNotIn('User=root', unit)

    def test_renewal_refreshes_credentials_and_retries_only_listener_startup(self):
        with mock.patch('sys.argv', ['helper', 'renew']), mock.patch.object(tls.os, 'geteuid', return_value=0), \
                mock.patch.object(tls.socket, 'gethostname', return_value='ig0r'), \
                mock.patch.object(tls, 'read_scope', return_value=self.scope), \
                mock.patch.object(tls, 'DROPIN') as dropin, mock.patch.object(tls, 'protected'), \
                mock.patch.object(tls, 'check_nginx'), mock.patch.object(tls, 'run') as run, \
                mock.patch.object(tls, 'probe', side_effect=[ConnectionRefusedError(), None]) as probe, \
                mock.patch.object(tls.time, 'sleep') as sleep, mock.patch('builtins.print'):
            dropin.exists.return_value = True
            tls.main()
        run.assert_called_once_with(['/usr/bin/systemctl', 'restart', 'microserver-ui.service'], timeout=45)
        self.assertEqual(probe.call_count, 2)
        sleep.assert_called_once_with(0.5)

    @unittest.skipUnless(os.environ.get('MICROSERVER_NGINX_TEST_IMAGE'), 'Requires the owned pinned nginx fixture image')
    def test_native_https_private_peer_rules_sni_redirects_and_authentication(self):
        image = os.environ['MICROSERVER_NGINX_TEST_IMAGE']
        self.assertRegex(image, r'^docker[.]io/library/nginx:[0-9.]+@sha256:[a-f0-9]{64}$')
        endpoint = subprocess.check_output(['docker', 'context', 'inspect', '--format', '{{.Endpoints.docker.Host}}'], text=True).strip()
        self.assertTrue(endpoint.startswith('unix://'))
        with tempfile.TemporaryDirectory(prefix='DUMMY-nginx-tls.') as directory:
            path = Path(directory) / 'nginx.conf'
            text = self.render('ui-nginx.conf.j2', microserver_ui_address='127.0.0.1', microserver_ui_allowed_networks=['127.0.0.1/32'])
            text = text.replace('127.0.0.1:80', '127.0.0.1:18080').replace('127.0.0.1:443', '127.0.0.1:18443')
            # Dummy upstreams live only in the owned network namespace.
            stubs = '''
    server {
        listen 127.0.0.1:3000;
        location = /api/user { return 401; }
        location = /headers { return 200 "$http_x_forwarded_proto|$http_x_forwarded_for|$host"; }
        location / { return 200 "DUMMY_GRAFANA"; }
    }
    server { listen 127.0.0.1:9090; return 200 "DUMMY_PROMETHEUS"; }
    server { listen 127.0.0.1:9093; return 200 "DUMMY_ALERTMANAGER"; }
'''
            path.write_text('user www-data;\n' + text.rsplit('}', 1)[0] + stubs + '}\n')
            path.chmod(0o644)
            www = ['setpriv', '--reuid=33', '--regid=33', '--clear-groups']
            cert = '/run/credentials/microserver-ui.service/ui-fullchain.pem'
            key = '/run/credentials/microserver-ui.service/ui-privkey.pem'
            commands = [
                'set -eu',
                shlex.join(www + ['mkdir', '-p', '/run/credentials/microserver-ui.service'] +
                           ['/run/microserver-ui/' + name for name in ['client-body', 'proxy', 'fastcgi', 'uwsgi', 'scgi']]),
                shlex.join(www + ['openssl', 'req', '-x509', '-newkey', 'ec', '-pkeyopt', 'ec_paramgen_curve:prime256v1', '-nodes',
                                 '-keyout', key, '-out', cert, '-subj', '/CN=DUMMY TLS FIXTURE',
                                 '-addext', 'subjectAltName=DNS:*.lan.example.test', '-days', '1']) + ' >/dev/null 2>&1',
                "nginx -e stderr -t -c /fixture.conf -g 'pid /dev/null;'",
                'test ! -e /run/microserver-ui/nginx.pid',
                shlex.join(www + ['nginx', '-e', 'stderr', '-c', '/fixture.conf', '-g', 'daemon off; pid /run/microserver-ui/nginx.pid;']) + ' &',
                'main=$!',
                "stop_nginx() { setpriv --reuid=33 --regid=33 --clear-groups /bin/sh -c 'kill -QUIT \"$1\"' signal \"$main\"; }",
                "trap 'stop_nginx 2>/dev/null || true' EXIT",
                'ready=no',
                'for attempt in $(seq 1 30); do if curl --noproxy "*" --silent --max-time 1 --cacert ' + cert +
                ' --resolve grafana.lan.example.test:18443:127.0.0.1 https://grafana.lan.example.test:18443/api/health >/dev/null; then ready=yes; break; fi; sleep 0.1; done',
                '[ "$ready" = yes ]',
                'openssl verify -CAfile ' + cert + ' -verify_hostname grafana.lan.example.test ' + cert + ' >/dev/null',
                'if openssl verify -CAfile ' + cert + ' -verify_hostname grafana.other.example.test ' + cert + ' >/dev/null 2>&1; then exit 1; fi',
                'curl --noproxy "*" --silent --show-error --max-time 3 --cacert ' + cert +
                ' --resolve grafana.lan.example.test:18443:127.0.0.1 https://grafana.lan.example.test:18443/headers | grep -Fx "https|127.0.0.1|grafana.lan.example.test"',
                'test "$(curl --noproxy "*" --silent --max-time 3 --cacert ' + cert +
                ' --resolve grafana.lan.example.test:18443:127.0.0.1 --output /dev/null --write-out "%{http_code}" https://grafana.lan.example.test:18443/api/user)" = 401',
                'test "$(curl --noproxy "*" --silent --max-time 3 -H "Host: grafana.lan.example.test" --output /dev/null --write-out "%{http_code} %{redirect_url}" http://127.0.0.1:18080/)" = "308 https://grafana.lan.example.test/"',
                'test "$(curl --noproxy "*" --silent --max-time 3 --interface 127.0.0.2 -H "Host: grafana.lan.example.test" -H "X-Forwarded-For: 127.0.0.1" --output /dev/null --write-out "%{http_code}" http://127.0.0.1:18080/)" = 403',
                'test "$(curl --noproxy "*" --silent --max-time 3 --interface 127.0.0.2 --cacert ' + cert +
                ' --resolve grafana.lan.example.test:18443:127.0.0.1 -H "X-Forwarded-For: 127.0.0.1" --output /dev/null --write-out "%{http_code}" https://grafana.lan.example.test:18443/)" = 403',
                'if curl --noproxy "*" --silent --max-time 3 --cacert ' + cert +
                ' --resolve unknown.lan.example.test:18443:127.0.0.1 https://unknown.lan.example.test:18443/ >/dev/null 2>&1; then exit 1; fi',
                'test "$(curl --noproxy "*" --silent --max-time 3 --cacert ' + cert +
                ' --resolve grafana.lan.example.test:18443:127.0.0.1 -H "Host: unknown.lan.example.test" --output /dev/null --write-out "%{http_code}" https://grafana.lan.example.test:18443/)" = 404',
                'test "$(stat -c %u /run/microserver-ui/nginx.pid)" = 33',
                'stop_nginx', 'wait "$main"', 'trap - EXIT',
            ]
            result = subprocess.run(['docker', '--host', endpoint, 'run', '--rm', '--pull', 'never', '--network', 'none',
                '--read-only', '--cap-drop', 'ALL', '--cap-add', 'SETUID', '--cap-add', 'SETGID', '--cap-add', 'DAC_OVERRIDE',
                '--security-opt', 'no-new-privileges', '--memory', '128m', '--cpus', '0.5', '--pids-limit', '32',
                '--tmpfs', '/run/microserver-ui:rw,nosuid,nodev,noexec,size=16m,uid=33,gid=33,mode=0750',
                '--tmpfs', '/run/credentials:rw,nosuid,nodev,noexec,size=4m,uid=33,gid=33,mode=0700',
                '--mount', f'type=bind,source={path},target=/fixture.conf,readonly', '--entrypoint', '/bin/sh', image,
                '-ec', '\n'.join(commands)], capture_output=True, text=True, timeout=40)
            self.assertEqual(result.returncode, 0, result.stderr)


if __name__ == '__main__':
    unittest.main()
