#!/usr/bin/env bash
set -euo pipefail

usage() {
  cat <<'EOF'
Usage: sudo drivecheck-uninstall [--purge-data]

The default removes the app and service while keeping config, reports, settings,
and the access token. --purge-data also removes those files. APT packages remain.
EOF
}

purge=false
case "${1:-}" in
  "") ;;
  --purge-data) purge=true ;;
  -h|--help) usage; exit 0 ;;
  *) usage >&2; exit 2 ;;
esac

install_root=${DRIVECHECK_TEST_ROOT:-}
while [[ -n "$install_root" && "$install_root" != / && "$install_root" == */ ]]; do
  install_root=${install_root%/}
done
if [[ -z "$install_root" ]]; then
  if [[ $(uname -s) != Linux || $EUID -ne 0 ]]; then
    echo "Run this uninstaller with sudo on Linux." >&2
    exit 1
  fi
elif [[ ${DRIVECHECK_INSTALL_TESTING:-} != 1 || "$install_root" != /* || \
        "$install_root" == / || -L "$install_root" ]]; then
  echo "DRIVECHECK_TEST_ROOT is reserved for isolated installer tests." >&2
  exit 1
fi

app_dir="$install_root/opt/drivecheck"
config_dir="$install_root/etc/drivecheck"
data_dir="$install_root/var/lib/drivecheck"
unit_path="$install_root/etc/systemd/system/drivecheck.service"
uninstall_path="$install_root/usr/local/sbin/drivecheck-uninstall"
marker="$config_dir/.managed-by-drivecheck"

guard_owned_path() {
  local path=$1 expected=$2
  if [[ -L "$path" || ( -e "$path" && "$expected" == directory && ! -d "$path" ) || \
        ( -e "$path" && "$expected" == file && ! -f "$path" ) ]]; then
    echo "Refusing unsafe uninstall path: $path" >&2
    exit 1
  fi
  if [[ -e "$path" && -z "$install_root" && $(stat -c '%u' -- "$path") != 0 ]]; then
    echo "Refusing non-root-owned uninstall path: $path" >&2
    exit 1
  fi
  if [[ -d "$path" && -z "$install_root" ]] && mountpoint -q -- "$path"; then
    echo "Refusing uninstall path that is a mount point: $path" >&2
    exit 1
  fi
}

guard_owned_path "$config_dir" directory
if [[ -L "$marker" || ! -f "$marker" || $(cat -- "$marker") != drivecheck-linux-install-v1 ]]; then
  echo "DriveCheck ownership marker is missing; refusing to remove fixed paths." >&2
  exit 1
fi
guard_owned_path "$app_dir" directory
guard_owned_path "$data_dir" directory
guard_owned_path "$unit_path" file
guard_owned_path "$uninstall_path" file

if systemctl list-unit-files drivecheck.service --no-legend 2>/dev/null | grep -q '^drivecheck.service'; then
  systemctl stop drivecheck.service
  systemctl disable drivecheck.service
elif [[ -e "$app_dir" || -e "$unit_path" ]]; then
  echo "DriveCheck files remain but systemd cannot find its unit; refusing unsafe removal." >&2
  exit 1
fi
rm -rf -- "$app_dir"
rm -f -- "$unit_path"
systemctl daemon-reload
systemctl reset-failed drivecheck.service 2>/dev/null || true

if [[ "$purge" == true ]]; then
  rm -rf -- "$data_dir" "$config_dir"
  printf 'DriveCheck uninstalled; configuration, reports, settings, and token were purged.\n'
else
  printf 'DriveCheck uninstalled. Preserved:\n'
  printf '  /etc/drivecheck (configuration and install record)\n'
  printf '  /var/lib/drivecheck (reports, settings, and access token)\n'
  printf 'APT dependencies were left installed because they may be shared.\n'
  printf 'To purge later, rerun scripts/uninstall-linux.sh --purge-data from a DriveCheck checkout.\n'
fi

# Unix keeps the opened script readable while its command pathname is removed.
rm -f -- "$uninstall_path"
