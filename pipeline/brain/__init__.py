"""brain — predict cortical brain activity for a UI and judge it good-vs-bad.

The pipeline mirrors the `affective-decode` repo's stance on Meta's TRIBE model:
TRIBE is an *encoding* model (stimulus -> predicted cortical fMRI). Here we

  1. turn a rendered UI (a screenshot) into a TRIBE-style predicted cortical
     parcel vector  (`tribe_encoder.encode_image`), then
  2. classify that brain vector as "good-website-like" vs "AI-slop-like" using a
     model trained on awwwards (good) vs madewithlovable (bad)
     (`classifier.GoodBadBrainClassifier`).

The trained classifier is consumed by the `brain_judge` signal so the AutoDesign
loop can score generated UIs by the brain activity they are *predicted* to evoke.
"""

from pipeline.brain import classifier, tribe_encoder

__all__ = ["classifier", "tribe_encoder"]
