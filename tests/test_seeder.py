"""Tests for mota-seeder host (folder scan + MS/ms framing)."""

from __future__ import annotations

import io
import struct
import tempfile
import unittest
from pathlib import Path

from envybot.mota import (
    FORMAT_VER,
    HEADER_LEN,
    MAGIC,
    MFL,
    TRAILER,
    TRAILER_LEN,
    load_served_mota,
    parse_manifest,
)
from envybot.seeder import (
    OP_BEGIN,
    OP_COUNT,
    OP_DESCRIBE,
    OP_FIN,
    OP_READ,
    OP_STAT,
    OP_WRITE,
    SeederCore,
    SeederFolder,
    read_request,
    scan_folder,
    send_response,
    xor_bytes,
)


def _build_test_mota(payload: bytes, *, target_id: int = 0x5C6AB408, fw_version: int = 0x01110100) -> bytes:
    """Minimal valid `.mota` for seeder tests."""
    block_size_log2 = 10  # 1024
    block_size = 1 << block_size_log2
    block_count = (len(payload) + block_size - 1) // block_size
    leaves = b"\x00" * (block_count * 4)

    mf = bytearray(MFL)
    mf[0] = FORMAT_VER
    mf[1] = 0x01  # MFLAG_FULL
    mf[2] = 0x12
    struct.pack_into("<I", mf, 3, target_id)
    struct.pack_into("<I", mf, 7, fw_version)
    struct.pack_into("<I", mf, 11, len(payload))
    struct.pack_into("<I", mf, 15, len(payload))
    mf[19] = block_size_log2
    struct.pack_into("<I", mf, 20, 0xDEADBEEF)  # merkle_root / mid
    mf[56] = 0  # codec full
    hw = (b"RAK4631" + b"\x00" * 32)[:32]
    mf[57:89] = hw

    body = MAGIC + b"\x00\x00\x00\x00" + bytes(mf) + leaves + payload + TRAILER
    total = len(body)
    body = MAGIC + struct.pack("<I", total) + body[8:]
    return body


class BuildMotaTests(unittest.TestCase):
    def test_synthetic_roundtrip(self) -> None:
        raw = _build_test_mota(bytes(range(256)))
        m = parse_manifest(raw)
        assert m is not None
        self.assertEqual(m.target_id, 0x5C6AB408)
        self.assertEqual(m.codec_id, 0)
        served = load_served_mota_from_bytes(raw)
        assert served is not None
        self.assertEqual(served.manifest.mid_hex(), "EFBEADDE")


def load_served_mota_from_bytes(raw: bytes):
    import tempfile

    with tempfile.NamedTemporaryFile(suffix=".mota", delete=False) as f:
        f.write(raw)
        path = Path(f.name)
    try:
        from envybot.mota import load_served_mota

        return load_served_mota(path)
    finally:
        path.unlink(missing_ok=True)


class ScanFolderTests(unittest.TestCase):
    def test_non_recursive_skips_subdir(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            (root / "top.mota").write_bytes(_build_test_mota(b"x" * 64))
            sub = root / ".images"
            sub.mkdir()
            (sub / "hidden.mota").write_bytes(_build_test_mota(b"y" * 64))
            folder = scan_folder(root, recursive=False)
            self.assertEqual(folder.count(), 1)
            self.assertEqual(folder.at(0).path.name, "top.mota")

    def test_only_glob(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            (root / "from-0.1.0-to-0.1.1.mota").write_bytes(_build_test_mota(b"a" * 32))
            (root / "from-0.1.0-to-0.1.2.mota").write_bytes(_build_test_mota(b"b" * 32))
            folder = scan_folder(root, only_globs=["from-0.1.0-to-0.1.1*"])
            self.assertEqual(folder.count(), 1)
            self.assertEqual(folder.at(0).path.name, "from-0.1.0-to-0.1.1.mota")

    def test_skips_invalid(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            (root / "bad.mota").write_bytes(b"not a mota")
            (root / "good.mota").write_bytes(_build_test_mota(b"z" * 16))
            warned: list[tuple[Path, str]] = []

            def warn(p: Path, m: str) -> None:
                warned.append((p, m))

            folder = scan_folder(root, warn=warn)
            self.assertEqual(folder.count(), 1)
            self.assertEqual(len(warned), 1)


class SeederCoreTests(unittest.TestCase):
    def setUp(self) -> None:
        raw = _build_test_mota(bytes(i % 256 for i in range(3000)))
        served = load_served_mota_from_bytes(raw)
        assert served is not None
        self.core = SeederCore(SeederFolder([served]))

    def test_count_describe_read(self) -> None:
        st, payload = self.core.handle(OP_COUNT, b"")  # type: ignore[misc]
        self.assertEqual(st, 0)
        self.assertEqual(payload, b"\x01")

        st, desc = self.core.handle(OP_DESCRIBE, bytes([0]))
        self.assertEqual(st, 0)
        self.assertEqual(len(desc), 38)
        self.assertEqual(desc[0:4].hex().upper(), "EFBEADDE")

        read_args = bytes([0]) + (0).to_bytes(4, "little") + (4).to_bytes(2, "little")
        st, chunk = self.core.handle(OP_READ, read_args)
        self.assertEqual(st, 0)
        self.assertEqual(chunk, MAGIC)

        st, _ = self.core.handle(OP_DESCRIBE, bytes([9]))
        self.assertEqual(st, 1)

    def test_storage_ops_err(self) -> None:
        mid = b"\xde\xad\xbe\xef"
        for op in (OP_STAT, OP_BEGIN, OP_WRITE, OP_FIN):
            st, payload = self.core.handle(op, mid + b"\x00" * 4)
            self.assertEqual(st, 1)
            self.assertEqual(payload, b"")


class FramingTests(unittest.TestCase):
    def test_request_xor_and_response(self) -> None:
        op = OP_COUNT
        args = b""
        xsum = xor_bytes(args, op)
        wire = b"MS" + bytes([op]) + args + bytes([xsum])

        class FakeSer:
            def __init__(self, data: bytes) -> None:
                self._buf = io.BytesIO(data)
                self.out = bytearray()

            def read(self, n: int = 1) -> bytes:
                return self._buf.read(n)

            def write(self, data: bytes) -> None:
                self.out.extend(data)

            def flush(self) -> None:
                pass

        ser = FakeSer(wire[2:])  # read_request called after MS seen
        # Simulate magic already consumed
        req = read_request(ser)
        assert req is not None
        self.assertEqual(req[0], OP_COUNT)

        send_response(ser, OP_COUNT, 0, b"\x01")
        out = bytes(ser.out)
        self.assertTrue(out.startswith(b"ms"))
        self.assertEqual(out[2], OP_COUNT)
        self.assertEqual(out[3], 0)
        self.assertEqual(out[4], 1)
        expect_xsum = xor_bytes(out[:-1])
        self.assertEqual(out[-1], expect_xsum)


if __name__ == "__main__":
    unittest.main()
