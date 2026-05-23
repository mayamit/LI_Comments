"""Server-side proxy for LinkedIn-hosted post images.

Ad-blockers commonly block `*.licdn.com` because LinkedIn ships tracking
pixels from the same host. The browser can't load those images directly,
which is fine for tracking but breaks our post-preview thumbnails. This
proxy fetches the image on the server side and re-serves it on the local
origin, so the browser only ever sees a localhost URL.

Only `media.licdn.com` URLs are allowed — this avoids turning the app
into an SSRF gadget or open relay.
"""
from urllib.parse import urlparse

import httpx
from fastapi import APIRouter, HTTPException, Query
from fastapi.responses import Response

router = APIRouter(tags=["image-proxy"])

ALLOWED_HOSTS = {"media.licdn.com"}
TIMEOUT_S = 15
MAX_BYTES = 10 * 1024 * 1024  # 10 MB — LinkedIn feedshare images are well under this


@router.get("/image-proxy")
async def image_proxy(url: str = Query(..., max_length=2000)):
    parsed = urlparse(url)
    if parsed.scheme != "https" or parsed.hostname not in ALLOWED_HOSTS:
        raise HTTPException(400, "URL must be https on an allowed host.")

    async with httpx.AsyncClient(timeout=TIMEOUT_S, follow_redirects=True) as client:
        try:
            r = await client.get(url)
        except httpx.HTTPError as e:
            raise HTTPException(502, f"Upstream fetch failed: {e}")

    if r.status_code != 200:
        raise HTTPException(r.status_code, "Upstream returned non-200.")

    body = r.content
    if len(body) > MAX_BYTES:
        raise HTTPException(413, "Upstream image too large.")

    content_type = r.headers.get("content-type", "image/jpeg")
    if not content_type.startswith("image/"):
        raise HTTPException(415, "Upstream did not return an image.")

    # Cache aggressively in the browser — LinkedIn signs URLs with ~30 day
    # expiry, so reusing the same URL for a day is safe.
    return Response(
        content=body,
        media_type=content_type,
        headers={"Cache-Control": "private, max-age=86400"},
    )
