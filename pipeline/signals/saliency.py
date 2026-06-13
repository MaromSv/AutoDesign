"""SaliencySignal — does the rendered design pull attention toward the focal area?

Computes a saliency map over the at-rest frame and measures how much of the
predicted attention mass lands inside the focal bbox declared in
`config["saliency"]["focal_bbox"]`.
"""

from __future__ import annotations

from pipeline.context import CandidateContext, SignalResult
from pipeline.registry import register_signal


@register_signal
class SaliencySignal:
    key = "saliency"

    def score(self, ctx: CandidateContext) -> SignalResult:
        """TODO: implement saliency scoring.

        1. Load the at-rest frame from `ctx.frames[0]`.
        2. Compute a saliency map (opencv-contrib StaticSaliencyFineGrained, or
           a learned model later).
        3. Read `ctx.config["saliency"]["focal_bbox"]` and integrate the map
           inside that bbox normalized by the total map mass.
        4. Map the [0, 1] fraction onto a 0-10 score (TODO: pick a calibrated
           transform). Save the overlay as `saliency.png` in `ctx.candidate_dir`
           and reference it in `details["overlay"]`.
        """
        _ = ctx
        return SignalResult(score=None, skipped="not implemented")
