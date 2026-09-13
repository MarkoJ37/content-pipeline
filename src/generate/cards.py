"""Render TEXT_CARD shots as PNG stills with Pillow. Free, local.

Cards are rendered at 1350x2400 (9:16 with headroom) so the Ken Burns zoom in
assemble.py never runs out of pixels at 1080x1920.
"""

from __future__ import annotations

from pathlib import Path

from PIL import Image, ImageDraw, ImageFont

from ..lib import brand as branding

CARD_W, CARD_H = 1350, 2400

# Dark, brand-neutral gradients cycled per shot so consecutive cards differ.
GRADIENTS = [
    ((16, 18, 46), (58, 28, 96)),  # navy -> violet
    ((10, 34, 40), (16, 88, 82)),  # ink -> teal
    ((40, 14, 30), (110, 32, 60)),  # plum -> raspberry
    ((20, 22, 28), (60, 66, 84)),  # charcoal -> slate
]
ACCENT = (255, 196, 61)

_FONT_CANDIDATES = ["arialbd.ttf", "DejaVuSans-Bold.ttf", "LiberationSans-Bold.ttf", "seguisb.ttf"]


def _load_font(size: int) -> ImageFont.FreeTypeFont:
    for name in _FONT_CANDIDATES:
        try:
            return ImageFont.truetype(name, size)
        except OSError:
            continue
    return ImageFont.load_default(size)


def _gradient(index: int) -> Image.Image:
    top, bottom = GRADIENTS[index % len(GRADIENTS)]
    img = Image.new("RGB", (CARD_W, CARD_H))
    for y in range(CARD_H):
        t = y / (CARD_H - 1)
        row = tuple(round(top[c] + (bottom[c] - top[c]) * t) for c in range(3))
        img.paste(Image.new("RGB", (CARD_W, 1), row), (0, y))
    return img


def _fit_font(
    draw: ImageDraw.ImageDraw, lines: list[str], max_width: int
) -> ImageFont.FreeTypeFont:
    size = 170
    while size > 40:
        font = _load_font(size)
        if all(draw.textlength(line, font=font) <= max_width for line in lines):
            return font
        size -= 10
    return _load_font(40)


def render_card(text: str, index: int, out_path: str | Path, brand: dict | None = None) -> Path:
    """Render one card. `text` uses \\n for author-controlled line breaks."""
    style = branding.validate(brand)
    img = Image.new("RGB", (CARD_W, CARD_H), style["background"]) if brand else _gradient(index)
    draw = ImageDraw.Draw(img)
    lines = [line for line in text.split("\n") if line.strip()]
    if not lines:
        raise ValueError("Text card cannot be empty")
    # Wrap long copy by measured width and reserve room for a lower caption-safe area.
    size = 150
    while True:
        font = branding.font(style["font"], size)
        wrapped = []
        for line in lines:
            current = ""
            for word in line.split():
                if draw.textlength(word, font=font) > CARD_W - 300:
                    current = None
                    break
                candidate = f"{current} {word}".strip()
                if current and draw.textlength(candidate, font=font) > CARD_W - 300:
                    wrapped.append(current)
                    current = word
                else:
                    current = candidate
            if current is None:
                break
            if current:
                wrapped.append(current)
        if current is not None and len(wrapped) <= 5:
            lines = wrapped
            break
        size -= 10
        if size < 60:
            raise ValueError("Text card has too much copy; shorten it")

    line_height = round(font.size * 1.25)
    block_height = line_height * len(lines)
    y = (CARD_H - block_height) // 2

    for line in lines:
        width = draw.textlength(line, font=font)
        draw.text(
            ((CARD_W - width) / 2, y),
            line,
            font=font,
            fill=style["card_color"],
            stroke_width=2,
            stroke_fill=(0, 0, 0),
        )
        y += line_height

    bar_w = 220
    bar_y = (CARD_H + block_height) // 2 + 90
    draw.rounded_rectangle(
        [(CARD_W - bar_w) // 2, bar_y, (CARD_W + bar_w) // 2, bar_y + 14],
        radius=7,
        fill=style["accent"],
    )

    out_path = Path(out_path)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    img.save(out_path)
    return out_path
