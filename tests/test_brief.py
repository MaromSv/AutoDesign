"""Tests for brief / per-candidate prompt resolution."""
from __future__ import annotations

from pipeline.brief import (
    is_placeholder, extract_hypothesis, find_run_brief, prose_brief, resolve_brief,
)


def test_is_placeholder():
    assert is_placeholder("")
    assert is_placeholder("   ")
    assert is_placeholder(None)
    assert is_placeholder("TODO: paste the design brief here, or leave blank")
    assert not is_placeholder("A landing page for a coffee shop")


def test_extract_hypothesis():
    html = ("<html><body>\n"
            "<!-- hypothesis: bold retro fruit-stand poster, chunky grotesk type -->\n"
            "<h1>Hi</h1></body></html>")
    assert extract_hypothesis(html) == "bold retro fruit-stand poster, chunky grotesk type"


def test_extract_hypothesis_absent():
    assert extract_hypothesis("<html><body><h1>Hi</h1></body></html>") == ""
    assert extract_hypothesis("") == ""


def test_find_run_brief_walks_up(tmp_path):
    run = tmp_path / "runs" / "R1"
    cand = run / "gen-000" / "cand-02"
    cand.mkdir(parents=True)
    (run / "brief.txt").write_text("make me a finance dashboard")
    assert find_run_brief(cand) == "make me a finance dashboard"


def test_find_run_brief_ignores_placeholder(tmp_path):
    run = tmp_path / "R"
    cand = run / "gen-000" / "cand-00"
    cand.mkdir(parents=True)
    (run / "brief.txt").write_text("TODO: paste the design brief here")
    assert find_run_brief(cand) == ""


def test_resolve_brief_prefers_real_brief(tmp_path):
    assert resolve_brief("a real brief", tmp_path) == "a real brief"


def test_resolve_brief_falls_back_to_run_brief(tmp_path):
    run = tmp_path / "R"
    cand = run / "gen-000" / "cand-00"
    cand.mkdir(parents=True)
    (run / "brief.txt").write_text("the actual run brief")
    # config brief is the placeholder -> should pick up brief.txt
    assert resolve_brief("TODO: paste the design brief here", cand) == "the actual run brief"


def test_prose_brief_parses_blockquote(tmp_path):
    md = tmp_path / "autodesign.md"
    md.write_text(
        "# Title\n\n## Brief\n\n"
        "> A landing page for **Space Jam** — an indie space game.\n"
        "> Single primary CTA pulls the eye.\n\n"
        "## How this file is consumed\n\nstuff\n"
    )
    out = prose_brief(md)
    assert "Space Jam" in out and "primary CTA" in out
    assert "How this file" not in out  # stops at the next heading


def test_resolve_brief_falls_back_to_prose(tmp_path):
    md = tmp_path / "autodesign.md"
    md.write_text("## Brief\n\n> A calm meditation app landing page.\n\n## Next\n")
    # no brief.txt anywhere; placeholder config brief -> prose
    assert "meditation app" in resolve_brief("", tmp_path / "nope", md)
