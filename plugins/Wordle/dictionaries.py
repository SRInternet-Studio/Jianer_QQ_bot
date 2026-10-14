"""Vendored exam dictionaries for the Wordle plugin.

The JSON files come from ``nonebot-plugin-wordle`` (MIT,
https://github.com/noneplugin/nonebot-plugin-wordle).  Each file maps a word to
``{"中释": ..., "英释": ...}``.  Loading is lazy and cached.
"""

from __future__ import annotations

import json
import re
import threading
from pathlib import Path
from typing import Any

from plugins.Wordle.game import (
    MIN_LENGTH,
    MAX_LENGTH,
    normalize_word,
)


WORDS_DIR = Path(__file__).resolve().parent / "resources" / "words"
DEFAULT_DICTIONARY = "CET4"

_LOCK = threading.RLock()
_RAW_CACHE: dict[str, dict[str, str]] = {}
_LEGAL_CACHE: set[str] | None = None
SYSTEM_DICTIONARY = Path("/usr/share/dict/cracklib-small")


def list_dictionaries() -> list[str]:
    """Return available dictionary names, default first."""

    if not WORDS_DIR.is_dir():
        return [DEFAULT_DICTIONARY]
    names = sorted(f.stem for f in WORDS_DIR.iterdir() if f.suffix == ".json")
    if not names:
        return [DEFAULT_DICTIONARY]
    if DEFAULT_DICTIONARY in names:
        names.remove(DEFAULT_DICTIONARY)
        names.insert(0, DEFAULT_DICTIONARY)
    return names


def _load_raw(name: str) -> dict[str, str]:
    with _LOCK:
        cached = _RAW_CACHE.get(name)
        if cached is not None:
            return cached
    path = WORDS_DIR / f"{name}.json"
    entries: dict[str, str] = {}
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        raw = {}
    if isinstance(raw, dict):
        for word, payload in raw.items():
            normalized = normalize_word(word)
            if not normalized:
                continue
            meaning = ""
            if isinstance(payload, dict):
                meaning = str(
                    payload.get("中释")
                    or payload.get("meaning")
                    or payload.get("英释")
                    or ""
                ).strip()
            elif isinstance(payload, str):
                meaning = payload.strip()
            entries.setdefault(normalized, meaning)
    with _LOCK:
        _RAW_CACHE[name] = entries
    return entries


def dictionary_entries(
    name: str,
    length: int,
) -> list[tuple[str, str]]:
    """Return ``(word, meaning)`` pairs of the requested length."""

    entries = _load_raw(name)
    return [
        (word, meaning)
        for word, meaning in entries.items()
        if len(word) == int(length)
    ]


def _legal_words() -> set[str]:
    global _LEGAL_CACHE
    with _LOCK:
        if _LEGAL_CACHE is not None:
            return _LEGAL_CACHE
    words: set[str] = set()
    for name in list_dictionaries():
        words.update(_load_raw(name))
    if SYSTEM_DICTIONARY.is_file():
        try:
            text = SYSTEM_DICTIONARY.read_text(encoding="utf-8", errors="ignore")
        except OSError:
            text = ""
        for line in text.splitlines():
            candidate = normalize_word(line)
            if candidate and re.fullmatch(r"[a-z]+", candidate):
                words.add(candidate)
    with _LOCK:
        _LEGAL_CACHE = words
    return words


def is_legal(word: str) -> bool:
    """A guess is legal when it is a known English word we recognize."""

    candidate = normalize_word(word)
    if not candidate:
        return False
    return candidate in _legal_words()
