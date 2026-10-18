"""Offline tests of calibration.py (synaptics0078/calibration.py; proto/calibration.py is its alias).

Reference payloads in testdata/ are 02 commands recorded by two other projects
(untrusted third-party data, never executed): iusearch/Synaptics78 and
ToLiKNoSoV (CMD_LONG2 = tk2, CMD_LONG3 = tk3). As in research/calibration-analysis.md
section 0, each one is taken apart into its three inputs (factory values, calibration
frame inverted from the int8 values, baseline frame) and rebuilt; the result must
be byte-exact, which shows nothing else in the payload varies per unit.

Those payloads are third-party files kept on this machine only (gitignored since B5a),
so their tests skip where they are absent, and so do the tests that check the real template
file itself (proto/data/cmd02_template.bin, gitignored too: its pin, its provenance from
the references, and the programs rebuilt from the references). Synthetic programs, built here from
synthetic inputs on the synthetic template of synaptics0078/tests/fixtures.py (a fake
DLL's, laid out exactly as the real one), cover the same logic everywhere with no local
file; the real template is also rebuilt from the vendor DLL by
synaptics0078/tests/test_template.py.

The real-unit test needs ../extracted/CalibrationData_decrypted.bin (private to
this machine's owner, not copied into testdata/) and is skipped without it.
"""

import hashlib
import re
import struct
import sys
import tempfile
import unittest
import unittest.mock
from pathlib import Path

import calibration as cal  # puts the repo root on sys.path, so the package imports below work

from synaptics0078.tests import fixtures as fx

HERE = Path(__file__).resolve().parent
TESTDATA = HERE / "testdata"
REAL_CAL = HERE.parent / "extracted" / "CalibrationData_decrypted.bin"

OURS_SHA256_PREFIX = "fac382964da2e908"  # research/calibration-analysis.md section 0

LOG_DIRS = (HERE / "logs", HERE.parent / "local" / "logs")  # gitignored, this machine only
ROM_INFO_MARK = b"command 0x01: RomInfo"
RX_REPLY = re.compile(rb"RX\s.*?\b(\d+) byte\(s\) in [\d.]+ ms: ([0-9a-fA-F ]+)")


def logged_rom_serial():
    """Serial (bytes 0x12..0x17) of the first RomInfo reply in the local logs, or None."""
    for d in LOG_DIRS:
        for path in sorted(d.glob("*.log")) if d.is_dir() else ():
            data = path.read_bytes()
            at = data.find(ROM_INFO_MARK)
            while at >= 0:
                start = data.find(b"\n", at) + 1  # the reply is on the next line
                end = data.find(b"\n", start)
                m = RX_REPLY.search(data, start, end if end >= 0 else len(data)) if start else None
                if m and int(m.group(1)) == 38:
                    reply = bytes.fromhex(m.group(2).decode())
                    if len(reply) == 38:
                        return reply[0x12:0x18]
                at = data.find(ROM_INFO_MARK, at + 1)
    return None


# b -> q is many-to-one (clamping, 10/4 scale); pick the smallest b for each q.
_INVERSE = {}
for _b in range(256):
    _INVERSE.setdefault(cal.rescale(_b, False), _b)


