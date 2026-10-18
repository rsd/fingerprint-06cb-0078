# SPDX-License-Identifier: MIT
"""Every pin of the 06cb:0078 setup: sha256 values, lengths and the known vendor archives.

Pure data, stdlib only. A sha256 ships no bytes: the vendor DLL tables and the capture
module are found at run time in a downloaded file and accepted only when they hash to
the values here (research/steps/b5-setup-design.md, "Provenance of the fixed bytes").

The module-level names are the pins in force. `Pins` bundles the ones the template
builder, the archive extractor and the command guard take as an argument, so tests can
pass synthetic pins (`dataclasses.replace(DEFAULT_PINS, ...)`); `Pins.to_json` /
`Pins.from_json` carry them to the unprivileged extractor child.
"""

from __future__ import annotations

import json
from dataclasses import asdict, dataclass

# ------------------------------------------------------------------ module
# The 10500-byte capture module (DLL module-table record 30, ROM 6/0x14): its full
# sha256, and the sha256 of its first MODULE_HEAD_LEN bytes, the prefilter of the scan
# (a full-window sha256 at every offset is too slow in Python).
MODULE_LEN = 10500
MODULE_SHA256 = "67f4a332d89f76fe12c57fa7f67b43a076f689e78267b888c694e26b05caacc1"
MODULE_HEAD_LEN = 32
MODULE_HEAD_SHA256 = "2c689d24db8fa281596ede9025b32aa4446a43e4a5a5d8a42caa3d51590a3101"

# ---------------------------------------------------------------- template
# The 18869-byte identify program with every per-unit region zeroed
# (proto/data/cmd02_template.bin, rebuilt by synaptics0078.template.build_from_dll).
TEMPLATE_LEN = 18869
TEMPLATE_SHA256 = "bca3d4289d668431dfac0597f22030ea95946583867fd4ce7a71e3f72e86cbc4"


@dataclass(frozen=True)
class ChunkPin:
    """One chunk: u16 type, u16 data length, sha256 of the 4-byte header plus the data."""

    name: str
    type: int
    length: int
    sha256: str


# The template, chunk by chunk in program order, with the per-unit regions zeroed.
# program_matches_template names the first chunk that differs from these without
# needing the template bytes. The 5-byte header (02, u16 152, u16 0) is pinned apart.
TEMPLATE_HEADER_SHA256 = "397d506be813d0271034df6a897641bd5c115ac8835a31f15b6333574bba5456"
TEMPLATE_CHUNKS = (
    ChunkPin("0x23 reply config", 0x23, 0, "d2d27d69fc0a2c6cc0aabec462ce665aa8a92766844f081b672588acdf8a2c71"),
    ChunkPin("0x20 set register", 0x20, 8, "6f21a8af5cd16d7934e85d2778e960309cbb750ceb233cd4bc257b9cdfd315af"),
    ChunkPin("0x32 register list (92)", 0x32, 92, "d4fe780a35eb55d2c922886613957315e62bc4a44678aa0386b3cd55846fb46a"),
    ChunkPin("0x33 register list (40)", 0x33, 40, "8ba74240fb41c6ccf56b936f5d5fd2c8ddbea8f7b4de722dcc930859f966f4d4"),
    ChunkPin("0x32 register list (64)", 0x32, 64, "0100687df42c95ea5875299aa67549e1c3decb7bcbe3732ddabe3ed47b28b7a1"),
    ChunkPin("0x33 register list (868)", 0x33, 868, "36275126da626f5cbdcadd50b198192db3d02b68b1747c617396dc891b444e25"),
    ChunkPin("0x32 register list (12)", 0x32, 12, "5cd41592097185ef5691ba53629658fd62787c08a4a8a0ea5b09ed46bd558b60"),
    ChunkPin("0x34 timeslot table", 0x34, 536, "21f0480628cc373acd23c08e67de79c72f2a0afc09d4089256c03b28a8f14a09"),
    ChunkPin("0x42 data encoder", 0x42, 8, "2346059983295ffae8034eb5a5e024741b07c27e8abd8a6f955c3a8b78bd00f4"),
    ChunkPin("0x2f rows", 0x2F, 4, "52329cba3e90443d141853f2563e375dbbe34bb5bb06a51309161bd170362a18"),
    ChunkPin("0x29 timeslot offset", 0x29, 4, "5d37727c70911db978bc504b280cd1a292f604a8c9e2508a5372763bb283efee"),
    ChunkPin("0x35 finger-detect offset", 0x35, 4, "0bdd15d65a090db4aee652777589bc0bbcbfa89b4f850e14b668c1f7fab1e1ae"),
    ChunkPin("0x17 reply config", 0x17, 0, "a376d173ece243d587f352f04307fb971f10cdbc9b9d850e8ecbd414a586fa3c"),
    ChunkPin("0x26 finger detect", 0x26, 40, "9d1bc06b97909a0bba570de676debe71eb602b0163a94f68f0032137eef0a0c5"),
    ChunkPin("0x2e image reconstruction", 0x2E, 28, "b14099f397a166a8a1e6161f3eb19b50a70228bb4e7cf906dd5c89fdae1c4b53"),
    ChunkPin("0x47 enable ASP", 0x47, 0, "0ab677189bbd88d8d69abceb88946f32a36333b36d4e75cb1ae69ef3c4cf1fc2"),
    ChunkPin("0x44 line interleave", 0x44, 4, "d5d1c67ab86fc9995f7f2d6da7917e7fef3f91465aedb8615415ced1698062d6"),
    ChunkPin("0x30 line update", 0x30, 8404, "487c2ec6cd83d24d449094c28a5f1af25dde74003daf3983aff92ce0b05af5d8"),
    ChunkPin("0x43 line transform", 0x43, 152, "3274ed5a58828a0c5994fe5ee76baac8bfc9cbab5b4883cde1daa27576b56f64"),
    ChunkPin("0x48 baseline frame", 0x48, 8516, "6c2e9f58a608c27cb65ff9b3f7d36d6c4259856b9db963a0bf4dff0bf1bb7545"),
)

