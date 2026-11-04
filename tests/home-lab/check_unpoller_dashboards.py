"""Check immutable dashboard provenance, provisioning and native query syntax."""
import importlib.util
import json
from pathlib import Path
import re
import shutil
import subprocess
import tempfile
import unittest

import yaml

from check_microserver import ENV, ROLE, ROOT, read_yaml

spec = importlib.util.spec_from_file_location('unpoller_dashboards', ROOT / 'observability/unpoller/render.py')
renderer = importlib.util.module_from_spec(spec)
spec.loader.exec_module(renderer)


def substitute(expression, dashboard):
    values = {item['name']: '.*' for item in dashboard['templating']['list']}
    values.update(Smooth='5m', Identifier='name', __interval='1m', __rate_interval='5m', __range='3h', __range_s='10800')
    for name, value in sorted(values.items(), key=lambda pair: -len(pair[0])):
        expression = expression.replace('${' + name + '}', value)
        expression = re.sub(r'\$' + re.escape(name) + r'\b', lambda _: value, expression)
    if re.search(r'\$(?:[A-Za-z_]|\{)', expression):
        raise AssertionError('Unresolved dashboard variable: ' + expression)
    return expression


class UnPollerDashboardTests(unittest.TestCase):
    def setUp(self):
        self.outputs = renderer.render()
        self.lock = read_yaml(ROOT / 'observability/unpoller/lock.json')
        self.dashboards = [json.loads(value) for path, value in self.outputs.items() if path.parent.name == 'generated']

    def test_reviewed_sources_generated_outputs_license_and_payload(self):
        self.assertEqual(len(self.dashboards), 4)
        self.assertRegex(self.lock['commit'], r'^[a-f0-9]{40}$')
        self.assertEqual(len({dashboard['uid'] for dashboard in self.dashboards}), 4)
        self.assertTrue((ROOT / self.lock['license_file']).read_text().startswith('MIT LICENSE.'))
        payload = set((ROOT / 'installer/media/payload.files').read_text().splitlines())
        for path, content in self.outputs.items():
            self.assertEqual(path.read_text(), content, str(path))
            self.assertIn(str(path.relative_to(ROOT)), payload)
        for entry in self.lock['dashboards']:
            self.assertIn('observability/unpoller/upstream/' + entry['name'] + '.json', payload)
            self.assertRegex(entry['sha256'], r'^[a-f0-9]{64}$')
        self.assertIn(self.lock['license_file'], payload)
        tasks = read_yaml(ROLE / 'tasks/monitoring.yml')
        copy = next(task for task in tasks if task['name'].startswith('Provision the pinned upstream UnPoller'))
        self.assertIn('/Network/', copy['ansible.builtin.copy']['dest'])
        self.assertIn('microserver_unifi_insights.dashboards', copy['loop'])

    def test_native_panels_datasource_namespace_and_smoothing(self):
        supported = {'row', 'text', 'stat', 'table', 'timeseries', 'piechart', 'gauge'}
        for dashboard in self.dashboards:
            self.assertIsNone(dashboard['id'])
            self.assertNotIn('__inputs', dashboard)
            self.assertEqual(dashboard['refresh'], '1m')
            for panel in renderer.panels(dashboard['panels']):
                self.assertIn(panel['type'], supported)
                for target in panel.get('targets', []):
                    self.assertIsNone(re.search(r'\[(?:30s|1m|2m)\]', target.get('expr', '')))
                    self.assertNotIn('[$__interval]', target.get('expr', ''))
            for text in renderer.strings(dashboard):
                self.assertNotIn('${DS_PROMETHEUS}', text)
                self.assertIsNone(re.search(r'\bunpoller_[a-z]', text))
                self.assertNotIn('_dpi_', text)
            for variable in dashboard['templating']['list']:
                if variable.get('includeAll'):
                    self.assertEqual(variable['allValue'], '.*')
                    self.assertEqual(variable['current']['value'], '$__all')
                if variable['name'] == 'Smooth':
                    self.assertEqual(variable['current']['value'], '5m')
                    self.assertFalse({'30s', '1m', '2m'} & {item['value'] for item in variable['options']})
        ap = next(item for item in self.dashboards if item['uid'] == 'g5wFWqxZk')
        self.assertEqual(next(panel for panel in renderer.panels(ap['panels']) if panel['id'] == 8)['fieldConfig']['defaults']['unit'], 'percentunit')
        self.assertEqual(next(panel for panel in renderer.panels(ap['panels']) if panel['id'] == 32)['title'], 'Collection age')

    def test_prometheus_keeps_dashboard_families_and_excludes_unneeded_metrics(self):
        metrics = json.loads(self.outputs[ROOT / 'observability/unpoller/metrics.json'])
        context = read_yaml(ROLE / 'defaults/main.yml')
        context.update(microserver_unifi_metrics_enabled=True, microserver_unifi_metrics=metrics)
        config = yaml.safe_load(ENV.from_string((ROLE / 'templates/prometheus.yaml.j2').read_text()).render(**context))
        job = next(item for item in config['scrape_configs'] if item['job_name'] == 'microserver-unifi')
        policy = job['metric_relabel_configs'][0]
        self.assertEqual(policy['action'], 'keep')
        self.assertEqual(job['scrape_interval'], '60s')
        self.assertLess(len(metrics), 150)
        for dashboard in self.dashboards:
            for text in renderer.strings(dashboard):
                for metric in renderer.METRIC.findall(text):
                    self.assertIsNotNone(re.fullmatch(policy['regex'], metric))
        for name in ['unifipoller_dpi_application_present', 'unifipoller_country_present', 'unifipoller_firewall_rule_enabled']:
            self.assertIsNone(re.fullmatch(policy['regex'], name))

    def test_native_promql_parser_accepts_panel_and_variable_queries(self):
        tool = shutil.which('promtool')
        self.assertIsNotNone(tool, 'Pinned promtool is required')
        expressions = set()
        for dashboard in self.dashboards:
            for panel in renderer.panels(dashboard['panels']):
                for target in panel.get('targets', []):
                    if target.get('expr'):
                        expressions.add(substitute(target['expr'], dashboard))
            for variable in dashboard['templating']['list']:
                query = variable.get('query', '')
                if isinstance(query, str) and query.startswith('label_values(') and ',' in query:
                    expressions.add(substitute(query[len('label_values('):-1].rsplit(',', 1)[0], dashboard))
        self.assertGreater(len(expressions), 100)
        rules = dict(groups=[dict(name='unpoller-query-fixtures', rules=[
            dict(record=f'fixture_unpoller_query_{index}', expr=expression)
            for index, expression in enumerate(sorted(expressions))])])
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'queries.yaml'
            path.write_text(yaml.safe_dump(rules))
            result = subprocess.run([tool, 'check', 'rules', str(path)], capture_output=True, text=True, timeout=30)
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)


if __name__ == '__main__':
    unittest.main()
