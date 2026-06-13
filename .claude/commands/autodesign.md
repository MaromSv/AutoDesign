---
description: Build candidate UI(s) for a brief (loop.initial_candidates; default 1 = one UI, continuously refined), then iteratively improve under .autodesign/runs/<id>/
argument-hint: <brief — what UI to build>
---

You are running the **AutoDesign** loop on this brief:

> $ARGUMENTS

If `$ARGUMENTS` is empty, ask the user for a brief in one sentence, then proceed.

The architecture is in [CLAUDE.md](../../CLAUDE.md). The control surface is
[autodesign.md](../../autodesign.md) — read its yaml block before doing anything.

## The shape of the loop

```
brief --> [gen-0: build loop.initial_candidates UI(s)] --> score --> pick winner
                                                                 |
                                                                 v
                            [gen-N>=1: refine the winner] <-- critique
                                       |
                                       v
                            score --> new winner --> loop until threshold
                                       |
                                       v
                                 final.html + design_journey.html
```

Gen-0 builds `loop.initial_candidates` candidate(s). The default is **1**: build a
single UI and then continuously refine that one page every generation (no gen-0
fan-out — the lone candidate is trivially the winner). Set `initial_candidates > 1`
to fan out into several distinct starting directions and pick the best. Every
generation after gen-0 is a single critique-driven refinement of the previous
winner. The dashboard reads `.autodesign/runs/<id>/` to render the run.

## Setup

> **CRITICAL — always use the project venv.** The scoring signals live ONLY in
> `.venv` (`anthropic` for `vlm_judge`, `deepgaze_pytorch` for `saliency`). Bare
> `python` resolves to a different interpreter (e.g. miniconda) that lacks them,
> so every signal silently skips and **every score comes back `null`** — the
> dashboard then shows no scores. Run every command below as `.venv/bin/python …`
> (shell state does not persist between calls, so `source .venv/bin/activate`
> will NOT carry over — you must prefix each command explicitly).

1. **Load config + create the run dir.** Run from the repo root:

   ```bash
   .venv/bin/python -c "
   import json
   from pipeline.config import load_config
   from pipeline.artifacts import new_run_id, run_dir
   cfg = load_config()
   rid = new_run_id()
   d = run_dir(rid); d.mkdir(parents=True, exist_ok=True)
   print(json.dumps({'run_id': rid, 'run_dir': str(d), 'config': cfg}, indent=2))
   "
   ```

   From the printed JSON extract: `run_id`, `run_dir`, and from `config`:
   - `loop.initial_candidates` — how many candidates for gen-0 (default 1).
   - `loop.iterations` — how many refinement generations after gen-0.
   - `loop.target_score` — stop early when best `combined` ≥ this.
   - `criteria` — signal keys + weights for `pipeline/benchmark.py`.
   - `capture.viewport` — viewport the html should target.
   - `models.*` — which subagent uses which model tier.

2. **Persist the brief** to `<run_dir>/brief.txt`.

## Generation 0 — seed (or fan out)

Generate `loop.initial_candidates` (default **1**) candidate(s). With the default of 1,
build a single strong candidate (`cand-00`) — it becomes the seed that every later
generation refines. When `initial_candidates > 1`, make the variants meaningfully
different on at least three axes:

- Layout (asymmetric left-hero / symmetric bold / editorial columns / illustration-led / minimal grid)
- Primary focal element (typography hero / product preview / illustration / data)
- Color & tone (light editorial / dark bold / neutral conversion / accent-color minimal)
- Information density (sparse hero / dense product / mixed)

For each `i` in `0..loop.initial_candidates - 1`:

**a. Create the candidate dir.**

```bash
.venv/bin/python -c "
from pipeline.artifacts import ensure_cand_dir
print(ensure_cand_dir('<run_id>', 0, <i>))
"
```

