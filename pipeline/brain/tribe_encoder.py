"""TRIBE-v2 encoder adapter: a rendered UI -> predicted cortical brain activity.

Meta's TRIBE (Trimodal Brain Encoder, v2) is an *encoding* model: it maps a
stimulus to predicted cortical BOLD over a high-resolution surface, which is then
summarized into a Schaefer parcellation (1000 parcels). We do **not** ship those
weights here. This module is the seam where a real TRIBE-v2 forward pass plugs in:

    encode_image(png) -> np.ndarray of shape (N_PARCELS,)   # predicted cortical response

Two backends, picked automatically:

  * **real** — if `TRIBE_ENDPOINT` (an HTTP service) or `TRIBE_WEIGHTS` (a local
    checkpoint dir) is configured, we delegate to it. These hooks are stubs that
    raise `NotImplementedError` with a clear message until wired to your TRIBE-v2
    deployment — fill in `_encode_real_*`.

  * **perceptual fallback** (default) — a deterministic stand-in that derives a
    stable, reproducible parcel vector from perceptual image statistics (clutter,
    colorfulness, contrast, whitespace, symmetry, palette entropy). It is NOT a
    brain simulation; it is a placeholder that (a) lets the whole good-vs-bad
    pipeline run end-to-end today and (b) carries enough signal that the
    downstream classifier can actually separate polished sites from AI slop.
    Swap in real TRIBE-v2 and everything downstream is unchanged.

The parcel vector is built as `W @ features` with `W` a fixed, seeded projection,
so the fallback is a linear lift of the perceptual features into parcel space —
deterministic per image and honest about what it encodes.
"""

from __future__ import annotations

import os
from pathlib import Path

import numpy as np

# Match the Schaefer-1000 parcellation the affective-decode pipeline consumes.
N_PARCELS = 1000
# Fixed seed for the perceptual->parcel projection. Changing it invalidates any
# classifier trained on the old projection, so keep it stable.
_PROJECTION_SEED = 20240613


# --------------------------------------------------------------------------- #
# Perceptual feature extraction
# --------------------------------------------------------------------------- #
def _load_rgb(path: str | Path, max_side: int = 512) -> np.ndarray:
    """Load an image as a float RGB array in [0,1], downscaled for speed."""
    from PIL import Image

    img = Image.open(path).convert("RGB")
    if max(img.size) > max_side:
        scale = max_side / max(img.size)
        img = img.resize((max(1, int(img.width * scale)), max(1, int(img.height * scale))))
    return np.asarray(img, dtype=np.float32) / 255.0


def _rgb_to_hsv(rgb: np.ndarray) -> tuple[np.ndarray, np.ndarray, np.ndarray]:
    """Vectorized RGB->HSV (each channel in [0,1]). rgb: (H,W,3)."""
    r, g, b = rgb[..., 0], rgb[..., 1], rgb[..., 2]
    mx = rgb.max(axis=-1)
    mn = rgb.min(axis=-1)
    diff = mx - mn
    # hue
    hue = np.zeros_like(mx)
    mask = diff > 1e-6
    rc = np.where(mask, (mx - r) / np.where(mask, diff, 1), 0)
    gc = np.where(mask, (mx - g) / np.where(mask, diff, 1), 0)
    bc = np.where(mask, (mx - b) / np.where(mask, diff, 1), 0)
    hue = np.where(mx == r, bc - gc, np.where(mx == g, 2.0 + rc - bc, 4.0 + gc - rc))
    hue = (hue / 6.0) % 1.0
    hue = np.where(mask, hue, 0.0)
    sat = np.where(mx > 1e-6, diff / np.where(mx > 1e-6, mx, 1), 0.0)
    val = mx
    return hue, sat, val


