"""Deterministic poster for items that have neither a frame nor a preview."""

from __future__ import annotations

import logging
from pathlib import Path

logger = logging.getLogger(__name__)

PLACEHOLDER_SIZE = (640, 360)
_TOP_COLOR = (43, 47, 54)
_BOTTOM_COLOR = (74, 85, 104)
_TEXT_COLOR = (240, 243, 248)
_MUTED_COLOR = (176, 186, 200)


def placeholder_initials(title: str) -> str:
    """Return up to two initials from a title, never an empty caption."""
    words = [word for word in str(title or "").replace("-", " ").split() if word]
    letters = "".join(word[0] for word in words[:2] if word[0].isalnum()).upper()
    return letters or "?"


def write_placeholder_artwork(dest: Path, *, title: str, studio: str = "") -> bool:
    """Write a neutral gradient poster with the item's initials.

    Some Telegram files exist only as previews: ffmpeg cannot decode a frame and
    the preview proxy answers 404, so the item would stay without artwork for
    ever. A generated poster keeps the grid readable and is deliberately not
    black, so the artwork checks treat it as finished artwork.
    """
    dest = Path(dest)
    if dest.is_symlink():
        return False
    try:
        from PIL import Image, ImageDraw, ImageFont
    except Exception:
        logger.debug("Pillow is unavailable; skipping placeholder artwork", exc_info=True)
        return False
    width, height = PLACEHOLDER_SIZE
    image = Image.new("RGB", PLACEHOLDER_SIZE)
    draw = ImageDraw.Draw(image)
    for y in range(height):
        ratio = y / max(height - 1, 1)
        color = tuple(
            int(_TOP_COLOR[channel] + (_BOTTOM_COLOR[channel] - _TOP_COLOR[channel]) * ratio)
            for channel in range(3)
        )
        draw.line([(0, y), (width, y)], fill=color)

    initials = placeholder_initials(title)
    try:
        big = ImageFont.truetype("DejaVuSans-Bold.ttf", 132)
        small = ImageFont.truetype("DejaVuSans.ttf", 26)
    except Exception:
        big = ImageFont.load_default()
        small = ImageFont.load_default()

    box = draw.textbbox((0, 0), initials, font=big)
    draw.text(
        ((width - (box[2] - box[0])) / 2 - box[0], (height - (box[3] - box[1])) / 2 - box[1] - 12),
        initials, font=big, fill=_TEXT_COLOR,
    )
    caption = (str(studio or "").strip() or "No preview")[:40]
    caption_box = draw.textbbox((0, 0), caption, font=small)
    draw.text(
        (
            (width - (caption_box[2] - caption_box[0])) / 2 - caption_box[0],
            height - 54,
        ),
        caption, font=small, fill=_MUTED_COLOR,
    )

    temporary = dest.with_suffix(".jpg.tmp")
    try:
        dest.parent.mkdir(parents=True, exist_ok=True)
        image.save(temporary, "JPEG", quality=88)
        temporary.replace(dest)
        return True
    except OSError:
        logger.warning("Could not write placeholder artwork to %s", dest, exc_info=True)
        try:
            temporary.unlink(missing_ok=True)
        except OSError:
            pass
        return False
