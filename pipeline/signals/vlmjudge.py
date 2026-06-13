"""JudgeSignal — VLM judge that scores the rendered design against the brief + a UX rubric.

This is the highest-cost signal. It sends the captured frame *sequence* (the 1-fps clip,
so animation is visible) plus the brief and a rubric to the vision model named in
`models.judge` (see `autodesign.md`), and parses a structured 0-10 score per UX principle
plus a short critique.

Design notes:
  - We call the vision model directly (Anthropic Messages API for Claude tiers, OpenAI
    chat for gpt-* tiers) rather than going through DeepEval's multimodal layer — that
    layer's API (`MultimodalGEval`/`MLLMTestCase`) is unstable across deepeval versions
    and absent from the vendored 4.0.6. The methodology here is still G-Eval-style:
    explicit per-principle evaluation steps + chain-of-thought + a structured score.
  - The rubric lives in `_ui_rubric.py` and is overridable from `autodesign.md`
    (`vlm_judge:` block), so taste is config-driven per the project's invariants.
  - The signal never mutates `ctx` and degrades gracefully: missing frames, missing SDK,
    or no API key all return `score=None` with a human-readable `skipped` reason.
"""

from __future__ import annotations

import base64
import json
import os
import re
from pathlib import Path

from pipeline.context import CandidateContext, SignalResult
from pipeline.registry import register_signal
from pipeline.signals._ui_rubric import UXPrinciple, load_rubric
from pipeline.signals._slop_evidence import ai_pitfalls_principle, slop_evidence, format_evidence

# Tier name (from `models.judge`) -> concrete vision-capable model id. Overridable via
# `vlm_judge.tier_map` / `vlm_judge.model` in autodesign.md so nothing is truly hardcoded.
_DEFAULT_TIER_MAP = {
    "opus": "claude-opus-4-8",
    "sonnet": "claude-sonnet-4-6",
    "haiku": "claude-haiku-4-5",
    "gpt-4o": "gpt-4o",
    "gpt-4.1": "gpt-4.1",
}
_MAX_FRAMES = 10  # cap frames sent to bound cost/context; sampled evenly across the clip


@register_signal
class JudgeSignal:
    key = "vlm_judge"

    def score(self, ctx: CandidateContext) -> SignalResult:
        if not ctx.frames:
            return SignalResult(score=None, skipped="no frames captured for this candidate")

        rubric = load_rubric(ctx.config)
        if not rubric:
            return SignalResult(score=None, skipped="rubric is empty (all weights 0?)")

        # When the run acquired similar-use-case references (real competitors), fold an
        # originality dimension into the same VLM call: the judge sees those peers in-context
        # and scores how much the candidate stands out from the competitive landscape.
        references = [Path(p) for p in (ctx.references or []) if Path(p).exists()]
        if references:
            rubric = rubric + [_originality_principle(ctx.topic)]

        # AI-pitfall criterion: run slop-detector's matching (builder fingerprints +
        # optional corpus similarity) on the candidate's code and hand the findings to the
        # judge as evidence. The VLM scores the `ai_pitfalls` principle from that + the
        # visual tells. Skipped silently if slop_detector is absent or yields nothing.
        ap_cfg = (ctx.config or {}).get("ai_pitfalls") or {}
        evidence_text = ""
        evidence = None
        if ap_cfg.get("enabled", True):
            evidence = slop_evidence(ctx.code_text, ctx.html_url,
                                     corpus_path=ap_cfg.get("corpus"),
                                     render=bool(ap_cfg.get("render", False)))
            if evidence is not None:
                rubric = rubric + [ai_pitfalls_principle(weight=float(ap_cfg.get("weight", 2.5)))]
                evidence_text = format_evidence(evidence)

        model = _resolve_model(ctx.config)
        frames = _select_frames(ctx.frames, _MAX_FRAMES)
        if not frames:
            return SignalResult(score=None, skipped="frame paths do not exist on disk")
        prompt = _build_prompt(ctx.brief, rubric, n_frames=len(frames),
                               n_refs=len(references), topic=ctx.topic,
                               slop_evidence_text=evidence_text)

        try:
            raw = _call_vision_model(model, prompt, frames, references)
        except _JudgeUnavailable as exc:
            return SignalResult(score=None, skipped=str(exc))
        except Exception as exc:  # noqa: BLE001 - any model/transport error -> skip, don't crash the loop
            return SignalResult(score=None, skipped=f"judge model error: {exc}")

        parsed = _parse_response(raw)
        if parsed is None:
            return SignalResult(
                score=None,
                skipped="could not parse judge response",
                details={"raw_response": raw[:2000]},
            )

        score10, per_principle = _combine(rubric, parsed["scores"])
        return SignalResult(
            score=score10,
            details={
                "model": model,
                "n_frames": len(frames),
                "n_references": len(references),
                "ai_pitfalls_evidence": evidence,
                "critique": parsed.get("critique", ""),
                "per_principle": per_principle,
                "nameable_decisions": parsed.get("nameable_decisions", []),
            },
        )


