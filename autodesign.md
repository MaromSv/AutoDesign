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
  # vlm_judge carries the heavy lift now — it folds in the ai_pitfalls (AI-slop) and
  # originality (vs. competitors) principles on top of the 9 UX principles — so it is
  # weighted above the narrower saliency (attention-to-focal-region) signal.
  saliency:  0.4
  vlm_judge: 0.6
  # brain_judge: 0.2   # re-add once the brain-scan classifier is working (WIP)

saliency:
  # Which region of the page the saliency signal should optimize attention toward.
  focal_element: "primary_cta"    # CSS-selector-ish hint; signal-specific
  focal_bbox:    [0.10, 0.20, 0.55, 0.45]   # [x0, y0, x1, y1] as fractions of the viewport (top-left to bottom-right)

vlm_judge:
  # Optional. The signal works with no config — it uses the default UX rubric and the
  # `models.judge` tier above (opus -> claude-opus-4-8), feeding it the captured frame
  # sequence so animation is judged too. Override any of:
  # model:    claude-opus-4-8        # pin an exact model id (skips tier_map)
  # tier_map: {opus: claude-opus-4-8, sonnet: claude-sonnet-4-6}   # remap tiers
  # weights:  {motion: 0.0, visual_hierarchy: 1.5}   # reweight / zero-out rubric principles
  # principles:                      # OR replace the rubric entirely
  #   - {key: brand_fit, name: "Brand Fit", weight: 1.5,
  #      evaluation_steps: ["Does the look match the brief's brand and tone?"]}

ai_pitfalls:
  # Brings slop-detector's matching into the vlm_judge rubric as the `ai_pitfalls`
  # principle. We run slop-detector (builder/stack fingerprints + optional corpus
  # similarity) on the candidate's code, inject the findings as evidence, and the VLM
  # decides the score — a confirmed AI-builder fingerprint is strong evidence of slop.
  # Weighted heavily (2.5 vs ~1.0 for general principles) so a polished-but-generic
  # site is actually pulled down. Needs slop_detector installed; skips silently if not.
  enabled: true
  weight:  2.5
  render:  false   # for URL candidates with no saved DOM, fetch served HTML (false) or render (true)
  # corpus: ../slop-detector/data/slop_corpus.json   # optional — enables copy/structural similarity too

originality:
  # Originality vs. the competitive landscape. A research agent (web search) reads the
  # brief / the site under review, names the product use case, and finds real, live
  # similar products & competitors in that space. Those screenshots are fed into the
  # `vlm_judge` call so it scores how much the candidate STANDS OUT (the `originality`
  # principle). Folded into vlm_judge — no separate weight; the principle carries it.
  # Fully optional: with no ANTHROPIC_API_KEY / Playwright it no-ops. In `rank`, enable
  # with --references (the agent infers each site's use case from the URL).
  enabled:      true
  n_references: 5      # peer screenshots to keep (caps the expensive render step)
  n_candidates: 10     # URLs the agent returns; we over-fetch then keep what renders
  # search_model: claude-opus-4-8   # pin the research model (defaults to models.judge)

capture:
  viewport:           [1280, 800]
  animation_seconds:  10.0
  # 10 frames evenly spaced across the 10s window (fractional timestamps, endpoints included)
  keyframes:          [0.0, 0.11, 0.22, 0.33, 0.44, 0.56, 0.67, 0.78, 0.89, 1.0]
```
