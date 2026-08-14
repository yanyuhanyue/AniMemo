#!/usr/bin/env python3
"""Generate AniMemo-owned default avatar and missing-cover artwork."""

from pathlib import Path

from PIL import Image, ImageDraw


ROOT = Path(__file__).resolve().parents[1]
ASSET_ROOT = ROOT / "public" / "assets"
INK = "#111111"
CREAM = "#FFF5EE"
CORAL = "#FF6B6B"
PINK = "#FF8FAB"
TEAL = "#4ECDC4"
YELLOW = "#FFE66D"


def rounded(draw, box, radius, fill, outline=INK, width=18):
    draw.rounded_rectangle(box, radius=radius, fill=fill, outline=outline, width=width)


def star(draw, cx, cy, size, fill=YELLOW):
    points = [
        (cx, cy - size),
        (cx + size * 0.28, cy - size * 0.28),
        (cx + size, cy),
        (cx + size * 0.28, cy + size * 0.28),
        (cx, cy + size),
        (cx - size * 0.28, cy + size * 0.28),
        (cx - size, cy),
        (cx - size * 0.28, cy - size * 0.28),
    ]
    draw.polygon(points, fill=fill, outline=INK)
    draw.line(points + [points[0]], fill=INK, width=max(5, size // 8), joint="curve")


def generate_avatar(path):
    image = Image.new("RGB", (1024, 1024), CREAM)
    draw = ImageDraw.Draw(image)
    draw.ellipse((72, 72, 952, 952), fill=YELLOW, outline=INK, width=24)
    draw.ellipse((122, 122, 902, 902), fill=PINK, outline=INK, width=18)
    rounded(draw, (232, 242, 792, 782), 150, TEAL, width=26)
    draw.polygon([(360, 752), (512, 920), (664, 752)], fill=TEAL, outline=INK)
    draw.line([(360, 752), (512, 920), (664, 752)], fill=INK, width=26, joint="curve")
    draw.ellipse((350, 414, 430, 494), fill=INK)
    draw.ellipse((594, 414, 674, 494), fill=INK)
    draw.arc((406, 450, 618, 628), 15, 165, fill=INK, width=22)
    draw.ellipse((278, 302, 382, 406), fill=CORAL, outline=INK, width=16)
    draw.ellipse((642, 606, 746, 710), fill=YELLOW, outline=INK, width=16)
    star(draw, 784, 230, 82)
    image.save(path, format="PNG", optimize=True)


def generate_fallback(path):
    image = Image.new("RGB", (900, 1200), CREAM)
    draw = ImageDraw.Draw(image)
    draw.rectangle((0, 0, 900, 1200), fill=CREAM)
    for offset, color in ((-120, PINK), (160, YELLOW), (440, TEAL), (720, CORAL)):
        draw.polygon(
            [(offset, 0), (offset + 250, 0), (offset + 20, 1200), (offset - 230, 1200)],
            fill=color,
        )
    draw.rectangle((48, 48, 852, 1152), outline=INK, width=28)
    rounded(draw, (178, 318, 722, 886), 86, CREAM, width=26)
    draw.ellipse((252, 392, 648, 788), fill=TEAL, outline=INK, width=24)
    draw.ellipse((324, 466, 414, 556), fill=CREAM, outline=INK, width=18)
    draw.ellipse((486, 466, 576, 556), fill=CREAM, outline=INK, width=18)
    draw.rounded_rectangle((340, 622, 560, 682), radius=30, fill=INK)
    star(draw, 690, 268, 78)
    draw.line((230, 930, 670, 930), fill=INK, width=24)
    draw.line((300, 1000, 600, 1000), fill=INK, width=18)
    image.save(path, format="WEBP", quality=88, method=6)


def main():
    poster_root = ASSET_ROOT / "posters"
    poster_root.mkdir(parents=True, exist_ok=True)
    generate_avatar(ASSET_ROOT / "avatar.png")
    generate_fallback(poster_root / "poster-01.webp")


if __name__ == "__main__":
    main()