# --------------------------------------------------------------- DLL tables
# The chunks the template takes verbatim from synaWudfBioUsb.dll (class a), located by
# their 4-byte header and accepted only when sha256(header + data) is the pin. The DLL
# offsets in the 2018 build are given for information; the builder searches.
DLL_CHUNKS = (
    ChunkPin("reg32", 0x20, 8, "6f21a8af5cd16d7934e85d2778e960309cbb750ceb233cd4bc257b9cdfd315af"),  # @0x1dbfa4
    ChunkPin("list16_92", 0x32, 92, "d4fe780a35eb55d2c922886613957315e62bc4a44678aa0386b3cd55846fb46a"),  # @0x1e2f30
    ChunkPin("list32_40", 0x33, 40, "8ba74240fb41c6ccf56b936f5d5fd2c8ddbea8f7b4de722dcc930859f966f4d4"),  # @0x1df390
    ChunkPin("list16_64", 0x32, 64, "0100687df42c95ea5875299aa67549e1c3decb7bcbe3732ddabe3ed47b28b7a1"),  # @0x1df3bc
    ChunkPin("list32_868", 0x33, 868, "36275126da626f5cbdcadd50b198192db3d02b68b1747c617396dc891b444e25"),  # @0x1e0e40
    ChunkPin("list16_12", 0x32, 12, "5cd41592097185ef5691ba53629658fd62787c08a4a8a0ea5b09ed46bd558b60"),  # @0x1dfa70
    ChunkPin("timeslot", 0x34, 536, "2fabd6102520053138e68b6f0452b3b626d7788f00287cbaf72d4b16b3c05783"),  # @0x1e1f60
    ChunkPin("encoder", 0x42, 8, "2346059983295ffae8034eb5a5e024741b07c27e8abd8a6f955c3a8b78bd00f4"),  # @0x1dfc90
)

# ---------------------------------------------------- calibration programs
# sha256 of calib_model.skeleton(program) for the two calibration programs built from
# the template: they pin every byte of programs #1 and #2 that is not per unit.
PROGRAM1_LEN = 1917
PROGRAM2_LEN = 10269
PROGRAM1_SKELETON_SHA256 = "1ccfc70fadd99f9e827d7135ab7fbdff0ac90fdf37a82aae40c8dae5d13ceb6b"
PROGRAM2_SKELETON_SHA256 = "ee276e7d58b59d9be70dafa06c9104614f120c7b0af7fbf9bd0426c534ae8512"

# ----------------------------------------------------------------- sources

MB = 1 << 20


@dataclass(frozen=True)
class Archive:
    """A public vendor archive that holds the DLL (and so the module), tried in this order."""

    name: str  # short id, also the cache file suffix
    label: str  # for users: where it comes from
    url: str
    size: int
    sha256: str
    kind: str  # "cab" or "zip"
    max_bytes: int  # curl --max-filesize
    max_time: int  # curl --max-time, seconds


