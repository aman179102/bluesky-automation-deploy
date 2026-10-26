from __future__ import annotations

import re
from pathlib import Path

from PIL import Image, ImageDraw, ImageFont

from app.models import PostDraft, Story
from app.text import normalize_space, truncate_words

WIDTH = 1200
HEIGHT = 675

CATEGORY_COLORS = {
    "security": (239, 68, 68),
    "ai": (139, 92, 246),
    "dev": (16, 185, 129),
    "hardware": (245, 158, 11),
    "startups": (236, 72, 153),
    "bigtech": (59, 130, 246),
    "tech": (56, 189, 248),
}

CATEGORY_LABELS = {
    "security": "SECURITY",
    "ai": "AI",
    "dev": "DEVELOPER",
    "hardware": "HARDWARE",
    "startups": "STARTUPS",
    "bigtech": "BIG TECH",
    "tech": "TECH",
}


def _font(name: str, size: int) -> ImageFont.FreeTypeFont | ImageFont.ImageFont:
    path = Path(__file__).resolve().parent.parent / "assets" / "fonts" / name
    try:
        return ImageFont.truetype(str(path), size=size)
    except OSError:
        try:
            return ImageFont.load_default(size=size)
        except TypeError:
            return ImageFont.load_default()


def _wrap(draw: ImageDraw.ImageDraw, text: str, font, max_width: int) -> list[str]:
    words = normalize_space(text).split()
    if not words:
        return []
    lines: list[str] = []
    current = ""
    for word in words:
        candidate = f"{current} {word}".strip()
        if draw.textlength(candidate, font=font) <= max_width:
            current = candidate
            continue
        if current:
            lines.append(current)
        current = word
    if current:
        lines.append(current)
    return lines


def _fit_headline(draw: ImageDraw.ImageDraw, text: str) -> tuple[object, list[str]]:
    max_width = 1030
    for size in (78, 72, 66, 60, 54, 48):
        font = _font("Inter-Bold.ttf", size)
        lines = _wrap(draw, text, font, max_width)
        if len(lines) > 3:
            continue
        bbox = draw.multiline_textbbox(
            (0, 0), "\n".join(lines), font=font, spacing=8
        )
        if bbox[3] - bbox[1] <= 240:
            return font, lines
    font = _font("Inter-Bold.ttf", 48)
    lines = _wrap(draw, text, font, max_width)[:3]
    if lines:
        lines[-1] = truncate_words(lines[-1], 48)
    return font, lines


def _fit_fact(draw: ImageDraw.ImageDraw, text: str) -> tuple[object, list[str]]:
    for size in (30, 27, 24):
        font = _font("Inter-SemiBold.ttf", size)
        lines = _wrap(draw, text, font, 920)
        if len(lines) <= 2:
            return font, lines
    font = _font("Inter-SemiBold.ttf", 24)
    lines = _wrap(draw, text, font, 920)[:2]
    if lines:
        lines[-1] = truncate_words(lines[-1], 56)
    return font, lines


def _hero_parts(fact: str) -> tuple[str, str, int]:
    fact = normalize_space(fact)
    match = re.fullmatch(r"CVSS\s*:?\s*([0-9.]+)", fact, re.IGNORECASE)
    if match:
        return "CVSS", match.group(1), 72
    match = re.fullmatch(r"(CVE-\d{4}-\d{4,})", fact, re.IGNORECASE)
    if match:
        return "CVE", match.group(1).upper(), 52
    match = re.fullmatch(r"(?:Due|Patch by)\s*:?\s*(.+)", fact, re.IGNORECASE)
    if match:
        return "PATCH BY", match.group(1), 52
    match = re.fullmatch(r"Fixed\s*:?\s*(.+)", fact, re.IGNORECASE)
    if match:
        return "FIXED", match.group(1), 38
    match = re.fullmatch(
        r"(?:Price|Cost)\s*:?\s*(\$[0-9][0-9,.]*(?:\s*(?:billion|million))?)",
        fact,
        re.IGNORECASE,
    )
    if match:
        return "PRICE", match.group(1), 62
    match = re.fullmatch(r"Version\s*:?\s*(.+)", fact, re.IGNORECASE)
    if match:
        return "VERSION", match.group(1), 52
    match = re.fullmatch(r"(\$[0-9][0-9,.]*(?:\s*(?:billion|million))?)", fact, re.IGNORECASE)
    if match:
        return "PRICE", match.group(1), 62
    match = re.fullmatch(
        r"([0-9][0-9,.]*(?:\s*(?:million|billion))?%?)\s+([A-Za-z][A-Za-z -]+)",
        fact,
    )
    if match and match.group(2).strip().lower() in {
        "countries", "new countries", "advertisers", "users", "employees",
        "customers", "devices", "cameras", "people", "companies", "models",
        "markets", "subscribers",
    }:
        return match.group(2).strip().upper(), match.group(1), 62
    return "KEY DETAIL", fact, 38


