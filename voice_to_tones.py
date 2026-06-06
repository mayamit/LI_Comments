#!/usr/bin/env python3
"""Convert a voice-profile markdown file into a personalised tones.yaml.

This is the setup tool that makes the app personalisable: a new user drops in
their own voice profile (a free-form markdown document describing how they
write) and this script distils it into the structured tones.yaml the comment
generator loads.

The app's *tone taxonomy* is fixed product structure (operator, strategic,
curious, ...). What is personal is two things, and only those are generated
from the profile:
  1. the shared_system_prompt (who you are, your voice, your hard nos), and
  2. one short example reply per tone, written in your voice.

Everything else (the per-tone mechanics, the universal anti-AI-slop rules, the
length/output/spirit guidance) is fixed scaffold, so the output is consistent
no matter whose profile goes in.

Generation runs through the same `claude` CLI the app uses for comments, so it
relies on the user's Claude Code subscription rather than an API key.

Usage:
    python voice_to_tones.py my-voice-profile.md
    python voice_to_tones.py my-voice-profile.md -o tones.yaml --force
    python voice_to_tones.py my-voice-profile.md --model claude-opus-4-8
"""
import argparse
import json
import os
import subprocess
import sys
import tempfile
from pathlib import Path

import yaml

try:
    # Reuse the app's dumper so the generated file is byte-for-byte the same
    # style (block scalars for multiline) as one edited through the admin UI.
    from tones import TonesYAMLDumper
except Exception:  # pragma: no cover - allow running outside the project too
    class TonesYAMLDumper(yaml.SafeDumper):
        pass

    def _str_representer(dumper, data):
        style = "|" if "\n" in data else None
        return dumper.represent_scalar("tag:yaml.org,2002:str", data, style=style)

    TonesYAMLDumper.add_representer(str, _str_representer)


# --- Fixed tone scaffold ----------------------------------------------------
# key, name, description, and the tone *mechanics* (how to write in this tone).
# These describe the comment style, not the person, so they stay constant.
# The per-tone voice example is appended from the model's output.
TONE_SCAFFOLD = [
    {
        "key": "operator",
        "name": "Operator Lens",
        "description": "Ground-level, practical execution perspective",
        "instruction": (
            "Write as a hands-on operator who has shipped this kind of work. "
            "Name a real friction or tradeoff that does not show up in the "
            "post. Engage with what is actually hard about doing the thing, "
            "not the headline. No invented anecdotes."
        ),
    },
    {
        "key": "strategic",
        "name": "Strategic",
        "description": "Big picture, market or business angle",
        "instruction": (
            "Pull the conversation up one level: market dynamics, business "
            "model, or an industry shift. Reference one concrete pattern, a "
            "comparable company, a sector trend, or a real second-order "
            "effect. A skeptical register usually lands best here."
        ),
    },
    {
        "key": "curious",
        "name": "Curious",
        "description": "Question-led, invites dialogue",
        "instruction": (
            "Lead with one specific question the post almost-but-not-quite "
            "answered. The question must come from having actually thought "
            "about the post. Not a Socratic trap, not a 'what do you think?' "
            "platitude. Answering it should sharpen the author's own "
            "thinking. One question, optionally one short framing line. Never "
            "two questions."
        ),
    },
    {
        "key": "contrarian",
        "name": "Contrarian",
        "description": "Respectful pushback or alternative view",
        "instruction": (
            "Disagree with one specific claim or framing in the post. Never "
            "the whole post, never the author. Acknowledge what is true, then "
            "offer the alternative read. Direct contrast over hedging. No "
            "sarcasm, no 'Actually,', no point-scoring."
        ),
    },
    {
        "key": "affirming",
        "name": "Affirming",
        "description": "Builds on their point, adds a layer",
        "instruction": (
            "Agree, then earn the comment by adding what the author did not "
            "say: a real example, a second-order implication, or a place "
            "where the argument is even stronger than they framed it. Lead "
            "with the addition, not the agreement. Affirmation lives in the "
            "substance, not the words."
        ),
    },
    {
        "key": "concise",
        "name": "Concise",
        "description": "One punchy sentence",
        "instruction": (
            "One sentence. Under 25 words. Sharp observation, tight reframe, "
            "or memorable line. Never tepid agreement. No hedges (perhaps, I "
            "think, kind of). No setup.\n\n"
            "If you cannot land it in one sentence, return the single token: "
            "SKIP."
        ),
    },
    {
        "key": "reference",
        "name": "Popular Reference",
        "description": "Citing a reference from a book, song, or popular culture",
        "instruction": (
            "Write a comment that:\n\n"
            "Is 3-5 sentences max, tight, no fluff\n"
            "Opens with a hook: a surprising angle, a contrarian take, or a "
            "bold observation\n"
            "Weaves in a reference from a book, film, music, art, "
            "architecture, sport, or pop culture, used as a metaphor or lens, "
            "not a name-drop\n"
            "Feels like it comes from someone well-read and curious, not a "
            "consultant rattling off buzzwords\n"
            "Ends with either a sharp one-liner or an open question that "
            "invites replies\n"
            "Tone: conversational and a little irreverent, like a smart "
            "friend at a dinner table, not a keynote speaker"
        ),
    },
]

