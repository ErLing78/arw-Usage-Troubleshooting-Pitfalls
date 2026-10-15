#!/usr/bin/env python3
"""pngtool - crop, rotate, scale and preview a PNG with no dependencies.

A deliberately self-contained single file: copy it anywhere and it works, as
long as Python 3.8+ is present.  For a fuller toolkit with palette
quantisation, palette file I/O and format conversion, see stdpng.

Everything here exists because Pillow, ImageMagick, ffmpeg and (sometimes)
PowerShell's Add-Type are all unavailable on the machines this was written for.

Usage
-----
    python pngtool.py in.png --info
    python pngtool.py in.png --out out.png --crop 10,10,64,64 --scale 2
    python pngtool.py in.png --out rotated.png --rotate 90
    python pngtool.py in.png --ascii 120 --mode min
    python pngtool.py in.png --crop 100,300,400,40 --scale 4 --out region.png

Rotation is clockwise for positive values and must be a multiple of 90.
Scaling is nearest-neighbour: this tool is for pixel art and UI assets, where
any interpolation destroys the look.

``--mode`` controls what each ASCII character cell represents:
``mean`` (default) for layout and colour blocks, ``min`` for reading strokes and
text, ``max`` against a dark background.
"""

import argparse
import struct
import sys
import zlib

PNG_SIGNATURE = b"\x89PNG\r\n\x1a\n"
CHANNELS = {0: 1, 2: 3, 3: 1, 4: 2, 6: 4}
RAMP = " .:-=+*#%@"


class PngError(Exception):
    """Raised for anything this tool cannot read or write."""


# ----------------------------------------------------------------------
# decode
# ----------------------------------------------------------------------


def _chunks(data):
    pos = 8
    end = len(data)
    while pos + 8 <= end:
        (length,) = struct.unpack(">I", data[pos : pos + 4])
        ctype = data[pos + 4 : pos + 8]
        if pos + 12 + length > end:
            raise PngError("truncated %s chunk" % ctype.decode("ascii", "replace"))
        yield ctype, data[pos + 8 : pos + 8 + length]
        pos += 12 + length
        if ctype == b"IEND":
            return


