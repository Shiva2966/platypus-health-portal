"""File validation / delivery helpers for the document store (W2).

Everything here is pure (no DB access): magic-byte sniffing, safe filenames,
hashing, optional Pillow thumbnails and safe response headers.
"""
from __future__ import annotations

import hashlib
import io
import os
import re
import unicodedata
import zipfile
from urllib.parse import quote

MAX_UPLOAD_BYTES = 15 * 1024 * 1024  # 15 MB

# canonical mime -> (extensions, default extension)
ALLOWED_TYPES: dict[str, tuple[tuple[str, ...], str]] = {
    "application/pdf": (("pdf",), "pdf"),
    "image/jpeg": (("jpg", "jpeg", "jpe", "jfif"), "jpg"),
    "image/png": (("png",), "png"),
    "image/webp": (("webp",), "webp"),
    "image/heic": (("heic",), "heic"),
    "image/heif": (("heif",), "heif"),
    "text/plain": (("txt", "text"), "txt"),
    "application/vnd.openxmlformats-officedocument.wordprocessingml.document": (("docx",), "docx"),
}

# Types a browser can render inline safely from our origin.
INLINE_SAFE = {"application/pdf", "image/jpeg", "image/png", "image/webp", "text/plain"}
IMAGE_TYPES = {"image/jpeg", "image/png", "image/webp", "image/heic", "image/heif"}

ACCEPT_ATTR = (
    "application/pdf,image/jpeg,image/png,image/webp,image/heic,image/heif,"
    "text/plain,application/vnd.openxmlformats-officedocument.wordprocessingml.document,"
    ".pdf,.jpg,.jpeg,.png,.webp,.heic,.heif,.txt,.docx"
)

_HEIF_BRANDS = {b"heic", b"heix", b"hevc", b"hevx", b"heim", b"heis", b"hevm", b"hevs", b"mif1", b"msf1", b"heif"}
_HEIC_BRANDS = {b"heic", b"heix", b"hevc", b"hevx", b"heim", b"heis", b"hevm", b"hevs"}


class FileValidationError(ValueError):
    """Raised for rejected uploads. `.status` is the suggested HTTP status."""

    def __init__(self, message: str, status: int = 400):
        super().__init__(message)
        self.status = status


