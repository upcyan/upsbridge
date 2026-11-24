"""Render the upsbridge icon PNGs from app/ui/images/icon.svg.

The vector source is the single source of truth; this script only rasterises it
with the system librsvg, so the app icon never drifts from the drawing.
"""

import argparse
import ctypes
from pathlib import Path


SIZES = (64, 256)


def _load():
    rsvg = ctypes.CDLL("librsvg-2.so.2")
    pixbuf = ctypes.CDLL("libgdk_pixbuf-2.0.so.0")
    rsvg.rsvg_handle_new_from_file.argtypes = [ctypes.c_char_p, ctypes.c_void_p]
    rsvg.rsvg_handle_new_from_file.restype = ctypes.c_void_p
    rsvg.rsvg_handle_get_pixbuf.argtypes = [ctypes.c_void_p]
    rsvg.rsvg_handle_get_pixbuf.restype = ctypes.c_void_p
    pixbuf.gdk_pixbuf_scale_simple.argtypes = [ctypes.c_void_p, ctypes.c_int,
                                               ctypes.c_int, ctypes.c_int]
    pixbuf.gdk_pixbuf_scale_simple.restype = ctypes.c_void_p
    pixbuf.gdk_pixbuf_save.argtypes = [ctypes.c_void_p, ctypes.c_char_p,
                                       ctypes.c_char_p, ctypes.c_void_p,
                                       ctypes.c_void_p]
    return rsvg, pixbuf


def render(svg, size):
    rsvg, pixbuf = _load()
    handle = rsvg.rsvg_handle_new_from_file(str(svg).encode(), None)
    if not handle:
        raise SystemExit("cannot read %s" % svg)
    base = rsvg.rsvg_handle_get_pixbuf(handle)
    if not base:
        raise SystemExit("cannot rasterise %s" % svg)
    scaled = pixbuf.gdk_pixbuf_scale_simple(base, size, size, 2)  # 2 = BILINEAR
    if not scaled:
        raise SystemExit("cannot scale to %dpx" % size)
    return scaled, pixbuf


def write(svg, path, size):
    scaled, pixbuf = render(svg, size)
    if not pixbuf.gdk_pixbuf_save(scaled, str(path).encode(), b"png", None, None):
        raise SystemExit("cannot write %s" % path)


def main():
    parser = argparse.ArgumentParser(description="rasterise the app icon")
    parser.add_argument("path", nargs="?", type=Path)
    parser.add_argument("size", nargs="?", type=int)
    args = parser.parse_args()

    app = Path(__file__).resolve().parents[1] / "app"
    svg = app / "ui" / "images" / "icon.svg"
    if args.path is not None:
        if not args.size or args.size < 1:
            parser.error("path requires a positive size")
        write(svg, args.path, args.size)
        return

    desktop = app / "ui" / "images"
    for size in SIZES:
        targets = [desktop / ("logo_%d.png" % size)]
        if size == 64:
            targets.append(app / "ICON.PNG")
        else:
            targets += [app / "ICON_256.PNG", desktop / "logo.png"]
        for target in targets:
            write(svg, target, size)
            print("wrote", target.relative_to(app.parent))


if __name__ == "__main__":
    main()
