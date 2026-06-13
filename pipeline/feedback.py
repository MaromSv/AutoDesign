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
    explorations = vj.get("explorations") or []   # creative design moves to TRY
    issues = vj.get("issues") or []               # visual things the judge saw to refine
    judge_critique = vj.get("critique", "")
    critic_critique = scores.get("critique", "")
    decisions = scores.get("nameable_decisions") or []
    lowlights = _per_principle_lowlights(vj.get("per_principle") or {})
    combined = scores.get("combined")

    # The non-visual signals find concrete, fixable defects the VLM judge isn't focused on:
    # off-target attention (saliency), broken/dead interactions (stress_test), and brief
    # requirements missing from the build (prompt_consistency). These are the "fix" list.
    saliency = (raw.get("saliency", {}) or {}).get("details", {}) or {}
    attention_fixes = _saliency_lowlights(saliency)
    stress = (raw.get("stress_test", {}) or {}).get("details", {}) or {}
    stress_issues = stress.get("issues") or []
    consistency = (raw.get("prompt_consistency", {}) or {}).get("details", {}) or {}
    missing = consistency.get("missing") or []
    contradictions = consistency.get("contradictions") or []

    lines: list[str] = ["# Direction for the next version — make it more striking, then fix what's broken"]
    if combined is not None:
        lines.append(f"\nPrevious combined score: **{combined:.2f}/10**. The biggest wins come from "
                     "design ambition, not just patching defects — lead with the creative direction "
                     "below, then clean up the fixes. Keep what already works.")

    # ---- PRIORITY: creative design direction from the VLM judge -------------------------
    if explorations:
        lines.append("\n## Design direction — bold creative moves to try (THIS IS THE PRIORITY)")
        lines.append("The judge looked at the current design and named what it lacks. Pick the ones "
                     "that fit the brief and push them hard — aim for a more distinctive, memorable, "
                     "beautiful page, not a safe one. Pursue at least the top idea fully:")
        for i, ex in enumerate(explorations, 1):
            lacks = ex.get("lacks", "")
            idea = ex.get("idea", "")
            lines.append(f"{i}. **Try:** {idea}")
            if lacks:
                lines.append(f"   - *currently lacks:* {lacks}")
    else:
        # No structured explorations came back — still steer toward elevation, grounded in the
        # judge's verdict / weakest aesthetic principles rather than only fixing.
        lines.append("\n## Design direction — make it more striking (THIS IS THE PRIORITY)")
        lines.append("Don't just patch defects — make a bold, tasteful design move that raises "
                     "creativity / originality / visual appeal (a signature motif, a more confident "
                     "hero, a distinctive type or color treatment, an editorial layout).")

    # ---- Secondary: visual refinements the judge flagged --------------------------------
    if issues:
        lines.append("\n## Visual refinements the judge flagged (worst first)")
        for i, it in enumerate(issues, 1):
            where = it.get("where", "(unspecified element)")
            problem = it.get("problem", "")
            fix = it.get("fix", "")
            sev = (it.get("severity", "medium") or "medium").upper()
            lines.append(f"{i}. [{sev}] **{where}** — {problem}")
            if fix:
                lines.append(f"   - FIX: {fix}")

    # ---- Things to fix, from the non-visual checks --------------------------------------
    if attention_fixes:
        lines.append("\n## Attention problems the saliency model found (fix — the eye must land right)")
        for a in attention_fixes:
            lines.append(f"- {a}")

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
    lines.append("\n**Commit to at least one bold creative move from the design direction above, AND "
                 "fix the concrete issues. Do not return a near-identical page — a timid copy-edit "
                 "wastes the round.**")
    return "\n".join(lines)


def _saliency_lowlights(saliency_details: dict, threshold: float = 0.6) -> list[str]:
    """Saliency subscores below `threshold` (0-1), worst-first, with their explanations.

    These are attention problems (eye not landing on the focal element, chaotic scanpath,
    no clear hierarchy) the VLM judge isn't scoring — the concrete-fix half of the brief.
    """
    subs = saliency_details.get("subscores") or {}
    expl = saliency_details.get("explanations") or {}
    rows = []
    for key, score in subs.items():
        if key == "total":
            continue
        if isinstance(score, (int, float)) and score < threshold:
            why = expl.get(key, "")
            rows.append((score, f"{key.replace('_', ' ')} ({score:.2f}): {why}".strip()))
    rows.sort(key=lambda r: r[0])
    return [r[1] for r in rows]


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
