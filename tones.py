"""Tone config loader and CRUD.

Source of truth: tones.yaml at the project root. Editable both directly on
disk and via the admin UI; this module keeps the two paths in sync.
"""
import re
from pathlib import Path
from threading import Lock
from typing import Optional

import yaml

TONES_FILE = Path(__file__).parent / "tones.yaml"
# Shipped template. tones.yaml is gitignored (each user forks their own), so on
# a fresh clone we read from the example until the first edit writes tones.yaml.
EXAMPLE_FILE = Path(__file__).parent / "tones.example.yaml"
KEY_RE = re.compile(r"^[a-z0-9_]{1,40}$")

_lock = Lock()


class TonesYAMLDumper(yaml.SafeDumper):
    pass


def _str_representer(dumper, data):
    if "\n" in data:
        return dumper.represent_scalar("tag:yaml.org,2002:str", data, style="|")
    return dumper.represent_scalar("tag:yaml.org,2002:str", data)


TonesYAMLDumper.add_representer(str, _str_representer)


class ToneError(ValueError):
    pass


def load() -> dict:
    """Read tones fresh on every call so edits land immediately. Falls back to
    tones.example.yaml when the user hasn't created their own tones.yaml yet."""
    source = TONES_FILE if TONES_FILE.exists() else EXAMPLE_FILE
    with _lock:
        with source.open() as f:
            data = yaml.safe_load(f) or {}
    data.setdefault("shared_system_prompt", "")
    data.setdefault("tones", [])
    return data


def _save(data: dict) -> None:
    """Atomic write."""
    with _lock:
        tmp = TONES_FILE.with_suffix(".yaml.tmp")
        with tmp.open("w") as f:
            yaml.dump(
                data,
                f,
                Dumper=TonesYAMLDumper,
                sort_keys=False,
                allow_unicode=True,
                width=80,
            )
        tmp.replace(TONES_FILE)


def get_shared_system_prompt() -> str:
    return load()["shared_system_prompt"]


def is_active(tone: dict) -> bool:
    """Tones default to active when the 'active' field is missing."""
    return bool(tone.get("active", True))


def get_all() -> list[dict]:
    return load()["tones"]


def get_active() -> list[dict]:
    return [t for t in load()["tones"] if is_active(t)]


def get(key: str) -> Optional[dict]:
    for t in load()["tones"]:
        if t["key"] == key:
            return t
    return None


def set_active(key: str, active: bool) -> None:
    data = load()
    for t in data["tones"]:
        if t["key"] == key:
            t["active"] = bool(active)
            _save(data)
            return
    raise ToneError(f"Tone '{key}' not found.")


def _validate(key: str, name: str, tone_prompt: str) -> None:
    if not KEY_RE.match(key):
        raise ToneError(
            f"Invalid key '{key}'. Use lowercase letters, numbers, and underscores (max 40 chars)."
        )
    if not name.strip():
        raise ToneError("Name is required.")
    if not tone_prompt.strip():
        raise ToneError("Tone prompt is required.")


def create(key: str, name: str, description: str, tone_prompt: str) -> None:
    _validate(key, name, tone_prompt)
    data = load()
    if any(t["key"] == key for t in data["tones"]):
        raise ToneError(f"A tone with key '{key}' already exists.")
    data["tones"].append(
        {
            "key": key,
            "name": name.strip(),
            "description": description.strip(),
            "tone_prompt": tone_prompt.strip() + "\n",
        }
    )
    _save(data)


def update(key: str, name: str, description: str, tone_prompt: str) -> None:
    if not name.strip() or not tone_prompt.strip():
        raise ToneError("Name and tone prompt are required.")
    data = load()
    for t in data["tones"]:
        if t["key"] == key:
            t["name"] = name.strip()
            t["description"] = description.strip()
            t["tone_prompt"] = tone_prompt.strip() + "\n"
            _save(data)
            return
    raise ToneError(f"Tone '{key}' not found.")


def delete(key: str) -> None:
    data = load()
    new_tones = [t for t in data["tones"] if t["key"] != key]
    if len(new_tones) == len(data["tones"]):
        raise ToneError(f"Tone '{key}' not found.")
    data["tones"] = new_tones
    _save(data)


def update_shared_prompt(text: str) -> None:
    data = load()
    data["shared_system_prompt"] = text.strip() + "\n"
    _save(data)
