"""Offline tests of the parsers added for probe --only 6f and for capture.py:
factory bits (0x6f), the factory-value comparison, EP 0x83 events, the 0x51
image reply and the PGM writer.

No real 0x6f reply from a 0078 exists in any reference, so the 0x6f replies here
are synthetic, built to the layout of python-validity sensor.py:128-151.
"""

import struct
import unittest

import parsers
from test_parsers import V63_ROM_INFO

STORED = bytes(0x76 + (i % 17) for i in range(56))


def entry(subtag, value, ptr=0, tag=0x0E, flags=0x8000):
    return struct.pack("<LHHHH", ptr, len(value), tag, subtag, flags) + value


def factory_reply(entries, status=0, wtf=0xD88):
    return struct.pack("<HLL", status, wtf, len(entries)) + b"".join(entries)


class FactoryRequest(unittest.TestCase):
    def test_request_bytes(self):
        self.assertEqual(parsers.REQ_FACTORY_BITS, bytes.fromhex("6f000e000000000000"))
        self.assertEqual(len(parsers.REQ_FACTORY_BITS), 9)
        self.assertEqual(parsers.REQ_FACTORY_BITS, struct.pack("<BHHL", 0x6F, 0x0E00, 0, 0))


class FactoryBits(unittest.TestCase):
    def test_parse_two_entries(self):
        reply = factory_reply([entry(3, bytes.fromhex("07000000") + STORED), entry(2, bytes.fromhex("0d007100"))])
        d = parsers.parse_factory_bits(reply)
        self.assertEqual(d.values["status"], 0)
        self.assertEqual(sorted(d.values["by_subtag"]), [2, 3])
        self.assertEqual(d.values["by_subtag"][3][4:], STORED)
        self.assertEqual(d.warnings, [])

    def test_nonzero_status(self):
        d = parsers.parse_factory_bits(b"\xbe\x04")
        self.assertEqual(d.values["status"], 0x04BE)
        self.assertNotIn("entries", d.values)

    def test_truncated_and_trailing_do_not_raise(self):
        reply = factory_reply([entry(3, bytes(60))])
        d = parsers.parse_factory_bits(reply[:-5])
        self.assertTrue(any("truncated" in w for w in d.warnings))
        d = parsers.parse_factory_bits(reply + b"\x00\x00")
        self.assertTrue(any("after the last entry" in w for w in d.warnings))
        d = parsers.parse_factory_bits(b"\x00\x00\x01")
        self.assertTrue(d.warnings)

    def test_compare_match(self):
        r = parsers.compare_factory_values({3: bytes.fromhex("07000000") + STORED}, STORED)
        self.assertIs(r["match"], True)
        self.assertTrue(r["verdict"].startswith("MATCH"))

    def test_compare_differences_listed(self):
        live = bytearray(STORED)
        live[5] += 1
        live[40] -= 2
        r = parsers.compare_factory_values({3: b"\x07\x00\x00\x00" + bytes(live)}, STORED)
        self.assertIs(r["match"], False)
        self.assertIn("2 of 56", r["verdict"])
        text = "\n".join(r["lines"])
        self.assertIn("value  5", text)
        self.assertIn("value 40", text)

    def test_compare_length_mismatch_and_missing_subtag(self):
        r = parsers.compare_factory_values({3: b"\x07\x00\x00\x00" + STORED[:55]}, STORED)
        self.assertIs(r["match"], False)
        r = parsers.compare_factory_values({7: b"xx" + STORED}, STORED)
        self.assertIsNone(r["match"])
        self.assertIn("subtag 7 at offset 2", "\n".join(r["lines"]))


class RomSerial(unittest.TestCase):
    def test_serial_taken_from_bytes_0x12_to_0x18(self):
        d = parsers.parse_rom_info(V63_ROM_INFO)
        # V63_ROM_INFO carries the synthetic serial that replaces #63's real one.
        self.assertEqual(d.values["serial"].hex(), "00630a00630b")


# The two real 01 replies logged on the unit, before and after the 06 module upload
# (proto/logs/capture-20261013-204956-*.log and capture-20261013-205005-*.log),
# with the serial bytes 0x12..0x17 replaced by a synthetic one.
ROM_BEFORE = bytes.fromhex("000010b604549a0000000614013300010000facade0000780000000000000100000000000000")
ROM_AFTER = bytes.fromhex("000010b604549a000000061401330001000efacade0000780000000002010100000000000000")


