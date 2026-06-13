# AutoDesign

Nvidia x Whale Hackathon project.

## Goal

Make AI-generated UIs that feel human. The premise: AI coding is mostly solved, but UI generation is not — even tools like Claude's design output don't feel human. AutoDesign is a loop that generates candidate UIs, benchmarks them, and iterates until a quality threshold is reached.

Tagline: **"Use our system to design our system."** The landing page / demo UI is itself produced by AutoDesign.

## Pipeline

```
user prompt
  -> build 5 candidate UIs
  -> evaluate each with the benchmark
  -> get metrics + feedback
  -> select best UI
  -> apply feedback to improve UI  ──┐
                                     │ loop until threshold reached
  -> (back to evaluate) <────────────┘
  -> output final UI + design-journey page
```

The design-journey page is a deliverable: it shows the evolution of the UI across iterations alongside saliency maps and judge rationales.

## Benchmark (the core IP)

Three signals, combined:

1. **Tribe v2 emotion metrics** — optimize for the emotion / feeling the UI should evoke. *Owner: oliver.*
2. **Saliency prediction (pysaliency / DeepGaze family)** — predict where eyes will land and scan, then score the heatmap/scanpath against design-theory rules (hierarchy alignment, CTA prominence, F/Z patterns, attention dispersion). *Owner: marom.*
3. **VLM-as-judge** — vision model scores the rendered UI against design-theory rubrics. Includes an "AI slop" sub-metric — calibrated by showing the judge known-slop reference sites. *Owner: oliver.*

Auxiliary signals under consideration:
- **One-shottability check**: periodically hand a UI snapshot to a VLM, ask it to generate the prompt that would produce that UI, regenerate from that prompt, and measure how close the regenerated UI is. High one-shottability = the design is legible and not over-engineered.
- **Agent simulation**: simulate a user agent attempting a task on the UI.

## Demo plan

- Prerecorded fast-forwarded video of using the tool end-to-end.
- Show one UI's evolution across loop iterations.
- Side-by-side: AutoDesign output vs. baseline AI-generated UIs (real vs fake framing).
- Saliency-map evolution overlay + "brain visualization" + LLM-judge commentary playing alongside.

## Ownership

- **Harness / orchestration**: marom
- **Saliency benchmark component**: marom
- **Tribe v2 emotion metrics + AI-slop metric**: oliver

## System prompt note

UI generation should use **dev21.tools** (or similar) as the target stack — flagged in the design but not finalized.

## Repo layout

- `experiments/` — exploratory scripts. `eyetrackingmetrics.py` is the saliency-benchmark scratchpad.
- (more to come as the harness lands)
