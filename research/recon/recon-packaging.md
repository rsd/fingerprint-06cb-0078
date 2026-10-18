# recon-packaging: packaging and update-protection for a forked libfprint on Omarchy

Test system state at time of research (2026-10-14): pacman 7.1.0, omarchy 4.0.4-1, stock libfprint 1.94.100-1 (extra, installed as a dependency, owns /usr/lib/libfprint-2.so.2), fprintd 1.94.5-2 (extra), libfprint-git NOT installed. /etc/pacman.conf is Omarchy's shipped default (default/pacman/pacman-stable.conf) apart from one local IgnorePkg line: [core] [extra] [multilib] [omarchy] with `Server = https://pkgs.omarchy.org/stable/$arch`.

## 1. omarchy-pkg-missing / omarchy-pkg-present

Both are a loop over `pacman -Q "$pkg" &>/dev/null` (pkg-missing exits 0 if ANY name fails, pkg-present exits 0 only if ALL pass). No `-Qq` list matching, no `pacman -T`.

Key empirical finding: on pacman 7.1.0, `pacman -Q <name>` DOES honour provides. Tested on that system:
- `pacman -Q libfprint-2.so` and `pacman -Q libfprint-2.so=2-64` print `libfprint 1.94.100-1` (rc 0)
- `pacman -Q initramfs` prints `mkinitcpio 41.1-1` (rc 0)
- `pacman -Q libfprint-2.so.2` and a nonexistent name fail (rc 1)
- `omarchy-pkg-present libfprint-2.so` rc 0, `omarchy-pkg-missing initramfs` rc 1

So a package that provides `libfprint-git` makes `omarchy-pkg-missing libfprint-git ...` report it as present. (This is pacman >= 7.0 behaviour; the older "-Q ignores provides" assumption is wrong here.)

Contrast: `omarchy-pkg-drop` (used by omarchy-remove-security-fingerprint) matches EXACT names from `pacman -Qq`, so it ignores provides.

omarchy-pkg-add: runs `pacman -S --noconfirm --needed` only if pkg-missing, then re-verifies each name with `pacman -Q` (also provides-aware).

## 2. Omarchy's libfprint-git

Repo: `[omarchy]` -> https://pkgs.omarchy.org/stable/x86_64 (channels edge -> rc -> stable; "packages only move forward"). /etc/pacman.d has only mirrorlist (+ backups); no separate omarchy repo file. Source recipe: https://github.com/omacom/omarchy-pkgs , `pkgbuilds/libfprint-git/PKGBUILD`.

Published versions (db files downloaded and checked 2026-10-14; local working files, not published):
- stable x86_64: `1:1.94.100.r10.g6f9479c-1`
- rc x86_64: `1:1.94.100.r10.g6f9479c-1`
- edge x86_64: `1:1.94.100.r10.g6f9479c-3`
- local sync db (/var/lib/pacman/sync/omarchy.db, dated 2026-10-13): `-1`; `pacman -Si` shows Provides `libfprint libfprint-2.so=2-64`, Conflicts With `libfprint`, no Replaces.

PKGBUILD at master (commit a1ad25bb, 2026-10-08):
- pkgname=libfprint-git, pkgver=1.94.100.r10.g6f9479c, pkgrel=3, epoch=1
- source pinned: `git+https://gitlab.freedesktop.org/libfprint/libfprint.git#commit=6f9479c3d55f847c1b3769f28ceb99227f9858cf` (the commit adding Synaptics 06cb:010b), plus focaltech_moc-add-pid-0xA97A.patch (conditional); aarch64 also gets aurora-apple-sep.patch (Touch ID)
- `provides=("libfprint=$pkgver" libfprint-2.so)` (the versioned `libfprint=` arrived 2026-10-05; the stable/rc build still has the older unversioned `libfprint`), `conflicts=(libfprint)`, no `replaces`
- Build: `-D drivers=all -D installed-tests=false`, runs meson test in check()
- `.omarchy/package.json`: `"source": "local", "sync": false`; docs/upstream-sources.md lists libfprint-git under "Existing manual holds" (no automatic upstream watch). Bumps are by hand.

