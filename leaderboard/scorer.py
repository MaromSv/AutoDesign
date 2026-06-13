"""Wraps pipeline.rank.rank_urls for the leaderboard.

One URL at a time — the worker invokes this synchronously inside its own thread
so torch (DeepGaze) and Playwright don't fight each other. Returns the raw
scores.json dict plus the artifact directory it wrote into.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import yaml


# Submitters don't give us a brief, so we hand the judge an explicit "no brief"
# statement instead. WHY non-empty: `pipeline.brief.resolve_brief` treats any
# blank / "TODO:" brief as a placeholder and falls back to the `## Brief` prose
# block in autodesign.md (which is the Space Jam game-page brief used by the
# research loop) — that leaked through and made the judge score every submitted
# URL "off-brief for an arcade-sci-fi game." A meaningful non-placeholder brief
# is returned verbatim by resolve_brief, so we can frame the judge's task here.
_NO_BRIEF_DIRECTIVE = (
    "This is a public design leaderboard. The submitter has not provided a design "
    "brief, target audience, or product category. Judge the page on universal UI "
    "quality only — creativity & distinctiveness, visual hierarchy, color & "
    "typography, motion, and overall design taste. Do NOT assume the page is for "
    "any specific product (game, SaaS, etc.) or compare it against a brief; the "
    "page is whatever its makers shipped, and that is what you are evaluating."
)


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

    # Always pass the no-brief directive so resolve_brief returns it verbatim
    # instead of falling back to autodesign.md's prose Space Jam brief.
    effective_brief = (brief or "").strip() or _NO_BRIEF_DIRECTIVE

    records = rank_urls(
        urls=[url],
        config=config,
        out_dir=out_root,
        brief=effective_brief,
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
