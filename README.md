# DriveCheck

A hard-drive intake station for Raspberry Pi, with read-only hardware support on macOS. Connect an unmounted
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

## Connected drives on macOS

The default preview is simulated. Use hardware mode to discover actual external
physical disks, including their mounted volumes:

```sh
cd ~/development/drivecheck
uv run drivecheck --hardware --data-dir data/macos
```

This works without root for inventory. The dashboard explains missing tools and
permissions. Install the real I/O tools with `brew install fio smartmontools`.
Stop the inventory process, then launch the existing virtual environment as root
to run raw read tests (use a separate private data directory):

```sh
sudo env PATH="$PATH" "$PWD/.venv/bin/drivecheck" --hardware --data-dir /var/db/drivecheck --port 8766
```

Open <http://127.0.0.1:8766> and sign in with `/var/db/drivecheck/access-token`.
Port 8766 lets this testing station run alongside the inventory preview. Close files/apps using the target,
then select **Unmount for testing**. This uses normal macOS unmounting and refuses
busy volumes. Quick and Extended read the raw disk; they never write test data.
macOS write verification is disabled because the Linux exclusive block claim is
not available. Mount state and identity are checked throughout read I/O, but
macOS cannot provide that Linux mount exclusion guarantee. Use a dedicated test
dock and avoid mounting the disk during a run. USB SMART passthrough varies by
bridge/driver; unsupported health/self-test checks produce incomplete reports.
Eject uses `diskutil eject`. Internal, system-backed, virtual, ambiguous, or
unidentified disks are blocked. Windows currently supports simulation only.

## Install on Linux (Raspberry Pi or Ubuntu)

Use Raspberry Pi OS Bookworm or newer, **64-bit**, Python 3.11+, a Pi 4/5 and a
powered USB-to-SATA dock with working SMART passthrough. Ubuntu hosts with Python
3.11+ are also supported. Boot from separate storage. Copy this folder to the Pi using your preferred transfer method (a Git
remote is not required), then run:

```sh
cd drivecheck
sudo bash scripts/install-linux.sh
sudo systemctl status drivecheck
sudo cat /var/lib/drivecheck/access-token
```

The installer installs Debian's `fio` (Flexible I/O Tester), `smartmontools`, and
`util-linux` and `udisks2`, installs the locked Python dependencies, and creates a systemd
service. `install-pi.sh` remains an alias. It preserves existing station settings when rerun. Stop the service
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

## Completely headless Pi intake

After installation, either configure notifications once in the dashboard or put
your provider credentials into the protected service environment file:

```sh
sudo nano /etc/drivecheck/drivecheck.env
```

For Discord, add `DRIVECHECK_NOTIFICATION_PROVIDER=discord` and
`DRIVECHECK_DISCORD_WEBHOOK=<your webhook URL>`. For Telegram, use
`DRIVECHECK_NOTIFICATION_PROVIDER=telegram`, `DRIVECHECK_TELEGRAM_TOKEN=<bot token>`,
and `DRIVECHECK_TELEGRAM_CHAT_ID=<chat ID>`. Set `DRIVECHECK_HEADLESS=true`, then:

```sh
sudo systemctl restart drivecheck
sudo journalctl -u drivecheck -f
```

No browser, monitor, or keyboard is needed afterward. Dock an eligible, unmounted
USB drive: the service runs Quick read-only checks, saves the report, and sends
the result message. It holds a passed/warning drive for 180 seconds so you can
choose **Run Extended** or **Eject now** in Telegram or the dashboard. With no
choice, it safely powers off
the drive through UDisks. A second message says **ready to remove** only after
release is confirmed. The dashboard remains available for observation.
Headless mode requires tools, raw I/O permissions, and a valid enabled provider
at startup. It forces automatic testing and eject; write tests stay manual.
Keep desktop automount disabled on this dedicated Pi. Mounted drives remain
blocked; the unattended station never automatically unmounts your filesystems.

If the network is down, the report and message stay queued; eject still proceeds
after the bounded action/delivery wait. Notification and eject outcomes are recorded
separately from the test verdict. Failed or unsupported eject never produces a
ready-to-remove confirmation. Cancellation does not automatically eject. An
interrupted post-test release is recorded and never blindly resumed on restart.
Failed drive tests also take this release path when automatic eject is enabled;
the failed verdict stays in the report even after a successful eject.
An already tested drive isn't repeatedly scanned while it remains connected;
the station must observe a detach before automatic retesting.

Use a single-drive USB dock for unattended power-off. UDisks may affect sibling
drives sharing one bridge; DriveCheck refuses power-off when it cannot establish
an isolated device. Physically verify the dock's behavior before relying on it.
Environment-managed provider settings are read-only in the dashboard. Without
provider environment variables, stored dashboard settings remain configurable.


## Telegram controls and dashboard links

Telegram is the primary remote interface; Discord remains notification-only.
Use a dedicated Telegram bot for each station. The station receives button presses
through outbound long polling, so it does not require an inbound public endpoint.
An existing bot webhook must be removed deliberately before long polling can work;
DriveCheck reports that conflict and does not alter another application's webhook.

For a private numeric chat ID, only the user with that same numeric ID can control
tests. In groups, configure **Authorized Telegram user ID** (or
`DRIVECHECK_TELEGRAM_USER_ID`) in addition to the chat ID. Both chat and sender must
match. Channel usernames can receive notices but cannot authorize test controls.
Buttons only apply to the current connected drive during its action window; expired,
replayed, missing, mounted, or replaced drive actions are rejected. Writes cannot
be started from Telegram buttons. Use the dashboard's separately enabled and
serial-confirmed write verification flow when deliberately erasing a drive.

