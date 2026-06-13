"""AI-slop evidence for the vlm_judge `ai_pitfalls` rubric principle.

Runs slop-detector's deterministic matching — builder/stack fingerprints (Lovable, v0,
Bolt, Framer, shadcn, Tailwind...) plus, if a reference corpus is configured, copy and
structural similarity to known AI-generated sites — on the candidate's code, and formats
the findings as text evidence. That evidence is injected into the VLM judge prompt; the
VLM then *decides* the `ai_pitfalls` score using it alongside the visual tells it sees.

Not a registered signal (leading underscore) — a shared helper `vlmjudge` imports. The
whole thing is optional: if `slop_detector` isn't importable, `slop_evidence` returns
None and the principle is simply not added to the rubric.
"""

from __future__ import annotations

from pathlib import Path

from pipeline.signals._ui_rubric import UXPrinciple


def ai_pitfalls_principle(weight: float = 2.5) -> UXPrinciple:
    """The rubric dimension the VLM scores from slop-detector evidence + what it sees.

    Weighted heavily by default (2.5 vs ~1.0 for the general UX principles): a
    polished-but-generic AI site scores well on hierarchy/typography/contrast, so the
    pitfall dimension needs real pull to drag its combined score down to where it belongs.
    """
    return UXPrinciple(
        key="ai_pitfalls", name="AI-Generated Pitfall Avoidance", weight=weight,
        evaluation_steps=[
            "An automated code analysis (slop-detector) is provided above under "
            "'AUTOMATED SLOP ANALYSIS': it reports AI no-code builder fingerprints "
            "(Lovable, v0, Bolt, Framer) and the page's similarity to known AI-generated "
            "sites. Treat a confirmed builder fingerprint as strong evidence of AI generation.",
            "Combine that with the visual tells you can see in the frames: the "
            "hero -> 3 feature cards -> CTA boilerplate layout, purple/indigo gradients, "
            "generic Inter/system typography, glassmorphism, filler copy (Elevate / "
            "Seamlessly / Unlock), and generic stock imagery.",
            "Score 10 = no AI-generated pitfalls (looks bespoke and hand-built); "
            "0 = textbook AI-slop (strong fingerprints AND generic, templated visuals). "
            "A high automated slop score with matching visuals should score low here.",
        ],
    )


def slop_evidence(html: str | None, url: str | None,
                  corpus_path: str | None = None, render: bool = False) -> dict | None:
    """Run slop-detector on the candidate; return its `SlopScore.as_dict()` or None.

    Prefers the candidate's HTML (`html`, e.g. a generated index.html or rendered DOM);
    falls back to fetching `url`. With a `corpus_path` it uses the full matching (hard
    fingerprints + copy/structural similarity); without one it is fingerprints-only —
    which still catches AI no-code builders by their smoking-gun markers.
    """
    try:
        from slop_detector import SlopDetector
    except ImportError:
        return None

    if corpus_path and Path(corpus_path).exists():
        detector = SlopDetector.from_corpus_file(corpus_path)
    else:
        detector = SlopDetector.fingerprints_only()

    try:
        if html and html.strip():
            result = detector.analyze_html(html)
        elif url:
            result = detector.analyze_url(url, render=render)
        else:
            return None
    except Exception:  # noqa: BLE001 - matching is best-effort evidence; never break the judge
        return None
    return result.as_dict()


def format_evidence(ev: dict) -> str:
    """Render a `SlopScore.as_dict()` into the prompt's AUTOMATED SLOP ANALYSIS block.

    The human-readable `reasons` carry the specific tells ("gpteng.co", "lucide icons"),
    so we surface those rather than the fingerprint dict's internal keys.
    """
    comp = ev.get("components") or {}
    lines = [
        "AUTOMATED SLOP ANALYSIS (slop-detector — evidence for the ai_pitfalls principle; "
        "higher slop score = more AI-generated):",
        f"  overall slop score: {ev.get('score')}/100 "
        f"({ev.get('label')}, {ev.get('confidence')} confidence)",
        f"  hard builder-fingerprint signal: {comp.get('hard')}  "
        "(1.0 = definitive AI no-code builder match; 0.4 = generic AI stack tell)",
        f"  copy similarity to AI-site voice: {comp.get('copy')}  "
        f"· structural similarity to AI layouts: {comp.get('structural')}",
    ]
    reasons = ev.get("reasons") or []
    if reasons:
        lines.append("  findings:")
        lines += [f"    - {r}" for r in reasons[:4]]
    return "\n".join(lines)
