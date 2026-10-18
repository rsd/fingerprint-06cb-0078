# Synaptics 06cb:0078 fingerprint reader: handoff notes for the Linux side

Written 2026-10-13 from Windows 11 to pick this work up from Linux.
Read this first. README.md has the shorter collection log. This file explains what is known, what is guessed, what failed, and what to do next.

Facts are labeled **[verified]** (seen directly on Windows) or **[hypothesis]** (inferred and not yet confirmed).

---

## 1. Goal

Make the built-in fingerprint reader on this laptop work under Linux. As far as we know, there is no driver for it in libfprint, fprintd or python-validity.

## 2. Hardware identity [verified]

| Item | Value |
|---|---|
| Laptop | Clevo P870KM (Windows reports manufacturer "Notebook", model "P870KM_KM1") |
| Windows name | "Synaptics WBDI Fingerprint Reader - USB 073" |
| USB VID:PID | **06cb:0078** (Synaptics), bcdDevice 0x0154 |
| USB serial / instance | <serial> |
| Device class | 0xFF / subclass 0x10 / protocol 0xFF (vendor-specific) |
| bcdUSB | 2.00, full speed (DeviceSpeed=1 in ETW), bMaxPacketSize0 = 8 |
| Configurations | 1. iManufacturer = 0, iProduct = 0, iSerialNumber = 1 |
| Location | Internal USB, on a root hub port |

### Endpoints [verified, from Windows USB ETW]
| EP | Dir | Type | wMaxPacketSize | bInterval |
|---|---|---|---|---|
| 0x00 | ctrl | control | 8 | – |
| 0x01 | OUT | bulk | 64 | – |
| 0x81 | IN | bulk | 64 | – |
| 0x82 | IN | bulk | 64 | – |
| 0x83 | IN | interrupt | 8 | 4 |
| 0x84 | IN | interrupt | 16 | 10 |

[hypothesis] By analogy with python-validity's sensors: 0x01 and 0x81 carry the command/response channel (TLS-wrapped after the handshake), 0x82 is probably the image/bulk data channel, and 0x83 is the interrupt channel for "finger down/up" events. 0x84 is unknown.

On Linux, confirm all of this first with:
```
lsusb -v -d 06cb:0078 > lsusb-v.txt   # local-only, gitignored (embeds the unit serial)
```

## 3. Windows driver stack [verified]

- INF: `synawudfbiousbclevoprod.inf`. Synaptics 5.5.2707.1073, dated 2018-03-08, a **Clevo-specific build**. It is WHQL-signed.
- It is a **UMDF (user-mode) driver**. The kernel side is just WUDFRd plus the WinUsb lower filter. So **all of the protocol logic is in user-space DLLs**, which makes it a good reverse-engineering target.

| File | Role |
|---|---|
| `synaWudfBioUsb.dll` (2.6 MB) | **The driver.** USB protocol, TLS, pairing, firmware upload, calibration. Exports only `DllGetClassObject` and `Microsoft_WDF_UMDF_Version`. Source tree name in strings: `D:\workspace\clevo-HDRFP-2685\sw\usdk5Core\src\{vfm,scs,...}`. FileDescription: "Synaptics Fingeprint Sensor Driver". |
| `synaBscAdapter.dll` (360 KB) | WinBio sensor and engine adapter (Windows Hello glue and matching). |
| `WudfUpdate_01011.dll` | UMDF co-installer. Irrelevant. |

- WinBio configuration 0: SensorMode=2 ("Advanced"), SystemSensor=1, sensor and engine adapter `synaBscAdapter.dll`, storage `WinBioStorageAdapter.DLL`, DatabaseId 7B5D3242-420A-4287-8CFE-495C28DD9F21.
- WinBio configuration 1 references `synaAdvAdapter.DLL`, which **does not exist on disk** (a leftover from an older driver).
- Windows Hello **works** with this sensor. It identified the user successfully on 2026-10-12, so fingerprints **are enrolled**.

## 4. Protocol clues from the driver strings [verified strings, hypothesis meaning]

Interesting strings in synaWudfBioUsb.dll (with file offsets; see `extracted/synaWudfBioUsb_strings.txt`):

