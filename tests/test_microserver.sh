#!/usr/bin/env bash
# Offline by default; optional native fixtures use owned local containers only.
set -euo pipefail
root=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd -P)
if [[ -z ${MICROSERVER_PYTHON:-} ]]; then
  echo 'SKIP: MicroServer template checks require MICROSERVER_PYTHON with Jinja2/PyYAML'
  exit 0
fi
work=$(mktemp -d)
trap 'rm -rf -- "$work"' EXIT
"$MICROSERVER_PYTHON" "$root/tests/home-lab/check_microserver.py" "$work"
"$MICROSERVER_PYTHON" "$root/tests/home-lab/check_ui.py"
"$MICROSERVER_PYTHON" "$root/tests/home-lab/check_telemetry.py"
"$MICROSERVER_PYTHON" "$root/tests/home-lab/check_workstation.py"
"$MICROSERVER_PYTHON" "$root/tests/home-lab/check_zfs.py"
"$MICROSERVER_PYTHON" "$root/tests/home-lab/check_zfs_upgrade.py"
"$MICROSERVER_PYTHON" "$root/tests/home-lab/check_tls.py"
"$MICROSERVER_PYTHON" "$root/tests/home-lab/check_cockpit.py"
"$MICROSERVER_PYTHON" "$root/tests/home-lab/check_houston.py"
"$MICROSERVER_PYTHON" "$root/tests/home-lab/check_zfs_write.py"
"$MICROSERVER_PYTHON" "$root/tests/home-lab/check_registry.py"
"$MICROSERVER_PYTHON" "$root/tests/home-lab/check_identity.py"
"$MICROSERVER_PYTHON" "$root/tests/home-lab/check_receivers_native.py"
if [[ $(uname -s) == Linux ]] && command -v systemd-analyze >/dev/null 2>&1; then
  cp "$root/ansible/roles/debian_microserver/files/microserver-docker-metrics.service" "$work/"
  cp "$root/ansible/roles/debian_microserver/files/microserver-docker-metrics.timer" "$work/"
  cp "$root/tests/fixtures/microserver/docker.service" "$work/"
  SYSTEMD_UNIT_PATH="$work:" systemd-analyze verify --man=no --generators=no \
    "$work/microserver-docker-metrics.service" "$work/microserver-docker-metrics.timer"
fi
if command -v docker >/dev/null 2>&1 && docker compose version >/dev/null 2>&1; then
  docker compose --project-name microserver --file "$work/compose.yaml" config --quiet
  docker compose --project-name microserver --file "$work/compose-unifi.yaml" config --quiet
else
  echo 'SKIP: MicroServer Compose schema check requires Docker Compose (no daemon needed)'
fi
if command -v promtool >/dev/null 2>&1; then
  promtool check config --syntax-only "$work/prometheus.yaml"
  promtool check config --syntax-only "$work/prometheus-unifi.yaml"
  cp "$root/ansible/roles/debian_microserver/files/alerts.yaml" "$work/alerts.yaml"
  cp "$root/tests/fixtures/microserver/alerts_test.yaml" "$work/alerts_test.yaml"
  (cd "$work" && promtool test rules alerts_test.yaml)
  promtool test rules "$work/dashboard_test.yaml"
  promtool test rules "$work/network_test.yaml"
  cp "$root/observability/config/workstation-alerts.yaml" "$work/workstation-alerts.yaml"
  cp "$root/tests/fixtures/microserver/workstation_alerts_test.yaml" "$work/workstation_alerts_test.yaml"
  (cd "$work" && promtool test rules workstation_alerts_test.yaml)
  cp "$root/tests/fixtures/microserver/zfs_alerts_test.yaml" "$work/zfs_alerts_test.yaml"
  (cd "$work" && promtool test rules zfs_alerts_test.yaml)
  promtool check metrics <"$work/docker_metrics.prom"
else
  echo 'SKIP: MicroServer Prometheus configuration and alert checks require promtool'
fi
if command -v amtool >/dev/null 2>&1; then
  amtool check-config "$root/ansible/roles/debian_microserver/files/alertmanager.yaml"
else
  echo 'SKIP: MicroServer Alertmanager configuration check requires amtool'
fi
