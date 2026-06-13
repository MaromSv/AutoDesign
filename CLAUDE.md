# AutoDesign — Project Context for Claude Code

AutoDesign is an evolutionary UI generator. Given a brief, it produces candidate landing
pages, scores each through a set of pluggable signals, and iteratively refines a winner.
A read-only dashboard inspects runs after the fact.

This file is a stub. It exists so the project always has a place to put orientation
notes for future Claude Code sessions. Keep it short — point to code, do not duplicate it.

## Where things live

- `autodesign.md` — control surface. The yaml block at the bottom drives behavior
  (brief, loop settings, model assignments, signal weights, saliency focus, capture
  settings). Edit this, not the code, to change what the loop does.
- `pipeline/` — the engine. Builds `CandidateContext`s, runs registered signals,
  writes artifacts under `.autodesign/runs/<id>/`.
- `pipeline/signals/` — pluggable evaluations. Each file registers exactly one
  `Signal` via `@register_signal`. To add an evaluation, add a file and a config entry.
- `dashboard/` — a read-only viewer. Talks to the engine through `/api/runs` and
  `/api/run/<id>` only — never imports from `pipeline/`.
- `.claude/agents/` — one subagent per reasoning role (generator, critic, judge,
  evaluator, persona). Each has its own model in frontmatter for cost tiering.
- `.claude/skills/autodesign/SKILL.md` — the loop protocol and artifact schema.
- `.claude/commands/autodesign.md` — the orchestration prompt that runs the loop.

## Architecture invariants (do not violate)

1. Signals plug in via the registry. Adding one = a new file + a config entry.
2. The loop only writes to disk; the dashboard only reads from disk through the
   manifest API. They never share memory.
3. Run state lives at `.autodesign/runs/<id>/`. That layout is the contract.
4. Behavior is config-driven. No hardcoded weights or model names in code.
5. Agentic steps are separate subagents so each can use an independent model tier.

## Status

Scaffold only. Every function has a docstring and a `# TODO`. There is no real
scoring, capture, evolution, or rendering yet — only the seams to add them.
