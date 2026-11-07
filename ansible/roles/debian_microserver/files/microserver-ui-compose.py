"""Refuse monitoring changes outside the three UI external URL settings."""
import copy
import json
import subprocess
import sys

CURRENT = '/etc/microserver/monitoring/compose.yaml'


def without_ui_urls(config):
    result = copy.deepcopy(config)
    services = result['services']
    for name in ('prometheus', 'alertmanager'):
        services[name]['command'] = [value for value in services[name]['command']
                                     if not value.startswith('--web.external-url=')]
    environment = services['grafana']['environment']
    for key in ('GF_SERVER_DOMAIN', 'GF_SERVER_ROOT_URL', 'GF_SECURITY_COOKIE_SECURE'):
        environment.pop(key, None)
    return result


def require_ui_only(current, proposed):
    grafana = proposed['services']['grafana']['environment']
    if grafana.get('GF_SERVER_ROOT_URL', '').startswith('https://') and str(grafana.get('GF_SECURITY_COOKIE_SECURE')).lower() != 'true':
        raise ValueError('HTTPS Grafana requires secure cookies')
    if without_ui_urls(current) != without_ui_urls(proposed):
        raise ValueError('Existing monitoring configuration differs outside UI external URLs')


def load_config(path):
    result = subprocess.run(['/usr/bin/docker-compose', '--project-name', 'microserver',
                             '--file', path, 'config', '--format', 'json'],
                            stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, timeout=20, check=True)
    if len(result.stdout) > 2097152:
        raise ValueError('Compose configuration exceeds the review bound')
    return json.loads(result.stdout)


if __name__ == '__main__':
    try:
        require_ui_only(load_config(CURRENT), load_config(sys.argv[1]))
    except (ValueError, TypeError, KeyError, IndexError, OSError, subprocess.SubprocessError):
        raise SystemExit('UI apply refused: review monitoring drift or Compose validation failure') from None
