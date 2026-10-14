"""Wordle dictionary loading and guess legality."""

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from plugins.Wordle import dictionaries


def test_all_expected_dictionaries_are_present():
    names = dictionaries.list_dictionaries()
    assert names[0] == "CET4"  # default first
    for expected in ("CET4", "CET6", "GRE", "考研", "专四", "专八", "TOEFL", "SAT"):
        assert expected in names


def test_dictionary_entries_filter_by_length_and_carry_meaning():
    entries = dictionaries.dictionary_entries("CET4", 5)
    assert entries, "CET4 should have 5-letter words"
    assert all(len(word) == 5 for word, _ in entries)
    assert any(meaning for _, meaning in entries)


def test_is_legal_accepts_dictionary_words_and_rejects_junk():
    assert dictionaries.is_legal("apple") is True
    assert dictionaries.is_legal("crane") is True
    assert dictionaries.is_legal("qwertyuiop") is False
    assert dictionaries.is_legal("zzzzz") is False
    assert dictionaries.is_legal("") is False
