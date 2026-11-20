"""Images attached to an agent task or a follow-up (shared by IDE and engine).

Images travel as JSON ``[{"name": "screenshot.png", "data": "<base64>"}]``. The
type comes from the file's own bytes (PNG, JPEG, GIF, WebP), never from the
name or a client-sent media type, so SVG (which can carry script) and anything
else are refused. Both sides validate: the engine never trusts the IDE alone.
"""
from __future__ import annotations

import base64
import binascii
import re
from pathlib import Path

MAX_IMAGES = 6
MAX_IMAGE_BYTES = 5 * 1024 * 1024
MAX_TOTAL_BYTES = 15 * 1024 * 1024
MAX_NAME = 100

EXTENSIONS = {"image/png": "png", "image/jpeg": "jpg", "image/gif": "gif", "image/webp": "webp"}
# stored file names: "task-01.png" (images sent with the objective), "step03-02.jpg" (a follow-up's)
STORED_NAME = re.compile(r"^(task|step\d{2})-\d{2}\.(png|jpg|gif|webp)$")


class ImageError(ValueError):
    pass


def sniff(data: bytes) -> str | None:
    """The image's media type from its magic bytes, or None if it is not a supported image."""
    if data.startswith(b"\x89PNG\r\n\x1a\n"):
        return "image/png"
    if data.startswith(b"\xff\xd8\xff"):
        return "image/jpeg"
    if data[:6] in (b"GIF87a", b"GIF89a"):
        return "image/gif"
    if len(data) >= 12 and data[:4] == b"RIFF" and data[8:12] == b"WEBP":
        return "image/webp"
    return None


def clean_name(raw, fallback: str) -> str:
    """The user's file name, for display and the prompt: no path, no control characters."""
    name = str(raw or "").replace("\\", "/").rsplit("/", 1)[-1]
    name = "".join(ch for ch in name if ch.isprintable()).strip()
    return name[:MAX_NAME] or fallback


def stored_name(prefix: str, n: int, media_type: str) -> str:
    """``prefix`` is "task" or "stepNN"; ``n`` counts from 1."""
    return f"{prefix}-{n:02d}.{EXTENSIONS[media_type]}"


def step_prefix(index: int) -> str:
    return f"step{index + 1:02d}"


def clean_images(raw) -> list[dict]:
    """Validate a list of ``{"name", "data"}`` (base64). Returns ``[{"name", "media_type", "data": bytes}]``.
    Raises ImageError with a message meant for the user."""
    if raw is None:
        return []
    if not isinstance(raw, list):
        raise ImageError("images must be a list of {name, data}")
    if len(raw) > MAX_IMAGES:
        raise ImageError(f"Attach at most {MAX_IMAGES} images")
    out, total = [], 0
    for i, item in enumerate(raw, 1):
        if not isinstance(item, dict) or not isinstance(item.get("data"), str):
            raise ImageError(f"Image {i}: expected {{name, data}} with base64 data")
        name = clean_name(item.get("name"), f"image-{i}")
        b64 = item["data"]
        if b64.startswith("data:"):  # a data: URL as read by the browser
            b64 = b64.partition(",")[2]
        if len(b64) > (MAX_IMAGE_BYTES * 4) // 3 + 8:
            raise ImageError(f'"{name}" is larger than {MAX_IMAGE_BYTES // (1024 * 1024)} MB')
        try:
            data = base64.b64decode(b64, validate=True)
        except (binascii.Error, ValueError):
            raise ImageError(f'"{name}" is not valid base64 data')
        if not data:
            raise ImageError(f'"{name}" is empty')
        if len(data) > MAX_IMAGE_BYTES:
            raise ImageError(f'"{name}" is larger than {MAX_IMAGE_BYTES // (1024 * 1024)} MB')
        media_type = sniff(data)
        if media_type is None:
            raise ImageError(f'"{name}" is not a PNG, JPEG, GIF or WebP image')
        total += len(data)
        if total > MAX_TOTAL_BYTES:
            raise ImageError(f"Images may be at most {MAX_TOTAL_BYTES // (1024 * 1024)} MB together")
        out.append({"name": name, "media_type": media_type, "data": data})
    return out


def save_images(dest: Path, images: list[dict], prefix: str) -> list[dict]:
    """Write cleaned images to ``dest`` as ``<prefix>-NN.<ext>``; returns their records
    ``[{"name", "file", "media_type", "size"}]``."""
    out = []
    if images:
        dest.mkdir(parents=True, exist_ok=True)
    for n, im in enumerate(images, 1):
        file = stored_name(prefix, n, im["media_type"])
        (dest / file).write_bytes(im["data"])
        out.append({"name": im["name"], "file": file, "media_type": im["media_type"], "size": len(im["data"])})
    return out


def to_payload(images: list[dict]) -> list[dict]:
    """Cleaned images back to the JSON form (for the IDE -> engine request)."""
    return [{"name": im["name"], "data": base64.b64encode(im["data"]).decode("ascii")} for im in images]
