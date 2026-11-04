#!/usr/bin/env bash
# Only OS adapters are mocked. No diskutil, device open, unmount or eject runs.
set -euo pipefail
root=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd -P)
# shellcheck source=installer/media/usb.sh
source "$root/installer/media/usb.sh"
work=$(mktemp -d)
trap 'rm -rf -- "$work"' EXIT
mkdir "$work/source with spaces"
iso="$work/source with spaces/microserver-test.iso"
# A regular file, not a bootable ISO: test copy/checksum/length semantics only.
dd if=/dev/zero of="$iso" bs=4096 count=1 2>/dev/null
printf '%s  %s\n' "$(common::sha256_file "$iso")" "${iso##*/}" >"${iso%/*}/SHA256SUMS"
device=/dev/disk8
policy='{}'
layout_policy='{}'
phase=normal
usb_platform() { :; }
usb_plist() { command cat; }
common::require_root() { [[ "$phase" != nonroot ]] || common::die 'fixture non-root'; }
usb_size() { wc -c <"$1" | tr -d ' '; }
usb_source_device() { if [[ "$phase" == source-on-target ]]; then printf '/dev/disk8s1\n'; else printf '/dev/disk3s1\n'; fi; }
usb_info() {
  case "$1" in
    /dev/disk8)
      # diskutil uses WholeDisk; IOKit below uses Whole. Do not conflate them.
      jq -cn --argjson policy "$policy" --arg phase "$phase" '{DeviceNode:"/dev/disk8",DeviceIdentifier:"disk8",ParentWholeDisk:"disk8",
        WholeDisk:true,Internal:false,VirtualOrPhysical:"Physical",BusProtocol:"USB",Writable:true,
        MediaName:"Fixture Flash Drive",DeviceTreePath:"IODeviceTree:/fixture/usb",TotalSize:8192,DeviceBlockSize:512} + $policy |
        if $phase == "missing-disk-whole" then del(.WholeDisk) else . end'
      ;;
    / | /dev/disk3s1) printf '{"DeviceIdentifier":"disk3s1","ParentWholeDisk":"disk3"}\n' ;;
    /dev/disk8s1) printf '{"DeviceIdentifier":"disk8s1","ParentWholeDisk":"disk8"}\n' ;;
    *) return 1 ;;
  esac
}
usb_layout() {
  [[ "$phase" != failed-inventory ]] || return 1
  if [[ "$phase" == remounted && -e "$work/unmounted" ]]; then
    printf '{"AllDisksAndPartitions":[{"DeviceIdentifier":"disk8","MountPoint":"/Volumes/fixture"}]}\n'
    return
  fi
  jq -cn --argjson p "$layout_policy" '{AllDisksAndPartitions:[{DeviceIdentifier:"disk8",Content:"FDisk_partition_scheme",Partitions:[{DeviceIdentifier:"disk8s1",Content:"DOS_FAT_32"}]}]} * $p'
}
usb_media() {
  local id=1234
  if [[ "$phase" == missing-media ]]; then
    printf '[]\n'
    return
  fi
  if [[ "$phase" == duplicate-media ]]; then
    printf '[{"BSD Name":"disk8","Whole":true},{"BSD Name":"disk8","Whole":true}]\n'
    return
  fi
  if [[ "$phase" == wrong-media-whole ]]; then
    printf '[{"BSD Name":"disk8","WholeDisk":true,"Size":8192,"Writable":true,"IORegistryEntryID":1234}]\n'
    return
  fi
  [[ ! -f "$work/replaced" ]] || id=5678
  jq -cn --argjson id "$id" '[{"BSD Name":"disk8",Whole:true,Size:8192,Writable:true,IORegistryEntryID:$id}]'
}
usb_lock_path() { printf '%s/lock\n' "$work"; }
usb_confirm() {
  printf 'confirm\n' >>"$work/actions"
  [[ "$1" == "ERASE /dev/disk8 8192 "* ]] || return 1
  case "$phase" in
    cancel) common::die 'fixture confirmation cancelled' ;;
    replug-confirm) touch "$work/replaced" ;;
    mutate-iso) printf x >>"$iso" ;;
  esac
}
usb_diskutil() {
  printf '%s\n' "$*" >>"$work/actions"
  case "$1" in
    list)
      [[ "$*" == 'list -plist external physical' ]] || return 1
      printf '{"AllDisksAndPartitions":[{"DeviceIdentifier":"disk8"}]}\n'
      ;;
    unmountDisk)
      [[ "$2" == /dev/disk8 && "$phase" != failed-unmount ]] || return 1
      touch "$work/unmounted"
      [[ "$phase" != replug-unmount ]] || touch "$work/replaced"
      ;;
    eject) [[ "$2" == /dev/disk8 && "$phase" != failed-eject ]] ;;
    *) return 99 ;;
  esac
}
usb_open() {
  [[ "$1" == /dev/disk8 && "$2" == /dev/rdisk8 ]] || return 1
  printf 'open\n' >>"$work/actions"
  exec 9>"$work/usb-file"
  [[ "$phase" != replug-open ]] || touch "$work/replaced"
}
usb_copy() {
  printf 'write\n' >>"$work/actions"
  [[ "$phase" != failed-write ]] || return 1
  if [[ "$phase" == signal ]]; then kill -TERM "$BASHPID"; fi
  command cat "$1" >&9
}
usb_flush() {
  printf 'flush\n' >>"$work/actions"
  [[ "$phase" != failed-sync ]]
}
usb_read() {
  printf 'compare\n' >>"$work/actions"
  [[ "$1" == 4096 && "$2" == /dev/rdisk8 && "$phase" != failed-readback ]] || return 1
  if [[ "$phase" == short-read ]]; then
    printf short
    return
  fi
  if [[ "$phase" == corrupt-read ]]; then printf corrupt; fi
  # Trailing device capacity is intentionally ignored; compare exactly ISO bytes.
  printf 'unwritten trailing capacity' >>"$work/usb-file"
  /usr/bin/head -c "$1" "$work/usb-file"
}
reset_case() {
  phase=normal
  policy='{}'
  layout_policy='{}'
  rm -f "$work/actions" "$work/replaced" "$work/usb-file" "$work/unmounted"
}
reject() {
  if (main --execute write "$iso" "$device") >"$work/output" 2>&1; then
    printf 'unsafe USB case accepted: %s %s %s\n' "$phase" "$policy" "$layout_policy" >&2
    exit 1
  fi
  [[ ! -d "$work/lock" ]] || {
    echo 'lock leaked' >&2
    exit 1
  }
}

