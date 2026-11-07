"""Owner TLS validation and renewal deployment; never print protected material."""
import argparse
import hmac
from http.client import HTTPResponse
import ipaddress
import json
import os
from pathlib import Path
import re
import socket
import ssl
import stat
import subprocess
import sys
import tempfile
import time

LIVE = Path('/etc/letsencrypt/live/microserver-ui')
ARCHIVE = Path('/etc/letsencrypt/archive/microserver-ui')
CREDENTIALS = Path('/run/credentials/microserver-ui.service')
SCOPE = Path('/etc/microserver/ui/tls.json')
CONFIG = Path('/etc/microserver/ui/nginx.conf')
DROPIN = Path('/etc/systemd/system/microserver-ui.service.d/tls.conf')
NAMES = ('grafana', 'prometheus', 'alertmanager')


def require(condition, message):
    if not condition:
        raise RuntimeError(message)


def run(arguments, data=None, timeout=15):
    result = subprocess.run(arguments, input=data, stdout=subprocess.PIPE,
                            stderr=subprocess.PIPE, timeout=timeout, check=False)
    operation = Path(arguments[0]).name + (' ' + arguments[1] if len(arguments) > 1 else '')
    require(result.returncode == 0, 'TLS ' + operation + ' failed (exit ' + str(result.returncode) + '); protected output omitted')
    require(len(result.stdout) <= 65536 and len(result.stderr) <= 65536, 'TLS command output exceeded its bound')
    return result.stdout


def validate_scope(scope):
    require(type(scope.get('cockpit', False)) is bool, 'Expected a boolean Cockpit scope')
    address = ipaddress.IPv4Address(scope['address'])
    require(str(address) == scope['address'] and any(address in ipaddress.IPv4Network(network)
            for network in ('10.0.0.0/8', '172.16.0.0/12', '192.168.0.0/16')), 'Expected a canonical private NAS address')
    require(isinstance(scope['domain'], str) and len(scope['domain']) <= 230 and re.fullmatch(
        r'(?:[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?\.)+[a-z](?:[a-z0-9-]{0,61}[a-z0-9])?', scope['domain']),
        'Expected a lowercase DNS suffix')


def protected(path, directory=False, private=False):
    info = path.lstat()
    require((stat.S_ISDIR(info.st_mode) if directory else stat.S_ISREG(info.st_mode)) and info.st_uid == 0
            and not info.st_mode & (0o077 if private else 0o022), 'Untrusted TLS file type, owner or permissions')
    return path


def read_scope():
    protected(SCOPE)
    scope = json.loads(SCOPE.read_text())
    validate_scope(scope)
    return scope


def certificate_member(name, private=False):
    for directory in [LIVE.parent.parent, LIVE.parent, LIVE, ARCHIVE.parent, ARCHIVE]:
        protected(directory, directory=True)
    path = (LIVE / (name + '.pem')).resolve(strict=True)
    require(path.parent == ARCHIVE and re.fullmatch(name + r'[0-9]+\.pem', path.name),
            'Certificate link leaves the reviewed Certbot lineage')
    return protected(path, private=private)


def verify_certificate(scope):
    leaf = certificate_member('cert')
    chain = certificate_member('fullchain')
    key = certificate_member('privkey', private=True)
    run(['/usr/bin/openssl', 'x509', '-in', str(leaf), '-noout', '-checkend', '604800'])
    for name in NAMES + (('microserver',) if scope.get('cockpit', False) else ()):
        run(['/usr/bin/openssl', 'verify', '-CApath', '/etc/ssl/certs', '-purpose', 'sslserver',
             '-verify_hostname', name + '.' + scope['domain'], '-untrusted', str(chain), str(leaf)])
    public = run(['/usr/bin/openssl', 'x509', '-in', str(leaf), '-noout', '-pubkey'])
    public = run(['/usr/bin/openssl', 'pkey', '-pubin', '-outform', 'DER'], public)
    # Certbot keys are unencrypted. An explicit empty passphrase prevents a
    # prompt; file:/dev/null fails while OpenSSL reads the password source.
    paired = run(['/usr/bin/openssl', 'pkey', '-in', str(key), '-passin', 'pass:', '-pubout', '-outform', 'DER'])
    require(hmac.compare_digest(public, paired), 'Certificate and private key do not match')


