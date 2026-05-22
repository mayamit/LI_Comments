"""Profile enrichment + auto-tagging.

Fetches a LinkedIn profile via Apify, derives reach/cadence deterministically,
asks Claude (via CLI) for 1-2 persona tags, formats a "Name - Position, Company"
display name, and replaces the handle's reach/cadence/persona tag assignments.

Intent tags are never touched — they're a strategic decision for the user.
"""
import asyncio
import json
import logging
import os
import re
import statistics
import tempfile
from typing import Any, Optional

import httpx

from database import get_db

logger = logging.getLogger(__name__)

PROFILE_ACTOR = os.getenv("APIFY_PROFILE_ACTOR", "harvestapi~linkedin-profile-scraper")
APIFY_BASE = "https://api.apify.com/v2"
PROFILE_TIMEOUT_S = 180

REACH_BUCKETS = [
    (100_000, "reach-mega"),
    (10_000, "reach-large"),
    (1_000, "reach-mid"),
    (0, "reach-niche"),
]

# Median gap in days between recent posts → cadence slug.
CADENCE_DAILY_MAX_DAYS = 2.0
CADENCE_WEEKLY_MAX_DAYS = 10.0


class EnrichmentError(Exception):
    pass


# --------------------------- profile fetch ---------------------------


async def fetch_profile(handle: str) -> dict:
    """Run the profile actor synchronously and return the first item."""
    token = os.getenv("APIFY_TOKEN")
    if not token:
        raise EnrichmentError("APIFY_TOKEN not set")
    url = (
        f"{APIFY_BASE}/acts/{PROFILE_ACTOR}/run-sync-get-dataset-items"
        f"?token={token}&timeout={PROFILE_TIMEOUT_S}"
    )
    body = {
        "profileUrls": [f"https://www.linkedin.com/in/{handle}"],
        "queries": [handle],
    }
    async with httpx.AsyncClient(timeout=PROFILE_TIMEOUT_S + 20) as client:
        r = await client.post(url, json=body)
    if r.status_code >= 400:
        raise EnrichmentError(
            f"Apify profile actor returned {r.status_code}: {r.text[:200]}"
        )
    items = r.json()
    if not items:
        raise EnrichmentError("Apify profile actor returned no items")
    return items[0]


# --------------------------- deterministic derivations ---------------------------


def derive_reach_slug(profile: dict) -> Optional[str]:
    n = profile.get("followerCount")
    if not isinstance(n, (int, float)):
        return None
    for threshold, slug in REACH_BUCKETS:
        if n >= threshold:
            return slug
    return None


def _parse_iso(s: Optional[str]):
    if not s:
        return None
    from datetime import datetime
    try:
        return datetime.fromisoformat(s.replace("Z", "+00:00"))
    except (ValueError, AttributeError):
        return None


async def derive_cadence_slug(handle_id: int) -> Optional[str]:
    """Median gap in days between the last <=10 posts → cadence slug."""
    async with get_db() as db:
        cur = await db.execute(
            "SELECT posted_at FROM posts WHERE handle_id = ? "
            "AND posted_at IS NOT NULL ORDER BY posted_at DESC LIMIT 10",
            (handle_id,),
        )
        rows = await cur.fetchall()
    dates = [_parse_iso(r[0]) for r in rows]
    dates = [d for d in dates if d is not None]
    if len(dates) < 2:
        return None
    gaps_days = []
    for i in range(len(dates) - 1):
        delta = (dates[i] - dates[i + 1]).total_seconds() / 86_400.0
        if delta > 0:
            gaps_days.append(delta)
    if not gaps_days:
        return None
    median = statistics.median(gaps_days)
    if median <= CADENCE_DAILY_MAX_DAYS:
        return "cadence-daily"
    if median <= CADENCE_WEEKLY_MAX_DAYS:
        return "cadence-weekly"
    return "cadence-sporadic"


