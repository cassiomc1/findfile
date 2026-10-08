#!/usr/bin/env python3
"""
Gera o ícone AppIcon.icns para o FindFile no formato padrão do macOS (Squircle com alta resolução).
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
    
    # 1. Base image com transparência
    canvas = Image.new("RGBA", (SIZE, SIZE), (0, 0, 0, 0))

    # 2. Gradiente elegante e moderno de fundo (Dark Slate / Deep Navy)
    bg = Image.new("RGBA", (SIZE, SIZE), (0, 0, 0, 0))
    for y in range(SIZE):
        for x in range(SIZE):
            factor = (x * 0.4 + y * 0.6) / SIZE
            r = int(22 * (1 - factor) + 10 * factor)
            g = int(30 * (1 - factor) + 14 * factor)
            b = int(46 * (1 - factor) + 22 * factor)
            bg.putpixel((x, y), (r, g, b, 255))

    # 3. Máscara Squircle padrão do macOS
    # Raio e proporções aproximados da Apple para ícones 1024x1024
    mask = Image.new("L", (SIZE, SIZE), 0)
    mask_draw = ImageDraw.Draw(mask)
    margin = 82
    mask_draw.rounded_rectangle(
        [margin, margin, SIZE - margin, SIZE - margin],
        radius=204,
        fill=255,
    )

    # 4. Aplica a máscara no fundo
    base = Image.new("RGBA", (SIZE, SIZE), (0, 0, 0, 0))
    base.paste(bg, (0, 0), mask)

    # 5. Borda sutil de destaque (inner stroke)
    border_draw = ImageDraw.Draw(base)
    border_draw.rounded_rectangle(
        [margin, margin, SIZE - margin, SIZE - margin],
        radius=204,
        outline=(65, 85, 125, 180),
        width=5,
    )

    draw = ImageDraw.Draw(base)

    # 6. Símbolo: Lupa de Busca Estilizada com Raio Elétrico Central
    cx, cy = 470, 470
    radius = 175
    ring_width = 32

    # Brilho sutil interno da lente
    draw.ellipse(
        [cx - radius + 14, cy - radius + 14, cx + radius - 14, cy + radius - 14],
        fill=(14, 45, 78, 140),
    )

    # Anel externo da lente (Azul Elétrico / Ciano Neon)
    draw.ellipse(
        [cx - radius, cy - radius, cx + radius, cy + radius],
        outline=(0, 210, 255, 255),
        width=ring_width,
    )

    # Cabo da lente a 45 graus
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
    # Ponta arredondada do cabo
    half_w = ring_width // 2
    draw.ellipse(
        [h_end_x - half_w, h_end_y - half_w, h_end_x + half_w, h_end_y + half_w],
        fill=(0, 210, 255, 255),
    )

    # Raio Dourado (Símbolo de Alta Velocidade)
    bolt_points = [
        (cx + 8, cy - 95),
        (cx - 52, cy - 8),
        (cx - 8, cy - 8),
        (cx - 28, cy + 92),
        (cx + 62, cy - 8),
        (cx + 16, cy - 8),
    ]
    draw.polygon(bolt_points, fill=(255, 215, 0, 255))

    # 7. Gera o diretório .iconset com todas as resoluções exigidas pelo macOS
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

    # 8. Executa iconutil do macOS para compilar o .icns
    cmd = ["iconutil", "-c", "icns", str(iconset_dir), "-o", str(output_icns)]
    subprocess.run(cmd, check=True)

    # Limpeza do diretório temporário .iconset
    shutil.rmtree(iconset_dir)
    print(f"✅ Ícone gerado com sucesso: {output_icns}")


if __name__ == "__main__":
    dest = Path(sys.argv[1]) if len(sys.argv) > 1 else Path("AppIcon.icns")
    generate_icon(dest)
