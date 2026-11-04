#!/usr/bin/env bash
# File-only Debian media preparation. USB device operations live in usb.sh.
set -euo pipefail
root=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/../.." && pwd -P)
# shellcheck source=lib/common.sh
source "$root/lib/common.sh"

media_lock() { jq -er --arg key "$1" '.[$key] | select(type == "string" and length > 0)' "$root/installer/media/debian.json"; }
media_key() { printf '%s/installer/media/debian-cd.asc\n' "$root"; }
media_regular() { [[ -f $1 && ! -L $1 && -s $1 ]] || common::die "Require a nonempty regular, non-symlink file: $1"; }
media_new_directory() {
  [[ ! -e $1 && ! -L $1 ]] || common::die "Output already exists; preserve it and choose a new directory: $1"
  mkdir -p -- "$(dirname -- "$1")" || return 1
  mkdir -m 700 -- "$1" || return 1
  (cd -- "$1" && pwd -P)
}
media_download() {
  curl --fail --silent --show-error --location --proto '=https' --proto-redir '=https' \
    --retry 2 --connect-timeout 20 --max-time 900 "$1" -o "$2"
}
media_verify_metadata() (
  local directory=$1 key work signer expected filename manifest_hash
  media_regular "$directory/SHA256SUMS"
  media_regular "$directory/SHA256SUMS.sign"
  key=$(media_key)
  media_regular "$key"
  [[ $(common::sha256_file "$key") == "$(media_lock public_key_sha256)" ]] || common::die 'Debian public-key bytes differ from the reviewed lock'
  work=$(mktemp -d "${TMPDIR:-/tmp}/microserver-signature.XXXXXXXX") || return 1
  trap 'rm -rf -- "$work"' EXIT
  mkdir -m 700 "$work/home" || return 1
  gpg --no-options --homedir "$work/home" --batch --dearmor --output "$work/keyring.gpg" "$key" || return 1
  if ! gpgv --homedir "$work/home" --keyring "$work/keyring.gpg" --status-fd 1 \
    "$directory/SHA256SUMS.sign" "$directory/SHA256SUMS" >"$work/status" 2>"$work/log"; then
    cat "$work/log" >&2
    common::die 'Debian checksum signature verification failed'
  fi
  signer=$(awk '$1 == "[GNUPG:]" && $2 == "VALIDSIG" { count++; signer=$NF } END { if (count != 1) exit 1; print signer }' "$work/status") || common::die 'Expected exactly one valid Debian signature'
  expected=$(media_lock signing_primary_fingerprint)
  [[ $signer == "$expected" ]] || common::die 'Checksum signer differs from the reviewed Debian CD primary key'
  filename=$(media_lock filename)
  manifest_hash=$(awk -v name="$filename" 'NF == 2 && ($2 == name || $2 == "*" name) { count++; hash=$1 } END { if (count != 1) exit 1; print hash }' "$directory/SHA256SUMS") || common::die 'Signed checksum file has no unique selected ISO entry'
  [[ $manifest_hash == "$(media_lock sha256)" ]] || common::die 'Signed Debian checksum differs from the reviewed ISO pin'
  common::info "Debian checksum signature and pinned digest verified: $signer"
)
media_verify() {
  local directory=$1 image
  media_verify_metadata "$directory" || return 1
  image="$directory/$(media_lock filename)"
  media_regular "$image"
  [[ $(common::sha256_file "$image") == "$(media_lock sha256)" ]] || common::die 'Debian ISO checksum mismatch'
  common::info 'Debian ISO bytes verified'
}
media_fetch() {
  local output base_url
  output=$(media_new_directory "$1") || return 1
  base_url=$(media_lock base_url) || return 1
  media_download "$base_url/SHA256SUMS" "$output/SHA256SUMS" || return 1
  media_download "$base_url/SHA256SUMS.sign" "$output/SHA256SUMS.sign" || return 1
  media_verify_metadata "$output" || return 1
  media_download "$base_url/$(media_lock filename)" "$output/$(media_lock filename)" || return 1
  media_verify "$output" || return 1
  printf 'Verified Debian source: %s\n' "$output"
}
media_payload_file() {
  local relative=$1 component current=$root
  [[ $relative =~ ^[A-Za-z0-9][A-Za-z0-9_./-]*$ && $relative != */../* && $relative != ../* && $relative != */.. && $relative != */./* ]] || common::die 'Unsafe payload manifest path'
  local remaining=$relative
  while [[ $remaining == */* ]]; do
    component=${remaining%%/*}
    current="$current/$component"
    [[ -d $current && ! -L $current ]] || common::die "Unsafe payload parent: $relative"
    remaining=${remaining#*/}
  done
  media_regular "$root/$relative"
}
media_payload() {
  local output=$1 relative hash
  mkdir -m 755 "$output" || return 1
  while IFS= read -r relative || [[ -n $relative ]]; do
    [[ -n $relative && $relative != \#* ]] || continue
    media_payload_file "$relative"
    [[ ! -e $output/$relative ]] || common::die 'Duplicate payload manifest entry'
    (
      umask 022
      mkdir -p -- "$output/$(dirname -- "$relative")"
    ) || return 1
    cp -- "$root/$relative" "$output/$relative" || return 1
    chmod 0644 "$output/$relative" || return 1
    hash=$(common::sha256_file "$output/$relative") || return 1
    printf '%s  %s\n' "$hash" "$relative" >>"$output/SOURCE.sha256" || return 1
  done <"$root/installer/media/payload.files"
  [[ -s $output/SOURCE.sha256 ]] || common::die 'Empty setup payload'
  chmod 0644 "$output/SOURCE.sha256"
}
media_md5() {
  if command -v md5sum >/dev/null 2>&1; then md5sum "$1" | awk '{print $1}'; else md5 -q "$1"; fi
}
media_iso_extract() { xorriso -osirrox on -indev "$1" -extract "$2" "$3" >>"$4" 2>&1; }
media_build() (
  local source_dir=$1 output image stage payload relative hash revision=unversioned path index=0 result
  media_verify "$source_dir" || return 1
  source_dir=$(cd -- "$source_dir" && pwd -P) || return 1
  output=$(media_new_directory "$2") || return 1
  image="$source_dir/$(media_lock filename)"
  stage="$output/work"
  payload="$stage/microserver"
  mkdir "$stage" || return 1
  media_payload "$payload" || return 1
  cp "$root/installer/media/preseed.cfg" "$stage/preseed.cfg" || return 1
  cp "$root/installer/media/grub-entry.cfg" "$stage/grub-entry.cfg" || return 1
  cp "$root/installer/media/isolinux-entry.cfg" "$stage/microserver.cfg" || return 1
  media_iso_extract "$image" /boot/grub/grub.cfg "$stage/vendor-grub.cfg" "$output/build.log" || return 1
  media_iso_extract "$image" /isolinux/menu.cfg "$stage/vendor-menu.cfg" "$output/build.log" || return 1
  media_iso_extract "$image" /md5sum.txt "$stage/vendor-md5sum.txt" "$output/build.log" || return 1
  cat "$stage/grub-entry.cfg" "$stage/vendor-grub.cfg" >"$stage/grub.cfg" || return 1
  cat "$stage/vendor-menu.cfg" >"$stage/menu.cfg" || return 1
  printf '\ninclude microserver.cfg\n' >>"$stage/menu.cfg" || return 1
  if command -v git >/dev/null 2>&1; then revision=$(git -C "$root" rev-parse HEAD 2>/dev/null || printf unversioned); fi
  jq -n --arg base_sha256 "$(media_lock sha256)" --arg version "$(media_lock version)" \
    --arg source_revision "$revision" --arg payload_sha256 "$(common::sha256_file "$payload/SOURCE.sha256")" \
    '{base_version:$version,base_sha256:$base_sha256,source_revision:$source_revision,payload_sha256:$payload_sha256,status:"derived-image-not-signed-by-debian-not-boot-qualified"}' >"$payload/BUILD-IDENTITY.json" || return 1
  # Preserve Debian's media-check coverage, replacing only the edited entries.
  awk '$2 != "./boot/grub/grub.cfg" && $2 != "./isolinux/menu.cfg" && $2 != "./isolinux/microserver.cfg" && $2 != "./preseed.cfg"' "$stage/vendor-md5sum.txt" >"$stage/md5sum.txt" || return 1
  for relative in grub.cfg menu.cfg microserver.cfg preseed.cfg; do
    case $relative in
      grub.cfg) path=boot/grub/grub.cfg ;;
      menu.cfg) path=isolinux/menu.cfg ;;
      microserver.cfg) path=isolinux/microserver.cfg ;;
      *) path=$relative ;;
    esac
    hash=$(media_md5 "$stage/$relative") || return 1
    printf '%s  ./%s\n' "$hash" "$path" >>"$stage/md5sum.txt" || return 1
  done
  while read -r hash relative; do
    hash=$(media_md5 "$payload/$relative") || return 1
    printf '%s  ./microserver/%s\n' "$hash" "$relative" >>"$stage/md5sum.txt" || return 1
  done <"$payload/SOURCE.sha256"
  for relative in SOURCE.sha256 BUILD-IDENTITY.json; do
    hash=$(media_md5 "$payload/$relative") || return 1
    printf '%s  ./microserver/%s\n' "$hash" "$relative" >>"$stage/md5sum.txt" || return 1
  done
  result="$output/microserver-debian-$(media_lock version)-amd64.iso"
  xorriso -indev "$image" -outdev "$result" \
    -map "$payload" /microserver -map "$stage/preseed.cfg" /preseed.cfg \
    -map "$stage/grub.cfg" /boot/grub/grub.cfg -map "$stage/menu.cfg" /isolinux/menu.cfg \
    -map "$stage/microserver.cfg" /isolinux/microserver.cfg -map "$stage/md5sum.txt" /md5sum.txt \
    -boot_image any replay >>"$output/build.log" 2>&1 || return 1
  # Check that the vendor kernel, initrd and EFI boot image survived unchanged.
  for path in /install.amd/vmlinuz /install.amd/initrd.gz /boot/grub/efi.img /EFI/boot/bootx64.efi /EFI/boot/grubx64.efi; do
    media_iso_extract "$image" "$path" "$stage/vendor-$index" "$output/build.log" || return 1
    media_iso_extract "$result" "$path" "$stage/result-$index" "$output/build.log" || return 1
    cmp "$stage/vendor-$index" "$stage/result-$index" || common::die 'Vendor boot component changed'
    index=$((index + 1))
  done
  xorriso -indev "$result" -report_el_torito plain -report_system_area plain >"$output/boot-layout.log" 2>&1 || return 1
  grep -Eq 'El Torito boot img :.*BIOS.*y' "$output/boot-layout.log" || common::die 'BIOS boot record missing'
  grep -Eq 'El Torito boot img :.*UEFI.*y' "$output/boot-layout.log" || common::die 'UEFI boot record missing'
  grep -Eq 'System area summary:.*MBR.*GPT|System area summary :.*MBR.*GPT' "$output/boot-layout.log" || common::die 'Hybrid USB system area missing'
  media_verify "$source_dir" || return 1
  while read -r hash relative; do
    [[ $(common::sha256_file "$root/$relative") == "$hash" ]] || common::die 'Setup source changed during the build; preserve this failed attempt'
  done <"$payload/SOURCE.sha256"
  cp "$source_dir/SHA256SUMS" "$output/DEBIAN-SHA256SUMS" || return 1
  cp "$source_dir/SHA256SUMS.sign" "$output/DEBIAN-SHA256SUMS.sign" || return 1
  cp "$payload/BUILD-IDENTITY.json" "$output/BUILD-IDENTITY.json" || return 1
  hash=$(common::sha256_file "$result") || return 1
  printf '%s  %s\n' "$hash" "${result##*/}" >"$output/SHA256SUMS" || return 1
  printf 'Derived installation image: %s\n' "$result"
  common::info 'Signature, source, boot-component and boot-record checks passed. Physical boot and USB readback remain separate.'
)
media_main() {
  case ${1:-} in
    --help | -h)
      printf '%s\n' 'Usage: media.sh fetch NEW_SOURCE_DIR' '       media.sh verify SOURCE_DIR' '       media.sh build SOURCE_DIR NEW_OUTPUT_DIR' \
        'Fetch authenticates the pinned official Debian netinst ISO. Build works on files only.' \
        'Use usb.sh for a separate owner-run, confirmed macOS USB write.'
      return
      ;;
    fetch | verify) (($# == 2)) || common::die 'fetch/verify requires one directory' ;;
    build) (($# == 3)) || common::die 'build requires source and new output directories' ;;
    *) common::die 'Use --help for media preparation commands' ;;
  esac
  for command in jq gpg gpgv; do common::require_command "$command"; done
  case $1 in
    fetch)
      common::require_command curl
      media_fetch "$2"
      ;;
    verify) media_verify "$2" ;;
    build)
      common::require_command xorriso
      media_build "$2" "$3"
      ;;
  esac
}
if [[ ${BASH_SOURCE[0]} == "$0" ]]; then media_main "$@"; fi
