#!/usr/bin/env bash
# Real vendor signature checks and file-only fixtures. No devices or live roots.
set -euo pipefail
root=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd -P)
# shellcheck source=installer/media/media.sh
source "$root/installer/media/media.sh"
work=$(mktemp -d)
trap 'rm -rf -- "$work"' EXIT
mkdir "$work/vendor"
cp "$root/tests/fixtures/media/debian-SHA256SUMS" "$work/vendor/SHA256SUMS"
cp "$root/tests/fixtures/media/debian-SHA256SUMS.sign" "$work/vendor/SHA256SUMS.sign"
media_verify_metadata "$work/vendor" >/dev/null
cp "$work/vendor/SHA256SUMS" "$work/original-sums"
printf 'tampered\n' >>"$work/vendor/SHA256SUMS"
if (media_verify_metadata "$work/vendor") >"$work/rejected-signature.log" 2>&1; then exit 1; fi
if (media_verify "$work/vendor") >"$work/rejected-source.log" 2>&1; then exit 1; fi
cp "$work/original-sums" "$work/vendor/SHA256SUMS"
printf 'not the vendor ISO\n' >"$work/vendor/$(media_lock filename)"
if (media_verify "$work/vendor") >"$work/rejected-iso.log" 2>&1; then exit 1; fi
if (media_build "$work/vendor" "$work/rejected-build") >"$work/rejected-build.log" 2>&1; then exit 1; fi
[[ ! -e $work/rejected-build ]]
if (media_new_directory "$work/vendor") >"$work/rejected-reuse.log" 2>&1; then exit 1; fi
(
  original_lock_sha=$(media_lock sha256)
  media_lock() {
    case $1 in
      sha256) printf '%064d\n' 0 ;;
      *) jq -er --arg key "$1" '.[$key]' "$root/installer/media/debian.json" ;;
    esac
  }
  if (media_verify_metadata "$work/vendor") >"$work/rejected-pin.log" 2>&1; then exit 1; fi
  [[ $original_lock_sha != "$(media_lock sha256)" ]]
)
media_payload "$work/payload"
[[ -f $work/payload/ansible/microserver.yml && -f $work/payload/observability/versions.json ]]
[[ ! -e $work/payload/test-results && ! -e $work/payload/.git && ! -e $work/payload/ansible/inventory.local.ini ]]
while read -r hash relative; do
  [[ $(common::sha256_file "$work/payload/$relative") == "$hash" ]]
done <"$work/payload/SOURCE.sha256"
if (media_payload_file ../escape) >/dev/null 2>&1; then exit 1; fi
check_linked_parent() {
  local root="$work/linked-repository"
  mkdir -p "$root/real"
  printf 'fixture\n' >"$root/real/source.txt"
  ln -s real "$root/linked"
  if (media_payload_file linked/source.txt) >/dev/null 2>&1; then exit 1; fi
}
check_linked_parent
# The late hook copies into a disposable root, never the real /target tree.
# shellcheck source=installer/media/finish.sh
source "$root/installer/media/finish.sh"
finish_source() { printf '%s/payload\n' "$work"; }
finish_target() { printf '%s/target\n' "$work"; }
chown() { [[ $* == "-R 0:0 $work/target/opt/microserver-installer" ]]; }
if ! command -v sha256sum >/dev/null 2>&1; then
  sha256sum() {
    [[ $* == '-c SOURCE.sha256' ]] || return 1
    local hash relative
    while read -r hash relative; do [[ $(common::sha256_file "$relative") == "$hash" ]] || return 1; done <SOURCE.sha256
  }
  # Keep the common hash helper from recursively selecting this fixture adapter.
  common::sha256_file() { shasum -a 256 "$1" | awk '{print $1}'; }
fi
mkdir -p "$work/target/etc/apt/apt.conf.d"
finish_main >"$work/late-hook.log"
cmp "$root/installer/media/80microserver-minimal" "$work/target/etc/apt/apt.conf.d/80microserver-minimal"
if (finish_main) >"$work/rejected-repeat.log" 2>&1; then exit 1; fi
finish_target() { printf '%s/tampered-target\n' "$work"; }
mkdir -p "$work/tampered-target/etc/apt/apt.conf.d"
printf 'tampered\n' >>"$work/payload/README.md"
if (finish_main) >"$work/rejected-payload.log" 2>&1; then exit 1; fi
[[ ! -e $work/tampered-target/opt/microserver-installer ]]
printf 'Media authentication, payload and late-hook fixtures passed; no real target or device operations ran\n'
