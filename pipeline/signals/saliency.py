"""SaliencySignal — predicts attention with DeepGaze IIE + III and scores it
against an intent-grounded rubric.

Pipeline per candidate:
  1. `ctx.frames` is ordered entry → … → settled. Primary view scored for
     intent / focus / reading is `ctx.frames[-1]` (settled — what the user
     sees most). Earlier frames feed `animation_focus`.
  2. DeepGaze IIE -> log-density heatmap (smoothed by a centered Gaussian prior).
  3. DeepGaze III -> scanpath with inhibition-of-return (10 fixations).
  4. Compute resolution-invariant rubric:
       intent_alignment  (0.45) — did the eye land on the intended focal region?
       focus_clarity     (0.20) — is there one dominant peak, or is the page flat?
       reading_order     (0.20) — does the scanpath flow top-to-bottom / left-to-right?
       animation_focus   (0.15) — does motion guide the eye TO the target, or steal it?
  5. Renormalize over present subscores (any subscore can `skip`), produce a
     0-100 total -> divide by 10 -> SignalResult.score.
  6. Save overlay PNG to `<cand_dir>/saliency.png` with focal_bbox + first-fixation
     marker drawn on it for human verification.

Every constant that influences the score is a normalized fraction of image
dimensions (no raw-pixel magic numbers) and carries an inline comment stating
the assumption it encodes.

The two DeepGaze models are heavy; the first call downloads weights and loads
them onto CPU/CUDA. Subsequent calls reuse the module-level singletons.
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

# Weighted rubric. Renormalized at runtime over subscores that returned a value
# (a skipped subscore drops out instead of penalizing the candidate).
DEFAULT_WEIGHTS = {
    "intent_alignment": 0.45,  # headline signal: attention vs. intended focal region
    "focus_clarity": 0.20,  # is there a clear focal point, or is the page flat?
    "reading_order": 0.20,  # does the scanpath flow naturally (top→bottom, left→right)?
    "animation_focus": 0.15,  # multi-frame: does motion guide the eye to the target?
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
    """Raw, signal-agnostic diagnostics. Kept separate from subscores so the
    dashboard and critic can inspect the underlying distribution without
    re-deriving it from the heatmap."""

    entropy_bits: float
    gini: float
    top5pct_mass: float
    com_x_norm: float
    com_y_norm: float
    mass_top_third: float
    mass_mid_third: float
    mass_bot_third: float
    n_peaks: int
    scanpath_length_norm: float  # sum of saccade magnitudes, in image-diagonal units
    mean_saccade_norm: float  # mean saccade magnitude, in image-diagonal units
    first_fix_x_norm: float
    first_fix_y_norm: float
    peak_dominance: float
    composition_spread: float


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


def _ior_penalty(
    h: int,
    w: int,
    fixations: list[tuple[float, float]],
    sigma: float = IOR_SIGMA,
    strength: float = IOR_STRENGTH,
) -> np.ndarray:
    pen = np.zeros((h, w), dtype=np.float32)
    if not fixations:
        return pen
    ys, xs = np.mgrid[0:h, 0:w].astype(np.float32)
    for fx, fy in fixations:
        d2 = (xs - fx) ** 2 + (ys - fy) ** 2
        pen -= strength * np.exp(-d2 / (2 * sigma**2))
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


# ---------- diagnostic metric helpers (no scoring constants) ----------


def _gini(arr: np.ndarray) -> float:
    a = np.sort(arr.ravel())
    n = a.size
    if a.sum() == 0:
        return 0.0
    cum = np.cumsum(a)
    return float((n + 1 - 2 * (cum.sum() / cum[-1])) / n)


def _count_peaks(
    density: np.ndarray, min_distance_frac: float = 0.04, top_k: int = 5
) -> int:
    """Count distinct attention peaks. `min_distance_frac` is the suppression
    radius as a fraction of the larger image dimension — peaks closer than this
    are treated as the same lobe. 0.04 ≈ a finger-width on a typical viewport,
    which is roughly the resolution at which two CTAs feel like "one cluster"."""
    d = density.copy()
    h, w = d.shape
    min_distance = max(1, int(min_distance_frac * max(h, w)))
    peaks = 0
    ys, xs = np.mgrid[0:h, 0:w].astype(np.float32)
    # A "peak" must be at least 25% of the global max — below that it's noise.
    thresh = d.max() * 0.25
    for _ in range(top_k):
        idx = d.argmax()
        v = d.flat[idx]
        if v < thresh:
            break
        peaks += 1
        py, px = np.unravel_index(idx, d.shape)
        mask = ((xs - px) ** 2 + (ys - py) ** 2) < (min_distance**2)
        d[mask] = 0.0
    return peaks


def _peak_dominance(density: np.ndarray, rel_thresh: float = 0.5) -> float:
    """Ratio of the strongest attention region's peak to the runner-up's peak.
    Connected-components are formed at 50% of the global max so a single broad
    lobe counts once. Capped at 5 to avoid blowing up when there is no
    competing peak at all (a flat distribution returns 1.0)."""
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
    """Diagnostic only (NOT a scoring component): normalized 2-D std-dev of
    attention. Useful for the dashboard, but a high value is not inherently
    "good" — centered designs are also valid."""
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


def _compute_metrics(
    image: np.ndarray, density: np.ndarray, scanpath: list
) -> _Metrics:
    h, w = density.shape
    diag = math.sqrt(
        h * h + w * w
    )  # image diagonal in pixels — used to normalize distances
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
    mass_mid = float(density[third : 2 * third].sum())
    mass_bot = float(density[2 * third :].sum())
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
        scanpath_length_norm=round(float(saccades.sum() / diag), 3),
        mean_saccade_norm=round(float(saccades.mean() / diag), 3),
        first_fix_x_norm=round(fx0 / w, 3),
        first_fix_y_norm=round(fy0 / h, 3),
        peak_dominance=round(_peak_dominance(density), 3),
        composition_spread=round(_composition_spread(density), 3),
    )


def _clip01(x: float) -> float:
    return float(max(0.0, min(1.0, x)))


# ---------- new rubric: subscore helpers ----------


def _read_focal_bbox(cfg: dict) -> tuple[float, float, float, float] | None:
    """Read `config['saliency']['focal_bbox']` as a normalized [x0, y0, x1, y1]
    rectangle. Returns None if absent or malformed. Coordinates are fractions
    of the viewport (0..1); the rectangle must be non-degenerate."""
    if not isinstance(cfg, dict):
        return None
    sal = cfg.get("saliency")
    if not isinstance(sal, dict):
        return None
    bbox = sal.get("focal_bbox")
    if not isinstance(bbox, (list, tuple)) or len(bbox) != 4:
        return None
    try:
        x0, y0, x1, y1 = (float(v) for v in bbox)
    except (TypeError, ValueError):
        return None
    # Clamp to the unit square, then reject degenerate rectangles.
    x0, x1 = max(0.0, min(1.0, x0)), max(0.0, min(1.0, x1))
    y0, y1 = max(0.0, min(1.0, y0)), max(0.0, min(1.0, y1))
    if x1 <= x0 or y1 <= y0:
        return None
    return (x0, y0, x1, y1)


def _concentration_in_bbox(
    density: np.ndarray, bbox: tuple[float, float, float, float]
) -> tuple[float, float]:
    """Return (mass_in_bbox, concentration). `density` is a probability
    distribution summing to 1; `bbox` is normalized [x0, y0, x1, y1].

    `concentration = mass / area_frac`: 1.0 means the bbox got exactly its fair
    share of attention by area (i.e. no design pull), >1 means above-chance."""
    h, w = density.shape
    x0, y0, x1, y1 = bbox
    px0, px1 = int(round(x0 * w)), int(round(x1 * w))
    py0, py1 = int(round(y0 * h)), int(round(y1 * h))
    px0, px1 = max(0, px0), min(w, px1)
    py0, py1 = max(0, py0), min(h, py1)
    if px1 <= px0 or py1 <= py0:
        return 0.0, 0.0
    mass = float(density[py0:py1, px0:px1].sum())
    area_frac = ((px1 - px0) * (py1 - py0)) / float(h * w)
    if area_frac <= 0:
        return 0.0, 0.0
    return mass, mass / area_frac


def _intent_alignment_score(
    density: np.ndarray, scanpath: list, bbox: tuple[float, float, float, float]
) -> tuple[float, dict]:
    """Subscore + diagnostics for "did the eye land on the intended region?"

    Map concentration → [0, 1] with documented anchors:
      concentration <= 1.0 → 0.0 (no better than chance for a region of this size)
      concentration >= 3.0 → 1.0 (3× chance: a clear, deliberate pull)
    The 3× upper anchor: empirically, when a region commands ~3× its
    area-share of attention, the rest of the page stops competing with it.
    Below the lower anchor we treat any reward as accidental.
    """
    h, w = density.shape
    mass, concentration = _concentration_in_bbox(density, bbox)
    score = _clip01((concentration - 1.0) / (3.0 - 1.0))
    fx0, fy0 = scanpath[0]
    x0, y0, x1, y1 = bbox
    first_in = (x0 <= fx0 / w <= x1) and (y0 <= fy0 / h <= y1)
    return score, {
        "mass_in_bbox": round(mass, 3),
        "concentration": round(concentration, 3),
        "first_fixation_in_bbox": bool(first_in),
        "bbox": [round(v, 3) for v in bbox],
    }


def _focus_clarity_score(peak_dominance: float) -> tuple[float, dict]:
    """Subscore + diagnostics for "is there one clear focal point?"

    Map peak_dominance → [0, 1] with documented anchors:
      ratio <= 1.2 → 0.0 (top peak ~tied with the runner-up; flat hierarchy)
      ratio >= 3.0 → 1.0 (top peak commands attention; clear single focus)
    Why 1.2 floor: a ratio under 1.2 is within prediction noise — the two
    peaks are effectively equal. Why 3.0 ceiling: at 3× the runner-up, the
    primary lobe unambiguously dominates the page.
    """
    score = _clip01((peak_dominance - 1.2) / (3.0 - 1.2))
    return score, {"peak_dominance": round(peak_dominance, 3)}


def _reading_order_score(scanpath: list, w: int, h: int) -> tuple[float, dict]:
    """Subscore + diagnostics for "does gaze flow naturally?"

    Each saccade is scored on its direction in normalized coordinates:
      s = dy + 0.5 * dx, clipped to [-1, 1]
    This rewards downward motion (the dominant reading axis) and rewards
    rightward motion at half-weight (the secondary F/Z axis). Backtracks
    (upward or leftward) get negative scores proportional to their distance,
    so a single big jump backwards costs more than a few small wobbles.
    The per-saccade scores are weighted by saccade magnitude (in normalized
    units) so trivial micro-saccades don't dominate the average.
    """
    if len(scanpath) < 2:
        return 0.5, {"raw_score": 0.0, "n_saccades": 0, "fraction_forward": 0.0}
    pts = [(fx / w, fy / h) for fx, fy in scanpath]
    weighted_sum = 0.0
    total_weight = 0.0
    n_forward = 0
    n_total = 0
    # 0.001 of the unit square ≈ 1px on a 1000px image — below this is sub-pixel jitter.
    min_meaningful_mag = 1e-3
    for i in range(1, len(pts)):
        dx = pts[i][0] - pts[i - 1][0]
        dy = pts[i][1] - pts[i - 1][1]
        mag = math.sqrt(dx * dx + dy * dy)
        if mag < min_meaningful_mag:
            continue
        s = max(-1.0, min(1.0, dy + 0.5 * dx))
        weighted_sum += mag * s
        total_weight += mag
        if s > 0:
            n_forward += 1
        n_total += 1
    if total_weight == 0:
        return 0.5, {"raw_score": 0.0, "n_saccades": 0, "fraction_forward": 0.0}
    raw = weighted_sum / total_weight  # in [-1, 1]
    score = (raw + 1.0) / 2.0  # remap to [0, 1]
    return score, {
        "raw_score": round(raw, 3),
        "n_saccades": n_total,
        "fraction_forward": round(n_forward / n_total, 3),
    }


def _animation_focus_score(
    frames: list[Path], bbox: tuple[float, float, float, float]
) -> tuple[float, dict]:
    """Subscore + diagnostics for "does motion guide the eye TO the target?"

    Compares intent-alignment on a mid-animation frame vs. the settled frame.
    Reward if alignment grew (motion drew the eye in); penalize if it shrank
    (decorative motion stole attention away).

    Formula: animation_focus = clip01(a_end + 0.5 * (a_end - a_mid))
      - a_end alone is the baseline (settled alignment is what users see last)
      - the (a_end - a_mid) term adds up to +0.5 if motion improved alignment
        and subtracts up to 0.5 if motion degraded it
    The 0.5 weight on the delta encodes: "the final state matters more than
    the journey, but the journey matters."
    """
    settled = Path(frames[-1])
    # Pick a frame strictly before the settled one. Middle index is the safest
    # default — far enough into the animation to be meaningful, not so close
    # to the end that it's identical to settled.
    mid_idx = len(frames) // 2
    if mid_idx >= len(frames) - 1:
        mid_idx = len(frames) - 2
    mid = Path(frames[mid_idx])

    if not settled.exists() or not mid.exists():
        raise FileNotFoundError("animation frames missing on disk")

    _, _, density_settled, _ = _predict(settled)
    _, _, density_mid, _ = _predict(mid)
    _, conc_settled = _concentration_in_bbox(density_settled, bbox)
    _, conc_mid = _concentration_in_bbox(density_mid, bbox)

    a_end = _clip01((conc_settled - 1.0) / (3.0 - 1.0))
    a_mid = _clip01((conc_mid - 1.0) / (3.0 - 1.0))
    score = _clip01(a_end + 0.5 * (a_end - a_mid))
    return score, {
        "alignment_mid": round(a_mid, 3),
        "alignment_settled": round(a_end, 3),
        "improvement": round(a_end - a_mid, 3),
        "mid_frame_index": mid_idx,
    }


# ---------- explanation strings ----------


def _explain_intent(diag: dict) -> str:
    mass_pct = diag["mass_in_bbox"] * 100.0
    conc = diag["concentration"]
    first_in = diag["first_fixation_in_bbox"]
    fix_phrase = (
        "and the first fixation landed inside it"
        if first_in
        else "but the first fixation landed outside it"
    )
    if conc >= 3.0:
        verdict = "the design pulls the eye strongly to the intended region"
    elif conc >= 1.5:
        verdict = "the design pulls the eye moderately toward the intended region"
    elif conc >= 1.0:
        verdict = "attention on the intended region is only at chance — no real pull"
    else:
        verdict = "attention actively avoids the intended region"
    return (
        f"{mass_pct:.0f}% of predicted attention fell on the intended focal region, "
        f"~{conc:.1f}x its fair share by area, {fix_phrase} -> {verdict}."
    )


def _explain_focus(diag: dict) -> str:
    pd = diag["peak_dominance"]
    if pd >= 3.0:
        return f"The strongest region is {pd:.1f}x more salient than the runner-up -> one clearly dominant focal point."
    if pd >= 1.5:
        return f"The strongest region is {pd:.1f}x more salient than the runner-up -> a primary focal point exists, but other elements compete."
    return f"Top region is only {pd:.1f}x the runner-up -> attention is roughly evenly spread; no clear hierarchy."


def _explain_reading(diag: dict) -> str:
    n = diag["n_saccades"]
    if n == 0:
        return "Only one meaningful fixation — no scanpath flow to score."
    raw = diag["raw_score"]
    pct_fwd = int(round(diag["fraction_forward"] * 100))
    if raw >= 0.3:
        return f"{pct_fwd}% of saccades move down/right -> natural top-to-bottom reading flow."
    if raw >= 0.0:
        return (
            f"{pct_fwd}% of saccades move forward -> mixed flow with some backtracking."
        )
    return (
        f"Only {pct_fwd}% of saccades move forward -> chaotic, disorienting scanpath."
    )


def _explain_animation(diag: dict) -> str:
    a_mid = diag["alignment_mid"]
    a_end = diag["alignment_settled"]
    delta = diag["improvement"]
    if delta > 0.15:
        return (
            f"Alignment with the focal region rose from {a_mid:.2f} mid-animation to {a_end:.2f} at rest "
            f"-> motion purposefully drew the eye toward the target."
        )
    if delta < -0.15:
        return (
            f"Alignment dropped from {a_mid:.2f} mid-animation to {a_end:.2f} at rest "
            f"-> motion pulled the eye away from the target (gratuitous motion)."
        )
    return f"Alignment held roughly steady through the animation (~{a_end:.2f}) -> motion is neutral with respect to the focal region."


# ---------- rubric assembly ----------


def _compute_subscores(
    ctx: CandidateContext,
    density: np.ndarray,
    scanpath: list,
    bbox: tuple[float, float, float, float] | None,
    metrics: _Metrics,
    h: int,
    w: int,
) -> tuple[dict, dict, dict]:
    """Run the four subscores. Any that can't be evaluated are recorded as
    None and their weight is redistributed proportionally across the rest.

    Returns: (subscores_dict, renormalized_weights_dict, explanations_dict)
    where subscores includes a `total` on 0-100 and each subscore on 0-1.
    """
    raw: dict[str, float | None] = {}
    explanations: dict[str, str] = {}

    # 1. intent_alignment — requires a focal_bbox.
    if bbox is None:
        raw["intent_alignment"] = None
        explanations["intent_alignment"] = (
            "skipped: no focal_bbox configured under config['saliency']['focal_bbox'] "
            "(expected normalized [x0, y0, x1, y1])."
        )
    else:
        s, diag = _intent_alignment_score(density, scanpath, bbox)
        raw["intent_alignment"] = s
        explanations["intent_alignment"] = _explain_intent(diag)

    # 2. focus_clarity — always available.
    s, diag = _focus_clarity_score(metrics.peak_dominance)
    raw["focus_clarity"] = s
    explanations["focus_clarity"] = _explain_focus(diag)

    # 3. reading_order — always available (degenerate path → neutral 0.5).
    s, diag = _reading_order_score(scanpath, w, h)
    raw["reading_order"] = s
    explanations["reading_order"] = _explain_reading(diag)

    # 4. animation_focus — needs ≥2 frames AND a focal_bbox to compare against.
    if len(ctx.frames) < 2:
        raw["animation_focus"] = None
        explanations["animation_focus"] = (
            "skipped: only one frame captured (need >=2 to assess animation)."
        )
    elif bbox is None:
        raw["animation_focus"] = None
        explanations["animation_focus"] = (
            "skipped: no focal_bbox to compare intent alignment across frames."
        )
    else:
        try:
            s, diag = _animation_focus_score(ctx.frames, bbox)
            raw["animation_focus"] = s
            explanations["animation_focus"] = _explain_animation(diag)
        except Exception as e:
            raw["animation_focus"] = None
            explanations["animation_focus"] = (
                f"skipped: animation analysis failed ({e})."
            )

    # Renormalize over subscores that returned a value.
    present = {k: v for k, v in raw.items() if v is not None}
    weight_sum = sum(DEFAULT_WEIGHTS[k] for k in present) if present else 0.0
    weights_out: dict[str, float] = {}
    for k in DEFAULT_WEIGHTS:
        if k in present and weight_sum > 0:
            weights_out[k] = round(DEFAULT_WEIGHTS[k] / weight_sum, 3)
        else:
            weights_out[k] = 0.0

    total = (
        100.0 * sum(weights_out[k] * present[k] for k in present) if present else 0.0
    )

    subscores: dict = {"total": round(total, 1)}
    for k, v in raw.items():
        subscores[k] = round(v, 3) if v is not None else None

    return subscores, weights_out, explanations


# ---------- overlay ----------


def _save_overlay(
    image: np.ndarray,
    density: np.ndarray,
    out_path: Path,
    bbox: tuple[float, float, float, float] | None = None,
    first_fix: tuple[float, float] | None = None,
) -> None:
    """Heat-only RGBA PNG — transparent everywhere except where attention lands.

    The PNG carries no baked-in page pixels. Alpha is driven by the
    normalized density itself: cold regions are fully transparent so the
    dashboard's live iframe shows through, hot regions are opaque inferno
    color. This is what lets the dashboard overlay the heatmap on the
    candidate's running animation without darkening the rest of the page."""
    _ = (image, bbox, first_fix)  # image kept in signature for backwards-compatible callers
    sm = gaussian_filter(density, sigma=8)
    sm = (sm - sm.min()) / (sm.max() - sm.min() + 1e-12)
    import matplotlib.cm as cm

    heat_rgb = cm.inferno(sm)[..., :3]
    h, w = sm.shape
    rgba = np.zeros((h, w, 4), dtype=np.float32)
    rgba[..., :3] = heat_rgb
    # Alpha = saliency density itself → smooth fade from transparent cold
    # to opaque hot. No hard threshold; the heatmap fades in naturally.
    rgba[..., 3] = sm
    rgba = (np.clip(rgba, 0, 1) * 255).astype(np.uint8)

    out_path.parent.mkdir(parents=True, exist_ok=True)
    Image.fromarray(rgba, mode="RGBA").save(out_path)