TONE_KEYS = [t["key"] for t in TONE_SCAFFOLD]

# --- Fixed shared-prompt sections (not person-specific) ---------------------
# Anti-AI-slop hygiene that applies to anyone's comments. The model adds the
# person-specific hard nos on top of these.
UNIVERSAL_HARD_NOS = [
    'AI words: surfaced, fostered, utilized, paradigm, mitigated, leveraged, '
    'moreover, holistic, synergy, streamlined, pivoted, ecosystem, robust, '
    'facilitate, cultivate.',
    'AI phrases: "delve into", "landscape" (the noun), "in today\'s '
    'fast-paced world", "game changer", "unlock the power of". Avoid the '
    '"not only X but Y" construction.',
    'LinkedIn slop hooks: "Stop doing X. Do Y", "Unpopular opinion", "Read '
    'that again", "Let that sink in", "Here is the shift", "And here is the '
    'kicker", "Not because X. Because Y".',
    'Generic openers: "Great post!", "Love this!", "Thanks for sharing", '
    '"100% agree".',
    'Multiple emojis. A single ":)" is allowed only as a warmth signal.',
    'Invented anecdotes. If you have not earned a specific story, skip it. No '
    '"I once worked on a project where...". No claims of clients, deals, or '
    'numbers you have not actually provided.',
    'Summarizing the post back to the author. They wrote it.',
]

LENGTH_SECTION = (
    "- 90 words maximum. Tighter is better.\n"
    "- The concise tone overrides this and demands a single sentence, or two "
    "at the maximum."
)

OUTPUT_SECTION = (
    "- Return only the comment text. No preamble, no quotes, no labels, no "
    "signature. It should read like a real person talking, not a rule being "
    "followed or a template being filled in."
)


def _spirit_section(name: str) -> str:
    return (
        "These are taste, not a checklist. Inhabit the voice, do not imitate "
        "it. A comment that uses three of these tendencies naturally beats one "
        "that forces in ten. The hard nos above are absolute. Everything else "
        "is a tendency, so apply it with judgment. Before finalizing, ask: "
        f"does this sound like something {name} would actually write, or like "
        "an AI trying hard to imitate them? If it feels forced, pull back."
    )


# --- The extraction request -------------------------------------------------
EXTRACTION_INSTRUCTIONS = """\
You are distilling a person's writing voice profile into a compact spec used to
generate short LinkedIn reply comments in their voice.

Read the VOICE PROFILE below and return ONE JSON object, and nothing else. No
markdown fences, no commentary. The JSON must have exactly these keys:

{
  "name": string,            // the writer's display name
  "who_you_are": string,     // 2-4 sentences: who they are, their core belief,
                             //   what they value. Written as second person
                             //   ("You are ...").
  "voice": string,           // 2-4 sentences on sentence rhythm and register:
                             //   length, fragments, punctuation habits, the
                             //   register they write best in. Second person.
  "personality": string,     // 2-4 sentences on character: humor style, use of
                             //   analogy/reference, emotional engine, warmth.
                             //   Second person.
  "hard_nos": [string],      // 3-8 PERSON-SPECIFIC things they never do:
                             //   words/phrases they hate, off-limits topics,
                             //   stylistic bans drawn from the profile. Do NOT
                             //   repeat generic anti-AI rules; those are added
                             //   separately. Each item one short sentence.
  "vocabulary_freely": [string],   // 5-12 words/phrases they reach for often
  "vocabulary_sparingly": [string],// 3-8 words they ration for impact
  "tone_examples": {         // ONE example reply per tone, in their voice.
    "operator": string,      //   1-2 sentences, no surrounding quotes.
    "strategic": string,     //   Make each genuinely sound like them, using
    "curious": string,       //   their phrasing, rhythm, and beliefs. These
    "contrarian": string,    //   are the single most important output: they
    "affirming": string,     //   teach the generator the voice by example.
    "concise": string,
    "reference": string
  }
}

The seven tones mean:
- operator: ground-level, practical execution perspective; names a real friction
- strategic: big-picture market or business angle; skeptical register
- curious: leads with one sharp question that sharpens the author's thinking
- contrarian: respectful pushback on one specific claim; direct contrast
- affirming: agrees, then adds a real example or second-order implication
- concise: one punchy sentence, under 25 words
- reference: weaves in a book/film/music/pop-culture metaphor; well-read, irreverent

Constraints:
- Keep who_you_are / voice / personality tight. This is a system prompt, not an
  essay. No bullet points inside those strings.
- Derive everything from the profile. Do not invent biography, clients, or
  numbers the profile does not state.
- If the profile bans em dashes (or any punctuation/phrase), include that in
  hard_nos. Match the profile's actual preferences, not generic defaults.

VOICE PROFILE:
---
{profile}
---

Return only the JSON object now."""


