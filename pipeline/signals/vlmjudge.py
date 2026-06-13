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
_MAX_FRAMES = 5  # cap frames sent to bound cost/context; sampled evenly across the clip
# The judge returns a per-principle reason for every rubric principle (10+), a critique,
# a worst-first `issues` array, and `nameable_decisions`. That JSON is long — a healthy
# response is ~1.5-2k tokens, but when the model pretty-prints or adds reasoning it can run
# much larger, and any cut-off mid-object is unparseable. 8192 gives ~4x headroom over a
# typical response so truncation effectively never happens.
_MAX_OUTPUT_TOKENS = 8192
# vlm_judge is the dominant criterion (0.8). A transient bad response (truncation, a stray
# malformed-JSON emission) used to return score=None and silently tank the candidate, which
# is why runs needed manual rescoring. Retry the call a few times so intermittent failures
# self-heal before we give up. The model's verbosity varies run-to-run, so a fresh call
# usually parses cleanly.
_MAX_JUDGE_ATTEMPTS = 3


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

        # Call the judge with bounded retries. vlm_judge is the dominant criterion, so an
        # intermittent truncation / malformed-JSON / transport blip must not silently return
        # null and tank the candidate — retry before giving up. Config/credential problems
        # (_JudgeUnavailable) are NOT retryable, so we bail immediately on those.
        raw = ""
        parsed = None
        last_reason = "could not parse judge response"
        for attempt in range(1, _MAX_JUDGE_ATTEMPTS + 1):
            try:
                raw = _call_vision_model(model, prompt, frames, references)
            except _JudgeUnavailable as exc:
                return SignalResult(score=None, skipped=str(exc))
            except _JudgeTruncated as exc:
                last_reason = str(exc); raw = ""; continue  # retry: a fresh response usually fits
            except Exception as exc:  # noqa: BLE001 - transport/model error -> retry, then skip
                last_reason = f"judge model error: {exc}"; raw = ""; continue
            parsed = _parse_response(raw)
            if parsed is not None:
                break
            last_reason = "could not parse judge response"

        if parsed is None:
            return SignalResult(
                score=None,
                skipped=f"{last_reason} (after {_MAX_JUDGE_ATTEMPTS} attempts)",
                details={"raw_response": raw[:2000], "attempts": _MAX_JUDGE_ATTEMPTS},
            )

        score10, per_principle = _combine(rubric, parsed["scores"])
        # Re-shape per_principle into the (subscores / weights / explanations)
        # contract `serve.py::_flat_criteria` reads from `details`. Result:
        # every UX principle becomes its own row in the dashboard's criterion
        # strip alongside the saliency subscores — one flat list, no
        # "from saliency" / "from vlm_judge" grouping.
        subscores = {k: round(v["score"] / 10.0, 4) for k, v in per_principle.items()}
        weights_out = {k: v["weight"] for k, v in per_principle.items()}
        explanations = {k: v.get("reason", "") for k, v in per_principle.items()}
        return SignalResult(
            score=score10,
            details={
                "model": model,
                "n_frames": len(frames),
                "n_references": len(references),
                "ai_pitfalls_evidence": evidence,
                "critique": parsed.get("critique", ""),
                # Decomposed shape the dashboard reads:
                "subscores": subscores,
                "weights": weights_out,
                "explanations": explanations,
                # Nested shape kept for older clients / debug:
                "per_principle": per_principle,
                "issues": _clean_issues(parsed.get("issues")),
                "explorations": _clean_explorations(parsed.get("explorations")),
                "nameable_decisions": parsed.get("nameable_decisions", []),
            },
        )


# --------------------------------------------------------------------------- helpers
class _JudgeUnavailable(RuntimeError):
    """Raised when the judge cannot run for an expected reason (no SDK / no key).
    NOT retryable — retrying won't conjure a key."""


