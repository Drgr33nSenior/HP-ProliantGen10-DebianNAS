"""Offline checks for the Kubernetes NFS export and the Zot registry component."""
import json
import re
import subprocess
import sys
import unittest

import yaml

import check_microserver as microserver
from check_microserver import ENV, ROLE, ROOT, assertions, read_yaml


def task(tasks, name):
    matches = [item for item in tasks if item.get('name') == name]
    if len(matches) != 1:
        raise AssertionError('expected exactly one task named ' + name)
    return matches[0]


class PropertyReadTests(unittest.TestCase):
    def test_property_lists_are_one_argument(self):
        # YAML flow sequences split unquoted commas into separate arguments.
        reads = 0
        for source in (ROLE / 'tasks').glob('*.yml'):
            for item in read_yaml(source):
                argv = item.get('ansible.builtin.command', {}).get('argv', [])
                if argv[:2] in [['zfs', 'get'], ['zpool', 'get']]:
                    reads += 1
                    with self.subTest(source=source.name, task=item['name']):
                        self.assertEqual(argv[2:5], ['-H', '-o', 'value'])
                        self.assertEqual(len(argv), 7, argv)
        self.assertGreaterEqual(reads, 5)


class KubernetesStorageTests(unittest.TestCase):
    def setUp(self):
        baseline = microserver.MicroServerTests()
        baseline.setUp()
        self.context = dict(baseline.context, microserver_k8s_storage_enabled=True,
                            microserver_k8s_nfs_client='192.168.30.11')
        self.tasks = read_yaml(ROLE / 'tasks/k8s-storage.yml')

    def render(self, name, **changes):
        return str(ENV.from_string((ROLE / 'templates' / name).read_text()).render(**dict(self.context, **changes)))

    def test_export_is_one_client_all_squash_and_synchronous(self):
        lines = [line for line in self.render('k8s-exports.j2').splitlines() if line and not line.startswith('#')]
        self.assertEqual(lines, ['/srv/microserver/k8s 192.168.30.11(rw,sync,all_squash,anonuid=2000,'
                                 'anongid=2000,no_subtree_check,mountpoint,sec=sys)'])

    def test_scope_accepts_one_canonical_private_client_only(self):
        scope = task(self.tasks, 'Require the Kubernetes NFS scope')
        self.assertTrue(assertions(scope, self.context))
        for field, value in [('microserver_k8s_nfs_client', '192.168.30.0/24'),
                             ('microserver_k8s_nfs_client', '203.0.113.5'),
                             ('microserver_k8s_nfs_client', '192.168.030.11'),
                             ('microserver_k8s_nfs_client', '*'),
                             ('microserver_k8s_nfs_uid', 0),
                             ('microserver_k8s_nfs_uid', 65534),
                             ('microserver_k8s_quota', 'unlimited'),
                             ('microserver_k8s_quota', '0T')]:
            with self.subTest(field=field, value=value):
                self.assertFalse(assertions(scope, dict(self.context, **{field: value})))

    def test_dataset_is_created_only_when_missing_and_never_over_files(self):
        create = task(self.tasks, 'Create only the missing Kubernetes dataset')
        self.assertEqual(create['when'], 'microserver_k8s_dataset.rc == 1')
        argv = create['ansible.builtin.command']['argv']
        self.assertEqual(argv[:2], ['zfs', 'create'])
        self.assertIn('mountpoint=/srv/microserver/k8s', argv)
        self.assertIn('quota={{ microserver_k8s_quota }}', argv)
        refuse = task(self.tasks, 'Refuse to mount a new dataset over an existing directory')
        self.assertIn('microserver_k8s_dataset.rc == 0 or not microserver_k8s_path.stat.exists',
                      refuse['ansible.builtin.assert']['that'])

    def test_nfs_server_settings_match_the_shared_storage_component(self):
        storage = read_yaml(ROLE / 'tasks/storage.yml')
        ours = task(self.tasks, 'Restrict the NFS server to NFSv4 over TCP')['ansible.builtin.copy']
        theirs = task(storage, 'Restrict this fresh server to NFSv4 over TCP')['ansible.builtin.copy']
        self.assertEqual((ours['dest'], ours['content']), (theirs['dest'], theirs['content']))


