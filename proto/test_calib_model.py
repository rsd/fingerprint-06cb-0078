"""Offline tests of calib_model.py (no USB).

The pure checks need nothing. The program checks (Programs, SyntheticUnit) run on the synthetic
template and pins of synth_template.py, patched in per class (not for the module: SkeletonPins
checks the real pins), so they run the same on a clean clone. RealTemplate checks the real
template file (proto/data/cmd02_template.bin) against the real pins and is skipped without it.
The reproductions need this machine's per-unit files and are skipped without them (local-only);
SyntheticUnit runs the same code paths on the synthetic unit of synaptics0078/tests/fixtures.py:
  extracted/CalibrationData_decrypted.bin       Windows CalibrationData of our unit
  proto/logs/probe-20261013-204946-724232-p613500.log   the 890-byte 6f reply
    (fallback local/b2/ours_6f_reply.bin)
  research/calib/ours_cmd02.bin                  our identify program
No per-unit data is copied into the repo and no per-unit hash is pinned here.
"""

import struct
import unittest
from pathlib import Path

import calib_model as cm
import calibration
import parsers
import synth_template

from synaptics0078 import pins
from synaptics0078.tests import fixtures as fx

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent
REAL_CAL = ROOT / "extracted" / "CalibrationData_decrypted.bin"
PROBE_LOG = HERE / "logs" / "probe-20261013-204946-724232-p613500.log"
SAVED_6F = ROOT / "local" / "b2" / "ours_6f_reply.bin"
OURS_CMD02 = ROOT / "research" / "calib" / "ours_cmd02.bin"
HAVE_TEMPLATE = calibration.TEMPLATE_PATH.exists()

FACTORY = bytes(0x70 + (i % 32) for i in range(56))  # packs to exactly 5 bits
CF = bytes((0x80 + ((i * 37) % 61) - 30) & 0xFF for i in range(cm.FRAME_LEN))


def our_6f_reply():
    """The 890-byte 6f reply of our unit, or None."""
    if PROBE_LOG.exists():
        for line in PROBE_LOG.read_text().splitlines():
            if " RX " in f" {line} " and "EP 0x81 890" in line:
                return bytes.fromhex(line.split()[-1])
    if SAVED_6F.exists():
        return SAVED_6F.read_bytes()
    return None


REPLY_6F = our_6f_reply() if REAL_CAL.exists() else None


class Matrix(unittest.TestCase):
    def test_m_is_hadamard(self):
        for i in range(16):
            for j in range(16):
                dot = sum(cm.M[i][k] * cm.M[j][k] for k in range(16))
                self.assertEqual(dot, 16 if i == j else 0, (i, j))
        self.assertTrue(all(v in (1, -1) for row in cm.M for v in row))

    def test_decode_constant_is_identity(self):
        for c in (0, 77, 128, 255):
            self.assertEqual(cm.decode_line(bytes([c]) * 144), bytes([c]) * 144)

    def test_decode_known_vector(self):
        # x = 100 * e0 (times 4 to survive >> 2): y_i = clamp(100 * M[i][0]); column 0 of M by hand
        x = bytes([255]) + bytes(15)
        col0 = [1, -1, -1, -1, -1, 1, 1, 1, -1, 1, 1, 1, -1, 1, 1, 1]
        want = bytes(max(0, min(255, (255 * c) >> 2)) for c in col0)
        self.assertEqual(want[:2], bytes([63, 0]))  # -255 >> 2 = -64 clamps to 0
        self.assertEqual(cm.decode_line(x * 9), want * 9)

    def test_decode_clamps_high(self):
        # all 255 except x0 = 0: (1020 - 255 * M[i][0]) >> 2 = 191 or 318 -> 255
        x = bytes([0]) + bytes([255]) * 15
        want = bytes([191, 255, 255, 255, 255, 191, 191, 191, 255, 191, 191, 191, 255, 191, 191, 191])
        self.assertEqual(cm.decode_line(x * 9), want * 9)

    def test_decode_frame_keeps_headers(self):
        frame = bytearray(cm.FRAME_LEN)
        for r in range(cm.ROWS):
            frame[r * 152 : r * 152 + 8] = bytes([1, 0xFE, 2 * r + 1, 2, 0xF0, 0xF7, 1, 0])
            frame[r * 152 + 8 : (r + 1) * 152] = bytes([128]) * 144
        self.assertEqual(cm.decode_frame(bytes(frame)), bytes(frame))
        self.assertEqual(cm.header_rows_ok(bytes(frame)), 56)

    def test_decode_inverts_the_encoder(self):
        """x = M s + 128 decodes to 4 s + 128 exactly (M M = 16 I, M 1 = 4): no clamp for |s| <= 6."""
        s = [((i * 5) % 13) - 6 for i in range(144)]
        x = bytes(128 + sum(cm.M[k][j] * s[blk + j] for j in range(16)) for blk in range(0, 144, 16) for k in range(16))
        self.assertEqual(cm.decode_line(x), bytes(128 + 4 * v for v in s))

    def test_decode_rejects_bad_lengths(self):
        with self.assertRaises(calibration.CalibrationError):
            cm.decode_line(bytes(143))
        with self.assertRaises(calibration.CalibrationError):
            cm.decode_frame(bytes(8511))