class _JudgeTruncated(RuntimeError):
    """Raised when the model stopped on its token limit, so the JSON is cut off.
    Retryable — a fresh, less verbose response usually fits."""


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
        key="originality", name="Originality vs. Similar Products", weight=3.0,
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
        "Then PINPOINT the concrete problems. Your feedback is read by the agent that will edit "
        "this exact page in the next round, so every issue must be locatable and fixable WITHOUT "
        "guessing. For each issue:",
        "  - `where`: name the specific on-screen element or region — quote its visible text if it "
        "    has any (e.g. the \"Play Free Demo\" button, the hero headline, the top-right starfield). "
        "    If it moves/animates, say at which point (entrance / settled state).",
        "  - `problem`: what is actually wrong or broken, and QUOTE THE OBSERVED STATE — describe "
        "    what you literally see (the approximate current size, color, position, weight, or "
        "    spacing) so the fix has a baseline (overlapping text, clipped element, illegible "
        "    low-contrast label, CTA sitting outside the focal area, a second element competing "
        "    for attention, layout breaking at this viewport, etc.). Do NOT write vague notes "
        "    like 'improve hierarchy' or 'needs polish'.",
        "  - `principle`: the rubric key it hurts most.",
        "  - `fix`: the single concrete change, phrased as observed → target — which element, "
        "    which CSS property, and a specific value, not a direction (e.g. 'raise the CTA label "
        "    from ~18px to ~36px / 600 weight and give it a solid #1b5e20 fill', NOT 'make the CTA "
        "    stronger'). The next agent must be able to apply it without guessing a number.",
        "  - `severity`: \"high\" (broken / blocks the brief), \"medium\", or \"low\" (polish).",
        "List the issues worst-first. Keep `issues` to genuinely broken or weak things "
        "(illegible text, clipped/overlapping elements, a CTA outside the focal area). If the "
        "design is visually clean, return an empty `issues` list rather than inventing problems. "
        "In each principle's `reason`, when the score is below 7, name the specific offending "
        "element — do not give a generic justification.",
        "",
        "MOST IMPORTANT — propose `explorations`. Beyond fixing what's wrong, your primary job is "
        "to push this design somewhere more interesting. Give 2-4 bold, specific CREATIVE moves "
        "that would make the page more striking, memorable, and distinctive — the kind of ideas a "
        "great art director would be excited to try, NOT safe tweaks. Each must be grounded in "
        "something the current design LACKS or plays too safe on (say what's missing, then the "
        "idea). Think art direction: a signature visual motif or illustration, an unexpected "
        "layout/composition, a more confident hero treatment, a distinctive type or color move, a "
        "tasteful entrance or hover interaction, texture/depth, an editorial detail. Be concrete "
        "enough to build (name the element, the treatment, rough sizes/colors), but optimize for "
        "DISTINCTIVENESS and visual delight (creativity / originality / visual appeal), not "
        "correctness. Do NOT restate the issues here — explorations are upside, not fixes.",
        "",
        "Respond with ONLY a single JSON object, no prose before or after, in this exact shape:",
        "{",
        '  "scores": {',
        f"    // one entry per principle key: {keys}",
        '    "<key>": {"score": <0-10 number>, "reason": "<one short sentence; if <7, name the offending element>"}',
        "  },",
        '  "critique": "<two sentences: the single biggest strength and the single biggest weakness>",',
        '  "explorations": [',
        '    {"lacks": "<what the current design is missing or playing too safe on>",',
        '     "idea": "<a specific, creative design move to try — art direction, concrete enough to build>",',
        '     "principle": "<rubric key it would lift, e.g. creativity / originality / visual_hierarchy>"}',
        "  ],",
        '  "issues": [',
        '    {"where": "<specific element/region, quote its text>", "problem": "<what is observably wrong>",',
        '     "principle": "<rubric key>", "fix": "<the one concrete change to make>", "severity": "high|medium|low"}',
        "  ],",
        '  "nameable_decisions": ["<concrete, located, actionable change the generating agent could make>", "..."]',
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
    # Pick up AutoDesign/.env so the key works from any entry point (e.g. a bare
    # `python -m pipeline.benchmark` that doesn't load .env itself), mirroring _nemotron.
    from pipeline.envfile import ensure_loaded
    ensure_loaded()
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
        max_tokens=_MAX_OUTPUT_TOKENS,
        messages=[{"role": "user", "content": content}],
    )
    text = "".join(b.text for b in resp.content if getattr(b, "type", None) == "text")
    if getattr(resp, "stop_reason", None) == "max_tokens":
        raise _JudgeTruncated(f"judge response hit max_tokens ({_MAX_OUTPUT_TOKENS})")
    return text


def _call_openai(model: str, prompt: str, frames: list[Path],
                 references: list[Path]) -> str:
    from pipeline.envfile import ensure_loaded
    ensure_loaded()
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
        max_tokens=_MAX_OUTPUT_TOKENS,
        messages=[{"role": "user", "content": content}],
    )
    choice = resp.choices[0]
    if getattr(choice, "finish_reason", None) == "length":
        raise _JudgeTruncated(f"judge response hit max_tokens ({_MAX_OUTPUT_TOKENS})")
    return choice.message.content or ""


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


_SEVERITY_RANK = {"high": 0, "medium": 1, "low": 2}


def _clean_issues(issues) -> list[dict]:
    """Normalize the judge's `issues` array: keep well-formed entries, sort worst-first.

    Each kept issue carries the located, actionable feedback the next-round generator
    needs (where / problem / fix). Tolerant of a missing or malformed field — a dropped
    issue is better than a crash in the loop.
    """
    if not isinstance(issues, list):
        return []
    cleaned: list[dict] = []
    for it in issues:
        if not isinstance(it, dict):
            continue
        where = str(it.get("where", "")).strip()
        problem = str(it.get("problem", "")).strip()
        if not where and not problem:
            continue  # an issue with neither a location nor a description is useless
        sev = str(it.get("severity", "medium")).strip().lower()
        if sev not in _SEVERITY_RANK:
            sev = "medium"
        cleaned.append({
            "where": where,
            "problem": problem,
            "principle": str(it.get("principle", "")).strip(),
            "fix": str(it.get("fix", "")).strip(),
            "severity": sev,
        })
    cleaned.sort(key=lambda i: _SEVERITY_RANK[i["severity"]])
    return cleaned


def _clean_explorations(explorations) -> list[dict]:
    """Normalize the judge's `explorations` array: creative design moves to try next round.

    Each entry is {lacks, idea, principle} — what the design is missing and a specific,
    buildable creative direction to address it. Tolerant of malformed/partial entries;
    an entry needs at least an `idea` to be useful.
    """
    if not isinstance(explorations, list):
        return []
    cleaned: list[dict] = []
    for it in explorations:
        if not isinstance(it, dict):
            continue
        idea = str(it.get("idea", "")).strip()
        if not idea:
            continue
        cleaned.append({
            "lacks": str(it.get("lacks", "")).strip(),
            "idea": idea,
            "principle": str(it.get("principle", "")).strip(),
        })
    return cleaned


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