def invert(payload):
    """Recover (CalibrationInputs, ival, flags) from a reference 02 command."""
    _, chunks = cal.split_chunks(payload)
    by_type = {}
    for typ, data in chunks:
        by_type.setdefault(typ, data)
    # factory values: first bit-packed entry of chunk 0x43 (after the 60-byte head)
    c43 = by_type[0x43]
    v0, mn = c43[60], c43[61]
    bits = (v0 & 7) + 1
    stream = int.from_bytes(c43[64 : 64 + (bits * cal.FACTORY_COUNT + 7) // 8], "little")
    factory = bytes(mn + ((stream >> (bits * i)) & ((1 << bits) - 1)) for i in range(cal.FACTORY_COUNT))
    # calibration frame from chunk 0x30: 36 column groups x 56 rows x 4 columns
    c30 = by_type[0x30]
    d0 = 4 + 8 * struct.unpack_from("<L", c30, 0)[0]
    data = c30[d0:]
    frame = bytearray(cal.FRAME_LEN)
    pos = 0
    for k in range(cal.COLUMNS // 4):
        for j in range(cal.LINES):
            for c in range(4):
                frame[j * cal.BPL + 8 + 4 * k + c] = _INVERSE[data[pos]]
                pos += 1
    baseline = by_type[0x48][4:]
    ival = struct.unpack_from("<I", by_type[0x26], 12)[0] - 0x20000
    flags = by_type[0x2E][4]
    return cal.CalibrationInputs(bytes(factory), bytes(frame), bytes(baseline)), ival, flags


def ref(name):
    return (TESTDATA / name).read_bytes()


THIRD_PARTY = ("iusearch_cmd02.bin", "toliknosov_cmd02.bin", "toliknosov_cmd03.bin")


def needs_ref(*names):
    """Skip unless the third-party payloads `names` (default: all three) are on this machine."""
    names = names or THIRD_PARTY
    missing = [n for n in names if not (TESTDATA / n).exists()]
    return unittest.skipIf(bool(missing), f"testdata/{', '.join(missing)} not present (third-party, kept local)")


SYNTH_TEMPLATE = fx.expected_template()  # 18869 B, the real layout, synthetic table bytes
needs_template = unittest.skipUnless(
    cal.TEMPLATE_PATH.exists(),
    "proto/data/cmd02_template.bin not present"
    " (the real template, gitignored, local-only; rebuilt from the vendor DLL)",
)


def synthetic_program(ival=None, flags=None):
    """A full 18869-byte program from _synthetic_inputs() and SYNTH_TEMPLATE (ival, flags default)."""
    kw = {k: v for k, v in (("ival", ival), ("flags", flags)) if v is not None}
    return cal.build_cmd02(_synthetic_inputs(), SYNTH_TEMPLATE, **kw)


SYNTHETIC_SETTINGS = ((0xA0, 0x02), (0xAA, 0x02), (0xAA, 0x23))  # as iusearch, tk2, tk3


class TemplateTests(unittest.TestCase):
    @needs_template
    def test_template_hash_pinned(self):
        data = cal.load_template()
        self.assertEqual(len(data), cal.PAYLOAD_LEN)
        self.assertEqual(hashlib.sha256(data).hexdigest(), cal.TEMPLATE_SHA256)

    @needs_template
    @needs_ref("iusearch_cmd02.bin")
    def test_template_is_the_reference_with_unit_regions_zeroed(self):
        """Provenance: re-derive the shipped template from iusearch's payload."""
        self.assertEqual(zero_regions_by_hand(ref("iusearch_cmd02.bin")), cal.load_template())

    def test_template_is_a_program_with_unit_regions_zeroed(self):
        """The same derivation on a synthetic program: only the four regions vary per unit."""
        self.assertEqual(zero_regions_by_hand(synthetic_program()), SYNTH_TEMPLATE)

    def test_corrupt_template_refused(self):
        with tempfile.TemporaryDirectory() as td:
            tmp = Path(td) / "bad.bin"
            tmp.write_bytes(SYNTH_TEMPLATE[:-1] + b"\x01")
            with self.assertRaises(cal.CalibrationError):
                cal.load_template(tmp)


def zero_regions_by_hand(payload):
    """`payload` with the four per-unit regions zeroed, by the layout in the module docstring."""
    header, chunks = cal.split_chunks(payload)
    out = bytearray(header)
    for typ, data in chunks:
        p = bytearray(data)
        if typ == 0x34:
            p[0:144] = bytes(144)
        elif typ == 0x30:
            d0 = 4 + 8 * struct.unpack_from("<L", p, 0)[0]
            p[d0:] = bytes(len(p) - d0)
        elif typ == 0x43:
            p[60:138] = bytes(78)
        elif typ == 0x48:
            p[:] = bytes(len(p))
        out += struct.pack("<HH", typ, len(p)) + p
    return bytes(out)


class RoundTrip(unittest.TestCase):
    def check(self, name, **expect):
        payload = ref(name)
        inputs, ival, flags = invert(payload)
        for key, value in expect.items():
            self.assertEqual({"ival": ival, "flags": flags}[key], value, key)
        rebuilt = cal.build_cmd02(inputs, ival=ival, flags=flags)
        self.assertEqual(len(rebuilt), cal.PAYLOAD_LEN)
        self.assertEqual(rebuilt, payload)

    @needs_template
    @needs_ref("iusearch_cmd02.bin")
    def test_iusearch_unit_byte_exact(self):
        self.check("iusearch_cmd02.bin", ival=0xA0, flags=0x02)

    @needs_template
    @needs_ref("toliknosov_cmd02.bin")
    def test_toliknosov_unit_byte_exact(self):
        self.check("toliknosov_cmd02.bin", ival=0xAA, flags=0x02)

    @needs_template
    @needs_ref("toliknosov_cmd03.bin")
    def test_toliknosov_second_write_byte_exact(self):
        # same unit, capture flags 0x23 (the re-arm variant)
        self.check("toliknosov_cmd03.bin", ival=0xAA, flags=0x23)

    @needs_template
    @needs_ref("iusearch_cmd02.bin")
    def test_defaults_match_quality_100_identify(self):
        inputs, _, _ = invert(ref("iusearch_cmd02.bin"))
        self.assertEqual(cal.build_cmd02(inputs), ref("iusearch_cmd02.bin"))

    def test_synthetic_programs_byte_exact(self):
        """invert() then build_cmd02() on synthetic programs with the three references' settings."""
        for ival, flags in SYNTHETIC_SETTINGS:
            with self.subTest(ival=hex(ival), flags=hex(flags)):
                payload = synthetic_program(ival, flags)
                inputs, got_ival, got_flags = invert(payload)
                self.assertEqual((got_ival, got_flags), (ival, flags))
                self.assertEqual(cal.build_cmd02(inputs, SYNTH_TEMPLATE, got_ival, got_flags), payload)

    def test_synthetic_defaults_are_quality_100_identify(self):
        inputs, ival, flags = invert(synthetic_program())
        self.assertEqual((ival, flags), (0xA0, 0x02))
        self.assertEqual(cal.build_cmd02(inputs, SYNTH_TEMPLATE), synthetic_program())


def without_unit_data(check):
    """check(step) -> problem text or None, run so that no real-unit data reaches a report.

    The unit's data (its serial above all) must not show in a failure: an error
    becomes "<step>: <error type>" with no message, and the caller fails outside this
    frame, so neither a traceback nor `pytest -l` locals hold the data.
    """
    step = ["starting"]
    try:
        return check(step)
    except Exception as e:  # noqa: BLE001 - its message or frames could carry the data
        error = type(e).__name__
    return f"{step[0]}: {error}"


def _check_build_from_real_blob(step):
    step[0] = "reading the calibration data"
    blob = REAL_CAL.read_bytes()
    step[0] = "building the 02 command"
    out = cal.build_cmd02_from_calibration_data(blob)
    step[0] = "comparing with the reference"
    if out[:1] != b"\x02" or len(out) != 18869:
        return "built 02 command has the wrong first byte or length"
    if hashlib.sha256(out).hexdigest()[:16] != OURS_SHA256_PREFIX or out != ref("ours_cmd02.bin"):
        return "built 02 command differs from testdata/ours_cmd02.bin"
    return None


def _check_parse_real_blob(step):
    step[0] = "reading the calibration data"
    blob = REAL_CAL.read_bytes()
    step[0] = "parsing the calibration data"
    i = cal.parse_calibration_data(blob)
    step[0] = "checking the parsed fields"
    if (len(i.factory), len(i.frame), len(i.baseline)) != (56, 8512, 8512):
        return "factory/frame/baseline sizes are not 56/8512/8512"
    # Structure only: the unit's serial must never be written into a tracked file.
    if len(i.serial) != 6:
        return "calibration serial is not 6 bytes"
    if i.serial in (bytes(6), b"\xff" * 6):
        return "calibration serial is blank"
    step[0] = "reading the RomInfo logs"
    logged = logged_rom_serial()
    if logged is not None and i.serial != logged:
        return "calibration serial differs from RomInfo serial"
    return None


class OurUnit(unittest.TestCase):
    @unittest.skipUnless((TESTDATA / "ours_cmd02.bin").exists(), "testdata/ours_cmd02.bin not present (gitignored)")
    def test_reference_output_hash(self):
        self.assertEqual(hashlib.sha256(ref("ours_cmd02.bin")).hexdigest()[:16], OURS_SHA256_PREFIX)

    @unittest.skipUnless(REAL_CAL.exists(), "extracted/CalibrationData_decrypted.bin not present")
    def test_built_from_calibration_data_matches_reference(self):
        problem = without_unit_data(_check_build_from_real_blob)
        if problem:
            self.fail(problem)

    @unittest.skipUnless(REAL_CAL.exists(), "extracted/CalibrationData_decrypted.bin not present")
    def test_parse_real_blob(self):
        problem = without_unit_data(_check_parse_real_blob)
        if problem:
            self.fail(problem)

    def test_without_unit_data_reports_step_and_type_only(self):
        def boom(step):
            step[0] = "parsing"
            secret = b"\x13\x57\x9b"  # synthetic
            raise ValueError(secret.hex())

        self.assertEqual(without_unit_data(boom), "parsing: ValueError")
        self.assertEqual(without_unit_data(lambda step: "why"), "why")
        self.assertIsNone(without_unit_data(lambda step: None))


class Validation(unittest.TestCase):
    def blob(self):
        b = bytearray(cal.MIN_CAL_LEN)
        struct.pack_into("<III", b, 4, 0xE4, 2, 0x2449)
        struct.pack_into("<II", b, cal.CAL_STRUCT + 0x70, 56, 0)
        struct.pack_into("<I", b, cal.CAL_STRUCT + 0x88, 8512)
        for desc in (0x2294, 0x22B4):
            struct.pack_into("<II", b, desc + 4, 152, 56)
        return b

    def test_good_synthetic_blob_parses(self):
        i = cal.parse_calibration_data(bytes(self.blob()))
        self.assertEqual(len(i.frame), 8512)

    def test_rejects(self):
        def put(off, fmt, val):
            return lambda b: struct.pack_into(fmt, b, off, val)

        mutations = {
            "sensor type": put(0xC, "<I", 0x1234),
            "version": put(8, "<I", 3),
            "tag": put(4, "<I", 0xE8),
            "factory size": put(cal.CAL_STRUCT + 0x70, "<I", 55),
            "frame size": put(cal.CAL_STRUCT + 0x88, "<I", 8000),
            "frame descriptor": put(0x2298, "<I", 144),
        }
        for name, mutate in mutations.items():
            b = self.blob()
            mutate(b)
            with self.assertRaises(cal.CalibrationError, msg=name):
                cal.parse_calibration_data(bytes(b))
        with self.assertRaises(cal.CalibrationError, msg="short"):
            cal.parse_calibration_data(bytes(self.blob())[:100])

    def test_synthetic_blob_parses_and_builds_like_the_real_one(self):
        """The synthetic counterpart of OurUnit's two real-blob tests: same parse, same build, no unit data."""
        inputs, serial = _synthetic_inputs(), bytes.fromhex("12ab30c4de5f")  # synthetic, not a real unit's
        blob = self.blob()
        blob[cal.FACTORY_OFFSET : cal.FACTORY_OFFSET + 56] = inputs.factory
        blob[cal.FRAME_OFFSET : cal.FRAME_OFFSET + cal.FRAME_LEN] = inputs.frame
        blob[cal.BASELINE_OFFSET : cal.BASELINE_OFFSET + cal.FRAME_LEN] = inputs.baseline
        blob[cal.SERIAL_OFFSET : cal.SERIAL_OFFSET + 6] = serial
        blob = bytes(blob)
        parsed = cal.parse_calibration_data(blob)
        self.assertEqual(
            (parsed.factory, parsed.frame, parsed.baseline, parsed.serial),
            (inputs.factory, inputs.frame, inputs.baseline, serial),
        )
        out = cal.build_cmd02_from_calibration_data(blob, template=SYNTH_TEMPLATE)
        self.assertEqual((out[:1], len(out)), (b"\x02", 18869))
        self.assertEqual(out, synthetic_program())
        self.assertNotIn(serial, out)

    def test_logged_rom_serial_reads_the_first_38_byte_reply(self):
        reply = bytes(0x12) + bytes.fromhex("12ab30c4de5f") + bytes(14)  # synthetic 38-byte RomInfo reply
        log = b"... command 0x01: RomInfo\nRX EP 0x81 38 byte(s) in 1.5 ms: " + reply.hex(" ").encode() + b"\n"
        with tempfile.TemporaryDirectory() as td:
            (Path(td) / "a.log").write_bytes(log)
            with unittest.mock.patch.object(sys.modules[__name__], "LOG_DIRS", (Path(td),)):
                self.assertEqual(logged_rom_serial(), reply[0x12:0x18])
            with unittest.mock.patch.object(sys.modules[__name__], "LOG_DIRS", (Path(td) / "none",)):
                self.assertIsNone(logged_rom_serial())

    def test_build_rejects_bad_sizes(self):
        good = cal.CalibrationInputs(bytes(56), bytes(8512), bytes(8512))
        with self.assertRaises(cal.CalibrationError):
            cal.build_cmd02(cal.CalibrationInputs(bytes(55), good.frame, good.baseline), SYNTH_TEMPLATE)
        with self.assertRaises(cal.CalibrationError):
            cal.build_cmd02(cal.CalibrationInputs(good.factory, bytes(8511), good.baseline), SYNTH_TEMPLATE)

    def test_rescale_matches_the_documented_formula(self):
        self.assertEqual(cal.rescale(0x80, False), 0)
        self.assertEqual(cal.rescale(0xFF, False), 127)  # clamped
        self.assertEqual(cal.rescale(0x00, False), 0x80)  # -128 as a byte
        self.assertEqual(cal.rescale(0x82, False), 5)  # (2*10)/4 = 5
        self.assertEqual(cal.rescale(0x82, True), 4)  # the key-line 5 -> 4 step


def _synthetic_inputs():
    factory = bytes(0x70 + (i * 7) % 32 for i in range(56))  # packs to exactly 5 bits
    frame = bytes((0x80 + ((i * 37) % 61) - 30) & 0xFF for i in range(cal.FRAME_LEN))
    baseline = bytes(128 + (i % 5) - 2 for i in range(cal.FRAME_LEN))
    return cal.CalibrationInputs(factory, frame, baseline)


def _offset_of(payload, typ):
    """Data offset of the first chunk `typ` in a 02 command."""
    off = 5
    while off < len(payload):
        t, size = struct.unpack_from("<HH", payload, off)
        if t == typ:
            return off + 4
        off += 4 + size
    raise KeyError(typ)


class Assemble(unittest.TestCase):
    """The shared splice: assemble_cmd02, unit_regions, program_problem."""

    @needs_template
    @needs_ref()
    def test_unit_regions_round_trip_on_references(self):
        self.round_trip({name: ref(name) for name in THIRD_PARTY}, cal.load_template())

    def test_unit_regions_round_trip_on_synthetic_programs(self):
        self.round_trip(
            {f"ival {i:#x}, flags {f:#x}": synthetic_program(i, f) for i, f in SYNTHETIC_SETTINGS}, SYNTH_TEMPLATE
        )

    def round_trip(self, payloads, template):
        for name, payload in payloads.items():
            with self.subTest(name=name):
                _, ival, flags = invert(payload)
                regions = cal.unit_regions(payload)
                self.assertEqual(
                    [
                        len(regions.key_line),
                        len(regions.line_update),
                        len(regions.factory_entries),
                        len(regions.baseline),
                    ],
                    [144, 8064, 78, 8516],
                )
                self.assertEqual(cal.assemble_cmd02(template, regions, ival, flags), payload)
                self.assertEqual(cal.unit_regions(cal.assemble_cmd02(template, regions, ival, flags)), regions)

    def test_build_equals_assemble_on_derived_regions(self):
        import calib_model as cm

        inputs = _synthetic_inputs()
        chunks = cm.derive_chunks(inputs.factory, inputs.frame, inputs.baseline)
        self.assertIs(cm.UnitChunks, cal.UnitChunks)
        for ival, flags in ((cal.DEFAULT_IVAL, cal.DEFAULT_FLAGS), (0xAA, 0x23)):
            built = cal.build_cmd02(inputs, SYNTH_TEMPLATE, ival, flags)
            self.assertEqual(built, cal.assemble_cmd02(SYNTH_TEMPLATE, chunks, ival, flags))
            self.assertEqual(cal.unit_regions(built), chunks)

    def test_spans_cover_the_regions(self):
        payload = synthetic_program()
        spans = cal.unit_region_spans(payload)
        self.assertEqual(list(spans), ["key_line", "line_update", "factory_entries", "baseline"])
        self.assertEqual(sum(hi - lo for lo, hi in spans.values()), 144 + 8064 + 78 + 8516)
        self.assertEqual(spans["factory_entries"][0], _offset_of(payload, 0x43) + 60)

    def test_assemble_rejects_bad_regions(self):
        good = cal.unit_regions(synthetic_program())
        e = good.factory_entries

        def with_(**kw):
            return cal.UnitChunks(**{**good.__dict__, **kw})

        bad = {
            "key line length": with_(key_line=good.key_line[:-1]),
            "line update length": with_(line_update=good.line_update + b"\0"),
            "factory length": with_(factory_entries=e[:-1]),
            "baseline length": with_(baseline=good.baseline[:-152]),
            "entries differ": with_(factory_entries=e[:-1] + bytes([e[-1] ^ 1])),
            "format byte": with_(factory_entries=(bytes([0x0D]) + e[1:39]) * 2),
            "reserved u16": with_(factory_entries=(e[:2] + b"\x01\x00" + e[4:39]) * 2),
            "baseline dims": with_(baseline=struct.pack("<HH", 144, 56) + good.baseline[4:]),
        }
        for name, chunks in bad.items():
            with self.subTest(name), self.assertRaises(cal.CalibrationError):
                cal.assemble_cmd02(SYNTH_TEMPLATE, chunks)
        with self.assertRaises(cal.CalibrationError):
            cal.assemble_cmd02(SYNTH_TEMPLATE, good, ival=0x100)

    def test_program_problem_accepts_valid_programs(self):
        self.assertIsNone(cal.program_problem(synthetic_program(), SYNTH_TEMPLATE))
        self.assertIsNone(cal.program_problem(synthetic_program(0xA0, 0x02), SYNTH_TEMPLATE))

    def test_program_problem_default_is_what_load_template_gives(self):
        other = fx.expected_template({**fx.FAKE_CHUNKS, "reg32": fx.rand("other tables", len(fx.FAKE_CHUNKS["reg32"]))})
        with unittest.mock.patch.object(cal, "load_template", lambda path=cal.TEMPLATE_PATH: SYNTH_TEMPLATE):
            self.assertIsNone(cal.program_problem(cal.build_cmd02(_synthetic_inputs())))
            self.assertIn("byte", cal.program_problem(cal.build_cmd02(_synthetic_inputs(), other)))  # other tables

    @needs_template
    def test_program_problem_default_is_the_real_template(self):
        self.assertIsNone(cal.program_problem(cal.build_cmd02(_synthetic_inputs())))
        self.assertIn("byte", cal.program_problem(synthetic_program()))  # the synthetic tables differ

    def test_program_problem_without_the_template_file(self):
        with tempfile.TemporaryDirectory() as td:
            missing = Path(td) / "cmd02_template.bin"
            with unittest.mock.patch.object(cal, "load_template", lambda path=missing: cal.Path(path).read_bytes()):
                self.assertIn("template not usable", cal.program_problem(synthetic_program()))

    @needs_template
    @needs_ref("iusearch_cmd02.bin")
    def test_program_problem_accepts_the_reference(self):
        self.assertIsNone(cal.program_problem(ref("iusearch_cmd02.bin")))
        self.assertIsNone(cal.program_problem(ref("iusearch_cmd02.bin"), cal.load_template()))

    def test_program_problem_reasons(self):
        good = synthetic_program()
        c43 = _offset_of(good, 0x43)

        def mutated(offset, value):
            p = bytearray(good)
            p[offset] = value
            return bytes(p)

        cases = {
            "short": (good[:-1], "18868 bytes"),
            "long": (good + b"\0", "18870 bytes"),
            "first byte": (b"\x03" + good[1:], "first byte is 0x03"),
            "header": (mutated(1, 0x99), "the 5-byte header"),
            "fixed byte": (mutated(_offset_of(good, 0x2F) + 1, good[_offset_of(good, 0x2F) + 1] ^ 0x40), "chunk 0x2f"),
            "chunk length field": (mutated(_offset_of(good, 0x23) - 2, good[_offset_of(good, 0x23) - 2] ^ 1), "chunk"),
            "ival": (synthetic_program(ival=0xAA), "chunk 0x26, data byte 12 (finger-detect threshold"),
            "flags": (mutated(_offset_of(good, 0x2E) + 4, 0x23), "chunk 0x2e, data byte 4 (capture flags"),
            "entries differ": (mutated(c43 + 60 + 39 + 10, good[c43 + 60 + 39 + 10] ^ 1), "entries differ"),
            "baseline dims": (mutated(_offset_of(good, 0x48), 144), "baseline dimensions"),
        }
        both = bytearray(good)
        both[c43 + 60] = both[c43 + 99] = 0x0D
        cases["format byte"] = (bytes(both), "format byte is 0x0d")
        both = bytearray(good)
        both[c43 + 62] = both[c43 + 101] = 1
        cases["reserved u16"] = (bytes(both), "reserved u16")
        for name, (program, needle) in cases.items():
            with self.subTest(name):
                reason = cal.program_problem(program, SYNTH_TEMPLATE)
                self.assertIsNotNone(reason)
                self.assertIn(needle, reason)
                self.assertLess(len(reason), 160)
                self.assertNotIn(program[5:20].hex(), reason)

    def test_program_problem_bad_template(self):
        self.assertIn("template", cal.program_problem(synthetic_program(), b"\x02\x00"))


class SourceMode(unittest.TestCase):
    """proto/calibration.py loaded by path from its source (as tools/dev/b2_program.py does, as root)."""

    def test_package_modules_are_compiled_from_source_not_a_planted_pyc(self):
        # A .pyc whose header matches the source (its mtime and size) runs instead of the source under
        # an import loader. In a copy of the tree, plant one for synaptics0078/pins.py and calibration.py.
        import importlib.machinery
        import importlib.util
        import os
        import py_compile
        import shutil
        import sys
        import types

        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            (root / "proto").mkdir()
            (root / "synaptics0078").mkdir()
            shutil.copyfile(HERE / "calibration.py", root / "proto" / "calibration.py")
            shutil.copyfile(HERE.parent / "synaptics0078" / "__init__.py", root / "synaptics0078" / "__init__.py")
            caches = []
            for name in ("pins", "calibration"):
                src = root / "synaptics0078" / f"{name}.py"
                text = (HERE.parent / "synaptics0078" / f"{name}.py").read_text(encoding="utf-8")
                src.write_text(text + "\nWHO = 'pyc'\n", encoding="utf-8")
                cache = importlib.util.cache_from_source(str(src))
                py_compile.compile(str(src), cfile=cache, doraise=True)
                st = os.stat(src)
                src.write_text(text + "\nWHO = 'src'\n", encoding="utf-8")  # the same size
                os.utime(src, ns=(st.st_atime_ns, st.st_mtime_ns))
                caches.append(cache)
            # The plant works: an import loader runs it.
            pins_py = str(root / "synaptics0078" / "pins.py")
            loader = importlib.machinery.SourceFileLoader("planted_control", pins_py)
            control = importlib.util.module_from_spec(importlib.util.spec_from_loader("planted_control", loader))
            sys.modules["planted_control"] = control  # @dataclass looks its module up there
            try:
                loader.exec_module(control)
            finally:
                sys.modules.pop("planted_control", None)
            self.assertEqual(control.WHO, "pyc")
            # The real package is imported in this process already: the source mode must not reuse it.
            real = {k: v for k, v in sys.modules.items() if k == "synaptics0078" or k.startswith("synaptics0078.")}
            self.assertIn("synaptics0078.pins", real)
            self.assertIn("synaptics0078.calibration", real)
            shim = root / "proto" / "calibration.py"
            module = types.ModuleType("calibration_source_mode")  # no __spec__: the source mode
            module.__file__ = str(shim)
            exec(compile(shim.read_bytes(), str(shim), "exec", dont_inherit=True), module.__dict__)  # noqa: S102
            self.assertEqual(module.WHO, "src")  # calibration.py's names, from its source
            self.assertEqual(module.pins.WHO, "src")  # and the pins it imported
            self.assertEqual(module.pins.__file__, pins_py)
            self.assertIsNot(module.pins, real["synaptics0078.pins"])
            self.assertEqual(
                {k: v for k, v in sys.modules.items() if k == "synaptics0078" or k.startswith("synaptics0078.")},
                real,
                "sys.modules is left as it was",
            )
            self.assertIsNone(module.program_problem(synthetic_program(), SYNTH_TEMPLATE))
            for cache in caches:  # nothing read from or written to a cache
                self.assertEqual(
                    sorted(os.listdir(os.path.dirname(cache))), sorted(os.path.basename(c) for c in caches)
                )


if __name__ == "__main__":
    unittest.main()
