"""Stage 2 (code part): fingerprint the file and tell a text PDF from a scan."""

import hashlib
import re

import pymupdf

MIN_TEXT_CHARS = 50  # fewer than this across all pages = treat as a scan


def file_hash(pdf: bytes) -> str:
    return hashlib.sha256(pdf).hexdigest()


def text_layer(pdf: bytes) -> str:
    with pymupdf.open(stream=pdf, filetype="pdf") as doc:
        return "\n".join(page.get_text() for page in doc)


def source_type(pdf: bytes) -> tuple[str, str]:
    """Return ('text' | 'scanned', text layer)."""
    text = text_layer(pdf)
    return ("text" if len(text.strip()) >= MIN_TEXT_CHARS else "scanned"), text


def squash(s: str) -> str:
    """Whitespace- and case-insensitive form used for quote validation."""
    return re.sub(r"\s+", " ", s or "").strip().lower()


def page_image(pdf: bytes, dpi: int = 110) -> bytes:
    """First page as PNG, for the reviewer's side-by-side view."""
    with pymupdf.open(stream=pdf, filetype="pdf") as doc:
        return doc[0].get_pixmap(dpi=dpi).tobytes("png")
