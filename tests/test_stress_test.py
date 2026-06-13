"""Offline tests for the agentic stress_test signal.

No network/browser: registration, target resolution, persona loading, issue
normalization, per-persona + merged scoring, and the deterministic heuristic. The live
Playwright subagent run and the Nemotron calls are exercised via browser_agent tests
with fakes; here we test the merge/scoring math and the no-key/no-target skips.
"""
from __future__ import annotations

from pathlib import Path

from pipeline.context import CandidateContext
from pipeline.interact import Interaction, InteractionReport
from pipeline.registry import get_signals
from pipeline.signals.stress_test import (
    _resolve_target, _load_personas, _norm_issues, _persona_score, _merge,
    _heuristic_score, DEFAULT_PERSONAS,
)
from pipeline.browser_agent import AgentResult
import pipeline.signals  # noqa: F401  (ensure registration)


def _ctx(**kw) -> CandidateContext:
    base = dict(candidate_dir=Path("/tmp"), html_path=None, html_url=None, frames=[],
                code_text="", brief="A landing page", config={})
    base.update(kw)
    return CandidateContext(**base)


def _result(name, *, achieved, issues=None, stopped="finish", session=None, steps=0):
    return AgentResult(
        persona={"name": name, "goal": "g"},
        findings={"goal_achieved": achieved, "summary": "did stuff", "issues": issues or []},
        steps=[None] * steps,
        stopped=stopped,
        session=session or {"clicks": 0, "dead_clicks": 0, "console_errors": 0},
    )


def test_signal_registered():
    assert "stress_test" in get_signals()


def test_skips_without_target(tmp_path):
    res = get_signals()["stress_test"].score(_ctx(candidate_dir=tmp_path))
    assert res.score is None and "no renderable target" in res.skipped


def test_skips_without_key(monkeypatch, tmp_path):
    # Real target present, but no Nemotron and no playwright -> falls back to probe, which
    # skips for lack of playwright OR a missing target. Either way: score is None, no crash.
    monkeypatch.setattr("pipeline.envfile.ensure_loaded", lambda: None)
    monkeypatch.delenv("NEBIUS_API_KEY", raising=False)
    html = tmp_path / "index.html"
    html.write_text("<html><body><a href='#'>x</a></body></html>")
    res = get_signals()["stress_test"].score(_ctx(candidate_dir=tmp_path, html_path=html))
    assert res.score is None or isinstance(res.score, float)  # heuristic probe path, never raises


def test_resolve_target_prefers_live_url():
    assert _resolve_target(_ctx(html_url="https://example.com")) == "https://example.com"


def test_resolve_target_prefers_page_html(tmp_path):
    (tmp_path / "page.html").write_text("<html></html>")
    html = tmp_path / "index.html"
    html.write_text("<html></html>")
    assert _resolve_target(_ctx(candidate_dir=tmp_path, html_path=html)).endswith("page.html")


# ----------------------------------------------------------------- personas
def test_load_personas_defaults():
    assert _load_personas({}) is DEFAULT_PERSONAS
    assert _load_personas({"personas": []}) is DEFAULT_PERSONAS


def test_load_personas_custom():
    p = _load_personas({"personas": [{"name": "shopper", "goal": "buy something"}]})
    assert p == [{"name": "shopper", "goal": "buy something"}]


def test_load_personas_drops_invalid():
    # entries without a goal are dropped; if none survive, fall back to defaults
    assert _load_personas({"personas": [{"name": "x"}]}) is DEFAULT_PERSONAS


# ----------------------------------------------------------------- issue normalization
def test_norm_issues_dicts_and_strings():
    out = _norm_issues([
        {"severity": "HIGH", "description": "dead CTA"},
        "nav link 404",
        {"severity": "bogus", "description": "weird"},
        {"description": ""},  # dropped
    ])
    assert out[0] == {"severity": "high", "description": "dead CTA"}
    assert out[1] == {"severity": "medium", "description": "nav link 404"}
    assert out[2]["severity"] == "medium"  # unknown severity coerced
    assert len(out) == 3


# ----------------------------------------------------------------- scoring
def test_persona_score_clean_success_is_high():
    r = _result("v", achieved=True, issues=[])
    assert _persona_score(r) == 10.0  # goal achieved, no issues -> full marks


def test_persona_score_single_issue_is_a_minor_ding():
    # One issue should NOT crater the score — forgiving first issue (quadratic curve).
    r = _result("v", achieved=True, issues=[{"severity": "high", "description": "dead CTA"}])
    # base 10 - load(1.3)^2 = 10 - 1.69 = 8.31
    assert _persona_score(r) == 8.31


def test_persona_score_penalizes_issues_and_defects():
    # Several real problems (issue + JS console error + dead clicks) compound -> genuinely bad.
    r = _result("v", achieved=True,
                issues=[{"severity": "high", "description": "dead CTA"}],
                session={"clicks": 4, "dead_clicks": 2, "console_errors": 1})
    # load = 1.3 (high) + 0.3*2 (dead) + 1.0 (console) = 2.9 ; base 10 - 2.9^2 = 10 - 8.41 = 1.59
    assert _persona_score(r) == 1.59


def test_persona_score_timeout_without_goal_is_capped():
    # Not achieving a goal on a one-page demo isn't proof of breakage -> moderate, not near-zero.
    r = _result("v", achieved=False, issues=[], stopped="max_steps")
    assert _persona_score(r) <= 5.0


def test_merge_averages_personas_and_collects_issues():
    results = [
        _result("a", achieved=True, issues=[]),
        _result("b", achieved=False, issues=[{"severity": "medium", "description": "form fails"}]),
    ]
    overall, details = _merge(results)
    assert details["mode"] == "subagents"
    assert details["n_personas"] == 2
    assert isinstance(overall, float)
    # each issue is tagged with its persona for the combined feedback list
    assert details["issues"][0]["persona"] == "b"


# ----------------------------------------------------------------- heuristic fallback math
def test_heuristic_all_working():
    report = InteractionReport(target="x", n_interactive=2, interactions=[
        Interaction(label="a", tag="button", selector="b", clicked=True, dom_changed=True),
        Interaction(label="b", tag="a", selector="a", href="/x", clicked=True, navigated=True),
    ])
    assert _heuristic_score(report) == 10.0


def test_heuristic_dead_placeholder_clicks_are_forgiven():
    # A click with no observable effect is EXPECTED on a static landing-page demo (href="#"),
    # so it must NOT crater the score — this was the bug that pinned 'function' near 0.
    report = InteractionReport(target="x", n_interactive=1, interactions=[
        Interaction(label="a", tag="button", selector="b", clicked=True, dead=True),
    ])
    assert _heuristic_score(report) >= 9.0


def test_heuristic_console_errors_score_low():
    # Genuine breakage (JS console errors on click) IS penalized hard once there's more than one.
    report = InteractionReport(target="x", n_interactive=2, interactions=[
        Interaction(label="a", tag="button", selector="b", clicked=True, new_console_errors=["TypeError"]),
        Interaction(label="b", tag="button", selector="c", clicked=True, new_console_errors=["ReferenceError"]),
    ])
    # load = 2 clicks-with-console-errors -> 2.0 ; 10 - 2^2 = 6.0
    assert _heuristic_score(report) == 6.0
