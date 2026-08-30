"""Formats this backend claims.

Kept as data rather than derived from the library at import time on purpose: the
list must be answerable by `/capability` even when `markitdown` is not installed
yet, which is exactly when a user is deciding whether to install it. Core unions
this list with its own builtin floor and serves the result as the composer's file
picker `accept` list.

Two rules shaped the contents:

* **Only formats `markitdown[all]` has a converter for.** No `.doc`/`.ppt`/`.xls`
  legacy binary Office (MarkItDown reads `.xls` through xlrd but not the other
  two), no `.odt`/`.rtf` — those need LibreOffice or pandoc, which is exactly the
  native-toolchain cost this backend exists to avoid. A user with that corpus
  should bind `unstructured`, and advertising a format we cannot read would send
  them a blank document instead of an answer.
* **Media formats are advertised only when their native tool is present.**
  MarkItDown's image and audio converters shell out to `exiftool` and (through
  pydub) `ffmpeg`. Without them the conversion returns nothing useful, and a
  format list that promises `.png` and delivers an empty string is the silent
  drop this whole capability exists to kill. `capability_extensions()` adds them
  when the tool is on PATH and omits them when it is not.
"""

from __future__ import annotations

from .deps import EXIFTOOL, FFMPEG

# Everything `markitdown[all]` reads with pure Python and no native tool.
BASE_EXTENSIONS: frozenset[str] = frozenset(
    {
        # Portable documents
        ".pdf",
        # Office (OOXML only — the legacy binary formats need LibreOffice)
        ".docx",
        ".pptx",
        ".xlsx",
        ".xls",
        # Markup and plain text
        ".txt",
        ".text",
        ".md",
        ".markdown",
        ".html",
        ".htm",
        ".xml",
        ".json",
        ".csv",
        # Ebooks, email, notebooks
        ".epub",
        ".msg",
        ".ipynb",
        # Archives we expand and parse member-by-member
        ".zip",
        ".tar",
        ".tar.gz",
        ".tgz",
        ".tar.bz2",
        ".tbz2",
        ".tar.xz",
        ".txz",
    }
)

# Images: MarkItDown reports EXIF metadata (and an LLM caption when one is
# configured, which Core does not do here). Useless without `exiftool`.
IMAGE_EXTENSIONS: frozenset[str] = frozenset({".jpg", ".jpeg", ".png", ".tiff", ".tif"})

# Audio: EXIF metadata plus speech transcription, which goes through pydub and
# therefore needs `ffmpeg` for anything that is not a plain `.wav`.
AUDIO_EXTENSIONS: frozenset[str] = frozenset({".wav", ".mp3", ".m4a"})


def capability_extensions() -> list[str]:
    """The sorted format list `/capability` advertises on *this* host."""
    extensions = set(BASE_EXTENSIONS)
    if EXIFTOOL.present():
        extensions |= IMAGE_EXTENSIONS
    if FFMPEG.present():
        extensions |= AUDIO_EXTENSIONS
    return sorted(extensions)
