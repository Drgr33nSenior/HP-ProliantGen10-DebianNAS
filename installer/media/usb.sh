#!/usr/bin/env bash
# macOS-only ISO writer. No building, formatting, force-unmount or auto-selection.
set -euo pipefail
root=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/../.." && pwd -P)
# shellcheck source=lib/common.sh
source "$root/lib/common.sh"

usage() {
  printf '%s\n' \
    'Usage: usb.sh list' \
    '       usb.sh [--execute] write ISO_FILE /dev/diskN' \
    'Default: read-only preview. Requires SHA256SUMS beside the ISO.' \
    'Execution requires root and a typed confirmation on /dev/tty.' \
    'WARNING: writing destroys existing contents of the selected USB disk.'
}

# Small OS adapters keep fixture tests away from real disk operations. There
# are no environment overrides, alternative device roots or bypass flags.
usb_platform() { [[ $(uname -s) == Darwin ]] || common::die 'USB writing is supported only on macOS'; }
usb_diskutil() { /usr/sbin/diskutil "$@"; }
usb_plist() { /usr/bin/plutil -convert json -o - -; }
usb_info() { usb_diskutil info -plist "$1" | usb_plist; }
usb_layout() { usb_diskutil list -plist "$1" | usb_plist; }
usb_media() { /usr/sbin/ioreg -a -r -c IOMedia | usb_plist; }
usb_size() { /usr/bin/stat -f '%z' "$1"; }
usb_source_device() { /bin/df -P "$1" | awk 'NR == 2 { print $1 }'; }
usb_lock_path() { printf '/var/run/microserver-usb-%s.lock\n' "${1##*/}"; }
usb_open() {
  [[ -b "$1" && ! -L "$1" && -c "$2" && ! -L "$2" ]] || common::die 'target device nodes are missing or unsafe'
  exec 9>"$2"
}
usb_copy() { /bin/dd if="$1" bs=1m >&9; }
usb_flush() { /bin/sync; }
usb_read() { /usr/bin/head -c "$1" "$2"; }
# macOS cmp can reject unequal regular-file sizes even with -n. Feed it a
# bounded stream instead; pipefail and cmp also reject errors/short reads.
usb_compare() { usb_read "$1" "$3" | /usr/bin/cmp "$2" -; }

usb_confirm() {
  local expected="$1" answer
  printf 'Type exactly: %s\n> ' "$expected" >/dev/tty || common::die 'an interactive controlling terminal is required'
  IFS= read -r answer </dev/tty || common::die 'confirmation was not received'
  [[ "$answer" == "$expected" ]] || common::die 'confirmation did not match; no disk was unmounted or written'
}

