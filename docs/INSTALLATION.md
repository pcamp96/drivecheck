# Installation and maintenance

[Back to README](../README.md)

## Install on Raspberry Pi or Linux

Use a Pi 4/5 with a suitable power supply, a powered **single-drive** USB-to-SATA
dock with SMART passthrough, and separate boot storage. Install a supported
64-bit OS with Python 3.11+, network access, and an account with `sudo` privileges.
The native installer supports Debian-based systems with APT and systemd.

Run these commands **on the station**, over SSH or in its terminal. Clone the
repository into any directory you choose; no particular home-directory layout
is required:

```sh
sudo apt-get update
sudo apt-get install -y git
git clone https://github.com/pcamp96/drivecheck.git
cd drivecheck
sudo bash scripts/install-linux.sh
sudo systemctl status drivecheck
sudo cat /var/lib/drivecheck/access-token
```

The installer installs the required system tools and locked Python dependencies
into `/opt/drivecheck/.venv`, then enables the `drivecheck` systemd service at boot.
You do not need `uv` or development dependencies for this installation.
Configuration lives in `/etc/drivecheck/drivecheck.env`; private settings, reports,
and the generated sign-in token live in `/var/lib/drivecheck`. Rerunning the
installer preserves station settings. `scripts/install-pi.sh` is an alias.

The service runs as root because raw block I/O and SMART ioctls require device
permissions. Its dashboard listens on loopback by default. From a **separate
computer**, open an SSH tunnel; replace `YOUR_USER` and `YOUR_PI` with the station's
SSH username and hostname or IP address:

```sh
ssh -L 8765:127.0.0.1:8765 YOUR_USER@YOUR_PI
```

Then browse <http://127.0.0.1:8765>. To use a trusted LAN directly, set
`DRIVECHECK_HOST=0.0.0.0` in `/etc/drivecheck/drivecheck.env` and restart the
service, then open `http://YOUR_PI:8765` using the station's hostname or IP address.
Plain HTTP exposes the sign-in token/session to anyone able to observe
that connection; use SSH forwarding or HTTPS on shared/untrusted networks. Do
not forward the service port to the internet. For a TLS reverse proxy set
`DRIVECHECK_PUBLIC_ORIGIN` to its exact HTTPS origin and
`DRIVECHECK_SECURE_COOKIE=true`; the app does not trust forwarded headers.

## Use on macOS

Install Python 3.11+, [uv](https://docs.astral.sh/uv/), and the I/O tools
(`brew install fio smartmontools` when using Homebrew). Clone the repository into
any directory, then create its virtual environment:

```sh
git clone https://github.com/pcamp96/drivecheck.git
cd drivecheck
uv sync --locked
uv run --locked drivecheck --hardware
```

This launch supports inventory without root. Open <http://127.0.0.1:8765> and use
the token in `data/access-token`. The dashboard explains missing tools and
permissions. Stop this process with Ctrl+C before launching a privileged testing
station from the same checkout:

```sh
sudo env PATH="$PATH" "$PWD/.venv/bin/drivecheck" --hardware --data-dir /var/db/drivecheck
```

Leave that process running. In another terminal, read its sign-in token:

```sh
sudo cat /var/db/drivecheck/access-token
```

Use that station's token at <http://127.0.0.1:8765>. Close files and applications
using the target, then choose **Unmount for testing**; busy volumes are refused.
Quick and Extended use raw reads. Destructive operations are disabled because
macOS cannot provide Linux's exclusive block-device claim. Mount state and
identity are checked throughout the test; use a dedicated dock and avoid mounting
the disk during a run. Unsupported USB SMART produces incomplete coverage.
Normal eject uses `diskutil eject`; internal, system-backed, virtual, ambiguous,
and unidentified disks remain blocked.

The systemd installer is Linux-only. The macOS commands above run in the
foreground; they do not install a boot-time service.

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

## Update a Linux station

Wait for all tests and safe-eject work to finish before updating. In the source
checkout used for installation, run:

```sh
sudo systemctl stop drivecheck
git pull --ff-only
sudo bash scripts/install-linux.sh
sudo systemctl status drivecheck
```

The installer preserves configuration, saved settings, reports, and the access
token. Do not restart during an erase operation. Interrupted tests are marked
incomplete; neither tests nor erase jobs resume automatically.

## Reports and operations

Select a test in History to inspect every result and its log. Download a readable
report for review or a JSON report for troubleshooting and archiving. Reports include raw SMART/fio data,
selected profile, coverage, identity, timestamps, and a simulation flag.
The dashboard displays the newest 200 runs; older reports remain in SQLite and
are available by their run ID. The result view exposes raw detail alongside key speed.

```sh
sudo journalctl -u drivecheck -f
sudo systemctl restart drivecheck
sudo systemctl stop drivecheck
```

Back up `/var/lib/drivecheck` **while the service is stopped**, including settings,
SQLite, and the access token. Keep that directory private. To rotate the generated
access token, stop the service, remove only `access-token`, and restart. Sessions
expire after 12 hours or a service restart. There must be one process/one worker
per data directory; a filesystem lock enforces this.

## Uninstall or reinstall

The Linux installer adds a removal command:

```sh
sudo drivecheck-uninstall
```

It stops/disables the service and removes the application, its private Python
environment, service unit, and uninstall command. It preserves
`/etc/drivecheck` and `/var/lib/drivecheck`, including settings, reports and the
sign-in token, so reinstalling can restore the station. To also permanently
remove those DriveCheck settings and reports, explicitly run:

```sh
sudo drivecheck-uninstall --purge-data
```

Shared APT dependencies, system journal entries, and your source checkout remain
installed. The installation manifest records packages that DriveCheck added.
The uninstaller refuses unmanaged or symbolic-link installation paths. If the
command has already been removed, run `sudo bash scripts/uninstall-linux.sh`
from the source checkout. Stop active tests before uninstalling.
