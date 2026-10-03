# DriveCheck

A dedicated hard-drive intake station for Raspberry Pi. Connect an unmounted
USB drive, run a repeatable set of checks, and review a live dashboard and saved
report before putting the drive into service. Configurable Discord or Telegram
notifications deliver the outcome. Built for a single station, one drive test at
a time, with no cloud service or frontend build required.

**Status:** implemented and verified in simulation and isolated tests. Real Pi,
USB dock, SMART passthrough, physical I/O, and real provider delivery still require
owner testing. The app never claims a successful check predicts future lifespan.

## Preview on this computer

Python 3.11+ and [uv](https://docs.astral.sh/uv/) required for these commands:

```sh
cd ~/development/drivecheck
uv sync --extra dev
uv run drivecheck --demo
```

Open <http://127.0.0.1:8765> and sign in using the token in `data/access-token`.
Demo mode uses synthetic drive information and simulated results; it never
inspects or reads host devices. A demo notification is a **real message** if you
configure a provider and press Send test notification or enable test notices.
All demo notifications and downloaded reports are labelled as simulated.

## Install on the Pi

Use Raspberry Pi OS Bookworm or newer, **64-bit**, Python 3.11+, a Pi 4/5 and a
powered USB-to-SATA dock with working SMART passthrough. Boot the Pi from separate
storage. Copy this folder to the Pi using your preferred transfer method (a Git
remote is not required), then run:

```sh
cd drivecheck
sudo bash scripts/install-pi.sh
sudo systemctl status drivecheck
sudo cat /var/lib/drivecheck/access-token
```

The installer installs Debian's `fio` (Flexible I/O Tester), `smartmontools`, and
`util-linux`, installs the locked Python dependencies, and creates a systemd
service. It preserves existing station settings when rerun. Stop the service
before updating the installed application. It runs as root because raw block
I/O and SMART ioctls need device permissions. Its web listener defaults to
loopback; use an SSH tunnel from your computer:

```sh
ssh -L 8765:127.0.0.1:8765 YOUR_USER@YOUR_PI
```

Then browse <http://127.0.0.1:8765>. To use a trusted LAN directly, set
`DRIVECHECK_HOST=0.0.0.0` in `/etc/drivecheck/drivecheck.env` and restart the
service. Plain HTTP exposes the sign-in token/session to anyone able to observe
that connection; use SSH forwarding or HTTPS on shared/untrusted networks. Do
not forward the service port to the internet. For a TLS reverse proxy set
`DRIVECHECK_PUBLIC_ORIGIN` to its exact HTTPS origin and
`DRIVECHECK_SECURE_COOKIE=true`; the app does not trust forwarded headers.

## Profiles and automatic intake

| Profile | Checks | Drive writes |
| --- | --- | --- |
| Quick | SMART before/after and a 30-second sequential read sample | None |
| Extended | SMART, extended drive self-test, read benchmark, full read scan, final SMART | None |
| Write verification | Extended self-test, read benchmark, full-drive write and checksum readback, SMART before/after | **Entire selected drive overwritten** |

Choose a drive on the dashboard and start Quick or Extended. Enable automatic
intake in Settings to queue **Extended, read-only** checks on eligible drives.
Each drive is attempted once per observed connection. On service restart,
already-recorded drives aren't automatically retested; run a manual test or
unplug/reconnect to retry. Unplugging and reconnecting between discovery polls
may not be observed; use a manual test in that case.

Eligibility is conservative: only unmounted USB disks with a unique serial and
valid capacity are accepted. System storage, mounted partitions, active swap,
internal disks, missing serials and duplicate bridge identities are blocked.
Disable desktop automount on the Pi; run the service on a dedicated station.
Review the identity and capacity before starting a test. All phases recheck
identity; an exclusive block-device claim prevents a new mount during raw I/O.
A disconnect or changed identity stops the test as incomplete.
A raw full-drive write destroys partitions and files. It is never auto-triggered.

To deliberately enable full-drive write verification, change
`DRIVECHECK_ALLOW_DESTRUCTIVE=true` in the station environment and restart the
service. The dashboard then requires typing `ERASE <exact serial>` for each job.
Queued/running jobs are recorded as incomplete on restart, and erase jobs never
resume automatically. Cancellation is best effort for the drive's internal
self-test; review its status before disconnecting.

A full surface pass on a large HDD takes hours. The benchmark samples the start
of the disk and measures the whole Pi/USB/drive path; it is not a complete drive
performance characterization. The full read scan checks readability, while
write verification checks newly written test data. Unsupported SMART/self-tests
produce an **incomplete** report, not a pass. Existing SMART warnings remain
visible even when the selected I/O test succeeds.

## Notifications

Open **Settings** and choose Discord or Telegram. Enable notifications and save.
Blank secret inputs preserve existing credentials. Stored secrets have mode
0600 and never appear in API snapshots or downloaded reports. Disable delivery
before forgetting saved credentials. Optional start notices are off by default;
completion, failure, cancellation, and incomplete results are delivered when
notifications are enabled.

- **Discord:** Create an incoming webhook for a standard text channel in channel
  settings, then paste its HTTPS URL. DriveCheck uses `wait=true` to confirm
  acceptance and suppresses mentions. Forum channels requiring a thread are not
  supported by the initial UI.
- **Telegram:** Create a bot through BotFather, start a conversation with it (or
  add it to your target group), then enter its token and numeric chat ID. A bot
  cannot initiate a private conversation before you start it. The bot must have
  permission to post in its target chat.

Press **Send test notification** to check credentials and destination. Outages
retry through a persistent SQLite outbox with exponential backoff, up to six
attempts. Failure is visible on the dashboard; test reports remain saved.
Delivery is at-least-once: an ambiguous network response or crash immediately
after provider acceptance can lead to a duplicate. Pending notices use the
currently selected provider when retried. Failed outbox records stay on disk;
the initial app does not provide a manual retry action after six attempts.

## Reports and operations

Select a test in History to inspect every result and its log. Download the JSON
report for article evidence or archiving. Reports include raw SMART/fio data,
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

## Development verification

```sh
uv run --extra dev pytest
uv run --extra dev ruff check .
uv run --extra dev ruff format --check .
node --check drivecheck/static/app.js
uv run --extra dev playwright install chromium
uv run --extra dev python scripts/verify-browser.py
```

See [Monday's test plan](docs/MONDAY-TEST-PLAN.md),
[architecture and API](docs/ARCHITECTURE.md), and
[implementation checkpoints](docs/IMPLEMENTATION.md).

## Primary references

- [smartctl manual source](https://github.com/smartmontools/smartmontools/blob/master/smartmontools/smartctl.8.in)
- [fio documentation](https://fio.readthedocs.io/en/latest/fio_doc.html)
- [Raspberry Pi USB and power documentation](https://www.raspberrypi.com/documentation/computers/raspberry-pi.html)
- [FastAPI application lifespan](https://fastapi.tiangolo.com/advanced/events/)
- [Discord incoming webhooks](https://docs.discord.com/developers/resources/webhook)
- [Telegram Bot API](https://core.telegram.org/bots/api#sendmessage)

No remote repository or public release has been created. Licensing and public
packaging can be decided after physical testing.
