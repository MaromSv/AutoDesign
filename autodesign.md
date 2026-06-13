# AutoDesign Control Surface

This file is the single place you edit to change what the loop does. The prose at the
top is the design brief; the yaml block at the bottom is parsed by `pipeline/config.py`
and read by every other module.

## Brief

> A website for **Mintism** — a new religion whose central tenet is:
> one mint, every hour, brings you closer to heaven. The site exists to
> convert visitors. There must be a primary call-to-action inside
> `saliency.focal_bbox`.
>
> (Deliberately bare. ALL style/quality requirements — palette, sigil,
> typography, motion, tone — live in the `vlm_judge.principles` rubric
> below, so gen-0 pages start naive and climb up the rubric over
> iterations rather than starting near-perfect.)

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
  # Cost tiers. Sonnet for the judge — fast enough to iterate, vision-capable.
  # Opus is reserved for final-pass review if needed.
  generate:  sonnet        # candidate UI generator
  judge:     sonnet        # VLM judge (was opus — sonnet is ~3x faster on multi-image vision calls)
  cheap_pass: haiku        # quick passes, persona reactions, sanity checks
  evaluator: sonnet        # held-out pairwise evaluator (MUST differ from `judge`)

criteria:
  # Signal key -> weight. Weights are renormalized over signals that returned a
  # non-None score, so a skipped signal does not penalize the candidate.
  # Add a new entry here AND a matching @register_signal class to extend.
  # vlm_judge is the creativity engine — the `creativity` (3.5) + `originality`
  # (3.0) principles alone account for the lion's share of its weight, so the
  # combined score is driven by how far the candidate gets from AI-slop and how
  # distinct it is from real competitors. Saliency stays in the mix as a
  # floor — the design still has to land attention on the right element — but
  # it does not get to dominate a creatively bold page.
  saliency:  0.2
  vlm_judge: 0.8
  # brain_judge: 0.2   # re-add once the brain-scan classifier is working (WIP)

saliency:
  # Which region of the page the saliency signal should optimize attention toward.
  # For a desktop conversion landing page, the primary CTA lives in the hero —
  # roughly horizontally centered, sitting between the headline and the fold.
  focal_element: "primary_cta"    # CSS-selector-ish hint; signal-specific
  focal_bbox:    [0.30, 0.45, 0.70, 0.70]   # [x0, y0, x1, y1] as fractions of the viewport

