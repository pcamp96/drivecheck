# Tests, reports, and drive safety

[Back to README](../README.md)

## Profiles and automatic intake

| Profile | Checks | Drive writes |
| --- | --- | --- |
| Quick | SMART before/after, firmware short SMART self-test, and a 30-second sequential read sample | None |
| Extended | SMART, extended drive self-test, read benchmark, full read scan, final SMART | None |
| Write verification | Extended self-test, read benchmark, full-drive write and checksum readback, SMART before/after | **Entire selected drive overwritten** |
| Quick erase (manual) | Recreates the selected existing volume as exFAT without scanning every sector; preserves the partition table and other volumes | **Selected volume's filesystem and files lost; old contents may remain recoverable** |
| Initialize/reset disk (manual) | Replaces the entire partition layout with one empty exFAT volume | **All partitions and files lost; old contents may remain recoverable** |
| Firmware secure erase (manual) | Explicit ATA firmware Secure Erase when the drive and adapter support it; may take many hours and cannot safely be cancelled after it starts | **Entire selected drive erased** |
| Full erase (manual) | Full accessible drive overwrite and SHA-256 checksum readback | **Entire selected drive overwritten** |

Choose a drive on the dashboard and start Quick or Extended. Enable automatic
intake in Settings to queue **Quick, read-only** checks on eligible drives.
The automatic eject delay defaults to 180 seconds; set it to zero for immediate
eject. Failed or incomplete tests skip the action window. Manually requested
Extended tests eject on completion when automatic eject is enabled.
Each drive is attempted once per observed connection. On service restart,
already-recorded drives aren't automatically retested; run a manual test or
unplug/reconnect to retry. Unplugging and reconnecting between discovery polls
may not be observed; use a manual test in that case.

Extended opens an estimate dialog before starting from the dashboard, including
retests and the Quick action window. Quick completion messages include the
estimated Extended duration before its Telegram button can start the test.
The total includes the drive's recommended extended self-test time, the fixed
30-second benchmark, and a full read scan estimate. The scan estimate uses the
latest successful read sample for that drive with a 25% scheduling allowance;
without a sample it explicitly assumes 100 MB/s until the benchmark finishes.
These are approximate scheduling estimates, not health evidence or deadlines.
Unknown SMART duration stays unknown rather than being invented.

The dashboard separates overall stage completion from current task progress.
It shows the current task, firmware-reported percentage (or unavailable), last
poll, elapsed task time, hours/minutes remaining, and the local finish-time ETA.
Timers move between polls while firmware progress can remain unchanged. An
overrun says the task is taking longer than estimated and withdraws its ETA;
a stalled/offline dashboard marks its timing data stale. During the full read
scan, the ETA updates using the observed scan rate. Firmware Secure Erase uses
the drive's advertised erase duration when available; its progress remains
indeterminate because it supplies no measured completion percentage. A missing
firmware duration is shown as unavailable, rather than guessed. Full erase starts
with a provisional write/read estimate and refines it from verified byte progress.
The ETA is withdrawn when an operation exceeds its estimate. It never advances task
progress merely because time passed.

Quick uses `smartctl -t short`, then waits for a fresh result from that specific
test before the benchmark. The short test is read-only and checks drive
mechanical/electrical/read behavior according to its firmware. Its advertised
runtime plus grace is bounded by a 30-minute safety limit. If the drive/bridge
cannot run or report it, Quick records incomplete coverage and continues the
read sample when safe; it does not substitute an old successful test result.

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

An old Linux RAID member can remain claimed by an inactive MD array. **Take
control + Quick** is available only when the array is inactive, all its members
belong to this selected drive, and there are no mounts, swap, or upper device
holders. Type `TAKE CONTROL <exact serial>` to stop that specific inactive array
and queue read-only Quick intake. It preserves RAID metadata and existing file
contents. Active/shared arrays and other holders remain blocked; DriveCheck
never automatically takes control or removes RAID metadata.

Quick erase follows Windows Quick Format semantics for a selected existing volume:
it recreates the filesystem metadata as exFAT without scanning every sector. The
partition table and other partitions are preserved. The dashboard requires a
specific volume choice when a disk has more than one; Telegram offers Quick erase
only when exactly one eligible volume exists and otherwise directs you to the
dashboard. A disk with no existing volume cannot be quick-formatted. Use the
separate **Initialize/reset disk** action to replace its entire partition layout
with one exFAT volume. Both operations are **not secure erasure**: old file contents
can remain recoverable. ATA firmware Secure Erase is a separate, explicit action
with its estimated duration shown before confirmation. Frozen,
locked, already security-enabled drives, ambiguous probes, unsupported adapters,
and failed firmware commands block that action without changing Quick erase. The
installer includes `hdparm`, `gdisk`, `exfatprogs`, `parted`, and `mdadm` along with
the testing tools. macOS remains read-only; erase and RAID takeover are currently
Linux-only.

Firmware erase uses a temporary password with a protected recovery record under
`<data directory>/erase-recovery/<drive identity>.json` (directory 0700, file 0600).
The password never goes into reports or messages. Once firmware erase is armed,
normal cancellation is unavailable. The dashboard shows a disabled **Cancel erase**
button with the reason; Telegram offers **Why can’t I cancel?**. Keep the drive
powered and connected. Software quick formats and full overwrites can be cancelled
from the dashboard or Telegram, but cancellation does not restore erased data.
Queued jobs can be cancelled before they start. A failure, timeout, disconnect, or interrupted
service leaves the record intact, marks the job incomplete, and blocks testing,
erasing, and ejection of that drive, including after restart. Do not power off;
inspect the recovery record and drive security state before deliberate recovery.
DriveCheck does not automatically unlock, retry, or downgrade an uncertain erase.
Do not delete that record merely to bypass the guard.

To deliberately enable manual erase and full-drive write verification, change
`DRIVECHECK_ALLOW_DESTRUCTIVE=true` in the station environment and restart the
service. The dashboard shows the detected erase method before requiring
`QUICK FORMAT <exact serial> <volume path>` or `FULL ERASE <exact serial>`; legacy write verification
still uses `ERASE <exact serial>`. A changed method requires a fresh confirmation.
Automatic intake always remains Quick and read-only, even with manual erase enabled.
Queued/running jobs are recorded as incomplete on restart, and erase jobs never
resume automatically. Cancellation is best effort for the drive's internal
self-test; review its status before disconnecting.

A full surface pass on a large HDD takes hours. The benchmark samples the start
of the disk and measures the whole Pi/USB/drive path; it is not a complete drive
performance characterization. The full read scan checks readability, while
write verification checks newly written test data. Unsupported SMART/self-tests
produce an **incomplete** report, not a pass. Existing SMART warnings remain
visible even when the selected I/O test succeeds.

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
