"""SaliencySignal — predicts attention with DeepGaze IIE + III and scores the
distribution against a 4-component AttentionScore rubric.

Pipeline per candidate:
  1. Read the at-rest frame from `ctx.frames[0]` (captured by pipeline/capture.py).
  2. DeepGaze IIE -> log-density heatmap (smoothed by a centered Gaussian prior).
  3. DeepGaze III -> scanpath with inhibition-of-return (10 fixations).
  4. Compute metrics: top-third mass, peak dominance, composition spread,
     mean saccade, gini, etc.
  5. Roll up into AttentionScore on 0-100 -> divide by 10 -> SignalResult.score.
  6. Save overlay PNG to `<cand_dir>/saliency.png` for the dashboard.

The two DeepGaze models are heavy; the first call downloads weights and loads
them onto CPU/CUDA. Subsequent calls reuse the module-level singletons.

Weights live in `WEIGHTS` and are duplicated in `details["subscores"]` so the
dashboard and critic can read them without reloading this module.
"""

from __future__ import annotations

import math
from dataclasses import asdict, dataclass
from pathlib import Path

import numpy as np
from PIL import Image
from scipy.ndimage import gaussian_filter, label

from pipeline.context import CandidateContext, SignalResult
from pipeline.registry import register_signal


# ---------- config ----------

MAX_DIM = 1024
N_FIXATIONS = 10
IOR_SIGMA = 60
IOR_STRENGTH = 4.0

WEIGHTS = {
    "hero_pull": 0.35,
    "focal_dominance": 0.30,
    "composition_spread": 0.20,
    "scan_rhythm": 0.15,
}


# ---------- lazy model loading ----------

_models: dict = {"device": None, "iie": None, "iii": None}


def _get_models():
    """Load DeepGaze IIE + III once and reuse. Returns (device, iie, iii)."""
    if _models["iie"] is not None:
        return _models["device"], _models["iie"], _models["iii"]
    import torch
    import deepgaze_pytorch
    device = torch.device("cuda" if torch.cuda.is_available() else "cpu")
    iie = deepgaze_pytorch.DeepGazeIIE(pretrained=True).to(device).eval()
    iii = deepgaze_pytorch.DeepGazeIII(pretrained=True).to(device).eval()
    _models.update(device=device, iie=iie, iii=iii)
    return device, iie, iii


# ---------- data structures ----------

@dataclass
class _Metrics:
    entropy_bits: float
    gini: float
    top5pct_mass: float
    com_x_norm: float
    com_y_norm: float
    mass_top_third: float
    mass_mid_third: float
    mass_bot_third: float
    n_peaks: int
    scanpath_length_px: float
    mean_saccade_px: float
    first_fix_x_norm: float
    first_fix_y_norm: float
    peak_dominance: float
    composition_spread: float


@dataclass
class _Subscores:
    total: float
    hero_pull: float
    focal_dominance: float
    composition_spread: float
    scan_rhythm: float


# ---------- saliency math (ported from experiments/eyetrackingmetrics.py) ----------

def _load_and_resize(path: Path, max_dim: int = MAX_DIM) -> np.ndarray:
    img = Image.open(path).convert("RGB")
    w, h = img.size
    scale = min(max_dim / w, max_dim / h, 1.0)
    if scale < 1.0:
        img = img.resize((int(w * scale), int(h * scale)), Image.LANCZOS)
    return np.asarray(img)


def _centerbias(h: int, w: int, sigma_frac: float = 0.25) -> np.ndarray:
    ys = np.linspace(-1, 1, h)[:, None]
    xs = np.linspace(-1, 1, w)[None, :]
    sigma = sigma_frac * 2
    g = np.exp(-(xs**2 + ys**2) / (2 * sigma**2))
    g = g / g.sum()
    cb = np.log(g + 1e-12)
    cb = cb - np.log(np.exp(cb).sum())
    return cb


def _to_tensor(img: np.ndarray, device):
    import torch
    t = torch.tensor(img.transpose(2, 0, 1), dtype=torch.float32, device=device)
    return t.unsqueeze(0)


