#!/usr/bin/env bash
set -euo pipefail
purge=false
case "${1:-}" in
  '') ;;
  --purge-data) purge=true ;;
  -h|--help) echo 'Usage: sudo drivecheck-uninstall [--purge-data]'; exit 0 ;;
  *) echo 'Usage: sudo drivecheck-uninstall [--purge-data]' >&2; exit 2 ;;
esac
install_root=${DRIVECHECK_TEST_ROOT:-}
if [[ -z "$install_root" ]]; then
  if [[ $(uname -s) != Darwin || $EUID -ne 0 ]]; then
    echo 'Run this uninstaller with sudo on macOS.' >&2; exit 1
  fi
elif [[ ${DRIVECHECK_INSTALL_TESTING:-} != 1 || "$install_root" != /* || "$install_root" == / || -L "$install_root" ]]; then
  echo 'DRIVECHECK_TEST_ROOT is reserved for isolated installer tests.' >&2; exit 1
fi
app_dir="$install_root/opt/drivecheck"
config_dir="$install_root/etc/drivecheck"
data_dir="$install_root/var/db/drivecheck"
unit_path="$install_root/Library/LaunchDaemons/org.drivecheck.station.plist"
uninstall_path="$install_root/usr/local/sbin/drivecheck-uninstall"
marker="$config_dir/.managed-by-drivecheck"
for path in "$app_dir" "$config_dir" "$data_dir" "$unit_path" "$uninstall_path" "$marker"; do
  if [[ -L "$path" ]]; then
    echo "Refusing unsafe uninstall path: $path" >&2; exit 1
  fi
  if [[ -e "$path" && -z "$install_root" && $(stat -f '%u' "$path") != 0 ]]; then
    echo "Refusing non-root-owned uninstall path: $path" >&2; exit 1
  fi
  if [[ -d "$path" && -z "$install_root" && $(stat -f '%d' "$path") != $(stat -f '%d' "$(dirname "$path")") ]]; then
    echo "Refusing uninstall path that is a mount point: $path" >&2; exit 1
  fi
done
if [[ ! -f "$marker" || $(cat "$marker") != drivecheck-macos-install-v1 ]]; then
  echo 'DriveCheck macOS ownership marker is missing; refusing removal.' >&2; exit 1
fi
if launchctl print system/org.drivecheck.station >/dev/null 2>&1; then
  launchctl bootout system/org.drivecheck.station
fi
launchctl disable system/org.drivecheck.station
rm -rf -- "$app_dir"
rm -f -- "$unit_path" "$uninstall_path"
if [[ "$purge" == true ]]; then
  rm -rf -- "$config_dir" "$data_dir"
fi
echo 'DriveCheck removed. Homebrew dependencies are retained.'
if [[ "$purge" == false ]]; then
  echo 'Settings, reports, and sign-in token are preserved for reinstalling.'
fi