main list >"$work/list"
grep -q '/dev/disk8' "$work/list"
[[ "$(cat "$work/actions")" == 'list -plist external physical' && ! -e "$work/usb-file" ]]
reset_case
main write "$iso" "$device" >"$work/preview" 2>&1
[[ ! -e "$work/actions" && ! -e "$work/usb-file" && ! -e "$work/lock" ]]
grep -q 'Preview only' "$work/preview"
for policy in '{"Internal":true}' '{"WholeDisk":false}' '{"VirtualOrPhysical":"Virtual"}' \
  '{"WholeDisk":null}' '{"WholeDisk":"true"}' '{"WholeDisk":null,"Whole":true}' \
  '{"Writable":false}' '{"BusProtocol":"Thunderbolt"}' '{"TotalSize":2048}' \
  '{"Internal":null}' '{"DeviceTreePath":""}' '{"DeviceNode":"/dev/disk9"}' '{"DeviceBlockSize":8192}'; do
  reject
  [[ ! -e "$work/actions" ]]
done
reset_case
for device in /dev/disk0 /dev/disk8s1 /dev/rdisk8 /tmp/disk8; do reject; done
device=/dev/disk8
for layout_policy in \
  '{"AllDisksAndPartitions":[{"DeviceIdentifier":"disk8", "Content":"Apple_APFS"}]}' \
  '{"AllDisksAndPartitions":[{"DeviceIdentifier":"disk8", "Content":"Apple_CoreStorage"}]}' \
  '{"AllDisksAndPartitions":[{"DeviceIdentifier":"disk8", "MountPoint":"/"}]}' \
  '{"AllDisksAndPartitions":[]}'; do reject; done
reset_case
for phase in source-on-target missing-disk-whole failed-inventory missing-media duplicate-media wrong-media-whole nonroot cancel replug-confirm replug-unmount replug-open failed-unmount remounted; do
  reject
  if grep -q '^write$' "$work/actions" 2>/dev/null; then exit 1; fi
  rm -f "$work/actions" "$work/replaced" "$work/usb-file" "$work/unmounted"
done
reset_case
for phase in failed-write failed-sync failed-readback short-read corrupt-read signal; do
  # BASHPID is not present in macOS Bash 3.2; test signal trapping separately
  # only when the test shell can identify its subshell safely.
  if [[ "$phase" == signal && -z ${BASHPID:-} ]]; then
    printf 'SKIP: USB signal-injection fixture requires Bash 4+\n'
    continue
  fi
  reject
  if grep -q '^eject ' "$work/actions"; then exit 1; fi
  if grep -q 'USB bytes verified' "$work/output"; then exit 1; fi
  rm -f "$work/actions" "$work/usb-file"
done
reset_case
phase=failed-eject
reject
grep -q 'bytes verified but eject failed' "$work/output" || {
  tail -40 "$work/output"
  exit 1
}
reset_case
mkdir "$work/lock"
if (main --execute write "$iso" "$device") >/dev/null 2>&1; then exit 1; fi
[[ -d "$work/lock" && ! -e "$work/actions" ]]
rmdir "$work/lock"
main --execute write "$iso" "$device" >"$work/output" 2>&1
[[ "$(tr '\n' ' ' <"$work/actions")" == 'confirm unmountDisk /dev/disk8 open write flush compare eject /dev/disk8 ' ]]
grep -q 'USB bytes verified' "$work/output"
[[ ! -d "$work/lock" ]]
reset_case
cp "${iso%/*}/SHA256SUMS" "$work/checksums"
printf '%064d  %s\n' 0 "${iso##*/}" >"${iso%/*}/SHA256SUMS"
reject
[[ ! -e "$work/actions" ]]
mv "${iso%/*}/SHA256SUMS" "$work/wrong-checksum"
reject
[[ ! -e "$work/actions" ]]
cp "$work/checksums" "${iso%/*}/SHA256SUMS"
original_iso="$iso"
ln -s "$iso" "$work/link.iso"
iso="$work/link.iso"
reject
[[ ! -e "$work/actions" ]]
iso="$original_iso"
printf '%s  %s\n' "$(common::sha256_file "$iso")" "${iso##*/}" >>"${iso%/*}/SHA256SUMS"
reject
[[ ! -e "$work/actions" ]]
cp "$work/checksums" "${iso%/*}/SHA256SUMS"
phase=mutate-iso
reject
if grep -q '^unmountDisk ' "$work/actions"; then exit 1; fi
printf 'USB writer safety fixtures passed; no real device operations ran\n'
