"""Score one candidate by running every registered signal whose key is in
`config["criteria"]`, then combine the per-signal scores via weight
renormalization over signals that returned a non-None score.

The combiner logic is real (it is generic plumbing) — only the per-signal bodies
are stubbed. Once a signal becomes real, this module needs no changes.

Usage:

    python -m pipeline.benchmark --config autodesign.md --candidate path/to/candidate.html
    python -m pipeline.benchmark --candidate path/to/dir --dry-run
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Iterable

# Ensure all signal modules import and register before we read the registry.
import pipeline.signals  # noqa: F401
from pipeline.config import criteria_weights, load_config, load_dotenv
from pipeline.context import CandidateContext, SignalResult
from pipeline.registry import get_signals


def _resolve_paths(candidate: Path) -> tuple[Path, Path | None, str]:
    """Resolve the candidate dir, html path, and concatenated source text.

    Accepts either a directory containing an `index.html` (or any single .html
    file) or a path directly to an html file.
    """
    candidate = Path(candidate)
    if candidate.is_dir():
        candidate_dir = candidate
        html_path: Path | None = candidate / "index.html"
        if not html_path.exists():
            htmls = sorted(candidate.glob("*.html"))
            html_path = htmls[0] if htmls else None
    else:
        candidate_dir = candidate.parent
        html_path = candidate if candidate.suffix.lower() == ".html" else None

    code_text = ""
    if html_path and html_path.exists():
        code_text = html_path.read_text(encoding="utf-8", errors="replace")
    return candidate_dir, html_path, code_text


def _discover_frames(candidate: Path) -> list[Path]:
    """Find capture frames on disk under `<candidate_dir>/frames/`.

    The `--candidate` CLI scores *after* a separate capture step (the /autodesign loop
    runs `capture()` then `python -m pipeline.benchmark`), so the frames live on disk,
    not in memory. Without picking them up, the vlm_judge signal skips for lack of frames.
    """
    candidate = Path(candidate)
    cand_dir = candidate if candidate.is_dir() else candidate.parent
    frames_dir = cand_dir / "frames"
    return sorted(frames_dir.glob("*.png")) if frames_dir.is_dir() else []


def build_context(
    candidate: Path,
    brief: str,
    config: dict,
    frames: Iterable[Path] | None = None,
    html_url: str | None = None,
    references: Iterable[Path] | None = None,
    topic: str = "",
) -> CandidateContext:
    """Construct the uniform input every signal receives.

    `references`/`topic` carry the run's distinctiveness exemplars (see
    `references.acquire_references`); the engine acquires them once per run and
    passes the same set into every candidate's context. Default empty so callers
    that don't use distinctiveness (and existing tests) are unaffected.
    """
    candidate_dir, html_path, code_text = _resolve_paths(candidate)
    # Resolve the *real* brief: a placeholder/empty `brief` (the shipped "TODO: paste..."
    # default) falls back to the run's brief.txt, then the prose `## Brief`. And pull this
    # candidate's own generation directive from its `<!-- hypothesis: ... -->` comment.
    from pipeline.brief import extract_hypothesis, resolve_brief
    brief = resolve_brief(brief, candidate_dir)
    return CandidateContext(
        candidate_dir=candidate_dir,
        html_path=html_path,
        html_url=html_url,
        frames=list(frames or []),
        code_text=code_text,
        brief=brief,
        config=config,
        references=list(references or []),
        topic=topic,
        generation_prompt=extract_hypothesis(code_text),
    )


def combine(
    per_criterion: dict[str, float | None],
    weights: dict[str, float],
) -> tuple[float, list[str], list[str]]:
    """Combine per-signal scores via renormalized weights over non-None entries.

    Returns (combined_score, scored_keys, skipped_keys). When no signal scored,
    `combined_score` is 0.0 and `scored_keys` is empty.
    """
    scored = [k for k, v in per_criterion.items() if v is not None and k in weights]
    skipped = [k for k in per_criterion.keys() if k not in scored]
    if not scored:
        return 0.0, [], skipped
    total_weight = sum(weights[k] for k in scored)
    if total_weight <= 0:
        return 0.0, scored, skipped
    combined = sum(per_criterion[k] * weights[k] for k in scored) / total_weight  # type: ignore[operator]
    return combined, scored, skipped


def score_candidate(ctx: CandidateContext) -> dict:
    """Run every applicable signal, combine, and return a scores.json dict.

    "Applicable" = the signal's `key` appears in `ctx.config["criteria"]`. Signals
    not in the config never run. Signals that return `score=None` are recorded
    but excluded from the combiner.
    """
    weights = criteria_weights(ctx.config)
    signals = get_signals()

    per_criterion: dict[str, float | None] = {}
    raw: dict[str, dict] = {}
    for key in weights.keys():
        sig = signals.get(key)
        if sig is None:
            per_criterion[key] = None
            raw[key] = {"details": {}, "skipped": "no signal registered for this key"}
            continue
        result: SignalResult = sig.score(ctx)
        per_criterion[key] = result.score
        raw[key] = {"details": result.details, "skipped": result.skipped}

    combined, scored, skipped = combine(per_criterion, weights)
    return {
        "candidate": str(ctx.candidate_dir),
        "per_criterion": per_criterion,
        "combined": combined,
        "scored_criteria": scored,
        "skipped_criteria": skipped,
        "critique": "",
        "nameable_decisions": [],
        "raw": raw,
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Score one AutoDesign candidate.")
    parser.add_argument(
        "--config",
        default="autodesign.md",
        help="Path to autodesign.md (yaml block is extracted). Default: ./autodesign.md",
    )
    parser.add_argument(
        "--candidate",
        required=True,
        help="Path to a candidate html file or directory.",
    )
    parser.add_argument(
        "--out",
        default=None,
        help="Where to write scores.json. Default: <candidate_dir>/scores.json",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Build the context and print the result instead of writing to disk.",
    )
    parser.add_argument(
        "--references",
        action="store_true",
        help="Acquire similar-use-case competitors (web search) so the vlm_judge also "
             "scores originality. Cached under <run-dir>/references so it runs once per run.",
    )
    parser.add_argument(
        "--run-dir",
        default=None,
        help="Run root where references are cached/shared across candidates. "
             "Defaults to the candidate dir (per-candidate acquisition).",
    )
    parser.add_argument(
        "--env",
        default=".env",
        help="Path to a .env file with ANTHROPIC_API_KEY (default: ./.env). Loaded so "
             "the vlm_judge picks up the key without a manual export.",
    )
    args = parser.parse_args(argv)

    load_dotenv(args.env)
    config = load_config(args.config)
    brief = (config.get("brief") or "").strip()

    references: list = []
    topic = ""
    if args.references:
        from pipeline.references import acquire_references
        run_dir = Path(args.run_dir) if args.run_dir else Path(args.candidate)
        ref = acquire_references(brief, run_dir, config)
        references, topic = ref.screenshots, ref.topic

    ctx = build_context(Path(args.candidate), brief=brief, config=config,
                        frames=_discover_frames(args.candidate),
                        references=references, topic=topic)
    result = score_candidate(ctx)

    if args.dry_run:
        json.dump(result, sys.stdout, indent=2, sort_keys=True)
        sys.stdout.write("\n")
        return 0

    out_path = Path(args.out) if args.out else ctx.candidate_dir / "scores.json"
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(
        json.dumps(result, indent=2, sort_keys=True), encoding="utf-8"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
