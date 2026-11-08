#!/usr/bin/python3
"""Owner-only synthetic firing/resolved delivery and metadata checks."""
import datetime
import json
from pathlib import Path
import socket
import sqlite3
import stat
import subprocess
import sys
import time
import urllib.request
import urllib.parse
import uuid

DATABASE = Path('/var/lib/sqlite/45drives/notifications.db')


def request(path, body=None):
    request = urllib.request.Request('http://127.0.0.1:9093' + path,
                                     data=None if body is None else json.dumps(body).encode(),
                                     headers={'Content-Type': 'application/json'})
    with urllib.request.urlopen(request, timeout=5) as response:
        if response.status != 200:
            raise RuntimeError('Alertmanager request failed')


def received(identity, status):
    # Read only the unique synthetic event; no SMTP/configuration columns.
    with sqlite3.connect('file:' + str(DATABASE) + '?mode=ro', uri=True) as connection:
        rows = connection.execute('SELECT event FROM notifications WHERE event LIKE ? LIMIT 100',
                                  ('%' + identity + '%',)).fetchall()
    return any(json.loads(row[0]).get('status') == status for row in rows)


def wait_for(identity, status):
    deadline = time.monotonic() + 75
    while time.monotonic() < deadline:
        if received(identity, status):
            return
        time.sleep(2)
    raise RuntimeError('synthetic Houston delivery timed out')


def verify():
    if socket.gethostname().split('.')[0] != 'ig0r':
        raise RuntimeError('unexpected host')
    with urllib.request.urlopen('http://127.0.0.1:5002/ready', timeout=5) as response:
        if response.status != 200:
            raise RuntimeError('receiver not ready')
    listeners = subprocess.run(['/usr/bin/ss', '-H', '-ltn', 'sport', '=', ':5002'],
                               capture_output=True, text=True, check=True, timeout=10).stdout.splitlines()
    if len(listeners) != 1 or listeners[0].split()[3] != '127.0.0.1:5002':
        raise RuntimeError('receiver is not restricted to IPv4 loopback')
    for name in ('houston-dbus.service', 'houston-alerts-webhook.service'):
        subprocess.run(['/usr/bin/systemctl', 'is-active', '--quiet', name], check=True, timeout=10)
    for name in ('houston-zfs-storage-alert.service', 'houston-zfs-storage-alert.timer'):
        result = subprocess.run(['/usr/bin/systemctl', 'is-active', '--quiet', name], timeout=10)
        if result.returncode == 0:
            raise RuntimeError('duplicate vendor storage alerts became active')
    for name in ('data-notify', 'pool_import-led', 'resilver_finish-notify', 'scrub_finish-notify',
                 'statechange-notify', 'vdev_attach-led', 'vdev_clear-led'):
        metadata = Path('/etc/zfs/zed.d/' + name + '_zed_dbus_notify.sh').lstat()
        if not stat.S_ISREG(metadata.st_mode) or metadata.st_uid != 0 or metadata.st_mode & 0o111:
            raise RuntimeError('vendor ZED hook isolation changed')
    for link, destination in (('/etc/prometheus/prometheus.yml', '/etc/microserver/monitoring/prometheus.yaml'),
                              ('/etc/alertmanager/alertmanager.yml', '/etc/microserver/monitoring/alertmanager.yaml')):
        if not Path(link).is_symlink() or str(Path(link).resolve()) != destination:
            raise RuntimeError('vendor configuration status link changed')
    with urllib.request.urlopen('http://127.0.0.1:9090/api/v1/alertmanagers', timeout=5) as response:
        targets = json.load(response)['data']['activeAlertmanagers']
    addresses = [urllib.parse.urlsplit(item['url']) for item in targets]
    if not any(item.hostname == '127.0.0.1' and item.port == 9093 for item in addresses):
        raise RuntimeError('Prometheus Alertmanager target is not active')
    identity = uuid.uuid4().hex
    now = datetime.datetime.now(datetime.timezone.utc)
    alert = dict(labels=dict(alertname='MicroserverHoustonAcceptance', acceptance_id=identity,
                             severity='info', instance='microserver-owner-acceptance'),
                 annotations=dict(summary='Owner acceptance: local notification delivery'),
                 startsAt=now.isoformat(), endsAt=(now + datetime.timedelta(minutes=4)).isoformat())
    print('HOUSTON_ACCEPTANCE_ID=' + identity, flush=True)
    request('/api/v2/alerts', [alert])
    try:
        wait_for(identity, 'firing')
    finally:
        # Resolve only this synthetic alert even when a readback fails.
        alert['endsAt'] = datetime.datetime.now(datetime.timezone.utc).isoformat()
        request('/api/v2/alerts', [alert])
    wait_for(identity, 'resolved')
    print('HOUSTON_ALERTS=passed; FIRING=stored; RESOLVED=stored; RECEIVER=loopback; DELIVERY=local-ui-only')


if __name__ == '__main__':
    try:
        verify()
    except Exception:
        print('STOP: Houston acceptance failed; retain owner log and recovery state', file=sys.stderr)
        sys.exit(1)
