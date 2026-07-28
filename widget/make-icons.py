#!/usr/bin/env python3
"""Generate the widget's app icons. Stdlib only - no PIL, no design tool.

    python3 widget/make-icons.py

The mark is relay's own visual language: the beacon from the mascot's head
(`((⌖))` in MASCOT_SKINS) - a dot with two signal arcs opening upward. It is
the one shape in relay that still reads at 16px, which a face does not.

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
INK_DIM = (47, 200, 102)     # the guarding tint, for the outer arc


def squircle(nx, ny, r, n=4.0):
    """Superellipse coverage test - the macOS-style rounded square."""
    return (abs(nx / r) ** n + abs(ny / r) ** n) <= 1.0


def render():
    """One high-res RGBA buffer as a flat list of (r,g,b,a) tuples."""
    n = BASE
    c = (n - 1) / 2.0
    buf = [None] * (n * n)

    r_sq = n * 0.46                      # squircle half-extent
    dot_r = n * 0.085                    # beacon dot
    dot_cy = c + n * 0.17                # sits low, arcs rise above it
    arcs = [(n * 0.20, n * 0.045, INK),        # inner arc
            (n * 0.32, n * 0.042, INK_DIM)]    # outer arc, dimmer

    for y in range(n):
        dy = y - c
        row = y * n
        for x in range(n):
            dx = x - c
            px = (0, 0, 0, 0)
            if squircle(dx, dy, r_sq):
                px = BG + (255,)
                ddy = y - dot_cy
                d = math.hypot(dx, ddy)
                if d <= dot_r:
                    px = INK + (255,)
                else:
                    for rad, w, col in arcs:
                        # Annulus, upper half only: a signal fanning upward.
                        if abs(d - rad) <= w / 2 and ddy < -rad * 0.30:
                            px = col + (255,)
                            break
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
