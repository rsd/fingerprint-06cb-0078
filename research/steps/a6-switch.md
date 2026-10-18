# Step A6: switch-over to the package, and next-day verify (2026-10-16)

## Switch-over (by the owner, 2026-10-15 late / 2026-10-16)

| Step | Result |
|---|---|
| `sudo pacman -U libfprint-sigfm-0078-1.94.100.r23.g40cda34-1` | The conflict prompt removed stock libfprint 1.94.100-1. Hooks: udev hwdb, udev reload. |
| `sudo tools/dev/a5-fprintd-dev.sh undo --keep-data` | The dev drop-in and lib were removed; data files, the 2 enrolled prints and 356 diag dumps were kept. |
| `sudo synaptics0078-setup status` | Ready: directory root 0700, module.bin and program-<serial>.bin valid. |
| `fprintd-list` | left-index-finger and right-index-finger, unchanged. |
| First a5-verify | Refused: the preflight required the dev install. Fixed (`log on/off/status` plus a package-mode preflight). |

## Next-day verify with the packaged lib (enrolled 2026-10-15 15:43; run 2026-10-16 09:44)

`sudo tools/dev/a5-fprintd-dev.sh log on`, then 10 rounds per finger. The preflight reported mode: package.

| Finger | Result | Best score per round, sorted (threshold 105) |
|---|---|---|
| right index | 10/10 match, 0 retries | 261, 2909, 6627, 91271, 323581, 414919, 522327, 578218, 1395875, 6139023 |
| left index | 10/10 match, 0 retries | 152, 182, 456, 3513, 12030, 45050, 2691259, 4081275, 5849086, 12326156 |

The FRR is 0/20 the next day, but the margin is much thinner than on enrollment day. On 2026-10-15 the lowest best score was about 33k; today three rounds scored under 500, the lowest at 152. That is consistent with the earlier offline cross-day FRR (4/10 with captures 2 days old). Skin condition and placement drift between days are the likely causes. Watch day-to-day use. Options if rejections show up:

- Lower the threshold. That needs B3's impostor tail first: the impostor max so far is 4 in 180 comparisons.
- Widen the enrollment coverage, for example by enrolling the same finger a second time under another name. pam_fprintd identifies against all enrolled prints.

## USB runtime autosuspend (the package hwdb lists 06cb:0078)

- After the switch the sensor showed power/control=auto with no reboot needed, and autosuspend_delay_ms=2000.
- After the runs: runtime_status=suspended, runtime_suspended_time=1561451 ms, runtime_active_time=129764 ms.
- Both verify runs opened with "Capture module: already loaded (RomInfo byte 17 = 0x0e)".

So runtime autosuspend does not wipe the loaded module. The A6 report's open question about autosuspend is answered. System suspend (S3) is still untested.