def parse_args(argv=None):
    p = argparse.ArgumentParser(
        description="Convert a voice-profile markdown file into a tones.yaml.",
    )
    p.add_argument("profile", help="Path to the voice-profile markdown file.")
    p.add_argument(
        "-o",
        "--output",
        default="tones.yaml",
        help="Output path (default: tones.yaml).",
    )
    p.add_argument(
        "--force",
        action="store_true",
        help="Overwrite the output file if it already exists.",
    )
    p.add_argument(
        "--model",
        default=os.getenv("CLAUDE_MODEL", "claude-sonnet-4-6"),
        help="Claude model to use (default: $CLAUDE_MODEL or claude-sonnet-4-6).",
    )
    p.add_argument(
        "--cli",
        default=os.getenv("CLAUDE_CLI", "claude"),
        help="Path to the claude CLI (default: $CLAUDE_CLI or 'claude').",
    )
    p.add_argument(
        "--timeout",
        type=int,
        default=int(os.getenv("CLAUDE_TIMEOUT_S", "300")),
        help="CLI timeout in seconds (default: 300).",
    )
    return p.parse_args(argv)


def call_claude(prompt: str, model: str, cli: str, timeout: int) -> str:
    """Run the Claude CLI once and return stripped stdout.

    Runs from a temp dir so the CLI does not pick up this project's CLAUDE.md
    as context, matching how the app calls it.
    """
    try:
        proc = subprocess.run(
            [cli, "-p", prompt, "--model", model],
            cwd=tempfile.gettempdir(),
            capture_output=True,
            text=True,
            timeout=timeout,
        )
    except FileNotFoundError:
        raise SystemExit(
            f"error: could not find the '{cli}' CLI. Install Claude Code or "
            f"set --cli / $CLAUDE_CLI to its path."
        )
    except subprocess.TimeoutExpired:
        raise SystemExit(f"error: claude CLI timed out after {timeout}s.")
    if proc.returncode != 0:
        raise SystemExit(
            f"error: claude CLI exited {proc.returncode}: {proc.stderr.strip()}"
        )
    return proc.stdout.strip()


def extract_json(text: str) -> dict:
    """Pull the JSON object out of the model response, tolerating fences."""
    s = text.strip()
    if s.startswith("```"):
        # drop the first fence line and any trailing fence
        s = s.split("\n", 1)[1] if "\n" in s else s
        if s.rstrip().endswith("```"):
            s = s.rstrip()[:-3]
    start, end = s.find("{"), s.rfind("}")
    if start == -1 or end == -1 or end < start:
        raise ValueError("no JSON object found in the model response")
    return json.loads(s[start : end + 1])


def validate(data: dict) -> None:
    required = [
        "name",
        "who_you_are",
        "voice",
        "personality",
        "hard_nos",
        "vocabulary_freely",
        "vocabulary_sparingly",
        "tone_examples",
    ]
    missing = [k for k in required if k not in data or not data[k]]
    if missing:
        raise ValueError(f"model output missing keys: {', '.join(missing)}")
    if not isinstance(data["tone_examples"], dict):
        raise ValueError("tone_examples must be an object")
    missing_ex = [k for k in TONE_KEYS if not data["tone_examples"].get(k)]
    if missing_ex:
        raise ValueError(
            f"tone_examples missing tones: {', '.join(missing_ex)}"
        )


