#!/usr/bin/env bash
set -euo pipefail

usage() {
  echo 'Usage: bash scripts/install-macos.sh [--no-start] [--lan]'
  echo 'Requires Homebrew. Installs a root launchd drive testing and formatting service.'
}
source_dir=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd)
install_root=${DRIVECHECK_TEST_ROOT:-}
if [[ -n "$install_root" && ( ${DRIVECHECK_INSTALL_TESTING:-} != 1 || \
      "$install_root" != /* || "$install_root" == / || -L "$install_root" ) ]]; then
  echo 'DRIVECHECK_TEST_ROOT is reserved for isolated installer tests.' >&2
  exit 1
fi

# Homebrew must run as the login user. Elevate only the native application install.
if [[ ${1:-} != --system ]]; then
  start_service=true
  lan=false
  while (($#)); do
    case "$1" in
      --no-start) start_service=false ;;
      --lan) lan=true ;;
      -h|--help) usage; exit 0 ;;
      *) usage >&2; exit 2 ;;
    esac
    shift
  done
  if [[ -z "$install_root" && ( $(uname -s) != Darwin || $EUID -eq 0 ) ]]; then
    echo 'Run this installer on macOS as your normal user, without sudo.' >&2
    exit 1
  fi
  if ! command -v brew >/dev/null; then
    echo 'Install Homebrew first: https://brew.sh' >&2
    exit 1
  fi
  bootstrap_python=$(command -v python3)
  sudo "$bootstrap_python" "$source_dir/scripts/check-idle.py" "$install_root/var/db/drivecheck/drivecheck.sqlite3"
  brew install python@3.13 fio smartmontools
  python_path="$(brew --prefix python@3.13)/bin/python3.13"
  tool_prefix=$(brew --prefix)
  set -- "$0" --system "$python_path" "$tool_prefix"
  if [[ "$start_service" == false ]]; then set -- "$@" --no-start; fi
  if [[ "$lan" == true ]]; then set -- "$@" --lan; fi
  sudo bash "$@"
  exit 0
fi
shift
if (($# < 2)); then usage >&2; exit 2; fi
python_path=$1
tool_prefix=$2
start_service=true
lan=false
shift 2
while (($#)); do
  case "$1" in
    --no-start) start_service=false ;;
    --lan) lan=true ;;
    *) usage >&2; exit 2 ;;
  esac
  shift
done
if [[ -z "$install_root" && ( $(uname -s) != Darwin || $EUID -ne 0 ) ]]; then
  echo 'The system install stage requires root on macOS.' >&2
  exit 1
fi
if [[ "$python_path" != /* || ! -x "$python_path" || "$tool_prefix" != /* ]]; then
  echo 'Invalid Homebrew Python or tool prefix.' >&2; exit 1
fi
"$python_path" -c 'import sys; assert sys.version_info >= (3, 11)'
app_dir="$install_root/opt/drivecheck"
config_dir="$install_root/etc/drivecheck"
data_dir="$install_root/var/db/drivecheck"
unit_path="$install_root/Library/LaunchDaemons/org.drivecheck.station.plist"
uninstall_path="$install_root/usr/local/sbin/drivecheck-uninstall"
marker="$config_dir/.managed-by-drivecheck"

for path in "$app_dir" "$config_dir" "$data_dir"; do
  if [[ -L "$path" || ( -e "$path" && ! -d "$path" ) ]]; then
    echo "Refusing unsafe install path: $path" >&2; exit 1
  fi
  if [[ -d "$path" && -z "$install_root" && $(stat -f '%u' "$path") != 0 ]]; then
    echo "Refusing non-root-owned install path: $path" >&2; exit 1
  fi
  if [[ -d "$path" && -z "$install_root" && $(stat -f '%d' "$path") != $(stat -f '%d' "$(dirname "$path")") ]]; then
    echo "Refusing install path that is a mount point: $path" >&2; exit 1
  fi
done
for path in "$unit_path" "$uninstall_path" "$marker" "$config_dir/drivecheck.env"; do
  if [[ -L "$path" || ( -e "$path" && ! -f "$path" ) ]]; then
    echo "Refusing unsafe install path: $path" >&2; exit 1
  fi
done
existing_content=false
for path in "$app_dir" "$config_dir" "$data_dir" "$unit_path" "$uninstall_path"; do
  if [[ -f "$path" ]] || { [[ -d "$path" ]] && [[ -n $(find "$path" -mindepth 1 -maxdepth 1 -print -quit) ]]; }; then
    existing_content=true
  fi
done
if [[ "$existing_content" == true && ( ! -f "$marker" || $(cat "$marker") != drivecheck-macos-install-v1 ) ]]; then
  echo 'Existing files lack a DriveCheck macOS ownership marker; refusing to overwrite them.' >&2
  exit 1
fi
"$python_path" "$source_dir/scripts/check-idle.py" "$data_dir/drivecheck.sqlite3"
if launchctl print system/org.drivecheck.station >/dev/null 2>&1; then
  launchctl bootout system/org.drivecheck.station
fi

install -d -m 755 "$app_dir" "$config_dir" "$(dirname "$unit_path")" "$(dirname "$uninstall_path")"
install -d -m 700 "$data_dir"
printf '%s\n' drivecheck-macos-install-v1 >"$marker"
chmod 600 "$marker"
install -m 755 "$source_dir/scripts/uninstall-macos.sh" "$uninstall_path"
find "$app_dir" -mindepth 1 -maxdepth 1 -exec rm -rf -- {} +
cp -R "$source_dir/drivecheck" "$app_dir/drivecheck"
install -m 644 "$source_dir/pyproject.toml" "$source_dir/requirements.lock" \
  "$source_dir/LICENSE" "$source_dir/README.md" "$app_dir/"
install -m 755 "$source_dir/deploy/run-macos.sh" "$app_dir/run-macos.sh"
if [[ -f "$source_dir/DEPLOYED_REVISION" ]]; then
  install -m 644 "$source_dir/DEPLOYED_REVISION" "$app_dir/"
fi
"$python_path" -m venv "$app_dir/.venv"
"$app_dir/.venv/bin/pip" install --require-hashes -r "$app_dir/requirements.lock"
"$app_dir/.venv/bin/pip" install --no-deps "$app_dir"
if [[ ! -e "$config_dir/drivecheck.env" ]]; then
  sed 's|DRIVECHECK_DATA_DIR=/var/lib/drivecheck|DRIVECHECK_DATA_DIR=/var/db/drivecheck|' \
    "$source_dir/.env.example" >"$config_dir/drivecheck.env"
  chmod 600 "$config_dir/drivecheck.env"
fi
if [[ "$lan" == true ]]; then
  "$python_path" "$source_dir/scripts/configure-lan.py" "$config_dir/drivecheck.env"
fi
"$python_path" - "$unit_path" "$app_dir" "$data_dir" "$tool_prefix" <<'PY'
import plistlib
import sys

unit, app, data, prefix = sys.argv[1:]
with open(unit, "wb") as output:
    plistlib.dump({
        "Label": "org.drivecheck.station",
        "ProgramArguments": [f"{app}/run-macos.sh"],
        "WorkingDirectory": app,
        "EnvironmentVariables": {"PATH": f"{prefix}/bin:{prefix}/sbin:/usr/bin:/bin:/usr/sbin:/sbin"},
        "RunAtLoad": True,
        "KeepAlive": True,
        "ThrottleInterval": 10,
        "ExitTimeOut": 60,
        "Umask": 63,
        "StandardOutPath": f"{data}/station.log",
        "StandardErrorPath": f"{data}/station-error.log",
    }, output)
PY
chmod 644 "$unit_path"
if [[ -z "$install_root" ]]; then
  chown -R root:wheel "$app_dir" "$config_dir" "$data_dir"
  chown root:wheel "$unit_path" "$uninstall_path"
fi
launchctl enable system/org.drivecheck.station
if [[ "$start_service" == true ]]; then
  launchctl bootstrap system "$unit_path"
  if ! ready_output=$("$python_path" "$source_dir/scripts/wait-ready.py" "$config_dir/drivecheck.env" --platform macos); then
    printf '%s\n' "$ready_output" >&2
    exit 1
  fi
fi
printf '\nDriveCheck installed on macOS (native testing, formatting, and overwrite).\n'
if [[ "$start_service" == true ]]; then
  printf '%s\n' "$ready_output"
fi
printf 'Status: sudo launchctl print system/org.drivecheck.station\n'
if [[ "$start_service" == false ]]; then
  printf 'Service left stopped. Start: sudo launchctl bootstrap system /Library/LaunchDaemons/org.drivecheck.station.plist\n'
  printf 'The sign-in token is generated on first startup, unless DRIVECHECK_API_KEY is configured.\n'
fi
printf 'Uninstall later: sudo /usr/local/sbin/drivecheck-uninstall [--purge-data]\n'
