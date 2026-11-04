#!/usr/bin/env bash
# Offline only: no inventory connection, containers, disks or service activation.
set -euo pipefail
root=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd -P)
if [[ -z ${MICROSERVER_PYTHON:-} ]]; then
  echo 'SKIP: MicroServer template checks require MICROSERVER_PYTHON with Jinja2/PyYAML'
  exit 0
fi
work=$(mktemp -d)
trap 'rm -rf -- "$work"' EXIT
"$MICROSERVER_PYTHON" "$root/tests/home-lab/check_microserver.py" "$work"
if command -v docker >/dev/null 2>&1 && docker compose version >/dev/null 2>&1; then
  docker compose --project-name microserver --file "$work/compose.yaml" config --quiet
else
  echo 'SKIP: MicroServer Compose schema check requires Docker Compose (no daemon needed)'
fi
if command -v promtool >/dev/null 2>&1; then
  promtool check config --syntax-only "$work/prometheus.yaml"
  cp "$root/ansible/roles/debian_microserver/files/alerts.yaml" "$work/alerts.yaml"
  cp "$root/tests/fixtures/microserver/alerts_test.yaml" "$work/alerts_test.yaml"
  (cd "$work" && promtool test rules alerts_test.yaml)
else
  echo 'SKIP: MicroServer Prometheus configuration and alert checks require promtool'
fi
if command -v amtool >/dev/null 2>&1; then
  amtool check-config "$root/ansible/roles/debian_microserver/files/alertmanager.yaml"
else
  echo 'SKIP: MicroServer Alertmanager configuration check requires amtool'
fi
