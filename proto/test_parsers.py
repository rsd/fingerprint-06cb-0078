"""Offline tests: run the parsers on the real replies recorded in the references.

No USB, no device. Run with:  .venv/bin/python -m pytest -v    (or: python3 -m unittest -v)

Recorded replies (copied by hand from the issue bodies, hex as printed there):
  Validity90 issue #63, reporter cydia2020, a REAL 06cb:0078, prototype v15 log:
    step 1  write 01  -> 38 bytes
    step 2  write 19  -> 68 bytes
    step 8  write 75  -> 10 bytes
  Validity90 issue #69, reporter junocomp, a 06cb:00a8 (sibling, used only for
  comparison): step 1 and step 2 replies.
In both, the unit serial (RomInfo bytes 0x12..0x17) and the unit-specific 0x19 words
(at 0x14 and 0x1c) are replaced with synthetic bytes; every other byte is as printed.
"""

import io
import unittest
from contextlib import redirect_stdout

import parsers
import probe


def b(rows: str) -> bytes:
    return bytes.fromhex(rows.replace("\n", " "))


# ---- Validity90 #63 (06cb:0078) ----
# Serial (0x12..0x17) and the 0x19 words at 0x14/0x1c replaced with synthetic bytes.
V63_ROM_INFO = b("""00 00 10 b6 04 54 9a 00  00 00 06 14 01 33 00 01
       00 00 00 63 0a 00 63 0b  00 00 00 00 00 00 01 00
       00 00 00 00 00 00""")
V63_QUERY_19 = b("""00 00 00 03 01 02 00 01  00 00 00 00 00 00 00 00
       00 00 00 00 63 19 00 01  00 00 00 00 63 19 00 02
       00 00 00 00 00 00 00 00  00 00 00 00 00 00 00 00
       00 00 00 00 00 00 00 00  00 00 00 00 00 00 00 00
       00 00 00 00""")
V63_SENSOR_ID = b("00 00 00 00 00 00 37 00  b7 00")
# Rejection replies the same 0078 gave to other (never sent by us) commands.
V63_STATUS_ONLY = [b("be 04"), b("01 04"), b("91 04")]

# ---- Validity90 #69 (06cb:00a8, comparison only) ----
# Serial (0x12..0x17) and the 0x19 words at 0x14/0x1c replaced with synthetic bytes.
V69_ROM_INFO = b("""00 00 10 b6 04 54 9a 00  00 00 06 14 01 33 00 01
       00 00 00 69 0a 00 69 0b  00 21 00 00 00 00 01 00
       00 00 00 00 00 06""")
V69_QUERY_19 = b("""00 00 00 03 01 02 00 01  00 00 00 00 00 00 00 00
       00 00 00 00 69 19 00 01  00 00 00 00 69 19 00 02
       00 00 00 00 00 00 00 00  00 00 00 00 00 00 00 00
       00 00 00 00 00 00 00 00  00 00 00 00 00 00 00 00
       00 00 00 00""")


class RecordedReplyLengths(unittest.TestCase):
    def test_lengths_match_expected(self):
        self.assertEqual(len(V63_ROM_INFO), parsers.EXPECTED_LEN[0x01])
        self.assertEqual(len(V63_QUERY_19), parsers.EXPECTED_LEN[0x19])
        self.assertEqual(len(V63_SENSOR_ID), parsers.EXPECTED_LEN[0x75])


class Status(unittest.TestCase):
    def test_ok(self):
        self.assertEqual(parsers.status_word(V63_ROM_INFO), 0)

    def test_error_codes_are_little_endian(self):
        self.assertEqual([parsers.status_word(r) for r in V63_STATUS_ONLY], [0x04BE, 0x0401, 0x0491])

    def test_too_short(self):
        self.assertIsNone(parsers.status_word(b""))
        self.assertIsNone(parsers.status_word(b"\x00"))

    def test_error_reply_is_not_decoded_further(self):
        for cmd, parse in parsers.PARSERS.items():
            d = parse(b("be 04"))
            self.assertEqual(d.values["status"], 0x04BE, hex(cmd))
            self.assertNotIn("major", d.values)


class SensorId0x75(unittest.TestCase):
    def test_v63_decodes_to_2449(self):
        d = parsers.parse_sensor_id(V63_SENSOR_ID)
        self.assertEqual(d.values["status"], 0)
        self.assertEqual(d.values["zeroes"], 0)
        self.assertEqual(d.values["major"], 0x00B7)
        self.assertEqual(d.values["minor"], 0x0037)
        self.assertEqual(d.values["sensor_type"], 0x2449)
        self.assertEqual(d.values["sensor_name"], "SYN 86C  TM03267001")
        self.assertEqual(d.warnings, [])

    def test_unknown_pair_is_not_guessed(self):
        d = parsers.parse_sensor_id(b("00 00 00 00 00 00 99 00  b7 00"))
        self.assertIsNone(d.values["sensor_type"])
        self.assertTrue(any(f.certainty == parsers.UNCERTAIN and f.name == "sensor type" for f in d.fields))

    def test_wrong_type_warns(self):
        # A table row that resolves to something other than 0x2449 would warn;
        # emulate by temporarily extending the subset.
        parsers.DEVICE_INFO_SUBSET[(0x1234, 0x01)] = (0x0121, "test")
        try:
            d = parsers.parse_sensor_id(b("00 00 00 00 00 00 01 00  34 12"))
        finally:
            del parsers.DEVICE_INFO_SUBSET[(0x1234, 0x01)]
        self.assertTrue(any("expected 0x2449" in w for w in d.warnings))

    def test_nonzero_reserved_warns(self):
        d = parsers.parse_sensor_id(b("00 00 01 00 00 00 37 00  b7 00"))
        self.assertTrue(any("non-zero" in w for w in d.warnings))

    def test_short_reply(self):
        d = parsers.parse_sensor_id(b("00 00 00 00"))
        self.assertNotIn("major", d.values)
        self.assertTrue(d.warnings)


