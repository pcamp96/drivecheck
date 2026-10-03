#!/usr/bin/env bash
set -euo pipefail

# Compatibility entry point retained for existing Pi instructions and bookmarks.
script_dir=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)
exec "$script_dir/install-linux.sh" "$@"
