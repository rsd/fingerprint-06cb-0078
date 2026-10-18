# Step A6c: fingerprint login at SDDM (2026-10-16)

- Enabled with `sudo tools/sddm-fingerprint.sh on`. The script puts the pam_fprintd block before `auth include system-login` in `/etc/pam.d/sddm` and keeps a backup at `/etc/pam.d/sddm.before-fingerprint`.
- **First try, after a reboot:** no fingerprint login. The reader was touched without pressing Enter. SDDM only runs PAM once the greeter sends Login, which happens on Enter. The journal showed one Login message, a typed-password login on the unchanged password path, and no pam_fprintd call.
- **The reboot was not the cause.** The module is gone after a power cycle, and the first open re-uploads it ("Capture module: not loaded, uploading", then "uploaded and confirmed") in under a second, well inside pam_fprintd's `timeout=10`.
- **Second try, after a logout:** empty password field, Enter, touch. The fingerprint login worked. Enter-then-touch is counter-intuitive but accepted. A touch-only login would need a modified greeter theme, and Omarchy updates overwrite its theme, so it is not pursued.
