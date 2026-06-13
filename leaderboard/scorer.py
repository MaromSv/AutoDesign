"""Wraps pipeline.rank.rank_urls for the leaderboard.

One URL at a time — the worker invokes this synchronously inside its own thread
so torch (DeepGaze) and Playwright don't fight each other. Returns the raw
scores.json dict plus the artifact directory it wrote into.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import yaml


def load_leaderboard_config(config_path: Path) -> dict[str, Any]:
    """Load the leaderboard's YAML config (not the autodesign.md control file)."""
    with config_path.open("r", encoding="utf-8") as f:
        return yaml.safe_load(f) or {}


def score_url(
    url: str,
    out_root: Path,
    config: dict[str, Any],
    brief: str = "",
) -> dict[str, Any]:
    """Capture + score a single URL. Returns the scores.json dict.

    `out_root` is the per-submission artifact directory (frames, video, scores.json
    end up under it). The pipeline's `rank_urls` slugs the URL into a subfolder of
    `out_root`; we surface that path as `artifacts_subdir` so the API can serve it.
    """
    # Import lazily so the FastAPI process can boot even if torch/playwright
    # take a second to import the first time the worker runs.
    from pipeline.rank import rank_urls

    out_root = Path(out_root)
    out_root.mkdir(parents=True, exist_ok=True)

    records = rank_urls(
        urls=[url],
        config=config,
        out_dir=out_root,
        brief=brief or "",
        labels=None,
        use_references=False,
    )
    if not records:
        raise RuntimeError("rank_urls returned no records")
    record = records[0]

    # Find the artifact subdir rank_urls wrote into (one level under out_root).
    subdirs = [p for p in out_root.iterdir() if p.is_dir()]
    record["artifacts_subdir"] = str(subdirs[0]) if subdirs else str(out_root)
    return record