def _ior_penalty(h: int, w: int, fixations: list[tuple[float, float]],
                 sigma: float = IOR_SIGMA, strength: float = IOR_STRENGTH) -> np.ndarray:
    pen = np.zeros((h, w), dtype=np.float32)
    if not fixations:
        return pen
    ys, xs = np.mgrid[0:h, 0:w].astype(np.float32)
    for (fx, fy) in fixations:
        d2 = (xs - fx) ** 2 + (ys - fy) ** 2
        pen -= strength * np.exp(-d2 / (2 * sigma ** 2))
    return pen


def _predict(image_path: Path, n_fixations: int = N_FIXATIONS):
    import torch
    device, iie, iii = _get_models()
    img = _load_and_resize(image_path, MAX_DIM)
    h, w = img.shape[:2]
    img_t = _to_tensor(img, device)
    cb = _centerbias(h, w)
    cb_t = torch.tensor(cb, dtype=torch.float32, device=device).unsqueeze(0)

    with torch.no_grad():
        log_density = iie(img_t, cb_t).squeeze().cpu().numpy()
    density = np.exp(log_density)
    density = density / density.sum()

    fy0, fx0 = np.unravel_index(density.argmax(), density.shape)
    fixations: list[tuple[float, float]] = [(float(fx0), float(fy0))]
    hist_x = [float(fx0)] * 4
    hist_y = [float(fy0)] * 4
    for _ in range(n_fixations - 1):
        x_hist = torch.tensor([hist_x[-4:]], dtype=torch.float32, device=device)
        y_hist = torch.tensor([hist_y[-4:]], dtype=torch.float32, device=device)
        with torch.no_grad():
            ld_step = iii(img_t, cb_t, x_hist, y_hist).squeeze().cpu().numpy()
        ld_step = ld_step + _ior_penalty(h, w, fixations)
        fy, fx = np.unravel_index(ld_step.argmax(), ld_step.shape)
        fixations.append((float(fx), float(fy)))
        hist_x.append(float(fx))
        hist_y.append(float(fy))

    return img, log_density.astype(np.float32), density.astype(np.float32), fixations


def _gini(arr: np.ndarray) -> float:
    a = np.sort(arr.ravel())
    n = a.size
    if a.sum() == 0:
        return 0.0
    cum = np.cumsum(a)
    return float((n + 1 - 2 * (cum.sum() / cum[-1])) / n)


def _count_peaks(density: np.ndarray, min_distance: int = 40, top_k: int = 5) -> int:
    d = density.copy()
    h, w = d.shape
    peaks = 0
    ys, xs = np.mgrid[0:h, 0:w].astype(np.float32)
    thresh = d.max() * 0.25
    for _ in range(top_k):
        idx = d.argmax()
        v = d.flat[idx]
        if v < thresh:
            break
        peaks += 1
        py, px = np.unravel_index(idx, d.shape)
        mask = ((xs - px) ** 2 + (ys - py) ** 2) < (min_distance ** 2)
        d[mask] = 0.0
    return peaks


def _peak_dominance(density: np.ndarray, rel_thresh: float = 0.5) -> float:
    v1 = float(density.max())
    if v1 == 0:
        return 1.0
    py, px = np.unravel_index(density.argmax(), density.shape)
    mask = density > (rel_thresh * v1)
    labeled, _ = label(mask)
    peak_label = labeled[py, px]
    d2 = density.copy()
    d2[labeled == peak_label] = 0.0
    v2 = float(d2.max())
    if v2 == 0:
        return 5.0
    return float(min(v1 / v2, 5.0))


def _composition_spread(density: np.ndarray) -> float:
    h, w = density.shape
    ys, xs = np.mgrid[0:h, 0:w].astype(np.float32)
    total = float(density.sum())
    if total == 0:
        return 0.0
    com_x = (xs * density).sum() / total
    com_y = (ys * density).sum() / total
    var_x = ((xs - com_x) ** 2 * density).sum() / total
    var_y = ((ys - com_y) ** 2 * density).sum() / total
    norm = ((var_x / (w * w / 12.0)) + (var_y / (h * h / 12.0))) / 2.0
    return float(min(math.sqrt(norm), 1.0))


