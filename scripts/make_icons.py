"""Draw the app icons (a vault door) with the standard library only: python scripts/make_icons.py"""

import math
import struct
import zlib
from pathlib import Path

OUT = Path(__file__).resolve().parent.parent / "finance" / "web" / "static" / "icons"
BG, FG = (36, 87, 214), (255, 255, 255)           # the app's accent blue and white


def png(size: int, pixels: bytes) -> bytes:
    def chunk(tag: bytes, data: bytes) -> bytes:
        return struct.pack(">I", len(data)) + tag + data + struct.pack(">I", zlib.crc32(tag + data) & 0xFFFFFFFF)
    rows = b"".join(b"\x00" + pixels[y * size * 3:(y + 1) * size * 3] for y in range(size))
    return b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", struct.pack(">IIBBBBB", size, size, 8, 2, 0, 0, 0)) + \
        chunk(b"IDAT", zlib.compress(rows, 9)) + chunk(b"IEND", b"")


def coverage(x: float, y: float, scale: float) -> float:
    """1.0 where the glyph is, in unit coordinates centred on (0, 0); `scale` shrinks it into the maskable safe zone."""
    x, y = x / scale, y / scale
    r = math.hypot(x, y)
    if 0.56 <= r <= 0.66 or r <= 0.12:             # the door's rim and its hub
        return 1.0
    if r < 0.56:                                    # three spokes from the hub
        for k in range(3):
            a = k * 2 * math.pi / 3 - math.pi / 2
            along = x * math.cos(a) + y * math.sin(a)
            across = abs(-x * math.sin(a) + y * math.cos(a))
            if 0.0 < along < 0.5 and across < 0.05:
                return 1.0
    return 0.0


def draw(size: int, scale: float, rounded: bool) -> bytes:
    ss, out = 3, bytearray()                        # 3x3 supersampling for smooth edges
    for py in range(size):
        for px in range(size):
            fg = inside = 0
            for sy in range(ss):
                for sx in range(ss):
                    u = ((px + (sx + .5) / ss) / size) * 2 - 1
                    v = ((py + (sy + .5) / ss) / size) * 2 - 1
                    ok = True
                    if rounded:                     # rounded square background
                        qx, qy = abs(u) - 0.78, abs(v) - 0.78
                        ok = math.hypot(max(qx, 0), max(qy, 0)) + min(max(qx, qy), 0) <= 0.22
                    inside += ok
                    fg += ok and coverage(u, v, scale) > 0
            a = inside / (ss * ss)
            mix = (fg / inside) if inside else 0
            colour = [round(BG[i] * (1 - mix) + FG[i] * mix) for i in range(3)]
            colour = [round(c * a + 255 * (1 - a)) for c in colour] if rounded else colour
            out += bytes(colour)
    return png(size, bytes(out))


if __name__ == "__main__":
    OUT.mkdir(parents=True, exist_ok=True)
    for name, size, scale, rounded in [("icon-192.png", 192, 1.25, True), ("icon-512.png", 512, 1.25, True),
                                       ("icon-maskable-512.png", 512, 0.95, False), ("apple-touch-icon.png", 180, 1.0, False)]:
        (OUT / name).write_bytes(draw(size, scale, rounded))
        print("wrote", name)
