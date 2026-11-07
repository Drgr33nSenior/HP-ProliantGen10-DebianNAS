"""Permit only the workstation rule file and its read-only Prometheus mount."""
import copy
import json
from pathlib import Path
import re
import subprocess
import sys

COMPOSE = Path('/etc/microserver/monitoring/compose.yaml')
PROMETHEUS = Path('/etc/microserver/monitoring/prometheus.yaml')
OLD_RULES = 'rule_files: [/etc/prometheus/alerts.yaml]'
NEW_RULES = 'rule_files: [/etc/prometheus/alerts.yaml, /etc/prometheus/workstation-alerts.yaml]'
SOURCE = '/etc/microserver/monitoring/workstation-alerts.yaml'
TARGET = '/etc/prometheus/workstation-alerts.yaml'


def strip_rule_reference(text, required=False):
    lines = [line for line in text.splitlines() if line.strip()]
    if lines.count(NEW_RULES) == 1 and OLD_RULES not in lines:
        return [OLD_RULES if line == NEW_RULES else line for line in lines]
    if not required and lines.count(OLD_RULES) == 1 and NEW_RULES not in lines:
        return lines
    raise ValueError('Unexpected Prometheus rule declarations')


def require_rule_only(current, proposed):
    if strip_rule_reference(current) != strip_rule_reference(proposed, required=True):
        raise ValueError('Existing Prometheus configuration drift')


def remove_mount(config, required=False):
    result = copy.deepcopy(config)
    volumes = result['services']['prometheus']['volumes']
    matches = [volume for volume in volumes if volume.get('target') == TARGET]
    if len(matches) != 1:
        if not required and not matches:
            return result
        raise ValueError('Unexpected workstation rule mounts')
    volume = matches[0]
    if (volume.get('type') != 'bind' or volume.get('source') != SOURCE or volume.get('read_only') is not True or
            set(volume) - {'type', 'source', 'target', 'read_only', 'bind'} or
            volume.get('bind', {}) not in [{}, {'create_host_path': True}]):
        raise ValueError('Workstation rules require the exact read-only bind')
    volumes.remove(volume)
    return result


def require_mount_only(current, proposed):
    if remove_mount(current) != remove_mount(proposed, required=True):
        raise ValueError('Existing Compose service drift')


def load_compose(path):
    result = subprocess.run(['/usr/bin/docker-compose', '--project-name', 'microserver', '--file', str(path),
                             'config', '--format', 'json'], check=True, capture_output=True, timeout=20)
    if len(result.stdout) > 2097152:
        raise ValueError('Compose review size exceeded')
    return json.loads(result.stdout)


if __name__ == '__main__':
    try:
        mode, path, *image = sys.argv[1:]
        if mode == 'compose' and not image:
            require_mount_only(load_compose(COMPOSE), load_compose(path))
        elif mode == 'prometheus' and len(image) == 1:
            require_rule_only(PROMETHEUS.read_text(), Path(path).read_text())
            if not re.fullmatch(r'docker[.]io/prom/prometheus:v[0-9.]+@sha256:[a-f0-9]{64}', image[0]):
                raise ValueError('Expected the pinned Prometheus image')
            subprocess.run(['/usr/bin/docker', 'run', '--rm', '--pull', 'never', '--network', 'none',
                '--read-only', '--user', '0:0', '--cap-drop', 'ALL', '--security-opt', 'no-new-privileges',
                '--memory', '128m', '--cpus', '0.25', '--pids-limit', '64', '--volume', path + ':/candidate.yaml:ro',
                '--entrypoint', '/bin/promtool', image[0], 'check', 'config', '--syntax-only', '/candidate.yaml'],
                check=True, capture_output=True, timeout=60)
        else:
            raise ValueError('Unexpected validation arguments')
    except (ValueError, TypeError, KeyError, IndexError, OSError, subprocess.SubprocessError):
        raise SystemExit('Workstation rule apply refused: review configuration drift or native validation failure') from None
