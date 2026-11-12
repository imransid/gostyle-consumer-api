"""
A reference's size and its grid copy (docs/my-gallery-fe-contract.md,
sections 1 and 4), read once, when it is added.

The size is the photo as it is shown: a phone photo taken upright is often
stored on its side with an EXIF turn, so its stored width is its shown
height. HEIC turns itself when it is opened (pillow-heif), JPEG, PNG, WebP
and AVIF are turned here.
"""

import logging
from io import BytesIO

from PIL import ExifTags, Image, ImageOps
from pillow_heif import register_heif_opener

logger = logging.getLogger(__name__)

# HEIC, what iPhones take. AVIF is in Pillow itself.
register_heif_opener()

THUMBNAIL_WIDTH = 480
JPEG_QUALITY = 80

# EXIF orientations with a quarter turn: stored width is shown height.
_QUARTER_TURNS = {5, 6, 7, 8}


def read(upload):
    """
    (width, height, thumbnail) for an UploadedFile: the shown size, and the
    grid copy as JPEG bytes, or None when the photo is not wider than
    THUMBNAIL_WIDTH. (None, None, None) when it can't be read: the doc allows
    nulls, and the photo is still added.
    """
    try:
        with upload.file.open("rb") as stream, Image.open(stream) as photo:
            width, height, turned = _shown_size(photo)
            thumbnail = None
            if width > THUMBNAIL_WIDTH:
                thumbnail = _thumbnail(photo, width, height, turned)
    except Exception:
        # Pillow has many errors (not an image, too big, cut short), and the
        # store has its own. None of them may fail the call.
        logger.warning("My gallery: photo %s could not be read", upload.id, exc_info=True)
        return None, None, None
    return width, height, thumbnail


def _shown_size(photo):
    width, height = photo.size
    turned = photo.getexif().get(ExifTags.Base.Orientation) in _QUARTER_TURNS
    if turned:
        return height, width, True
    return width, height, False


def _thumbnail(photo, width, height, turned):
    size = (THUMBNAIL_WIDTH, max(1, round(height * THUMBNAIL_WIDTH / width)))
    # JPEG only (a no-op for the others): decode at a smaller scale, far
    # faster on a 12 MP photo. It asks in the stored direction.
    photo.draft("RGB", size[::-1] if turned else size)
    shown = ImageOps.exif_transpose(photo)
    if shown.mode != "RGB":
        # Transparent parts on white, not black.
        shown = shown.convert("RGBA")
        background = Image.new("RGB", shown.size, "white")
        background.paste(shown, mask=shown.getchannel("A"))
        shown = background
    shown = shown.resize(size, Image.Resampling.LANCZOS)
    out = BytesIO()
    shown.save(out, "JPEG", quality=JPEG_QUALITY, optimize=True)
    return out.getvalue()
