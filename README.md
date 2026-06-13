# autodesign

**benchmarking the slop out of AI.**

AI generates interfaces fast but almost all of it looks the same. Everyone can *feel*
which UI is better. Nobody can *measure* it. AutoDesign is a benchmark and an agent loop
that does. By Marom and Oliver.

> Leaderboard: **[TODO: leaderboard URL]**

![hero](docs/screenshots/hero.png)

---

## Why

Karpathy framed *autoresearch* — agents that run their own experiments against a
benchmark and climb it. The piece that was missing for **UIs** was the benchmark
itself. There is no widely-accepted, automatic, multi-signal scoring rubric for
generated web UI. Without one, every "AI website builder" converges to the same
purple-gradient slop, because the only feedback signal is human vibes.

We built the rubric, the scorer, and the agent loop that climbs it.

![problem](docs/screenshots/problem.png)

---

## The benchmark (the part nobody had built)

A composite score over **24 sub-metrics in 8 themed buckets**, weighted in
[`autodesign.md`](autodesign.md). Three of those buckets are signals we
implemented from scratch:

| Bucket | Source | What's novel |
|---|---|---|
| **Attention** (`intent_alignment`, `focus_clarity`, `reading_order`) | DeepGaze saliency map | scan-path geometry derived from a pretrained gaze model — not just "is the CTA visible" |
| **Motion** (`animation_focus`) | multi-frame saliency over a 5s capture | does the entrance *resolve* attention onto the CTA by the settled frame |
| **Distinctiveness** (`creativity`, `originality`, `ai_pitfalls`, `brain_judge`) | VLM rubric + research agent + **a model we trained** | quantifies distance from AI-slop |
| Hierarchy / color / type / usability | VLM-as-judge (Opus) | rubric-grounded principle scoring |
| **Brief fidelity** (`prompt_consistency`) | Nemotron text check | every brief element actually shipped? |
| **Function** (`stress_test`) | Nemotron + headless browser sub-agents | do buttons/links actually behave |

### The model we trained: `brain_judge`

A perceptual classifier (RBF SVM, **CV-AUC 0.85**) over clutter, colorfulness,
whitespace, contrast, symmetry, and hue-entropy, trained on awwwards winners
(masterpiece) vs. madewithlovable (slop). Plugs in as a single `Signal`.
[`pipeline/brain/`](pipeline/brain/).

![slop-vs-masterpiece](docs/screenshots/slop_vs_masterpiece.png)

---

## The agent loop

```
brief ──> generator (sonnet) ──> capture (headless chromium, 5 frames)
              ▲                              │
              │                              ▼
         critic (sonnet)  <──  signals  ──> scores.json
              ▲                              │
              └─── refinement plan ──────────┘
```

- **One file owns behavior** — [`autodesign.md`](autodesign.md). YAML at the bottom
  drives the brief, model tiers, signal weights, focal bbox, and capture.
- **Signals are pluggable.** New evaluation = new file in
  [`pipeline/signals/`](pipeline/signals/) + a line in `criteria:`.
- **Critic owns the plan, generator executes.** Critic reads `scores.json` +
  `saliency.png`, picks the 1–3 lowest sub-scores, emits surgical
  `nameable_decisions` (selector, property, target value). Stops system-level
  rewrites that tank everything at once.
- **Disk-as-contract.** Loop only writes to `.autodesign/runs/<id>/`. Dashboard
  only reads. They never share memory; runs are fully replayable.

![loop](docs/screenshots/loop.png)

---

## Cost / quality trade-offs

Every signal and agent gets the *cheapest model that still works* for that job.
This is wired in [`autodesign.md`](autodesign.md) → `models:` and per-signal
overrides, not hard-coded.

| Job | Model | Why |
|---|---|---|
| In-loop UI generation | **Sonnet** | needs taste, but called every iteration |
| Critic refinement plan | **Sonnet** | structured reasoning over scores.json |
| Final VLM judge (visual rubric) | **Opus** | most consequential signal, run once per candidate |
| Persona reactions | **Haiku** | cheap, called often |
| Brief-presence text check (`prompt_consistency`) | **Nemotron** (Nebius Token Factory) | pure text comparison — no need to pay frontier rates |
| Headless-browser stress test sub-agents | **Nemotron** | many short tool-call turns; cost adds up fast |
| Slop classifier (`brain_judge`) | **local sklearn** | no API call at all |

The Nemotron pair runs on Nebius Token Factory's OpenAI-compatible endpoint;
both signals **skip cleanly** if `NEBIUS_API_KEY` is unset, so the loop degrades
gracefully instead of failing.

### Parallel sub-agents

The `stress_test` signal spawns **N independent Nemotron personas** (first-time
visitor, link auditor, form tester) that each drive a separate headless browser
through `list / click / type / read / back` tool calls and report findings.
Scores are merged. Parallel where the work is independent; sequential where
it isn't.

---

## Agent collaboration

Each reasoning role is a separate Claude Code sub-agent in [`.claude/agents/`](.claude/agents/),
with its own system prompt and its own model tier in frontmatter:

- **`generator`** (sonnet) — builds the HTML
- **`critic`** (sonnet) — reads scores, plans the next iteration
- **`judge`** (opus) — final VLM rubric pass
- **`persona`** (haiku) — fast gut-reaction signal

Roles are *independently swappable*. Change the model for one role without
touching the others.

---

## Run it

```bash
pip install -r requirements.txt

# in Claude Code:
/autodesign
```

The loop reads [`autodesign.md`](autodesign.md), generates a candidate,
refines it across iterations, and stops at `loop.target_score` (default 9.0)
or `loop.iterations`.

Inspect runs:

```bash
python dashboard/serve.py    # read-only dashboard on .autodesign/runs/
```

![dashboard](docs/screenshots/dashboard.png)

---

## Repo map

- [`autodesign.md`](autodesign.md) — control surface (brief + yaml config)
- [`pipeline/`](pipeline/) — engine, signal registry, capture
- [`pipeline/signals/`](pipeline/signals/) — pluggable evaluations
- [`pipeline/brain/`](pipeline/brain/) — the trained slop classifier
- [`.claude/agents/`](.claude/agents/) — generator / critic / judge / persona
- [`.claude/skills/autodesign/`](.claude/skills/autodesign/) — loop protocol
- [`dashboard/`](dashboard/) — read-only run viewer
- [`leaderboard/`](leaderboard/) — public leaderboard site
