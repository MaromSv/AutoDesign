---
name: autodesign
description: Generate candidate UIs for a brief, then evolve them iteratively against the saliency benchmark
---

# AutoDesign — Loop Protocol & Artifact Schema

This skill documents the *protocol* the loop follows and the *artifacts* it
produces. It is the contract between the orchestration prompt
(`.claude/commands/autodesign.md`), the engine (`pipeline/`), and the dashboard
(`dashboard/`). All three read this doc; none of them duplicate it.

## Loop protocol

A run consists of:

1. **gen-0 fan-out.** The `generator` subagent runs `loop.initial_candidates`
   times (default 5) in parallel, producing meaningfully different candidates
   from the brief. Each is captured + scored independently. The highest
   `combined` wins; its name is written to `gen-000/winner.json`.
2. **Iterate.** For each generation `N >= 1` until `loop.iterations` or
   `loop.target_score`:
   - `critic` reads the previous generation's winner (`scores.json` + frames)
     and writes a critique + a list of `nameable_decisions`.
   - `generator` (in edit mode) produces a single refinement at
     `gen-NNN/cand-00/index.html`.
   - Capture → benchmark → write `gen-NNN/winner.json` (always `cand-00` when
     only one candidate exists) → append to `lineage.jsonl`.
3. **Stop.** Copy the overall winner's html to `<run>/final.html`. The
   dashboard manifest API exposes the full history.

## On-disk artifact schema

Layout (defined in `pipeline/artifacts.py`):

    .autodesign/runs/<utc-timestamp>/
      lineage.jsonl
      gen-NNN/
        cand-NN/
          index.html
          frames/0000.png ...
          saliency.png        (optional)
          scores.json
        winner.json           # { "winner": "cand-NN", "combined": <float|null> }
      final.html              # copy of the overall winner (written at run end)

`scores.json` per candidate:

    {
      "candidate": "<path>",
      "per_criterion": { "<key>": <float|null>, ... },
      "combined": <float>,
      "scored_criteria": [...],
      "skipped_criteria": [...],
      "critique": "<string>",
      "nameable_decisions": [...],
      "raw": { "<key>": { "details": {}, "skipped": null }, ... }
    }

`winner.json` per generation:

    { "winner": "cand-NN", "combined": <float|null> }

`lineage.jsonl` line per generation:

    { "generation": 0, "combined": null, "winner": "cand-00",
      "changed": "", "answered_critique": "" }

## Status

TODO: this skill is documentation-only today. The orchestration prompt in
`.claude/commands/autodesign.md` and the engine in `pipeline/` are still being
filled in.
