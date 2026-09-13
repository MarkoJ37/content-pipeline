"""Validated local styling, shared by cards and captions."""

from __future__ import annotations

import re

from PIL import ImageFont

DEFAULTS = {
    "font": "Arial",
    "color": "#FFFFFF",
    "size": 76,
    "position": 560,
    "background": "#15231D",
    "accent": "#D1EE8A",
    "card_color": "#FFFFFF",
}
FONT_FILES = {
    "Arial": ["arialbd.ttf", "LiberationSans-Bold.ttf", "DejaVuSans-Bold.ttf"],
    "DejaVu Sans": ["DejaVuSans-Bold.ttf", "arialbd.ttf"],
    "DejaVu Serif": ["DejaVuSerif-Bold.ttf", "georgiab.ttf"],
}


def validate(value=None):
    value = {**DEFAULTS, **(value or {})}
    if value["font"] not in FONT_FILES:
        raise ValueError("Unsupported brand font")
    for field in ("color", "background", "accent", "card_color"):
        if not isinstance(value[field], str) or not re.fullmatch(r"#[a-fA-F0-9]{6}", value[field]):
            raise ValueError(f"Invalid {field} color")
    if value["size"] not in (64, 76, 84) or value["position"] not in (360, 560, 800):
        raise ValueError("Invalid caption size or position")
    return value


def font(name, size):
    for file in FONT_FILES[name]:
        try:
            return ImageFont.truetype(file, size)
        except OSError:
            pass
    return ImageFont.load_default(size)