- `scsTLS.c`, `BCryptGenerateKeyPair`, `BCryptImportKeyPair`, `BCryptFinalizeKeyPair`: the host↔sensor link is a **TLS-like encrypted channel** with ECDH-style key exchange. This is the same design as the Validity/Synaptics "match-on-chip" family that python-validity reversed (138a:0090, 138a:0097, 06cb:009a, …).
- `CBiometricDevice::DoPairing`, `DoUnpairing`, `ProcessPairing`, `OnResetOwnership`, the `PairingData`, `PairingContext` and `UnpairingContext` strings, and `CBiometricDeviceUSB::DeviceReset`: the host **pairs** with the sensor (takes ownership), and that ownership can be reset.
- `.xpfwext`, `\ProgramData\Validity\fwext\`, `CBiometricDevice::UpdateFirmwareExtension`, `OnUpdateFirmware`, and the `FwextType / FwextMajorver / FwextMinorver / FwextBuildnum / FwextBuildtime` strings: the host uploads a **firmware extension** to the sensor. python-validity does the same with a `.xpfwext` file that it pulls from the Windows driver.
- `checkSGXModeAndTransportOpen`: the driver can use **Intel SGX**. [hypothesis] The pairing keys may be sealed in SGX, which would explain why we never found them.
- `SCM_ADC_FW_CTRL`: a sensor ADC/firmware control command name.

**The firmware extension file was NOT on disk.** `C:\ProgramData\Validity\fwext\` doesn't exist. So [hypothesis] the fwext is embedded in the DLL. There is a **496 KB high-entropy region at file offset 0x160000–0x1dc000** of synaWudfBioUsb.dll (inside .rdata/.data). It is saved as `extracted/synaWudfBioUsb_hiEntropy_0x160000.bin`. It could be the compressed or encrypted fwext, the matcher, or crypto tables. It needs investigating. There is also an 8 KB high-entropy region at 0x13e000–0x140000 (possibly keys or certificates).

The PE resources are only a message table and version info (`extracted/synaWudfBioUsb_resources/`). There is no firmware there.

## 5. Keys: what Linux needs

There are probably two kinds of secrets (by analogy with python-validity):

1. **Built-in driver constants, the same on every machine.** These are hard-coded keys and passwords used to set up the factory TLS/handshake and to decrypt or validate the fwext. python-validity's versions came out of the Windows driver DLL. For 06cb:0078 they should be in `synaWudfBioUsb.dll`, which is in `driverstore/`. **This is what Linux needs.** Getting it means reverse-engineering the DLL (Ghidra), starting from `scsTLS.c` and the pairing functions.
2. **The per-machine pairing state.** Windows generated a host key at pairing time, and the sensor stores the matching record. **Not found**: there is no `PairingData` value anywhere in HKLM\SYSTEM\CurrentControlSet or HKLM\SOFTWARE. [hypothesis] It is under another name, derived at runtime, or sealed in SGX. **Linux doesn't need it** if Linux re-pairs (resets ownership and pairs with its own key), which is what python-validity does.

⚠️ **Dual-boot consequence:** the sensor can be owned by only one host key at a time. Pairing from Linux will most likely break Windows Hello and wipe the templates on the sensor, so you would have to re-enroll in Windows, and then Windows would re-pair and might break Linux in turn. Sharing one pairing between both OSes would require the Windows pairing key, which we didn't find.

## 6. Calibration data [verified]

- Registry: `HKLM\SYSTEM\CurrentControlSet\Enum\USB\VID_06CB&PID_0078\<serial>\Device Parameters\Device Data\CalibrationData` (REG_BINARY, 26470 bytes).
- It is a **DPAPI blob** (starts with `01000000 d08c9ddf 0115d111 8c7a00c0 4fc297eb`). It decrypts **only as the SYSTEM account** (CurrentUser scope under SYSTEM). It does not decrypt as an admin user with LocalMachine scope.
- Decrypted: `extracted/CalibrationData_decrypted.bin` (26244 bytes). Raw: `extracted/CalibrationData_dpapi.bin`.
- Contents: a header with little-endian ints (`6c220000 e4000000 02000000 49240000 …`) and leftover 64-bit heap pointers. Then, from about offset 0xEC, **per-pixel baseline values around 0x80–0x90**, followed by more varied data. [hypothesis] It is a raw memory dump of the driver's calibration struct, containing the sensor's baseline/flat-field frame. It may be useful for image correction. It is not keys.
- [hypothesis] Under Linux, python-validity-style drivers run their own calibration on the sensor, so this blob is mainly a reference.

## 7. Other files found

- `C:\ProgramData\Validity\valfpssc`: 72 bytes, header `SSC!` + `02000000` + 64 bytes, from 2017-07-13. Copied to `programdata/`. [hypothesis] An old Validity "secure storage container" from an earlier driver install.
- `C:\Windows\System32\WinBioDatabase\7B5D3242-….DAT` (162 KB) is the WinBio DB for this sensor. It was **not copied** on purpose: it holds per-user, Windows-encrypted template and identity data.
- `C:\Program Files\Synaptics\SynTP\` is the **touchpad** driver only. It is unrelated. The touchpad is Synaptics SMBus `SYN1220`.
- Events: `logs/Biometrics-Operational.evtx` and a text extract filtered to this sensor. Every boot shows "failed to start its secure component … Reason 24 … 0x80070032" (Windows Hello Enhanced Sign-in Security is unavailable). This is normal on this hardware.

## 8. USB traffic capture: what failed and why

- **USBPcap** (installed, interface `\\.\USBPcap1`) lists the sensor but **captured ZERO packets** from it. This held with `--devices`, with `-A`, after re-enumeration, and while Windows was actively reading the sensor. It did capture other devices on the same hub. Cause unknown.
- Each `pnputil /restart-device` re-enumerates the sensor with a **new USB address**.
- **Windows USB ETW** (USBXHCI, UCX and USBHUB3 providers) works. But it logs only URB metadata (endpoint, length, flags, timing), **not payload bytes**. A sensor-only subset was recorded during a WinBioIdentify (kept locally, not published). The endpoint table in §2 and the transfer lengths come from it.
- Raw full-hub captures were **deleted on purpose** because they contained traffic from other devices on the hub.

**The way to get payloads (this is how python-validity was reversed):** from Linux, run Windows in QEMU/KVM with USB passthrough of 06cb:0078 and the driver from `driverstore/` installed. Capture on the host:
```
sudo modprobe usbmon
lsusb -d 06cb:0078          # note bus number N
sudo wireshark -i usbmonN   # or: sudo tshark -i usbmonN -w fp.pcapng
```
Capture these sequences: driver load (fwext upload + TLS handshake + pairing), enrollment, identification, and the user clicking "Remove" in Windows Hello (reset). The payloads after the handshake are TLS-encrypted, so you will still need the keys or a debugger on the VM to read them.

## 9. Suggested plan on Linux

1. `lsusb -v -d 06cb:0078` to confirm the descriptors and endpoints above.
2. Check the current state of support: search the libfprint supported-devices list, python-validity (github.com/uunicorn/python-validity) and its issues, plus the libfprint/fprint issue trackers, for **06cb:0078** and "Synaptics 0078". Look for newer forks and libfprint "synaptics"/"validity" drivers. Our knowledge says it's unsupported; **re-check this, don't trust it.**
3. If python-validity has nothing: compare its protocol (init commands, TLS handshake, fwext upload) with our DLL in **Ghidra**. Start from the strings `scsTLS.c`, `DoPairing`, `UpdateFirmwareExtension`, `.xpfwext` and `checkSGXModeAndTransportOpen`, and from the high-entropy blobs at 0x13e000 and 0x160000.
4. Try python-validity's init/handshake against the device **read-only first** (identify-type commands) before anything that pairs, resets or partitions the flash. Remember the dual-boot consequence in §5.
5. If needed, use the VM + usbmon capture (§8).

## 10. Folder inventory

```
README.md                        short collection log
HANDOFF-LINUX.md                 this file
driverstore/                     full driver package (INF, CAT, PNF, 3 DLLs). Same hashes as installed copies.
installed/                       the DLLs as installed in System32 (identical SHA256 to driverstore)
registry/enum_device.reg         device Enum key (includes the encrypted CalibrationData and the WinBio configs)
registry/class_biometric.reg     Biometric class key {53d29ef7-377c-4d14-864b-eb3a85769359}
registry/svc_WbioSrvc.reg        Windows Biometric Service key
registry/sw_synaptics.reg        HKLM\SOFTWARE\Synaptics (mostly touchpad)
pnp_properties.txt               all PnP properties of the device (local-only, gitignored)
system_info.txt                  laptop model / BIOS (local-only, gitignored)
logs/                            Biometrics event log (.evtx plus a filtered text copy)
programdata/valfpssc             old Validity "SSC!" file (72 bytes)
extracted/
  synaWudfBioUsb_strings.txt     all ASCII (A) and UTF-16 (W) strings with file offsets
  synaBscAdapter_strings.txt     same, for the WinBio adapter
  synaWudfBioUsb_hiEntropy_0x160000.bin   496 KB high-entropy region (fwext? matcher?)
  synaWudfBioUsb_resources/      PE resources (message table, version info)
  CalibrationData_dpapi.bin      encrypted calibration (registry)
  CalibrationData_decrypted.bin  decrypted calibration (SYSTEM DPAPI)
  CalibrationData_decrypt.log    result of the decrypt run
usb_capture/
  etw_sensor_events.xml          Windows USB ETW events for this sensor only (no payloads)
```

None of these files is in the public repo: the driver files are proprietary, and the rest is per-machine data (`.gitignore` excludes them). The findings drawn from them are in this file and in README.md.

## 11. Side effects left on Windows

- The sensor was restarted (`pnputil /restart-device`) several times. It still works: Windows Hello unlocked with a fingerprint afterwards.
- The one-time SYSTEM scheduled task used for the DPAPI decrypt was **deleted**, and the USB ETW trace session was **stopped**. No capture processes are left running.
- USBPcap and Wireshark were already installed. Nothing new was installed.