_CAB_2019 = (
    "https://catalog.s.download.windowsupdate.com/d/msdownload/update/driver/drvs/2019/07/"
    "c337faa1-00ec-4849-a62d-439fa9ef915b_ed7982efc8a40ca11c27348041c9830e26ecd880.cab"
)
_CAB_2020 = (
    "https://catalog.s.download.windowsupdate.com/c/msdownload/update/driver/drvs/2020/11/"
    "198986b3-7e10-4476-bf0c-0446e71ba7c7_f76a9a492da9b1cadf627700be4c297eea42058b.cab"
)
_ORIGIN_ZIP = (
    "https://www3.corsair.com/Files/Origin_PC/Drivers/Laptops/Legacy/EON17-SLX/"
    "P870KM1-G%20%28KABYLAKE%29/Options/02_Finger.zip"
)
# The Wayback raw (id_) captures. The Origin capture's URL spells the parentheses
# unescaped, as Wayback stored it; it is pinned to the live zip's size and sha256.
_WAYBACK_2019 = "https://web.archive.org/web/20250717152615id_/"
_WAYBACK_2020 = "https://web.archive.org/web/20250717144014id_/"
_WAYBACK_ORIGIN = (
    "https://web.archive.org/web/20261017115848id_/https://www3.corsair.com/Files/Origin_PC/Drivers/"
    "Laptops/Legacy/EON17-SLX/P870KM1-G%20(KABYLAKE)/Options/02_Finger.zip"
)
_SHA_2019 = "169cf21c0b20276abbc510b0b9e3491ebe3cc215887024e31f29b45645e91960"
_SHA_2020 = "2874fc25ccb1972e8d730ca92ec8227eabb6bec566d453234d29f79e728fdfd1"
_SHA_ORIGIN = "87d67e500ac3e25956726e66875b24b5a7c29a07e85af4548757a67bd4faddc2"

LABEL_CATALOG = "Microsoft Update Catalog"
LABEL_WAYBACK = "Internet Archive copy"
LABEL_ORIGIN = "Origin PC"

ARCHIVES = (
    Archive("catalog-2019", LABEL_CATALOG, _CAB_2019, 3_365_928, _SHA_2019, "cab", 16 * MB, 300),
    Archive("catalog-2020", LABEL_CATALOG, _CAB_2020, 3_364_070, _SHA_2020, "cab", 16 * MB, 300),
    Archive("wayback-2019", LABEL_WAYBACK, _WAYBACK_2019 + _CAB_2019, 3_365_928, _SHA_2019, "cab", 16 * MB, 600),
    Archive("wayback-2020", LABEL_WAYBACK, _WAYBACK_2020 + _CAB_2020, 3_364_070, _SHA_2020, "cab", 16 * MB, 600),
    Archive("origin-2017", LABEL_ORIGIN, _ORIGIN_ZIP, 7_901_691, _SHA_ORIGIN, "zip", 32 * MB, 300),
    Archive("wayback-origin-2017", LABEL_WAYBACK, _WAYBACK_ORIGIN, 7_901_691, _SHA_ORIGIN, "zip", 32 * MB, 600),
)
KNOWN_ARCHIVE_SHA256 = frozenset(a.sha256 for a in ARCHIVES)

# ------------------------------------------------------------------ bundle


@dataclass(frozen=True)
class Pins:
    """The pins one extraction or one guarded session uses (DEFAULT_PINS in production)."""

    module_len: int = MODULE_LEN
    module_sha256: str = MODULE_SHA256
    module_head_len: int = MODULE_HEAD_LEN
    module_head_sha256: str = MODULE_HEAD_SHA256
    template_len: int = TEMPLATE_LEN
    template_sha256: str = TEMPLATE_SHA256
    template_header_sha256: str = TEMPLATE_HEADER_SHA256
    template_chunks: tuple[ChunkPin, ...] = TEMPLATE_CHUNKS
    dll_chunks: tuple[ChunkPin, ...] = DLL_CHUNKS
    program1_len: int = PROGRAM1_LEN
    program1_skeleton_sha256: str = PROGRAM1_SKELETON_SHA256
    program2_len: int = PROGRAM2_LEN
    program2_skeleton_sha256: str = PROGRAM2_SKELETON_SHA256

    @property
    def skeletons(self) -> dict[int, str]:
        """{program length: skeleton sha256}, the 02 forms the command guard lets through."""
        return {self.program1_len: self.program1_skeleton_sha256, self.program2_len: self.program2_skeleton_sha256}

    def dll_chunk(self, name: str) -> ChunkPin:
        for c in self.dll_chunks:
            if c.name == name:
                return c
        raise KeyError(name)

    def to_json(self) -> str:
        return json.dumps(asdict(self), sort_keys=True)

    @classmethod
    def from_json(cls, text: str) -> Pins:
        """The inverse of to_json; raises ValueError on anything that is not exactly such a document."""
        try:
            d = json.loads(text)
        except json.JSONDecodeError as exc:
            raise ValueError(f"pins: not JSON ({exc.msg})") from None
        if not isinstance(d, dict) or set(d) != {f for f in cls.__dataclass_fields__}:
            raise ValueError("pins: not a pins document (wrong field set)")
        try:
            for key in ("template_chunks", "dll_chunks"):
                d[key] = tuple(ChunkPin(**c) for c in d[key])
            out = cls(**d)
        except TypeError as exc:
            raise ValueError(f"pins: bad field ({exc})") from None
        for f in cls.__dataclass_fields__:
            want = type(getattr(DEFAULT_PINS, f))
            if not isinstance(getattr(out, f), want):
                raise ValueError(f"pins: field {f} is not a {want.__name__}")  # noqa: TRY004 (one error type)
        return out


DEFAULT_PINS = Pins()