usb_image_check() {
  local iso="$1" name manifest hash actual
  name=${iso##*/}
  [[ "$name" =~ ^[A-Za-z0-9][A-Za-z0-9._-]*\.iso$ && -f "$iso" && ! -L "$iso" && -s "$iso" ]] ||
    common::die 'select a nonempty regular ISO with a simple .iso filename; symlinks are refused'
  manifest="${iso%/*}/SHA256SUMS"
  [[ -f "$manifest" && ! -L "$manifest" ]] || common::die 'the ISO needs a regular adjacent SHA256SUMS from its build'
  hash=$(awk -v name="$name" '
    NF == 2 && ($2 == name || $2 == "*" name) { count++; hash=$1 }
    END { if (count != 1) exit 1; print hash }
  ' "$manifest") || common::die 'SHA256SUMS must contain exactly one matching ISO entry'
  [[ "$hash" =~ ^[a-fA-F0-9]{64}$ ]] || common::die 'invalid ISO checksum entry'
  actual=$(common::sha256_file "$iso") || common::die 'cannot hash the ISO'
  [[ "$(printf '%s' "$hash" | tr 'A-F' 'a-f')" == "$actual" ]] || common::die 'ISO checksum mismatch; no device changes made'
  printf '%s\n' "$actual"
}

usb_identity() {
  local device="$1" bytes="$2" unmounted="${3:-false}" info layout media system identity
  [[ "$device" =~ ^/dev/disk[0-9]+$ && "$device" != /dev/disk0 ]] || common::die 'select an explicit whole /dev/diskN, never disk0, a slice, raw alias or symlink'
  info=$(usb_info "$device") || common::die 'cannot inspect selected disk'
  jq -e --arg device "$device" --argjson bytes "$bytes" '
    .DeviceNode == $device and .DeviceIdentifier == ($device|ltrimstr("/dev/")) and
    .WholeDisk == true and .Internal == false and .VirtualOrPhysical == "Physical" and
    .BusProtocol == "USB" and .Writable == true and
    (.MediaName|type == "string" and length > 0) and
    (.DeviceTreePath|type == "string" and length > 0) and
    (.TotalSize|type == "number" and . > 0 and . >= $bytes) and
    (.DeviceBlockSize|type == "number" and . > 0) and ($bytes % .DeviceBlockSize == 0)
  ' <<<"$info" >/dev/null || common::die 'require an identifiable, writable, external physical USB whole disk large enough for the ISO; unknown properties fail closed'
  layout=$(usb_layout "$device") || common::die 'cannot inspect target partitions'
  jq -e --arg disk "${device##*/}" --argjson unmounted "$unmounted" '
    (.AllDisksAndPartitions|type == "array" and length == 1) and
    .AllDisksAndPartitions[0].DeviceIdentifier == $disk and
    all(..|objects;
      ((.Content // "")|test("APFS|CoreStorage|RAID";"i")|not) and
      ((.MountPoint // "")|test("^/$|^/System(/|$)|^/private(/|$)")|not) and
      ($unmounted == false or (.MountPoint // "") == ""))
  ' <<<"$layout" >/dev/null || common::die 'refusing unknown layout, macOS system mounts, APFS, CoreStorage or RAID media; no automatic reformatting'
  system=$(usb_info /) || common::die 'cannot identify the running macOS system volume'
  jq -e --arg disk "${device##*/}" '
    (.DeviceIdentifier|type == "string") and (.ParentWholeDisk|type == "string") and
    .DeviceIdentifier != $disk and .ParentWholeDisk != $disk
  ' <<<"$system" >/dev/null || common::die 'target is the system disk or system-disk identity is unknown'
  media=$(usb_media) || common::die 'cannot inspect live IOKit media identity'
  identity=$(jq -ce --arg disk "${device##*/}" --argjson info "$info" '
    [..|objects|select(.["BSD Name"]? == $disk and .Whole? == true)] |
    select(length == 1) | .[0] |
    select((.IORegistryEntryID|type) == "number" and .IORegistryEntryID > 0 and .Size == $info.TotalSize and .Writable == true) |
    {device:$info.DeviceNode,name:$info.MediaName,bytes:$info.TotalSize,
      block_size:$info.DeviceBlockSize,device_tree:$info.DeviceTreePath,registry_id:.IORegistryEntryID}
  ' <<<"$media") || common::die 'live whole-media identity is missing, ambiguous or inconsistent'
  printf '%s\n' "$identity"
}

usb_source_check() {
  local iso="$1" target="$2" source info
  source=$(usb_source_device "$iso") || common::die 'cannot identify the ISO filesystem'
  [[ "$source" =~ ^/dev/disk[0-9]+(s[0-9]+)*$ ]] || common::die 'copy the ISO and checksum to a local disk first; unknown/network sources are refused'
  info=$(usb_info "$source") || common::die 'cannot resolve the ISO filesystem device'
  jq -e --arg disk "${target##*/}" '
    (.ParentWholeDisk|type == "string") and .ParentWholeDisk != $disk and .DeviceIdentifier != $disk
  ' <<<"$info" >/dev/null || common::die 'the ISO must not reside on the USB disk being overwritten'
}

usb_list() {
  local devices device info
  devices=$(usb_diskutil list -plist external physical | usb_plist) || common::die 'cannot list external physical disks'
  jq -e '.AllDisksAndPartitions|type == "array"' <<<"$devices" >/dev/null || common::die 'unexpected disk inventory'
  printf 'External physical USB disks (listing is not write approval):\n'
  while IFS= read -r device; do
    [[ "$device" =~ ^disk[0-9]+$ ]] || common::die 'invalid disk identifier in inventory'
    info=$(usb_info "/dev/$device") || common::die 'cannot inspect an external disk'
    jq -r 'select(.BusProtocol == "USB") |
      [.DeviceNode, (.MediaName // "UNKNOWN"), ((.TotalSize // "UNKNOWN")|tostring),
       (if .Writable == true then "writable" else "read-only/unknown" end)] | @tsv' <<<"$info"
  done < <(jq -r '.AllDisksAndPartitions[].DeviceIdentifier' <<<"$devices")
  printf 'Columns: device, media name, capacity in bytes, write status. No disk selected.\n'
}

usb_write() (
  local requested="$1" device="$2" execute="$3" iso bytes hash initial fresh raw lock completed=false touched=false
  [[ -f "$requested" && ! -L "$requested" ]] || common::die 'ISO must be a regular non-symlink file'
  iso="$(cd -- "$(dirname -- "$requested")" && pwd -P)/${requested##*/}"
  hash=$(usb_image_check "$iso") || exit 1
  bytes=$(usb_size "$iso") || common::die 'cannot read ISO size'
  [[ "$bytes" =~ ^[1-9][0-9]*$ ]] || common::die 'cannot determine ISO size'
  initial=$(usb_identity "$device" "$bytes") || exit 1
  usb_source_check "$iso" "$device"
  raw="/dev/r${device##*/}"
  printf 'ISO: %s\nISO bytes: %s\nSHA256: %s\nTarget: %s\n' "$iso" "$bytes" "$hash" "$initial"
  common::warn 'WRITING DESTROYS EXISTING CONTENTS OF THE SELECTED USB DISK. Back it up first.'
  common::print_command /usr/sbin/diskutil unmountDisk "$device"
  common::print_command /bin/dd "if=$iso" "of=$raw" bs=1m
  common::print_command /bin/sync
  printf ' + /usr/bin/head -c %q %q | /usr/bin/cmp %q -\n' "$bytes" "$raw" "$iso"
  common::print_command /usr/sbin/diskutil eject "$device"
  if [[ "$execute" == false ]]; then
    common::info 'Preview only: no unmount, write, synchronization or eject occurred.'
    exit 0
  fi
  common::require_root
  lock=$(usb_lock_path "$device") || exit 1
  mkdir -m 0700 "$lock" || common::die 'USB writer lock exists; inspect any previous writer, never remove an active lock'
  trap 'exec 9>&-; rmdir "$lock"; if [[ "$completed" != true ]]; then common::warn "USB operation incomplete (unmount/write started: $touched). Do not assume a bootable copy; no automatic retry or recovery was performed."; fi' EXIT
  trap 'exit 130' INT
  trap 'exit 143' TERM
  usb_confirm "ERASE $device $(jq -r .bytes <<<"$initial") ${hash:0:12}"
  [[ "$(usb_image_check "$iso")" == "$hash" && "$(usb_size "$iso")" == "$bytes" ]] || common::die 'ISO changed after preview'
  usb_source_check "$iso" "$device"
  fresh=$(usb_identity "$device" "$bytes") || exit 1
  [[ "$fresh" == "$initial" ]] || common::die 'USB identity changed after confirmation; start again'
  touched=true
  usb_diskutil unmountDisk "$device" || common::die 'unmount failed; nothing was written (force is not supported)'
  fresh=$(usb_identity "$device" "$bytes" true) || exit 1
  [[ "$fresh" == "$initial" ]] || common::die 'USB identity changed after unmount; nothing was written'
  usb_open "$device" "$raw" || common::die 'cannot open the target device; nothing was written'
  # Recheck after opening, then write through the held descriptor instead of
  # reopening a potentially reused disk number in dd. Do not hotplug during IO.
  fresh=$(usb_identity "$device" "$bytes" true) || exit 1
  [[ "$fresh" == "$initial" ]] || common::die 'USB identity changed while opening the device'
  common::info 'Writing ISO. On macOS, press Ctrl-T for dd progress; Ctrl-C aborts.'
  usb_copy "$iso" || common::die 'ISO write failed; USB contents are incomplete'
  exec 9>&-
  usb_flush || common::die 'synchronization failed; copy is not verified'
  # The partition map intentionally changed. The identity record excludes
  # partition UUIDs and labels; the external/system-disk guards still apply.
  fresh=$(usb_identity "$device" "$bytes") || exit 1
  [[ "$fresh" == "$initial" ]] || common::die 'USB identity changed before readback'
  [[ "$(usb_image_check "$iso")" == "$hash" && "$(usb_size "$iso")" == "$bytes" ]] || common::die 'ISO changed during writing; copy is not verified'
  usb_compare "$bytes" "$iso" "$raw" || common::die 'readback mismatch or read failure; do not boot this USB'
  [[ "$(usb_image_check "$iso")" == "$hash" && "$(usb_size "$iso")" == "$bytes" ]] || common::die 'ISO changed during readback; copy is not verified'
  [[ "$(usb_identity "$device" "$bytes")" == "$initial" ]] || common::die 'USB identity changed during readback'
  usb_diskutil eject "$device" || common::die 'bytes verified but eject failed; resolve open users and eject manually before removal'
  completed=true
  common::info 'USB bytes verified against the ISO and device ejected. UEFI/Secure Boot testing remains separate.'
)

main() {
  local execute=false
  if [[ ${1:-} == --execute ]]; then
    execute=true
    shift
  fi
  case ${1:-} in
    -h | --help)
      usage
      return
      ;;
    list | write) ;;
    *)
      usage >&2
      return 2
      ;;
  esac
  usb_platform
  common::require_command jq
  case "$1" in
    list)
      (($# == 1)) && [[ "$execute" == false ]] || common::die 'list takes no options or device and never executes writes'
      usb_list
      ;;
    write)
      (($# == 3)) || common::die 'write requires an explicit ISO and whole /dev/diskN'
      usb_write "$2" "$3" "$execute"
      ;;
  esac
}

if [[ ${BASH_SOURCE[0]} == "$0" ]]; then main "$@"; fi
