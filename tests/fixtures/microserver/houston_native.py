"""Real vendor DB, D-Bus, HTTP and Alertmanager inside one network-isolated fixture."""
import datetime
import json
from pathlib import Path
import signal
import sqlite3
import subprocess
import time
import urllib.request
import uuid


def request(url, body=None):
    value = urllib.request.Request(url, data=None if body is None else json.dumps(body).encode(),
                                   headers={'Content-Type': 'application/json'})
    with urllib.request.urlopen(value, timeout=3) as response:
        assert response.status == 200


def wait(predicate, seconds=60):
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        try:
            if predicate():
                return
        except (OSError, sqlite3.Error):
            pass
        time.sleep(0.5)
    raise AssertionError('native fixture acceptance timed out')


children = []
try:
    Path('/run/dbus').mkdir(parents=True, exist_ok=True)
    Path('/var/lib/sqlite/45drives').mkdir(parents=True, exist_ok=True, mode=0o700)
    log = open('/tmp/fixture.log', 'wb')
    def start(*args):
        children.append(subprocess.Popen(args, stdout=log, stderr=log))
    # This private bus stays UID 0 because the fixture drops every capability.
    # Its normal system policies and the production Houston policy still apply.
    config = Path('/usr/share/dbus-1/system.conf').read_text()
    assert config.count('<user>messagebus</user>') == 1
    Path('/tmp/system.conf').write_text(config.replace('<user>messagebus</user>', ''))
    start('/usr/bin/dbus-daemon', '--config-file=/tmp/system.conf', '--nofork', '--nopidfile')
    wait(lambda: Path('/run/dbus/system_bus_socket').exists(), 10)
    subprocess.run(['/usr/bin/python3', '/usr/local/libexec/microserver-houston-dbus.py', '--setup'], check=True)
    start('/usr/bin/python3', '/usr/local/libexec/microserver-houston-dbus.py')
    import dbus
    def bus_ready():
        return dbus.SystemBus().name_has_owner('org._45drives.Houston')
    wait(bus_ready, 15)
    # The parent starts the receiver through docker exec as microserver-alerts.
    Path('/tmp/bus-ready').touch()
    subprocess.run(['/usr/local/bin/amtool', 'check-config', '/fixture/alertmanager.yaml'], check=True)
    start('/usr/local/bin/alertmanager', '--config.file=/fixture/alertmanager.yaml', '--storage.path=/tmp/alertmanager',
          '--web.listen-address=127.0.0.1:9093', '--cluster.listen-address=')
    wait(lambda: request('http://127.0.0.1:9093/-/ready') is None, 15)
    wait(lambda: request('http://127.0.0.1:5002/ready') is None, 15)
    identity = uuid.uuid4().hex
    now = datetime.datetime.now(datetime.timezone.utc)
    alert = dict(labels=dict(alertname='MicroserverHoustonAcceptance', acceptance_id=identity, severity='critical'),
                 annotations=dict(summary='FIXTURE_PRIVATE_PAYLOAD'), startsAt=now.isoformat(),
                 endsAt=(now + datetime.timedelta(minutes=4)).isoformat())
    def stored(status):
        with sqlite3.connect('/var/lib/sqlite/45drives/notifications.db') as connection:
            events = connection.execute('SELECT event, severity FROM notifications WHERE event LIKE ?',
                                        ('%' + identity + '%',)).fetchall()
        expected_severity = 'error' if status == 'firing' else 'info'
        return any(json.loads(event)['status'] == status and severity == expected_severity for event, severity in events)
    request('http://127.0.0.1:9093/api/v2/alerts', [alert])
    wait(lambda: stored('firing'))
    alert['endsAt'] = datetime.datetime.now(datetime.timezone.utc).isoformat()
    request('http://127.0.0.1:9093/api/v2/alerts', [alert])
    wait(lambda: stored('resolved'))
    log.flush()
    assert b'FIXTURE_PRIVATE_PAYLOAD' not in Path('/tmp/fixture.log').read_bytes()
    assert not Path('/var/log/msmtp_test.log').exists()
    print('HOUSTON_NATIVE=passed; firing and resolved persisted; no payload logs or SMTP configuration', flush=True)
    Path('/tmp/accepted').touch()
    # Parent test performs separate UID policy checks, then stops this owned fixture.
    signal.pause()
except Exception:
    if Path('/tmp/fixture.log').exists():
        print(Path('/tmp/fixture.log').read_text()[-3000:], flush=True)
    raise
finally:
    for child in reversed(children):
        child.terminate()
    for child in children:
        child.wait(timeout=5)
