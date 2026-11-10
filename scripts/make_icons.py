#!/usr/bin/env python3
"""Generate icons for manyue (pure stdlib, no PIL).

Icons are written as RGBA PNGs (color type 6): fnOS App Center rejects
packages whose ICON files are not RGBA ("应用包不符合系统要求").

Design: dark navy background, a pale full-moon disc, and a small open
book (two paper pages) at the bottom.
"""

import binascii
import os
import struct
import sys
import zlib

BG = (16, 24, 40)
MOON = (240, 230, 199)
PAGE = (247, 243, 234)

# Open-book pages, normalized coordinates (two quads, spine in the middle).
LEFT_PAGE = [(0.20, 0.76), (0.47, 0.68), (0.47, 0.90), (0.20, 0.96)]
RIGHT_PAGE = [(0.80, 0.76), (0.53, 0.68), (0.53, 0.90), (0.80, 0.96)]


def _inside_polygon(x, y, poly):
    inside = False
    n = len(poly)
    for i in range(n):
        x1, y1 = poly[i]
        x2, y2 = poly[(i + 1) % n]
        if (y1 > y) != (y2 > y):
            xinters = (x2 - x1) * (y - y1) / (y2 - y1) + x1
            if x < xinters:
                inside = not inside
    return inside


def _png_chunk(kind, data):
    chunk = struct.pack(">I", len(data)) + kind + data
    chunk += struct.pack(">I", binascii.crc32(kind + data) & 0xFFFFFFFF)
    return chunk


def write_png(path, size):
    cx = size * 0.5
    cy = size * 0.40
    radius = size * 0.28
    raw = bytearray()
    for py in range(size):
        raw.append(0)  # filter: none
        for px in range(size):
            nx = (px + 0.5) / size
            ny = (py + 0.5) / size
            dx = px + 0.5 - cx
            dy = py + 0.5 - cy
            if dx * dx + dy * dy <= radius * radius:
                color = MOON
            elif (_inside_polygon(nx, ny, LEFT_PAGE)
                    or _inside_polygon(nx, ny, RIGHT_PAGE)):
                color = PAGE
            else:
                color = BG
            raw.extend(color + (255,))
    png = b"\x89PNG\r\n\x1a\n"
    png += _png_chunk(b"IHDR", struct.pack(
        ">IIBBBBB", size, size, 8, 6, 0, 0, 0))
    png += _png_chunk(b"IDAT", zlib.compress(bytes(raw), level=9))
    png += _png_chunk(b"IEND", b"")
    with open(path, "wb") as fh:
        fh.write(png)
    print("wrote %s (%d bytes)" % (path, len(png)))


if __name__ == "__main__":
    root = os.path.join(os.path.dirname(__file__), "..")
    write_png(os.path.join(root, "ICON.PNG"), 64)
    write_png(os.path.join(root, "ICON_256.PNG"), 256)
    ui_images = os.path.join(root, "app", "ui", "images")
    os.makedirs(ui_images, exist_ok=True)
    write_png(os.path.join(ui_images, "icon_64.png"), 64)
    write_png(os.path.join(ui_images, "icon_256.png"), 256)