def _fit_hero_value(
    draw: ImageDraw.ImageDraw, text: str, preferred_size: int
) -> tuple[object, list[str]]:
    sizes = [preferred_size]
    sizes.extend(size for size in (52, 46, 40, 34, 30, 26) if size < preferred_size)
    for size in sizes:
        font = _font("Inter-Bold.ttf", size)
        lines = _wrap(draw, text, font, 900)
        if len(lines) <= 2:
            bbox = draw.multiline_textbbox((0, 0), "\n".join(lines), font=font, spacing=4)
            if bbox[3] - bbox[1] <= 104:
                return font, lines
    font = _font("Inter-Bold.ttf", 26)
    lines = _wrap(draw, text, font, 900)[:2]
    if lines:
        lines[-1] = truncate_words(lines[-1], 48)
    return font, lines


def _gradient_background(accent: tuple[int, int, int]) -> Image.Image:
    base = (8, 15, 28)
    tint = tuple(min(255, int(channel * 0.22)) for channel in accent)
    image = Image.new("RGB", (WIDTH, HEIGHT), base)
    pixels = image.load()
    for y in range(HEIGHT):
        ratio = y / max(HEIGHT - 1, 1)
        color = (
            int(base[0] + (tint[0] - base[0]) * ratio),
            int(base[1] + (tint[1] - base[1]) * ratio),
            int(base[2] + (tint[2] - base[2]) * ratio),
        )
        for x in range(WIDTH):
            pixels[x, y] = color
    return image


