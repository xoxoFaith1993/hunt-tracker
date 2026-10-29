"""Cuts the red stat icons out of the in-game screenshots into icons/<name>.png (transparent background).
Run once with the screenshots' raw pixels (BGRA dumps); the tracker only needs the icons folder."""
import struct
import sys
import zlib
from pathlib import Path

# screenshot number -> stat name, as matched by reading each screenshot's text
NAMES = {
    14: "Rifle", 29: "Crossbow Deadeye", 19: "Semi-Automatic", 13: "Revolver", 8: "Melee", 20: "Shotgun",
    21: "Silencer", 22: "Single-Shot", 23: "Sniper", 5: "Lever-Action", 6: "Marksman", 12: "Pump-Action",
    17: "Sawn-Off", 35: "Crossbow", 7: "Maynard Sniper", 30: "Vetterli 71", 16: "Romero 77", 25: "Specter 1882",
    15: "Rival 78", 26: "Springfield 1866", 4: "Krag", 10: "Nitro Express", 28: "Terminus", 9: "Mosin-Nagant",
    11: "Officer", 24: "Sparks", 18: "Scottfield", 2: "Headshots", 27: "Teams Wiped", 3: "Kills/Deaths",
    75: "Furthest Kill Distance", 76: "Maxed-Out Hunters",
}


def png_rgba(w: int, h: int, rgba: bytes) -> bytes:
    def chunk(kind: bytes, body: bytes) -> bytes:
        return struct.pack(">I", len(body)) + kind + body + struct.pack(">I", zlib.crc32(kind + body))
    raw = b"".join(b"\x00" + rgba[y * w * 4:(y + 1) * w * 4] for y in range(h))
    return (b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", struct.pack(">IIBBBBB", w, h, 8, 6, 0, 0, 0))
            + chunk(b"IDAT", zlib.compress(raw, 9)) + chunk(b"IEND", b""))


def cut(raw: bytes, w: int, h: int) -> tuple:
    """The red icon at the left of a stat row -> (w, h, RGBA) with the dark background made clear."""
    def red(x, y):
        i = (y * w + x) * 4
        b, g, r = raw[i], raw[i + 1], raw[i + 2]
        return r > 110 and r - g > 60 and r - b > 60
    pts = [(x, y) for y in range(h) for x in range(int(w * 0.3)) if red(x, y)]
    x0, x1 = min(p[0] for p in pts), max(p[0] for p in pts)
    y0, y1 = min(p[1] for p in pts), max(p[1] for p in pts)
    x0, y0, x1, y1 = max(0, x0 - 1), max(0, y0 - 1), min(w - 1, x1 + 1), min(h - 1, y1 + 1)
    out = bytearray()
    for y in range(y0, y1 + 1):
        for x in range(x0, x1 + 1):
            i = (y * w + x) * 4
            b, g, r = raw[i], raw[i + 1], raw[i + 2]
            a = max(0, min(255, int((r - max(g, b) - 25) * 2.2)))
            out += bytes((r, g, b, a))
    return x1 - x0 + 1, y1 - y0 + 1, bytes(out)


if __name__ == "__main__":
    raw_dir, out_dir = Path(sys.argv[1]), Path(sys.argv[2])
    out_dir.mkdir(parents=True, exist_ok=True)
    for n, name in NAMES.items():
        meta = (raw_dir / f"{n}.txt").read_text().split()
        w, h = int(meta[0]), int(meta[1])
        iw, ih, rgba = cut((raw_dir / f"{n}.raw").read_bytes(), w, h)
        (out_dir / (name.replace("/", "-") + ".png")).write_bytes(png_rgba(iw, ih, rgba))
        print(f"{name}: {iw}x{ih}")
