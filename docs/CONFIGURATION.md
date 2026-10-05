# Headless operation and notifications

[Back to README](../README.md)

## Completely headless Pi intake

1. Open **Settings** in the dashboard and configure
   [Telegram or Discord notifications](#notifications). Enable notifications,
   save them, and use **Send test notification** to verify the destination.
2. Disable automatic mounting on the station. If it uses `udiskie` as a user
   service, run `systemctl --user disable --now udiskie.service` as that user.
   Other desktop environments may provide their own automount setting.
3. Set `DRIVECHECK_HEADLESS=true` in the protected service environment file:

```sh
sudo nano /etc/drivecheck/drivecheck.env
```

Alternatively, manage notifications through that environment file. For Discord,
add `DRIVECHECK_NOTIFICATION_PROVIDER=discord` and
`DRIVECHECK_DISCORD_WEBHOOK=<your webhook URL>`. For Telegram, use
`DRIVECHECK_NOTIFICATION_PROVIDER=telegram`, `DRIVECHECK_TELEGRAM_TOKEN=<bot token>`,
and `DRIVECHECK_TELEGRAM_CHAT_ID=<chat ID>`. Restart the service after configuration:

```sh
sudo systemctl restart drivecheck
sudo journalctl -u drivecheck -f
```

Confirm the **DriveCheck is ready** message arrives before docking your first
drive. Keep the drive unmounted; automatic intake never takes control of mounted
filesystems. The default action window is 180 seconds and is configurable in
Settings. See the [validation checklist](HARDWARE-VALIDATION.md) for the
first test with your particular dock.

No browser, monitor, or keyboard is needed afterward. Dock an eligible, unmounted
USB drive: the service runs Quick read-only checks, saves the report, and sends
the result message. It holds a passed/warning drive for 180 seconds so you can
choose **Run Extended** or **Eject now** in Telegram or the dashboard. With no
choice, it safely powers off
the drive through UDisks. A separate message says **🟢 Safe to remove** only after
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
Successful manual Quick tests use the same 180-second action window as automatic intake when automatic eject is enabled.
Completion messages offer **Run Extended**, **Eject now**, and available manual erase options.
After release, **Reconnect / rescan** refreshes the drive inventory. Power-cycle the
dock or reconnect USB if the drive was powered off. If it is idle and eligible,
Telegram offers fresh **Quick test**, **Extended test**, and available erase buttons.
These refreshed buttons expire after five minutes and can only queue one job.
If automatic intake already started, rescan reports that job rather than queuing a duplicate.

Buttons only apply to the original, safely identified drive; expired,
replayed, missing, mounted, or replaced drive actions are rejected. When manual
erase is enabled, **Quick erase**, **Initialize/reset disk**, **Firmware secure erase**,
and **Full erase** buttons open a confirmation
prompt; pressing the button does not write to the drive. Reply to that exact
prompt with `QUICK FORMAT <exact serial> <volume path>`,
`INITIALIZE DISK <exact serial>`, `SECURE ERASE <exact serial>`, or
`FULL ERASE <exact serial>` within
120 seconds (or the remaining action window, whichever is shorter). Only the
configured chat and authorized sender can confirm. The original eject countdown
continues while the prompt is open. Discord only sends notifications.

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

Messages use separate headings for station readiness, testing, results, and safe
removal. Chat shows the drive identity, a brief result or failure reason, and the
next action. Full evidence stays in the dashboard and readable report attachments.

After safe power-off, reconnect or cycle the dock's power before another test.
Filesystem mounting cannot bring an ejected USB device back online. Quick runs the drive's short SMART self-test and a read sample; it does not establish full-surface health.