class RomInfo0x01(unittest.TestCase):
    def test_v63(self):
        d = parsers.parse_rom_info(V63_ROM_INFO)
        v = d.values
        self.assertEqual(v["status"], 0)
        self.assertEqual((v["major"], v["minor"]), (6, 0x14))
        self.assertEqual(v["product"], 0x33)
        self.assertEqual(v["timestamp"], 0x5404B610)
        self.assertEqual(v["build"], 154)
        self.assertEqual(v["u1"], 0)
        self.assertEqual(v["byte_0x19"], 0x00)
        self.assertEqual(v["last_byte"], 0x00)
        self.assertEqual(d.warnings, [])

    def test_v69_sibling_differs_where_expected(self):
        d = parsers.parse_rom_info(V69_ROM_INFO)
        self.assertEqual((d.values["major"], d.values["minor"]), (6, 0x14))
        self.assertEqual(d.values["byte_0x19"], 0x21)
        self.assertEqual(d.values["last_byte"], 0x06)

    def test_uncertain_fields_are_flagged(self):
        d = parsers.parse_rom_info(V63_ROM_INFO)
        by_name = {f.name: f.certainty for f in d.fields}
        self.assertEqual(by_name["ROM version (major.minor)"], parsers.CERTAIN)
        self.assertEqual(by_name["timestamp (u32le @2)"], parsers.UNCERTAIN)
        self.assertEqual(by_name["byte 0x19"], parsers.UNCERTAIN)
        self.assertEqual(by_name["last byte"], parsers.UNCERTAIN)

    def test_short_reply(self):
        d = parsers.parse_rom_info(V63_ROM_INFO[:10])
        self.assertNotIn("major", d.values)
        self.assertTrue(d.warnings)


class Query0x19(unittest.TestCase):
    def test_v63(self):
        d = parsers.parse_query_19(V63_QUERY_19)
        self.assertEqual(d.values["status"], 0)
        self.assertEqual(len(d.values["body"]), 66)
        self.assertEqual(d.values["word_0x14"], 0x01001963)
        self.assertEqual(d.values["word_0x1c"], 0x02001963)
        self.assertEqual(d.warnings, [])
        # Nothing but the status is claimed as certain.
        self.assertEqual([f.name for f in d.fields if f.certainty == parsers.CERTAIN], ["status"])

    def test_nonzero_runs(self):
        runs = parsers.nonzero_runs(V63_QUERY_19[2:], base=2)
        self.assertEqual(
            runs,
            [
                (3, b("03 01 02")),
                (7, b("01")),
                # The synthetic words hold a zero byte, so each splits into two runs.
                (0x14, b("63 19")),
                (0x17, b("01")),
                (0x1C, b("63 19")),
                (0x1F, b("02")),
            ],
        )

    def test_v69_words_differ(self):
        d = parsers.parse_query_19(V69_QUERY_19)
        self.assertEqual(d.values["word_0x14"], 0x01001969)
        self.assertEqual(d.values["word_0x1c"], 0x02001969)


class Rendering(unittest.TestCase):
    def test_render_flags_uncertain_loudly(self):
        text = "\n".join(parsers.parse_rom_info(V63_ROM_INFO).render())
        self.assertIn("[UNCERTAIN]", text)
        self.assertIn("[CERTAIN]", text)

    def test_hexdump(self):
        self.assertEqual(parsers.hexdump(b("01 02")), "  0000  01 02")
        self.assertEqual(parsers.hexdump(b""), "  (empty)")


class ProbeSafety(unittest.TestCase):
    """The probe may only ever write 01, 19 and 75."""

    def test_request_set_is_closed(self):
        self.assertEqual([r for _, r, _ in probe.REQUESTS], [b"\x01", b"\x19", b"\x75"])
        self.assertEqual(probe.EP_OUT, 0x01)
        self.assertEqual(probe.EP_IN, 0x81)

    def test_only_rejects_other_commands(self):
        with self.assertRaises(SystemExit):
            probe.selected_requests("01,06")
        self.assertEqual([c for c, _, _ in probe.selected_requests("75,01")], [0x01, 0x75])

    def test_dry_run_needs_no_usb(self):
        buf = io.StringIO()
        with redirect_stdout(buf):
            rc = probe.main(["--dry-run"])
        self.assertEqual(rc, 0)
        out = buf.getvalue()
        self.assertIn("0x01", out)
        self.assertIn("0x19", out)
        self.assertIn("0x75", out)


if __name__ == "__main__":
    unittest.main()
