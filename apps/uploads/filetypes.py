"""Which files we accept, decided by their first bytes.

The file name and the Content-Type the client sends are both typed by the
client, so neither is trusted. `filetype` reads the magic bytes at the start
of the file instead. Anything it cannot name, or that is not in the list
below, is refused. That also refuses HTML, SVG and plain text, which have no
magic bytes.
"""

import filetype

from .models import FileType

ALLOWED_MIME_TYPES = {
    "image/jpeg": FileType.IMAGE,
    "image/png": FileType.IMAGE,
    "image/gif": FileType.IMAGE,
    "image/webp": FileType.IMAGE,
    "image/heic": FileType.IMAGE,
    "image/avif": FileType.IMAGE,
    "video/mp4": FileType.VIDEO,
    "video/quicktime": FileType.VIDEO,
    "video/webm": FileType.VIDEO,
    "video/3gpp": FileType.VIDEO,
    "audio/mpeg": FileType.AUDIO,
    "audio/mp4": FileType.AUDIO,
    "audio/aac": FileType.AUDIO,
    "audio/x-wav": FileType.AUDIO,
    "audio/ogg": FileType.AUDIO,
    "audio/amr": FileType.AUDIO,
    "application/pdf": FileType.PDF,
    "application/msword": FileType.DOCUMENT,
    "application/vnd.openxmlformats-officedocument.wordprocessingml.document": FileType.DOCUMENT,
    "application/vnd.ms-excel": FileType.DOCUMENT,
    "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet": FileType.DOCUMENT,
    "application/vnd.ms-powerpoint": FileType.DOCUMENT,
    "application/vnd.openxmlformats-officedocument.presentationml.presentation": FileType.DOCUMENT,
    "application/vnd.oasis.opendocument.text": FileType.DOCUMENT,
    "application/vnd.oasis.opendocument.spreadsheet": FileType.DOCUMENT,
    "application/vnd.oasis.opendocument.presentation": FileType.DOCUMENT,
}


def detect(upload):
    """Return {"mime", "extension", "file_type"}, or None if we refuse it."""
    kind = filetype.guess(upload)
    if kind is None or kind.mime not in ALLOWED_MIME_TYPES:
        return None
    return {
        "mime": kind.mime,
        "extension": kind.extension,
        "file_type": ALLOWED_MIME_TYPES[kind.mime],
    }