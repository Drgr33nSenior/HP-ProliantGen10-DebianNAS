#!/usr/bin/env bash
# CI-only private validation binaries. Uses existing reviewed archive hashes.
set -euo pipefail
root=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd -P)
output=${1:?new private tools directory}
[[ $(uname -s) == Linux && $(uname -m) == x86_64 && ! -e $output ]] || exit 2
umask 077
mkdir -p "$output/bin" "$output/downloads"
output=$(cd -- "$output" && pwd -P)
while IFS=$'\t' read -r name url digest; do
  archive="$output/downloads/$name"
  curl --fail --silent --show-error --location --retry 2 --max-time 180 "$url" -o "$archive"
  printf '%s  %s\n' "$digest" "$archive" | sha256sum --check --status
  if [[ $name == shfmt ]]; then
    install -m 755 "$archive" "$output/bin/shfmt"
    continue
  fi
  mkdir "$output/$name"
  tar -xf "$archive" --strip-components=1 -C "$output/$name"
  case "$name" in
    shellcheck) ln -s "$output/$name/shellcheck" "$output/bin/shellcheck" ;;
    prometheus) ln -s "$output/$name/promtool" "$output/bin/promtool" ;;
    alertmanager)
      ln -s "$output/$name/alertmanager" "$output/bin/alertmanager"
      ln -s "$output/$name/amtool" "$output/bin/amtool"
      ;;
    xorriso)
      (
        cd "$output/$name"
        ./configure --prefix="$output/xorriso-install" --disable-libacl --disable-xattr --disable-libreadline >"$output/xorriso-configure.log" 2>&1
        make -j2 >"$output/xorriso-build.log" 2>&1
        make install >"$output/xorriso-install.log" 2>&1
      )
      ln -s "$output/xorriso-install/bin/xorriso" "$output/bin/xorriso"
      ;;
    *) exit 2 ;;
  esac
done < <(jq -r '.[] | [.name,.url,.sha256] | @tsv' "$root/tests/tools-linux.json")