# --------------------------------------------------------------------------- helpers
class _JudgeUnavailable(RuntimeError):
    """Raised when the judge cannot run for an expected reason (no SDK / no key)."""


def _resolve_model(config: dict) -> str:
    cfg = (config or {}).get("vlm_judge") or {}
    if cfg.get("model"):
        return str(cfg["model"])
    tier_map = {**_DEFAULT_TIER_MAP, **(cfg.get("tier_map") or {})}
    tier = ((config or {}).get("models") or {}).get("judge", "opus")
    return tier_map.get(str(tier), str(tier))


def _is_openai(model: str) -> bool:
    return model.startswith(("gpt-", "o1", "o3", "o4"))


def _select_frames(frames: list[Path], cap: int) -> list[Path]:
    frames = [Path(f) for f in frames if Path(f).exists()]
    if len(frames) <= cap:
        return frames
    # even sample across the clip, always keeping first and last
    idx = [round(i * (len(frames) - 1) / (cap - 1)) for i in range(cap)]
    return [frames[i] for i in sorted(set(idx))]


def _originality_principle(topic: str) -> UXPrinciple:
    """A rubric dimension scored only when similar-use-case references are in context.

    Added by `JudgeSignal.score` when the run acquired peer screenshots (real competitors
    for the same use case), so the same VLM call also judges how much the candidate stands
    out from the competitive landscape it lives in.
    """
    where = f"similar {topic} products / competitors" if topic else "similar products / competitors"
    return UXPrinciple(
        key="originality", name="Originality vs. Similar Products", weight=2.0,
        evaluation_steps=[
            f"The images labeled REFERENCE are real, currently-deployed {where} serving the "
            "same use case. The frames labeled CANDIDATE are the design under review.",
            "Name what the candidate SHARES with the reference set vs. what makes it its own: "
            "layout archetype, color story, type personality, imagery, signature interactions. "
            "A design that looks interchangeable with its competitors is not original.",
            "Score how much the candidate STANDS OUT as a distinctive, memorable take on this "
            "use case: 10 = a fresh, ownable identity clearly differentiated from the peers; "
            "5 = competent but blends in; 0 = indistinguishable from the reference set.",
            "Do NOT reward novelty for its own sake — standing out must still be a competent, "
            "on-brief, usable page. Penalize both look-alike sameness and incoherent gimmickry.",
        ],
    )


def _build_prompt(brief: str, rubric: list[UXPrinciple], n_frames: int,
                  n_refs: int = 0, topic: str = "", slop_evidence_text: str = "") -> str:
    intro = (
        "You are an expert UI/UX design critic. You are shown the frames of a short screen "
        f"recording (~1 fps, {n_frames} frame(s), in time order) of a rendered web UI that an "
        "AI agent generated. The frames let you see both the static design and any animation."
    )
    if n_refs:
        topic_str = f" {topic}" if topic else ""
        intro += (
            f"\n\nBEFORE the candidate frames, you are shown {n_refs} REFERENCE image(s): "
            f"screenshots of real, currently-deployed{topic_str} products / competitors serving "
            "the same use case as the candidate. They are the competitive landscape. Use them "
            "ONLY for the originality dimension — judge every other principle on the CANDIDATE alone."
        )
    lines = [
        intro,
        "",
        "DESIGN BRIEF:",
        (brief.strip() or "(no brief provided — judge on general UI quality)"),
        "",
    ]
    if slop_evidence_text:
        lines += [slop_evidence_text, ""]
    lines += [
        "Score the UI on each of the following principles, 0-10 (0 = terrible, 10 = excellent). "
        "Work through the evaluation steps for each principle before settling on a number.",
        "",
    ]
    for i, p in enumerate(rubric, 1):
        tag = " [needs the frame sequence]" if p.needs_motion else ""
        lines.append(f"{i}. {p.name} (key: {p.key}){tag}")
        for step in p.evaluation_steps:
            lines.append(f"   - {step}")
    keys = ", ".join(f'"{p.key}"' for p in rubric)
    lines += [
        "",
        "Respond with ONLY a single JSON object, no prose before or after, in this exact shape:",
        "{",
        '  "scores": {',
        f"    // one entry per principle key: {keys}",
        '    "<key>": {"score": <0-10 number>, "reason": "<one short sentence>"}',
        "  },",
        '  "critique": "<two sentences: the single biggest strength and the single biggest weakness>",',
        '  "nameable_decisions": ["<concrete, actionable change the generating agent could make>", "..."]',
        "}",
    ]
    return "\n".join(lines)


