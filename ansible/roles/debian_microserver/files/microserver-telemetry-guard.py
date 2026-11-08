"""Allow only the NAS receiver's Tempo and optional Pyroscope services and ingestion block."""
import copy
import json
from pathlib import Path
import subprocess
import sys

CURRENT = Path('/etc/microserver/monitoring/compose.yaml')
NGINX = Path('/etc/microserver/ui/nginx.conf')
BEGIN = '    # BEGIN workstation ingestion'
END = '    # END workstation ingestion'
PROFILES_BEGIN = '        # BEGIN workstation profiles'
PROFILES_END = '        # END workstation profiles'


def require_receiver_services_only(current, proposed):
    """Tempo is required; Pyroscope may be added once and never changed or dropped."""
    before, after = copy.deepcopy(current), copy.deepcopy(proposed)
    old = before['services'].pop('tempo', None)
    new = after['services'].pop('tempo', None)
    if new is None or (old is not None and old != new):
        raise ValueError('Review a missing or changed Tempo service')
    old = before['services'].pop('pyroscope', None)
    new = after['services'].pop('pyroscope', None)
    if (old is not None and old != new) or before != after:
        raise ValueError('Review existing service drift or a changed Pyroscope service')


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


def split_profiles(block):
    """Separate the optional profiles routes from the rest of the ingestion block."""
    if PROFILES_BEGIN not in block and PROFILES_END not in block:
        return block, []
    if block.count(PROFILES_BEGIN) != 1 or block.count(PROFILES_END) != 1:
        raise ValueError('Ambiguous profiles markers')
    start, stop = block.index(PROFILES_BEGIN), block.index(PROFILES_END)
    if stop <= start:
        raise ValueError('Invalid profiles marker order')
    return block[:start] + block[stop + 1:], block[start:stop + 1]


def significant(lines):
    # Template conditionals leave blank lines that nginx ignores.
    return [line for line in lines if line.strip()]


def require_ingestion_only(current, proposed):
    before, old = split_ingestion(current)
    after, new = split_ingestion(proposed)
    if not new or before != after:
        raise ValueError('Review existing proxy drift or a changed ingestion block')
    old_core, old_profiles = split_profiles(old)
    new_core, new_profiles = split_profiles(new)
    # Adding the profiles routes is the only permitted change to an installed block.
    if (old and significant(old_core) != significant(new_core)) or \
            (old_profiles and significant(old_profiles) != significant(new_profiles)):
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
            require_receiver_services_only(load_compose(CURRENT), load_compose(path))
        elif mode == 'nginx':
            require_ingestion_only(NGINX.read_text(), Path(path).read_text())
            subprocess.run(['/usr/sbin/nginx', '-e', 'stderr', '-t', '-c', path, '-g', 'user www-data; pid /dev/null;'],
                           check=True, timeout=20)
        else:
            raise ValueError('Unsupported validation mode')
    except (ValueError, TypeError, KeyError, IndexError, OSError, subprocess.SubprocessError):
        raise SystemExit('Telemetry apply refused: review configuration drift or validation failure') from None
