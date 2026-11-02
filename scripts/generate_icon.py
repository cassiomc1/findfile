#!/usr/bin/env python3
"""Build the high-resolution macOS AppIcon.icns for FindFile.

The artwork mirrors the green-phosphor interface: a dark squircle, a phosphor
green magnifying glass, and an amber lightning bolt.
"""

from __future__ import annotations

import math
import shutil
import subprocess
import sys
from pathlib import Path

from PIL import Image, ImageDraw

try:  # Pillow 10+ renamed the constants.
    LANCZOS = Image.Resampling.LANCZOS
except AttributeError:  # pragma: no cover - older Pillow
    LANCZOS = Image.LANCZOS

SIZE = 1024
MARGIN = 82
CORNER_RADIUS = 204
ICON_SIZES = [
    (16, 1),
    (16, 2),
    (32, 1),
    (32, 2),
    (128, 1),
    (128, 2),
    (256, 1),
    (256, 2),
    (512, 1),
    (512, 2),
]


def _gradient() -> Image.Image:
    """A small diagonal gradient, upscaled to keep generation fast and smooth."""
    scale = 128
    small = Image.new("RGB", (scale, scale))
    pixels = small.load()
    for y in range(scale):
        for x in range(scale):
            factor = (x * 0.4 + y * 0.6) / scale
            pixels[x, y] = (
                int(3 * (1 - factor) + 10 * factor),
                int(9 * (1 - factor) + 34 * factor),
                int(5 * (1 - factor) + 16 * factor),
            )
    return small.resize((SIZE, SIZE), LANCZOS)


def _squircle_mask() -> Image.Image:
    mask = Image.new("L", (SIZE, SIZE), 0)
    ImageDraw.Draw(mask).rounded_rectangle(
        [MARGIN, MARGIN, SIZE - MARGIN, SIZE - MARGIN],
        radius=CORNER_RADIUS,
        fill=255,
    )
    return mask


def _draw_lens(draw: ImageDraw.ImageDraw) -> None:
    center_x, center_y = 470, 470
    radius = 175
    ring_width = 32

    # Dark green glass with a lighter center.
    draw.ellipse(
        [center_x - radius + 12, center_y - radius + 12,
         center_x + radius - 12, center_y + radius - 12],
        fill=(6, 30, 13, 255),
    )
    draw.ellipse(
        [center_x - radius * 0.62, center_y - radius * 0.62,
         center_x + radius * 0.62, center_y + radius * 0.62],
        fill=(10, 46, 20, 255),
    )

    # Phosphor ring.
    draw.ellipse(
        [center_x - radius, center_y - radius,
         center_x + radius, center_y + radius],
        outline=(51, 255, 51, 255),
        width=ring_width,
    )

    # Handle at 45 degrees, with a rounded tip.
    angle = math.radians(45)
    start_x = center_x + int((radius - 4) * math.cos(angle))
    start_y = center_y + int((radius - 4) * math.sin(angle))
    end_x = start_x + 205
    end_y = start_y + 205

    draw.line(
        [(start_x, start_y), (end_x, end_y)],
        fill=(51, 255, 51, 255),
        width=ring_width,
    )
    half_width = ring_width // 2
    draw.ellipse(
        [end_x - half_width, end_y - half_width,
         end_x + half_width, end_y + half_width],
        fill=(51, 255, 51, 255),
    )

    # Amber bolt.
    bolt = [
        (center_x + 8, center_y - 95),
        (center_x - 52, center_y - 8),
        (center_x - 8, center_y - 8),
        (center_x - 28, center_y + 92),
        (center_x + 62, center_y - 8),
        (center_x + 16, center_y - 8),
    ]
    draw.polygon(bolt, fill=(255, 176, 0, 255))


def generate_icon(output_icns: Path) -> None:
    output_icns = output_icns.expanduser().resolve()
    output_icns.parent.mkdir(parents=True, exist_ok=True)

    base = Image.new("RGBA", (SIZE, SIZE), (0, 0, 0, 0))
    base.paste(_gradient(), (0, 0), _squircle_mask())

    draw = ImageDraw.Draw(base)
    draw.rounded_rectangle(
        [MARGIN, MARGIN, SIZE - MARGIN, SIZE - MARGIN],
        radius=CORNER_RADIUS,
        outline=(51, 255, 51, 95),
        width=5,
    )
    _draw_lens(draw)

    iconset_dir = output_icns.parent / f"{output_icns.stem}.iconset"
    if iconset_dir.exists():
        shutil.rmtree(iconset_dir)
    iconset_dir.mkdir(parents=True)

    for size, scale in ICON_SIZES:
        target_size = size * scale
        resized = base.resize((target_size, target_size), LANCZOS)
        filename = f"icon_{size}x{size}@2x.png" if scale == 2 else f"icon_{size}x{size}.png"
        resized.save(iconset_dir / filename)

    if shutil.which("iconutil") is None:
        raise SystemExit("iconutil is required to build an .icns file (macOS only).")

    subprocess.run(
        ["iconutil", "-c", "icns", str(iconset_dir), "-o", str(output_icns)],
        check=True,
    )
    shutil.rmtree(iconset_dir)
    print(f"Icon written: {output_icns}")


if __name__ == "__main__":
    destination = Path(sys.argv[1]) if len(sys.argv) > 1 else Path("AppIcon.icns")
    generate_icon(destination)