class RomInfoModuleState(unittest.TestCase):
    def test_before_upload(self):
        d = parsers.parse_rom_info(ROM_BEFORE)
        self.assertEqual(d.values["u1"], 0)
        self.assertIs(d.values["module_probably_loaded"], False)
        self.assertEqual(d.values["word_0x1c"], 0)
        self.assertEqual(d.values["serial"].hex(), "facade000078")

    def test_after_upload(self):
        d = parsers.parse_rom_info(ROM_AFTER)
        self.assertEqual(d.values["u1"], 0x0E)
        self.assertIs(d.values["module_probably_loaded"], True)
        self.assertEqual(d.values["word_0x1c"], 0x0102)
        self.assertEqual((d.values["major"], d.values["minor"], d.values["build"]), (6, 0x14, 0x9A))

    def test_decoded_fields_are_tagged_uncertain_and_cite_their_evidence(self):
        d = parsers.parse_rom_info(ROM_AFTER)
        by_name = {f.name: f for f in d.fields}
        u1 = by_name["u1 (byte 17) = third version component"]
        self.assertEqual(u1.certainty, parsers.UNCERTAIN)
        self.assertIn("06.20.14.0154", u1.value)
        for needle in ("0x18005f960", "0x04aa", "Hardware runs"):
            self.assertIn(needle, u1.note)
        w = by_name["u16le @0x1c"]
        self.assertEqual(w.certainty, parsers.UNCERTAIN)
        self.assertEqual(w.value, "0x0102")
        # the changed bytes are decoded once, not repeated under "other non-zero bytes"
        rest = [f for f in d.fields if f.name == "other non-zero bytes"]
        self.assertTrue(all("[0x1c]" not in f.value and "[0x1d]" not in f.value for f in rest))

    def test_helper(self):
        self.assertEqual(parsers.module_probably_loaded(0)[0], False)
        self.assertEqual(parsers.module_probably_loaded(0x0E)[0], True)
        self.assertIn("inference", parsers.module_probably_loaded(1)[1])


class Interrupts(unittest.TestCase):
    def test_actions(self):
        c = parsers.classify_interrupt
        self.assertEqual(c(bytes.fromhex("0000000000")).action, "ignore")  # start ack
        self.assertEqual(c(bytes.fromhex("0200401000")).action, "finger")  # iusearch main.py:63
        self.assertEqual(c(bytes.fromhex("0340010000")).action, "ignore")  # position ok
        self.assertEqual(c(bytes.fromhex("0342040040")).action, "ready")  # code 4
        self.assertEqual(c(bytes.fromhex("0343040041")).action, "ready")
        self.assertEqual(c(bytes.fromhex("0341030040")).action, "ignore")  # code 3, then 4 follows
        self.assertEqual(c(bytes.fromhex("0350050000")).action, "extend")  # code 5
        self.assertEqual(c(bytes.fromhex("0358060050")).action, "fail")  # code 6
        self.assertEqual(c(bytes.fromhex("0300000000")).action, "fail")  # code 0
        self.assertEqual(c(bytes.fromhex("0340070000")).action, "ready")  # code 7
        self.assertEqual(c(b"").action, "ignore")
        self.assertEqual(c(b"\x03\x00").action, "ignore")


def image_reply(pixels, x=56, y=144, error=0, length=None, status=0):
    body = struct.pack("<HHHHL", x, y, 1, 1, error) + pixels
    return struct.pack("<HI", status, len(body) if length is None else length) + body


class ImageReply(unittest.TestCase):
    PIX = bytes(range(256)) * 31 + bytes(128)  # 8064

    def test_good(self):
        self.assertEqual(len(self.PIX), 8064)
        r = parsers.parse_image_reply(image_reply(self.PIX))
        self.assertEqual(r.pixels, self.PIX)
        self.assertEqual(r.problems, [])
        self.assertEqual(len(image_reply(self.PIX)), 8082)

    def test_problems_never_raise(self):
        for reply in (
            b"",
            b"\x00",
            b"\x02\x00\x00\x00\x00\x00",
            image_reply(self.PIX, length=5),
            image_reply(self.PIX, error=7),
            image_reply(self.PIX[:-1]),
            image_reply(self.PIX, x=10, y=10),
            image_reply(self.PIX, status=0x0100),
            image_reply(bytes(100), x=10, y=10),
        ):
            r = parsers.parse_image_reply(reply)
            self.assertIsNone(r.pixels, reply[:8].hex())
            self.assertTrue(r.problems, reply[:8].hex())

    def test_no_finger_reply_from_hardware_run_1(self):
        r = parsers.parse_image_reply(bytes.fromhex("010000000000"))
        self.assertEqual(r.status, parsers.IMAGE_STATUS_NO_FINGER)
        self.assertIsNone(r.pixels)
        self.assertIn(parsers.IMAGE_STATUS_NO_FINGER, parsers.IMAGE_READ_AGAIN)
        self.assertIn(0x0001, parsers.IMAGE_STATUS_NOTES)

    def test_status_is_kept_for_the_retry_decision(self):
        self.assertEqual(parsers.parse_image_reply(b"\x02\x00\x00\x00\x00\x00").status, 2)
        self.assertIn(2, parsers.IMAGE_READ_AGAIN)
        self.assertNotIn(0x04BE, parsers.IMAGE_READ_AGAIN)


