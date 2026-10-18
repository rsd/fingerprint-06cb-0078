# Step A6b: Omarchy's own fingerprint setup over our package (2026-10-16)

## What was run (by the owner, about four minutes)

1. `fprintd-delete "$USER" -f right-index-finger`. Without this, fprintd's duplicate check refuses to enroll the right index again. The left index stayed enrolled.
2. `omarchy-setup-security-fingerprint` in a normal terminal.
3. Lock with Super+Ctrl+L, then unlock with a finger.

## Results

| Check | Result |
|---|---|
| Install step | Skipped. `omarchy-pkg-missing libfprint-git fprintd usbutils` returns 1, because our package provides `libfprint-git`. |
| Package kept | `pacman -Q libfprint-sigfm-0078` still shows 1.94.100.r23.g40cda34-1. |
| Enroll (right index, `sudo fprintd-enroll`) | 18/18 stages, with 3 capture retries (image reply error, status 7). The script's "keep moving the finger around" hint likely made the finger slide during a capture. |
| Verify (`fprintd-verify`) | Match: scores 8, 0, then 14474 at stage 3, against threshold 105. |
| `fprintd-list` | right-index-finger, left-index-finger. |
| PAM changes by Omarchy's setup | It adds pam_fprintd to the sudo and polkit-1 stacks behind a lid-closed gate (`pam_exec.so quiet /usr/bin/omarchy-hw-laptop-closed`), and writes a separate `omarchy-lock-fingerprint` service for the lock screen (`auth required pam_fprintd.so`). |
| Lock screen | Fingerprint unlock works. |

This meets phase C's acceptance item "Omarchy Setup Fingerprint keeps our package" on the test laptop.

## Notes for the Omarchy proposal

- The `libfprint-git` provide is what keeps the package. Without it, the setup's `pacman -S --needed libfprint-git` would replace our library.
- Do not run Omarchy's "Remove Fingerprint" and then "Setup Fingerprint". The removal uninstalls fprintd, and the next setup would install `libfprint-git` over our package.
- Re-running the setup on an already enrolled right index fails with enroll-duplicate. Delete that finger first, as above.
