# B1: public sources for the 10500-byte capture module

Researched 2026-10-16. Web only; the sensor was not touched. Nothing downloaded was executed; archives were opened with 7z and cabextract and read with `python3 -I`.

Our module: `driverstore/synaWudfBioUsb.dll` (sha256 `19cada14...a707`, 2,617,864 B, file date 2019-03-31), bytes 0x19cca0..+10500.
Module sha256: `67f4a332d89f76fe12c57fa7f67b43a076f689e78267b888c694e26b05caacc1` (the pin in `synaptics0078-setup`).

## Verdict

- The module is the same for every unit. It is a static blob inside the vendor DLL. It was found byte-exact (exactly one occurrence) in all four packages tested, covering builds from 2017, 2018, 2019 and 2020. Only its offset moves.
- The first public, no-login download is the Microsoft Update Catalog. A reseller mirror of Clevo's own 2017 package is the second.
- Clevo's own downloads are behind a partners-only portal ("Service Center / CLEVO partners only", clevo.com.tw), so there is no public Clevo URL.

## Candidates

| # | Source | URL | Package version / date | Package sha256 | Path inside | Module match | Module sha256 |
|---|---|---|---|---|---|---|---|
| 1 | Microsoft Update Catalog, "Synaptics Incorporated - Biometric - 5.5.2707.1073" (many entries; same INF `synaWudfBioUsbClevoProd.inf`, lists PID_0078 and PID_00A8) | https://catalog.s.download.windowsupdate.com/d/msdownload/update/driver/drvs/2019/07/c337faa1-00ec-4849-a62d-439fa9ef915b_ed7982efc8a40ca11c27348041c9830e26ecd880.cab (catalog update id 1169d5b8-2217-4497-abad-bf0d33426e66) | 5.5.2707.1073, DriverVer 03/08/2018; DLL build 2019-05-30; cab 3,365,928 B | `169cf21c0b20276abbc510b0b9e3491ebe3cc215887024e31f29b45645e91960` | `synaWudfBioUsb.dll` (2,617,792 B, sha256 `ffece17e...382a`), offset 0x19cca0 | yes, byte-exact, same offset as ours | `67f4a332...acc1` |
| 2 | Microsoft Update Catalog, same title, id 4dcef3e2-f7be-4b0e-83f9-0a6e9d37e49a | https://catalog.s.download.windowsupdate.com/c/msdownload/update/driver/drvs/2020/11/198986b3-7e10-4476-bf0c-0446e71ba7c7_f76a9a492da9b1cadf627700be4c297eea42058b.cab | 5.5.2707.1073; DLL build 2020-08-24; cab 3,364,070 B | `2874fc25ccb1972e8d730ca92ec8227eabb6bec566d453234d29f79e728fdfd1` | `synaWudfBioUsb.dll` (2,626,400 B, sha256 `c04f1ff5...7511`), offset 0x19cca0 | yes, byte-exact | `67f4a332...acc1` |
| 3 | Origin PC support, EON17-SLX P870KM1-G (a Clevo P870KM1 reseller), file hosted by Corsair. Page: https://support.originpc.com/hc/en-us/articles/23432009751821-EON17-SLX-P870KM1-Drivers | https://www3.corsair.com/Files/Origin_PC/Drivers/Laptops/Legacy/EON17-SLX/P870KM1-G%20%28KABYLAKE%29/Options/02_Finger.zip | `Syna_WBFFPReader_Setup.exe` 5.5.2702.1073, DriverVer 05/26/2017; zip 7,901,691 B, last-modified 2026-08-12 | zip `87d67e500ac3e25956726e66875b24b5a7c29a07e85af4548757a67bd4faddc2`; exe `410945cb87632be31b7671cb4c993e47ef8f011866e2cde7dbba71ff03fec12d` | zip > exe > two embedded MSI resources (BIN/412, BIN/416), each holds `synaWudfBioUsb.dll` (2,583,096 B, sha256 `c00c1cb7...ed33`), offset 0x19e4c0. 7z opens all three layers without running anything | yes, byte-exact, different offset | `67f4a332...acc1` |

Notes:
- Candidates 1 and 2 are the same package family. The catalog lists 25 entries for PID_0078; they come in two cab sizes (3,364,070 and 3,365,928 B), which are the two cabs above. Others were not downloaded.
- Candidate 3 is a 2017 build, so the module is the same across 2017 to 2020 builds.
- Whether the Origin file is byte-identical to Clevo's original could not be checked, because Clevo's portal is not public. Origin's driver set carries the Clevo layout (`01_Chipset ... 02_Finger`), which matches Clevo's driver-disc order, and the INF is Clevo-branded.
- Eurocom's P870KM1 page has no fingerprint package. Sager's drivers page lists nothing public for the NP9876. XMG blocked the fetch (HTTP 403). None of these were tested further.

## What else the packages cover

- Present: the DLLs themselves, so the tables used to generate the fixed chunks of the `02` program. In our payload, 11 of the 19 chunks occur verbatim in the DLL (registers, the 868-byte register list, Data Encoder, Timeslot Table Offsets, etc.). The rest (finger detect, image reconstruction, the 8404-byte Line Update chunk's table, part of the timeslot table and transform) are produced by DLL code, so reproducing them is the B2 work, not a download.
- Not present, per unit and still needing Windows or a Linux recalibration (B2): the calibration frame, the baseline frame (together about 16.5 KB of the 18,869 bytes), and the 56 factory values (readable from the sensor with the read-only `6f` command, to be confirmed).
- Other chunks (the fixed ones) were identical between two different units' references, per `research/calibration-analysis.md`.

## Redistribution and licence

- We should not ship any of this. The plan is right: the setup tool downloads at install time and checks the sha256 of the extracted module.
- Catalog files are Synaptics' proprietary binaries, which Microsoft redistributes through Windows Update; the catalog needs no login or cookie, and the cab URL is plain HTTPS. No licence text was found that permits third-party redistribution, and none was examined in depth. A tool that fetches from Microsoft or from the reseller does not redistribute them. Precedent: python-validity's `validity-sensors-firmware`.
- Do not pin the cab hash as the only check: Microsoft can re-sign or replace cabs. Pin the module sha256 (final proof), and treat the cab sha256 as a convenience.
- Catalog URLs carry a per-file hash and are stable for years in practice, but are not guaranteed. The catalog page search `USB\VID_06CB&PID_0078` can discover the current URL; the download URL comes from a POST to `DownloadDialog.aspx` with the update id.

## Recommendation

1. Best: Microsoft Update Catalog cab #1 (candidate 1 URL above). No login, small (3.4 MB), plain `cabextract`, `synaWudfBioUsb.dll` at 0x19cca0, same offset as ours. Tool logic: download, cabextract, search the DLL for the 10500-byte module by its sha256 (scan, do not trust the offset), write `module.bin`.
2. Fallback: the Origin PC / Corsair `02_Finger.zip` (candidate 3), or catalog cab #2. The scan-by-hash approach covers the different offset.