def _encode(path: Path) -> tuple[str, str]:
    data = base64.b64encode(Path(path).read_bytes()).decode("ascii")
    ext = Path(path).suffix.lower().lstrip(".")
    media = "image/jpeg" if ext in ("jpg", "jpeg") else f"image/{ext or 'png'}"
    return media, data


def _call_vision_model(model: str, prompt: str, frames: list[Path],
                       references: list[Path] | None = None) -> str:
    references = references or []
    if _is_openai(model):
        return _call_openai(model, prompt, frames, references)
    return _call_anthropic(model, prompt, frames, references)


def _call_anthropic(model: str, prompt: str, frames: list[Path],
                    references: list[Path]) -> str:
    try:
        import anthropic
    except ImportError as exc:
        raise _JudgeUnavailable("anthropic SDK not installed (pip install anthropic)") from exc
    if not os.getenv("ANTHROPIC_API_KEY"):
        raise _JudgeUnavailable("ANTHROPIC_API_KEY not set")

    content: list[dict] = []
    for i, f in enumerate(references, 1):
        media, data = _encode(f)
        content.append({"type": "text", "text": f"REFERENCE {i} of {len(references)} "
                                                 "(a typical AI-generated site, for distinctiveness only):"})
        content.append({"type": "image",
                        "source": {"type": "base64", "media_type": media, "data": data}})
    for i, f in enumerate(frames, 1):
        media, data = _encode(f)
        content.append({"type": "text", "text": f"CANDIDATE frame {i} of {len(frames)}:"})
        content.append({"type": "image",
                        "source": {"type": "base64", "media_type": media, "data": data}})
    content.append({"type": "text", "text": prompt})

    client = anthropic.Anthropic()
    resp = client.messages.create(
        model=model,
        max_tokens=1500,
        messages=[{"role": "user", "content": content}],
    )
    return "".join(b.text for b in resp.content if getattr(b, "type", None) == "text")


def _call_openai(model: str, prompt: str, frames: list[Path],
                 references: list[Path]) -> str:
    try:
        from openai import OpenAI
    except ImportError as exc:
        raise _JudgeUnavailable("openai SDK not installed (pip install openai)") from exc
    if not os.getenv("OPENAI_API_KEY"):
        raise _JudgeUnavailable("OPENAI_API_KEY not set")

    content: list[dict] = [{"type": "text", "text": prompt}]
    for i, f in enumerate(references, 1):
        media, data = _encode(f)
        content.append({"type": "text", "text": f"REFERENCE {i} (typical AI-generated site):"})
        content.append({"type": "image_url", "image_url": {"url": f"data:{media};base64,{data}"}})
    for i, f in enumerate(frames, 1):
        media, data = _encode(f)
        content.append({"type": "text", "text": f"CANDIDATE frame {i}:"})
        content.append({"type": "image_url",
                        "image_url": {"url": f"data:{media};base64,{data}"}})

    client = OpenAI()
    resp = client.chat.completions.create(
        model=model,
        max_tokens=1500,
        messages=[{"role": "user", "content": content}],
    )
    return resp.choices[0].message.content or ""


def _parse_response(text: str) -> dict | None:
    """Extract the JSON object from the model response, tolerant of stray prose/fences."""
    for candidate in (text, _strip_fence(text), _first_brace_block(text)):
        if not candidate:
            continue
        try:
            data = json.loads(candidate)
        except (json.JSONDecodeError, TypeError):
            continue
        if isinstance(data, dict) and "scores" in data and isinstance(data["scores"], dict):
            return data
    return None


def _strip_fence(text: str) -> str | None:
    m = re.search(r"```(?:json)?\s*(\{.*?\})\s*```", text, re.DOTALL)
    return m.group(1) if m else None


def _first_brace_block(text: str) -> str | None:
    start = text.find("{")
    end = text.rfind("}")
    return text[start : end + 1] if 0 <= start < end else None


def _combine(rubric: list[UXPrinciple], scores: dict) -> tuple[float, dict]:
    """Weighted mean of per-principle scores (0-10), over principles the judge returned."""
    per_principle: dict[str, dict] = {}
    num = 0.0
    den = 0.0
    for p in rubric:
        entry = scores.get(p.key)
        val = entry.get("score") if isinstance(entry, dict) else entry
        reason = entry.get("reason", "") if isinstance(entry, dict) else ""
        try:
            val = float(val)
        except (TypeError, ValueError):
            continue
        val = max(0.0, min(10.0, val))
        per_principle[p.key] = {"score": val, "weight": p.weight, "reason": reason}
        num += p.weight * val
        den += p.weight
    combined = (num / den) if den > 0 else 0.0
    return round(combined, 2), per_principle
