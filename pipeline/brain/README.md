# brain — judge UIs by their *predicted brain activity*

A new AutoDesign criteria (`brain_judge`) built around Meta's **TRIBE-v2** brain
encoder. The idea, end to end:

> Predict the brain activity a UI evokes, then judge whether that activity looks
> like a **known-good** website (awwwards winner) or **AI slop** (madewithlovable).
> Use that judgement to score generated UIs.

```
 UI screenshot
      │  tribe_encoder.encode_image()      ← TRIBE-v2 adapter (real hook + perceptual fallback)
      ▼
 predicted cortical parcel vector  (Schaefer-1000)
      │  classifier.GoodBadBrainClassifier
      ▼
 P(good-website-like brain activity)  →  0–10 score  →  brain_judge signal
```

This mirrors the sibling `affective-decode` repo's stance: TRIBE is an *encoding*
model (stimulus → predicted fMRI). We consume its output. `affective-decode` turns
that output into a valence score; here we turn it into a learned good-vs-slop
judgement specific to web UIs.

## Build the classifier (once)

```bash
# 1. harvest URL lists: good = awwwards winners, bad = madewithlovable slop
python -m pipeline.brain.scripts.harvest_uis  --root data/brain --limit 60

# 2. render each site + encode to a brain vector  (needs Playwright + chromium)
python -m pipeline.brain.scripts.build_dataset --root data/brain

# 3. train + report cross-validated good/bad separation
python -m pipeline.brain.scripts.train        --root data/brain
```

This writes `data/brain/model.joblib`. The `brain_judge` signal picks it up
automatically (path configurable in `autodesign.md`).

### Which classifier? (run the bake-off)

`train` defaults to L2 **logistic regression** with the `C` chosen by cross-validated
AUC — a defensible default when the harvested set is small relative to the 1000-dim
parcel vector. To choose empirically instead of by default, run the bake-off:

```bash
python -m pipeline.brain.scripts.train --root data/brain --compare
```

It cross-validates a panel — logistic regression (L2/L1), linear & RBF SVM, kNN,
random forest, gradient boosting, HistGradientBoosting, **XGBoost** (if installed),
a small MLP, and PCA→logreg — prints an accuracy/AUC table, and keeps the best
CV-AUC model. XGBoost needs the `xgboost` wheel plus an OpenMP runtime
(`brew install libomp` on macOS); the panel skips it cleanly if absent.

At small n the families land in a noisy near-tie and boosting underperforms (it is
data-hungry) — more reason the honest lever is **more harvested sites + a better
encoder**, not a fancier classifier.

## Use it in the loop

`autodesign.md` already wires it in:

```yaml
criteria:
  saliency:    0.5
  vlm_judge:   0.3
  brain_judge: 0.2

brain_judge:
  enabled: true
  model:   data/brain/model.joblib
```

Every candidate's at-rest frame is encoded to a brain vector and scored. If the
model file isn't built yet (or sklearn/Pillow are missing), the signal skips
cleanly instead of failing the candidate.

## Real TRIBE-v2 vs the fallback

`tribe_encoder.encode_image` picks a backend automatically:

| Backend | When | What it does |
|---|---|---|
| `tribe-endpoint` | `TRIBE_ENDPOINT` env set | POST stimulus to your TRIBE-v2 service *(stub — implement `_encode_real_endpoint`)* |
| `tribe-weights` | `TRIBE_WEIGHTS` env set | local TRIBE-v2 forward pass *(stub — implement `_encode_real_weights`)* |
| `perceptual-fallback` | default | **deterministic stand-in**: lifts perceptual stats (clutter, colorfulness, contrast, whitespace, symmetry, palette entropy) into parcel space via a fixed seeded projection |

The fallback is **not** a brain simulation — it is an honest placeholder so the
whole good-vs-slop loop runs today. Swapping in real TRIBE-v2 means filling one
function; the classifier, signal, and config are unchanged. Rebuild the dataset
after switching backends (the parcel vectors change), then retrain.

`build_dataset` records which backend produced a dataset, and `train` prints it,
so a fallback-trained model can never be mistaken for a TRIBE-trained one.