**b. Generate the html.** Invoke the **`generator`** subagent (Agent tool with
`subagent_type: generator`). Pass it the brief, the viewport from
`config.capture.viewport`, and a one-line `hypothesis` describing the design
axis this sibling explores (e.g. "asymmetric left-hero, dark, sparse"). It
MUST write a single self-contained html file at:

```
<run_dir>/gen-000/cand-NN/index.html
```

Inline all CSS. No external assets beyond Google Fonts. Viewport-ready for
the configured size (default 1280×800). The generator MAY embed the
hypothesis as an html comment at the top of the file.

When generating more than one candidate, spawn the generator calls in **parallel**
(one message, multiple Agent tool calls) — they are independent. With the default of
1, this is just a single generator call.

**c. Capture frames** for each candidate:

```bash
.venv/bin/python -c "
from pathlib import Path
from pipeline.capture import capture
from pipeline.config import load_config
cfg = load_config()
out = Path('<run_dir>/gen-000/cand-NN')
capture(
    html_path=out / 'index.html',
    out_dir=out,
    viewport=tuple(cfg['capture']['viewport']),
    animation_seconds=float(cfg['capture']['animation_seconds']),
    keyframes=cfg['capture']['keyframes'],
)
"
```

`pipeline/capture.py` is implemented (Playwright): it writes the keyframe PNGs to
`<cand>/frames/` using `config.capture` (viewport, `animation_seconds`, `keyframes`).
It skips gracefully if Playwright/chromium isn't installed (`pip install playwright &&
playwright install chromium`).

**d. Score** each candidate:

```bash
.venv/bin/python -m pipeline.benchmark --candidate <run_dir>/gen-000/cand-NN \
    --references --run-dir <run_dir>