vlm_judge:
  # Custom rubric for the Mintism brief. The DESIGN BRIEF (above) is deliberately
  # bare — all the style guidance lives HERE as evaluation criteria. The judge scores
  # naturally against these principles; a naive page will score low because it doesn't
  # satisfy them, not because we hardcoded a floor. The CRITIC reads these `reason`
  # fields each generation and turns them into nameable_decisions for the generator,
  # which is how the design climbs the rubric over iterations.
  principles:
    - key: text_legibility
      name: "Text Legibility (no overlap, fully readable)"
      weight: 3.0
      evaluation_steps:
        - "Inspect every frame, especially the settled one. Is ALL text fully visible — no element covering any line of type, no clipping at the viewport edge, no two text blocks overlapping each other?"
        - "Check contrast against background: can every word be read without effort (approximate WCAG AA — body >= 4.5:1, headlines >= 3:1)? Faint text over textured / colored backgrounds counts against legibility."
        - "Mid-animation overlap is OK if the choreography resolves it; settled-state overlap is not."
        - "Reward generous line-height, comfortable line lengths (~45-90ch for body), and whitespace that guarantees every word can be read."
    - key: mintism_iconography
      name: "Mintism Iconography"
      weight: 2.5
      evaluation_steps:
        - "Is there a singular emblem / sigil / icon that communicates the Mintism concept at a glance — a mint leaf, an hourly clock, a wax seal, a sacred geometry mark — rendered with real care (custom SVG, considered proportions)?"
        - "Is the icon the visual centerpiece of the hero, sized for ceremony, integrated with the layout rather than dropped in as a small logo?"
        - "Reward well-drawn SVG paths, custom illustration, an icon that works as both wordmark and ceremonial object."
    - key: not_ai_slop
      name: "Distinctiveness vs Generic AI Output"
      weight: 2.2
      evaluation_steps:
        - "Imagine 20 landing pages someone would ship in an afternoon with v0 / Lovable / Bolt / Framer / GPT for 'make a website for my religion'. How far is this candidate from that baseline?"
        - "Common AI-slop fingerprints to watch for: hero → 3 feature cards → testimonial → CTA layout, default Inter + indigo/purple gradient, generic 'modern minimal' aesthetic, 'Trusted by' logo bar, stock icons, vapid taglines, glassmorphism, neon glow."
        - "Reward signature moves: a distinct visual concept that ties everything together, unconventional layout shapes, kinetic display typography, a named-and-memorable color, a custom illustration system, a motion idea that tells a story in 2 seconds."
        - "Calibration: 10 = a viewer screenshots it for inspiration; 7 = clearly considered, has personality; 5 = competent but forgettable; 2 = generic AI output."
    - key: ceremonious_typography
      name: "Ceremonious Typography"
      weight: 2.0
      evaluation_steps:
        - "Does the headline type feel ceremonial / reverent — serif or display face (Cormorant, Cinzel, EB Garamond, IM Fell, custom display)? A generic system sans (Inter, system-ui, Arial) does not communicate religious-movement gravitas."
        - "Is the type scale generous and considered — hero headline large (~72px+), intentional small-caps or italic accents, line-heights tuned for whitespace not density?"
        - "Reward type that reads as 'movement' or 'liturgy', not as 'startup' or 'SaaS landing'."
    - key: slow_purposeful_motion
      name: "Slow, Purposeful Motion"
      weight: 1.8
      needs_motion: true
      evaluation_steps:
        - "Compare frames in order. Is there a slow entrance choreography that builds the page like a ritual — emblem revealing first, type fading in piece by piece, the CTA materializing last with a finale beat?"
        - "Does the entrance terminate visually on the CTA (a scale, glow, or color flash that anchors the eye where the action is)?"
        - "Reward 5–10s reveals that earn their time and end on the CTA. Penalize fade-up-all-at-once, bouncy startup motion, decorative looping motion that fights the CTA."
    - key: restrained_palette
      name: "Restrained Mint & Off-White Palette"
      weight: 1.8
      evaluation_steps:
        - "Is the palette restrained to off-white / cream backgrounds, mint-green as a sparing accent (#3DB07A, #4DB896, #2D7A55 range), and one ink / charcoal text color? Roughly three colors total."
        - "Is mint-green used SPARINGLY — primarily on the CTA and one or two ritual markers — not as a saturated background or gradient wash?"
        - "Bright greens, lime, kelly green, dark mode, neon, glassmorphism, and gradient washes read as SaaS / tech and work against the ceremonial tone."
    - key: ceremonious_whitespace
      name: "Ceremonious Whitespace & Composition"
      weight: 1.5
      evaluation_steps:
        - "Does the page breathe — generous margins (~80px+), generous padding around the focal element, intentional silence around the sigil and CTA?"
        - "Is the composition centered-and-reverent (single hero stack with sigil + headline + CTA axis-aligned) or confidently editorial-asymmetric? A cramped marketing grid of feature sections works against this."
        - "Layouts that read as default marketing landing pages (hero + 3 feature cards + testimonial + CTA) work against ceremonial composition."
    - key: conversion_focus
      name: "Conversion Focus (CTA Dominance)"
      weight: 1.5
      evaluation_steps:
        - "Is there ONE obvious primary action — a single button or seal — inside the center-hero focal area, labeled in ceremonious language ('Take the Vow', 'Join the Hourly Order', 'Receive the First Mint')? Generic SaaS copy ('Get Started', 'Sign Up', 'Learn More') works against the tone."
        - "Does the CTA visually dominate the page in the settled state — through color contrast, size, and motion anchor — so a viewer's eye lands on it within the first second?"
        - "Multiple competing CTAs, nav-bar CTAs, or a CTA that's less prominent than decorative elements all weaken conversion focus."
    - key: typography_basics
      name: "Typographic Basics"
      weight: 0.4
      evaluation_steps:
        - "Limited type scale used consistently, readable line lengths, no clipping."
        - "Floor check, not a creativity dimension."
    - key: layout_basics
      name: "Layout & Alignment Basics"
      weight: 0.4
      evaluation_steps:
        - "Elements align to a grid, consistent gutters, no overflow at 1280×800."
        - "Floor check."

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
  # Desktop viewport for a conversion landing page. Candidates render at
  # 1280×800 (a standard laptop fold) and the dashboard scales them down
  # proportionally.
  viewport:           [1280, 800]
  animation_seconds:  10.0
  # 10 frames evenly spaced across the 10s window (fractional timestamps, endpoints included)
  keyframes:          [0.0, 0.11, 0.22, 0.33, 0.44, 0.56, 0.67, 0.78, 0.89, 1.0]
```
