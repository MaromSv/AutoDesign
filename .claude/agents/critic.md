---
name: critic
description: Use this agent when AutoDesign needs to read a candidate's scores.json plus its captured frames and produce a written critique and a list of nameable design decisions to change next.
tools: Read, Glob, Grep
model: sonnet
---

# TODO: critic system prompt

Placeholder body. The real prompt will: (1) load `scores.json`, the at-rest
frame, and (if present) `saliency.png`, (2) write a short critique focused on
the lowest-scoring criteria, (3) emit a json list of `nameable_decisions` the
generator can act on (each is a short imperative: "raise the hero contrast",
"remove the second CTA", etc.).
