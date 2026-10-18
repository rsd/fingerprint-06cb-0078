# Linux support for Synaptics 06cb:0078 (Clevo P870KM): web research findings

Researched 2026-10-13. Web only; no hardware touched. Where I checked something myself it says "verified"; everything else is a third party's claim.

## Bottom line

- No mainstream open-source project supports 06cb:0078 today. Not libfprint (stable or master), not python-validity (upstream or forks), not fprintd, not the libfprint merge request (MR) !626 that is adding Validity drivers.
- Two out-of-tree efforts reached "captures a fingerprint image". Neither reached working enroll/verify through fprintd that anyone has confirmed:
  - iusearch/Synaptics78 (2019), capture demo only.
  - ToLiKNoSoV/synaptics-06cb-0078-driver (March 2026), a libfprint-style driver plus a separate "backend" repo. The author says capture, minutiae and templates work but fprintd does not see the prints.
- The closest relative is the sibling ID 06cb:00a8, which is also unsupported. It shares the same Windows INF as 0078 and has an active, detailed reverse-engineering thread in September 2026.
- No page names the P870KM or P870 with this ID in a Linux support context. The only Clevo-chassis mentions are P751DM2-G (fprint list, 2018) and linux-hardware.org probes of P7xxDM2, P7xxTM1 and HASEE P870TM_TM1 boards.

## 1. libfprint (checked directly, verified)

- Supported-devices page, https://fprint.freedesktop.org/supported-devices.html. It lists 34 Synaptics IDs, all under the `synaptics` driver: 06cb:00bd, 00c2, 00c4, 00c6, 00df, 00e9, 00f0, 00f9, 00fc, 0100, 0103, 0104, 0106-010b, 010d, 010e, 0123, 0124, 0126, 0129, 015f, 0168, 0169, 016c, 0173, 0174, 019d, 019f, 01a0, 01a4. 06cb:0078 is absent, and no 0078 appears under any vendor.
- libfprint master, `libfprint/drivers/synaptics/synaptics.c`. The id_table has no 0x0078. `data/autosuspend.hwdb` has no 0078 either.
- Issue/MR search through the GitLab API (title and description only; comments are not searched): zero hits in issues and MRs for "0078", "06cb:0078", "06cb 0078", "USB 073", "Clevo" and "P870". The only "WBDI" hit is issue #266, which is about 06cb:009a. A sanity query for "00bd" returned results, so the API was working.
- Unsupported-devices wiki, https://gitlab.freedesktop.org/libfprint/wiki/-/wikis/Unsupported-Devices.md. It has no 06cb:0078 entry. It does list `06cb:00a8 | Clevo Laptops` (the sibling), and also 06cb:0081, 0088, 008a, 009a, 009b, 00a1, 00a2, 00b7, 00bb, 00bc, 00be and others. There is no `Devices/06cb:0078` wiki page (HTTP 404).
- Related open MRs, none covering 0078 (verified via the API):
  - https://gitlab.freedesktop.org/libfprint/libfprint/-/merge_requests/626 "validity: 0x969/0xd51 sensor support..." by ggiesen. It is open and unmerged, with last update 2026-09-28. IDs: 06cb:009a, 00b7, 00cb, 00d2 and 138a:0090, 0097, 009d, 00ab.
  - !579 (lewohart, closed draft) and !619 (14mRh4X0r, open draft).
  - Coverage: https://ubuntuhandbook.org/index.php/2026/08/validity-fingerprint-sensors-libfprint/

## 2. python-validity (https://github.com/uunicorn/python-validity)

- Upstream supports only 138a:0090, 138a:0097, 138a:009d and 06cb:009a (`validitysensor/usb.py`, verified). The README has no device list and no mention of 0078 or Clevo. The last push was 2025-06-10, and 207 open issues are shown. Not archived.
- Issue search: 0078 appears only in https://github.com/uunicorn/python-validity/issues/179 ("Support for 06cb:00a8", open). Issue #40 ("Support for 06cb:00c7") also mentions Clevo but not 0078.
- Forks: I fetched `validitysensor/usb.py` from the default branch of all 143 forks. 106 returned a file and 37 returned 404 (different layout). None lists 0078. Only Killersparrow1 (adds 00b7) and soosviktor (adds 0081) differ from upstream. This is a usb.py-only check, not a full-tree check.

### Issue #179, the key technical source (about the sibling 00a8, with direct statements about 0078)

Comments are by user "byPARSE", 2026-09-08 and 09, on a Clevo N151ZU. They are unverified third-party claims about 00a8, except where I say verified.

