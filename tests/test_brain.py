"""Offline tests for the brain good-vs-bad pipeline.

No network and no Playwright: we synthesize images directly, exercise the perceptual
encoder, the classifier's train/save/load/predict cycle, and the `brain_judge` signal's
graceful-skip paths and end-to-end scoring on a tiny separable dataset.
"""
from __future__ import annotations

import numpy as np
import pytest

from pipeline.brain import tribe_encoder
from pipeline.brain.classifier import BAD, GOOD, GoodBadBrainClassifier
from pipeline.context import CandidateContext
from pipeline.signals.brain_judge import BrainJudgeSignal

PIL = pytest.importorskip("PIL")
from PIL import Image  # noqa: E402


def _good_png(path, jitter=0):
    """A minimal, high-whitespace layout with one CTA — 'considered design'."""
    a = np.full((400, 640, 3), 248, np.uint8)
    a[140 + jitter : 180 + jitter, 260:380] = (30, 40, 90)  # single CTA
    a[30:60, 40:200] = (205, 208, 216)                      # restrained header
    Image.fromarray(a).save(path)


def _bad_png(path, seed=0):
    """Chaotic, saturated, cluttered noise — 'AI slop'."""
    rng = np.random.default_rng(seed)
    Image.fromarray(rng.integers(0, 255, (400, 640, 3)).astype(np.uint8)).save(path)


def test_encode_shape_and_determinism(tmp_path):
    p = tmp_path / "a.png"
    _good_png(p)
    v1 = tribe_encoder.encode_image(p)
    v2 = tribe_encoder.encode_image(p)
    assert v1.shape == (tribe_encoder.N_PARCELS,)
    assert np.allclose(v1, v2)  # deterministic per image


def test_good_and_bad_encode_differently(tmp_path):
    g, b = tmp_path / "g.png", tmp_path / "b.png"
    _good_png(g)
    _bad_png(b)
    assert not np.allclose(tribe_encoder.encode_image(g), tribe_encoder.encode_image(b))


def test_features_track_clutter(tmp_path):
    g, b = tmp_path / "g.png", tmp_path / "b.png"
    _good_png(g)
    _bad_png(b)
    fg = tribe_encoder.encode_features(g)
    fb = tribe_encoder.encode_features(b)
    assert fg["whitespace"] > fb["whitespace"]
    assert fb["colorfulness"] > fg["colorfulness"]


def _tiny_dataset(tmp_path, n=8):
    X, y = [], []
    for i in range(n):
        g, b = tmp_path / f"g{i}.png", tmp_path / f"b{i}.png"
        _good_png(g, jitter=i)
        _bad_png(b, seed=i)
        X.append(tribe_encoder.encode_image(g))
        y.append(GOOD)
        X.append(tribe_encoder.encode_image(b))
        y.append(BAD)
    return np.vstack(X), np.array(y)


def test_classifier_train_save_load_predict(tmp_path):
    X, y = _tiny_dataset(tmp_path)
    clf, report = GoodBadBrainClassifier.train(X, y)
    assert report.n_good == report.n_bad == 8
    assert report.train_accuracy == 1.0  # trivially separable synthetic data

    mp = tmp_path / "model.joblib"
    clf.save(mp)
    loaded = GoodBadBrainClassifier.load(mp)

    g, b = tmp_path / "tg.png", tmp_path / "tb.png"
    _good_png(g, jitter=3)
    _bad_png(b, seed=99)
    assert loaded.proba_good(tribe_encoder.encode_image(g)) > 0.5
    assert loaded.proba_good(tribe_encoder.encode_image(b)) < 0.5


def test_signal_skips_without_model(tmp_path):
    frame = tmp_path / "f.png"
    _good_png(frame)
    ctx = CandidateContext(
        candidate_dir=tmp_path, html_path=None, html_url=None, frames=[frame],
        code_text="", brief="", config={"brain_judge": {"model": str(tmp_path / "nope.joblib")}},
    )
    res = BrainJudgeSignal().score(ctx)
    assert res.score is None and "no trained model" in res.skipped


def test_signal_skips_when_disabled(tmp_path):
    ctx = CandidateContext(
        candidate_dir=tmp_path, html_path=None, html_url=None, frames=[],
        code_text="", brief="", config={"brain_judge": {"enabled": False}},
    )
    res = BrainJudgeSignal().score(ctx)
    assert res.score is None and res.skipped == "disabled in config"


def test_signal_scores_good_above_bad(tmp_path):
    X, y = _tiny_dataset(tmp_path)
    clf, _ = GoodBadBrainClassifier.train(X, y)
    mp = tmp_path / "model.joblib"
    clf.save(mp)

    g, b = tmp_path / "tg.png", tmp_path / "tb.png"
    _good_png(g, jitter=5)
    _bad_png(b, seed=123)
    sig = BrainJudgeSignal()

    def _score(frame):
        ctx = CandidateContext(
            candidate_dir=tmp_path, html_path=None, html_url=None, frames=[frame],
            code_text="", brief="", config={"brain_judge": {"model": str(mp)}},
        )
        return sig.score(ctx)

    rg, rb = _score(g), _score(b)
    assert rg.score is not None and rb.score is not None
    assert rg.score > rb.score
    assert 0.0 <= rb.score <= rg.score <= 10.0
