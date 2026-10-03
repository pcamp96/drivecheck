#!/usr/bin/env bash
set -euo pipefail
if [[ $(uname -s) != Linux || $EUID -ne 0 ]]; then
  echo "Run this installer with sudo on the Raspberry Pi (Linux)." >&2
  exit 1
fi
source_dir=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd)
if systemctl is-active --quiet drivecheck; then
  echo "Stop DriveCheck before updating: sudo systemctl stop drivecheck" >&2
  exit 1
fi
python3 -c 'import sys; assert sys.version_info >= (3,11), "Python 3.11+ required (Pi OS Bookworm or newer)"'
apt-get update
apt-get install -y python3-venv python3-pip smartmontools fio util-linux udisks2
install -d -m 755 /opt/drivecheck /etc/drivecheck
install -d -m 700 /var/lib/drivecheck
cp -R "$source_dir/drivecheck" /opt/drivecheck/
install -m 644 "$source_dir/pyproject.toml" "$source_dir/requirements.lock" /opt/drivecheck/
python3 -m venv /opt/drivecheck/.venv
/opt/drivecheck/.venv/bin/pip install --require-hashes -r /opt/drivecheck/requirements.lock
/opt/drivecheck/.venv/bin/pip install --no-deps /opt/drivecheck
if [[ ! -e /etc/drivecheck/drivecheck.env ]]; then
  install -m 600 "$source_dir/.env.example" /etc/drivecheck/drivecheck.env
fi
install -m 644 "$source_dir/deploy/drivecheck.service" /etc/systemd/system/drivecheck.service
systemctl daemon-reload
systemctl enable --now drivecheck
printf '\nDriveCheck installed. Inspect status with: sudo systemctl status drivecheck\n'
printf 'Read sign-in token with: sudo cat /var/lib/drivecheck/access-token\n'
printf 'Forward dashboard: ssh -L 8765:127.0.0.1:8765 USER@PI\n'
printf 'Headless: configure provider and DRIVECHECK_HEADLESS=true in /etc/drivecheck/drivecheck.env, then restart.\n'
printf 'Then open http://127.0.0.1:8765 on your computer.\n'
