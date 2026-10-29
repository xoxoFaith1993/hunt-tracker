"""Draws the app icon (a brass diamond on a dark tile, like the tracker's header) as icon.ico."""
import struct
import zlib
from pathlib import Path


def png(size: int) -> bytes:
    bg, edge, brass, dark = (20, 16, 13), (107, 36, 29), (201, 164, 106), (29, 23, 18)
    c, rows = size / 2, []
    for y in range(size):
        row = bytearray()
        for x in range(size):
            d = abs(x + 0.5 - c) + abs(y + 0.5 - c)  # diamond distance
            inset = min(x, y, size - 1 - x, size - 1 - y)
            if inset < max(1, size // 32):
                px = edge + (255,)                       # red frame
            elif d < size * 0.36 and d > size * 0.27:
                px = brass + (255,)                      # diamond outline
            elif d <= size * 0.18:
                px = brass + (255,)                      # inner diamond
            elif d <= size * 0.27:
                px = dark + (255,)
            else:
                px = bg + (255,)
            row += bytes(px)
        rows.append(b"\x00" + bytes(row))

    def chunk(kind: bytes, body: bytes) -> bytes:
        return struct.pack(">I", len(body)) + kind + body + struct.pack(">I", zlib.crc32(kind + body))

    return (b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", struct.pack(">IIBBBBB", size, size, 8, 6, 0, 0, 0))
            + chunk(b"IDAT", zlib.compress(b"".join(rows), 9)) + chunk(b"IEND", b""))


sizes = [16, 32, 48, 256]
images = [png(s) for s in sizes]
header = struct.pack("<HHH", 0, 1, len(sizes))
offset = 6 + 16 * len(sizes)
entries = b""
for s, img in zip(sizes, images):
    entries += struct.pack("<BBBBHHII", s % 256, s % 256, 0, 0, 1, 32, len(img), offset)
    offset += len(img)
out = Path(__file__).with_name("icon.ico")
out.write_bytes(header + entries + b"".join(images))
print("wrote", out)
