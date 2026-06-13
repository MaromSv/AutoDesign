"""Preflight readiness check for scoring signals.

The failure mode we keep hitting: a signal's dependency (Python SDK or API key) is
missing, so the signal silently returns score=None, `combined` collapses, and the
dashboard shows blank scores with no obvious cause. This module checks, for every
signal ENABLED in `criteria:`, whether its runtime deps are actually present — so the
gap is reported LOUDLY before a run rather than discovered after.

`check(config)` returns one row per enabled criterion with a status:
  - READY     — all deps + keys present; the signal will produce a real score.
  - DEGRADED  — will still score, but in a reduced mode (e.g. stress_test without
                NEBIUS_API_KEY falls back to its deterministic heuristic).
  - SKIP      — a hard dep is missing; the signal WILL return None and not contribute.

`format_report(rows)` renders it for the console; `run_generation` prints it at start.
"""

from __future__ import annotations

import importlib.util
import os

from pipeline.signals._nemotron import DEFAULT_KEY_ENV, nemotron_config

# signal key -> what it needs for a real score. `soft_env` keys only DEGRADE (not SKIP)
# when missing, because the signal has a working fallback. `NEBIUS_API_KEY` is resolved
# to the configured env var name at check time (nemotron.api_key_env).
_REQUIREMENTS: dict[str, dict] = {
    "vlm_judge":          {"modules": ["anthropic"], "env": ["ANTHROPIC_API_KEY"], "note": "Claude VLM judge"},
    "saliency":           {"modules": ["torch", "deepgaze_pytorch"], "env": [], "note": "DeepGaze attention"},
    "stress_test":        {"modules": ["openai", "playwright"], "env": ["NEBIUS_API_KEY"],
                           "soft_env": ["NEBIUS_API_KEY"], "note": "Nemotron QA (heuristic without key)"},
    "prompt_consistency": {"modules": ["openai"], "env": ["NEBIUS_API_KEY"], "note": "Nemotron brief-match"},
    "brain_judge":        {"modules": ["sklearn", "PIL", "joblib"], "env": [], "note": "perceptual-fallback classifier (no real TRIBE)"},
}


def _module_present(name: str) -> bool:
    try:
        return importlib.util.find_spec(name) is not None
    except (ImportError, ValueError):
        return False


def check(config: dict | None) -> list[dict]:
    """Readiness of every signal listed in `criteria:`. See module docstring."""
    criteria = (config or {}).get("criteria") or {}
    nebius_env = str(nemotron_config(config).get("api_key_env") or DEFAULT_KEY_ENV)

    def resolve_env(name: str) -> str:
        return nebius_env if name == "NEBIUS_API_KEY" else name

    rows: list[dict] = []
    for key, weight in criteria.items():
        req = _REQUIREMENTS.get(key, {"modules": [], "env": [], "note": "(unknown signal)"})
        soft = set(req.get("soft_env", []))
        missing_mod = [m for m in req.get("modules", []) if not _module_present(m)]
        missing_env_hard = [resolve_env(e) for e in req.get("env", [])
                            if e not in soft and not os.getenv(resolve_env(e))]
        missing_env_soft = [resolve_env(e) for e in soft if not os.getenv(resolve_env(e))]

        if missing_mod or missing_env_hard:
            status = "SKIP"
        elif missing_env_soft:
            status = "DEGRADED"
        else:
            status = "READY"

        reasons = []
        if missing_mod:
            reasons.append(f"missing module(s): {', '.join(missing_mod)} (pip install -r requirements.txt)")
        if missing_env_hard:
            reasons.append(f"missing key(s): {', '.join(missing_env_hard)}")
        if missing_env_soft:
            reasons.append(f"no {', '.join(missing_env_soft)} -> heuristic fallback")

        rows.append({
            "key": key, "weight": weight, "status": status,
            "note": req.get("note", ""), "reasons": reasons,
        })
    return rows


def format_report(rows: list[dict]) -> str:
    """One line per signal: status, key, note, and any missing-dep reasons."""
    if not rows:
        return "preflight: no criteria enabled."
    out = ["preflight — signal readiness:"]
    for r in rows:
        tail = f" — {'; '.join(r['reasons'])}" if r["reasons"] else ""
        out.append(f"  [{r['status']:<8}] {r['key']} (w={r['weight']}) · {r['note']}{tail}")
    skips = [r["key"] for r in rows if r["status"] == "SKIP"]
    if skips:
        out.append(f"  ⚠ WILL NOT CONTRIBUTE (returns null): {', '.join(skips)} — fix deps/keys above.")
    return "\n".join(out)