def perceptual_features(rgb: np.ndarray) -> dict[str, float]:
    """Derive interpretable perceptual statistics from an RGB image in [0,1].

    These are the features the fallback encoder lifts into parcel space. They are
    chosen to track qualities that separate considered design from AI slop:
    clutter, colour discipline, contrast, breathing room, and layout regularity.
    """
    h, w, _ = rgb.shape
    gray = rgb @ np.array([0.299, 0.587, 0.114], dtype=np.float32)
    hue, sat, val = _rgb_to_hsv(rgb)

    # Edge density (visual clutter): mean gradient magnitude.
    gy, gx = np.gradient(gray)
    edges = np.sqrt(gx * gx + gy * gy)
    edge_density = float(edges.mean())

    # Contrast: RMS luminance contrast.
    contrast = float(gray.std())

    # Colorfulness (Hasler & Süsstrunk 2003).
    r, g, b = rgb[..., 0], rgb[..., 1], rgb[..., 2]
    rg = r - g
    yb = 0.5 * (r + g) - b
    colorfulness = float(
        np.sqrt(rg.std() ** 2 + yb.std() ** 2) + 0.3 * np.sqrt(rg.mean() ** 2 + yb.mean() ** 2)
    )

    # Saturation discipline.
    sat_mean = float(sat.mean())
    sat_std = float(sat.std())

    # Whitespace / breathing room: fraction of near-white, low-saturation pixels.
    whitespace = float(((val > 0.92) & (sat < 0.08)).mean())

    # Palette entropy: how many distinct hues carry mass (slop -> generic gradients
    # OR chaotic rainbows; both read in the hue histogram).
    hue_hist, _ = np.histogram(hue[sat > 0.15], bins=18, range=(0, 1))
    p = hue_hist / max(hue_hist.sum(), 1)
    nz = p[p > 0]
    hue_entropy = float(-(nz * np.log2(nz)).sum()) if nz.size else 0.0

    # Vertical / horizontal symmetry (layout regularity).
    h_sym = 1.0 - float(np.abs(gray - gray[:, ::-1]).mean())
    v_sym = 1.0 - float(np.abs(gray - gray[::-1, :]).mean())

    # Luminance of the upper region vs whole (good hero sections tend to have a
    # clear figure/ground separation above the fold).
    top = gray[: h // 3]
    top_contrast = float(top.std())

    return {
        "edge_density": edge_density,
        "contrast": contrast,
        "colorfulness": colorfulness,
        "sat_mean": sat_mean,
        "sat_std": sat_std,
        "whitespace": whitespace,
        "hue_entropy": hue_entropy,
        "h_symmetry": h_sym,
        "v_symmetry": v_sym,
        "top_contrast": top_contrast,
    }


# Stable feature ordering for the projection.
FEATURE_KEYS = (
    "edge_density",
    "contrast",
    "colorfulness",
    "sat_mean",
    "sat_std",
    "whitespace",
    "hue_entropy",
    "h_symmetry",
    "v_symmetry",
    "top_contrast",
)


def _feature_vector(feats: dict[str, float]) -> np.ndarray:
    return np.array([feats[k] for k in FEATURE_KEYS], dtype=np.float64)


def _projection_matrix() -> np.ndarray:
    """Fixed (N_PARCELS, n_features) projection. Seeded, so identical every run."""
    rng = np.random.default_rng(_PROJECTION_SEED)
    return rng.standard_normal((N_PARCELS, len(FEATURE_KEYS)))


# --------------------------------------------------------------------------- #
# Real TRIBE-v2 hooks (stubs)
# --------------------------------------------------------------------------- #
def _encode_real_endpoint(path: str | Path, endpoint: str) -> np.ndarray:
    raise NotImplementedError(
        f"TRIBE_ENDPOINT is set ({endpoint}) but the real-TRIBE HTTP client is not "
        "wired up. Implement _encode_real_endpoint to POST the stimulus and return a "
        f"({N_PARCELS},) Schaefer-1000 parcel vector."
    )


def _encode_real_weights(path: str | Path, weights: str) -> np.ndarray:
    raise NotImplementedError(
        f"TRIBE_WEIGHTS is set ({weights}) but the local TRIBE-v2 forward pass is not "
        "wired up. Implement _encode_real_weights to load the checkpoint and return a "
        f"({N_PARCELS},) Schaefer-1000 parcel vector."
    )


# --------------------------------------------------------------------------- #
# Public API
# --------------------------------------------------------------------------- #
def active_backend() -> str:
    """Which encoder backend will run, given the current environment."""
    if os.environ.get("TRIBE_ENDPOINT"):
        return "tribe-endpoint"
    if os.environ.get("TRIBE_WEIGHTS"):
        return "tribe-weights"
    return "perceptual-fallback"


def encode_image(path: str | Path) -> np.ndarray:
    """Predict a cortical parcel response for a single UI screenshot.

    Returns a 1-D array of shape ``(N_PARCELS,)`` — a TRIBE-style per-parcel
    response map. Real TRIBE-v2 is used when configured; otherwise the
    deterministic perceptual fallback runs.
    """
    endpoint = os.environ.get("TRIBE_ENDPOINT")
    if endpoint:
        return _ensure_shape(_encode_real_endpoint(path, endpoint))
    weights = os.environ.get("TRIBE_WEIGHTS")
    if weights:
        return _ensure_shape(_encode_real_weights(path, weights))

    rgb = _load_rgb(path)
    feats = perceptual_features(rgb)
    fvec = _feature_vector(feats)
    # Standardize features to comparable scale before the linear lift so no single
    # raw-magnitude feature dominates the parcel vector.
    fvec = (fvec - _FEATURE_CENTER) / _FEATURE_SCALE
    parcels = _projection_matrix() @ fvec
    return _ensure_shape(parcels)


def encode_features(path: str | Path) -> dict[str, float]:
    """Expose the raw perceptual features for a screenshot (diagnostics/inspection)."""
    return perceptual_features(_load_rgb(path))


def _ensure_shape(arr: np.ndarray) -> np.ndarray:
    arr = np.asarray(arr, dtype=np.float64).reshape(-1)
    if arr.shape[0] != N_PARCELS:
        raise ValueError(f"encoder produced {arr.shape[0]} parcels, expected {N_PARCELS}")
    return arr


# Rough centering/scaling constants for the perceptual features, derived from the
# natural range each statistic takes on web screenshots. Used only to keep the
# linear lift well-conditioned; exact values are not load-bearing because the
# classifier standardizes again downstream.
_FEATURE_CENTER = np.array(
    [0.06, 0.18, 0.30, 0.20, 0.18, 0.25, 2.5, 0.85, 0.80, 0.18], dtype=np.float64
)
_FEATURE_SCALE = np.array(
    [0.04, 0.10, 0.20, 0.15, 0.10, 0.20, 1.0, 0.10, 0.12, 0.10], dtype=np.float64
)
