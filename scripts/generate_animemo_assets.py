"""Generate the original AniMemo brand avatar and missing-poster fallback.

The artwork is intentionally code-generated from simple geometric primitives so
the repository has a reproducible, self-owned source for the two bundled
assets.  No anime artwork, external media, or third-party image is sampled.
"""

from __future__ import annotations

import argparse
import random
from pathlib import Path

from PIL import Image, ImageDraw, ImageFont


PALETTE = {
    "ink": (17, 17, 17, 255),
    "navy": (24, 38, 74, 255),
    "cream": (255, 245, 238, 255),
    "paper": (251, 247, 239, 255),
    "coral": (255, 107, 107, 255),
    "teal": (78, 205, 196, 255),
    "yellow": (255, 230, 109, 255),
    "mint": (189, 240, 218, 255),
    "lavender": (224, 213, 246, 255),
}


def _font(size: int) -> ImageFont.FreeTypeFont | ImageFont.ImageFont:
    candidates = [
        Path("C:/Windows/Fonts/arialbd.ttf"),
        Path("C:/Windows/Fonts/seguisb.ttf"),
        Path("C:/Windows/Fonts/DejaVuSans-Bold.ttf"),
    ]
    for candidate in candidates:
        if candidate.is_file():
            return ImageFont.truetype(str(candidate), size)
    return ImageFont.load_default()


def _paper_texture(image: Image.Image, seed: int, strength: int = 5) -> None:
    rng = random.Random(seed)
    pixels = image.load()
    for y in range(image.height):
        for x in range(image.width):
            if rng.random() > 0.035:
                continue
            r, g, b, a = pixels[x, y]
            delta = rng.randint(-strength, strength)
            pixels[x, y] = (
                max(0, min(255, r + delta)),
                max(0, min(255, g + delta)),
                max(0, min(255, b + delta)),
                a,
            )


def _rounded_polygon(draw: ImageDraw.ImageDraw, points, radius: int, fill, outline, width: int) -> None:
    # Drawing a rounded hull with a thick outline keeps the mascot crisp at
    # favicon scale without depending on a vector renderer.
    draw.polygon(points, fill=fill)
    draw.line(points + [points[0]], fill=outline, width=width, joint="curve")
    for x, y in points:
        draw.ellipse((x - radius, y - radius, x + radius, y + radius), fill=fill, outline=outline, width=width)


def make_avatar(size: int = 512) -> Image.Image:
    image = Image.new("RGBA", (size, size), PALETTE["paper"])
    draw = ImageDraw.Draw(image)
    cx = cy = size // 2
    scale = size / 512
    ink = PALETTE["ink"]

    draw.ellipse((38 * scale, 38 * scale, 474 * scale, 474 * scale), fill=PALETTE["cream"], outline=ink, width=max(4, round(10 * scale)))
    draw.arc((70 * scale, 62 * scale, 442 * scale, 434 * scale), 205, 332, fill=PALETTE["yellow"], width=max(5, round(13 * scale)))

    body = [
        (164 * scale, 190 * scale),
        (143 * scale, 120 * scale),
        (207 * scale, 150 * scale),
        (256 * scale, 126 * scale),
        (305 * scale, 150 * scale),
        (369 * scale, 120 * scale),
        (348 * scale, 207 * scale),
        (378 * scale, 282 * scale),
        (351 * scale, 361 * scale),
        (285 * scale, 398 * scale),
        (207 * scale, 392 * scale),
        (154 * scale, 344 * scale),
        (137 * scale, 274 * scale),
    ]
    _rounded_polygon(draw, body, max(3, round(12 * scale)), PALETTE["navy"], ink, max(4, round(9 * scale)))

    # Coral bookmark: a brand cue, not a borrowed character detail.
    bookmark = [
        (296 * scale, 214 * scale),
        (345 * scale, 214 * scale),
        (345 * scale, 341 * scale),
        (320 * scale, 321 * scale),
        (296 * scale, 341 * scale),
    ]
    draw.polygon(bookmark, fill=PALETTE["coral"])
    draw.line(bookmark + [bookmark[0]], fill=ink, width=max(3, round(7 * scale)), joint="curve")

    eye_r = 10 * scale
    draw.ellipse((201 * scale - eye_r, 247 * scale - eye_r, 201 * scale + eye_r, 247 * scale + eye_r), fill=PALETTE["cream"])
    draw.ellipse((271 * scale - eye_r, 247 * scale - eye_r, 271 * scale + eye_r, 247 * scale + eye_r), fill=PALETTE["cream"])
    pupil_r = 4 * scale
    draw.ellipse((204 * scale - pupil_r, 249 * scale - pupil_r, 204 * scale + pupil_r, 249 * scale + pupil_r), fill=ink)
    draw.ellipse((274 * scale - pupil_r, 249 * scale - pupil_r, 274 * scale + pupil_r, 249 * scale + pupil_r), fill=ink)
    draw.arc((226 * scale, 257 * scale, 250 * scale, 279 * scale), 10, 170, fill=PALETTE["coral"], width=max(2, round(5 * scale)))

    star = [(180, 316), (190, 339), (215, 347), (192, 356), (180, 381), (168, 357), (145, 347), (169, 338)]
    star = [(round(x * scale), round(y * scale)) for x, y in star]
    draw.polygon(star, fill=PALETTE["mint"], outline=ink)
    draw.line(star + [star[0]], fill=ink, width=max(2, round(4 * scale)), joint="curve")

    _paper_texture(image, seed=110813, strength=4)
    return image


