"""Offline tests for the prompt_consistency signal.

No network: registration, graceful skips (no brief / no content / no key), content
assembly, and score coercion. The Nemotron call itself needs an API key.
"""
from __future__ import annotations

from pathlib import Path

from pipeline.context import CandidateContext
from pipeline.registry import get_signals
from pipeline.signals.prompt_consistency import _coerce_score, _build_user_content, _MAX_CODE_CHARS
import pipeline.signals  # noqa: F401  (ensure registration)


def _ctx(**kw) -> CandidateContext:
    base = dict(candidate_dir=Path("/tmp"), html_path=None, html_url=None, frames=[],
                code_text="<h1>Hi</h1>", brief="A landing page", config={})
    base.update(kw)
    return CandidateContext(**base)


def test_signal_registered():
    assert "prompt_consistency" in get_signals()


def test_skips_without_brief():
    res = get_signals()["prompt_consistency"].score(_ctx(brief="   "))
    assert res.score is None and "no brief" in res.skipped


def test_skips_without_content():
    res = get_signals()["prompt_consistency"].score(_ctx(code_text="", frames=[]))
    assert res.score is None and "no generated content" in res.skipped


def test_skips_without_key(monkeypatch):
    # Stub the .env autoloader so the developer's real key can't mask the no-key path.
    monkeypatch.setattr("pipeline.envfile.ensure_loaded", lambda: None)
    monkeypatch.delenv("NEBIUS_API_KEY", raising=False)
    res = get_signals()["prompt_consistency"].score(_ctx())
    assert res.score is None
    assert "NEBIUS_API_KEY" in res.skipped


def test_build_content_text_only():
    content = _build_user_content("brief", "", "<h1>x</h1>", None)
    assert isinstance(content, str)
    assert "brief" in content and "<h1>x</h1>" in content


def test_build_content_includes_generation_prompt():
    content = _build_user_content("the run brief", "make a watermelon poster", "<h1>x</h1>", None)
    assert "the run brief" in content
    assert "watermelon poster" in content
    assert "THIS CANDIDATE'S PROMPT" in content


def test_build_content_truncates_code():
    big = "x" * (_MAX_CODE_CHARS + 500)
    content = _build_user_content("brief", "", big, None)
    assert "(truncated)" in content
    assert len(content) < len(big) + 2000


def test_build_content_with_frame(tmp_path):
    # a tiny valid PNG header is enough for base64 encoding to run
    frame = tmp_path / "f.png"
    frame.write_bytes(b"\x89PNG\r\n\x1a\n" + b"0" * 32)
    content = _build_user_content("brief", "", "<h1>x</h1>", frame)
    assert isinstance(content, list)
    assert content[0]["type"] == "text"
    assert content[1]["type"] == "image_url"


def test_coerce_score_clamps():
    assert _coerce_score({"score": 11}) == 10.0
    assert _coerce_score({"score": "x"}) is None
    assert _coerce_score(None) is None
