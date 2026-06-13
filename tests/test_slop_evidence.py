"""Tests for the AI-pitfall evidence helper (slop-detector -> vlm_judge rubric).

slop_detector is installed in this venv, so `slop_evidence` runs for real against an
HTML snippet; the formatting and principle helpers are pure.
"""
from __future__ import annotations

from pipeline.signals._slop_evidence import (
    ai_pitfalls_principle,
    slop_evidence,
    format_evidence,
)


def test_principle_is_weighted_and_keyed():
    p = ai_pitfalls_principle()
    assert p.key == "ai_pitfalls"
    assert p.weight == 2.5            # heavy by default so it can pull a generic site down
    assert ai_pitfalls_principle(weight=1.0).weight == 1.0
    assert any("fingerprint" in s.lower() for s in p.evaluation_steps)


def test_slop_evidence_flags_a_builder_fingerprint():
    # The gpteng.co script is Lovable / GPT-Engineer's smoking gun.
    html = '<html><head><script src="https://cdn.gpteng.co/gptengineer.js"></script>' \
           '</head><body><h1>Hi</h1></body></html>'
    ev = slop_evidence(html=html, url=None)
    assert ev is not None
    assert ev["score"] > 50                       # strong slop signal
    assert ev["components"]["hard"] > 0.5         # hard fingerprint fired


def test_slop_evidence_clean_html_scores_low():
    ev = slop_evidence(html="<html><body><article>hand written</article></body></html>", url=None)
    assert ev is not None
    assert ev["score"] == 0.0


def test_slop_evidence_no_input_returns_none():
    assert slop_evidence(html=None, url=None) is None
    assert slop_evidence(html="   ", url=None) is None


def test_format_evidence_surfaces_score_and_reasons():
    ev = {"score": 98.2, "label": "Almost certainly AI-generated", "confidence": "high",
          "components": {"hard": 0.98, "copy": 0.1, "structural": 0.2},
          "reasons": ["Hard builder fingerprint detected (Lovable): gpteng.co"]}
    text = format_evidence(ev)
    assert "98.2/100" in text and "0.98" in text
    assert "gpteng.co" in text
