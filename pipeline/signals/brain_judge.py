"""BrainJudgeSignal — judge a candidate by its *predicted brain activity*.

This is the payoff of the `pipeline.brain` toolchain. A classifier trained on
TRIBE-style predicted cortical responses for known-good (awwwards) vs AI-slop
(madewithlovable) sites is applied to the candidate: we encode its at-rest frame
into a predicted brain vector and ask the classifier how "good-website-like" that
brain activity is. The probability maps to a 0-10 score.

Config (`brain_judge:` block in autodesign.md):
    model:    path to the trained classifier (default data/brain/model.joblib)
    enabled:  set false to skip without removing it from `criteria`

Degrades gracefully: if the model file, scikit-learn, Pillow, or a usable frame is
missing, the signal is skipped (score=None) rather than failing the candidate —
matching the rest of the scaffold's "skip silently if deps absent" contract.
"""

from __future__ import annotations

from pathlib import Path

from pipeline.context import CandidateContext, SignalResult
from pipeline.registry import register_signal

DEFAULT_MODEL_PATH = "data/brain/model.joblib"


@register_signal
class BrainJudgeSignal:
    key = "brain_judge"

    def score(self, ctx: CandidateContext) -> SignalResult:
        cfg = ctx.config.get("brain_judge", {}) or {}
        if cfg.get("enabled") is False:
            return SignalResult(score=None, skipped="disabled in config")

        if not ctx.frames:
            return SignalResult(score=None, skipped="no captured frame to encode")
        frame = Path(ctx.frames[0])
        if not frame.exists():
            return SignalResult(score=None, skipped=f"frame missing: {frame}")

        model_path = Path(cfg.get("model", DEFAULT_MODEL_PATH))
        if not model_path.is_absolute():
            # Resolve relative to the project root (two levels up from this file:
            # pipeline/signals/brain_judge.py -> project root).
            model_path = Path(__file__).resolve().parents[2] / model_path
        if not model_path.exists():
            return SignalResult(
                score=None,
                skipped=(
                    f"no trained model at {model_path} — run pipeline.brain.scripts."
                    "harvest_uis / build_dataset / train first"
                ),
            )

        try:
            from pipeline.brain import tribe_encoder
            from pipeline.brain.classifier import GoodBadBrainClassifier
        except Exception as exc:  # noqa: BLE001 - missing deps (sklearn/Pillow)
            return SignalResult(score=None, skipped=f"brain deps unavailable: {exc}")

        try:
            clf = GoodBadBrainClassifier.load(model_path)
            vec = tribe_encoder.encode_image(frame)
            p_good = clf.proba_good(vec)
        except Exception as exc:  # noqa: BLE001
            return SignalResult(score=None, skipped=f"brain scoring failed: {exc}")

        score = round(10.0 * p_good, 3)
        return SignalResult(
            score=score,
            details={
                "p_good": round(p_good, 4),
                "encoder_backend": tribe_encoder.active_backend(),
                "model": str(model_path),
                "interpretation": (
                    "P(predicted brain activity resembles a known-good website "
                    "vs AI slop), mapped to 0-10."
                ),
            },
        )
