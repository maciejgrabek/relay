#!/usr/bin/env python3
"""Generate the widget's app icons. Stdlib only - no PIL, no design tool.

    python3 widget/make-icons.py

The mark is the R from relay's own banner - the ANSI-Shadow block letterform in
app.BANNER, drop shadow and all - so the Dock icon and the panel logo are
visibly the same typeface rather than two unrelated marks. A block letter also
survives 16px, which a mascot face does not.

Rendered once at 4x into a supersample buffer and box-downsampled to each
target size, so edges are antialiased without an imaging library.
"""
import math
import os
import struct
import zlib

OUT = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                   "src-tauri", "icons")
SIZES = {"32x32.png": 32, "128x128.png": 128,
         "128x128@2x.png": 256, "icon.png": 512}

SS = 4                       # supersample factor
BASE = 512 * SS

BG = (13, 21, 18)            # near-black, matches the widget's panel
INK = (110, 255, 160)        # relay green
SHADOW = (24, 105, 58)       # the banner's bevel, one step down from INK

# The R from app.BANNER as a bitmap. The banner draws it in ANSI-Shadow, whose
# whole character is a heavy slab with a hard offset bevel - so the glyph is
# defined here and the bevel is applied below by sampling it at an offset,
# exactly as the figlet font does with its shadow column.
GLYPH = [
    "XXXXX.",
    "X....X",
    "X....X",
    "XXXXX.",
    "X..X..",
    "X...X.",
    "X....X",
]
GW, GH = len(GLYPH[0]), len(GLYPH)
BEVEL = 0.55                 # bevel offset, in glyph cells


def squircle(nx, ny, r, n=4.0):
    """Superellipse coverage test - the macOS-style rounded square."""
    return (abs(nx / r) ** n + abs(ny / r) ** n) <= 1.0


def _on(gx, gy):
    """Is glyph cell (gx, gy) filled? Out of bounds reads as empty."""
    if 0 <= gy < GH and 0 <= gx < GW:
        return GLYPH[gy][gx] == "X"
    return False


def render():
    """One high-res RGBA buffer as a flat list of (r,g,b,a) tuples."""
    n = BASE
    c = (n - 1) / 2.0
    buf = [None] * (n * n)

    r_sq = n * 0.46                      # squircle half-extent
    # Fit the glyph into the squircle with optical padding, keeping its aspect.
    cell = min(n * 0.60 / GW, n * 0.62 / GH)
    ox = c - (GW * cell) / 2.0
    oy = c - (GH * cell) / 2.0

    for y in range(n):
        dy = y - c
        row = y * n
        gy = (y - oy) / cell
        for x in range(n):
            px = (0, 0, 0, 0)
            if squircle(x - c, dy, r_sq):
                px = BG + (255,)
                gx = (x - ox) / cell
                if _on(int(gx), int(gy)) and gx >= 0 and gy >= 0:
                    px = INK + (255,)
                elif (_on(int(gx - BEVEL), int(gy - BEVEL))
                      and gx >= BEVEL and gy >= BEVEL):
                    px = SHADOW + (255,)
            buf[row + x] = px
    return buf


def downsample(buf, size):
    """Box filter from the BASE buffer down to `size`, averaging alpha too."""
    step = BASE // size
    out = []
    area = step * step
    for oy in range(size):
        for ox in range(size):
            r = g = b = a = 0
            for sy in range(oy * step, oy * step + step):
                row = sy * BASE
                for sx in range(ox * step, ox * step + step):
                    pr, pg, pb, pa = buf[row + sx]
                    # Premultiply so transparent pixels cannot darken the edge.
                    r += pr * pa; g += pg * pa; b += pb * pa; a += pa
            if a:
                out.append((r // a, g // a, b // a, a // area))
            else:
                out.append((0, 0, 0, 0))
    return out


def write_png(path, size, pixels):
    raw = bytearray()
    for y in range(size):
        raw.append(0)
        for x in range(size):
            raw.extend(pixels[y * size + x])

    def chunk(tag, data):
        c = tag + data
        return struct.pack(">I", len(data)) + c + struct.pack(">I", zlib.crc32(c))

    with open(path, "wb") as f:
        f.write(b"\x89PNG\r\n\x1a\n")
        f.write(chunk(b"IHDR", struct.pack(">IIBBBBB", size, size, 8, 6, 0, 0, 0)))
        f.write(chunk(b"IDAT", zlib.compress(bytes(raw), 9)))
        f.write(chunk(b"IEND", b""))


def main():
    os.makedirs(OUT, exist_ok=True)
    print(f"rendering {BASE}x{BASE} supersample buffer...")
    buf = render()
    for name, size in sorted(SIZES.items(), key=lambda kv: -kv[1]):
        write_png(os.path.join(OUT, name), size, downsample(buf, size))
        print(f"  {name} ({size}px)")
    print(f"wrote {len(SIZES)} icons to {OUT}")


if __name__ == "__main__":
    main()
