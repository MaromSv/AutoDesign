---
name: generator
description: Use this agent when AutoDesign needs to produce a new candidate landing-page HTML — either the gen-0 baseline from the brief, or a critique-driven edit of the current winner.
tools: Read, Write, Edit, Glob, Grep
model: sonnet
---

# TODO: generator system prompt

Placeholder body. The real prompt will: (1) read the brief + optional critique
and `nameable_decisions`, (2) write a single self-contained html file at the
target path with inline CSS and viewport sized to `config.capture.viewport`,
(3) emit no prose outside the file.
