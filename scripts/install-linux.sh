#!/usr/bin/env bash
set -euo pipefail

usage() {
  cat <<'EOF'
Usage: sudo bash scripts/install-linux.sh [--no-start]

Installs or updates DriveCheck on Debian/Ubuntu Linux. --no-start installs and
enables the service but leaves it stopped so its environment can be edited.
EOF
}

start_service=true
case "${1:-}" in
  "") ;;
  --no-start) start_service=false ;;
  -h|--help) usage; exit 0 ;;
  *) usage >&2; exit 2 ;;
esac

# Used only by fixture tests; production installations always have no prefix.
install_root=${DRIVECHECK_TEST_ROOT:-}
while [[ -n "$install_root" && "$install_root" != / && "$install_root" == */ ]]; do
  install_root=${install_root%/}
done
if [[ -z "$install_root" ]]; then
  if [[ $(uname -s) != Linux || $EUID -ne 0 ]]; then
    echo "Run this installer with sudo on Debian or Ubuntu Linux." >&2
    exit 1
  fi
elif [[ ${DRIVECHECK_INSTALL_TESTING:-} != 1 || "$install_root" != /* || \
        "$install_root" == / || -L "$install_root" ]]; then
  echo "DRIVECHECK_TEST_ROOT is reserved for isolated installer tests." >&2
  exit 1
fi

source_dir=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd)
app_dir="$install_root/opt/drivecheck"
config_dir="$install_root/etc/drivecheck"
data_dir="$install_root/var/lib/drivecheck"
unit_path="$install_root/etc/systemd/system/drivecheck.service"
uninstall_path="$install_root/usr/local/sbin/drivecheck-uninstall"
marker="$config_dir/.managed-by-drivecheck"
manifest="$config_dir/install-manifest"

guard_directory() {
  local path=$1
  if [[ -L "$path" || ( -e "$path" && ! -d "$path" ) ]]; then
    echo "Refusing unsafe install path: $path" >&2
    exit 1
  fi
  if [[ -d "$path" && -z "$install_root" ]]; then
    if [[ $(stat -c '%u' -- "$path") != 0 ]]; then
      echo "Refusing non-root-owned install path: $path" >&2
      exit 1
    fi
    if mountpoint -q -- "$path"; then
      echo "Refusing install path that is a mount point: $path" >&2
      exit 1
    fi
  fi
}

for path in "$app_dir" "$config_dir" "$data_dir"; do
  guard_directory "$path"
done
for path in "$unit_path" "$uninstall_path"; do
  if [[ -L "$path" || ( -e "$path" && ! -f "$path" ) ]]; then
    echo "Refusing unsafe install path: $path" >&2
    exit 1
  fi
done
for path in "$marker" "$manifest" "$config_dir/drivecheck.env"; do
  if [[ -L "$path" ]]; then
    echo "Refusing symlink at managed install path: $path" >&2
    exit 1
  fi
done

# Never claim or erase an unrelated directory at one of the fixed paths. The
# legacy signature permits an update from the original Pi installer, which did
# not yet write an ownership marker.
existing_content=false
for path in "$app_dir" "$config_dir" "$data_dir" "$unit_path" "$uninstall_path"; do
  if [[ -f "$path" ]]; then
    existing_content=true
  elif [[ -d "$path" ]] && [[ -n $(find "$path" -mindepth 1 -maxdepth 1 -print -quit) ]]; then
    existing_content=true
  fi
done
if [[ "$existing_content" == true ]]; then
  managed=false
  if [[ -f "$marker" && $(cat -- "$marker") == drivecheck-linux-install-v1 ]]; then
    managed=true
  elif [[ -f "$app_dir/pyproject.toml" && -d "$app_dir/drivecheck" && -f "$unit_path" ]]; then
    managed=true
  fi
  if [[ "$managed" != true ]]; then
    echo "Existing files lack a DriveCheck ownership marker; refusing to overwrite them." >&2
    exit 1
  fi
fi

python3 -c 'import sys; assert sys.version_info >= (3, 11), "Python 3.11 or newer is required"'

packages=(python3-venv python3-pip smartmontools fio util-linux udisks2)
missing=()
for package in "${packages[@]}"; do
  if ! dpkg-query -W -f='${Status}' "$package" 2>/dev/null | grep -q '^install ok installed$'; then
    missing+=("$package")
  fi
done

smart_package_new=false
smart_units_preexisting=false
if [[ " ${missing[*]-} " == *" smartmontools "* ]]; then
  smart_package_new=true
fi
for unit in smartmontools.service smartd.service; do
  if systemctl list-unit-files "$unit" --no-legend 2>/dev/null | grep -q "^$unit"; then
    smart_units_preexisting=true
  fi
done

if ((${#missing[@]})); then
  apt-get update
  NEEDRESTART_MODE=l DEBIAN_FRONTEND=noninteractive apt-get install -y --no-install-recommends "${missing[@]}"
fi

# smartmontools may start smartd. Disable only a daemon introduced by this
# installation; a daemon/unit that existed beforehand is never stopped.
if [[ "$smart_package_new" == true && "$smart_units_preexisting" == false ]]; then
  smart_unit=
  for unit in smartmontools.service smartd.service; do
    if systemctl list-unit-files "$unit" --no-legend 2>/dev/null | grep -q "^$unit"; then
      smart_unit=$(systemctl show --property=Id --value "$unit")
      smart_unit=${smart_unit:-$unit}
      break
    fi
  done
  if [[ -n "$smart_unit" ]]; then
    systemctl disable --now "$smart_unit"
  fi
fi

if systemctl is-active --quiet drivecheck.service; then
  systemctl stop drivecheck.service
fi

install -d -m 755 "$app_dir" "$config_dir" "$(dirname -- "$unit_path")" "$(dirname -- "$uninstall_path")"
install -d -m 700 "$data_dir"

# Establish recovery before rebuilding the application. If venv or pip fails,
# the same guarded uninstaller can still remove the partial installation.
install -m 644 "$source_dir/deploy/drivecheck.service" "$unit_path"
install -m 755 "$source_dir/scripts/uninstall-linux.sh" "$uninstall_path"
printf '%s\n' 'drivecheck-linux-install-v1' >"$marker"
chmod 600 "$marker"
systemctl daemon-reload

find "$app_dir" -mindepth 1 -maxdepth 1 -exec rm -rf -- {} +
cp -R "$source_dir/drivecheck" "$app_dir/drivecheck"
install -m 644 "$source_dir/pyproject.toml" "$source_dir/requirements.lock" \
  "$source_dir/LICENSE" "$source_dir/README.md" "$app_dir/"
if [[ -z "$install_root" ]]; then
  chown -R root:root "$app_dir" "$config_dir" "$data_dir"
fi

python3 -m venv "$app_dir/.venv"
"$app_dir/.venv/bin/pip" install --require-hashes -r "$app_dir/requirements.lock"
"$app_dir/.venv/bin/pip" install --no-deps "$app_dir"

if [[ ! -e "$config_dir/drivecheck.env" ]]; then
  install -m 600 "$source_dir/.env.example" "$config_dir/drivecheck.env"
fi
initially_missing=${missing[*]-}
if [[ -f "$manifest" ]] && grep -q '^apt_packages_initially_missing=' "$manifest"; then
  previous_missing=$(sed -n 's/^apt_packages_initially_missing=//p' "$manifest" | head -n 1)
  initially_missing=$previous_missing
fi
{
  printf 'format=1\n'
  printf 'installed_utc=%s\n' "$(date -u +%Y-%m-%dT%H:%M:%SZ)"
  printf 'apt_packages_initially_missing=%s\n' "$initially_missing"
  printf 'owned_paths=%s\n' '/opt/drivecheck /etc/systemd/system/drivecheck.service /usr/local/sbin/drivecheck-uninstall'
  printf 'preserved_paths=%s\n' '/etc/drivecheck /var/lib/drivecheck'
} >"$manifest"
chmod 600 "$manifest"

systemctl enable drivecheck.service
if [[ "$start_service" == true ]]; then
  systemctl start drivecheck.service
fi

printf '\nDriveCheck installed on Linux.\n'
if [[ "$start_service" == true ]]; then
  printf 'Status: sudo systemctl status drivecheck\n'
else
  printf 'Service left stopped (--no-start). Edit /etc/drivecheck/drivecheck.env, then run:\n'
  printf '  sudo systemctl start drivecheck\n'
fi
printf 'Sign-in token: sudo cat /var/lib/drivecheck/access-token\n'
printf 'Dashboard tunnel: ssh -L 8765:127.0.0.1:8765 USER@HOST\n'
printf 'Uninstall later: sudo drivecheck-uninstall [--purge-data]\n'