def format_display_name(profile: dict) -> Optional[str]:
    """Build 'First Last — Position, Company' from profile data.

    LinkedIn's `position` sometimes already contains the company name
    (e.g. "CEO at Scry AI"); detect and dedupe.
    """
    first = (profile.get("firstName") or "").strip()
    last = (profile.get("lastName") or "").strip()
    full = " ".join(p for p in (first, last) if p)
    if not full:
        return None
    cp_list = profile.get("currentPosition")
    if isinstance(cp_list, list) and cp_list:
        cp = cp_list[0]
    else:
        cp = None
    position = ((cp or {}).get("position") or "").strip()
    company = ((cp or {}).get("companyName") or "").strip()
    if position and company:
        if company.lower() in position.lower():
            return f"{full} — {position}"
        return f"{full} — {position}, {company}"
    if position:
        return f"{full} — {position}"
    if company:
        return f"{full} — {company}"
    return full


# --------------------------- persona via Claude CLI ---------------------------


def _persona_prompt(profile: dict, persona_options: list[dict]) -> str:
    options_list = "\n".join(
        f"- {p['slug']}: {p['label']} — {p['description'] or ''}".rstrip(" —")
        for p in persona_options
    )
    name = " ".join(
        x for x in (profile.get("firstName"), profile.get("lastName")) if x
    ) or "(unknown)"
    headline = (profile.get("headline") or "").strip() or "(none)"
    cp_list = profile.get("currentPosition") or []
    cp = cp_list[0] if cp_list else {}
    role = (cp.get("position") or "").strip() or "(none)"
    company = (cp.get("companyName") or "").strip() or "(none)"
    about = (profile.get("about") or "").strip()[:600] or "(none)"
    recent_companies = []
    for e in (profile.get("experience") or [])[:4]:
        rc = e.get("companyName")
        if rc:
            recent_companies.append(rc)
    history = ", ".join(recent_companies) or "(none)"

    return (
        "You classify LinkedIn profiles into 1 or 2 persona tags based on the hat "
        "they wear in their posts, not just their literal job title.\n\n"
        "Persona tag options:\n"
        f"{options_list}\n\n"
        "Profile:\n"
        f"Name: {name}\n"
        f"Headline: {headline}\n"
        f"Current role: {role}\n"
        f"Current company: {company}\n"
        f"Recent companies: {history}\n"
        f"About: {about}\n\n"
        "Pick the 1-2 persona slugs from the options above that best describe how "
        "this person shows up on LinkedIn. Output a JSON array of slugs, nothing else. "
        'Example: ["founder", "creator"]'
    )


async def _claude_call(prompt: str) -> str:
    cli = os.getenv("CLAUDE_CLI", "claude")
    model = os.getenv("CLAUDE_MODEL", "claude-sonnet-4-6")
    timeout = int(os.getenv("CLAUDE_TIMEOUT_S", "120"))
    proc = await asyncio.create_subprocess_exec(
        cli, "-p", prompt, "--model", model,
        cwd=tempfile.gettempdir(),
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    try:
        stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=timeout)
    except asyncio.TimeoutError:
        proc.kill()
        await proc.wait()
        raise EnrichmentError(f"claude CLI timed out after {timeout}s")
    if proc.returncode != 0:
        err = stderr.decode("utf-8", errors="replace").strip()
        raise EnrichmentError(f"claude CLI exited {proc.returncode}: {err}")
    return stdout.decode("utf-8", errors="replace").strip()


def _parse_persona_response(text: str, valid_slugs: set[str]) -> list[str]:
    # Pull the first JSON array out of the response.
    m = re.search(r"\[[^\[\]]*\]", text)
    if not m:
        return []
    try:
        arr = json.loads(m.group(0))
    except json.JSONDecodeError:
        return []
    return [s for s in arr if isinstance(s, str) and s in valid_slugs][:2]


async def derive_persona_slugs(profile: dict) -> list[str]:
    async with get_db() as db:
        cur = await db.execute(
            "SELECT slug, label, description FROM tags "
            "WHERE dimension = 'persona' AND deleted_at IS NULL ORDER BY sort_order"
        )
        options = [dict(r) for r in await cur.fetchall()]
    if not options:
        return []
    valid = {o["slug"] for o in options}
    prompt = _persona_prompt(profile, options)
    response = await _claude_call(prompt)
    return _parse_persona_response(response, valid)


# --------------------------- apply derived tags ---------------------------


