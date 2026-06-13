"""GoodBadBrainClassifier — judge a predicted brain vector as good vs AI-slop.

Trains on TRIBE-style cortical parcel vectors (see `tribe_encoder.encode_image`)
labeled good (awwwards) vs bad (madewithlovable slop). At inference it returns a
probability that a UI's *predicted brain activity* looks like that of a known-good
site, plus a 0-10 score the AutoDesign loop can combine with other signals.

Model: standardize -> L2-regularized logistic regression. The parcel vectors are
high-dimensional (1000) relative to a small harvested dataset, so regularization
and cross-validated reporting matter — `train` returns CV accuracy/AUC so you can
see whether the brain signal actually separates the two classes before trusting it.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path

import numpy as np

GOOD = 1
BAD = 0


@dataclass
class TrainReport:
    n_good: int
    n_bad: int
    cv_accuracy: float
    cv_auc: float
    train_accuracy: float
    notes: list[str] = field(default_factory=list)

    def as_dict(self) -> dict:
        return {
            "n_good": self.n_good,
            "n_bad": self.n_bad,
            "cv_accuracy": self.cv_accuracy,
            "cv_auc": self.cv_auc,
            "train_accuracy": self.train_accuracy,
            "notes": self.notes,
        }


class GoodBadBrainClassifier:
    """Wraps a scikit-learn pipeline over brain vectors. Train, save, load, score."""

    def __init__(self, model=None, meta: dict | None = None):
        self._model = model
        self.meta = meta or {}

    # ---- training ----
    # L2 strengths swept by cross-validation when `C` is left at "auto". The parcel
    # vectors are high-dim relative to a small harvested set, so the model is usually
    # under heavy regularization — the grid leans small.
    C_GRID = (0.005, 0.01, 0.05, 0.1, 0.5, 1.0)

    @classmethod
    def train(
        cls,
        X: np.ndarray,
        y: np.ndarray,
        C: float | str = "auto",
        seed: int = 0,
    ) -> tuple["GoodBadBrainClassifier", TrainReport]:
        """Fit on parcel vectors `X` (n, n_parcels) with labels `y` (GOOD/BAD).

        `C="auto"` (default) cross-validates the `C_GRID` and keeps the strength with
        the best CV AUC — the honest choice given small, high-dim data. Pass a float
        to pin it.
        """
        from sklearn.linear_model import LogisticRegression
        from sklearn.model_selection import StratifiedKFold, cross_val_predict
        from sklearn.pipeline import make_pipeline
        from sklearn.preprocessing import StandardScaler

        X = np.asarray(X, dtype=np.float64)
        y = np.asarray(y, dtype=int)
        n_good = int((y == GOOD).sum())
        n_bad = int((y == BAD).sum())
        notes: list[str] = []

        def _pipe(c):
            return make_pipeline(
                StandardScaler(),
                LogisticRegression(C=c, max_iter=2000, class_weight="balanced"),
            )

        def _cv(c, skf):
            proba = cross_val_predict(_pipe(c), X, y, cv=skf, method="predict_proba")[:, 1]
            acc = float(((proba >= 0.5).astype(int) == y).mean())
            try:
                from sklearn.metrics import roc_auc_score

                auc = float(roc_auc_score(y, proba))
            except Exception:  # noqa: BLE001 - single-class fold etc.
                auc = float("nan")
            return acc, auc

        # Cross-validated estimates (honest, given small data). Fall back gracefully
        # when there are too few samples per class to fold.
        n_splits = min(5, n_good, n_bad)
        cv_acc = cv_auc = float("nan")
        if n_splits >= 2:
            skf = StratifiedKFold(n_splits=n_splits, shuffle=True, random_state=seed)
            grid = list(cls.C_GRID) if C == "auto" else [float(C)]
            scored = [(c, *_cv(c, skf)) for c in grid]
            # pick best by AUC, tie-break on accuracy; NaN AUC sorts last.
            best = max(scored, key=lambda t: (-1.0 if t[2] != t[2] else t[2], t[1]))
            C, cv_acc, cv_auc = best
            if len(grid) > 1:
                notes.append(
                    "C selected by CV AUC over "
                    + ", ".join(f"{c}:{auc:.3f}" for c, _, auc in scored)
                )
        else:
            C = 0.05 if C == "auto" else float(C)
            notes.append(
                f"too few samples to cross-validate (n_good={n_good}, n_bad={n_bad}); "
                "CV metrics are NaN — harvest more sites before trusting this model."
            )

        model = _pipe(C).fit(X, y)
        train_acc = float((model.predict(X) == y).mean())
        meta = {
            "n_parcels": int(X.shape[1]),
            "C": C,
            "n_good": n_good,
            "n_bad": n_bad,
        }
        report = TrainReport(
            n_good=n_good,
            n_bad=n_bad,
            cv_accuracy=cv_acc,
            cv_auc=cv_auc,
            train_accuracy=train_acc,
            notes=notes,
        )
        return cls(model=model, meta=meta), report

    # ---- inference ----
    def proba_good(self, x: np.ndarray) -> float:
        """P(good-website-like) for a single brain vector."""
        if self._model is None:
            raise RuntimeError("classifier is not trained/loaded")
        x = np.asarray(x, dtype=np.float64).reshape(1, -1)
        return float(self._model.predict_proba(x)[0, 1])

    def score_0_10(self, x: np.ndarray) -> float:
        """Map P(good) onto the loop's 0-10 scale."""
        return round(10.0 * self.proba_good(x), 3)

    # ---- persistence ----
    def save(self, path: str | Path) -> Path:
        import joblib

        path = Path(path)
        path.parent.mkdir(parents=True, exist_ok=True)
        joblib.dump(self._model, path)
        path.with_suffix(".meta.json").write_text(json.dumps(self.meta, indent=2))
        return path

    @classmethod
    def load(cls, path: str | Path) -> "GoodBadBrainClassifier":
        import joblib

        path = Path(path)
        model = joblib.load(path)
        meta_path = path.with_suffix(".meta.json")
        meta = json.loads(meta_path.read_text()) if meta_path.exists() else {}
        return cls(model=model, meta=meta)
