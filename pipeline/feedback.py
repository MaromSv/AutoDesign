"""Turn a scored candidate into a ready-to-apply feedback brief for the next round.

The refinement step of the loop only improves if the *concrete* feedback the
evaluators produced actually reaches the generator. Two sources matter:

  - The VLM judge's `issues` (raw.vlm_judge.details.issues): located, worst-first
    `{where, problem, fix, severity}` — the most actionable signal in the run.
  - The critic's `critique` + `nameable_decisions`, once the loop has merged them.

Historically these lived nested in scores.json and the orchestrator had to dig
them out by hand — and when it didn't, the generator got nothing and just
re-emitted the previous page. This module extracts them deterministically and
formats one markdown block to paste straight into the generator's edit prompt,
so the feedback can never silently get dropped.

Usage:

    python -m pipeline.feedback --candidate .autodesign/runs/<id>/gen-000/cand-02
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path

from pipeline.artifacts import SCORES_FILENAME


def _per_principle_lowlights(per_principle: dict, threshold: float = 7.0) -> list[str]:
    """Principle reasons whose score is below `threshold` (0-10), worst-first."""
    rows = []
    for key, val in (per_principle or {}).items():
        score = val.get("score") if isinstance(val, dict) else val
        reason = val.get("reason", "") if isinstance(val, dict) else ""
        if isinstance(score, (int, float)) and score < threshold:
            rows.append((score, key, reason))
    rows.sort(key=lambda r: r[0])
    return [f"{key} ({score:.0f}/10): {reason}" for score, key, reason in rows]


def build_feedback(candidate_dir: Path) -> str:
    """Return a markdown feedback brief for `candidate_dir`, or "" if no scores."""
    scores_file = Path(candidate_dir) / SCORES_FILENAME
    if not scores_file.exists():
        return ""
    try:
        scores = json.loads(scores_file.read_text())
    except json.JSONDecodeError:
        return ""

    raw = scores.get("raw", {}) or {}
    vj = (raw.get("vlm_judge", {}) or {}).get("details", {}) or {}
    issues = vj.get("issues") or []
    judge_critique = vj.get("critique", "")
    critic_critique = scores.get("critique", "")
    decisions = scores.get("nameable_decisions") or []
    lowlights = _per_principle_lowlights(vj.get("per_principle") or {})
    combined = scores.get("combined")

    # The two Nemotron signals find concrete, fixable defects the VLM judge can't see:
    # broken/dead interactions (stress_test) and brief requirements missing from the build
    # (prompt_consistency). Fold them in so the next round actually fixes them.
    stress = (raw.get("stress_test", {}) or {}).get("details", {}) or {}
    stress_issues = stress.get("issues") or []
    consistency = (raw.get("prompt_consistency", {}) or {}).get("details", {}) or {}
    missing = consistency.get("missing") or []
    contradictions = consistency.get("contradictions") or []

    lines: list[str] = ["# Feedback on the previous winner — fix these, do not start over"]
    if combined is not None:
        lines.append(f"\nPrevious combined score: **{combined:.2f}/10**. Your job is to raise it by "
                     "fixing the specific problems below, while keeping what already works.")

    if issues:
        lines.append("\n## Concrete issues the VLM judge pinpointed (worst first — address EVERY one)")
        for i, it in enumerate(issues, 1):
            where = it.get("where", "(unspecified element)")
            problem = it.get("problem", "")
            fix = it.get("fix", "")
            sev = (it.get("severity", "medium") or "medium").upper()
            lines.append(f"{i}. [{sev}] **{where}** — {problem}")
            if fix:
                lines.append(f"   - FIX: {fix}")

    if stress_issues:
        lines.append("\n## Broken interactions the stress test found (fix — controls must work)")
        for s in stress_issues:
            lines.append(f"- {s}")

    if missing or contradictions:
        lines.append("\n## Brief requirements not satisfied (add/correct these)")
        for m in missing:
            lines.append(f"- MISSING: {m}")
        for c in contradictions:
            lines.append(f"- CONTRADICTS BRIEF: {c}")

    if decisions:
        lines.append("\n## Critic's nameable decisions (apply each)")
        for d in decisions:
            lines.append(f"- {d}")

    if lowlights:
        lines.append("\n## Weakest rubric principles (raise these scores)")
        for low in lowlights:
            lines.append(f"- {low}")

    summary = critic_critique or judge_critique
    if summary:
        lines.append(f"\n## One-line verdict\n{summary}")

    if len(lines) == 1:
        return ""  # nothing actionable was found
    lines.append("\n**Make a visible, substantive change for each issue above. "
                 "Do not return a near-identical page — if nothing changes, the round is wasted.**")
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Print a generator-ready feedback brief from a candidate's scores.json.")
    parser.add_argument("--candidate", required=True, help="Scored candidate dir (the prior winner).")
    args = parser.parse_args(argv)
    text = build_feedback(Path(args.candidate))
    print(text or "(no actionable feedback found in scores.json)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
