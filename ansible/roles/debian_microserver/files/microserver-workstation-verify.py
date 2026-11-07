"""Read only rule health and selected public Grafana metadata; no credentials."""
from datetime import datetime, timezone
from http.client import HTTPConnection
import argparse
import json
import os
from pathlib import Path
import socket
import sqlite3
import stat
import sys

NAMES = ('workstation', 'gpus', 'host', 'workloads', 'power', 'monitoring', 'kubernetes', 'pods')
DIRECTORY = Path('/etc/microserver/monitoring/dashboard-folders/Infrastructure')
DATABASE = Path('/var/lib/microserver/grafana/grafana.db')
SOURCES = {'prometheus': ('prometheus', 'http://127.0.0.1:9090'),
           'loki': ('loki', 'http://127.0.0.1:3100'), 'tempo': ('tempo', 'http://127.0.0.1:3200')}
VIEW = 'workstation_dashboard_metadata'
# Grafana 13 stores provisioned dashboards in resource, not the legacy dashboard
# table. Extract these public fields inside SQLite; never return resource.value.
DASHBOARD_VIEW = '''CREATE TEMP VIEW workstation_dashboard_metadata AS
SELECT name AS uid,
       json_extract(CAST(value AS TEXT), '$.spec.title') AS title,
       json_extract(CAST(value AS TEXT), '$.metadata.name') AS document_name,
       json_extract(CAST(value AS TEXT), '$.metadata.namespace') AS document_namespace,
       json_extract(CAST(value AS TEXT), '$.kind') AS kind,
       json_extract(CAST(value AS TEXT), '$.apiVersion') AS api_version,
       json_extract(CAST(value AS TEXT), '$.metadata.deletionTimestamp') AS deleted,
       json_extract(CAST(value AS TEXT), '$.metadata.labels."grafana.app/get-trash"') AS trashed
FROM main.resource
WHERE "group"='dashboard.grafana.app' AND resource='dashboards'
      AND namespace='default' AND action IN (1,2)
      AND name IN ('workstation-overview','workstation-gpus','workstation-host',
                   'workstation-workloads','workstation-power','workstation-monitoring',
                   'workstation-kubernetes','workstation-pods','microserver-zfs')'''


def require(condition, message):
    if not condition:
        raise RuntimeError(message)


def metadata_only(action, table, column, database, source):
    if action == sqlite3.SQLITE_SELECT:
        return sqlite3.SQLITE_OK
    if action == sqlite3.SQLITE_FUNCTION and column == 'json_extract' and source == VIEW:
        return sqlite3.SQLITE_OK
    if action == sqlite3.SQLITE_READ and database == 'temp' and table == VIEW and column in {
            'uid', 'title', 'document_name', 'document_namespace', 'kind', 'api_version', 'deleted', 'trashed'}:
        return sqlite3.SQLITE_OK
    if action == sqlite3.SQLITE_READ and database == 'main' and table == 'resource' and source == VIEW and column in {
            'name', 'value', 'group', 'resource', 'namespace', 'action'}:
        return sqlite3.SQLITE_OK
    if action == sqlite3.SQLITE_READ and database == 'main' and table == 'data_source' and column in {'uid', 'type', 'url', 'org_id'}:
        return sqlite3.SQLITE_OK
    return sqlite3.SQLITE_DENY


def verify_catalog(connection, expected, sources=None):
    sources = SOURCES if sources is None else sources
    # This view exists only in the connection's temporary schema. The main
    # database is opened mode=ro; the authorizer then denies all schema writes.
    connection.execute(DASHBOARD_VIEW)
    connection.execute('PRAGMA query_only=ON')
    connection.set_authorizer(metadata_only)
    placeholders = ','.join('?' for _ in expected)
    rows = connection.execute('SELECT uid,title FROM temp.' + VIEW +
                              " WHERE document_name=uid AND document_namespace='default' AND kind='Dashboard'" +
                              " AND api_version='dashboard.grafana.app/v0alpha1' AND deleted IS NULL" +
                              " AND (trashed IS NULL OR trashed='false') AND uid IN (" + placeholders + ')',
                              list(expected)).fetchall()
    require(dict(rows) == expected and len(rows) == len(expected), 'Grafana workstation dashboard metadata differs or is incomplete')
    rows = connection.execute('SELECT uid,type,url FROM data_source WHERE org_id=1 AND uid IN (' +
                              ','.join('?' for _ in sources) + ')', list(sources)).fetchall()
    require({uid: (kind, url) for uid, kind, url in rows} == sources and len(rows) == len(sources),
            'Grafana loopback datasource metadata differs or is incomplete')


def expected_dashboards(zfs=False):
    expected = {}
    for name in (('zfs',) if zfs else NAMES):
        path = DIRECTORY / (name + '.json')
        info = path.lstat()
        require(stat.S_ISREG(info.st_mode) and info.st_uid == 0 and not info.st_mode & 0o022,
                'Dashboard file type or ownership differs')
        data = json.loads(path.read_text())
        uid = 'microserver-zfs' if zfs else ('workstation-overview' if name == 'workstation' else 'workstation-' + name)
        require(data['uid'] == uid and uid not in expected, 'Dashboard identity differs')
        expected[uid] = data['title']
    return expected