class RegistryTests(unittest.TestCase):
    def setUp(self):
        baseline = microserver.MicroServerTests()
        baseline.setUp()
        self.context = dict(baseline.context, microserver_registry_enabled=True,
                            microserver_ui_enabled=True, microserver_ui_tls_enabled=True,
                            microserver_monitoring_enabled=True,
                            microserver_ui_address='192.168.30.10', microserver_ui_domain='lan.example.test',
                            microserver_ui_allowed_networks=['192.168.2.0/24', '192.168.30.0/24', '192.168.4.0/24'])
        self.tasks = read_yaml(ROLE / 'tasks/registry.yml')

    def render(self, name, **changes):
        return str(ENV.from_string((ROLE / 'templates' / name).read_text()).render(**dict(self.context, **changes)))

    def config(self):
        return json.loads(self.render('registry-config.json.j2'))

    def test_registry_listens_on_loopback_with_docker_compatibility(self):
        http = self.config()['http']
        self.assertEqual((http['address'], http['port'], http['compat']), ('127.0.0.1', '5000', ['docker2s2']))
        self.assertEqual(http['auth']['htpasswd']['path'], '/etc/zot/htpasswd')

    def test_large_layers_fit_the_zot_and_nginx_transfer_timeouts(self):
        # Zot defaults readTimeout/writeTimeout to 60s for the whole request;
        # a multi-GB layer upload took longer and Zot closed it (502 via nginx).
        http = self.config()['http']
        self.assertEqual((http['readTimeout'], http['writeTimeout']), ('1800s', '1800s'))
        nginx = self.render('ui-nginx.conf.j2')
        block = nginx[nginx.index('# BEGIN container registry'):nginx.index('# END container registry')]
        self.assertIn('proxy_send_timeout 1800s;', block)
        self.assertIn('proxy_read_timeout 1800s;', block)
        self.assertIn('client_max_body_size 0;', block)
        self.assertIn('proxy_request_buffering off;', block)

    def test_publisher_writes_only_local_and_everyone_reads(self):
        repositories = self.config()['http']['accessControl']['repositories']
        self.assertEqual(set(repositories), {'local/**', '**'})
        self.assertEqual(repositories['**'], {'anonymousPolicy': ['read']})
        local = repositories['local/**']
        self.assertEqual(local['anonymousPolicy'], ['read'])
        self.assertEqual(local['policies'], [{'users': ['publisher'], 'actions': ['read', 'create', 'update', 'delete']}])

    def test_each_upstream_is_an_on_demand_amd64_mirror_under_its_prefix(self):
        registries = self.config()['extensions']['sync']['registries']
        self.assertEqual([(r['urls'], r['content']) for r in registries], [
            (['https://registry-1.docker.io'], [{'prefix': '**', 'destination': '/docker.io'}]),
            (['https://ghcr.io'], [{'prefix': '**', 'destination': '/ghcr.io'}]),
            (['https://registry.k8s.io'], [{'prefix': '**', 'destination': '/registry.k8s.io'}]),
            (['https://quay.io'], [{'prefix': '**', 'destination': '/quay.io'}])])
        for registry in registries:
            self.assertTrue(registry['onDemand'] and registry['tlsVerify'])
            self.assertEqual(registry['platforms'], ['linux/amd64'])

    def test_ui_and_search_are_enabled_without_cve_scanning(self):
        extensions = self.config()['extensions']
        self.assertEqual(extensions['ui'], {'enable': True})
        self.assertEqual(extensions['search'], {'enable': True})
        # CVE scanning would download a vulnerability database; keep it off.
        self.assertNotIn('cve', extensions['search'])

    def test_container_is_pinned_unprivileged_and_read_only(self):
        compose = yaml.safe_load(self.render('registry-compose.yaml.j2'))
        zot = compose['services']['zot']
        self.assertRegex(zot['image'], r'^ghcr\.io/project-zot/zot-linux-amd64:v[0-9.]+@sha256:[a-f0-9]{64}$')
        self.assertEqual(zot['user'], '2001:2001')
        self.assertEqual(zot['command'], ['serve', '/etc/zot/config.json'])
        self.assertTrue(zot['read_only'])
        self.assertEqual(zot['cap_drop'], ['ALL'])
        self.assertIn('no-new-privileges:true', zot['security_opt'])
        self.assertEqual(sorted(zot['volumes']), sorted([
            '/etc/microserver/registry/config.json:/etc/zot/config.json:ro',
            '/etc/microserver/private/registry-htpasswd:/etc/zot/htpasswd:ro',
            '/srv/microserver/registry:/var/lib/registry']))

    def test_proxy_publishes_registry_and_ui_over_https_to_private_networks(self):
        text = self.render('ui-nginx.conf.j2')
        block = re.search(r'# BEGIN container registry\n(.*?)# END container registry', text, re.S).group(1)
        self.assertIn('listen 192.168.30.10:443 ssl;', block)
        self.assertIn('server_name registry.lan.example.test;', block)
        self.assertEqual(re.findall(r'allow ([^;]+);', block), ['10.0.0.0/8', '172.16.0.0/12', '192.168.0.0/16'])
        self.assertIn('deny all;', block)
        self.assertIn('client_max_body_size 0;', block)
        self.assertEqual(re.findall(r'location ([^ ]+) ', block), ['/'])
        self.assertIn('proxy_pass http://127.0.0.1:5000;', block)
        self.assertNotIn(':80', block)

    def test_proxy_omits_registry_unless_enabled_with_https(self):
        for changes in [dict(microserver_registry_enabled=False), dict(microserver_ui_tls_enabled=False)]:
            with self.subTest(changes=changes):
                self.assertNotIn('registry.', self.render('ui-nginx.conf.j2', **changes))

    def test_scope_requires_https_distinct_host_and_pinned_image(self):
        scope = task(self.tasks, 'Require the registry scope and its HTTPS proxy')
        self.assertTrue(assertions(scope, self.context))
        for field, value in [('microserver_ui_tls_enabled', False), ('microserver_registry_host', 'grafana'),
                             ('microserver_registry_host', 'Registry'), ('microserver_registry_port', 9090),
                             ('microserver_registry_image', 'ghcr.io/project-zot/zot-linux-amd64:latest'),
                             ('microserver_registry_publisher', 'root:x'), ('microserver_registry_quota', '1P')]:
            with self.subTest(field=field, value=value):
                self.assertFalse(assertions(scope, dict(self.context, **{field: value})))

    def test_network_validator_accepts_only_canonical_rfc1918_ipv4(self):
        argv = task(self.tasks, 'Require canonical RFC1918 client networks')['ansible.builtin.command']['argv']
        self.assertEqual(argv[:2], ['/usr/bin/python3', '-c'])

        def check(networks):
            return subprocess.run([sys.executable, '-c', argv[2], json.dumps(networks)],
                                  capture_output=True).returncode == 0
        self.assertTrue(check(['10.0.0.0/8', '172.16.0.0/12', '192.168.0.0/16']))
        self.assertTrue(check(['192.168.30.0/24']))
        for bad in [['0.0.0.0/0'], ['8.8.8.0/24'], ['192.168.30.1/24'], ['192.168.0.0/15'], ['fd00::/8'], ['192.168.030.0/24']]:
            with self.subTest(networks=bad):
                self.assertFalse(check(bad))

    def test_credential_is_owner_created_and_never_read_into_output(self):
        credential = task(self.tasks, 'Require the owner-created publisher credential')['ansible.builtin.assert']
        self.assertIn("microserver_registry_htpasswd.stat.mode == '0440'", credential['that'])
        inspect = task(self.tasks, 'Inspect the publisher credential without reading it')['ansible.builtin.stat']
        self.assertFalse(inspect['get_checksum'])
        entries = task(self.tasks, 'Require exactly one bcrypt entry for the publisher')['ansible.builtin.command']
        self.assertEqual(entries['argv'][:2], ['grep', '-c'])

    def test_https_verification_uses_only_the_nas_python(self):
        # Debian's minimal NAS image has no curl; reuse python3 like the UI probe.
        import ast
        verify = read_yaml(ROLE / 'tasks/registry-verify.yml')
        self.assertEqual(len(verify), 1)
        argv = verify[0]['ansible.builtin.command']['argv']
        self.assertEqual(argv[:2], ['/usr/bin/python3', '-c'])
        ast.parse(argv[2])
        for expected in ["('api', 'GET', '/v2/', 200)", "'/v2/registry.k8s.io/pause/manifests/3.10', 200",
                         "'/v2/local/verify/blobs/uploads/', 401", "('ui', 'GET', '/', 200)",
                         'server_hostname=host']:
            self.assertIn(expected, argv[2])
        for source in (ROLE / 'tasks').glob('registry*.yml'):
            self.assertNotIn('curl', source.read_text())

    def test_owner_plays_require_the_explicit_components(self):
        for play, flag in [('owner-k8s-storage.yml', 'microserver_k8s_storage_enabled'),
                           ('owner-registry.yml', 'microserver_registry_enabled')]:
            with self.subTest(play=play):
                guard = read_yaml(ROOT / 'ansible' / play)[0]['pre_tasks'][0]['ansible.builtin.assert']['that']
                self.assertIn("microserver_owner_install | default(false) | bool", guard)
                self.assertIn(flag + " | default(false) | bool", guard)
        for play in ['owner-k8s-storage.yml', 'owner-registry.yml']:
            self.assertIs(read_yaml(ROOT / 'ansible' / play)[0]['force_handlers'], True)
        imports = [step['ansible.builtin.import_role']['tasks_from']
                   for step in read_yaml(ROOT / 'ansible/owner-registry.yml')[0]['tasks']]
        self.assertEqual(imports, ['registry', 'ui', 'registry-verify'])


if __name__ == '__main__':
    unittest.main()