def _compute_metrics(image: np.ndarray, density: np.ndarray, scanpath: list) -> _Metrics:
    h, w = density.shape
    p = density.ravel()
    p = p[p > 0]
    entropy = float(-(p * np.log2(p)).sum())
    gini_v = _gini(density)
    flat_sorted = np.sort(density.ravel())[::-1]
    top5_n = max(1, int(0.05 * flat_sorted.size))
    top5 = float(flat_sorted[:top5_n].sum())
    ys, xs = np.mgrid[0:h, 0:w]
    com_x = float((xs * density).sum() / density.sum())
    com_y = float((ys * density).sum() / density.sum())
    third = h // 3
    mass_top = float(density[:third].sum())
    mass_mid = float(density[third:2 * third].sum())
    mass_bot = float(density[2 * third:].sum())
    pts = np.array(scanpath, dtype=np.float32)
    diffs = np.diff(pts, axis=0)
    saccades = np.linalg.norm(diffs, axis=1) if len(diffs) else np.array([0.0])
    fx0, fy0 = scanpath[0]
    return _Metrics(
        entropy_bits=round(entropy, 3),
        gini=round(gini_v, 3),
        top5pct_mass=round(top5, 3),
        com_x_norm=round(com_x / w, 3),
        com_y_norm=round(com_y / h, 3),
        mass_top_third=round(mass_top, 3),
        mass_mid_third=round(mass_mid, 3),
        mass_bot_third=round(mass_bot, 3),
        n_peaks=_count_peaks(density),
        scanpath_length_px=round(float(saccades.sum()), 1),
        mean_saccade_px=round(float(saccades.mean()), 1),
        first_fix_x_norm=round(fx0 / w, 3),
        first_fix_y_norm=round(fy0 / h, 3),
        peak_dominance=round(_peak_dominance(density), 3),
        composition_spread=round(_composition_spread(density), 3),
    )


def _clip01(x: float) -> float:
    return float(max(0.0, min(1.0, x)))


def _compute_subscores(m: _Metrics) -> _Subscores:
    hero = _clip01(m.mass_top_third / 0.40)
    focal = _clip01((m.peak_dominance - 1.0) / 2.0)
    spread = _clip01(m.composition_spread)
    scan = float(math.exp(-((m.mean_saccade_px - 350.0) / 250.0) ** 2 / 2.0))
    total = 100.0 * (
        WEIGHTS["hero_pull"] * hero
        + WEIGHTS["focal_dominance"] * focal
        + WEIGHTS["composition_spread"] * spread
        + WEIGHTS["scan_rhythm"] * scan
    )
    return _Subscores(
        total=round(total, 1),
        hero_pull=round(hero, 3),
        focal_dominance=round(focal, 3),
        composition_spread=round(spread, 3),
        scan_rhythm=round(scan, 3),
    )


def _save_overlay(image: np.ndarray, density: np.ndarray, out_path: Path) -> None:
    """Save a heatmap-over-image PNG to `out_path`. Used by the dashboard."""
    sm = gaussian_filter(density, sigma=8)
    sm = (sm - sm.min()) / (sm.max() - sm.min() + 1e-12)
    # Compose: image as base, inferno-mapped saliency on top with alpha
    base = image.astype(np.float32) / 255.0
    import matplotlib.cm as cm
    heat_rgba = cm.inferno(sm)
    heat_rgb = heat_rgba[..., :3]
    alpha = 0.55
    blended = (1 - alpha) * base + alpha * heat_rgb
    blended = (np.clip(blended, 0, 1) * 255).astype(np.uint8)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    Image.fromarray(blended).save(out_path)


@register_signal
class SaliencySignal:
    key = "saliency"

    def score(self, ctx: CandidateContext) -> SignalResult:
        if not ctx.frames:
            return SignalResult(score=None, skipped="no frames captured")

        frame = Path(ctx.frames[0])
        if not frame.exists():
            return SignalResult(score=None, skipped=f"frame missing: {frame}")

        try:
            image, _log_density, density, scanpath = _predict(frame)
        except Exception as e:
            return SignalResult(score=None, skipped=f"deepgaze error: {e}")

        metrics = _compute_metrics(image, density, scanpath)
        sub = _compute_subscores(metrics)

        # Save overlay alongside the candidate so the dashboard can render it.
        try:
            _save_overlay(image, density, ctx.candidate_dir / "saliency.png")
        except Exception:
            # Overlay failure is non-fatal — the score is still valid.
            pass

        # AttentionScore is 0-100; SignalResult.score is 0-10.
        return SignalResult(
            score=sub.total / 10.0,
            details={
                "subscores": asdict(sub),
                "metrics": asdict(metrics),
                "weights": dict(WEIGHTS),
            },
        )
