# Hardware validation checklist

Keep the first test on a spare drive whose contents aren't needed. Record the
Pi model, OS version, power supply, USB dock model, drive model/serial and tool
versions beside exported reports. Validate each new station, adapter, or drive
combination; simulated tests do not establish physical compatibility.

1. Install the service and sign in through SSH forwarding. Confirm **Hardware
   mode**, tools present, boot disk blocked, USB target serial/capacity correct.
2. With a partition mounted on the USB drive, check that testing is blocked.
   Unmount it and rescan. Ensure desktop automount is disabled before real I/O.
3. Run **Quick**. Confirm SMART data is from the target drive, the newly requested
   short self-test completes, and fio measures a read sample. Download the report.
   Confirm fio can open the device while DriveCheck holds its exclusive Linux
   block-device claim. Quick does not scan the entire surface. Validate that
   file/partition contents remain intact.
4. Run **Extended**. Confirm the new extended self-test reaches completion and
   full read scan covers the entire accessible capacity including final sectors.
   Check the duration estimate before starting, and the task progress, remaining
   time, and ETA during testing. Capture elapsed time and dashboard updates
   from a second browser. Refresh/reconnect during the job; it should continue.
5. Cancel an extended test. Verify the app reports cancelled and the hardware
   process/self-test stops before disconnecting. Then run another Quick test.
6. Enable automatic intake. Plug in a second eligible drive. Confirm one
   read-only Quick job queues; only one drive runs at once. Rescan repeatedly
   and check that no duplicate job appears. Restore your desired automation
   settings after checking.
7. Configure your chosen notification provider. Press the test-message button;
   restart the service and confirm the station-ready message arrives before docking
   a drive, then run a test and confirm the completion message reaches the intended destination.
   Temporarily disconnect the Pi network and confirm result persistence/retry.
8. Restart the service during a read-only job. Confirm it becomes incomplete,
   preserves results already saved, and doesn't resume or claim a pass.
9. If testing write verification, use a fully disposable drive. Arm writes in
   configuration, confirm its exact serial, and verify the entire write/read
   pass finishes. A wrong serial must block it. Disable writes afterward.
10. Test another dock/drive combination if available. Unsupported SMART should
    be visible and prevent a full pass. Save the resulting report.

Avoid disconnecting a drive as a routine test while it is writing. Read-only
removal behavior can be tested on disposable hardware if desired. A passed
intake test does not replace backups or ongoing monitoring.

Report bugs with the run ID, exported JSON, service log excerpt, OS/tool versions,
and adapter model. Exclude tokens, notification settings, and private account data.

## Headless and macOS acceptance

- On macOS, launch inventory mode as your normal user. Confirm the actual external
  drive identity, capacity and mounted state; missing tools/root must be explained.
  Relaunch as root with fio/smartctl installed. Use a spare drive, unmount normally,
  run Quick and then Extended, and verify original files after remounting.
  Unsupported USB SMART must leave coverage incomplete. On disposable storage,
  confirm Quick format preserves the partition layout, initialization creates one
  exFAT volume, and Full erase writes and verifies the whole disk. Mounted/system
  drives must remain blocked, and manual actions must require confirmation.
- On the Pi, use a single-bay dock and configure an enabled provider, then enable
  headless mode. Close the dashboard. Dock an unmounted spare drive and confirm
  one read-only Quick test and a result message. For a passed/warning drive,
  confirm the 180-second action window offers Extended and Eject. Let it expire
  and verify safe power-off and a separate **Safe to remove** message. Verify the
  report lifecycle matches actual hardware state. Repeat with **Eject now** and
  **Run Extended**; a failed or incomplete test should skip the action window.
- Repeat with notification delivery temporarily unavailable: the report is saved,
  delivery stays queued and eject proceeds after the bounded wait. Restore network
  and confirm eventual notices without false readiness.
- Use a busy mounted volume and a multi-bay dock: automatic testing or unsafe
  shared power-off must be refused. Confirm a cancelled test is not auto-ejected.
- Restart during finishing: it must report interrupted release, never issue an
  unverified readiness message or eject a replacement device.
