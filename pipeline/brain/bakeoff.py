"""Model bake-off: cross-validate several classifier families on the brain vectors.

`why logistic?` deserves an empirical answer, not an assertion. This compares a
panel of classifiers under identical stratified k-fold CV and reports accuracy +
AUC for each, so the choice is evidence-based. The winner (best CV AUC) can be
refit on all data and wrapped as a `GoodBadBrainClassifier`.

The panel spans linear (LR L2/L1, linear SVM), kernel (RBF SVM), instance-based
(kNN), trees/ensembles (random forest, gradient boosting), a small MLP, and a
PCA->LR pipeline (dimensionality reduction before a linear head — relevant because
the parcel vector is 1000-dim but the harvested set is small).
"""

from __future__ import annotations

from dataclasses import dataclass

import numpy as np


@dataclass
class ModelScore:
    name: str
    cv_accuracy: float
    cv_auc: float

    def as_dict(self) -> dict:
        return {"name": self.name, "cv_accuracy": self.cv_accuracy, "cv_auc": self.cv_auc}


def _panel(n_features: int, seed: int) -> dict:
    """Name -> sklearn estimator factory (callable, so each CV gets a fresh model)."""
    from sklearn.decomposition import PCA
    from sklearn.ensemble import (
        GradientBoostingClassifier,
        HistGradientBoostingClassifier,
        RandomForestClassifier,
    )
    from sklearn.linear_model import LogisticRegression
    from sklearn.neighbors import KNeighborsClassifier
    from sklearn.neural_network import MLPClassifier
    from sklearn.pipeline import make_pipeline
    from sklearn.preprocessing import StandardScaler
    from sklearn.svm import SVC

    sc = StandardScaler

    def lr(C, l1_ratio=0.0):
        # sklearn >=1.9: l1_ratio replaces penalty (0=L2, 1=L1). saga handles both.
        return make_pipeline(
            sc(), LogisticRegression(C=C, l1_ratio=l1_ratio, solver="saga",
                                     class_weight="balanced", max_iter=8000))

    pca_k = min(30, max(2, n_features // 2))
    panel = {
        "logreg_l2_C0.005": lambda: lr(0.005),
        "logreg_l2_C0.05": lambda: lr(0.05),
        "logreg_l1_C0.1": lambda: lr(0.1, l1_ratio=1.0),
        "linear_svm": lambda: make_pipeline(
            sc(), SVC(kernel="linear", C=0.1, probability=True,
                      class_weight="balanced", random_state=seed)),
        "rbf_svm": lambda: make_pipeline(
            sc(), SVC(kernel="rbf", C=1.0, gamma="scale", probability=True,
                      class_weight="balanced", random_state=seed)),
        "knn_15": lambda: make_pipeline(sc(), KNeighborsClassifier(n_neighbors=15)),
        "random_forest": lambda: RandomForestClassifier(
            n_estimators=400, max_depth=None, class_weight="balanced", random_state=seed),
        "gradient_boost": lambda: GradientBoostingClassifier(random_state=seed),
        "hist_gradient_boost": lambda: HistGradientBoostingClassifier(
            max_iter=400, l2_regularization=1.0, random_state=seed),
        "mlp": lambda: make_pipeline(
            sc(), MLPClassifier(hidden_layer_sizes=(64,), alpha=1e-2, max_iter=2000,
                                random_state=seed)),
        f"pca{pca_k}_logreg": lambda: make_pipeline(
            sc(), PCA(n_components=pca_k, random_state=seed),
            LogisticRegression(C=1.0, class_weight="balanced", max_iter=5000)),
    }

    # XGBoost — optional (needs the `xgboost` wheel + an OpenMP runtime). Added only
    # if importable so the panel degrades gracefully on machines without it.
    try:
        from xgboost import XGBClassifier

        panel["xgboost"] = lambda: XGBClassifier(
            n_estimators=400, max_depth=4, learning_rate=0.05, subsample=0.8,
            colsample_bytree=0.8, reg_lambda=2.0, eval_metric="logloss",
            tree_method="hist", random_state=seed)
    except Exception:  # noqa: BLE001 - xgboost missing / libomp absent -> skip it
        pass

    return panel


def compare(X: np.ndarray, y: np.ndarray, seed: int = 0) -> list[ModelScore]:
    """Cross-validate every model in the panel; return scores sorted by CV AUC desc."""
    from sklearn.metrics import roc_auc_score
    from sklearn.model_selection import StratifiedKFold, cross_val_predict

    X = np.asarray(X, dtype=np.float64)
    y = np.asarray(y, dtype=int)
    n_splits = min(5, int((y == 1).sum()), int((y == 0).sum()))
    if n_splits < 2:
        raise ValueError("not enough samples per class to cross-validate")
    skf = StratifiedKFold(n_splits=n_splits, shuffle=True, random_state=seed)

    out: list[ModelScore] = []
    for name, factory in _panel(X.shape[1], seed).items():
        try:
            proba = cross_val_predict(factory(), X, y, cv=skf, method="predict_proba")[:, 1]
        except Exception as exc:  # noqa: BLE001 - skip a model that can't fit, don't abort
            out.append(ModelScore(name=f"{name} (failed: {exc})",
                                  cv_accuracy=float("nan"), cv_auc=float("nan")))
            continue
        acc = float(((proba >= 0.5).astype(int) == y).mean())
        try:
            auc = float(roc_auc_score(y, proba))
        except Exception:  # noqa: BLE001
            auc = float("nan")
        out.append(ModelScore(name=name, cv_accuracy=acc, cv_auc=auc))

    out.sort(key=lambda s: (-1.0 if s.cv_auc != s.cv_auc else s.cv_auc, s.cv_accuracy),
             reverse=True)
    return out


def best_estimator(X: np.ndarray, y: np.ndarray, seed: int = 0):
    """Return (winning_name, fitted_estimator, all_scores) — winner by CV AUC, refit on all X."""
    scores = compare(X, y, seed=seed)
    winner = next((s for s in scores if s.cv_auc == s.cv_auc), scores[0])
    factory = _panel(np.asarray(X).shape[1], seed)[winner.name]
    model = factory().fit(np.asarray(X, dtype=np.float64), np.asarray(y, dtype=int))
    return winner.name, model, scores
