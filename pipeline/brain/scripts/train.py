"""Train the good-vs-bad brain classifier from a built dataset.

    python -m pipeline.brain.scripts.train --root data/brain --out data/brain/model.joblib

Prints cross-validated accuracy/AUC so you can see whether predicted brain
activity actually separates awwwards-good from AI-slop before the loop trusts it.
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path

import numpy as np

from pipeline.brain.classifier import GoodBadBrainClassifier


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--root", default="data/brain")
    ap.add_argument("--dataset", default="brain_dataset.npz",
                    help="dataset npz under --root (e.g. brain_dataset_tribe.npz)")
    ap.add_argument("--out", default=None, help="model path (default <root>/model.joblib)")
    ap.add_argument("--C", default="auto", help='inverse L2 strength, or "auto" to CV-select')
    ap.add_argument("--compare", action="store_true",
                    help="bake off a panel of classifier families and keep the best CV-AUC one")
    args = ap.parse_args()

    root = Path(args.root)
    npz = np.load(root / args.dataset, allow_pickle=True)
    X, y = npz["X"], npz["y"]
    backend = str(npz["backend"]) if "backend" in npz else "unknown"
    out = Path(args.out) if args.out else root / "model.joblib"

    if args.compare:
        from pipeline.brain.bakeoff import best_estimator

        name, model, scores = best_estimator(X, y)
        print(f"{'model':22} {'cv_acc':>8} {'cv_auc':>8}")
        for s in scores:
            print(f"{s.name:22} {s.cv_accuracy:8.3f} {s.cv_auc:8.3f}")
        print(f"\nwinner (by CV AUC): {name}")
        clf = GoodBadBrainClassifier(
            model=model,
            meta={"n_parcels": int(X.shape[1]), "model": name,
                  "n_good": int((y == 1).sum()), "n_bad": int((y == 0).sum()),
                  "selected_by": "bakeoff_cv_auc"},
        )
        clf.save(out)
        print(f"\nencoder backend used for dataset: {backend}")
        print(f"saved model -> {out}")
        return

    C = args.C if args.C == "auto" else float(args.C)
    clf, report = GoodBadBrainClassifier.train(X, y, C=C)
    clf.save(out)

    print(json.dumps(report.as_dict(), indent=2))
    print(f"\nencoder backend used for dataset: {backend}")
    print(f"saved model -> {out}")
    if backend == "perceptual-fallback":
        print(
            "\nNOTE: dataset was built with the perceptual fallback encoder, not real "
            "TRIBE-v2. Wire TRIBE_ENDPOINT/TRIBE_WEIGHTS and rebuild for true brain "
            "predictions; the classifier API is unchanged."
        )


if __name__ == "__main__":
    main()
