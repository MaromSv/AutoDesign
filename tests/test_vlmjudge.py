"""Offline tests for the vlm_judge signal.

No network: we exercise registration, graceful skips, rubric/config handling, prompt
assembly, response parsing, and the weighted 0-10 combine. The actual model call is the
only thing not covered here (it needs an API key); everything around it is.
"""
from __future__ import annotations

from pathlib import Path

from pipeline.context import CandidateContext
from pipeline.registry import get_signals
from pipeline.signals._ui_rubric import load_rubric, DEFAULT_PRINCIPLES
from pipeline.signals.vlmjudge import (
    _resolve_model,
    _build_prompt,
    _parse_response,
    _combine,
    _select_frames,
    _originality_principle,
)
import pipeline.signals  # noqa: F401  (ensure registration)


def _ctx(frames, config) -> CandidateContext:
    return CandidateContext(
        candidate_dir=Path("/tmp"), html_path=None, html_url=None,
        frames=frames, code_text="", brief="A calm meditation app landing page", config=config,
    )


def test_signal_registered():
    assert "vlm_judge" in get_signals()


def test_skips_when_no_frames():
    res = get_signals()["vlm_judge"].score(_ctx([], {"models": {"judge": "opus"}}))
    assert res.score is None
    assert "no frames" in res.skipped


def test_skips_when_frames_missing_on_disk(tmp_path):
    res = get_signals()["vlm_judge"].score(_ctx([tmp_path / "nope.png"], {}))
    assert res.score is None and res.skipped


# --------------------------------------------------------------- model resolution
def test_tier_resolves_to_claude_vision_id():
    assert _resolve_model({"models": {"judge": "opus"}}) == "claude-opus-4-8"
    assert _resolve_model({"models": {"judge": "sonnet"}}) == "claude-sonnet-4-6"


def test_explicit_model_and_tier_map_override():
    assert _resolve_model({"vlm_judge": {"model": "gpt-4o"}}) == "gpt-4o"
    cfg = {"models": {"judge": "opus"}, "vlm_judge": {"tier_map": {"opus": "claude-opus-4-7"}}}
    assert _resolve_model(cfg) == "claude-opus-4-7"


# --------------------------------------------------------------- rubric / config
def test_default_rubric_has_motion_and_brief_principles():
    keys = {p.key for p in DEFAULT_PRINCIPLES}
    assert {"motion", "brief_adherence", "visual_hierarchy"}.issubset(keys)
    assert any(p.needs_motion for p in DEFAULT_PRINCIPLES)


def test_config_can_zero_out_and_reweight_principles():
    rub = load_rubric({"vlm_judge": {"weights": {"motion": 0.0, "visual_hierarchy": 2.0}}})
    keys = [p.key for p in rub]
    assert "motion" not in keys                       # weight 0 drops it
    assert next(p for p in rub if p.key == "visual_hierarchy").weight == 2.0


def test_config_custom_principles():
    rub = load_rubric({"vlm_judge": {"principles": [
        {"key": "k", "name": "Kustom", "weight": 1.0, "evaluation_steps": ["check x"]},
    ]}})
    assert [p.key for p in rub] == ["k"]


# --------------------------------------------------------------- prompt / parse / combine
def test_prompt_contains_brief_rubric_and_json_contract():
    rub = load_rubric({})
    prompt = _build_prompt("A calm meditation app", rub, n_frames=3)
    assert "DESIGN BRIEF" in prompt and "A calm meditation app" in prompt
    assert '"scores"' in prompt and "Visual Hierarchy" in prompt
    assert "[needs the frame sequence]" in prompt  # motion principle flagged


def test_parse_handles_fenced_and_bare_json():
    fenced = '```json\n{"scores": {"typography": {"score": 7}}, "critique": "ok"}\n```'
    bare = 'Here you go: {"scores": {"typography": 7}} thanks'
    assert _parse_response(fenced)["scores"]["typography"]["score"] == 7
    assert _parse_response(bare)["scores"]["typography"] == 7
    assert _parse_response("not json at all") is None


def test_weighted_combine_is_0_to_10_and_clamps():
    rub = load_rubric({"vlm_judge": {"weights": {k.key: 0.0 for k in DEFAULT_PRINCIPLES
                                                 if k.key not in ("visual_hierarchy", "typography")}}})
    score, per = _combine(rub, {"visual_hierarchy": {"score": 12}, "typography": {"score": 4}})
    # visual_hierarchy clamps 12 -> 10; weights 1.2 and 1.0 -> (10*1.2 + 4*1.0)/2.2
    assert per["visual_hierarchy"]["score"] == 10.0
    assert 0.0 <= score <= 10.0
    assert abs(score - (10 * 1.2 + 4 * 1.0) / 2.2) < 0.01  # _combine rounds to 2 dp


# --------------------------------------------------------------- originality (references)
def test_prompt_without_references_has_no_reference_section():
    prompt = _build_prompt("A brief", load_rubric({}), n_frames=2)
    assert "REFERENCE" not in prompt


def test_prompt_with_references_explains_them_and_names_topic():
    rub = load_rubric({}) + [_originality_principle("real-estate marketplace")]
    prompt = _build_prompt("A brief", rub, n_frames=2, n_refs=3, topic="real-estate marketplace")
    assert "3 REFERENCE image(s)" in prompt
    assert "real-estate marketplace" in prompt
    assert "Originality" in prompt and "(key: originality)" in prompt


def test_originality_principle_is_topic_aware_and_weighted():
    p = _originality_principle("AI chatbot product")
    assert p.key == "originality" and p.weight >= 2.0
    assert any("AI chatbot product" in step for step in p.evaluation_steps)
    generic = _originality_principle("")
    assert any("similar products / competitors" in step for step in generic.evaluation_steps)


def test_select_frames_caps_and_keeps_endpoints(tmp_path):
    paths = []
    for i in range(20):
        p = tmp_path / f"{i:04d}.png"
        p.write_bytes(b"x")
        paths.append(p)
    sel = _select_frames(paths, 8)
    assert len(sel) <= 8
    assert sel[0] == paths[0] and sel[-1] == paths[-1]