Bump history for pkgbuilds/libfprint-git (9 commits since May 2026):
- 2026-05-07 and 06-12: tracked libfprint master (FocalTech MOC support)
- 2026-07-26: pinned to d79f157; 07-27: added epoch=1 (a published untested master build sorted above the pin)
- 2026-09-06: pin bumped to 6f9479c for Synaptics 06cb:010b (pkgver 1.94.100.r10.g6f9479c, pkgrel 1)
- 2026-10-05: aarch64 Touch ID driver, pkgrel 2, versioned libfprint provide
- 2026-10-08: pkgrel 3 ("bump releases for packages with mismatched cached archives")
So pkgver changes about every 1.5-2 months, driven by a new reader needing support; pkgrel rebuild bumps are more frequent and arrive without any content change. The -3 build is on edge and will reach stable on a later promotion (promotion is manual via bin/omarchy-release / bin/repo advance, tied to release trains). Any locally-named `libfprint-git` with version <= `1:...-3` will be overwritten the moment -3 reaches stable.

The 06cb:0078 reader is not in Omarchy's package (its description only lists 010b and FocalTech).

## 3. Keeping our build in place

How Omarchy updates: `omarchy-update` -> omarchy-update-pkg-prune (`paccache -rk2`) -> snapshot -> update-dev -> update-keyring -> `omarchy-update-system-pkgs` = `sudo env OMARCHY_UPDATE_PACMAN=1 pacman -Syu --noconfirm --overwrite '/usr/share/omarchy/*'` -> omarchy-migrate -> hook post-update (user hooks in ~/.config/omarchy/hooks/post-update.d) -> omarchy-update-aur-pkgs (`pacman -Qem` then `yay -Sua --noconfirm`) -> mise -> omarchy-update-orphan-pkgs (interactive, default No) -> restart. If -Syu fails on a package conflict, omarchy-update-system-pkgs-when-conflicted re-runs it interactively and puts the pacman question to the user (it refuses under -y or without a TTY). A PreTransaction alpm hook (00-omarchy-update-guard.hook) blocks bare `pacman -Syu` unless `OMARCHY_UPDATE_PACMAN=1` or `OMARCHY_ALLOW_DIRECT_PACMAN=1`; it does not affect `pacman -U`.
Other commands that matter: `omarchy-reinstall-pkgs` runs `pacman -Suu` (downgrades to stable), and `omarchy-refresh-pacman [channel]` overwrites /etc/pacman.conf and mirrorlist with the shipped defaults (backing up to .bak) then runs `pacman -Syyuu --noconfirm`; it calls the `pre-refresh-pacman` user hook (if one exists) between the copy and the upgrade.

libalpm behaviour that decides this: `-Su` upgrades by installed NAME (plus `replaces`); it never upgrades by provides. A same-named sync package with a higher vercmp (epoch first, then pkgver, then pkgrel) is a plain silent upgrade, no conflict question.

### Scheme A: name it libfprint-git, local version
- Setup script / migration: pkg-missing sees libfprint-git installed, so nothing is installed. Good.
- `pacman -Syu`: replaced silently by Omarchy's as soon as theirs is newer. Omarchy's epoch is 1 and pkgrel changes routinely (already -3 on edge), so a local `1:...-1.1`-style version loses. You would need epoch >= 2 and a standing "local is newer than omarchy" warning on every update.
- `omarchy-reinstall-pkgs` (`-Suu`) and `omarchy-refresh-pacman` (`-Syyuu`) downgrade it regardless of epoch.
- Needs IgnorePkg to be robust, and IgnorePkg lives in /etc/pacman.conf, which omarchy-refresh-pacman overwrites (re-add through a pre-refresh-pacman hook).
- Upside: omarchy-remove-security-fingerprint's `omarchy-pkg-drop fprintd libfprint libfprint-git` removes it cleanly; yay does not treat it as foreign.