- Comment 3 says the Windows INF `synaWudfBioUsbClevoProd.inf` lists both `USB\VID_06CB&PID_0078` and `USB\VID_06CB&PID_00A8`. The device name is "Synaptics WBDI Fingerprint Reader - USB 073" and `CustomerProject = "Clevo-073"`. VERIFIED by me: I downloaded the Wortmann package and read the INF. It has `DriverVer=03/08/2018,5.5.2707.1073` (exactly the driver version on this laptop), both PIDs on adjacent lines, "USB 073" and "Clevo-073".
- The same comment links the January 2018 fprint list thread about 0078 on a Clevo P751DM2-G (section 4).
- Findings on 00a8, likely to be relevant to 0078:
  - Sensor type 0x2449, ROM 6/0x14, no external SPI flash (so python-validity's flash model does not fit).
  - It speaks the Validity protocol, not libfprint's BMKT, so adding the ID to the `synaptics` driver would not help.
  - A TLS session can be established without flash by using command 0x4f then 0x50. The Global Wrapping Key is identical to python-validity's.
  - Live image data streams on EP 0x82 (152-byte records, 96 active pixels), and the data responds to a finger.
  - Where it stops: the finger-detect interrupt (type 2) never arrives. The image-reconstruction chunk 0x2e is not decoded and no ridge pattern has been reconstructed. The author posted this on 2026-09-09 and offered to share captures and scripts. The issue is open.

## 3. Projects that mention 0078 directly

| Project | What it is | How far it got | Why it stopped |
|---|---|---|---|
| fprint mailing list thread, Jan 2018: https://lists.freedesktop.org/archives/fprint/2018-January/000918.html (also /000917.html, /000919.html) | Dennis Mungai asks about 06cb:0078 on a Clevo P751DM2-G (Ubuntu 16.04). The same hardware also appears on the Lenovo Yoga 720. | libfprint's Vasily Khoruzhick replied "From what I can tell, it's not supported." | The thread ends there. The follow-up "Is there any project working on support?" got no answer in the archive. |
| nmikhailov/Validity90 issue #63 "Clevo 06cb:0078": https://github.com/nmikhailov/Validity90/issues/63 | Opened 2019-03-16 by cydia2020. He ran Validity90's `./prototype`, which is written for 0090/0097-class sensors. | The device answered the plaintext commands (0x01 RomInfo, 0x19), but the prototype's init steps failed ("Sensor not initialized, init byte is 0x0 (expected 0x02)", "Incomplete reverse engineering"). Comments from iusearch on 2019-08-31 point to Synaptics78. | Still open. The Validity90 repo was last pushed 2020-01-16, with 76 open issues, and no maintainer replied. |
| hmaarrfk/Validity91 issue #11: https://github.com/hmaarrfk/Validity91/issues/11 | Nov 2018. iusearch asks the Validity91 author about the image packet format on his "06cb 0078 sensor". | Within the day he obtained a clear fingerprint image, 56 (height) x 144 (width), sent as a single packet. | The hmaarrfk reply says libfprint lacks matching algorithms for images this small. The issue was closed. |
| iusearch/Synaptics78: https://github.com/iusearch/Synaptics78 (originally aliyuchang33/Synaptics78) | "Reverse engineering project focus on Synaptics 06cb:0078 usb fingerprint sensor." Python (`main.py`) and C++/libusb/OpenCV (`synaptics78.cpp`). 3 commits, 2019-08-31 to 2019-09-03, 5 stars. | A capture-only demo. It replays recorded init commands (01, 19, 0860.., 0780.., 75, then a 06-prefixed blob), waits for a finger and renders a 144x56 image. | The author says it is "just a prototype" and "not a driver project". His issue-tracker reply (https://github.com/iusearch/Synaptics78/issues/2, 2020-02-25): "libfprint is not supporting small sensor verification, this project will not move on to be integrated into libfprint". Issues #1 to #3 (2019 to 2023) ask how to use it; none lead anywhere. |
| ToLiKNoSoV/synaptics-06cb-0078-driver: https://github.com/ToLiKNoSoV/synaptics-06cb-0078-driver | A libfprint-style C driver ("Anatoliy Nosov", LGPL header) with an id_table of exactly 06cb:0078. 144x56 image-device with NBIS minutiae, plus `synaptics_0078_data.c` (303 KB of replayed byte blobs: 10501 B, 18869 B and 18869 B). Created 2026-03-01, last push 2026-03-07, 0 stars, Russian README. | README (translated): driver works, captures an image, finds minutiae and creates templates. It also says fprintd does not see the prints, and blames fprintd (needs libfprint >= 1.94.9; "fprintd looks for prints not where it saves them"). Built against libfprint 1.94.10. | Last push 2026-03-07; no issues, no upstream MR found. fprintd integration is not handled inside the driver; the author's next step was a separate backend (next row). |
| ToLiKNoSoV/synaptics-0078-backend: https://github.com/ToLiKNoSoV/synaptics-0078-backend | Created 2026-03-13, last push 2026-03-21. A "complete driver and backend": a `synaptics-capture` tool, `nbis-helper`, a backend daemon plus PAM module, a systemd unit, a `net.reactivated.Fprint.conf` D-Bus policy, and PAM configuration files (sddm, gdm, sudo, polkit, kscreenlocker, login). README claims enrollment via KDE System Settings or the CLI. | The author's claim only; I did not test it. | No further activity after 2026-03-21. |

A read-only code review of both 2026 repositories is in `research/review-toliknosov.md`.

## 4. Other mentions (no project, only evidence the hardware exists)

- linux-hardware.org, https://linux-hardware.org/index.php?id=usb:06cb-0078. "Synaptics WBDI Device". Probes include Clevo P7xxDM2-(G), "Notebook" P7xxTM1, HASEE P870TM_TM1, System76 Oryx Pro and Bonobo WS, EUROCOM Sky X4C and Q6, TUXEDO P7xxTM1, and P95xER, P95_HR and PA70Hx. P870KM is not listed. It reports no driver in the kernel (LKDDb up to 7.1) and quotes a Ubuntu 21.04 user who calls Synaptics78 "the closest workaround... too bad it won't get into libfprint".
- Incidental lsusb mentions in unrelated issues: pop-os/pop#368 (a System76 machine) and NixOS/nixpkgs#71070 (lsusb listing). No support discussion.
- Windows side: Microsoft Update Catalog and driver-aggregator sites list the "USB 073" driver for VID_06CB&PID_0078, version 5.5.2706.1073 (Dec 2017) and 5.5.2707.1073 (Mar 2018).
- Arch wiki Fprint page (https://wiki.archlinux.org/title/Fprint): no mention of 0078 or Clevo.
- Not found: any Ask Ubuntu, Reddit, Arch forum or other thread for 06cb:0078 or for a fingerprint reader on a Clevo P870 or P870KM. The search tool cannot query reddit.com or askubuntu.com, and open-web searches returned nothing relevant. Absence there is not proof.

## 5. Closest relative

1. **06cb:00a8 (Clevo N150U/N151ZU, System76 Darter Pro, TUXEDO InfinityBook v5, Wortmann TERRA MOBILE 1550). It is unsupported but heavily analysed.**
   - Same Windows INF and same driver version (verified above). Same device string "USB 073" and "Clevo-073".
   - Raw RomInfo (command 0x01) replies, compared byte by byte: the 0078 trace in Validity90 #63 and the 00a8 trace in python-validity #179 are identical in the first 16 bytes: `00 00 10 b6 04 54 9a 00 00 00 06 14 01 33 00 01`. That is ROM 6/0x14 and product code 0x33 on both. Bytes 18 to 25 and the last byte differ.
   - Inference, not established: on 00a8 the author points to the RomInfo byte at reply offset 0x19 (0x21) as the flag that decides whether the flash-related parts of command 0x4f are sent. On 0078 that byte is 0x00, so 0078 may differ from 00a8 and may expose flash. That would change the plan for it.
   - Another suggestive coincidence, unverified: 00a8 accepted a 10500-byte module, and the 2026 0078 driver's first replayed blob is 10501 bytes, which would be a command byte plus a 10500-byte module.
   - Image geometry: 0078 yields 144x56 (iusearch, Validity91 #11). The 00a8 author found 152-byte records with 96 active pixels, so the two sensors are not identical.
   - The libfprint wiki lists 00a8 as "Clevo Laptops", unsupported.
2. **Nearest supported-protocol family: Validity/Synaptics VCSFW, 138a:0090/0097/009d and 06cb:009a.** It is supported by python-validity, and MR !626 proposes it for libfprint.
   - 0078 answers the same command opcodes: 0x01, 0x19, 0x75 and 06-prefixed blob uploads. The iusearch demo and the 2026 driver replay the same pattern as python-validity's send_init.
   - But the firmware generation differs: ROM 6/0x14 on 0078 and 00a8, against 6/0x00 on the supported sensor types 0x199 and 0xdb, per #179. The 009a blobs were rejected with 0x04be on 00a8.
3. **Not the libfprint `synaptics` (BMKT) driver family.** Only 00a8 was tested, where a BMKT-framed GET_VERSION got a Validity error reply. Applying that to 0078 is an inference from the shared ROM header.

## Search coverage and limits

- GitLab web pages are behind Anubis, so WebFetch failed on them. A plain curl against the GitLab API and raw files worked, and that is how the libfprint results above were verified. API search covers titles and descriptions only, not comment bodies.
- Searched without hits for 0078: python-validity README and forks, synaTudor issues, libfprint wiki, Arch wiki Fprint, linux-fingerprint-drivers hub README, the libfprint synaptics driver source.
- Files I downloaded for verification (Wortmann driver zip, INF only extracted) are local working files, not published. Nothing was executed.
- Raw data used (local working files, not published): python-validity #179 comments, Synaptics78 and ToLiKNoSoV sources, MR 626 JSON, wiki dump.
