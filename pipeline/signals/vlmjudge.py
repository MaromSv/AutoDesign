"""JudgeSignal — VLM judge that scores the rendered design against the brief.

This is the highest-cost signal. It sends one or more captured frames plus the
brief to a vision-capable model (see `models.judge` in `autodesign.md`) and
parses a structured 0-10 score with a short critique.
"""

from __future__ import annotations

from pipeline.context import CandidateContext, SignalResult
from pipeline.registry import register_signal


@register_signal
class JudgeSignal:
    key = "vlm_judge"

    def score(self, ctx: CandidateContext) -> SignalResult:
        """TODO: implement VLM judge.

        1. Pick a representative frame from `ctx.frames` (or the at-rest frame).
        2. Build a prompt that contains `ctx.brief` and a small rubric tuned to
           the project (visual hierarchy, brief-adherence, taste, no-AI-slop).
        3. Call the model named in `ctx.config["models"]["judge"]`.
        4. Parse a 0-10 score and a one-sentence critique; put the critique in
           `details["critique"]`.
        """
        _ = ctx
        return SignalResult(score=None, skipped="not implemented")
