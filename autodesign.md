# AutoDesign Control Surface

This file is the single place you edit to change what the loop does. The prose at the
top is the design brief; the yaml block at the bottom is parsed by `pipeline/config.py`
and read by every other module.

## Brief

> A landing page for **Space Jam** — an indie space game. Hero shows the
> game's hook, a single primary CTA ("Play Free Demo") pulls the eye, supporting
> elements (one screenshot/illustration, one tagline) reinforce vibe without
> stealing attention. Tone: confident arcade-sci-fi, not corporate. The CTA
> must end up inside `saliency.focal_bbox` and any entrance animation must
> resolve attention onto it.

## How this file is consumed

- `pipeline/config.py` extracts the fenced ```yaml block below and returns it as a
  dict. Everything downstream (signals, capture, agents) reads from that dict.
- The `criteria:` map is the source of truth for which signals run and their weights.
  A signal whose `key` is not in `criteria` is silently skipped.
- `models:` assigns a model tier to each agentic role. The `.claude/agents/*.md`
  frontmatter should mirror these — change both when retiering.

```yaml
# ---- AutoDesign config ----
# Edit this block to control the loop. Everything below is parsed as YAML.

brief: |
  TODO: paste the design brief here, or leave blank to use the prose above.

loop:
  initial_candidates: 5    # number of siblings to generate in gen-0 (the wide first pass)
  iterations: 10           # max refinement generations after gen-0 (each iterates the previous winner)
  target_score: 9.0        # stop early when the best combined score meets/exceeds this
  diversity: 0.35          # minimum embedding distance required between gen-0 siblings (placeholder)

models:
  # Cost tiers. Cheaper models for fast inner-loop work, opus for the final judge.
  generate:  sonnet        # candidate UI generator
  judge:     opus          # VLM / final-pass judge
  cheap_pass: haiku        # quick passes, persona reactions, sanity checks
  evaluator: sonnet        # held-out pairwise evaluator (MUST differ from `judge`)

criteria:
  # Signal key -> weight. Weights are renormalized over signals that returned a
  # non-None score, so a skipped signal does not penalize the candidate.
  # Add a new entry here AND a matching @register_signal class to extend.
  # Start simple: just saliency + VLM judge. Add more rubrics later.
  saliency:  0.6
  vlm_judge: 0.4

saliency:
  # Which region of the page the saliency signal should optimize attention toward.
  focal_element: "primary_cta"    # CSS-selector-ish hint; signal-specific
  focal_bbox:    [0.10, 0.20, 0.55, 0.45]   # [x0, y0, x1, y1] as fractions of the viewport (top-left to bottom-right)

capture:
  viewport:           [1280, 800]
  animation_seconds:  2.0
  keyframes:          [0.0, 0.5, 1.0]   # fractional timestamps to screenshot
```