@register_signal
class SaliencySignal:
    key = "saliency"

    def score(self, ctx: CandidateContext) -> SignalResult:
        if not ctx.frames:
            return SignalResult(score=None, skipped="no frames captured")

        # `ctx.frames` is ordered entry → … → settled. The settled frame is
        # what the user sees most, so it's the primary view for intent_alignment,
        # focus_clarity and reading_order. The full sequence drives animation_focus.
        primary_frame = Path(ctx.frames[-1])
        if not primary_frame.exists():
            return SignalResult(score=None, skipped=f"frame missing: {primary_frame}")

        try:
            image, _log_density, density, scanpath = _predict(primary_frame)
        except Exception as e:
            return SignalResult(score=None, skipped=f"deepgaze error: {e}")

        h, w = density.shape
        metrics = _compute_metrics(image, density, scanpath)
        bbox = _read_focal_bbox(ctx.config)

        subscores, weights_out, explanations = _compute_subscores(
            ctx=ctx,
            density=density,
            scanpath=scanpath,
            bbox=bbox,
            metrics=metrics,
            h=h,
            w=w,
        )

        # Save the human-readable overlay alongside the candidate so the
        # dashboard can show "predicted gaze vs. intended target" side-by-side.
        first_fix_px = scanpath[0] if scanpath else None
        try:
            _save_overlay(
                image,
                density,
                ctx.candidate_dir / "saliency.png",
                bbox=bbox,
                first_fix=first_fix_px,
            )
        except Exception:
            # Overlay failure is non-fatal — the score is still valid.
            pass

        # Normalized scanpath: each fixation as (x, y) in [0, 1], relative to
        # the (resized) frame image. The dashboard stretches the saliency PNG
        # to fit the stage, so these normalized coords land at the right spot
        # on the live preview too. Order = predicted fixation 1 → 2 → … → N.
        scanpath_norm = [
            [round(fx / w, 4), round(fy / h, 4)] for fx, fy in scanpath
        ]

        # SignalResult.score is on a 0-10 scale; the rubric produces 0-100.
        return SignalResult(
            score=subscores["total"] / 10.0,
            details={
                "subscores": subscores,
                "weights": weights_out,
                "explanations": explanations,
                "metrics": asdict(metrics),
                "scanpath_norm": scanpath_norm,
            },
        )
