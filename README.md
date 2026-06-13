# AutoDesign

Scaffold for an evaluation-driven UI generator. A `/autodesign` loop produces candidate
landing pages from a brief, scores each against a pluggable set of signals (saliency,
VLM judge, code quality, task completion, embeddings), and evolves the winner across
generations. A read-only dashboard reads run artifacts from disk.

This repository is currently a **scaffold**: directories, file stubs, and the
interfaces that future implementations plug into. No real scoring, capture, evolution,
or rendering logic exists yet.

## How to run

```bash
pip install -r requirements.txt
python -c "import pipeline, pipeline.signals"   # all signal stubs self-register
python -m pytest tests/test_scaffold.py         # smoke test
python dashboard/serve.py                        # empty-state dashboard
```

## Extensibility contract

The scaffold is designed so common changes touch exactly one place:

- **Add an evaluation**: drop a new file in `pipeline/signals/<name>.py` with a class
  decorated by `@register_signal` whose `key` matches a new entry in the
  `criteria:` block of `autodesign.md`. Nothing else changes — `pipeline/signals/__init__.py`
  auto-imports the module, the registry picks it up, and the benchmark combiner runs
  it whenever the key is in config.
- **Swap or restyle the UI**: edit only `dashboard/`. It consumes the
  `GET /api/run/<id>` manifest contract and the `.autodesign/runs/<id>/` directory
  layout, nothing else.
- **Change models / cost tiers**: edit the `models:` block in `autodesign.md` and the
  frontmatter of `.claude/agents/*.md`. No code changes.

## Architecture invariants

1. **Pluggable evaluations.** Every signal implements the `Signal` protocol and
   self-registers via `@register_signal`.
2. **Engine / presentation decoupling.** The loop only writes artifacts to disk; the
   dashboard only reads them through the manifest API.
3. **Disk-as-contract.** All run state lives under `.autodesign/runs/<id>/`. No
   in-memory passing between the loop and the UI. Runs are replayable.
4. **Config-driven.** Behavior comes from the yaml block in `autodesign.md`. No
   hardcoded weights or model names in code.
5. **Model-tiering ready.** Each agentic step is a separate Claude Code subagent so
   models can be assigned independently.