def render_image(
    draft: PostDraft,
    story: Story,
    output_path: Path,
    handle: str = "aman179102.bsky.social",
) -> Path:
    output_path.parent.mkdir(parents=True, exist_ok=True)
    accent = CATEGORY_COLORS.get(draft.category, CATEGORY_COLORS["tech"])
    image = _gradient_background(accent).convert("RGB")
    overlay = Image.new("RGBA", image.size, (0, 0, 0, 0))
    draw = ImageDraw.Draw(overlay)

    for offset in range(-HEIGHT, WIDTH, 46):
        draw.line(
            [(offset, HEIGHT), (offset + HEIGHT, 0)],
            fill=(255, 255, 255, 7),
            width=1,
        )
    draw.rectangle([0, 0, WIDTH, 7], fill=(*accent, 255))
    draw.rectangle([0, HEIGHT - 7, WIDTH, HEIGHT], fill=(*accent, 255))
    draw.rectangle([55, 120, 62, 585], fill=(*accent, 255))

    image = Image.alpha_composite(image.convert("RGBA"), overlay).convert("RGB")
    draw = ImageDraw.Draw(image)

    label_font = _font("JetBrainsMono-Medium.ttf", 22)
    meta_font = _font("Inter-SemiBold.ttf", 20)
    footer_font = _font("Inter-Regular.ttf", 21)

    label = CATEGORY_LABELS.get(draft.category, "TECH")
    draw.text((92, 46), label, font=label_font, fill=(*accent, 255))
    automated = "AUTOMATED TECH NEWS"
    automated_width = draw.textlength(automated, font=meta_font)
    draw.text(
        (WIDTH - 75 - automated_width, 47),
        automated,
        font=meta_font,
        fill=(190, 200, 218),
    )

    headline_font, headline_lines = _fit_headline(draw, draft.image_title)
    headline_text = "\n".join(headline_lines)
    draw.multiline_text(
        (92, 132),
        headline_text,
        font=headline_font,
        fill=(248, 250, 252),
        spacing=8,
    )

    facts = [normalize_space(fact) for fact in draft.image_facts[:3] if normalize_space(fact)]
    gap = 10
    headline_bbox = draw.multiline_textbbox(
        (92, 132),
        headline_text,
        font=headline_font,
        spacing=8,
    )

    hero_specs: list[dict[str, object]] = []
    if facts:
        hero_label, hero_value, preferred_size = _hero_parts(facts[0])
        hero_font, hero_lines = _fit_hero_value(draw, hero_value, preferred_size)
        hero_text = "\n".join(hero_lines)
        hero_bbox = draw.multiline_textbbox((0, 0), hero_text, font=hero_font, spacing=4)
        hero_height = max(92, 44 + (hero_bbox[3] - hero_bbox[1]) + 14)
        hero_specs.append(
            {
                "kind": "hero",
                "label": hero_label,
                "lines": hero_lines,
                "font": hero_font,
                "height": hero_height,
            }
        )

    for fact in facts[1:]:
        fact_font, fact_lines = _fit_fact(draw, fact)
        fact_bbox = draw.multiline_textbbox(
            (0, 0), "\n".join(fact_lines), font=fact_font, spacing=4
        )
        height = max(46, (fact_bbox[3] - fact_bbox[1]) + 18)
        hero_specs.append(
            {
                "kind": "support",
                "lines": fact_lines,
                "font": fact_font,
                "height": height,
            }
        )

    block_height = sum(int(spec["height"]) for spec in hero_specs) + max(
        0, len(hero_specs) - 1
    ) * gap
    min_top = headline_bbox[3] + 28
    centered_top = min_top + max(0, (585 - min_top - block_height) // 2)
    top = min(392, max(min_top, centered_top))
    if top + block_height > 600:
        top = max(min_top, 600 - block_height)

    y = top
    for spec in hero_specs:
        height = int(spec["height"])
        if spec["kind"] == "hero":
            draw.rounded_rectangle(
                [88, y, 1120, y + height],
                radius=14,
                fill=(25, 38, 59),
            )
            draw.rectangle([104, y + 16, 110, y + height - 16], fill=accent)
            label_font = _font("JetBrainsMono-Medium.ttf", 18)
            draw.text(
                (132, y + 12),
                str(spec["label"]),
                font=label_font,
                fill=(*accent, 255),
            )
            draw.multiline_text(
                (132, y + 36),
                "\n".join(spec["lines"]),
                font=spec["font"],
                fill=(248, 250, 252),
                spacing=4,
            )
        else:
            draw.rounded_rectangle(
                [88, y, 1120, y + height],
                radius=12,
                fill=(20, 31, 49),
            )
            draw.rectangle([104, y + height // 2 - 5, 114, y + height // 2 + 5], fill=accent)
            text_height = draw.multiline_textbbox(
                (0, 0),
                "\n".join(spec["lines"]),
                font=spec["font"],
                spacing=4,
            )
            offset = max(0, (height - (text_height[3] - text_height[1])) // 2)
            draw.multiline_text(
                (132, y + offset),
                "\n".join(spec["lines"]),
                font=spec["font"],
                fill=(225, 232, 244),
                spacing=4,
            )
        y += height + gap

    draw.line([(92, 610), (1108, 610)], fill=(255, 255, 255, 45), width=1)
    source_text = f"Source: {draft.source_name}"
    draw.text((92, 625), source_text, font=footer_font, fill=(190, 200, 218))
    handle_text = f"@{handle}"
    handle_width = draw.textlength(handle_text, font=footer_font)
    draw.text(
        (WIDTH - 75 - handle_width, 625),
        handle_text,
        font=footer_font,
        fill=(190, 200, 218),
    )

    quality = 92
    while quality >= 70:
        image.save(output_path, format="JPEG", quality=quality, optimize=True)
        if output_path.stat().st_size < 1_900_000:
            break
        quality -= 6
    return output_path
