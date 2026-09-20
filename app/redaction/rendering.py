"""Mask appearance, explicit regions, and destructive pixel replacement."""

import math
import re
from pathlib import Path

from PIL import Image, ImageDraw, ImageFont

BLACK = {"mode": "black"}


def normalize_style(value, base_dir=None):
    if not isinstance(value, dict):
        raise ValueError("Mask style must be a mapping.")  # noqa: TRY004
    mode = value.get("mode", "black")
    allowed = {
        "black": {"mode"},
        "fill": {"mode", "background"},
        "text": {"mode", "text", "background", "foreground", "font_path"},
    }
    if not isinstance(mode, str) or mode not in allowed or set(value) - allowed[mode]:
        raise ValueError(
            "Invalid mask style: use black, fill, or text with the appropriate options."
        )
    style = {"mode": mode}
    if mode != "black":
        style["background"] = value.get("background", "#FFFFFF")
    if mode == "text":
        text = value.get("text", "XXXXX")
        if (
            not isinstance(text, str)
            or not text.strip()
            or len(text) > 200
            or any(ord(c) < 32 for c in text)
        ):
            raise ValueError(
                "Replacement text must be a non-empty single line, at most 200 characters."
            )
        style.update(text=text, foreground=value.get("foreground", "#000000"))
        if "font_path" in value:
            if not isinstance(value["font_path"], str) or not value["font_path"]:
                raise ValueError("font_path must be a font file path.")
            path = Path(value["font_path"])
            if base_dir is not None and not path.is_absolute():
                path = base_dir / path
            style["font_path"] = str(path.resolve())
            try:
                ImageFont.truetype(style["font_path"], size=12)
            except OSError as exc:
                raise ValueError(f"Cannot load replacement font: {path}") from exc
        elif not text.isascii():
            raise ValueError(
                "Non-ASCII replacement text requires font_path pointing to a font with the required glyphs."
            )
    for key in ("background", "foreground"):
        if key in style and (
            not isinstance(style[key], str)
            or not re.fullmatch(r"#[0-9a-fA-F]{6}", style[key])
        ):
            raise ValueError(
                f"{key} must be an opaque #RRGGBB color (quote it in YAML)."
            )
    if mode == "text" and style["background"].lower() == style["foreground"].lower():
        raise ValueError("Replacement text foreground and background must differ.")
    return style


def validate_mask(mask, page_count):
    required = {"page", "x", "y", "w", "h"}
    if (
        not isinstance(mask, dict)
        or not required <= set(mask)
        or set(mask) - required - {"style"}
    ):
        raise ValueError("Each mask needs page, x, y, w, h and optionally style.")
    if type(mask["page"]) is not int or not 0 <= mask["page"] < page_count:
        raise ValueError("Invalid mask page: page indexes start at zero.")
    values = [mask[k] for k in ("x", "y", "w", "h")]
    if not all(type(v) in (int, float) and math.isfinite(v) for v in values):
        raise ValueError("Mask coordinates must be finite numbers.")
    x, y, w, h = values
    if not (
        0 <= x < 1
        and 0 <= y < 1
        and 0 < w <= 1
        and 0 < h <= 1
        and x + w <= 1.0000001
        and y + h <= 1.0000001
    ):
        raise ValueError(
            "Mask coordinates must stay inside the page, normalized from 0 to 1."
        )
    normalize_style(mask.get("style", BLACK))


def parse_masking(value, base_dir):
    if not isinstance(value, dict) or set(value) - {"default", "fields", "regions"}:
        raise ValueError("masking accepts default, fields, and regions only.")
    default = normalize_style(value.get("default", BLACK), base_dir)
    raw_fields = value.get("fields", {})
    if not isinstance(raw_fields, dict) or not all(
        isinstance(k, str) and k for k in raw_fields
    ):
        raise ValueError("masking.fields must map finding names to styles.")
    fields = {
        key: normalize_style(style, base_dir) for key, style in raw_fields.items()
    }
    raw_regions = value.get("regions", [])
    if not isinstance(raw_regions, list) or len(raw_regions) > 20000:
        raise ValueError("masking.regions must be a list of up to 20,000 rectangles.")
    regions = []
    for raw in raw_regions:
        if not isinstance(raw, dict):
            raise ValueError("Each configured region must be a mapping.")  # noqa: TRY004
        region = {**raw, "style": normalize_style(raw.get("style", default), base_dir)}
        # Geometry is checked now; actual page existence is checked after PDF loading.
        validate_mask(region, 100)
        regions.append(region)
    return {"default": default, "fields": fields, "regions": regions}


def paint_mask(image, mask):
    """Paste a wholly new opaque patch; no original pixels enter the replacement."""
    style = normalize_style(mask.get("style", BLACK))
    left, top = (
        math.floor(mask["x"] * image.width),
        math.floor(mask["y"] * image.height),
    )
    right = min(image.width, math.ceil((mask["x"] + mask["w"]) * image.width))
    bottom = min(image.height, math.ceil((mask["y"] + mask["h"]) * image.height))
    width, height = right - left, bottom - top
    if style["mode"] == "black":
        ImageDraw.Draw(image).rectangle(
            (left, top, right - 1, bottom - 1), fill="black"
        )
        return
    patch = Image.new("RGB", (width, height), style["background"])
    if style["mode"] == "text":
        # Render at a comfortable size then shrink to fit, never outside the mask.
        size = max(12, min(height, 256))
        font = (
            ImageFont.truetype(style["font_path"], size=size)
            if "font_path" in style
            else ImageFont.load_default(size=size)
        )
        box = font.getbbox(style["text"])
        glyphs = Image.new("L", (max(1, box[2] - box[0]), max(1, box[3] - box[1])), 0)
        ImageDraw.Draw(glyphs).text(
            (-box[0], -box[1]), style["text"], font=font, fill=255
        )
        available_w, available_h = max(1, width - 4), max(1, height - 4)
        ratio = min(1, available_w / glyphs.width, available_h / glyphs.height)
        scaled = glyphs.resize(
            (max(1, int(glyphs.width * ratio)), max(1, int(glyphs.height * ratio))),
            Image.Resampling.LANCZOS,
        )
        patch.paste(
            style["foreground"],
            (
                (width - scaled.width) // 2,
                (height - scaled.height) // 2,
                (width + scaled.width) // 2,
                (height + scaled.height) // 2,
            ),
            scaled,
        )
        glyphs.close()
        scaled.close()
    image.paste(patch, (left, top))
    patch.close()
