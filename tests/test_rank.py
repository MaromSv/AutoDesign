"""Offline tests for the rank harness's pure helpers.

The capture + model-call paths need a browser and an API key, so they're not exercised
here; this covers the labeled-file parser, the .env loader, the slug, and the
good-vs-bad separation verdict math.
"""
from __future__ import annotations

import os

from pipeline.rank import parse_labeled_file, _load_dotenv, _slug, _separation_inversions


def test_parse_labeled_file(tmp_path):
    f = tmp_path / "ex.txt"
    f.write_text("Bad:\nhttps://a.lovable.app/\nhttps://b.com\nGood:\nhttps://c.io/\n")
    labels = parse_labeled_file(f)
    assert labels == {
        "https://a.lovable.app/": "bad",
        "https://b.com": "bad",
        "https://c.io/": "good",
    }


def test_load_dotenv_sets_only_missing_keys(tmp_path, monkeypatch):
    env = tmp_path / ".env"
    env.write_text('ANTHROPIC_API_KEY="sk-test-123"\n# comment\nALREADY=fromfile\n')
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    monkeypatch.setenv("ALREADY", "fromenv")
    _load_dotenv(env)
    assert os.environ["ANTHROPIC_API_KEY"] == "sk-test-123"  # quotes stripped, set
    assert os.environ["ALREADY"] == "fromenv"                # pre-set value not overwritten


def test_load_dotenv_missing_file_is_noop(tmp_path):
    _load_dotenv(tmp_path / "nope.env")  # must not raise


def test_slug_is_filesystem_safe():
    assert _slug("https://www.sui.io/") == "www.sui.io"
    assert "/" not in _slug("https://a.com/path/to/page?q=1")


def test_separation_inversions_counts_bad_outranking_good():
    # one bad (8.0) beats one good (7.0) -> 1 inversion of the 2x2 = 4 pairs... here 2 good, 2 bad
    good = [9.0, 7.0]
    bad = [8.0, 5.0]
    assert _separation_inversions(good, bad) == 1  # only (good=7.0, bad=8.0)
