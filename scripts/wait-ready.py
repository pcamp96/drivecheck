"""Verify the started native station before an installer reports success."""

import argparse
import json
import re
import shlex
import subprocess
import time
import urllib.error
import urllib.request
from pathlib import Path


def configuration(path, platform="linux"):
    values = {}
    for line in path.read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        key, separator, raw = line.removeprefix("export ").partition("=")
        if separator and key.strip().startswith("DRIVECHECK_"):
            # systemd recognizes comment lines, but '#' inside an assignment
            # remains literal. The macOS launcher uses shell comment rules.
            values[key.strip()] = " ".join(shlex.split(raw, comments=platform == "macos"))
    return values


def service_pid(platform):
    if platform == "linux":
        result = subprocess.run(
            ["systemctl", "show", "drivecheck.service", "--property=ActiveState",
             "--property=SubState", "--property=MainPID", "--property=Result"],
            capture_output=True, text=True, timeout=5,
        )
        values = dict(line.split("=", 1) for line in result.stdout.splitlines() if "=" in line)
        if values.get("ActiveState") == "failed" or values.get("SubState") == "auto-restart":
            raise RuntimeError("DriveCheck exited during startup")
        if result.returncode == 0 and values.get("ActiveState") == "active":
            return int(values.get("MainPID") or 0)
    else:
        result = subprocess.run(
            ["launchctl", "print", "system/org.drivecheck.station"],
            capture_output=True, text=True, timeout=5,
        )
        match = re.search(r"^\s*pid = (\d+)\s*$", result.stdout, re.MULTILINE)
        if result.returncode == 0 and match:
            return int(match.group(1))
    return 0


def wait_ready(path, platform, timeout=60):
    values = configuration(path, platform)
    host = values.get("DRIVECHECK_HOST", "127.0.0.1")
    host = {"0.0.0.0": "127.0.0.1", "::": "::1"}.get(host, host)
    port = int(values.get("DRIVECHECK_PORT", "8765"))
    if not 0 < port < 65536:
        raise ValueError("Invalid dashboard port")
    url_host = f"[{host}]" if ":" in host else host
    base = f"http://{url_host}:{port}"
    default_data = "/var/lib/drivecheck" if platform == "linux" else "/var/db/drivecheck"
    data_dir = Path(values.get("DRIVECHECK_DATA_DIR", default_data)).expanduser()
    if not data_dir.is_absolute():
        data_dir = Path("/opt/drivecheck") / data_dir
    token_file = data_dir / "access-token"
    # The health/auth probe stays on the configured station endpoint. Never
    # forward a private token via a proxy inherited from the installer shell.
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    deadline = time.monotonic() + timeout
    reason = "waiting for the service process"
    while time.monotonic() < deadline:
        try:
            pid = service_pid(platform)
            if not pid:
                reason = "waiting for the service process"
            else:
                key = values.get("DRIVECHECK_API_KEY", "")
                if not key and token_file.is_file():
                    key = token_file.read_text().strip()
                if not key:
                    reason = "waiting for the sign-in token"
                else:
                    request_timeout = min(2, max(0.01, deadline - time.monotonic()))
                    with opener.open(base + "/api/health", timeout=request_timeout) as response:
                        health = json.load(response)
                    if health.get("status") != "ok" or health.get("pid") != pid:
                        reason = "dashboard endpoint does not match the started service"
                    else:
                        request = urllib.request.Request(
                            base + "/api/state", headers={"Authorization": f"Bearer {key}"}
                        )
                        with opener.open(request, timeout=request_timeout) as response:
                            state = json.load(response)
                        if state.get("mode") == "hardware" and service_pid(platform) == pid:
                            return base, token_file, bool(values.get("DRIVECHECK_API_KEY"))
                        reason = "waiting for the hardware station"
        except urllib.error.HTTPError as error:
            reason = f"dashboard returned HTTP {error.code}"
        except (OSError, ValueError, urllib.error.URLError, subprocess.TimeoutExpired):
            reason = "waiting for the dashboard and valid sign-in credentials"
        time.sleep(min(0.25, max(0, deadline - time.monotonic())))
    raise RuntimeError(f"DriveCheck did not become ready within {timeout:g} seconds ({reason})")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("config", type=Path)
    parser.add_argument("--platform", choices=("linux", "macos"), required=True)
    parser.add_argument("--timeout", type=float, default=60)
    args = parser.parse_args()
    try:
        base, token_file, fixed_key = wait_ready(args.config, args.platform, args.timeout)
    except RuntimeError as error:
        print(f"DriveCheck startup verification failed: {error}.")
        diagnostics(args.platform)
        raise SystemExit(1) from None
    except (OSError, ValueError, subprocess.TimeoutExpired):
        # Don't expose exception data: malformed environment values and HTTP
        # requests could contain credentials. The local journal has the cause.
        print("DriveCheck startup verification failed. Application files and settings are retained.")
        diagnostics(args.platform)
        raise SystemExit(1) from None
    print(f"Dashboard ready: {base}")
    if fixed_key:
        print("Sign in with the configured DRIVECHECK_API_KEY; no access-token file is generated.")
    else:
        print(f"Sign-in token: sudo cat {shlex.quote(str(token_file))}")


def diagnostics(platform):
    print("Application files and settings are retained.")
    if platform == "linux":
        print("Check: sudo systemctl status drivecheck --no-pager -l")
        print("Logs: sudo journalctl -u drivecheck -n 60 --no-pager")
    else:
        print("Check: sudo launchctl print system/org.drivecheck.station")
        print("Logs: sudo tail -n 60 /var/db/drivecheck/station-error.log")


if __name__ == "__main__":
    main()
