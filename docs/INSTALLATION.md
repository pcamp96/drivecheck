# Installation and maintenance

[Back to README](../README.md)

## Install on Raspberry Pi or Linux

Use a Pi 4/5 with a suitable power supply, a powered **single-drive** USB-to-SATA
dock with SMART passthrough, and separate boot storage. Install a supported
64-bit OS with Python 3.11+, network access, and an account with `sudo` privileges.
The native installer supports Debian-based systems with APT and systemd.

Run these commands **on the station**, over SSH or in its terminal:

```sh
curl -fsSL https://raw.githubusercontent.com/pcamp96/drivecheck/main/install.sh -o install-drivecheck.sh
sudo bash install-drivecheck.sh
sudo systemctl status drivecheck
sudo cat /var/lib/drivecheck/access-token
```

If `curl` is missing, install it with `sudo apt-get install curl ca-certificates`.
The single bootstrap file resolves a GitHub revision, downloads its source,
installs the native station, and removes its temporary download. Git and `uv`
are not needed. Tools and locked Python dependencies live in
`/opt/drivecheck/.venv`; the systemd service starts at boot.
Configuration lives in `/etc/drivecheck/drivecheck.env`; private settings,
reports, and the sign-in token live in `/var/lib/drivecheck`.
Rerunning the installer preserves these files and refuses to interrupt queued
or active tests and pending safe release.
The installer waits for the service, dashboard, and sign-in credentials before
reporting success. A failed startup exits with status/journal commands instead.
With `--no-start`, the generated token will not exist until the first startup.
If `DRIVECHECK_API_KEY` is configured, use that key; a token file is not generated.

Installer options:

- `--no-start`: install the boot-time service, leaving it stopped for configuration.
- `--ref TAG_OR_COMMIT`: install a specific branch, tag, or full commit SHA.
- `--dry-run`: resolve the revision and show the plan without installing.
- `--lan`: switch an existing local-only installation to LAN access without editing its environment file; other settings are preserved.

A source checkout still supports `sudo bash scripts/install-linux.sh`;
`scripts/install-pi.sh` is an alias for that native installer.

The service runs as root because raw block I/O and SMART ioctls require device
permissions. Fresh installations listen on `0.0.0.0`, so the dashboard is
available directly across the LAN. Open `http://YOUR_STATION_IP:8765` from your
computer using the station's IP address; the installer prints detected network
addresses. On the station itself, <http://127.0.0.1:8765> also works.

To switch an older local-only installation, download the current bootstrap
script and run `sudo bash install-drivecheck.sh --lan`. On macOS, run
`bash install-drivecheck.sh --lan` without sudo. This preserves other settings,
reports, and credentials, and refuses to interrupt unfinished jobs.
Ordinary updates retain an explicitly configured bind address.

Local-only listening is optional: set `DRIVECHECK_HOST=127.0.0.1` in
`/etc/drivecheck/drivecheck.env` and restart. You can then access it remotely
using an SSH tunnel from the computer with the browser:

```sh
ssh -N -L 8765:127.0.0.1:8765 YOUR_USER@YOUR_STATION
```

For a TLS reverse proxy set `DRIVECHECK_PUBLIC_ORIGIN` to its exact HTTPS origin
and `DRIVECHECK_SECURE_COOKIE=true`; the app does not trust forwarded headers.
Authentication is required for drive controls and reports. Keep the dashboard
on your trusted network; HTTP is unencrypted and the port should not be forwarded
to the internet. SSH forwarding or HTTPS remains available on shared networks.

## Install on macOS

