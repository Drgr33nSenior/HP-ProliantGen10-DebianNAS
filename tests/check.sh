#!/usr/bin/env bash
# Controller validation, including synthetic loopback log fixtures. No real targets or containers.
set -euo pipefail
root=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd -P)
cd "$root"
for tool in docker promtool amtool loki vector ansible-playbook shellcheck shfmt jq gpg gpgv xorriso; do
  command -v "$tool" >/dev/null || {
    printf 'BLOCKED: source validation requires %s\n' "$tool" >&2
    exit 1
  }
done
docker compose version >/dev/null
[[ -n ${MICROSERVER_PYTHON:-} && -x $MICROSERVER_PYTHON ]] || {
  echo 'BLOCKED: set MICROSERVER_PYTHON to the configured Python with Jinja2/PyYAML' >&2
  exit 1
}
scripts=(tests/check.sh tests/test_microserver.sh tests/prepare-tools.sh tests/test_media.sh tests/test_usb.sh installer/media/media.sh installer/media/usb.sh installer/media/finish.sh lib/common.sh)
for script in "${scripts[@]}"; do bash -n "$script"; done
shellcheck "${scripts[@]}"
shfmt -d -i 2 -ci "${scripts[@]}"
ansible-playbook -i ansible/microserver.inventory.example.ini --syntax-check ansible/microserver.yml
ansible-playbook -i ansible/microserver.inventory.example.ini --syntax-check ansible/owner-install.yml
bash tests/test_microserver.sh
"$MICROSERVER_PYTHON" tests/home-lab/check_logs.py
bash tests/test_media.sh
bash tests/test_usb.sh
