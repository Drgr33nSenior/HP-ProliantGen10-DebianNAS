#!/bin/sh
# Debian-installer late hook: stage public setup source on the selected OS root.
# No account provisioning, pool commands, service activation or disk operations.
set -eu
# Fixed production paths; fixture tests replace only these OS adapters in a shell.
finish_source() { printf '/cdrom/microserver\n'; }
finish_target() { printf '/target\n'; }
finish_main() {
  source_dir=$(finish_source) || return 1
  target_root=$(finish_target) || return 1
  destination="$target_root/opt/microserver-installer"
  test -d "$target_root/etc" || return 1
  test -d "$target_root/etc/apt/apt.conf.d" || return 1
  test ! -e "$destination" || return 1
  test ! -L "$destination" || return 1
  (cd "$source_dir" && sha256sum -c SOURCE.sha256) || return 1
  mkdir -p "$target_root/opt" || return 1
  mkdir "$destination" || return 1
  cp -R "$source_dir/." "$destination/" || return 1
  chown -R 0:0 "$destination" || return 1
  cp "$destination/installer/media/80microserver-minimal" "$target_root/etc/apt/apt.conf.d/80microserver-minimal" || return 1
  chmod 0644 "$target_root/etc/apt/apt.conf.d/80microserver-minimal"
}
case ${0##*/} in finish.sh) finish_main ;; esac