class Averaging(unittest.TestCase):
    def test_floor_of_line_pairs_frame_2_only(self):
        raw = bytearray([0xEE]) * cm.RAW_LEN  # frames 1 and 3 and the extra line: ignored
        for r in range(cm.ROWS):
            a = 112 + 2 * r
            raw[a * 152 : (a + 1) * 152] = bytes([3]) * 152
            raw[(a + 1) * 152 : (a + 2) * 152] = bytes([4]) * 152  # odd sum 7 -> 3
        out = cm.average_frame2(bytes(raw))
        self.assertEqual(out, bytes([3]) * cm.FRAME_LEN)

    def test_extremes_and_order(self):
        raw = bytearray(cm.RAW_LEN)
        for r in range(cm.ROWS):
            a = 112 + 2 * r
            raw[a * 152 : (a + 1) * 152] = bytes([255]) * 152
            raw[(a + 1) * 152 : (a + 2) * 152] = bytes([r]) * 152
        out = cm.average_frame2(bytes(raw))
        for r in range(cm.ROWS):
            self.assertEqual(out[r * 152 : (r + 1) * 152], bytes([(255 + r) // 2]) * 152)

    def test_average_of_doubled_rows_is_the_frame(self):
        raw = bytes(112 * 152) + b"".join(CF[r * 152 : (r + 1) * 152] * 2 for r in range(56)) + bytes(152 * (337 - 224))
        self.assertEqual(len(raw), cm.RAW_LEN)
        self.assertEqual(cm.average_frame2(raw), CF)

    def test_short_raw_refused(self):
        with self.assertRaises(calibration.CalibrationError):
            cm.average_frame2(bytes(224 * 152 - 1))


class Rescale(unittest.TestCase):
    def test_q_truncates_toward_zero_and_clamps(self):
        self.assertEqual(cm.q(0x80), 0)
        self.assertEqual(cm.q(0x81), 2)  # 2.5 -> 2
        self.assertEqual(cm.q(0x7F), -2)  # -2.5 -> -2, not -3
        self.assertEqual(cm.q(0x7E), -5)
        self.assertEqual(cm.q(0x82), 5)  # no 5 -> 4 step outside the key line
        self.assertEqual(cm.q(0xFF), 127)  # 317.5 clamped
        self.assertEqual(cm.q(0x00), -128)  # -320 clamped
        self.assertEqual(cm.q(0xB3), 127)  # 51 * 2.5 = 127.5 -> 127
        self.assertEqual(cm.q(0x4D), -127)  # -51 * 2.5 = -127.5 -> -127

    def test_key_line_applies_5_to_4(self):
        cf = bytearray([0x82]) * cm.FRAME_LEN
        self.assertEqual(cm.key_line(bytes(cf)), bytes([4]) * 144)
        self.assertEqual(cm.line_update_data(bytes(cf)), bytes([5]) * cm.LINE_UPDATE_LEN)


class Score(unittest.TestCase):
    def ref_from(self, cf, shift=0):
        vals = bytearray(b"\x07\x00\x00\x00")
        for r in cm.CHECK_ROWS:
            for c in range(144):
                vals.append((cm.q(cf[r * 152 + 8 + c]) + shift) & 0xFF)
        return bytes(vals)

    def test_perfect_and_shifted(self):
        self.assertEqual(cm.check_score(CF, self.ref_from(CF)), 0)
        # every one of 720 samples off by 10: 72000 // 4096 = 17
        self.assertEqual(cm.check_score(CF, self.ref_from(CF, 10)), 72000 // 4096)
        self.assertGreater(cm.check_score(CF, self.ref_from(CF, 40)), cm.CHECK_MAX)

    def test_other_rows_fail(self):
        """Control rows moved by one no longer match the reference (as for the real unit)."""
        ref = self.ref_from(CF)
        self.assertGreater(cm.check_score(CF, ref, tuple(r + 1 for r in cm.CHECK_ROWS)), cm.CHECK_MAX)

    def test_signed_reference(self):
        flat = bytes([0x80]) * cm.FRAME_LEN  # q = 0 everywhere
        ref = b"\x00" * 4 + bytes([0xFF]) * 720  # int8 -1
        self.assertEqual(cm.check_score(flat, ref), 720 // 4096)
        ref = b"\x00" * 4 + bytes([0x80]) * 720  # int8 -128
        self.assertEqual(cm.check_score(flat, ref), 720 * 128 * 128 // 4096)

    def test_bad_sizes(self):
        with self.assertRaises(calibration.CalibrationError):
            cm.check_score(CF, bytes(723))
        with self.assertRaises(calibration.CalibrationError):
            cm.check_score(CF[:-1], bytes(724))


class Factory(unittest.TestCase):
    def test_entries_layout(self):
        e = cm.factory_entries(FACTORY)
        self.assertEqual(len(e), cm.FACTORY_ENTRIES_LEN)
        self.assertEqual(e[:4], bytes([0x0C, 0x70, 0, 0]))
        self.assertEqual(e[:39], e[39:])
        stream = int.from_bytes(e[4:39], "little")
        self.assertEqual(bytes(0x70 + ((stream >> (5 * i)) & 31) for i in range(56)), FACTORY)

    def test_not_five_bits_refused(self):
        for vals in (bytes([0x80]) * 56, bytes(0x70 + (i % 16) for i in range(56)), bytes(0x70 + i for i in range(56))):
            with self.assertRaises(calibration.CalibrationError):
                cm.factory_entries(vals)
        with self.assertRaises(calibration.CalibrationError):
            cm.factory_entries(FACTORY[:55])

    def test_subtags(self):
        good = {3: b"\x07\x00\x00\x00" + FACTORY, 12: bytes(724)}
        self.assertEqual(cm.factory_values(good), FACTORY)
        self.assertEqual(cm.check_reference(good), bytes(724))
        for bad in ({12: bytes(724)}, {3: bytes(59), 12: bytes(724)}):
            with self.assertRaises(calibration.CalibrationError):
                cm.factory_values(bad)
        for bad in ({3: bytes(60)}, {3: bytes(60), 12: bytes(720)}):
            with self.assertRaises(calibration.CalibrationError):
                cm.check_reference(bad)


class Compare(unittest.TestCase):
    def test_metrics(self):
        c = cm.compare([0, 10, 20, 30], [1, 12, 20, 50], tolerance=2)
        self.assertEqual(c.n, 4)
        self.assertAlmostEqual(c.mean_abs, (1 + 2 + 0 + 20) / 4)
        self.assertEqual(c.max_abs, 20)
        self.assertAlmostEqual(c.within, 0.75)
        self.assertGreater(c.correlation, 0.9)

    def test_constant_side_has_no_correlation(self):
        c = cm.compare([5, 5, 5], [1, 2, 3], 1)
        self.assertIsNone(c.correlation)
        self.assertFalse(cm.cf_passes(c))

    def test_unequal_lengths(self):
        with self.assertRaises(ValueError):
            cm.compare([1], [1, 2], 1)

    def test_cf_gate_and_row_slip(self):
        self.assertTrue(cm.cf_passes(cm.compare_cf(CF, CF)))
        slipped = CF[152:] + CF[:152]  # every row moved by one
        self.assertFalse(cm.cf_passes(cm.compare_cf(slipped, CF)))

    def test_bf_gate_ignores_correlation(self):
        flat = bytearray([128]) * cm.FRAME_LEN
        noisy = bytearray(flat)
        for i in range(8, cm.FRAME_LEN, 7):
            noisy[i] = 129
        b = cm.compare_bf(bytes(noisy), bytes(flat))
        self.assertIsNone(b.correlation)
        self.assertTrue(cm.bf_passes(b))
        self.assertFalse(cm.bf_passes(cm.compare_bf(bytes([140]) * cm.FRAME_LEN, bytes(flat))))

    def test_bf_sanity(self):
        self.assertTrue(cm.bf_sanity(bytes([128]) * cm.FRAME_LEN)[0])
        self.assertFalse(cm.bf_sanity(bytes([133]) * cm.FRAME_LEN)[0])
        wild = bytes((0 if (i // 3) % 2 else 255) for i in range(cm.FRAME_LEN))
        self.assertFalse(cm.bf_sanity(wild)[0])

    def test_factory_matches(self):
        self.assertEqual(cm.factory_matches(FACTORY, FACTORY), 56)
        self.assertEqual(cm.factory_matches(FACTORY, bytes([0]) + FACTORY[1:]), 55)
        self.assertEqual(cm.factory_matches(FACTORY, FACTORY[:-1]), 0)


class SkeletonPins(unittest.TestCase):
    """Runs without the template: calib_model's skeleton pins are the package's pins."""

    def test_skeleton_pins_are_the_package_pins(self):
        dp = pins.DEFAULT_PINS
        self.assertEqual((cm.PROGRAM1_LEN, cm.PROGRAM2_LEN), (dp.program1_len, dp.program2_len))
        self.assertEqual(cm.PROGRAM1_SKELETON_SHA256, dp.program1_skeleton_sha256)
        self.assertEqual(cm.PROGRAM2_SKELETON_SHA256, dp.program2_skeleton_sha256)
        self.assertEqual(cm.SKELETON_SHA256, dp.skeletons)


def check_sizes_and_pinned_skeletons(case, t):
    """Both programs built on template `t` have the pinned sizes and skeletons (cm's pins, real or patched)."""
    p1 = cm.build_program1(t, FACTORY)
    p2 = cm.build_program2(t, FACTORY, CF)
    case.assertEqual((len(p1), len(p2)), (cm.PROGRAM1_LEN, cm.PROGRAM2_LEN))
    case.assertEqual(cm.skeleton_sha256(p1), cm.PROGRAM1_SKELETON_SHA256)
    case.assertEqual(cm.skeleton_sha256(p2), cm.PROGRAM2_SKELETON_SHA256)
    # the skeleton does not depend on the per-unit inputs
    other = bytes(0x60 + (31 - i % 32) for i in range(56))
    case.assertEqual(cm.skeleton_sha256(cm.build_program1(t, other)), cm.PROGRAM1_SKELETON_SHA256)
    case.assertEqual(cm.skeleton_sha256(cm.build_program2(t, other, bytes(cm.FRAME_LEN))), cm.PROGRAM2_SKELETON_SHA256)


@unittest.skipUnless(
    HAVE_TEMPLATE, "proto/data/cmd02_template.bin not present (the real template, gitignored, local-only)"
)
class RealTemplate(unittest.TestCase):
    """The real template file and the real pins (no patch): they must agree."""

    def test_sizes_and_pinned_skeletons(self):
        check_sizes_and_pinned_skeletons(self, calibration.load_template())


class SyntheticPatches(unittest.TestCase):
    """Base: the synthetic template and its pins for this class only (synth_template.patches)."""

    @classmethod
    def setUpClass(cls):
        cls._patches = synth_template.patches()
        cls.addClassCleanup(cls._patches.close)  # also when a later setUpClass step fails


class Programs(SyntheticPatches):
    def setUp(self):
        self.t = synth_template.SYNTH_TEMPLATE

    def test_sizes_and_pinned_skeletons(self):
        check_sizes_and_pinned_skeletons(self, self.t)

    def test_shape(self):
        for prog, cols in ((cm.build_program1(self.t, FACTORY), 0), (cm.build_program2(self.t, FACTORY, CF), 36)):
            self.assertEqual(prog[:5], bytes.fromhex("0298005101"))
            _, chunks = calibration.split_chunks(prog)
            types = [t for t, _ in chunks]
            self.assertEqual(
                types, [0x23, 0x20, 0x32, 0x33, 0x32, 0x33, 0x32, 0x34, 0x42, 0x2F, 0x29, 0x35, 0x17, 0x44, 0x30, 0x43]
            )
            self.assertFalse({0x26, 0x2E, 0x47, 0x48} & set(types))
            d = cm.program_chunks(prog)
            (n,) = struct.unpack_from("<L", d[0x30], 0)
            self.assertEqual(n, 6 + cols)
            self.assertEqual(len(d[0x30]), 4 + 8 * n + (cm.LINE_UPDATE_LEN if cols else 0))
            self.assertEqual(d[0x43][60:138], cm.factory_entries(FACTORY))
        p1 = cm.program_chunks(cm.build_program1(self.t, FACTORY))
        self.assertEqual(p1[0x34][:144], bytes(144))

    def test_skeleton_detects_fixed_byte_changes_only(self):
        p2 = bytearray(cm.build_program2(self.t, FACTORY, CF))
        base = cm.skeleton_sha256(bytes(p2))
        self.assertEqual(base, cm.PROGRAM2_SKELETON_SHA256)
        flipped = bytearray(p2)
        flipped[20] ^= 1  # inside chunk 0x20/0x32, fixed
        self.assertNotEqual(cm.skeleton_sha256(bytes(flipped)), base)
        hdr = bytearray(p2)
        hdr[3] = 0  # req_lines
        self.assertNotEqual(cm.skeleton_sha256(bytes(hdr)), base)

    def chunk_at(self, prog, typ):
        """Offset in `prog` of chunk `typ`'s data."""
        _, chunks = calibration.split_chunks(prog)
        off = 5
        for t, data in chunks:
            if t == typ:
                return off + 4
            off += 4 + len(data)
        self.fail(f"no chunk 0x{typ:x}")

    def programs(self):
        return {"#1": cm.build_program1(self.t, FACTORY), "#2": cm.build_program2(self.t, FACTORY, CF)}

    def test_program1_key_line_is_pinned(self):
        p1 = cm.build_program1(self.t, FACTORY)
        at = self.chunk_at(p1, 0x34)
        for i in (0, 77, 143):
            bad = bytearray(p1)
            bad[at + i] = 0x04
            self.assertNotEqual(cm.skeleton_sha256(bytes(bad)), cm.PROGRAM1_SKELETON_SHA256, i)
        # program #2's key line is per unit
        p2 = bytearray(cm.build_program2(self.t, FACTORY, CF))
        p2[self.chunk_at(bytes(p2), 0x34)] ^= 0x01
        self.assertEqual(cm.skeleton_sha256(bytes(p2)), cm.PROGRAM2_SKELETON_SHA256)

    def test_factory_entry_format_and_reserved_are_pinned(self):
        e2 = cm.FACTORY_ENTRY_OFFSET + cm.FACTORY_ENTRY_LEN  # 99
        for name, prog in self.programs().items():
            pinned = cm.SKELETON_SHA256[len(prog)]
            at = self.chunk_at(prog, 0x43)
            self.assertEqual(prog[at + 60], 0x0C, name)
            self.assertEqual(prog[at + e2], 0x0C, name)
            for rel, value, what in ((0, 0x0D, "format 0x0d"), (2, 0x01, "reserved low"), (3, 0x80, "reserved high")):
                bad = bytearray(prog)
                bad[at + 60 + rel] = value
                bad[at + e2 + rel] = value  # both copies, so they stay equal
                self.assertNotEqual(cm.skeleton_sha256(bytes(bad)), pinned, f"{name} {what}")

    def test_factory_entry_copies_must_be_equal(self):
        e2 = cm.FACTORY_ENTRY_OFFSET + cm.FACTORY_ENTRY_LEN
        for name, prog in self.programs().items():
            at = self.chunk_at(prog, 0x43)
            for rel in (1, 4, 38):  # minimum, first and last stream byte of copy 2 only
                bad = bytearray(prog)
                bad[at + e2 + rel] ^= 0x01
                with self.assertRaises(calibration.CalibrationError, msg=f"{name} +{rel}"):
                    cm.skeleton(bytes(bad))
            # the same per-unit change in both copies is allowed
            ok = bytearray(prog)
            for e in (60, e2):
                ok[at + e + 1] ^= 0x01
                ok[at + e + 10] ^= 0x10
            self.assertEqual(cm.skeleton_sha256(bytes(ok)), cm.SKELETON_SHA256[len(prog)], name)

    def test_unpackable_factory_refused(self):
        with self.assertRaises(calibration.CalibrationError):
            cm.build_program1(self.t, bytes([0x80]) * 56)


class SyntheticUnit(SyntheticPatches):
    """OurUnit's reproductions on the synthetic unit of fixtures.py: the same code paths, no unit data.

    The unit's 6f reply, CalibrationData and identify program are fx.factory_reply(), the inputs
    (fx.FACTORY, fx.CF, fx.BF) and calibration.build_cmd02 on the synthetic template.
    """

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.inp = calibration.CalibrationInputs(fx.FACTORY, fx.CF, fx.BF)
        cls.by_subtag = parsers.parse_factory_bits(fx.factory_reply()).values["by_subtag"]

    def sub12(self):
        return cm.check_reference(self.by_subtag)

    def test_check_score_of_the_6f_reply(self):  # OurUnit.test_check_score_is_windows_7
        self.assertEqual(cm.check_score(self.inp.frame, self.sub12()), 0)

    def test_rows_plus_one_control_fails(self):  # OurUnit.test_rows_plus_one_control_fails
        rows = tuple(r + 1 for r in cm.CHECK_ROWS)
        self.assertGreater(cm.check_score(self.inp.frame, self.sub12(), rows), cm.CHECK_MAX)

    def test_live_factory_values_match_stored(self):  # OurUnit.test_live_factory_values_match_stored
        self.assertEqual(cm.factory_matches(cm.factory_values(self.by_subtag), self.inp.factory), 56)

    def test_average_of_raw_capture_is_cf(self):  # OurUnit.test_average_of_synthetic_raw_is_cf
        self.assertEqual(cm.average_frame2(fx.raw_capture(fx.CF_PIX)), self.inp.frame)

    def test_frames_pass_their_own_gates(self):  # OurUnit.test_windows_frames_pass_their_own_gates
        self.assertTrue(cm.bf_sanity(self.inp.baseline)[0])
        self.assertTrue(cm.cf_passes(cm.compare_cf(self.inp.frame, self.inp.frame)))

    def test_program2_chunks_equal_identify_builder(self):  # OurUnit.test_program2_chunks_equal_identify_builder
        t = calibration.load_template()
        self.assertEqual(t, synth_template.SYNTH_TEMPLATE)
        p2 = cm.program_chunks(cm.build_program2(t, self.inp.factory, self.inp.frame))
        idf = cm.program_chunks(calibration.build_cmd02(self.inp, t))
        self.assertEqual(p2[0x34], idf[0x34])
        self.assertEqual(p2[0x30], idf[0x30])
        self.assertEqual(cm.skeleton_sha256(cm.build_program1(t, self.inp.factory)), cm.PROGRAM1_SKELETON_SHA256)

    def test_derived_chunks_equal_the_identify_program(self):  # OurUnit.test_derived_chunks_equal_our_identify_program
        ours = cm.program_chunks(calibration.build_cmd02(self.inp, synth_template.SYNTH_TEMPLATE))
        ch = cm.derive_chunks(self.inp.factory, self.inp.frame, self.inp.baseline)
        self.assertEqual(ch.factory_entries, ours[0x43][60:138])
        self.assertEqual(ch.baseline, ours[0x48])
        self.assertEqual(ch.key_line, ours[0x34][:144])
        (n,) = struct.unpack_from("<L", ours[0x30], 0)
        self.assertEqual(ch.line_update, ours[0x30][4 + 8 * n :])

    # OurUnit.test_extra_line_decodes_to_bf_row_28: the stored extra line of the real blob is checked
    # by decode_line alone, which Matrix.test_decode_inverts_the_encoder covers (an encoded row decodes back).


@unittest.skipUnless(
    REAL_CAL.exists(), "extracted/CalibrationData_decrypted.bin not present (this unit's data, local-only)"
)
class OurUnit(unittest.TestCase):
    """Reproductions on this machine's data (no per-unit bytes or hashes are kept in the repo)."""

    @classmethod
    def setUpClass(cls):
        cls.blob = REAL_CAL.read_bytes()
        cls.inp = calibration.parse_calibration_data(cls.blob)

    def sub12(self):
        if REPLY_6F is None:
            self.skipTest("no 6f reply (probe log or local/b2/ours_6f_reply.bin; this unit's data, local-only)")
        self.assertEqual(len(REPLY_6F), 890)
        return cm.check_reference(parsers.parse_factory_bits(REPLY_6F).values["by_subtag"])

    def test_check_score_is_windows_7(self):
        self.assertEqual(cm.check_score(self.inp.frame, self.sub12()), 7)

    def test_rows_plus_one_control_fails(self):
        rows = tuple(r + 1 for r in cm.CHECK_ROWS)
        self.assertEqual(cm.check_score(self.inp.frame, self.sub12(), rows), 223)

    def test_live_factory_values_match_stored(self):
        self.sub12()
        by = parsers.parse_factory_bits(REPLY_6F).values["by_subtag"]
        self.assertEqual(cm.factory_matches(cm.factory_values(by), self.inp.factory), 56)

    def test_extra_line_decodes_to_bf_row_28(self):
        extra = self.blob[calibration.BASELINE_OFFSET + cm.FRAME_LEN : calibration.BASELINE_OFFSET + cm.FRAME_LEN + 152]
        got = cm.decode_line(extra[8:])
        want = self.inp.baseline[28 * 152 + 8 : 29 * 152]
        self.assertEqual(sum(1 for a, b in zip(got, want) if a == b), 144)

    def test_average_of_synthetic_raw_is_cf(self):
        cf = self.inp.frame
        raw = bytes(112 * 152) + b"".join(cf[r * 152 : (r + 1) * 152] * 2 for r in range(56)) + bytes(152 * (337 - 224))
        self.assertEqual(len(raw), cm.RAW_LEN)
        self.assertEqual(cm.average_frame2(raw), cf)

    def test_windows_frames_pass_their_own_gates(self):
        self.assertTrue(cm.bf_sanity(self.inp.baseline)[0])
        self.assertTrue(cm.cf_passes(cm.compare_cf(self.inp.frame, self.inp.frame)))

    @unittest.skipUnless(HAVE_TEMPLATE, "proto/data/cmd02_template.bin not present (the real template, local-only)")
    def test_program2_chunks_equal_identify_builder(self):
        t = calibration.load_template()
        p2 = cm.program_chunks(cm.build_program2(t, self.inp.factory, self.inp.frame))
        idf = cm.program_chunks(calibration.build_cmd02(self.inp, t))
        self.assertEqual(p2[0x34], idf[0x34])
        self.assertEqual(p2[0x30], idf[0x30])
        self.assertEqual(cm.skeleton_sha256(cm.build_program1(t, self.inp.factory)), cm.PROGRAM1_SKELETON_SHA256)

    @unittest.skipUnless(
        OURS_CMD02.exists(), "research/calib/ours_cmd02.bin not present (this unit's program, local-only)"
    )
    def test_derived_chunks_equal_our_identify_program(self):
        ours = cm.program_chunks(OURS_CMD02.read_bytes())
        ch = cm.derive_chunks(self.inp.factory, self.inp.frame, self.inp.baseline)
        self.assertEqual(ch.factory_entries, ours[0x43][60:138])
        self.assertEqual(ch.baseline, ours[0x48])
        self.assertEqual(ch.key_line, ours[0x34][:144])
        (n,) = struct.unpack_from("<L", ours[0x30], 0)
        self.assertEqual(ch.line_update, ours[0x30][4 + 8 * n :])


if __name__ == "__main__":
    unittest.main()