# Real replies from capture-20261013-231635-*.log, image 8 (a fast touch).
ERROR_REPLY = bytes.fromhex("07000c000000000000004d01080002000080")
NO_CAPTURE = bytes.fromhex("120400000000")
GOOD_HEADER = bytes.fromhex("00008c1f0000380090004d01080000000000")


class CaptureErrorReply(unittest.TestCase):
    def test_error_reply_is_a_capture_error_not_a_not_ready(self):
        r = parsers.parse_image_reply(ERROR_REPLY)
        self.assertEqual(r.status, 7)
        self.assertEqual(r.kind, parsers.KIND_CAPTURE_ERROR)
        self.assertEqual(r.error, 0x80000002)
        self.assertEqual((r.header["x"], r.header["y"], r.header["w1"], r.header["w2"]), (0, 0, 333, 8))
        self.assertIsNone(r.pixels)
        self.assertIn(7, parsers.IMAGE_READ_AGAIN)  # the reference set still lists it ...
        self.assertNotIn(7, parsers.IMAGE_NOT_READY)  # ... but it is not retried

    def test_good_header_has_no_error(self):
        r = parsers.parse_image_reply(GOOD_HEADER + bytes(8064 - 0))
        # header says 56 x 144 = 8064 pixels
        self.assertEqual(r.kind, parsers.KIND_IMAGE)
        self.assertEqual(r.error, 0)

    def test_kinds(self):
        k = lambda b: parsers.parse_image_reply(bytes.fromhex(b)).kind  # noqa: E731
        self.assertEqual(k("010000000000"), parsers.KIND_NOT_READY)
        self.assertEqual(k("020000000000"), parsers.KIND_NOT_READY)
        self.assertEqual(k("050000000000"), parsers.KIND_NOT_READY)
        self.assertEqual(k("060000000000"), parsers.KIND_NOT_READY)
        self.assertEqual(k("070000000000"), parsers.KIND_BAD)  # 7 without a header
        self.assertEqual(k("be04"), parsers.KIND_BAD)
        self.assertEqual(parsers.parse_image_reply(NO_CAPTURE).kind, parsers.KIND_NO_CAPTURE)
        self.assertEqual(parsers.parse_image_reply(b"").kind, parsers.KIND_BAD)

    def test_error_in_a_status_zero_reply_is_a_capture_error_too(self):
        bad = bytearray(GOOD_HEADER + bytes(8064))
        bad[14:18] = (3).to_bytes(4, "little")
        r = parsers.parse_image_reply(bytes(bad))
        self.assertEqual(r.kind, parsers.KIND_CAPTURE_ERROR)
        self.assertIsNone(r.pixels)

    def test_describe_error(self):
        text, certainty, why = parsers.describe_capture_error(0x80000002)
        self.assertEqual((text, certainty), ("error 2", parsers.UNCERTAIN))
        self.assertIn("sensor.py:733-734", why)
        self.assertIn("not documented", why)
        self.assertEqual(parsers.describe_capture_error(5)[0], "error 5")
        self.assertEqual(parsers.describe_capture_error(0x12340002)[0], "error 0x12340002")
        self.assertEqual(parsers.describe_capture_error(0)[0], "no error")

    def test_status_notes_cover_the_new_codes(self):
        self.assertIn("no capture in progress", parsers.IMAGE_STATUS_NOTES[0x0412])
        self.assertIn("error field", parsers.IMAGE_STATUS_NOTES[0x0007])


class Pgm(unittest.TestCase):
    def test_header_and_size(self):
        data = parsers.pgm_bytes(bytes(8064))
        self.assertTrue(data.startswith(b"P5\n144 56\n255\n"))
        self.assertEqual(len(data), len(b"P5\n144 56\n255\n") + 8064)
        with self.assertRaises(ValueError):
            parsers.pgm_bytes(bytes(10))


if __name__ == "__main__":
    unittest.main()
