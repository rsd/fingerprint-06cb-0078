# proto: probe and first capture for Synaptics 06cb:0078

Two programs. `probe.py` asks the reader three questions (optionally a fourth, `6f`)
and prints/decodes the answers; it changes nothing on the device. `capture.py`
replicates the Windows init + identify sequence and saves the first image; it
writes a RAM module and a capture program to the sensor (see "capture.py" below).

## What it sends

Exactly three one-byte plaintext commands on bulk OUT `0x01`, one at a time, each
followed by a read of the reply on bulk IN `0x81` (timeout 3 s by default):

| order | byte | name | expected reply |
|---|---|---|---|
| 1 | `01` | RomInfo | 38 bytes: status, timestamp, build, ROM major/minor, product ... |
| 2 | `19` | opaque query (layout undocumented) | 68 bytes, mostly zeros |
| 3 | `75` | sensor id | 10 bytes: status, reserved, minor, major -> sensor type `0x2449` by table lookup |

Nothing else is ever written (the optional `6f` query below aside): no USB reset, no `clear_halt`, no vendor control
transfers, no other endpoints. `set_configuration()` is called only if the device
reports it is unconfigured (it normally is configured). If a read times out the
probe stops; it does not send the remaining commands.

Decoded fields are tagged `[CERTAIN]` or `[UNCERTAIN]`. Only the status word,
the ROM major/minor, and the 0x75 major/minor/sensor-type lookup are firmly
established by the references; everything else is an observation or a guess and
says so. The sensor type is not in the 0x75 reply itself: it comes from a
(major, minor) table (two rows embedded from python-validity's `hw_tables.py`).

## Setup

```sh
cd proto
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt
```

libusb 1.0 must be installed system-wide (pyusb uses its libusb1 backend).

### Access without root (udev)

Create `/etc/udev/rules.d/70-fingerprint-06cb-0078.rules` containing:

```
SUBSYSTEM=="usb", ATTRS{idVendor}=="06cb", ATTRS{idProduct}=="0078", TAG+="uaccess"
```

then `sudo udevadm control --reload && sudo udevadm trigger --subsystem-match=usb`
and re-plug the device (or log in again). The logged-in user then gets access via
`uaccess`.

## Run

```sh
.venv/bin/python probe.py --dry-run                 # print what would be sent, no USB access
.venv/bin/python probe.py                           # the real probe
.venv/bin/python probe.py --detach-kernel-driver    # only if it refuses because a kernel driver holds interface 0
.venv/bin/python probe.py --only 01,75              # a subset (01, 19, 75; and 6f only when named)
.venv/bin/python probe.py --only 6f                 # read-only factory-bits query, compared with the stored values
```

Options: `--timeout-ms N` (per transfer, default 3000), `--log-dir DIR`.

If a kernel driver is attached the probe refuses unless `--detach-kernel-driver`
is given; it re-attaches the driver on exit. On exit (normal, error, Ctrl-C,
SIGTERM) it releases interface 0 and disposes the libusb resources. Ctrl-C takes
effect once the transfer in flight returns (at most the timeout).

Exit codes: 0 ok, 1 USB error or timeout, 2 device not found, 3 kernel driver
attached and no flag, 4 a reply carried a non-zero status, 130 interrupted.

## The optional `6f` query

`probe.py --only 6f` sends the 9-byte read-only request `6f 00 0e 00 00 00 00 00 00`
(python-validity `get_factory_bits(0x0e00)`; the vendor DLL builds the same request).
It is never part of the default run. The reply is decoded entry by entry
(`ptr, length, tag, subtag, flags, value`); subtag 3 minus its 4-byte prefix should
be the 56 factory values, which are compared with `CalibrationData_decrypted.bin`
offset `0xEC..0x123` (`--calibration PATH`, default `../extracted/...`). The log has
`COMPARE` lines and a final `RESULT: MATCH` or `RESULT: DIFFER (...)` listing each
differing value. No 0078 reply to this command has been recorded anywhere, so the
layout is unconfirmed: the decoder never raises and reports what does not fit.

## Output

Terminal: for each command the request bytes, the raw reply as an offset hex
dump, then the decoded view. Log: `logs/probe-YYYYmmdd-HHMMSS-<microseconds>-p<pid>.log` (unique per run; a `-N` counter is added if the name is somehow taken), one
timestamped line per event (`TX`/`RX` lines carry the raw bytes as a single hex
string; `DECODE` lines carry the decoded view). The log is flushed per line, so an
interrupted run keeps what happened.

The probe never reads USB string descriptors, so the device serial number is not
requested or logged (`lsusb -v` shows it though; do not paste that publicly).

## calibration.py: the `02` payload

Moved to the shared package `synaptics0078/` at the repo root (with `calib_model.py` and `parsers.py`).
The files in `proto/` are alias shims, so `import calibration` still works in `proto/` and `tools/`.
`proto/calibration.py` also has a source-only mode for `tools/dev/b2_program.py`, which loads it by path as root.

Pure function from a unit's `CalibrationData_decrypted.bin` to the 18869-byte `02`
command (`calibration.build_cmd02_from_calibration_data`). The ~2.1 KB fixed part is
`data/cmd02_template.bin` (built from the vendor DLL; hash pinned; recreate it with
`python3 -m synaptics0078.template --dll PATH --out proto/data/cmd02_template.bin [--force]`
from the repo root; the test suite re-derives it); the four per-unit regions come from the blob as
in `research/calibration-analysis.md`. The tests rebuild iusearch's and ToLiKNoSoV's
payloads from their own inputs byte-exactly and check that our unit's output has
sha256 `fac382964da2e908...`. The third-party payloads in `testdata/` (`iusearch_cmd02.bin`,
`toliknosov_cmd02.bin`, `toliknosov_cmd03.bin`) are no longer tracked: kept locally, gitignored,
their tests skip when absent, and synthetic fixtures cover the logic. They are never executed. The template itself is not needed for most tests: without `data/cmd02_template.bin` they run on the synthetic template (`synth_template.py`).

## capture.py

```sh
.venv/bin/python capture.py --dry-run                 # print the full byte sequence, no USB
.venv/bin/python capture.py --no-finger --dry-run     # same, without the finger wait
.venv/bin/python capture.py --no-finger               # blank frame: 02, then 51 at once
.venv/bin/python capture.py                           # wait for a finger, then capture
```

Sequence: `01`, `19`, `75`; `06` (skipped when a module is already loaded, see below) + the 10500-byte module read at run time from
`../driverstore/synaWudfBioUsb.dll` offset `0x19cca0` (sha256 checked); `02` +
the payload from `calibration.py`; (unless `--no-finger`) interrupt EP `0x83`: finger-down
event, then image-ready event; `51 00 20 00 00` and a read of the 144 x 56 8-bit image on EP `0x81`;
`04`, `52` on every exit path once `02` has been sent. The dry run lists every step with
its source (ETW lines, reference file:line, analysis section).

Checks, before anything is written to the sensor: the `01` reply must say ROM 6/0x14, the
`75` reply sensor type 0x2449, and the serial in the `01` reply (bytes 0x12-0x17) must equal
the serial in the CalibrationData (`--skip-identity-checks` overrides, not recommended).

### Module already loaded (`06` skipped)

The module lives in the sensor's RAM and survives until the device is reset or re-plugged. Hardware
runs 1-2 showed: RomInfo (`01`) byte 17 (python-validity's `u1`) is `0x00` before the first `06` and
`0x0e` after it (bytes `0x1c-0x1d` also went `00 00` -> `02 01`), and a second `06` is refused with
status `0x04aa`. The vendor DLL prints that byte as the third component of the firmware version
(`%02u.%02u.%02u.%04u` = major.minor.u1.build, function `0x18005f960`, here `06.20.00.0154`) and
tests it for non-zero in `0x180080c00`. So `capture.py` skips `06` when byte 17 is non-zero and logs
why (`MODULE` line). This is an inference, flagged UNCERTAIN in the code, the decoded RomInfo and the
log. `--force-module` sends `06` anyway (expect `0x04aa` and a stop). Byte `0x1c-0x1d` is decoded but
its meaning is unknown.

### The `51` reply and `--no-finger`

`51` answers on EP `0x81` (never `0x82`, which is only for calibration frames). The 6-byte reply
`01 00 00 00 00 00` (status `0x0001`) means "no image / no finger": ToLiKNoSoV treats bytes `01 00` as
"No finger detected" (`synaptics_0078.c:266-274`), python-validity lists 1, 2, 5, 6, 7 as "ask again" (7 is handled as a failed capture here, see below)
(`sensor_2449.py:85`), and hardware run 1 got it right after `02` with nothing on the sensor. The
identify-mode capture program delivers an image only after a finger is detected; no reference takes a
blank frame this way (python-validity's blank frames come from its separate calibrate module, EP `0x82`
and the forbidden `1a`). So `--no-finger` is a protocol smoke test: it ends with `RESULT: no image, as
expected without a finger` and exit code 0 after `01 19 75 [06] 02 51 04 52`. With a finger, a "not
ready" status is followed by a plain read of EP `0x81` (iusearch `main.py:100-101`) and, if nothing
arrives within 1 s, a re-send of `51` (PR #288 `sensor_2449.py:546-553`), up to `--image-retries N`
replies (default 3). Whether the plain read or the re-send is what works on a 0078 is not yet known; the
log shows which one produced the image.

### Collecting a sample set

```sh
.venv/bin/python capture.py --count 10 --label index-right --dry-run   # look first
.venv/bin/python capture.py --count 10 --label index-right             # in your own terminal
```

One USB session, N images, saved as `captures/NAME/NAME-01.pgm` + `.raw`, `NAME-02...`, plus
`captures/NAME/manifest.json`. `--label` is letters, digits, `.`, `_`, `-`. Nothing is ever overwritten: a
label that already holds a collection is refused before the device is touched, unless you pass `--append`.

`--append` tops up an existing label: `--label left-index --count 3 --append` adds three **more** images
(numbered after the highest existing `NAME-NN`, e.g. 08-10) to the same folder and manifest. The manifest gets
a new entry in `sessions` (`first_index`, `requested_count`, `info`, `status`); a manifest written before
`sessions` existed gets one synthesised from its top-level fields. A foreign manifest (other label) is
refused; `--append` on a label that does not exist yet simply starts it.

What is repeated per image: `02`, wait for finger-down and image-ready on EP `0x83`, `51`, then `04` and
`52` (the block that worked on hardware). `06` is sent at most once. Evidence for re-sending `02`:
iusearch re-sends it after every capture (`main.py:105-109`), PR #288 `get_print()` sends it at the start
of every capture, and the ETW only recorded one identify, which ends with `04`/`52`. That `02` can be
sent again without `06` in between is the same evidence (PR #288 loads the module once per session); it
has not been confirmed on the 0078 yet.

Prompts per image: `[3/10] touch the sensor now` (terminal bell, `--no-bell` to silence), then after the
image a one-line summary (`min, max, mean, sd, ridge coverage ~N%`; coverage is the share of 8 x 8
blocks with sd > 15, a heuristic measured on the first real capture: finger blocks sd 30-70, the blank
baseline < 5), then `[3/10] lift your finger`. **No finger-up event is known**: none of
python-validity, iusearch, ToLiKNoSoV or the ETW shows one. So the lift is a prompt, not a sensor
signal: on a terminal the tool waits for Enter (type `r` + Enter to discard the image just taken and
retake it; the discarded files stay on disk and are marked `accepted: false` in the manifest, and the
replacement gets the next number); without a terminal, or with `--lift-pause-s S`, it pauses a fixed
time (3 s default) and logs it. Before each `02` the tool drains EP `0x83` (100 ms quiet) and logs every
stale event as `DRAIN` (kept in the manifest as `drained_before`), so a late event from the previous
touch cannot count as the next finger-down, and any real finger-up event shows up in the data. A take that
is nearly identical to the previous one (mean abs pixel difference < 3) is flagged `finger probably not
lifted` (heuristic).

**Failed captures are retried, not fatal.** A touch that is too fast can end with a failed capture (hardware
run, image 8 of left-index, about 1 s per image). Three things count as one: a `51` reply whose image header
carries a non-zero error field (the reply was `07 00 0c 00 00 00 00 00 00 00 4d 01 08 00 02 00 00 80`:
status `0x0007`, error field `0x80000002`, no pixels; a good header is `00 00 8c 1f 00 00 38 00 90 00 4d 01
08 00 00 00 00 00`), a `51` reply of `0x0412` as the first answer, and a failed/aborted EP `0x83` event (code
0 or 6). The tool logs it (`bad capture (error 2), lift and touch again`), tells you, ends the block with
`04` and `52`, waits for Enter (or the fixed pause), and restarts that same image from `02`. At most
`--max-attempts` (default 3) failed captures per image, then it gives up cleanly (exit 6; saved images and
manifest stay). Failed attempts are listed under `failed_attempts` in the manifest (reply hex, events, message)
and do not use up an image number. In single-image mode the same failure ends with the message, the raw reply
saved as `.raw`, and exit code 6. Timeouts and a bad status on a command (`01 19 75 06 02 04 52`) still stop the run.

The error field: python-validity raises "Scanning problem" for any non-zero value (`sensor.py:733-734`); no
reference and no DLL string says what a code means, so `error 2` is decoded with an UNCERTAIN tag and
nothing more. The one observation also had a ready event with code 7 (`03 60 07 00 40`) instead of the usual
code 4 (`03 42 04 00 40`); python-validity counts code 7 as ready, so `51` is still sent, and the log notes it.

If something else fails partway (timeout, bad status, Ctrl-C) the images already saved stay, the
armed block is stopped with `04`/`52`, and the manifest records `incomplete after K accepted image(s): ...`.

`manifest.json`: `label`, `requested_count`, `status`, `accepted_count`, `info` (module/program hashes,
ROM, log file name), `sessions[]`, `failed_attempts[]` and `images[]`, each with `pgm`, `raw`, `timestamp`, `accepted`, `stats`
(min/max/mean/sd/coverage), `events` (EP `0x83` payloads between `02` and `51`), `drained_before`,
`reply_header`, `warnings`, `status_04`, `status_52`.

### Status `0x0412` (from `04` and from `51`)

`0x0412` is logged as `no capture in progress (inferred) [UNCERTAIN]`, at INFO level, never as an error.
Evidence, all from our own logs: `04` answered `0x0000` while a capture was still waiting (the two
`--no-finger` runs, a finger timeout) and `0x0412` after every delivered image and after the one failed
capture; a re-sent `51` after the `0x0007` error reply also answered `0x0412`. The references discard the
`04` status (python-validity `sensor.py:726`, iusearch `main.py:132`) and the ETW only shows a 2-byte reply.
(An earlier version called it "capture already finished"; the failed capture showed it also appears when
no image was delivered.)

Hard limits: only the opcodes 01 19 75 06 02 51 04 52 can be written (`check_opcode`);
`4f`, `1a` and anything else is refused in code. No flash, pairing or partition command. No USB
reset unless `--usb-reset`; no `clear_halt`. Every status word is checked; the first
non-zero status or timeout stops the run. The one exception is the `51` image request (above):
"not ready" statuses 1, 2, 5, 6 (short reply, no image header) are retried up to `--image-retries` times; the expected `--no-finger`
answer ends the run successfully.

Options: `--dry-run`, `--no-finger`, `--timeout-ms` (per transfer and for the image-ready
event, default 5000), `--finger-timeout-ms` (default 20000), `--log-dir`, `--out-dir`,
`--dll`, `--calibration`, `--detach-kernel-driver`, `--usb-reset`, `--image-retries` (default 3),
`--force-module`, `--skip-identity-checks`, `--count`, `--label`, `--append`, `--max-attempts`, `--lift-pause-s`, `--no-bell`.

Output: `captures/capture-<date>-<time>-<microseconds>-p<pid>.pgm` (the image) and `.raw`
(the complete `51` reply, 8082 bytes: 18-byte header + 8064 pixels). A `51` reply that
does not parse as an image is kept as a `.raw` only. Log: `logs/capture-....log`, same
format as the probe's.

Exit codes: 0 ok, 1 USB error or timeout, 2 device not found, 3 kernel driver attached,
4 non-zero status / bad reply, 5 bad input (DLL hash, CalibrationData, identity mismatch),
6 capture failed (device event 0/6) or an unsafe write was refused, 130 interrupted.

## fingerlift.py: the finger-lift experiment (brief step A2, decision D5)

```sh
.venv/bin/python fingerlift.py --dry-run          # the whole plan, no USB
.venv/bin/python fingerlift.py                    # all four scenarios, in your own terminal
.venv/bin/python fingerlift.py rearm-down         # one scenario (baseline, lift, rearm-down, rearm-up)
```

No finger-up event is known, so this guided run records what EP `0x83` does around a lift. It prompts
`Don't touch the sensor` (then waits for Enter), `Place your finger and keep it there`, `Keep your finger on
the sensor, do not lift` and `Lift your finger now`, and logs every EP `0x83` event and every command reply
with a monotonic `t=` stamp. Scenarios: 1 `baseline` (02 with nothing touching, listen 5 s); 2 `lift` (2a:
image, "lift", listen 8 s while still armed, then 04/52; 2b: image, 04/52, then "lift", listen 8 s);
3 `rearm-down` (image, 04/52, 02 again with the finger still down, listen 5 s, then "lift", listen 5 s);
4 `rearm-up` (image, 04/52, "lift", 2 s, 02 with nothing touching, listen 5 s). Times: `--baseline-s`,
`--lift-s`, `--rearm-s`, `--gap-s`. An image-ready event during a listen is answered with `51` and the image
saved, so a same-touch image after the re-arm in 3 is kept and compared (mean abs difference).

It sends only capture.py's commands through the same guard (`01 19 75 [06] 02 51 04 52`; `06` only when
RomInfo byte 17 is 0), ends every armed block and every exit path (Ctrl-C too) with `04`/`52`, and releases
the interface, so it can be run again at once. An EP `0x83` timeout is "no event". A block whose touch never
came is marked INCOMPLETE, closed with `04`/`52`, and the run goes on (exit 6 at the end). Output: the log
`logs/fingerlift-*.log`, its records as `logs/fingerlift-*.json`, images in
`captures/fingerlift/fingerlift-<time>-p<pid>/<block>-<n>.pgm` + `.raw`, and a per-scenario summary (each
prompt, command and event with the time since the prompt and since the last `02`).

## calibrate.py: the B2 calibration experiment

Goal: on Linux, without Windows, measure the per-unit parts of the 18869-byte `02` program: the 56
factory values (read-only `6f`) and two blank calibration captures (no finger). It does **not** build the full
program; `calib_program.py` does that offline from a run directory (main README, "Linux-built capture program (B2b)"). The pure
model (byte layout, check score, comparisons, thresholds) is in `synaptics0078/calib_model.py`; the why and the byte map
are in `research/steps/b2-calibration-source.md`.

### What it sends

In this order, each command followed by a read of the reply on EP `0x81` (3 s):

1. `01`, `19`, `75`, then `6f 00 0e 00 00 00 00 00 00` (all reads, before anything changes state).
2. `06` + the pinned 10500-byte module, **only** when RomInfo byte 17 is 0 (no module loaded). Then `01` again,
   logged only.
3. `02` program #1 (1917 B, no calibration applied), then 51224 B on EP `0x82`, then `04`, `52`. The first
   frame gives the calibration frame (CF); its check score must be 200 or less.
4. `02` program #2 (10269 B, CF applied), then 51224 B on EP `0x82`, then `04`, `52`. This gives the baseline frame (BF).

`04` and `52` are also sent on every exit path once a `02` was attempted (not if the reader is gone).
Every write goes through one send function that checks it first. A `02` must have the right length, the right
sha256 of its skeleton (the program with its per-unit bytes zeroed, so everything else is pinned), and two
identical `0x43` factory entries.

### What it never sends

`1a`, `4f`, `50`, `51`, any flash or db command, a `0x48` chunk, the identify program, or anything not on the
allow-list (refused before USB is touched). No USB reset, no `clear_halt`.

### Run

```sh
.venv/bin/python calibrate.py              # dry run (default): opens no USB, prints each command byte, length, sha256
.venv/bin/python calibrate.py --run        # the real run, in your own terminal
```

Before `--run`: fingers off the reader for the whole run (it says when it is done), and fprintd not holding the
reader. If it reports the reader is busy: close any fingerprint prompt (lock screen, settings), wait a moment,
run again; if it persists, `sudo systemctl stop fprintd` (it starts again on demand).

Options: `--module PATH` (10500-byte module, hash-checked; default `local/synaptics0078/module.bin`, then the
vendor DLL), `--windows-calibration PATH` (Windows `CalibrationData_decrypted.bin` to compare with, optional;
default `extracted/CalibrationData_decrypted.bin`).

### Output

`local/calib/<YYYYmmdd-HHMMSS>/`, directory mode 0700, files 0600, never overwritten. It holds every reply
(`reply-NN-<op>.bin`), both EP `0x82` dumps (`ep82-1.bin`, `ep82-2.bin`), both programs as sent (`program1.bin`,
`program2.bin`), `cf.bin`, `bf.bin`, the derived per-unit chunks (`chunk-34-keyline.bin`, `chunk-30-data.bin`,
`chunk-43-factory.bin`, `chunk-48-baseline.bin`), `result.json` and `calibrate.log`. The unit serial (RomInfo bytes
`0x12-0x17`) is never printed, logged or saved: it is zeroed in the saved RomInfo and shown as `<redacted>`.

### Pass criteria

All of these must hold for PASS (exit 0):

- **Check score <= 200.** 200 is Windows' own threshold in the DLL for sensor type `0x2449`; Windows scored 7 on
  our unit. A higher score stops the run before program #2 (FAIL).
- **Baseline sanity** (needs no Windows data): BF mean within 128 +- 4 and sd <= 6. The three known units
  have mean 127.5-127.9 and sd about 2.
- **Factory values:** all 56 identical to the Windows CalibrationData.
- **CF vs Windows**, compared in the `0x30` (q) domain: correlation >= 0.95, mean |d| <= 8, at least 95% of
  pixels within +-16. Basis: score 7 is an RMS of about 6.3 q-units; factory-vs-Windows correlation is 0.994;
  the signal sd is 48; adjacent rows correlate at only 0.72, so a one-row slip fails.
- **BF vs Windows:** mean |d| <= 3, at least 95% within +-8 (residuals above 8 occur only on about 20 clamped
  pixels per unit). Its correlation is shown but does not gate, because the frame is nearly flat.

Without Windows data, or with data for another unit (serial differs), the comparison is "not available" and
does not gate; only the score and the baseline sanity decide.

### Exit codes

0 PASS (or dry run), 1 USB error or timeout, 2 reader not found, 3 reader busy, 4 unexpected reply, 5 bad or
missing input, 6 FAIL, 7 command refused by the guard, 8 unexpected error in this program (a bug; traceback only
in `calibrate.log`), 130 interrupted (after `04`/`52`).

### Caveat

The two calibration programs are derived from the DLL's builders and have never been seen on the wire. A bad
program can at worst return an error status or stall the device; a replug or reboot clears that.

## Tests (offline, no device)

```sh
.venv/bin/python -m pytest -v
```

`test_parsers.py` and `test_parsers_capture.py` run the parsers on the replies recorded in
nmikhailov/Validity90 issue #63 (a 06cb:0078), with that unit's serial and unit-specific `19`
bytes replaced with synthetic ones: the 0x75 reply
`00 00 00 00 00 00 37 00 b7 00` decodes to major 0xb7, minor 0x37, sensor type
0x2449. `test_probe_flow.py` drives `probe.run()` against a fake pyusb and checks
that only `01`, `19`, `75` are written to EP 0x01, that EP 0x81 is the only read,
and that cleanup runs on success, timeout, USB error, Ctrl-C and kernel-driver
paths.

`test_collect_flow.py` (collection mode: sequence, manifest, late events, timeout partway, redo, quality stats),
`test_calibration.py` (byte-exact payload rebuilds), `test_capture_flow.py` (exact bytes written
in the finger and `--no-finger` flows, abort on bad status or timeout, cleanup on every exit
path), `test_fingerlift.py` (scenario sequencing, event logging, the closed command set, cleanup on Ctrl-C)
and the 6f/log-name cases in `test_probe_flow.py` complete the suite (200 tests).

The full gate, from the project root: `proto/.venv/bin/python -m pytest -q -p no:cacheprovider proto tools synaptics0078`.

## Files

- `probe.py` USB code and CLI; `UsbSession` is shared with `capture.py`.
- `fingerlift.py` the finger-lift experiment (reuses `capture.Session`).
- `capture.py` the capture sequence and CLI. `calibration.py` the pure `02` builder (shim to `synaptics0078/`); `data/` its template.
- `calibrate.py` the B2 calibration experiment; `calib_model.py` its pure model and thresholds (shim to `synaptics0078/`).
- `calib_program.py` offline: builds the full capture program (`program-linux.bin`) from a `calibrate.py` run directory; see the main README, "Linux-built capture program (B2b)".
- `captures/` images (created on first capture); `testdata/` reference payloads for the tests (third-party ones are local only).
- `parsers.py` pure parsing functions (no I/O), each field tagged with certainty (shim to `synaptics0078/`).
- `test_parsers.py`, `test_probe_flow.py` offline tests.
- `logs/` timestamped exchange logs.
