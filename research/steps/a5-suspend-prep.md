# Step A5: suspend/resume preparation (2026-10-15)

These are the findings gathered while preparing `tools/dev/a5-suspend.sh`. The helper was not finished at this point. Nothing below has run on hardware yet.

## Findings (recorded, nothing changed)

- **Suspend needs no polkit password for the logged-in user** on a stock logind setup (`CanSuspend` returns "yes"), so `systemctl suspend` works without one. The test laptop uses deep sleep (S3), so the sensor probably loses power.
  - On the test laptop the sensor sits on a USB port with power/control=on, runtime_status=active and wakeup=disabled.
- **Lock screen.**
  - A Hyprland lock screen (hyprlock) that has `fingerprint:enabled = false` does not compete with a running fprintd-verify for the reader.
  - hypridle can lock before sleep (waiting up to 3 s for the lock), so the password prompt appears on wake.
- **The driver has no suspend support.** synaptics0078 has no suspend/resume vfuncs.
- **Expected result for "suspend during verify".**
  - When the system starts to sleep, fprintd calls fp_device_suspend.
  - Without a driver handler, libfprint cancels the running action with "Cannot run while suspended." (fpi-device.c:1773 and 1975).
  - fprintd then logs "Unexpected error while suspending device".
  - The client probably gets verify-unknown-error and exits before the sleep. This last step is a guess, not checked against fprintd's source.
  - After resume, the next verify reopens the device. The driver should then re-upload the module, because RomInfo byte 17 is 0 after a power loss.
- **Detecting resume from a script.** The journal need not contain "PM: suspend" or "System returned from sleep" lines (none was logged on the test laptop before the first sleep). So the helper detects resume mainly from the gap between the wall clock (which advances during sleep) and the monotonic clock (which does not). The journal lines are only a backup.
- **Journals are readable without root** for a user in the right journal group: both the kernel journal and the fprintd journal.

## Consequence to weigh after the hardware run

If verify is cancelled on suspend, a lock screen with fingerprint enabled must restart its verify after resume. An alternative is to give the driver suspend/resume handlers: cancel any armed capture on suspend, then on resume re-check RomInfo and re-upload if needed. Decide after the real run.
