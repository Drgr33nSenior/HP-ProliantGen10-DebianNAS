"""Adapt four pinned upstream dashboards for the existing Grafana deployment."""
import argparse
import hashlib
import json
from pathlib import Path
import re

ROOT = Path(__file__).resolve().parents[2]
BUNDLE = ROOT / 'observability/unpoller'
METRIC = re.compile(r'\bunifipoller_[A-Za-z0-9_]+')


def strings(value):
    if isinstance(value, str):
        yield value
    elif isinstance(value, dict):
        for item in value.values():
            yield from strings(item)
    elif isinstance(value, list):
        for item in value:
            yield from strings(item)


def replace(value):
    if isinstance(value, str):
        return value.replace('${DS_PROMETHEUS}', 'prometheus').replace('unpoller_', 'unifipoller_')
    if isinstance(value, dict):
        return {key: replace(item) for key, item in value.items()}
    if isinstance(value, list):
        return [replace(item) for item in value]
    return value


def panels(items):
    for panel in items:
        yield panel
        yield from panels(panel.get('panels', []))


def adapt(source, name):
    dashboard = replace(source)
    dashboard.pop('__inputs', None)
    dashboard['id'] = None
    dashboard['refresh'] = '1m'
    # DPI is deliberately disabled in this installation.
    if name == 'sites':
        dashboard['panels'] = [panel for panel in dashboard['panels'] if panel['id'] != 45]
    for variable in dashboard.get('templating', {}).get('list', []):
        if variable.get('includeAll'):
            variable['allValue'] = '.*'
            variable['current'] = dict(selected=True, text='All', value='$__all')
        if variable['name'] == 'Smooth':
            options = []
            for option in variable['options']:
                match = re.fullmatch(r'(\d+)([smh])', option['value'])
                if not match:
                    raise ValueError('Unexpected upstream smoothing option')
                seconds = int(match[1]) * {'s': 1, 'm': 60, 'h': 3600}[match[2]]
                if seconds >= 300:
                    options.append(dict(option, selected=option['value'] == '5m'))
            variable['options'] = options
            variable['query'] = ','.join(option['value'] for option in options)
            variable['current'] = dict(selected=True, text='5m', value='5m')
    for panel in panels(dashboard['panels']):
        if panel['type'] == 'grafana-clock-panel':
            if name != 'access-points' or panel['id'] != 32:
                raise ValueError('Unexpected custom panel in upstream dashboard')
            original = dict(panel)
            panel.clear()
            panel.update(id=original['id'], gridPos=original['gridPos'], title='Collection age',
                         type='stat', datasource=dict(type='prometheus', uid='prometheus'),
                         targets=[dict(refId='A', expr='max(unifipoller_prometheus_cache_age_seconds{job="microserver-unifi"})', instant=True)],
                         fieldConfig=dict(defaults=dict(unit='s', noValue='No data',
                             thresholds=dict(mode='absolute', steps=[dict(color='green', value=None), dict(color='red', value=180)])), overrides=[]),
                         options=dict(reduceOptions=dict(calcs=['lastNotNull'], fields='', values=False)))
        if panel['type'] == 'graph':
            if name != 'access-points' or panel['id'] != 8:
                raise ValueError('Unexpected legacy graph in upstream dashboard')
            original = dict(panel)
            panel.clear()
            panel.update(id=original['id'], gridPos=original['gridPos'], title=original['title'],
                         type='timeseries', datasource=original['datasource'], targets=original['targets'],
                         fieldConfig=dict(defaults=dict(unit='percentunit', min=0, max=1, decimals=1,
                             custom=dict(drawStyle='line', lineWidth=3, fillOpacity=0, spanNulls=False)), overrides=[]),
                         options=dict(legend=dict(displayMode='table', placement='right', calcs=['lastNotNull', 'max']),
                                      tooltip=dict(mode='multi', sort='desc')))
        for target in panel.get('targets', []):
            if target.get('interval') == '$Smooth':
                target['interval'] = '1m'
            if isinstance(target.get('expr'), str):
                target['expr'] = re.sub(r'\[(?:30s|1m|2m)\]', '[$Smooth]', target['expr'])
                target['expr'] = target['expr'].replace('[$__interval]', '[$Smooth]')
    dashboard['__requires'] = [item for item in dashboard.get('__requires', [])
                               if item['id'] not in {'grafana-clock-panel', 'graph'}]
    return dashboard


def render():
    lock = json.loads((BUNDLE / 'lock.json').read_text())
    outputs = {}
    metrics = {'unifipoller_controller_up', 'unifipoller_prometheus_cache_age_seconds',
               'unifipoller_prometheus_refresh_failures_total'}
    for entry in lock['dashboards']:
        name = entry['name']
        if name not in {'sites', 'gateway', 'access-points', 'clients'}:
            raise ValueError('Unexpected dashboard filename')
        content = (BUNDLE / 'upstream' / (name + '.json')).read_bytes()
        blob = b'blob ' + str(len(content)).encode() + b'\0' + content
        if (hashlib.sha256(content).hexdigest() != entry['sha256']
                or hashlib.sha1(blob).hexdigest() != entry['git_blob_sha1']):
            raise ValueError('Upstream dashboard differs from its reviewed immutable pin')
        dashboard = adapt(json.loads(content), name)
        if dashboard['uid'] != entry['uid']:
            raise ValueError('Upstream dashboard UID changed')
        for value in strings(dashboard):
            metrics.update(METRIC.findall(value))
        outputs[BUNDLE / 'generated' / (name + '.json')] = dashboard
    overview = json.loads((ROOT / 'ansible/roles/debian_microserver/files/network-dashboard.json').read_text())
    for value in strings(overview):
        metrics.update(METRIC.findall(value))
    outputs[BUNDLE / 'metrics.json'] = sorted(metrics)
    return {path: json.dumps(content, indent=2) + '\n' for path, content in outputs.items()}


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--check', action='store_true', help='Verify generated files without writing')
    args = parser.parse_args()
    outputs = render()
    for path, content in outputs.items():
        if args.check:
            if not path.is_file() or path.read_text() != content:
                raise SystemExit('Regenerate reviewed UnPoller dashboards: ' + str(path.relative_to(ROOT)))
        else:
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(content)
    print('Verified' if args.check else 'Generated', 'four pinned UnPoller dashboards and their metric allowlist.')