Install [Homebrew](https://brew.sh) first. A working `python3` (3.8+) is needed
to unpack the download; Homebrew's installer prerequisites normally provide
Apple's Python. The DriveCheck installer adds Homebrew Python 3.13, `fio`, and
`smartmontools`. Run it as your normal user, **without sudo**, so Homebrew can
install its packages. It requests sudo for the station service:

```sh
curl -fsSL https://raw.githubusercontent.com/pcamp96/drivecheck/main/install.sh -o install-drivecheck.sh
bash install-drivecheck.sh
sudo cat /var/db/drivecheck/access-token
```

Open `http://YOUR_MAC_IP:8765` from another computer on the LAN, or
<http://127.0.0.1:8765> on the Mac itself, and sign in with that token. The native launchd
service starts at boot, with no terminal left open. The app lives in
`/opt/drivecheck`, configuration in `/etc/drivecheck/drivecheck.env`, and private
settings/reports in `/var/db/drivecheck`. It supports the same installer options
and preserves configuration on updates.

```sh
sudo launchctl print system/org.drivecheck.station
sudo tail -f /var/db/drivecheck/station-error.log
```

With `--no-start`, start it after configuration using:

```sh
sudo launchctl bootstrap system /Library/LaunchDaemons/org.drivecheck.station.plist
```

Close files and applications using the target, then choose **Unmount for
testing**; busy volumes are refused. Quick and Extended use raw reads. Manual
Quick format, disk initialization, and full overwrite require confirmation.
The firmware Secure Erase command and Linux RAID takeover are unavailable on
macOS. Unsupported USB SMART produces incomplete coverage. macOS does not offer
Linux's block-device exclusion; fresh identity and mount checks still apply.
Use a dedicated dock and avoid mounting the disk during a job. Internal,
system-backed, virtual, ambiguous, and unidentified disks remain blocked.

For development or a foreground launch, see [Contributing](../CONTRIBUTING.md).
Stop the installed service before running another process on its data directory.

## Try the simulation

Simulation lets you explore the dashboard without connecting a drive. With
Python 3.11+ and [uv](https://docs.astral.sh/uv/) installed, run:

```sh
git clone https://github.com/pcamp96/drivecheck.git
cd drivecheck
uv sync --locked
uv run --locked drivecheck --demo
```

Open <http://127.0.0.1:8765> and sign in using `data/access-token`. If you already
cloned the repository, run the last two commands from its root instead.
Simulation never discovers or reads host devices. Results and exported reports
are marked simulated. Configured notifications still send **real messages** when
you press Send test notification or enable notices. Stop the process with Ctrl+C.
Windows supports this mode only.

## Update a station

Wait for tests and safe release to finish, then download and rerun the same
installer commands for your OS. You do not need a source checkout or `git pull`.
The downloaded revision is recorded in `/opt/drivecheck/DEPLOYED_REVISION`.
The installer preserves configuration, settings, reports, and the access token,
and refuses updates while unfinished work is recorded. Interrupted tests and
erase jobs never resume automatically.

## Reports and operations

Select a test in History to inspect every result and its log. Download a readable
report for review or a JSON report for troubleshooting and archiving. Reports include raw SMART/fio data,
selected profile, coverage, identity, timestamps, and a simulation flag.
The dashboard displays the newest 200 runs; older reports remain in SQLite and
are available by their run ID. The result view exposes raw detail alongside key speed.

On Linux:

```sh
sudo journalctl -u drivecheck -f
sudo systemctl restart drivecheck
sudo systemctl stop drivecheck
```

On macOS, stop with `sudo launchctl bootout system/org.drivecheck.station` and
start with the `launchctl bootstrap` command above. Logs live in `/var/db/drivecheck`.

Back up the platform data directory **while the service is stopped**, including settings,
SQLite, and the access token. Keep that directory private. To rotate the generated
access token, stop the service, remove only `access-token`, and restart. Sessions
expire after 12 hours or a service restart. There must be one process/one worker
per data directory; a filesystem lock enforces this.

## Uninstall or reinstall

Both native installers add a removal command:

```sh
sudo /usr/local/sbin/drivecheck-uninstall
```

It stops/disables the service and removes the application, its private Python
environment, service unit, and uninstall command. It preserves
`/etc/drivecheck` and the platform data directory (`/var/lib/drivecheck` on Linux,
`/var/db/drivecheck` on macOS), including settings, reports and the
sign-in token, so reinstalling can restore the station. To also permanently
remove those DriveCheck settings and reports, explicitly run:

```sh
sudo /usr/local/sbin/drivecheck-uninstall --purge-data
```

Shared APT/Homebrew dependencies and existing system logs remain. Any source
checkout you created is retained. The Linux installation manifest records added
packages.
The uninstaller refuses unmanaged or symbolic-link installation paths. If the
command has already been removed, run `sudo bash scripts/uninstall-linux.sh`
from a source checkout (or `scripts/uninstall-macos.sh` on macOS).
Stop active tests before uninstalling.