def make_fallback(width: int = 720, height: int = 1024) -> Image.Image:
    image = Image.new("RGBA", (width, height), PALETTE["paper"])
    draw = ImageDraw.Draw(image)
    sx = width / 720
    sy = height / 1024
    ink = PALETTE["ink"]

    # Abstract memory-card composition; it deliberately contains no real
    # animation character, title, logo, or external media reference.
    draw.rectangle((0, 0, width, height), fill=PALETTE["lavender"])
    draw.polygon([(0, 0), (width, 0), (width, round(210 * sy)), (round(410 * sx), round(320 * sy)), (0, round(220 * sy))], fill=PALETTE["teal"])
    draw.polygon([(0, height), (width, height), (width, round(650 * sy)), (round(310 * sx), round(720 * sy)), (0, round(630 * sy))], fill=PALETTE["coral"])
    draw.ellipse((round(120 * sx), round(158 * sy), round(600 * sx), round(638 * sy)), fill=PALETTE["cream"], outline=ink, width=round(10 * sx))

    card = [
        (round(205 * sx), round(210 * sy)),
        (round(514 * sx), round(210 * sy)),
        (round(559 * sx), round(255 * sy)),
        (round(559 * sx), round(550 * sy)),
        (round(205 * sx), round(550 * sy)),
    ]
    draw.polygon(card, fill=PALETTE["paper"], outline=ink)
    draw.line(card + [card[0]], fill=ink, width=round(9 * sx), joint="curve")
    draw.polygon([(round(514 * sx), round(210 * sy)), (round(514 * sx), round(255 * sy)), (round(559 * sx), round(255 * sy))], fill=PALETTE["yellow"], outline=ink)

    # A small abstract missing-cover mark: bookmark + star inside a frame.
    draw.rounded_rectangle((round(264 * sx), round(286 * sy), round(500 * sx), round(472 * sy)), radius=round(18 * sx), fill=PALETTE["navy"], outline=ink, width=round(8 * sx))
    draw.rectangle((round(348 * sx), round(315 * sy), round(406 * sx), round(435 * sy)), fill=PALETTE["coral"], outline=PALETTE["cream"], width=round(5 * sx))
    draw.polygon([(round(377 * sx), round(395 * sy)), (round(402 * sx), round(430 * sy)), (round(377 * sx), round(414 * sy)), (round(352 * sx), round(430 * sy))], fill=PALETTE["coral"])
    star = [(377, 248), (389, 274), (416, 285), (390, 296), (377, 323), (364, 296), (338, 285), (365, 274)]
    star = [(round(x * sx), round(y * sy)) for x, y in star]
    draw.polygon(star, fill=PALETTE["mint"], outline=ink)
    draw.line(star + [star[0]], fill=ink, width=round(5 * sx), joint="curve")

    font = _font(round(40 * sx))
    label = "COVER\nMISSING"
    bbox = draw.multiline_textbbox((0, 0), label, font=font, spacing=2)
    tx = (width - (bbox[2] - bbox[0])) // 2
    ty = round(695 * sy)
    draw.multiline_text((tx, ty), label, font=font, fill=ink, spacing=2, align="center")
    draw.line((round(190 * sx), round(850 * sy), round(530 * sx), round(850 * sy)), fill=ink, width=round(8 * sx))
    draw.ellipse((round(190 * sx), round(879 * sy), round(235 * sx), round(924 * sy)), fill=PALETTE["yellow"], outline=ink, width=round(4 * sx))
    draw.ellipse((round(252 * sx), round(879 * sy), round(297 * sx), round(924 * sy)), fill=PALETTE["teal"], outline=ink, width=round(4 * sx))
    draw.ellipse((round(314 * sx), round(879 * sy), round(359 * sx), round(924 * sy)), fill=PALETTE["mint"], outline=ink, width=round(4 * sx))
    draw.ellipse((round(376 * sx), round(879 * sy), round(421 * sx), round(924 * sy)), fill=PALETTE["coral"], outline=ink, width=round(4 * sx))

    _paper_texture(image, seed=110814, strength=5)
    return image


def write_assets(output_dir: Path) -> None:
    output_dir.mkdir(parents=True, exist_ok=True)
    (output_dir / "posters").mkdir(parents=True, exist_ok=True)
    avatar = make_avatar()
    fallback = make_fallback()
    avatar.save(output_dir / "avatar.png", format="PNG", optimize=True)
    fallback.save(output_dir / "posters" / "poster-01.webp", format="WEBP", quality=92, method=6)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--output-dir", type=Path, required=True)
    args = parser.parse_args()
    write_assets(args.output_dir)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
