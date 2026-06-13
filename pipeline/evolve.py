"""Orchestrate generations of candidates.

A run is a sequence of generations. Each generation contains:
  1. A baseline (gen-0) or a critique-driven edit of the previous winner (gen-N>0).
  2. A capture pass (capture.py): screenshots into `gen-NNN/frames/`.
  3. A benchmark pass (benchmark.py): scores.json into `gen-NNN/`.
  4. A lineage append: one line into `<run>/lineage.jsonl`.

This module is a stub. The signature is the contract — fill the body in later.
"""

from __future__ import annotations

from pathlib import Path


def run_evolution(prompt: str, config: dict, run_dir: Path) -> None:
    """Drive a full evolutionary run from `prompt`, writing into `run_dir`.

    Args:
        prompt: the design brief / user prompt for this run.
        config: the parsed yaml dict from `autodesign.md`.
        run_dir: the run directory under `.autodesign/runs/<id>/`. Must exist.

    TODO: implement the loop.
        1. gen-0 fan-out: generate `config["loop"]["initial_candidates"]`
           siblings via the `generator` subagent in parallel. Write each at
           `gen-000/cand-NN/index.html`. Capture, score, then write
           `gen-000/winner.json` naming the highest-combined candidate.
        2. While generation < config["loop"]["iterations"] and best score <
           config["loop"]["target_score"]:
             a. Run the `critic` subagent on the previous winner to produce a
                critique + nameable decisions.
             b. Run the `generator` subagent in edit mode to answer the
                critique. Write `gen-NNN/cand-00/index.html`.
             c. Capture, score, write `gen-NNN/winner.json` (always cand-00),
                append to `lineage.jsonl`.
        3. On exit, copy the overall winner's html to `<run_dir>/final.html`.
           The dashboard reads the disk artifacts.
    """
    _ = (prompt, config, run_dir)
    raise NotImplementedError("evolve.run_evolution is a scaffold stub")
