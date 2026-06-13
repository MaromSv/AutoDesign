"""PromptConsistencySignal — does the generated website match the brief (the prompt)?

This is the standalone, text-based "prompt -> generated website consistency" criterion.
It is distinct from the vlm_judge `brief_adherence` rubric principle: that one is
visual-only (it judges the rendered screenshot and is folded into the vlm_judge score).
This signal reads the actual generated HTML/copy and asks a Nemotron model on Nebius
Token Factory to check, point by point, whether what the brief asked for is present,
missing, or contradicted — returning a 0-10 consistency score.

If a vision-capable Nemotron tier is configured (`nemotron.vision: true`), one rendered
frame is attached so the model can also see layout/visual requirements; otherwise it
works from the code + copy alone.

Skips (score=None) when there is no brief, no generated content, or Nemotron is
unavailable — this criterion is purely model-based, so there is no heuristic fallback.
"""

from __future__ import annotations

from pathlib import Path

from pipeline.context import CandidateContext, SignalResult
from pipeline.registry import register_signal
from pipeline.signals import _nemotron

# Cap the code we send so a huge inlined SVG/base64 blob doesn't blow the context window.
_MAX_CODE_CHARS = 24000

_SYSTEM = (
    "You evaluate whether a generated website is consistent with the design brief that "
    "was used to prompt its creation. You are given the brief and the generated site's "
    "HTML/CSS/copy (and possibly a screenshot). Go through the brief's CONCRETE, checkable "
    "requirements — required sections/elements, specific features, calls to action, named "
    "content/copy, data, and any explicit must-haves — and verify each is actually present "
    "in the build. Reward sites that deliver everything the brief asked for; penalize "
    "missing required elements, off-brief content, and direct contradictions.\n\n"
    "STAY IN YOUR LANE — do NOT score (other criteria already own these, scoring them here "
    "would double-count): visual design quality, aesthetics, layout, visual hierarchy, "
    "color/typography polish, tone refinement, originality vs competitors, or AI-slop "
    "tells. A page can look mediocre and still be fully consistent with the brief — if "
    "every requested thing is present and nothing contradicts the brief, score it high. "
    "Judge presence-and-fidelity of requested content/features ONLY.\n\n"
    "Respond with ONLY a JSON object: "
    '{"score": <0-10 number>, "matched": ["requirement met", ...], '
    '"missing": ["requirement absent", ...], "contradictions": ["brief said X, site does Y", ...], '
    '"rationale": "one or two sentences"}.'
)


@register_signal
class PromptConsistencySignal:
    key = "prompt_consistency"

    def score(self, ctx: CandidateContext) -> SignalResult:
        brief = (ctx.brief or "").strip()
        # The per-candidate generation prompt (the `<!-- hypothesis: ... -->` directive this
        # candidate was built to test). It IS the prompt for this specific build, so it's the
        # most direct thing to check consistency against — pair it with the run-wide brief.
        gen_prompt = (ctx.generation_prompt or "").strip()
        if not brief and not gen_prompt:
            return SignalResult(score=None, skipped="no brief or generation prompt to check consistency against")

        code = (ctx.code_text or "").strip()
        cfg = (ctx.config or {}).get("prompt_consistency") or {}
        nm = _nemotron.nemotron_config(ctx.config)
        use_vision = bool(cfg.get("vision", nm.get("vision", False)))
        frame = _first_frame(ctx) if use_vision else None

        if not code and frame is None:
            return SignalResult(score=None, skipped="no generated content (code or frame) to evaluate")

        content = _build_user_content(brief, gen_prompt, code, frame)
        try:
            raw = _nemotron.chat(
                ctx.config,
                messages=[
                    {"role": "system", "content": _SYSTEM},
                    {"role": "user", "content": content},
                ],
                model=cfg.get("model"),
                # Nemotron-3-Ultra is a reasoning model: the thinking trace eats the
                # token budget, so 1024 truncated the JSON answer mid-object and it
                # failed to parse. Give the visible answer room after reasoning.
                max_tokens=4096,
                force_json=True,
            )
        except _nemotron.NemotronUnavailable as exc:
            return SignalResult(score=None, skipped=str(exc))
        except Exception as exc:  # noqa: BLE001 - any model/transport error -> skip, don't crash
            return SignalResult(score=None, skipped=f"nemotron error: {exc}")

        parsed = _nemotron.extract_json(raw)
        score = _coerce_score(parsed)
        if score is None:
            return SignalResult(score=None, skipped="could not parse nemotron response",
                                details={"raw_response": raw[:2000]})

        return SignalResult(
            score=score,
            details={
                "model": _nemotron.resolve_model(ctx.config, cfg.get("model")),
                "used_vision": frame is not None,
                "generation_prompt": gen_prompt,
                "matched": _clean_list(parsed.get("matched")),
                "missing": _clean_list(parsed.get("missing")),
                "contradictions": _clean_list(parsed.get("contradictions")),
                "rationale": str(parsed.get("rationale", "")).strip(),
            },
        )


# --------------------------------------------------------------------------- helpers
def _first_frame(ctx: CandidateContext) -> Path | None:
    for f in ctx.frames or []:
        p = Path(f)
        if p.exists():
            return p
    return None


def _build_user_content(brief: str, gen_prompt: str, code: str, frame: Path | None):
    """Return OpenAI-style message content (str if text-only, else a content-part list)."""
    code_block = code[:_MAX_CODE_CHARS]
    truncated = " (truncated)" if len(code) > _MAX_CODE_CHARS else ""
    parts = []
    if brief:
        parts.append(f"RUN BRIEF (the overall goal every candidate shares):\n{brief}")
    if gen_prompt:
        parts.append(
            "THIS CANDIDATE'S PROMPT (the specific directive it was generated to deliver — "
            f"check the build against this most closely):\n{gen_prompt}"
        )
    parts.append(f"GENERATED WEBSITE SOURCE{truncated}:\n{code_block or '(no source captured)'}")
    parts.append("Check the site against the prompt(s) above and return only the JSON object.")
    text = "\n\n".join(parts)
    if frame is None:
        return text
    return [{"type": "text", "text": text}, _nemotron.image_content(frame)]


def _coerce_score(parsed: dict | None) -> float | None:
    if not isinstance(parsed, dict) or "score" not in parsed:
        return None
    try:
        return max(0.0, min(10.0, float(parsed["score"])))
    except (TypeError, ValueError):
        return None


def _clean_list(v) -> list[str]:
    if not isinstance(v, list):
        return []
    return [str(x).strip() for x in v if str(x).strip()][:20]
