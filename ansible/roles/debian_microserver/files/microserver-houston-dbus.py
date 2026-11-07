#!/usr/bin/python3
"""Pinned vendor Houston API with local-only notifications and no payload logs."""
import argparse
import hashlib
import json
import logging
from pathlib import Path
import runpy
import sqlite3
import stat
import sys

VENDOR = Path('/opt/45drives/houston')
HASHES = {
    'dbus_server.py': 'c439272d3339f5083066d4c6c781cc0b9572ada2e47b8bad4eb97c6c9d0a7d5f',
    'notification_utils.py': '24d175eeb843998a916812c4f193ce6c023ba9e1d6a328122dfd6a8f199cfd4f',
    'setup_notification_db.py': 'a81756fd59cfd9030d7d0c8db4df19beab895545b4d6c7c542c9303c4c9bd271',
}
DATABASE = Path('/var/lib/sqlite/45drives/notifications.db')
COLUMNS = {'id', 'timestamp', 'event', 'pool', 'vdev', 'state', 'health', 'guid', 'errors',
           'severity', 'received', 'dismissed', 'fileSystem', 'snapShot', 'replicationDestination',
           'taskName', 'taskType'}


def safe_paths():
    for name, expected in HASHES.items():
        path = VENDOR / name
        for candidate in (path, *path.parents):
            info = candidate.lstat()
            if stat.S_ISLNK(info.st_mode) or info.st_uid != 0 or info.st_mode & 0o022:
                raise RuntimeError('untrusted vendor code path')
        if hashlib.sha256(path.read_bytes()).hexdigest() != expected:
            raise RuntimeError('vendor code changed; review before enabling Houston')
    for path in (*reversed(DATABASE.parent.parents), DATABASE.parent, DATABASE):
        if not path.exists() and not path.is_symlink():
            continue
        info = path.lstat()
        if stat.S_ISLNK(info.st_mode) or info.st_uid != 0 or info.st_mode & 0o022:
            raise RuntimeError('untrusted notification database path')


def setup():
    safe_paths()
    if DATABASE.exists():
        with sqlite3.connect('file:' + str(DATABASE) + '?mode=ro', uri=True) as connection:
            columns = {row[1] for row in connection.execute('PRAGMA table_info(notifications)')}
            if columns and columns != COLUMNS:
                raise RuntimeError('retain existing database; schema migration requires review')
    runpy.run_path(str(VENDOR / 'setup_notification_db.py'), init_globals={'print': lambda *a, **kw: None})
    DATABASE.chmod(0o600)


def disabled(*args, **kwargs):
    return {'status': 'disabled', 'message': 'This NAS delivers notifications only to the local UI.'}


def serve():
    safe_paths()
    logging.getLogger().addHandler(logging.NullHandler())
    sys.path.insert(0, str(VENDOR))
    import notification_utils as vendor
    vendor.print = lambda *a, **kw: None
    vendor.sendEmailNotification = disabled
    vendor.sendTestEmail = disabled
    vendor.updateSMTPConfig = lambda *a, **kw: json.dumps(disabled())
    vendor.resetMsmtpData = lambda *a, **kw: json.dumps(disabled())
    vendor.fetchMsmtpDetails = lambda: json.dumps({})
    original_store = vendor.store_notification

    def confirmed_store(message):
        value = original_store(message)
        if not value:
            # Vendor otherwise swallows a failed insert and acknowledges delivery.
            raise RuntimeError('notification persistence failed')
        return value

    # The pinned server catches store errors. Change only that known handler so
    # failures reach the caller; all vendor UI methods and signals remain native.
    source = (VENDOR / 'dbus_server.py').read_text()
    old = 'print(f"[ERROR] store_notification failed: {e}", flush=True)'
    if source.count(old) != 1:
        raise RuntimeError('vendor failure handler changed')
    vendor.store_notification = confirmed_store
    scope = {'__name__': '__main__', '__file__': str(VENDOR / 'dbus_server.py'),
             'print': lambda *a, **kw: None}
    exec(compile(source.replace(old, 'raise RuntimeError("notification persistence failed") from None'),
                 str(VENDOR / 'dbus_server.py'), 'exec'), scope)


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--setup', action='store_true')
    args = parser.parse_args()
    try:
        setup() if args.setup else serve()
    except Exception:
        print('STOP: Houston initialization failed; review protected local state', file=sys.stderr)
        sys.exit(1)
