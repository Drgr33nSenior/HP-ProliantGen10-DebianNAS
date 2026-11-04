#!/usr/bin/env bash
# Small helpers shared by the file-only media builder and guarded USB writer.
common::info() { printf 'INFO: %s\n' "$*" >&2; }
common::warn() { printf 'WARN: %s\n' "$*" >&2; }
common::die() {
  printf 'ERROR: %s\n' "$*" >&2
  exit 1
}
common::require_command() { command -v "$1" >/dev/null 2>&1 || common::die "Required command is unavailable: $1"; }
common::require_root() { [[ $(id -u) == 0 ]] || common::die 'This operation requires owner-run root privileges.'; }
common::sha256_file() {
  if command -v sha256sum >/dev/null 2>&1; then
    sha256sum "$1" | awk '{print $1}'
  else
    shasum -a 256 "$1" | awk '{print $1}'
  fi
}
common::print_command() {
  printf ' +'
  local argument
  for argument in "$@"; do printf ' %q' "$argument"; done
  printf '\n'
}
