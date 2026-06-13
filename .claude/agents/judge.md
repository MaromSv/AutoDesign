---
name: judge
description: Use this agent when AutoDesign needs a high-quality VLM verdict on a rendered candidate — the in-loop quality signal that scores how well the design serves the brief.
tools: Read, Glob
model: opus
---

# TODO: judge system prompt

Placeholder body. The real prompt will: (1) take the brief and one or more
captured frames, (2) apply a fixed rubric (hierarchy, brief-adherence, taste,
no-AI-slop), (3) return a strict json object with a 0-10 score and a
one-sentence critique. This agent uses opus because it is the most expensive
and most consequential signal in the loop.
