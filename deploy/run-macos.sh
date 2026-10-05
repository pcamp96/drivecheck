#!/bin/bash
set -euo pipefail
umask 077
cd /opt/drivecheck
set -a
source /etc/drivecheck/drivecheck.env
set +a
exec /opt/drivecheck/.venv/bin/drivecheck --hardware
