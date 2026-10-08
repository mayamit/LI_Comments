"""Small shared helpers."""
from datetime import datetime, timezone
from typing import Optional


def relative_time(iso: Optional[str]) -> str:
    if not iso:
        return "Never"
    try:
        dt = datetime.fromisoformat(iso.replace("Z", "+00:00"))
    except ValueError:
        return iso
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    secs = int((datetime.now(timezone.utc) - dt).total_seconds())
    if secs < 60:
        return f"{max(secs, 0)}s ago"
    minutes = secs // 60
    if minutes < 60:
        return f"{minutes}m ago"
    hours = minutes // 60
    if hours < 24:
        return f"{hours}h ago"
    days = hours // 24
    if days < 30:
        return f"{days}d ago"
    return dt.strftime("%Y-%m-%d")


def truncate(text: Optional[str], limit: int = 300) -> str:
    if not text:
        return ""
    if len(text) <= limit:
        return text
    return text[:limit].rstrip() + "…"


def extract_post_images(item: dict) -> list[dict]:
    """Post image URLs from an Apify post item — the only part of the raw item
    the app displays, so it is all that gets stored alongside the counts."""
    out = []
    for img in item.get("postImages") or []:
        if isinstance(img, dict):
            url = img.get("url")
            if isinstance(url, str) and url.startswith("http"):
                out.append({"url": url, "width": img.get("width"), "height": img.get("height")})
    return out
