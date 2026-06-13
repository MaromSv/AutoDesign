"""Smoke tests for the scaffold.

These assert that the *shape* of the project is correct — imports work, the
registry self-populates, and the benchmark combiner can build a context and
emit a valid scores.json with every criterion null because every signal is a
stub. They do NOT assert anything about real scoring behavior.
"""

from __future__ import annotations

import json
from pathlib import Path


def test_package_imports():
    """Importing pipeline + pipeline.signals must not error."""
    import pipeline  # noqa: F401
    import pipeline.signals  # noqa: F401


def test_registry_populated_with_stub_signals():
    """Every signal module must self-register on import."""
    import pipeline.signals  # noqa: F401
    from pipeline.registry import get_signals

    signals = get_signals()
    assert signals, "registry is empty — signal modules failed to self-register"

    # The keys here must match `criteria` entries in autodesign.md.
    # Scaffold starts intentionally minimal — two real rubrics: saliency + VLM judge.
    # Add more here as new signal modules land.
    expected_keys = {"saliency", "vlm_judge"}
    assert expected_keys.issubset(signals.keys()), (
        f"missing registry keys: {expected_keys - set(signals.keys())}"
    )


def test_benchmark_produces_valid_scores_with_stub_signals(tmp_path: Path):
    """End-to-end through the combiner with stub signals.

    Every signal returns None (skipped), so the scores.json must:
      - list every criterion under skipped_criteria,
      - leave scored_criteria empty,
      - set combined to 0.0,
      - still be a valid manifest the dashboard can render.
    """
    import pipeline.signals  # noqa: F401
    from pipeline.benchmark import build_context, score_candidate

    candidate_dir = tmp_path / "candidate"
    candidate_dir.mkdir()
    (candidate_dir / "index.html").write_text(
        "<!doctype html><html><body>scaffold</body></html>", encoding="utf-8"
    )

    config = {
        "criteria": {
            "saliency": 0.6,
            "vlm_judge": 0.4,
        }
    }
    ctx = build_context(candidate_dir, brief="test brief", config=config)
    result = score_candidate(ctx)

    expected_top_level = {
        "candidate",
        "per_criterion",
        "combined",
        "scored_criteria",
        "skipped_criteria",
        "critique",
        "nameable_decisions",
        "raw",
    }
    assert expected_top_level.issubset(result.keys())

    for key in config["criteria"]:
        assert result["per_criterion"][key] is None, (
            f"{key} returned a real score — stubs must return None"
        )

    assert result["scored_criteria"] == []
    assert sorted(result["skipped_criteria"]) == sorted(config["criteria"].keys())
    assert result["combined"] == 0.0

    # Must be json-serializable — the dashboard reads it from disk.
    json.dumps(result)