def _unfilter(raw, width, height, bit_depth, colour_type):
    channels = CHANNELS[colour_type]
    bpp = max(1, channels * bit_depth // 8)
    stride = (width * channels * bit_depth + 7) // 8
    rows = []
    previous = bytearray(stride)
    pos = 0
    for _ in range(height):
        if pos >= len(raw):
            raise PngError("truncated image data")
        filter_type = raw[pos]
        pos += 1
        line = bytearray(raw[pos : pos + stride])
        if len(line) != stride:
            raise PngError("truncated image data")
        pos += stride

        if filter_type == 1:
            for i in range(bpp, stride):
                line[i] = (line[i] + line[i - bpp]) & 0xFF
        elif filter_type == 2:
            for i in range(stride):
                line[i] = (line[i] + previous[i]) & 0xFF
        elif filter_type == 3:
            for i in range(stride):
                left = line[i - bpp] if i >= bpp else 0
                line[i] = (line[i] + ((left + previous[i]) >> 1)) & 0xFF
        elif filter_type == 4:
            for i in range(stride):
                a = line[i - bpp] if i >= bpp else 0
                b = previous[i]
                c = previous[i - bpp] if i >= bpp else 0
                pa, pb, pc = abs(b - c), abs(a - c), abs(a + b - 2 * c)
                predictor = a if (pa <= pb and pa <= pc) else (b if pb <= pc else c)
                line[i] = (line[i] + predictor) & 0xFF
        elif filter_type != 0:
            raise PngError("unknown filter type %d" % filter_type)

        rows.append(line)
        previous = line
    return rows


def read_png(path):
    """Decode to ``(width, height, bytearray RGBA)``."""
    with open(path, "rb") as handle:
        data = handle.read()
    if data[:8] != PNG_SIGNATURE:
        raise PngError("not a PNG file (bad signature)")

    width = height = bit_depth = colour_type = interlace = None
    palette = None
    transparency = None
    idat = bytearray()

    for ctype, payload in _chunks(data):
        if ctype == b"IHDR":
            width, height, bit_depth, colour_type, _c, _f, interlace = struct.unpack(
                ">IIBBBBB", payload
            )
        elif ctype == b"PLTE":
            palette = bytes(payload)
        elif ctype == b"tRNS":
            transparency = bytes(payload)
        elif ctype == b"IDAT":
            idat += payload

    if width is None:
        raise PngError("missing IHDR chunk")
    if bit_depth != 8:
        raise PngError("only 8-bit PNGs are supported (got bit depth %s)" % bit_depth)
    if interlace != 0:
        raise PngError("interlaced (Adam7) PNGs are not supported")
    if not idat:
        raise PngError("missing IDAT chunk")

    try:
        raw = zlib.decompress(bytes(idat))
    except zlib.error as exc:
        raise PngError("corrupt image data: %s" % exc)

    rows = _unfilter(raw, width, height, bit_depth, colour_type)
    channels = CHANNELS[colour_type]
    out = bytearray(width * height * 4)
    palette_alpha = transparency if colour_type == 3 and transparency else None

    for y in range(height):
        row = rows[y]
        base = y * width * 4
        if colour_type == 2:
            for x in range(width):
                o = x * 3
                d = base + x * 4
                out[d : d + 3] = row[o : o + 3]
                out[d + 3] = 255
        elif colour_type == 6:
            out[base : base + width * 4] = row
        elif colour_type == 0:
            for x in range(width):
                v = row[x]
                d = base + x * 4
                out[d] = out[d + 1] = out[d + 2] = v
                out[d + 3] = 255
        elif colour_type == 4:
            for x in range(width):
                o = x * 2
                v = row[o]
                d = base + x * 4
                out[d] = out[d + 1] = out[d + 2] = v
                out[d + 3] = row[o + 1]
        else:  # indexed
            if palette is None:
                raise PngError("indexed PNG without a PLTE chunk")
            for x in range(width):
                index = row[x]
                o = index * 3
                d = base + x * 4
                out[d] = palette[o]
                out[d + 1] = palette[o + 1]
                out[d + 2] = palette[o + 2]
                if palette_alpha is not None and index < len(palette_alpha):
                    out[d + 3] = palette_alpha[index]
                else:
                    out[d + 3] = 255

    return width, height, out


# ----------------------------------------------------------------------
# encode
# ----------------------------------------------------------------------


def _chunk(ctype, payload):
    return (
        struct.pack(">I", len(payload))
        + ctype
        + payload
        + struct.pack(">I", zlib.crc32(ctype + payload) & 0xFFFFFFFF)
    )


def write_png(path, width, height, pixels):
    """Write 8-bit RGBA.  Filter type 0 throughout: simple and adequate here."""
    stride = width * 4
    raw = bytearray((stride + 1) * height)
    for y in range(height):
        dst = y * (stride + 1)
        raw[dst] = 0
        raw[dst + 1 : dst + 1 + stride] = pixels[y * stride : (y + 1) * stride]

    out = bytearray(PNG_SIGNATURE)
    out += _chunk(b"IHDR", struct.pack(">IIBBBBB", width, height, 8, 6, 0, 0, 0))
    out += _chunk(b"IDAT", zlib.compress(bytes(raw), 9))
    out += _chunk(b"IEND", b"")
    with open(path, "wb") as handle:
        handle.write(out)


# ----------------------------------------------------------------------
# operations
# ----------------------------------------------------------------------


def crop(width, height, pixels, x, y, w, h):
    x0 = max(0, min(x, width))
    y0 = max(0, min(y, height))
    x1 = max(x0, min(x + w, width))
    y1 = max(y0, min(y + h, height))
    if x1 <= x0 or y1 <= y0:
        raise PngError("crop rectangle lies outside the image")

    new_w = x1 - x0
    new_h = y1 - y0
    out = bytearray(new_w * new_h * 4)
    for row in range(new_h):
        src = ((y0 + row) * width + x0) * 4
        out[row * new_w * 4 : (row + 1) * new_w * 4] = pixels[src : src + new_w * 4]
    return new_w, new_h, out


def rotate(width, height, pixels, degrees):
    degrees %= 360
    if degrees == 0:
        return width, height, bytearray(pixels)

    if degrees == 180:
        out = bytearray(len(pixels))
        count = width * height
        for i in range(count):
            s = i * 4
            d = (count - 1 - i) * 4
            out[d : d + 4] = pixels[s : s + 4]
        return width, height, out

    out = bytearray(width * height * 4)
    if degrees == 90:  # clockwise
        for y in range(height):
            for x in range(width):
                sx, sy = x, height - 1 - y
                out[(x * height + y) * 4 : (x * height + y) * 4 + 4] = pixels[
                    (sy * width + sx) * 4 : (sy * width + sx) * 4 + 4
                ]
        return height, width, out
    if degrees == 270:  # counter-clockwise
        for y in range(height):
            for x in range(width):
                sx, sy = width - 1 - x, y
                out[(x * height + y) * 4 : (x * height + y) * 4 + 4] = pixels[
                    (sy * width + sx) * 4 : (sy * width + sx) * 4 + 4
                ]
        return height, width, out
    raise PngError("rotation must be a multiple of 90 degrees")


def scale(width, height, pixels, factor):
    """Nearest-neighbour upscale.  ``factor`` may be fractional."""
    if factor <= 0:
        raise PngError("scale factor must be positive")
    if factor == 1:
        return width, height, bytearray(pixels)

    new_w = max(1, int(width * factor))
    new_h = max(1, int(height * factor))
    out = bytearray(new_w * new_h * 4)
    xmap = [min(width - 1, int(x / factor)) * 4 for x in range(new_w)]
    for y in range(new_h):
        sy = min(height - 1, int(y / factor))
        src_row = sy * width * 4
        dst_row = y * new_w * 4
        for x in range(new_w):
            s = src_row + xmap[x]
            d = dst_row + x * 4
            out[d : d + 4] = pixels[s : s + 4]
    return new_w, new_h, out


def to_ascii(width, height, pixels, columns=120, mode="mean", invert=False):
    """Render as text.  'mean' for blocks, 'min' for strokes, 'max' against dark."""
    rows = max(1, int(columns * height / width / 2.05))
    cell_w = width / columns
    cell_h = height / rows
    lines = []

    for row in range(rows):
        y0 = int(row * cell_h)
        y1 = max(y0 + 1, int((row + 1) * cell_h))
        chars = []
        for col in range(columns):
            x0 = int(col * cell_w)
            x1 = max(x0 + 1, int((col + 1) * cell_w))
            total = count = 0
            darkest, lightest = 255, 0
            for y in range(y0, min(y1, height)):
                base = y * width * 4
                for x in range(x0, min(x1, width)):
                    o = base + x * 4
                    lum = (pixels[o] * 299 + pixels[o + 1] * 587 + pixels[o + 2] * 114) // 1000
                    total += lum
                    count += 1
                    if lum < darkest:
                        darkest = lum
                    if lum > lightest:
                        lightest = lum
            if count == 0:
                chars.append(" ")
                continue
            value = total // count if mode == "mean" else (darkest if mode == "min" else lightest)
            if invert:
                value = 255 - value
            chars.append(RAMP[int((255 - value) / 255 * (len(RAMP) - 1))])
        lines.append("".join(chars))
    return "\n".join(lines)


def describe(width, height, pixels):
    colours = set()
    transparent = 0
    for i in range(0, len(pixels), 4):
        colours.add((pixels[i], pixels[i + 1], pixels[i + 2]))
        if pixels[i + 3] == 0:
            transparent += 1
    parts = ["%dx%d" % (width, height), "%d colours" % len(colours)]
    if transparent:
        parts.append("%.1f%% transparent" % (transparent * 100.0 / (width * height)))
    return ", ".join(parts)


# ----------------------------------------------------------------------
# cli
# ----------------------------------------------------------------------


def _parse_crop(text):
    parts = [p.strip() for p in text.split(",")]
    if len(parts) != 4:
        raise argparse.ArgumentTypeError("crop must be x,y,width,height")
    try:
        return tuple(int(p) for p in parts)
    except ValueError:
        raise argparse.ArgumentTypeError("crop values must be integers")


def main(argv=None):
    parser = argparse.ArgumentParser(
        description="Crop, rotate, scale and preview PNG files with no dependencies."
    )
    parser.add_argument("input", help="source PNG")
    parser.add_argument("--out", help="write the result here")
    parser.add_argument("--crop", type=_parse_crop, metavar="X,Y,W,H")
    parser.add_argument("--rotate", type=int, default=0, help="0, 90, 180 or 270 (clockwise)")
    parser.add_argument("--scale", type=float, default=1.0, help="nearest-neighbour factor")
    parser.add_argument("--ascii", type=int, default=0, metavar="COLS", help="render as ASCII")
    parser.add_argument("--mode", choices=("mean", "min", "max"), default="mean")
    parser.add_argument("--invert", action="store_true")
    parser.add_argument("--info", action="store_true", help="print dimensions and colour count")
    args = parser.parse_args(argv)

    try:
        width, height, pixels = read_png(args.input)
        if args.info:
            print("%s: %s" % (args.input, describe(width, height, pixels)))

        if args.crop:
            width, height, pixels = crop(width, height, pixels, *args.crop)
        if args.rotate:
            width, height, pixels = rotate(width, height, pixels, args.rotate)
        if args.scale and args.scale != 1.0:
            width, height, pixels = scale(width, height, pixels, args.scale)

        if args.out:
            write_png(args.out, width, height, pixels)
            print("wrote %s (%dx%d)" % (args.out, width, height))

        if args.ascii:
            print(to_ascii(width, height, pixels, args.ascii, args.mode, args.invert))
    except (PngError, OSError) as exc:
        print("error: %s" % exc, file=sys.stderr)
        return 1

    if not (args.out or args.ascii or args.info):
        parser.print_help()
    return 0


if __name__ == "__main__":
    sys.exit(main())
