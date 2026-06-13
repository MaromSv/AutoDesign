---
name: evaluator
description: Use this agent when AutoDesign needs an OUT-OF-LOOP pairwise verdict for ablation analysis. The evaluator MUST be a different model from the in-loop judge so the held-out evaluation is not just measuring the optimizer.
tools: Read, Glob
model: sonnet
---

# TODO: evaluator system prompt

Placeholder body. The real prompt will: (1) take two captured candidates plus
the shared brief, blinded, (2) return a strict json object naming the
preferred candidate and one sentence of reasoning. Must NOT see in-loop scores.
Model must differ from `models.judge` in `autodesign.md`.