```

This writes `<run_dir>/gen-000/cand-NN/scores.json`. The benchmark CLI auto-discovers
the frames written in step (c) under `<cand>/frames/`, so `vlm_judge` scores for real
(needs `anthropic` + `ANTHROPIC_API_KEY`). It carries two extra rubric principles:
`ai_pitfalls` (slop-detector fingerprint/corpus evidence on the candidate's HTML —
always on) and, with `--references`, `originality` (the research agent finds
similar-use-case competitors once per run, cached under `<run_dir>/references/`, and the
judge scores how much the candidate stands out). `saliency` runs from the captured
frame sequence. Combined = weighted mean of `saliency` (0.4) + `vlm_judge` (0.6).

Drop `--references` to skip the per-run web search (originality won't be scored; the rest
is unchanged).

**e. Pick the winner.** With real signals, the winner is `argmax(combined)`.
Today, with stubs, every score is 0.0 — break the tie with judgment: open each
html, prefer the one that best matches the brief's hypothesis, and call out
that the score itself was uninformative.

Write `<run_dir>/gen-000/winner.json`:

```bash
.venv/bin/python -c "
from pipeline.artifacts import winner_path, write_winner
write_winner(winner_path('<run_id>', 0), 'cand-NN', <combined_or_None>)
"
```

**f. Append lineage.**

```bash
.venv/bin/python -c "
from pipeline.artifacts import lineage_path, append_lineage
append_lineage(lineage_path('<run_id>'), {
    'generation': 0,
    'combined': <combined_or_None>,
    'winner': 'cand-NN',
    'changed': 'gen-0 seed: <the candidate's hypothesis> (or, if fanned out, one line per sibling)',
    'answered_critique': '',
})
"
```

**g. Report to the user.** One line: `gen-000: <N> candidate(s) | winner=cand-NN | combined=<n> | <continuing|stopping>` (N = loop.initial_candidates).

## Generations 1..N — refine the winner

For each generation `g` in `1..loop.iterations` (stop early per the rules below):

**1. Critique the previous winner.** Invoke the **`critic`** subagent. Give it:

- The brief.
- The previous generation's winner: `<run_dir>/gen-(g-1)/<winner>/scores.json`,
  `index.html`, and the at-rest frame if present.

It MUST return JSON:

```json
{ "critique": "<one or two sentences>", "nameable_decisions": ["<imperative>", "..."] }
```

**1b. Extract the previous winner's concrete feedback.** The most actionable
signal is the VLM judge's pinpointed `issues` (located `where/problem/fix`),
which live nested in `scores.json`. Pull them out deterministically so they
cannot be dropped:

```bash
.venv/bin/python -m pipeline.feedback --candidate <run_dir>/gen-(g-1)/<winner>
```

This prints a markdown brief: the judge's worst-first issues, the critic's
nameable decisions, the weakest rubric principles, and a one-line verdict. Keep
this text — it is the spec for this generation.

**2. Generate the refinement.** Create the candidate dir, then invoke the
**`generator`** subagent in edit mode. Pass it the previous `index.html`, the
critic's `critique` + `nameable_decisions` from step 1, AND the full feedback
brief from step 1b. Tell it explicitly: address EVERY issue in the brief, make a
visible substantive change for each, and do NOT return a near-identical page. It
writes a single new html at:

```
<run_dir>/gen-GGG/cand-00/index.html
```

```bash
.venv/bin/python -c "
from pipeline.artifacts import ensure_cand_dir
print(ensure_cand_dir('<run_id>', <g>, 0))
"
```

**3. Capture + score** (same commands as gen-0 steps c and d, with `<g>` and
`cand-00`).

**4. Merge the critique** into `scores.json` (overwrite the empty
`critique` and `nameable_decisions` fields with what the critic returned in
step 1).

**5. Write the winner.json** (always `cand-00` for a single-candidate gen):

```bash
.venv/bin/python -c "
from pipeline.artifacts import winner_path, write_winner
write_winner(winner_path('<run_id>', <g>), 'cand-00', <combined_or_None>)
"
```

**6. Append lineage** (set `winner='cand-00'`, `answered_critique=<the critique
text>`, `changed=<one short note on what this gen changed>`).

**7. Report** one line: `gen-GGG: combined=<n> | critique: <first 80 chars> | <continuing|stopping>`.

## Stop conditions

Stop and jump to "Final output" when any of:

- Current best `combined` ≥ `loop.target_score`.
- Generations done = `loop.iterations + 1` (gen-0 plus N refinements).
- The latest `combined` did not improve by ≥ 0.5 over the previous winner
  (convergence — only meaningful once real signals exist; ignore today).

## Goodhart safety rails

Use judgment, not just the score.

- Signals are stubs today — every `combined` is 0.0. Do NOT pretend that is
  meaningful. Pick winners by reading the html.
- Once real signals land: if a candidate's score jumps by > 2.0 between
  generations, read the html and explain what changed before declaring
  victory. Big spikes are usually Goodhart, not real improvement.
- Never declare a final winner you would not ship.

## Final output

When the loop ends:

1. Copy the html of the overall winner (the winner of the final generation,
   or the highest `combined` across all generations once real scores exist)
   to `<run_dir>/final.html`.
2. Print a short summary:
   - The brief, verbatim.
   - The number of generations run and the final `combined`.
   - Markdown link to `final.html`.
   - The dashboard URL: `http://127.0.0.1:8765/api/run/<run_id>` (remind the
     user they can start the dashboard with `.venv/bin/python dashboard/serve.py`).
   - One sentence on whether the loop hit `target_score`, converged, or was
     capped at `iterations`.

## Notes

- This command is the only code path that drives the loop end-to-end today.
  `pipeline/evolve.py` exists as a stub for the same flow in pure Python;
  when that lands, this command becomes a thin wrapper around it.
- Adding a signal is one file in `pipeline/signals/` + one line in
  `autodesign.md`. Swapping the UI touches only `dashboard/`. Retiering models
  is `autodesign.md` + `.claude/agents/*.md` frontmatter.
