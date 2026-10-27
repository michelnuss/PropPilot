from pathlib import Path
from urllib.parse import urlparse

import httpx

from .config import Settings


ALLOWED_MIME = {"image/jpeg": ".jpg", "image/png": ".png", "image/webp": ".webp"}
MAX_BYTES = 8 * 1024 * 1024


def _sniff(data: bytes) -> str | None:
    if data.startswith(b"\xff\xd8\xff"):
        return "image/jpeg"
    if data.startswith(b"\x89PNG\r\n\x1a\n"):
        return "image/png"
    if data.startswith(b"RIFF") and data[8:12] == b"WEBP":
        return "image/webp"
    return None


def validate_image(data: bytes, declared_mime: str) -> str:
    mime = declared_mime.split(";", 1)[0].lower()
    if mime not in ALLOWED_MIME:
        raise ValueError("Only JPEG, PNG, or WebP photos are supported")
    if not data or len(data) > MAX_BYTES:
        raise ValueError("Photo must be under 8 MB")
    if _sniff(data) != mime:
        raise ValueError("Photo content does not match its media type")
    return mime


def save_image(settings: Settings, data: bytes, declared_mime: str, message_id: int) -> tuple[Path, str]:
    mime = validate_image(data, declared_mime)
    settings.media_dir.mkdir(parents=True, exist_ok=True)
    destination = settings.media_dir / f"message-{message_id}{ALLOWED_MIME[mime]}"
    destination.write_bytes(data)
    return destination, mime


def download_image(settings: Settings, url: str, declared_mime: str, message_id: int) -> tuple[Path, str]:
    mime = declared_mime.split(";", 1)[0].lower()
    if mime not in ALLOWED_MIME:
        raise ValueError("Only JPEG, PNG, or WebP photos are supported")
    if settings.mode == "mock" and url == "mock://sink":
        source = Path(__file__).parent.parent / "demo" / "broken_sink.png"
        data = source.read_bytes()
    else:
        parsed = urlparse(url)
        host = (parsed.hostname or "").lower()
        if parsed.scheme != "https" or not (host == "twilio.com" or host.endswith(".twilio.com") or host.endswith(".twiliocdn.com")):
            raise ValueError("Media URL is not a Twilio HTTPS URL")
        if not settings.twilio_sid or not settings.twilio_token:
            raise ValueError("Twilio credentials are required to download live media")
        with httpx.Client(timeout=20, follow_redirects=True, auth=(settings.twilio_sid, settings.twilio_token)) as client:
            with client.stream("GET", url) as response:
                response.raise_for_status()
                chunks, size = [], 0
                for chunk in response.iter_bytes():
                    size += len(chunk)
                    if size > MAX_BYTES:
                        raise ValueError("Photo exceeds 8 MB limit")
                    chunks.append(chunk)
                data = b"".join(chunks)
    return save_image(settings, data, mime, message_id)
