"""Disk layout and on-disk schemas.

The loop is decoupled from the dashboard by a strict disk contract. The dashboard
reads files from `.autodesign/runs/<id>/` and nothing else. This module is the
single place that knows the layout, so any future restructure is local.

## Run layout

A generation holds 1+ candidates plus a `winner.json` naming the chosen one.
gen-0 fans out (default 5 siblings); gen-1+ typically holds a single
critique-driven refinement of the previous winner.

    .autodesign/
      runs/
        <utc-timestamp>/                    # one directory per run; `id` is its name
          lineage.jsonl                     # one JSON object per generation, in order
          gen-000/
            cand-00/
              index.html                    # the rendered candidate
              frames/0000.png ...           # captured screenshots
              saliency.png                  # optional saliency overlay
              scores.json                   # see schema below
            cand-01/ ...
            cand-04/ ...
            winner.json                     # { "winner": "cand-02", "combined": 7.4 }
          gen-001/
            cand-00/                        # a single refinement of gen-000's winner
              index.html, frames/, scores.json
            winner.json
          ...
          final.html                        # copy of the overall winner (written at run end)

## scores.json schema (per generation)

    {
      "candidate": "<path>",
      "per_criterion": { "<key>": <float|null>, ... },
      "combined": <float>,
      "scored_criteria": ["..."],
      "skipped_criteria": ["..."],
      "critique": "<string>",
      "nameable_decisions": ["..."],
      "raw": { "<key>": { "details": {}, "skipped": null }, ... }
    }

## lineage.jsonl line schema

    { "generation": 0, "combined": null, "changed": "", "answered_critique": "" }
"""

from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


RUNS_ROOT = Path(".autodesign") / "runs"
SCORES_FILENAME = "scores.json"
LINEAGE_FILENAME = "lineage.jsonl"
WINNER_FILENAME = "winner.json"
HTML_FILENAME = "index.html"
SALIENCY_FILENAME = "saliency.png"
FRAMES_DIRNAME = "frames"
VIDEO_FILENAME = "animation.webm"
FINAL_FILENAME = "final.html"


def new_run_id(now: datetime | None = None) -> str:
    """Generate a run id from a UTC timestamp. Format is sortable as a string."""
    now = now or datetime.now(timezone.utc)
    return now.strftime("%Y%m%dT%H%M%SZ")


def run_dir(run_id: str, root: Path = RUNS_ROOT) -> Path:
    """Path to a run directory. Does NOT create it."""
    return root / run_id


def gen_dir(run_id: str, generation: int, root: Path = RUNS_ROOT) -> Path:
    """Path to a generation directory. Format is `gen-NNN` (zero-padded to 3)."""
    return run_dir(run_id, root) / f"gen-{generation:03d}"


def cand_dir(
    run_id: str,
    generation: int,
    cand_index: int,
    root: Path = RUNS_ROOT,
) -> Path:
    """Path to a candidate directory. Format is `cand-NN` (zero-padded to 2)."""
    return gen_dir(run_id, generation, root) / f"cand-{cand_index:02d}"


def ensure_cand_dir(
    run_id: str,
    generation: int,
    cand_index: int,
    root: Path = RUNS_ROOT,
) -> Path:
    """Create (if needed) and return a candidate directory plus a `frames/` subdir."""
    d = cand_dir(run_id, generation, cand_index, root)
    (d / FRAMES_DIRNAME).mkdir(parents=True, exist_ok=True)
    return d


def scores_path(
    run_id: str,
    generation: int,
    cand_index: int,
    root: Path = RUNS_ROOT,
) -> Path:
    return cand_dir(run_id, generation, cand_index, root) / SCORES_FILENAME


def winner_path(run_id: str, generation: int, root: Path = RUNS_ROOT) -> Path:
    return gen_dir(run_id, generation, root) / WINNER_FILENAME


def lineage_path(run_id: str, root: Path = RUNS_ROOT) -> Path:
    return run_dir(run_id, root) / LINEAGE_FILENAME


def write_scores(path: Path, scores: dict[str, Any]) -> None:
    """Write a scores.json file. Caller is responsible for the schema."""
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(scores, indent=2, sort_keys=True), encoding="utf-8")


def append_lineage(path: Path, record: dict[str, Any]) -> None:
    """Append one JSON line to a lineage.jsonl file."""
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as f:
        f.write(json.dumps(record, sort_keys=True) + "\n")


def empty_scores_record(candidate_path: str | Path, criteria_keys: list[str]) -> dict:
    """Return a scores.json dict with every criterion marked skipped.

    Used by the scaffold so a dry-run produces a valid, well-formed manifest
    before any real signal is implemented.
    """
    return {
        "candidate": str(candidate_path),
        "per_criterion": {k: None for k in criteria_keys},
        "combined": 0.0,
        "scored_criteria": [],
        "skipped_criteria": list(criteria_keys),
        "critique": "",
        "nameable_decisions": [],
        "raw": {k: {"details": {}, "skipped": "not implemented"} for k in criteria_keys},
    }


def empty_lineage_record(generation: int) -> dict:
    """Return a lineage.jsonl record for a generation that has not been scored yet."""
    return {
        "generation": generation,
        "combined": None,
        "winner": None,
        "changed": "",
        "answered_critique": "",
    }


def write_winner(path: Path, winner: str, combined: float | None) -> None:
    """Write a winner.json file naming the chosen candidate within a generation.

    `winner` is the candidate directory name (e.g. "cand-02"). `combined` is its
    combined score from scores.json (or None if signals all skipped).
    """
    import json as _json
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        _json.dumps({"winner": winner, "combined": combined}, indent=2, sort_keys=True),
        encoding="utf-8",
    )