def sha256_hex(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _docx_ok(data: bytes) -> bool:
    try:
        with zipfile.ZipFile(io.BytesIO(data)) as zf:
            names = set(zf.namelist())
            if "[Content_Types].xml" not in names or "word/document.xml" not in names:
                return False
            total = sum(i.file_size for i in zf.infolist())
            if total > 200 * 1024 * 1024 or len(names) > 5000:  # zip-bomb guard
                return False
        return True
    except (zipfile.BadZipFile, OSError, ValueError, RuntimeError):
        return False


def _text_ok(data: bytes) -> bool:
    if not data or b"\x00" in data:
        return False
    try:
        data.decode("utf-8-sig")
    except UnicodeDecodeError:
        return False
    return True


def sniff_mime(data: bytes) -> str | None:
    """Return the canonical mime type of `data` using magic bytes, or None."""
    if data[:5] == b"%PDF-":
        return "application/pdf"
    if data[:3] == b"\xff\xd8\xff":
        return "image/jpeg"
    if data[:8] == b"\x89PNG\r\n\x1a\n":
        return "image/png"
    if data[:4] == b"RIFF" and data[8:12] == b"WEBP":
        return "image/webp"
    if data[4:8] == b"ftyp":
        brand = data[8:12]
        if brand in _HEIC_BRANDS:
            return "image/heic"
        if brand in _HEIF_BRANDS:
            return "image/heif"
        return None
    if data[:4] == b"PK\x03\x04" and _docx_ok(data):
        return "application/vnd.openxmlformats-officedocument.wordprocessingml.document"
    if _text_ok(data[: MAX_UPLOAD_BYTES]):
        return "text/plain"
    return None


def extension_of(filename: str | None) -> str:
    if not filename or "." not in filename:
        return ""
    return filename.rsplit(".", 1)[-1].strip().lower()


def validate_upload(data: bytes, filename: str | None) -> str:
    """Validate raw bytes; return canonical mime. Raises FileValidationError."""
    if not data:
        raise FileValidationError("The file is empty.", 400)
    if len(data) > MAX_UPLOAD_BYTES:
        raise FileValidationError("File is too large (maximum is 15 MB).", 413)
    detected = sniff_mime(data)
    if detected is None or detected not in ALLOWED_TYPES:
        raise FileValidationError(
            "Unsupported or unrecognised file type. Allowed: PDF, JPG, PNG, HEIC/HEIF, WEBP, TXT, DOCX.", 415
        )
    ext = extension_of(filename)
    if ext:
        exts = ALLOWED_TYPES[detected][0]
        known = {e for v in ALLOWED_TYPES.values() for e in v[0]}
        if ext not in known:
            raise FileValidationError(f"File extension .{ext} is not allowed.", 415)
        if ext not in exts and not (detected.startswith("image/hei") and ext in ("heic", "heif")):
            raise FileValidationError("File content does not match its extension.", 415)
    return detected


_CTRL = re.compile(r"[\x00-\x1f\x7f]")
_BAD = re.compile(r'[\\/:*?"<>|\r\n\t;%]')


def clean_text(value: str | None, max_len: int) -> str:
    """Strip control characters, normalise unicode, trim and cap length."""
    if value is None:
        return ""
    value = unicodedata.normalize("NFC", value)
    value = _CTRL.sub("", value).strip()
    return value[:max_len]


def safe_download_name(name: str, mime: str) -> str:
    """Build a safe download file name: sanitised document name + canonical extension."""
    base = _BAD.sub("_", clean_text(name, 120)).strip(" .") or "document"
    default_ext = ALLOWED_TYPES.get(mime, ((), "bin"))[1]
    exts = ALLOWED_TYPES.get(mime, ((), ""))[0]
    if base.lower().rsplit(".", 1)[-1] in exts and "." in base:
        return base
    return f"{base}.{default_ext}"


def content_disposition(filename: str, inline: bool) -> str:
    ascii_name = unicodedata.normalize("NFKD", filename).encode("ascii", "ignore").decode("ascii")
    ascii_name = _BAD.sub("_", ascii_name) or "document"
    kind = "inline" if inline else "attachment"
    return f"{kind}; filename=\"{ascii_name}\"; filename*=UTF-8''{quote(filename, safe='')}"


def delivery_headers(name: str, mime: str, *, download: bool) -> dict[str, str]:
    """Headers for serving stored bytes. Non-inline-safe types are always attachments."""
    inline = (not download) and mime in INLINE_SAFE
    media = mime
    # The browser's built-in PDF viewer breaks under `default-src 'none'`, so PDFs get a lighter policy.
    csp = ("script-src 'none'; frame-ancestors 'self'" if mime == "application/pdf"
           else "default-src 'none'; style-src 'unsafe-inline'; frame-ancestors 'self'")
    headers = {
        "Content-Disposition": content_disposition(safe_download_name(name, mime), inline),
        "X-Content-Type-Options": "nosniff",
        "Cache-Control": "private, no-store",
        "Content-Security-Policy": csp,
        "Cross-Origin-Resource-Policy": "same-origin",
        "Referrer-Policy": "no-referrer",
    }
    if media == "text/plain":
        media = "text/plain; charset=utf-8"
    headers["Content-Type"] = media
    return headers


THUMBNAIL_MIMES = frozenset({"image/jpeg", "image/png", "image/webp"})


def thumbnails_available() -> bool:
    try:
        import PIL.Image  # noqa: F401  # type: ignore
        return True
    except Exception:
        return False


def make_thumbnail(data: bytes, mime: str, size: int = 256) -> bytes | None:
    """PNG thumbnail for JPG/PNG/WEBP via Pillow if installed; otherwise None."""
    if mime not in {"image/jpeg", "image/png", "image/webp"}:
        return None
    try:
        from PIL import Image, ImageOps  # type: ignore
    except Exception:
        return None
    try:
        Image.MAX_IMAGE_PIXELS = 60_000_000
        with Image.open(io.BytesIO(data)) as im:
            im = ImageOps.exif_transpose(im)
            im.thumbnail((size, size))
            if im.mode not in ("RGB", "RGBA"):
                im = im.convert("RGB")
            out = io.BytesIO()
            im.save(out, format="PNG")
            return out.getvalue()
    except Exception:
        return None


def qr_svg(text: str) -> str:
    """Render `text` as an inline SVG QR code (pure python, no Pillow needed)."""
    import qrcode
    import qrcode.image.svg

    img = qrcode.make(text, image_factory=qrcode.image.svg.SvgPathImage, box_size=10, border=2)
    buf = io.BytesIO()
    img.save(buf)
    return buf.getvalue().decode("utf-8")


def qr_png(text: str) -> bytes | None:
    """PNG QR when Pillow is available (qrcode PIL factory), else None."""
    try:
        import qrcode

        img = qrcode.make(text, box_size=8, border=2)
        buf = io.BytesIO()
        img.save(buf, format="PNG")
        return buf.getvalue()
    except Exception:
        return None
