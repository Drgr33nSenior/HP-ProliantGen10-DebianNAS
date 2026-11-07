"""Allow only the commissioned NAS's new Tempo service and ingestion block."""
import copy
import json
from pathlib import Path
import subprocess
import sys

CURRENT = Path('/etc/microserver/monitoring/compose.yaml')
NGINX = Path('/etc/microserver/ui/nginx.conf')
BEGIN = '    # BEGIN workstation ingestion'
END = '    # END workstation ingestion'


def require_tempo_only(current, proposed):
    before, after = copy.deepcopy(current), copy.deepcopy(proposed)
    old = before['services'].pop('tempo', None)
    new = after['services'].pop('tempo', None)
    if new is None or before != after or (old is not None and old != new):
        raise ValueError('Review existing service drift or a changed Tempo service')


def split_ingestion(text):
    lines = text.splitlines()
    if BEGIN not in lines and END not in lines:
        return [line for line in lines if line.strip()], []
    if lines.count(BEGIN) != 1 or lines.count(END) != 1:
        raise ValueError('Ambiguous ingestion markers')
    start, stop = lines.index(BEGIN), lines.index(END)
    if stop <= start:
        raise ValueError('Invalid ingestion marker order')
    return [line for line in lines[:start] + lines[stop + 1:] if line.strip()], lines[start:stop + 1]


def require_ingestion_only(current, proposed):
    before, old = split_ingestion(current)
    after, new = split_ingestion(proposed)
    if not new or before != after or (old and old != new):
        raise ValueError('Review existing proxy drift or a changed ingestion block')


def load_compose(path):
    result = subprocess.run(['/usr/bin/docker-compose', '--project-name', 'microserver', '--file', str(path),
                             'config', '--format', 'json'], capture_output=True, timeout=20, check=True)
    if len(result.stdout) > 2097152:
        raise ValueError('Compose review size exceeded')
    return json.loads(result.stdout)


if __name__ == '__main__':
    try:
        mode, path = sys.argv[1:]
        if mode == 'compose':
            require_tempo_only(load_compose(CURRENT), load_compose(path))
        elif mode == 'nginx':
            require_ingestion_only(NGINX.read_text(), Path(path).read_text())
            subprocess.run(['/usr/sbin/nginx', '-e', 'stderr', '-t', '-c', path, '-g', 'user www-data; pid /dev/null;'],
                           check=True, timeout=20)
        else:
            raise ValueError('Unsupported validation mode')
    except (ValueError, TypeError, KeyError, IndexError, OSError, subprocess.SubprocessError):
        raise SystemExit('Telemetry apply refused: review configuration drift or validation failure') from None