### Scheme B: libfprint-sigfm-0078, provides libfprint / libfprint-git / libfprint-2.so=2-64, conflicts libfprint / libfprint-git
- `pacman -Syu`: Omarchy's libfprint-git is not installed, so it is never a candidate. No `replaces` anywhere. Immune to Omarchy epoch/pkgrel changes, and to -Suu/-Syyuu. No pacman.conf edit needed.
- Setup script / migration 1785090473 / omarchy-pkg-add: `pacman -Q libfprint-git` is satisfied through provides (verified mechanism above), so pkg-missing is false when fprintd and usbutils are present and nothing is installed.
- Name must be unique on the AUR: checked, `libfprint-sigfm`, `libfprint-sigfm-0078` and `libfprint-synaptics-0078` do not exist on AUR (AUR does have a `libfprint-git` 1.94.7.r29). The package will be "foreign" (`pacman -Qem`), so `yay -Sua` looks it up on AUR by name, finds nothing, and leaves it alone. A same-named AUR package appearing later would be the only risk.
- Provides must be versioned to satisfy versioned dependants (Omarchy learned this on 2026-10-05: unversioned provide does not satisfy `libfprint>=X`). Use `provides=("libfprint=1.94.100" "libfprint-git=1.94.100" "libfprint-2.so=2-64")` (use a real version >= what fprintd/others might require; makepkg also auto-adds the soname provide, check with `pacman -Qip`). fprintd 1.94.5-2 currently needs plain `libfprint` and `libfprint-2.so=2-64`.
- Installing it over stock: `pacman -U pkg` asks "remove libfprint?" (answer y); the provides satisfy fprintd in the same transaction.
- Residual hazard (both schemes): the one line that can still clobber us is `omarchy-setup-security-fingerprint`'s `pacman -S --needed --noconfirm --ask 4 libfprint-git fprintd usbutils`. It only runs when pkg-missing is true. `--ask 4` auto-accepts the conflict removal, so if it does run it replaces our package with Omarchy's without a prompt. It runs when fprintd or usbutils is missing, i.e. after omarchy-remove-security-fingerprint (which removes fprintd, and in scheme A also our package, via exact-name drop; in scheme B it leaves ours installed) followed by a new setup. Also: running the setup BEFORE our package is installed would swap stock for Omarchy's libfprint-git (no 0078 support). So: install ours first, keep usbutils and fprintd installed, and do not run the Remove then Setup cycle without reinstalling ours afterwards.
- omarchy-remove-security-fingerprint under B: removes fprintd and PAM bits; leaves libfprint-sigfm-0078 installed (exact-name match fails). Harmless.
- Orphans: install ours explicitly (or leave as dependency of fprintd); omarchy-update-orphan-pkgs is interactive default-No, so nothing is removed silently.
- paccache keeps the last two versions of every package in /var/cache/pacman/pkg; keep your own copy of the built .pkg.tar.zst.

### Scheme C: IgnorePkg alone
IgnorePkg=libfprint-git (A) blocks the -Syu upgrade with a warning, but: (1) /etc/pacman.conf is overwritten by omarchy-refresh-pacman (any existing IgnorePkg line would be lost too; restoring it needs a pre-refresh-pacman hook); (2) it does nothing for an explicit `pacman -S libfprint-git`; (3) I did not verify that -Suu honours it. For B it is unnecessary because no sync package carries our name.

### Recommendation
Scheme B. Name `libfprint-sigfm-0078` (or similar unique name), versioned provides `libfprint=<ver>`, `libfprint-git=<ver>`, `libfprint-2.so=2-64`, `conflicts=(libfprint libfprint-git)`, no `replaces`, no IgnorePkg, no pacman.conf edits. Optionally add a user hook ~/.config/omarchy/hooks/post-update.d/NN-libfprint-sigfm that checks `pacman -Q libfprint-sigfm-0078` and notifies if it has been displaced (it runs after every `omarchy update`), and keep the .pkg.tar.zst in a stable path for one-command reinstall. Use plain pkgver 1.94.100.rN.gSHA, optionally with a local suffix; no epoch needed.

## 4. ABI: does fprintd need a rebuild?

