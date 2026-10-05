#!/usr/bin/env bash
# Standalone bootstrap: download DriveCheck, then run its native installer.
set -euo pipefail

usage() {
  cat <<'EOF'
Usage: bash install.sh [--ref BRANCH_TAG_OR_COMMIT] [--no-start] [--dry-run]

Downloads and installs DriveCheck on Debian-based Linux or macOS. Defaults to main.
Linux: run with sudo. macOS: run as your normal user with Homebrew installed;
the native installer requests sudo when needed.
--ref       Install a specific GitHub branch, tag, or full commit SHA.
--no-start  Install and enable the service without starting it.
--dry-run   Resolve the revision and print the plan without installing.
EOF
}

ref=main
no_start=false
dry_run=false
while (($#)); do
  case "$1" in
    --ref)
      if (($# < 2)) || [[ -z "$2" || "$2" == --* ]]; then
        echo "--ref requires a branch, tag, or full commit SHA." >&2
        exit 2
      fi
      ref=$2
      shift 2
      ;;
    --no-start) no_start=true; shift ;;
    --dry-run) dry_run=true; shift ;;
    -h|--help) usage; exit 0 ;;
    *) usage >&2; exit 2 ;;
  esac
done
if [[ ! "$ref" =~ ^[a-zA-Z0-9][a-zA-Z0-9._/-]{0,199}$ ]]; then
  echo "Invalid GitHub revision." >&2
  exit 2
fi

test_root=${DRIVECHECK_TEST_ROOT:-}
if [[ -n "$test_root" ]]; then
  if [[ ${DRIVECHECK_INSTALL_TESTING:-} != 1 || "$test_root" != /* || \
        "$test_root" == / || -L "$test_root" ]]; then
    echo "DRIVECHECK_TEST_ROOT is reserved for isolated installer tests." >&2
    exit 1
  fi
fi
platform=$(uname -s)
case "$platform" in
  Linux) required_tools=(python3 curl apt-get dpkg-query systemctl) ;;
  Darwin) required_tools=(python3 curl brew launchctl) ;;
  *) echo "This installer supports Debian-based Linux and macOS." >&2; exit 1 ;;
esac
if [[ "$platform" == Darwin && -z "$test_root" && $EUID -eq 0 ]]; then
  echo "On macOS run bash install.sh without sudo; Homebrew runs as your normal user." >&2
  exit 1
fi
if [[ "$platform" == Linux && -z "$test_root" && "$dry_run" == false && $EUID -ne 0 ]]; then
  echo "Run this installer with sudo: sudo bash install.sh" >&2
  exit 1
fi
for tool in "${required_tools[@]}"; do
  if ! command -v "$tool" >/dev/null; then
    echo "Required command missing: $tool. See the installation prerequisites in docs/INSTALLATION.md." >&2
    exit 1
  fi
done
if [[ "$platform" == Linux ]]; then
  python3 -c 'import sys; assert sys.version_info >= (3, 11), "Python 3.11 or newer is required"'
else
  # Apple's Python can unpack the source; the macOS installer adds current Python.
  python3 -c 'import sys; assert sys.version_info >= (3, 8), "Python 3.8 or newer is required to bootstrap"'
fi

curl_args=(--fail --silent --show-error --location --proto '=https' --proto-redir '=https'
  --connect-timeout 15 --max-time 180 --retry 2 --max-filesize 52428800)
if [[ "$ref" =~ ^[a-fA-F0-9]{40}$ ]]; then
  revision=$(python3 -c 'import sys; print(sys.argv[1].lower())' "$ref")
else
  encoded_ref=$(python3 -c 'import sys, urllib.parse; print(urllib.parse.quote(sys.argv[1], safe=""))' "$ref")
  revision=$(curl "${curl_args[@]}" \
    "https://api.github.com/repos/pcamp96/drivecheck/commits/$encoded_ref" |
    python3 -c 'import json, sys; print(json.load(sys.stdin)["sha"])')
fi
if [[ ! "$revision" =~ ^[a-f0-9]{40}$ ]]; then
  echo "GitHub did not return a valid commit SHA; installation stopped." >&2
  exit 1
fi
printf 'DriveCheck revision: %s\n' "$revision"
archive_url="https://codeload.github.com/pcamp96/drivecheck/zip/$revision"
if [[ "$dry_run" == true ]]; then
  printf 'Would download: %s\n' "$archive_url"
  printf 'Would install the native app, required packages, and %s service.\n' "$([[ "$platform" == Darwin ]] && echo launchd || echo systemd)"
  printf 'Existing station configuration and reports would be preserved.\n'
  printf 'Service start: %s\n' "$([[ "$no_start" == true ]] && echo disabled || echo enabled)"
  exit 0
fi

temp_parent=/tmp
if [[ -n "$test_root" ]]; then
  temp_parent=$(dirname -- "$test_root")
fi
work_dir=$(mktemp -d "$temp_parent/drivecheck-install.XXXXXX")
trap 'rm -rf -- "$work_dir"' EXIT
curl "${curl_args[@]}" "$archive_url" --output "$work_dir/source.zip"

# Extract only ordinary files/directories into a private new directory. Reject
# traversal, symlinks, unexpected archive roots, and excessive expanded sizes.
python3 - "$work_dir/source.zip" "$work_dir/source" "$revision" <<'PY'
import stat
import sys
import zipfile
from pathlib import Path, PurePosixPath

archive, destination, revision = sys.argv[1:]
target = Path(destination)
with zipfile.ZipFile(archive) as source:
    entries = source.infolist()
    if len(entries) > 10000 or sum(item.file_size for item in entries) > 128 * 1024 * 1024:
        raise SystemExit("Source archive is too large")
    expected = f"drivecheck-{revision}"
    names = set()
    for item in entries:
        path = PurePosixPath(item.filename)
        kind = stat.S_IFMT(item.external_attr >> 16)
        if (
            path.is_absolute() or ".." in path.parts or "\\" in item.filename
            or not path.parts or path.parts[0] != expected
            or item.filename in names
            or kind not in (0, stat.S_IFREG, stat.S_IFDIR)
        ):
            raise SystemExit("Unsafe or unexpected source archive entry")
        names.add(item.filename)
    source.extractall(target)
root = target / f"drivecheck-{revision}"
for required in (
    "scripts/install-linux.sh", "scripts/uninstall-linux.sh", "deploy/drivecheck.service",
    "scripts/install-macos.sh", "scripts/uninstall-macos.sh", "scripts/check-idle.py",
    "scripts/wait-ready.py",
    "deploy/run-macos.sh",
    "drivecheck/__init__.py", "pyproject.toml", "requirements.lock", ".env.example",
    "LICENSE", "README.md",
):
    if not (root / required).is_file():
        raise SystemExit(f"Incomplete source archive: {required}")
PY
native_installer=install-linux.sh
if [[ "$platform" == Darwin ]]; then
  native_installer=install-macos.sh
fi
printf '%s\n' "$revision" >"$work_dir/source/drivecheck-$revision/DEPLOYED_REVISION"
if [[ "$no_start" == true ]]; then
  bash "$work_dir/source/drivecheck-$revision/scripts/$native_installer" --no-start
else
  bash "$work_dir/source/drivecheck-$revision/scripts/$native_installer"
fi
printf 'Installed revision: %s\n' "$revision"
