#!/usr/bin/python3
"""Bounded loopback Alertmanager receiver for the installed Houston D-Bus API."""
import datetime
import hashlib
from http.server import BaseHTTPRequestHandler, HTTPServer
import json
import socket
import sys
import time

MAX_BODY = 1024 * 1024
MAX_ALERTS = 100


def messages(payload):
    if not isinstance(payload, dict) or payload.get('version') != '4':
        raise ValueError('unsupported webhook')
    truncated = payload.get('truncatedAlerts', 0)
    if type(truncated) is not int or truncated < 0:
        raise ValueError('invalid truncation count')
    alerts = payload.get('alerts')
    if not isinstance(alerts, list) or not 1 <= len(alerts) <= MAX_ALERTS:
        raise ValueError('invalid alert count')
    output = []
    for alert in alerts:
        if not isinstance(alert, dict) or alert.get('status') not in ('firing', 'resolved'):
            raise ValueError('invalid alert status')
        for field in ('labels', 'annotations'):
            value = alert.get(field)
            if not isinstance(value, dict) or len(value) > 100 or any(
                    not isinstance(k, str) or not isinstance(v, str) or len(k) > 256 or len(v) > 16384
                    for k, v in value.items()):
                raise ValueError('invalid alert metadata')
        name = alert['labels'].get('alertname', '')
        if not name or len(name) > 256 or not isinstance(alert.get('startsAt'), str):
            raise ValueError('missing alert identity')
        event = dict(status=alert['status'], alerts=[alert], commonLabels=alert['labels'],
                     commonAnnotations=alert['annotations'])
        key = hashlib.sha256(json.dumps(event, sort_keys=True).encode()).hexdigest()
        severity = alert['labels'].get('severity', 'warning') if alert['status'] == 'firing' else 'info'
        output.append((key, dict(timestamp=datetime.datetime.now(datetime.timezone.utc).isoformat(),
                                 event=json.dumps(event, separators=(',', ':')), severity=severity,
                                 subject=name, text=alert['annotations'].get('summary', name))))
    if truncated:
        # Preserve the included alerts and make the omitted count visible locally.
        warning = dict(status='firing', labels=dict(alertname='MicroserverHoustonBatchTruncated', severity='warning'),
                       annotations=dict(summary=f'Alertmanager omitted {truncated} alerts from this group; inspect Alertmanager.'),
                       startsAt=datetime.datetime.now(datetime.timezone.utc).isoformat())
        _, message = messages(dict(version='4', alerts=[warning]))[0]
        key = hashlib.sha256(json.dumps([payload.get('groupKey'), truncated], sort_keys=True).encode()).hexdigest()
        output.append((key, message))
    return output


def deliver(message):
    import dbus
    bus = dbus.SystemBus(private=True)
    try:
        remote = bus.get_object('org._45drives.Houston', '/org/_45drives/Houston', introspect=False)
        method = remote.get_dbus_method('ForwardMessage', 'org._45drives.Houston')
        method(json.dumps(message), timeout=10)
    finally:
        bus.close()


class Handler(BaseHTTPRequestHandler):
    protocol_version = 'HTTP/1.0'
    seen = {}

    def setup(self):
        self.request.settimeout(10)
        super().setup()

    def log_message(self, *args):
        # Alert bodies, credentials and HTTP headers never enter the journal.
        pass

    def reply(self, code):
        self.send_response(code)
        self.send_header('Content-Length', '0')
        self.end_headers()

    def do_GET(self):
        self.reply(200 if self.path == '/ready' else 404)

    def do_POST(self):
        if self.path != '/webhook':
            return self.reply(404)
        lengths = self.headers.get_all('Content-Length', [])
        if self.headers.get('Transfer-Encoding') or len(lengths) != 1 or not lengths[0].isdigit():
            return self.reply(400)
        length = int(lengths[0])
        if not 0 < length <= MAX_BODY:
            return self.reply(413)
        if self.headers.get_content_type() != 'application/json':
            return self.reply(415)
        try:
            body = self.rfile.read(length)
            if len(body) != length:
                return self.reply(400)
            pending = messages(json.loads(body))
        except (ValueError, UnicodeError, socket.timeout):
            return self.reply(400)
        try:
            now = time.monotonic()
            type(self).seen = {k: v for k, v in self.seen.items() if now - v < 300}
            for key, message in pending:
                if key not in self.seen:
                    deliver(message)
                    if len(self.seen) >= 4096:
                        self.seen.pop(next(iter(self.seen)))
                    self.seen[key] = now
        except Exception:
            print('HOUSTON_FORWARD=failed; Alertmanager retry required', file=sys.stderr, flush=True)
            return self.reply(503)
        self.reply(204)


if __name__ == '__main__':
    HTTPServer(('127.0.0.1', 5002), Handler).serve_forever()