No, replacing libfprint-2.so is enough as long as the fork keeps the public ABI.
- fprintd (/usr/lib/fprintd) has NEEDED `libfprint-2.so.2`; pam_fprintd.so does not link libfprint at all. Installed libfprint SONAME is `libfprint-2.so.2` (file .2.0.0).
- fprintd's 47 undefined fp_*/fpi_* dynamic symbols were all found in the installed libfprint's exported symbols (comm check empty); the symbol lists are local working files, not published.
- fprintd 1.94.5-2 (built 2026-03-27) declares `Depends: libfprint libfprint-2.so=2-64`. Omarchy already runs stock fprintd 1.94.5 against its libfprint-git (different upstream snapshot, same soname), which is in-the-wild proof the combination works.
- Constraints for the fork: keep SONAME libfprint-2.so.2 (do not touch soversion in meson), do not remove or change signatures of exported fp_* symbols and GObject types (SIGFM and a new driver are internal additions; check with `nm -D --defined-only` diff or `abidiff` against stock), keep the package providing `libfprint-2.so=2-64` (makepkg auto-adds it when the .so is packaged; confirm with `pacman -Qip`) and plain `libfprint`. If SIGFM pulls in a new runtime library (e.g. a C++ stdlib or an image library), declare it in `depends`; it does not affect fprintd.
- The package must also ship the udev rules/hwdb that libfprint installs (`/usr/lib/udev/rules.d/70-libfprint-2.rules`, `/usr/lib/udev/hwdb.d/60-autosuspend-libfprint-2.hwdb`), regenerated for the new 06cb:0078 driver so fprintd/udev (uaccess/autosuspend) pick up the device; systemd's hwdb pacman hook rebuilds hwdb on install.
- A future Arch soname bump (libfprint-2.so=2-65) would make a new fprintd refuse our package; pacman would propose stock libfprint, hit our `conflicts`, and Omarchy's conflict handler would stop and ask interactively: a loud failure, not a silent swap.

## 5. Other Omarchy fingerprint touchpoints on update

Grep of ~/.local/share/omarchy found fingerprint/fprint references in only these places outside the setup/remove scripts: omarchy-menu.jsonc (Setup shows when `omarchy-hw-fingerprint`; Remove shows when `omarchy-pkg-present fprintd`), first-run hook, shell lock/polkit plugins (read PAM state and `fprintd-list`), and three migrations. libfprint/fprintd are NOT in install/omarchy-base.packages (so omarchy-reinstall-pkgs does not install them).

Migrations (state markers in ~/.local/state/omarchy/migrations; a migration marked done there does not rerun):
- 1784818437.sh: "Gate sudo and polkit fingerprint auth behind the lid state". For /etc/pam.d/sudo and polkit-1 that contain pam_fprintd.so but not the omarchy-hw-laptop-closed gate, inserts a `pam_exec ... /usr/bin/omarchy-hw-laptop-closed` skip line before pam_fprintd. PAM-only; independent of libfprint packaging.
- 1785090473.sh: "Repair fingerprint support left without a libfprint". If fprintd is present and BOTH libfprint and libfprint-git are missing (via provides-aware `pacman -Q`), runs `omarchy-pkg-add libfprint-git`. With either of our schemes (installed package satisfies `libfprint` through provides) this is a no-op.
- 1788124236.sh: sshd hardening. Matched the grep only because of the word "fingerprint" (ssh-keygen -lf). Unrelated.
Other: `install/user/first-run/setup-fingerprint.hook` only sends an invite notification when a reader is detected and /etc/pam.d/omarchy-lock-fingerprint is absent. `omarchy-hw-fingerprint` detects readers from sysfs (vendor 06cb is in its list; a vendor-match is rejected if a kernel driver is bound). omarchy-setup-security-fingerprint writes PAM only after a successful enroll and verify (sudo, polkit-1, omarchy-lock-fingerprint).

Notably no migration or update step reinstalls or replaces libfprint in a normal `omarchy update`; the only update-time risk is `-Syu` seeing a same-named newer libfprint-git (scheme A) and the explicit-install paths listed under the residual hazard.

## Caveats / not verified
- Whether `pacman -Suu` honours IgnorePkg for downgrades was not tested (nothing to simulate with libfprint-git uninstalled).
- The -3 edge build's versioned provides are from the PKGBUILD diff; the db entries I read for stable/rc/edge were `-1`, `-1`, `-3` respectively, edge desc not individually inspected for provides.
- SIGFM's own dependency footprint was not checked here.
- No packages were installed or removed; the downloaded db files and symbol lists are local working files, not published.
