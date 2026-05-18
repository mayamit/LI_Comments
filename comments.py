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


async def _generate_one(
    shared_prompt: str,
    tone: dict,
    display_name: Optional[str],
    handle: str,
    post_content: Optional[str],
) -> Optional[str]:
    """Call the Claude CLI once. Returns the comment text or None for SKIP/empty."""
    prompt = _build_prompt(shared_prompt, tone, display_name, handle, post_content)
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
        raise RuntimeError(
            f"claude CLI timed out after {timeout}s for tone '{tone['key']}'"
        )

    if proc.returncode != 0:
        err = stderr.decode("utf-8", errors="replace").strip()
        raise RuntimeError(
            f"claude CLI exited with code {proc.returncode} for tone '{tone['key']}': {err}"
        )

    text = stdout.decode("utf-8", errors="replace").strip()
    if text.startswith('"') and text.endswith('"') and len(text) > 1:
        text = text[1:-1].strip()
    if not text or text == "SKIP":
        return None
    return text


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
    tones = data["tones"]

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
