"""Light `.mota` container parse for USB seeder catalog (no merkle verify)."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

MAGIC = b"mOTA"
TRAILER = b"vk496"
HEADER_LEN = 8
MFL = 197
TRAILER_LEN = 5
FORMAT_VER = 0x02

# Manifest offsets relative to manifest start (byte 8 of file).
OFF_FLAGS = 1
OFF_TARGET_ID = 3
OFF_FW_VERSION = 7
OFF_PAYLOAD_SIZE = 15
OFF_BLOCK_SIZE_LOG2 = 19
OFF_MERKLE_ROOT = 20
OFF_CODEC_ID = 56
OFF_HW_ID = 57
OFF_BASE_HASH = 89

DESC_WIRE = 38

CODEC_LABELS = {
    0: "full",
    1: "sequential",
    2: "in-place",
}


@dataclass(frozen=True)
class MotaManifest:
    format_ver: int
    flags: int
    target_id: int
    fw_version: int
    image_size: int
    payload_size: int
    block_size_log2: int
    block_count: int
    merkle_root: bytes
    codec_id: int
    hw_id: str
    base_hash: bytes

    @property
    def is_full(self) -> bool:
        return bool(self.flags & 0x01)

    @property
    def leaves_off(self) -> int:
        return HEADER_LEN + MFL

    @property
    def payload_off(self) -> int:
        return self.leaves_off + self.block_count * 4

    @property
    def total_size(self) -> int:
        return self.payload_off + self.payload_size + TRAILER_LEN

    def codec_label(self) -> str:
        return CODEC_LABELS.get(self.codec_id, f"codec{self.codec_id}")

    def fw_version_str(self) -> str:
        return version_str(self.fw_version)

    def mid_hex(self) -> str:
        return self.merkle_root.hex().upper()

    def base_hash_hex(self) -> str:
        if not any(self.base_hash):
            return "-"
        return self.base_hash.hex().upper()

    def target_hex(self) -> str:
        return f"{self.target_id:08X}"


@dataclass(frozen=True)
class ServedMota:
    path: Path
    bytes: bytes
    manifest: MotaManifest


def rd_u32(buf: bytes, off: int) -> int:
    return int.from_bytes(buf[off : off + 4], "little")


def version_str(v: int) -> str:
    return f"{(v >> 24) & 0xFF}.{(v >> 16) & 0xFF}.{(v >> 8) & 0xFF}"


def parse_manifest(blob: bytes) -> MotaManifest | None:
    """Parse and lightly validate a `.mota` file. Returns None if unusable."""
    if len(blob) < HEADER_LEN + MFL + TRAILER_LEN:
        return None
    if blob[:4] != MAGIC:
        return None
    total = rd_u32(blob, 4)
    if total != len(blob):
        return None
    if blob[-TRAILER_LEN:] != TRAILER:
        return None

    mf = blob[HEADER_LEN:]
    format_ver = mf[0]
    if format_ver != FORMAT_VER:
        return None

    block_size_log2 = mf[OFF_BLOCK_SIZE_LOG2]
    payload_size = rd_u32(mf, OFF_PAYLOAD_SIZE)
    if not (1 <= block_size_log2 <= 24) or payload_size == 0:
        return None

    block_size = 1 << block_size_log2
    block_count = (payload_size + block_size - 1) // block_size
    if not (1 <= block_count <= 0xFFFF):
        return None

    manifest = MotaManifest(
        format_ver=format_ver,
        flags=mf[OFF_FLAGS],
        target_id=rd_u32(mf, OFF_TARGET_ID),
        fw_version=rd_u32(mf, OFF_FW_VERSION),
        image_size=rd_u32(mf, 11),
        payload_size=payload_size,
        block_size_log2=block_size_log2,
        block_count=block_count,
        merkle_root=bytes(mf[OFF_MERKLE_ROOT : OFF_MERKLE_ROOT + 4]),
        codec_id=mf[OFF_CODEC_ID],
        hw_id=cstr(mf[OFF_HW_ID : OFF_HW_ID + 32]),
        base_hash=bytes(mf[OFF_BASE_HASH : OFF_BASE_HASH + 8]),
    )
    if manifest.total_size != len(blob):
        return None
    return manifest


def load_served_mota(path: Path) -> ServedMota | None:
    try:
        raw = path.read_bytes()
    except OSError:
        return None
    manifest = parse_manifest(raw)
    if manifest is None:
        return None
    return ServedMota(path=path, bytes=raw, manifest=manifest)


def describe_wire(served: ServedMota) -> bytes:
    """MotaDesc wire (38 B) for OP_DESCRIBE."""
    m = served.manifest
    w = bytearray(DESC_WIRE)
    w[0:4] = m.merkle_root
    w[4:8] = m.target_id.to_bytes(4, "little")
    w[8:12] = m.fw_version.to_bytes(4, "little")
    w[12] = m.codec_id
    w[13] = m.flags
    w[14:18] = len(served.bytes).to_bytes(4, "little")
    w[18:22] = m.leaves_off.to_bytes(4, "little")
    w[22:26] = m.block_count.to_bytes(4, "little")
    w[26:30] = m.payload_off.to_bytes(4, "little")
    w[30:34] = m.payload_size.to_bytes(4, "little")
    return bytes(w)


def cstr(data: bytes) -> str:
    end = data.find(b"\x00")
    if end < 0:
        end = len(data)
    return data[:end].decode("utf-8", errors="replace")
