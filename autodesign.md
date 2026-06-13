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
  initial_candidates: 1    # candidates to generate in gen-0. 1 = build a single UI and then
                           # continuously refine that one page every generation (no gen-0 fan-out).
                           # Raise it to explore several distinct starting directions in parallel.
  iterations: 10           # max refinement generations after gen-0 (each iterates the previous winner)
  target_score: 9.0        # stop early when the best combined score meets/exceeds this
  diversity: 0.35          # min embedding distance between gen-0 siblings (only relevant when initial_candidates > 1)

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
  # vlm_judge is the creativity engine — its creativity/originality principles drive the
  # bulk of the score (how far from AI-slop, how distinct from real competitors). Saliency
  # stays as a floor (attention still has to land on the right element) but does not get to
  # dominate a creatively bold page.
  saliency:  0.2
  vlm_judge: 0.8
  # The Nemotron + classifier signals are deliberately scoped to NOT overlap with vlm_judge/
  # saliency: prompt_consistency judges content/feature PRESENCE (not looks); stress_test
  # judges interaction BEHAVIOR; brain_judge scores perceptual design-DNA. Enable the
  # Nemotron pair when NEBIUS_API_KEY is set.
  prompt_consistency: 0.4   # Nemotron: is every requested element/feature actually in the build? (text, not visual)
  stress_test:        0.3   # Nemotron + headless browser: do buttons/links work & behave consistently? (behavior only)
  brain_judge:        0.2   # perceptual-fallback classifier (RBF SVM, CV-AUC 0.85) vs awwwards/Lovable; NOT real brain data yet

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
  n_references: 5      # how many similar sites to look for AND keep (keep all that render; 0 found is fine)
  max_workers:  5      # peers are captured in parallel; cap simultaneous headless browsers
  # search_model: claude-opus-4-8   # pin the research model (defaults to models.judge)

nemotron:
  # Shared config for the Nemotron-backed signals (stress_test, prompt_consistency).
  # Nebius Token Factory exposes an OpenAI-compatible API; we call it via the openai SDK.
  base_url:    https://api.tokenfactory.us-central1.nebius.com/v1/
  model:       nvidia/Nemotron-3-Ultra-550b-a55b   # verified id on this Token Factory account
  api_key_env: NEBIUS_API_KEY    # env var holding your Nebius Token Factory key
  vision:      false             # set true (and use a VL Nemotron) to send a screenshot to prompt_consistency

stress_test:
  # Agentic stress test: several Nemotron SUBAGENTS (personas below) each autonomously drive
  # a headless browser via tool calls (list/click/type/read/back) to pursue a goal, then
  # report findings; their scores are merged. Needs playwright + NEBIUS_API_KEY. Without the
  # key it falls back to a deterministic probe (clicks every element) for a behavioral score.
  max_steps:        12     # max tool-call steps per subagent before it must finish
  max_interactions: 25     # cap elements listed per page (keeps the listing/cost bounded)
  settle_ms:        600    # wait after each click for navigation/DOM to settle
  agent_max_tokens: 1024   # output budget per agent step
  # model: nvidia/Llama-3_1-Nemotron-Ultra-253B-v1   # override the nemotron.model for this signal
  # personas:              # override the default 3 (first-time visitor / link auditor / form tester)
  #   - {name: "checkout shopper", goal: "Add an item to the cart and reach checkout. Report anything that blocks the purchase."}
  #   - {name: "skeptic",          goal: "Click every link and button; report any dead control, JS error, or inconsistent behavior."}

prompt_consistency:
  # Text-based check of the generated site against the brief (the prompt). Distinct from
  # vlm_judge's visual `brief_adherence` principle. Needs NEBIUS_API_KEY; skips without it.
  # vision: true     # attach a rendered frame (requires a vision-capable Nemotron tier)
  # model: nvidia/Llama-3_3-Nemotron-Super-49B-v1   # override the nemotron.model for this signal

capture:
  viewport:           [1280, 800]
  animation_seconds:  5.0
  # 5 frames evenly spaced across the 5s window: 0s, 1.25s, 2.5s, 3.75s, 5s. Fewer frames
  # than the old 10 = faster + cheaper VLM judge calls. Endpoints always included by capture.py.
  keyframes:          [0.0, 0.25, 0.5, 0.75, 1.0]
```
