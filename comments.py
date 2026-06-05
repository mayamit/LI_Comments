"""Claude-powered comment generation via the `claude` CLI subprocess.

Uses the user's Claude Code subscription rather than the Anthropic API,
so there's no per-call cost beyond the existing Pro/Max plan.
"""
import asyncio
import logging
import os
import tempfile
from typing import Optional

import tones as tones_store
from database import get_db

logger = logging.getLogger(__name__)

def _cli_path() -> str:
    return os.getenv("CLAUDE_CLI", "claude")


def _model() -> str:
    return os.getenv("CLAUDE_MODEL", "claude-sonnet-4-6")


def _timeout_s() -> int:
    return int(os.getenv("CLAUDE_TIMEOUT_S", "120"))


def _build_prompt(
    shared_prompt: str,
    tone: dict,
    display_name: Optional[str],
    handle: str,
    post_content: Optional[str],
) -> str:
    return (
        f"{shared_prompt}\n\n"
        f"---\n\n"
        f"Tone: {tone['name']}\n\n"
        f"{tone['tone_prompt'].strip()}\n\n"
        f"---\n\n"
        f"Author: {display_name or handle} (@{handle})\n\n"
        f"Post:\n{post_content or '(no post content)'}"
    )


async def _call_claude(prompt: str, label: str) -> str:
    """Run the Claude CLI once and return the stripped stdout text.

    Raises RuntimeError on timeout or non-zero exit. `label` is only used to
    make error messages legible (e.g. "tone 'curious'" or "summary").
    """
    timeout = _timeout_s()

    # Run from a neutral cwd so the CLI does not load this project's CLAUDE.md
    # as context.
    proc = await asyncio.create_subprocess_exec(
        _cli_path(),
        "-p",
        prompt,
        "--model",
        _model(),
        cwd=tempfile.gettempdir(),
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    try:
        stdout, stderr = await asyncio.wait_for(
            proc.communicate(), timeout=timeout
        )
    except asyncio.TimeoutError:
        proc.kill()
        await proc.wait()
        raise RuntimeError(f"claude CLI timed out after {timeout}s for {label}")

    if proc.returncode != 0:
        err = stderr.decode("utf-8", errors="replace").strip()
        raise RuntimeError(
            f"claude CLI exited with code {proc.returncode} for {label}: {err}"
        )

    text = stdout.decode("utf-8", errors="replace").strip()
    if text.startswith('"') and text.endswith('"') and len(text) > 1:
        text = text[1:-1].strip()
    return text


async def _generate_one(
    shared_prompt: str,
    tone: dict,
    display_name: Optional[str],
    handle: str,
    post_content: Optional[str],
) -> Optional[str]:
    """Call the Claude CLI once. Returns the comment text or None for SKIP/empty."""
    prompt = _build_prompt(shared_prompt, tone, display_name, handle, post_content)
    text = await _call_claude(prompt, f"tone '{tone['key']}'")
    if not text or text == "SKIP":
        return None
    return text


SUMMARY_PROMPT = (
    "Summarize the LinkedIn post below in 2-3 plain sentences so a reader can "
    "grasp the gist without reading the whole thing. Capture the main point and "
    "any key takeaway. Do not add preamble, a title, hashtags, or quotation "
    "marks — reply with only the summary.\n\n"
    "---\n\n"
    "Post:\n{post_content}"
)


async def generate_summary(post_content: Optional[str]) -> Optional[str]:
    """Produce a short TL;DR for a post. Returns None when there's nothing to
    summarize. Raises RuntimeError on CLI failure (callers isolate this)."""
    if not post_content or not post_content.strip():
        return None
    text = await _call_claude(SUMMARY_PROMPT.format(post_content=post_content), "summary")
    return text or None


async def _generate_one_safe(
    shared_prompt: str,
    tone: dict,
    display_name: Optional[str],
    handle: str,
    post_content: Optional[str],
):
    try:
        return (
            tone,
            await _generate_one(shared_prompt, tone, display_name, handle, post_content),
            None,
        )
    except Exception as e:
        logger.exception("Generation failed for tone %s", tone["key"])
        return (tone, None, e)


async def regenerate_one_tone(post_id: int, tone_key: str) -> Optional[str]:
    """Regenerate a single tone for a post. Replaces the existing row.

    Returns the new content, or None if the model returned SKIP/empty.
    Raises on subprocess / API failure.
    """
    async with get_db() as db:
        cur = await db.execute(
            "SELECT p.id, p.content, h.linkedin_handle, h.display_name "
            "FROM posts p JOIN handles h ON p.handle_id = h.id "
            "WHERE p.id = ?",
            (post_id,),
        )
        post = await cur.fetchone()
    if not post:
        raise ValueError(f"Post {post_id} not found")

    data = tones_store.load()
    tone = next((t for t in data["tones"] if t["key"] == tone_key), None)
    if not tone:
        raise ValueError(f"Tone '{tone_key}' not found")

    content = await _generate_one(
        data["shared_system_prompt"],
        tone,
        post["display_name"],
        post["linkedin_handle"],
        post["content"],
    )

    async with get_db() as db:
        await db.execute(
            "DELETE FROM generated_comments WHERE post_id = ? AND tone = ?",
            (post_id, tone_key),
        )
        if content is not None:
            await db.execute(
                "INSERT INTO generated_comments (post_id, tone, content) VALUES (?, ?, ?)",
                (post_id, tone_key, content),
            )
        await db.commit()
    return content


async def generate_summary_for_post(post_id: int) -> Optional[str]:
    """Generate and persist a TL;DR for a post. Returns the summary, or None if
    there was nothing to summarize. Raises on CLI failure."""
    async with get_db() as db:
        cur = await db.execute("SELECT content FROM posts WHERE id = ?", (post_id,))
        row = await cur.fetchone()
    if not row:
        raise ValueError(f"Post {post_id} not found")

    summary = await generate_summary(row["content"])
    if summary is None:
        return None

    async with get_db() as db:
        await db.execute(
            "UPDATE posts SET summary = ? WHERE id = ?", (summary, post_id)
        )
        await db.commit()
    return summary


async def generate_for_post(post_id: int) -> dict:
    """Generate one comment per tone for a post, in parallel.

    Replaces any existing comments for the post (this is also the regenerate
    path). Per-tone failures are isolated and reported in the summary.
    """
    async with get_db() as db:
        cur = await db.execute(
            "SELECT p.id, p.content, h.linkedin_handle, h.display_name "
            "FROM posts p JOIN handles h ON p.handle_id = h.id "
            "WHERE p.id = ?",
            (post_id,),
        )
        post = await cur.fetchone()
    if not post:
        raise ValueError(f"Post {post_id} not found")

    data = tones_store.load()
    shared = data["shared_system_prompt"]
    tones = [t for t in data["tones"] if tones_store.is_active(t)]

    async with get_db() as db:
        await db.execute("DELETE FROM generated_comments WHERE post_id = ?", (post_id,))
        await db.commit()

    results = await asyncio.gather(
        *[
            _generate_one_safe(
                shared, t, post["display_name"], post["linkedin_handle"], post["content"]
            )
            for t in tones
        ]
    )

    summary = {"generated": 0, "skipped": 0, "errors": []}
    async with get_db() as db:
        for tone, content, err in results:
            if err is not None:
                summary["errors"].append({"tone": tone["key"], "error": str(err)})
                continue
            if content is None:
                summary["skipped"] += 1
                continue
            await db.execute(
                "INSERT INTO generated_comments (post_id, tone, content) VALUES (?, ?, ?)",
                (post_id, tone["key"], content),
            )
            summary["generated"] += 1
        await db.commit()

    logger.info(
        "Generated comments for post %d: %d ok, %d skipped, %d errors",
        post_id,
        summary["generated"],
        summary["skipped"],
        len(summary["errors"]),
    )
    return summary