def build_shared_prompt(data: dict) -> str:
    name = str(data["name"]).strip()
    hard_nos = ["Em dashes. None ever. Use a comma, a period, or a parenthetical."] \
        if _bans_em_dash(data) else []
    # Person-specific nos first (em dash handled above), then the universal
    # hygiene rules. Drop any person-no that just restates a universal one so
    # the list does not repeat itself.
    person_nos = [
        str(n).strip()
        for n in data["hard_nos"]
        if str(n).strip()
        and "em dash" not in str(n).lower()
        and not _is_universal_dupe(str(n))
    ]
    all_nos = hard_nos + person_nos + UNIVERSAL_HARD_NOS
    numbered = "\n".join(f"  {i + 1}. {n}" for i, n in enumerate(all_nos))

    freely = ", ".join(str(w).strip() for w in data["vocabulary_freely"])
    sparingly = ", ".join(str(w).strip() for w in data["vocabulary_sparingly"])

    return (
        f"You are writing a LinkedIn reply comment as {name}.\n\n"
        f"WHO YOU ARE\n{str(data['who_you_are']).strip()}\n\n"
        f"VOICE\n{str(data['voice']).strip()}\n\n"
        f"PERSONALITY\n{str(data['personality']).strip()}\n\n"
        f"HARD NOS (never use any of these)\n{numbered}\n\n"
        f"VOCABULARY\n"
        f"- Diagnostic words (use freely): {freely}.\n"
        f"- Character words (use sparingly): {sparingly}. One per comment max.\n\n"
        f"LENGTH\n{LENGTH_SECTION}\n\n"
        f"OUTPUT\n{OUTPUT_SECTION}\n\n"
        f"SPIRIT (read this last, it overrides the rest)\n{_spirit_section(name)}\n"
    )


def _bans_em_dash(data: dict) -> bool:
    return any("em dash" in str(n).lower() for n in data["hard_nos"])


# Markers that mean a person-specific no is really just one of the universal
# anti-AI-slop rules we already append, so we can drop it to avoid repetition.
_UNIVERSAL_MARKERS = (
    "delve into", "game changer", "unlock the power", "fast-paced",
    "thanks for sharing", "great post", "love this", "100% agree",
    "not only x", "multiple emoji", "read till the end", "read that again",
    "let that sink in", "unpopular opinion",
)


def _is_universal_dupe(no: str) -> bool:
    low = no.lower()
    return any(marker in low for marker in _UNIVERSAL_MARKERS)


def build_tones(data: dict) -> list:
    name = str(data["name"]).strip()
    examples = data["tone_examples"]
    tones = []
    for t in TONE_SCAFFOLD:
        example = str(examples[t["key"]]).strip().strip('"')
        prompt = (
            f"{t['instruction']}\n\n"
            f"In {name}'s voice (example reply): \"{example}\"\n"
        )
        tones.append(
            {
                "key": t["key"],
                "name": t["name"],
                "description": t["description"],
                "tone_prompt": prompt,
            }
        )
    return tones


def write_yaml(data: dict, path: Path) -> None:
    tmp = path.with_suffix(path.suffix + ".tmp")
    with tmp.open("w") as f:
        yaml.dump(
            data,
            f,
            Dumper=TonesYAMLDumper,
            sort_keys=False,
            allow_unicode=True,
            width=80,
        )
    tmp.replace(path)


def main(argv=None) -> int:
    args = parse_args(argv)

    profile_path = Path(args.profile)
    if not profile_path.is_file():
        raise SystemExit(f"error: profile not found: {profile_path}")

    out_path = Path(args.output)
    if out_path.exists() and not args.force:
        raise SystemExit(
            f"error: {out_path} already exists. Re-run with --force to "
            f"overwrite (or pass -o to a different path)."
        )

    profile_text = profile_path.read_text(encoding="utf-8")
    prompt = EXTRACTION_INSTRUCTIONS.replace("{profile}", profile_text)

    print(f"Distilling {profile_path.name} with {args.model} ...", file=sys.stderr)
    raw = call_claude(prompt, args.model, args.cli, args.timeout)

    try:
        data = extract_json(raw)
        validate(data)
    except (ValueError, json.JSONDecodeError) as e:
        debug = out_path.with_suffix(".raw.txt")
        debug.write_text(raw, encoding="utf-8")
        raise SystemExit(
            f"error: could not parse model output: {e}\n"
            f"Raw response saved to {debug} for inspection."
        )

    result = {
        "shared_system_prompt": build_shared_prompt(data),
        "tones": build_tones(data),
    }
    write_yaml(result, out_path)

    print(
        f"Wrote {out_path} for {data['name']}: "
        f"{len(result['tones'])} tones, "
        f"{len(result['shared_system_prompt'])} char shared prompt.",
        file=sys.stderr,
    )
    print("Review it, then start the app to generate comments in your voice.",
          file=sys.stderr)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
