#!/usr/bin/env bash
# Controller-only source validation. Never installs, connects to a host or starts containers.
set -euo pipefail
root=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd -P)
cd "$root"
for tool in docker promtool amtool ansible-playbook shellcheck shfmt; do
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
scripts=(tests/check.sh tests/test_microserver.sh tests/prepare-tools.sh)
for script in "${scripts[@]}"; do bash -n "$script"; done
shellcheck "${scripts[@]}"
shfmt -d -i 2 -ci "${scripts[@]}"
ansible-playbook -i ansible/microserver.inventory.example.ini --syntax-check ansible/microserver.yml
bash tests/test_microserver.sh