def verify_rule_group(payload):
    require(payload.get('status') == 'success', 'Prometheus rule metadata unavailable')
    groups = payload.get('data', {}).get('groups', [])
    selected = [group for group in groups if group['name'] == 'workstation' and
                group['file'] == '/etc/prometheus/workstation-alerts.yaml']
    require(len(selected) == 1 and len(selected[0]['rules']) == 16, 'Expected all 16 workstation rules')
    group = selected[0]
    names = {rule['name'] for rule in group['rules']}
    require(len(names) == 16 and {'WorkstationHostTelemetryMissing', 'WorkstationHostAlloyHeartbeatMissing',
                                'workstation:memory_available_ratio', 'workstation:memory_pressure_ratio:rate5m'} <= names,
            'Required workstation rule identities are missing')
    require(all(rule.get('health') == 'ok' and not rule.get('lastError') for rule in group['rules']),
            'Workstation rule evaluation is not healthy yet')
    evaluated = datetime.fromisoformat(group['lastEvaluation'].replace('Z', '+00:00'))
    require(0 <= (datetime.now(timezone.utc) - evaluated).total_seconds() < 90, 'Workstation rule evaluation is stale')
    base = [entry for entry in groups if entry['name'] == 'microserver' and entry['file'] == '/etc/prometheus/alerts.yaml']
    require(len(base) == 1 and len(base[0]['rules']) == 6, 'Existing MicroServer rules are missing')
    return sum(rule.get('state') == 'firing' for rule in group['rules'])


def verify_zfs_rules(payload):
    require(payload.get('status') == 'success', 'Prometheus rule metadata unavailable')
    groups = payload.get('data', {}).get('groups', [])
    selected = [group for group in groups if group['name'] == 'microserver_zfs' and
                group['file'] == '/etc/prometheus/alerts.yaml']
    require(len(selected) == 1 and len(selected[0]['rules']) == 7, 'Expected all seven ZFS rules')
    group = selected[0]
    expected = {'MicroServerZFSMetricsUnavailable', 'MicroServerZFSPoolUnhealthy', 'MicroServerZFSDeviceUnhealthy',
                'MicroServerZFSDeviceErrors', 'MicroServerZFSDataErrors', 'MicroServerZFSPoolSpaceLow',
                'MicroServerZFSPoolSpaceCritical'}
    require({rule['name'] for rule in group['rules']} == expected, 'Required ZFS rule identities are missing')
    require(all(rule.get('health') == 'ok' and not rule.get('lastError') for rule in group['rules']),
            'ZFS rule evaluation is not healthy yet')
    evaluated = datetime.fromisoformat(group['lastEvaluation'].replace('Z', '+00:00'))
    require(0 <= (datetime.now(timezone.utc) - evaluated).total_seconds() < 90, 'ZFS rule evaluation is stale')
    base = [entry for entry in groups if entry['name'] == 'microserver' and entry['file'] == '/etc/prometheus/alerts.yaml']
    require(len(base) == 1 and len(base[0]['rules']) == 6, 'Existing MicroServer rules are missing')
    return sum(rule.get('state') == 'firing' for rule in group['rules'])


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--zfs', action='store_true', help='Verify the ZFS dashboard, Prometheus datasource and ZFS rules')
    args = parser.parse_args()
    require(os.geteuid() == 0 and socket.gethostname().split('.')[0] == 'microserver', 'Expected the owner on the commissioned NAS')
    expected = expected_dashboards(args.zfs)
    info = DATABASE.lstat()
    require(stat.S_ISREG(info.st_mode) and info.st_uid == 472 and not info.st_mode & 0o022,
            'Grafana database type or ownership differs')
    connection = sqlite3.connect(DATABASE.as_uri() + '?mode=ro', uri=True, timeout=5)
    try:
        verify_catalog(connection, expected, {'prometheus': SOURCES['prometheus']} if args.zfs else SOURCES)
    finally:
        connection.close()
    client = HTTPConnection('127.0.0.1', 9090, timeout=8)
    try:
        client.request('GET', '/api/v1/rules')
        response = client.getresponse()
        data = response.read(2097153)
        require(response.status == 200 and len(data) <= 2097152, 'Bounded Prometheus rule query failed')
        firing = (verify_zfs_rules if args.zfs else verify_rule_group)(json.loads(data))
    finally:
        client.close()
    if args.zfs:
        print('GRAFANA_ZFS_METADATA=dashboard and loopback Prometheus datasource verified; credential columns excluded')
        print('ZFS_RULES=7 loaded and evaluated; FIRING_ALERT_COUNT=' + str(firing))
    else:
        print('GRAFANA_METADATA=8 workstation dashboards and 3 loopback datasources verified; credential columns excluded')
        print('WORKSTATION_RULES=16 loaded and evaluated; FIRING_ALERT_COUNT=' + str(firing))


if __name__ == '__main__':
    try:
        main()
    except (RuntimeError, OSError, ValueError, TypeError, KeyError, sqlite3.Error) as error:
        print('STOP: ' + (str(error) if isinstance(error, RuntimeError) else type(error).__name__), file=sys.stderr)
        sys.exit(1)
