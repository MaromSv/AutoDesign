"""UX rubric used by the VLM judge signal.

Not a registered signal (note the leading underscore) — a shared data module the
`vlm_judge` signal imports. Each `UXPrinciple` is one dimension the vision model scores
0-10, with explicit `evaluation_steps` (a short chain of thought) rather than a vague
one-line criterion — VLM grading is far more reproducible when the checks are spelled out.

`needs_motion` marks principles that can only be judged across the captured frame
*sequence* (the reason we feed the 1-fps clip rather than a single screenshot).

The default rubric is opinionated but overridable from `autodesign.md` under a
`vlm_judge:` block (see `load_rubric`), so taste lives in config, not code.
"""
from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class UXPrinciple:
    key: str
    name: str
    evaluation_steps: list[str]
    weight: float = 1.0
    needs_motion: bool = False


DEFAULT_PRINCIPLES: list[UXPrinciple] = [
    # Creativity is the headline dimension this benchmark exists to drive.
    # It is weighted to dominate combined score so iterations move FROM
    # generic AI output TOWARD genuinely distinctive design. Renaming away
    # from "polish_distinctiveness" makes the intent obvious in the rubric
    # and on the dashboard.
    UXPrinciple(
        key="creativity", name="Creativity & Distinctiveness", weight=3.5,
        evaluation_steps=[
            "Imagine 20 landing pages someone might ship in an afternoon with v0 / Lovable / Bolt / Framer for this brief. How far is this candidate from that AI-slop baseline? Distance is the score.",
            "Look for SIGNATURE MOVES: a distinct visual concept that ties everything together, unconventional layout shapes (diagonal grids, asymmetric mass, full-bleed art with text reserved to a tight column, vertical-rule typography), kinetic display typography, named-and-memorable colors, a custom-feeling illustration system, a motion idea that tells a 2-second story.",
            "Heavily penalize the AI-slop fingerprint: hero -> 3 feature cards -> CTA layout, default Inter + indigo/purple gradient, generic 'modern minimal' that could belong to any product, stock-photo vibes, decorative motion that doesn't say anything.",
            "Calibration: 10/10 = a viewer screenshots it for design inspiration; 7/10 = clearly considered, has personality; 5/10 = competent but forgettable; 2/10 = the generic slop this benchmark is trying to escape.",
            "Reward execution courage. A bold idea executed sloppily still outranks a safe idea executed cleanly — but well-executed boldness is the actual target.",
        ],
    ),
    UXPrinciple(
        key="motion", name="Motion & Animation Quality", weight=2.0, needs_motion=True,
        evaluation_steps=[
            "Compare frames in order. The entrance animation is the page's voice — does it have one?",
            "Reward purposeful motion: kinetic typography that lands a beat, staggered reveals that build a hierarchy, mask-wipes that reveal the hero idea, parallax that makes flat space feel deep. The animation should TELL A STORY about the product in 1-2 seconds.",
            "The settled state should resolve attention onto the focal element (a finale beat — scale, glow, color flash, underline draw) so the entrance terminates on the CTA.",
            "Penalize generic 'fade-up-and-stop' entrance, motion as decoration with no narrative arc, or settled-state motion that fights the CTA.",
            "If the UI is appropriately static for the brief, do NOT penalize absence of motion — score neutral-to-good — but for most briefs static = missed opportunity.",
        ],
    ),
    UXPrinciple(
        key="brief_adherence", name="Brief Adherence", weight=1.4,
        evaluation_steps=[
            "Read the design brief. Identify what was asked for: purpose, audience, tone, and any non-negotiables.",
            "Judge how well the rendered UI delivers on that brief — content, mood, and required elements.",
            "Penalize designs that look fine in the abstract but ignore the brief's specifics.",
        ],
    ),
    UXPrinciple(
        key="visual_hierarchy", name="Visual Hierarchy", weight=1.2,
        evaluation_steps=[
            "Find the single most prominent element in the at-rest frame; there should be one clear primary focal point (hero, primary CTA, key content).",
            "Check that size, weight, color, and spacing lead the eye from primary to secondary to tertiary.",
            "Penalize layouts where everything competes equally, or where the most dominant element is not the most important.",
        ],
    ),
    UXPrinciple(
        key="color_contrast", name="Color & Contrast", weight=1.1,
        evaluation_steps=[
            "Check the palette is cohesive and intentional, with a clear accent for primary actions.",
            "Assess whether text and controls have enough contrast to be legible (approximate WCAG AA).",
            "Penalize clashing colors, low-contrast text, or an accent used so liberally it loses meaning.",
        ],
    ),
    # Fundamentals — must hold a floor of quality but should NOT dominate
    # scoring. They're weighted low so a safe, well-typeset, well-aligned
    # generic page can't outscore a bold, slightly-rough creative leap.
    UXPrinciple(
        key="typography", name="Typography", weight=0.7,
        evaluation_steps=[
            "Check for a clear, limited type scale used consistently for headings, body, and captions.",
            "Check line length, line height, and text/background contrast for readability.",
            "Penalize too many fonts, inconsistent sizing for the same role, or cramped/illegible text.",
        ],
    ),
    UXPrinciple(
        key="layout_spacing", name="Layout, Spacing & Alignment", weight=0.6,
        evaluation_steps=[
            "Check elements align to a consistent grid with consistent gutters and margins.",
            "Check whitespace is intentional — neither cramped nor adrift.",
            "Penalize misaligned edges, inconsistent padding between similar components, and overflow/clipping.",
        ],
    ),
    UXPrinciple(
        key="consistency", name="Consistency", weight=0.5,
        evaluation_steps=[
            "Check repeated components (buttons, cards, inputs) share styling, sizing, and corner radius.",
            "Check spacing, color, and type decisions are applied uniformly.",
            "Penalize one-off styles or components that look like different design systems.",
        ],
    ),
    UXPrinciple(
        key="affordance_clarity", name="Affordance & Clarity", weight=0.6,
        evaluation_steps=[
            "Check interactive elements look interactive (buttons pressable, links clickable, inputs editable).",
            "Check the primary action is obvious and labels/icons communicate function without guesswork.",
            "Penalize ambiguous controls, unlabeled icons, or an unclear next step.",
        ],
    ),
]


def load_rubric(config: dict) -> list[UXPrinciple]:
    """Return the rubric, allowing `autodesign.md`'s `vlm_judge:` block to override it.

    Config shapes supported (all optional):
      vlm_judge:
        weights: {visual_hierarchy: 1.5, motion: 0.0}   # override/zero-out weights
        principles:                                       # fully custom rubric
          - {key: custom, name: "...", weight: 1.0, evaluation_steps: ["..."]}
    A weight of 0 drops that principle from scoring.
    """
    cfg = (config or {}).get("vlm_judge") or {}

    if cfg.get("principles"):
        out = []
        for p in cfg["principles"]:
            out.append(UXPrinciple(
                key=str(p["key"]),
                name=str(p.get("name", p["key"])),
                evaluation_steps=list(p.get("evaluation_steps", [])),
                weight=float(p.get("weight", 1.0)),
                needs_motion=bool(p.get("needs_motion", False)),
            ))
        return [p for p in out if p.weight > 0]

    overrides = cfg.get("weights") or {}
    rubric = []
    for p in DEFAULT_PRINCIPLES:
        w = float(overrides.get(p.key, p.weight))
        if w > 0:
            rubric.append(UXPrinciple(p.key, p.name, p.evaluation_steps, w, p.needs_motion))
    return rubric
