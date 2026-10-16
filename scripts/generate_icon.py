#!/usr/bin/env python3
"""
Build the high-resolution macOS AppIcon.icns for FindFile.
"""

import math
import os
import shutil
import subprocess
import sys
from pathlib import Path
from PIL import Image, ImageDraw


def generate_icon(output_icns: Path) -> None:
    SIZE = 1024
    
    # 1. Transparent base image
    canvas = Image.new("RGBA", (SIZE, SIZE), (0, 0, 0, 0))

    # 2. Dark slate / deep navy background gradient
    bg = Image.new("RGBA", (SIZE, SIZE), (0, 0, 0, 0))
    for y in range(SIZE):
        for x in range(SIZE):
            factor = (x * 0.4 + y * 0.6) / SIZE
            r = int(22 * (1 - factor) + 10 * factor)
            g = int(30 * (1 - factor) + 14 * factor)
            b = int(46 * (1 - factor) + 22 * factor)
            bg.putpixel((x, y), (r, g, b, 255))

    # 3. macOS squircle mask
    # Radius and proportions close to a 1024x1024 Apple icon
    mask = Image.new("L", (SIZE, SIZE), 0)
    mask_draw = ImageDraw.Draw(mask)
    margin = 82
    mask_draw.rounded_rectangle(
        [margin, margin, SIZE - margin, SIZE - margin],
        radius=204,
        fill=255,
    )

    # 4. Apply the mask to the background
    base = Image.new("RGBA", (SIZE, SIZE), (0, 0, 0, 0))
    base.paste(bg, (0, 0), mask)

    # 5. Subtle inner stroke
    border_draw = ImageDraw.Draw(base)
    border_draw.rounded_rectangle(
        [margin, margin, SIZE - margin, SIZE - margin],
        radius=204,
        outline=(65, 85, 125, 180),
        width=5,
    )

    draw = ImageDraw.Draw(base)

    # 6. Magnifying glass with a lightning bolt
    cx, cy = 470, 470
    radius = 175
    ring_width = 32

    # Soft inner glow of the lens
    draw.ellipse(
        [cx - radius + 14, cy - radius + 14, cx + radius - 14, cy + radius - 14],
        fill=(14, 45, 78, 140),
    )

    # Outer lens ring (electric blue / neon cyan)
    draw.ellipse(
        [cx - radius, cy - radius, cx + radius, cy + radius],
        outline=(0, 210, 255, 255),
        width=ring_width,
    )

    # Handle at 45 degrees
    angle = math.radians(45)
    h_start_x = cx + int((radius - 4) * math.cos(angle))
    h_start_y = cy + int((radius - 4) * math.sin(angle))
    h_end_x = h_start_x + 215
    h_end_y = h_start_y + 215

    draw.line(
        [(h_start_x, h_start_y), (h_end_x, h_end_y)],
        fill=(0, 210, 255, 255),
        width=ring_width,
    )
    # Rounded end of the handle
    half_w = ring_width // 2
    draw.ellipse(
        [h_end_x - half_w, h_end_y - half_w, h_end_x + half_w, h_end_y + half_w],
        fill=(0, 210, 255, 255),
    )

    # Gold bolt
    bolt_points = [
        (cx + 8, cy - 95),
        (cx - 52, cy - 8),
        (cx - 8, cy - 8),
        (cx - 28, cy + 92),
        (cx + 62, cy - 8),
        (cx + 16, cy - 8),
    ]
    draw.polygon(bolt_points, fill=(255, 215, 0, 255))

    # 7. Build the .iconset at the sizes macOS expects
    iconset_dir = output_icns.parent / f"{output_icns.stem}.iconset"
    if iconset_dir.exists():
        shutil.rmtree(iconset_dir)
    iconset_dir.mkdir(parents=True, exist_ok=True)

    sizes = [
        (16, 1), (16, 2),
        (32, 1), (32, 2),
        (128, 1), (128, 2),
        (256, 1), (256, 2),
        (512, 1), (512, 2),
    ]

    for s, scale in sizes:
        target_size = s * scale
        resized = base.resize((target_size, target_size), Image.Resampling.LANCZOS)
        filename = f"icon_{s}x{s}@2x.png" if scale == 2 else f"icon_{s}x{s}.png"
        resized.save(iconset_dir / filename)

    # 8. Compile the .icns with iconutil
    cmd = ["iconutil", "-c", "icns", str(iconset_dir), "-o", str(output_icns)]
    subprocess.run(cmd, check=True)

    # Remove the temporary .iconset directory
    shutil.rmtree(iconset_dir)
    print(f"Icon written: {output_icns}")


if __name__ == "__main__":
    dest = Path(sys.argv[1]) if len(sys.argv) > 1 else Path("AppIcon.icns")
    generate_icon(dest)
