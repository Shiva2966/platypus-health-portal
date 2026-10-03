"""Generate simple PNG app icons (pure Python, no Pillow). Run once: python tools_make_icons.py"""
import struct
import zlib
from pathlib import Path


def png(size: int) -> bytes:
    bg, fg = (11, 79, 138), (255, 255, 255)
    rows = []
    c, t = size / 2, size * 0.14  # plus-sign arm half-thickness
    arm = size * 0.30
    for y in range(size):
        row = bytearray([0])
        for x in range(size):
            in_v = abs(x - c) <= t and abs(y - c) <= arm
            in_h = abs(y - c) <= t and abs(x - c) <= arm
            row += bytes(fg if in_v or in_h else bg)
        rows.append(bytes(row))
    raw = b"".join(rows)

    def chunk(tag, data):
        return struct.pack(">I", len(data)) + tag + data + struct.pack(">I", zlib.crc32(tag + data) & 0xFFFFFFFF)

    return (b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", struct.pack(">IIBBBBB", size, size, 8, 2, 0, 0, 0))
            + chunk(b"IDAT", zlib.compress(raw, 9)) + chunk(b"IEND", b""))


out = Path(__file__).parent / "app" / "static" / "icons"
out.mkdir(parents=True, exist_ok=True)
for s in (192, 512):
    (out / f"icon-{s}.png").write_bytes(png(s))
print("icons written to", out)
