"""AutoDesign pipeline package.

Importing `pipeline.signals` triggers all signal modules to register themselves
with the registry. Most callers should import that submodule explicitly:

    import pipeline
    import pipeline.signals  # noqa: F401 — populates the registry

The pipeline package itself is intentionally thin — see the individual modules
for the real contracts:

- `pipeline.context`   — `CandidateContext`, `SignalResult` dataclasses
- `pipeline.registry`  — `@register_signal`, `get_signals()`
- `pipeline.config`    — `load_config()` reads the yaml block in `autodesign.md`
- `pipeline.artifacts` — disk layout under `.autodesign/runs/<id>/`
- `pipeline.benchmark` — score a single candidate
- `pipeline.capture`   — render html -> screenshots (TODO: Playwright)
- `pipeline.evolve`    — orchestrate generations (TODO)
- `pipeline.evaluate`  — held-out ablation + TrueSkill (TODO)
"""

__all__ = [
    "context",
    "registry",
    "config",
    "artifacts",
    "benchmark",
    "capture",
    "evolve",
    "evaluate",
]