async def _apply_tag_replacements(
    handle_id: int, dimension: str, new_slugs: list[str]
) -> None:
    """Replace this handle's tags within `dimension` with `new_slugs`.

    Tags in other dimensions are left untouched, so manual intent assignments
    are preserved.
    """
    async with get_db() as db:
        await db.execute(
            """
            DELETE FROM handle_tags
            WHERE handle_id = ?
              AND tag_id IN (SELECT id FROM tags WHERE dimension = ?)
            """,
            (handle_id, dimension),
        )
        if new_slugs:
            placeholders = ",".join("?" * len(new_slugs))
            cur = await db.execute(
                f"SELECT id FROM tags WHERE slug IN ({placeholders}) "
                f"AND dimension = ? AND deleted_at IS NULL",
                (*new_slugs, dimension),
            )
            ids = [r[0] for r in await cur.fetchall()]
            if ids:
                await db.executemany(
                    "INSERT OR IGNORE INTO handle_tags (handle_id, tag_id) VALUES (?, ?)",
                    [(handle_id, tid) for tid in ids],
                )
        await db.commit()


# --------------------------- top-level orchestration ---------------------------


async def enrich_handle(handle_id: int, handle_name: str) -> dict:
    """Fetch profile, derive tags, update display_name. Returns a per-handle summary."""
    result: dict[str, Any] = {
        "handle": handle_name,
        "reach": None,
        "cadence": None,
        "personas": [],
        "display_name": None,
        "follower_count": None,
        "error": None,
    }
    try:
        profile = await fetch_profile(handle_name)
    except EnrichmentError as e:
        result["error"] = str(e)
        return result

    result["follower_count"] = profile.get("followerCount")

    reach = derive_reach_slug(profile)
    cadence = await derive_cadence_slug(handle_id)
    try:
        personas = await derive_persona_slugs(profile)
    except EnrichmentError as e:
        logger.warning("Persona derivation failed for %s: %s", handle_name, e)
        personas = []
    display_name = format_display_name(profile)

    if reach:
        await _apply_tag_replacements(handle_id, "reach", [reach])
    if cadence:
        await _apply_tag_replacements(handle_id, "cadence", [cadence])
    if personas:
        await _apply_tag_replacements(handle_id, "persona", personas)

    async with get_db() as db:
        await db.execute(
            "UPDATE handles SET enrichment_json = ?, enriched_at = datetime('now'), "
            "display_name = COALESCE(?, display_name) WHERE id = ?",
            (json.dumps(profile, default=str), display_name, handle_id),
        )
        await db.commit()

    result["reach"] = reach
    result["cadence"] = cadence
    result["personas"] = personas
    result["display_name"] = display_name
    return result


_enriching = False


async def enrich_untagged_handles() -> dict:
    """Run enrichment over handles that have no tags yet.

    Includes inactive handles. Skips deleted handles. Skips any handle that
    already has at least one tag assignment (re-tagging is a manual action).
    """
    global _enriching
    if _enriching:
        return {"skipped": True, "reason": "An enrichment run is already in progress."}
    _enriching = True
    summary: dict[str, Any] = {
        "handles_processed": 0,
        "succeeded": 0,
        "failed": 0,
        "details": [],
    }
    try:
        async with get_db() as db:
            cur = await db.execute(
                """
                SELECT h.id, h.linkedin_handle
                FROM handles h
                WHERE h.deleted_at IS NULL
                  AND NOT EXISTS (
                      SELECT 1 FROM handle_tags ht WHERE ht.handle_id = h.id
                  )
                ORDER BY h.id
                """
            )
            handles = [(r["id"], r["linkedin_handle"]) for r in await cur.fetchall()]
        for hid, name in handles:
            summary["handles_processed"] += 1
            try:
                detail = await enrich_handle(hid, name)
                summary["details"].append(detail)
                if detail["error"]:
                    summary["failed"] += 1
                else:
                    summary["succeeded"] += 1
            except Exception as e:
                logger.exception("Enrichment crashed for %s", name)
                summary["failed"] += 1
                summary["details"].append({"handle": name, "error": str(e)})
        return summary
    finally:
        _enriching = False