In groups/channels, the dashboard link requires the normal station login; sign-in
grants are sent only to the authorized private numeric chat.
Set `DRIVECHECK_PUBLIC_ORIGIN` to the station URL reachable from your phone, such
as your Tailscale address. Private Telegram messages then include **Open dashboard** with
a one-use sign-in link valid for ten minutes. Anyone receiving that link can sign
in until it is used or expires, so use a private trusted chat. The link uses a
short-lived grant, never the persistent station token; link previews do not redeem
it. Opening the dashboard does not pause the eject timer. Restarting the station
invalidates outstanding links and action buttons. The dashboard URL still needs
to be reachable from the phone (for example, connected to your tailnet).

After safe power-off, reconnect or cycle the dock's power before another test.
Filesystem mounting cannot bring an ejected USB device back online. Quick is only
a brief health screen and read sample; it does not establish full-surface health.

## Profiles and automatic intake

| Profile | Checks | Drive writes |
| --- | --- | --- |
| Quick | SMART before/after and a 30-second sequential read sample | None |
| Extended | SMART, extended drive self-test, read benchmark, full read scan, final SMART | None |
| Write verification | Extended self-test, read benchmark, full-drive write and checksum readback, SMART before/after | **Entire selected drive overwritten** |

Choose a drive on the dashboard and start Quick or Extended. Enable automatic
intake in Settings to queue **Quick, read-only** checks on eligible drives.
The automatic eject delay defaults to 180 seconds; set it to zero for immediate
eject. Failed or incomplete tests skip the action window. Manually requested
Extended tests eject on completion when automatic eject is enabled.
Each drive is attempted once per observed connection. On service restart,
already-recorded drives aren't automatically retested; run a manual test or
unplug/reconnect to retry. Unplugging and reconnecting between discovery polls
may not be observed; use a manual test in that case.

The dashboard's Settings view uses one vertical form. Automation switches save
immediately and show success or an error; notification edits use Save settings.
SMART health and self-test reports explain the outcome and available evidence in
plain language. Raw JSON remains available under Technical details and Export JSON.
Export readable report downloads the same UTF-8 text format used for failure
attachments. Failed-test notifications include the failing step and specific
reason, such as a surface-read failure and its reported block. Telegram receives
one document message with that explanation as its caption; Discord receives the
explanation and text file in one webhook message. The notification outbox stores
the report snapshot with the message so retries preserve the attachment, including
across a restart. Notification acceptance is recorded only after the provider
confirms the attachment. Startup and successful-test messages remain text-only.

Completed reports offer safe eject and read-only retest controls. After Linux
USB power-off or macOS eject, reconnect the drive or power-cycle its dock first.
The operating system has removed the device; a filesystem mount cannot bring it
back. The retest button checks for the same uniquely identified, unmounted drive
and always queues Extended read-only checks, even for a historical erase report.
If automatic intake already queued that reconnected drive, the button opens the
existing job instead of starting a duplicate. A mounted drive stays blocked.

Eligibility is conservative: only unmounted external disks (USB on Linux) with a unique serial and
valid capacity are accepted. System storage, mounted partitions, active swap,
internal disks, missing serials and duplicate bridge identities are blocked.
Disable desktop automount on the Pi; run the service on a dedicated station.
Review the identity and capacity before starting a test. All phases recheck
identity; on Linux an exclusive block-device claim prevents a new mount during raw I/O.
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
notifications are enabled. **Notify when the station is ready after startup** is
on by default: once startup/discovery succeeds, the station sends its boot time
and readiness status through the same provider. A network outage queues that
notice for retry; a restart replaces any undelivered notice from the previous
boot. Startup failures never announce readiness. For environment-managed
notifications, set `DRIVECHECK_NOTIFY_READY=false` to turn this off.

- **Discord:** Create an incoming webhook for a standard text channel in channel
  settings, then paste its HTTPS URL. DriveCheck uses `wait=true` to confirm
  acceptance and suppresses mentions. Forum channels requiring a thread are not
  supported by the initial UI.
- **Telegram:** Create a bot through BotFather, start a conversation with it (or
  add it to your target group), then enter its token and numeric chat ID. A bot
  cannot initiate a private conversation before you start it. The bot must have
  permission to post in its target chat.

Press **Send test notification** to check credentials and destination. After
saving an enabled provider, restart the service with
`sudo systemctl restart drivecheck` to check the startup-ready notice. Outages
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
- [UDisks safe power-off](https://storaged.org/doc/udisks2-api/latest/udisksctl.1.html)
- [macOS diskutil manual](https://keith.github.io/xcode-man-pages/diskutil.8.html)
- [fio documentation](https://fio.readthedocs.io/en/latest/fio_doc.html)
- [Raspberry Pi USB and power documentation](https://www.raspberrypi.com/documentation/computers/raspberry-pi.html)
- [FastAPI application lifespan](https://fastapi.tiangolo.com/advanced/events/)
- [Discord incoming webhooks](https://docs.discord.com/developers/resources/webhook)
- [Telegram Bot API](https://core.telegram.org/bots/api#sendmessage)

Source: [pcamp96/drivecheck](https://github.com/pcamp96/drivecheck).

## License

DriveCheck is licensed under the [MIT License](LICENSE), copyright 2026 Patrick
Campanale. Dependencies and system tools retain their respective licenses.
This source publication is a test build; physical acceptance remains pending.