def check_nginx(candidate, scope):
    verify_certificate(scope)
    source = Path(candidate).read_text()
    # PID 1 loads credentials at service activation. During root preflight,
    # reference the same protected Certbot files without copying private keys.
    for credential, filename in [('ui-fullchain.pem', 'fullchain.pem'), ('ui-privkey.pem', 'privkey.pem')]:
        source = source.replace(str(CREDENTIALS / credential), str(LIVE / filename))
    with tempfile.TemporaryDirectory(prefix='microserver-tls-check-', dir='/run') as directory:
        path = Path(directory) / 'nginx.conf'
        path.write_text(source)
        # Root validation also changes temporary-directory ownership. Debian's
        # compiled default is nobody; retain the actual service identity.
        run(['/usr/sbin/nginx', '-e', 'stderr', '-t', '-c', str(path), '-g', 'user www-data; pid /dev/null;'])


def request(scope, name, path, secure=True):
    hostname = name + '.' + scope['domain']
    connection = socket.create_connection((scope['address'], 443 if secure else 80), timeout=8)
    try:
        if secure:
            connection = ssl.create_default_context().wrap_socket(connection, server_hostname=hostname)
            current = run(['/usr/bin/openssl', 'x509', '-in', str(certificate_member('cert')), '-outform', 'DER'])
            require(hmac.compare_digest(connection.getpeercert(binary_form=True), current),
                    'Proxy is not presenting the current Certbot certificate')
        connection.sendall(('GET ' + path + ' HTTP/1.1\r\nHost: ' + hostname + '\r\nConnection: close\r\n\r\n').encode('ascii'))
        response = HTTPResponse(connection)
        response.begin()
        body = response.read(65537)
        require(len(body) <= 65536, 'TLS health response exceeded its bound')
        return response.status, response.getheader('Location')
    finally:
        connection.close()


def probe(scope):
    endpoints = [('grafana', '/api/health'), ('prometheus', '/-/ready'), ('alertmanager', '/-/ready')]
    if scope.get('cockpit', False):
        endpoints.append(('microserver', '/'))
    for name, path in endpoints:
        require(request(scope, name, path)[0] == 200, 'Trusted HTTPS health check failed for ' + name)
        status, location = request(scope, name, '/', secure=False)
        require(status == 308 and location == 'https://' + name + '.' + scope['domain'] + '/',
                'HTTP redirect differs for ' + name)
    require(request(scope, 'grafana', '/api/user')[0] == 401, 'Grafana authenticated API became available anonymously')
    if scope.get('cockpit', False):
        require(request(scope, 'microserver', '/cockpit/login')[0] == 401, 'Cockpit login did not require authentication')


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('operation', choices=['cert', 'nginx', 'probe', 'renew'])
    parser.add_argument('candidate', nargs='?')
    args = parser.parse_args()
    require(os.geteuid() == 0 and socket.gethostname().split('.')[0] == 'microserver', 'Expected the owner on the commissioned NAS')
    scope = read_scope()
    if args.operation == 'cert':
        verify_certificate(scope)
        print('TLS_CERTIFICATE=trusted chain, configured hostnames, expiry and key pairing verified')
    elif args.operation == 'nginx':
        require(bool(args.candidate), 'Specify the candidate configuration')
        check_nginx(args.candidate, scope)
    elif args.operation == 'probe':
        probe(scope)
        print('TLS_UI=configured HTTPS endpoints, redirects and authentication verified')
    elif DROPIN.exists():
        protected(DROPIN)
        check_nginx(CONFIG, scope)
        # systemd credential snapshots refresh on restart, rather than HUP.
        run(['/usr/bin/systemctl', 'restart', 'microserver-ui.service'], timeout=45)
        for attempt in range(20):
            try:
                probe(scope)
                break
            except ConnectionRefusedError:
                if attempt == 19:
                    raise
                time.sleep(0.5)
        print('TLS_RENEW_DEPLOY=proxy credential snapshot refreshed and verified')


if __name__ == '__main__':
    try:
        main()
    except (RuntimeError, OSError, ValueError, TypeError, KeyError, subprocess.SubprocessError) as error:
        print('STOP: ' + (str(error) if isinstance(error, RuntimeError) else type(error).__name__), file=sys.stderr)
        sys.exit(1)
