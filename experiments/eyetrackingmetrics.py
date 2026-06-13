"""
Saliency benchmark for AutoDesign — v1.

End-to-end pipeline:
  1. Screenshot one or more UIs (from local HTML or URL) with Playwright.
  2. Predict a saliency heatmap with DeepGaze IIE.
  3. Generate a scanpath with DeepGaze III, seeded from IIE.
  4. Compute a metric pack (entropy, attention distribution, scanpath stats).
  5. Render a side-by-side comparison PNG + dump metrics to JSON.

Run:
    python experiments/eyetrackingmetrics.py
"""

from __future__ import annotations

import argparse
import json
import math
import time
from dataclasses import asdict, dataclass
from pathlib import Path

import matplotlib.pyplot as plt
import numpy as np
import torch
from matplotlib.patches import FancyArrowPatch
from PIL import Image
from scipy.ndimage import gaussian_filter

import deepgaze_pytorch

# ---------- paths ----------

REPO_ROOT = Path(__file__).resolve().parent.parent
EXP_DIR = REPO_ROOT / "experiments"
UI_DIR = EXP_DIR / "uis"
SHOT_DIR = EXP_DIR / "screenshots"
OUT_DIR = EXP_DIR / "outputs"

IMAGE_EXTS = {".png", ".jpg", ".jpeg", ".webp"}
HTML_EXTS = {".html", ".htm"}


def discover_uis(*dirs: Path) -> list[tuple[str, Path]]:
    """Find UI inputs across one or more directories. Accepts .html (screenshot first)
    or image files (use as-is). Deduplicated by stem (HTML wins over PNG of same name,
    since the HTML capture would overwrite the cached PNG anyway).
    Returned in stable alphabetical order so comparisons are reproducible."""
    by_stem: dict[str, Path] = {}
    for d in dirs:
        if not d.exists():
            continue
        for p in sorted(d.iterdir()):
            if not (p.is_file() and p.suffix.lower() in (IMAGE_EXTS | HTML_EXTS)):
                continue
            existing = by_stem.get(p.stem)
            if existing is None or (p.suffix.lower() in HTML_EXTS
                                    and existing.suffix.lower() in IMAGE_EXTS):
                by_stem[p.stem] = p
    return sorted(by_stem.items())

# ---------- config ----------

MAX_DIM = 1024          # downscale images so the long side is at most this
VIEWPORT = (1280, 800)  # browser viewport for screenshots
N_FIXATIONS = 10        # scanpath length
IOR_SIGMA = 60          # inhibition-of-return radius (px, in resized image coords)
IOR_STRENGTH = 4.0      # how strongly to suppress recent fixations in log-space

# ---------- screenshot ----------

def capture_screenshot(source: Path | str, out_path: Path) -> Path:
    """Take a screenshot of a local HTML file or URL with Playwright."""
    from playwright.sync_api import sync_playwright

    if isinstance(source, Path):
        url = source.resolve().as_uri()
    else:
        url = source

    out_path.parent.mkdir(parents=True, exist_ok=True)
    with sync_playwright() as p:
        browser = p.chromium.launch()
        ctx = browser.new_context(viewport={"width": VIEWPORT[0], "height": VIEWPORT[1]},
                                  device_scale_factor=1)
        page = ctx.new_page()
        page.goto(url, wait_until="networkidle")
        # take a viewport-only shot (above the fold) — saliency is about first impression
        page.screenshot(path=str(out_path), full_page=False)
        browser.close()
    return out_path


# ---------- saliency ----------

@dataclass
class SaliencyResult:
    name: str
    image: np.ndarray           # H,W,3 uint8 (resized)
    log_density: np.ndarray     # H,W float — DeepGaze IIE log-density
    density: np.ndarray         # H,W float — normalized probability density
    scanpath: list[tuple[float, float]]   # ordered list of (x, y) in pixel coords of resized image


def _load_and_resize(path: Path, max_dim: int = MAX_DIM) -> np.ndarray:
    img = Image.open(path).convert("RGB")
    w, h = img.size
    scale = min(max_dim / w, max_dim / h, 1.0)
    if scale < 1.0:
        img = img.resize((int(w * scale), int(h * scale)), Image.LANCZOS)
    return np.asarray(img)


def _centerbias(h: int, w: int, sigma_frac: float = 0.25) -> np.ndarray:
    """Centered 2D Gaussian log-density prior, broadcast to (H,W)."""
    ys = np.linspace(-1, 1, h)[:, None]
    xs = np.linspace(-1, 1, w)[None, :]
    sigma = sigma_frac * 2
    g = np.exp(-(xs**2 + ys**2) / (2 * sigma**2))
    g = g / g.sum()
    cb = np.log(g + 1e-12)
    # DeepGaze expects log-density normalized so exp(cb) sums to ~1
    cb = cb - np.log(np.exp(cb).sum())
    return cb


def _to_tensor(img: np.ndarray, device) -> torch.Tensor:
    # DeepGaze expects (B, C, H, W) float, range 0–255
    t = torch.tensor(img.transpose(2, 0, 1), dtype=torch.float32, device=device)
    return t.unsqueeze(0)


def _ior_penalty(h: int, w: int, fixations: list[tuple[float, float]],
                 sigma: float = IOR_SIGMA, strength: float = IOR_STRENGTH) -> np.ndarray:
    """Negative log-space mass placed at recent fixations to discourage revisits."""
    pen = np.zeros((h, w), dtype=np.float32)
    if not fixations:
        return pen
    ys, xs = np.mgrid[0:h, 0:w].astype(np.float32)
    for (fx, fy) in fixations:
        d2 = (xs - fx) ** 2 + (ys - fy) ** 2
        pen -= strength * np.exp(-d2 / (2 * sigma ** 2))
    return pen


def predict_saliency_and_scanpath(name: str, screenshot: Path,
                                  iie: deepgaze_pytorch.DeepGazeIIE,
                                  iii: deepgaze_pytorch.DeepGazeIII,
                                  device,
                                  n_fixations: int = N_FIXATIONS) -> SaliencyResult:
    img = _load_and_resize(screenshot, MAX_DIM)
    h, w = img.shape[:2]

    img_t = _to_tensor(img, device)
    cb = _centerbias(h, w)
    cb_t = torch.tensor(cb, dtype=torch.float32, device=device).unsqueeze(0)

    # IIE — single heatmap, no scanpath conditioning
    with torch.no_grad():
        log_density = iie(img_t, cb_t).squeeze().cpu().numpy()
    density = np.exp(log_density)
    density = density / density.sum()

    # First fixation = argmax of density (could also sample)
    fy0, fx0 = np.unravel_index(density.argmax(), density.shape)
    fixations: list[tuple[float, float]] = [(float(fx0), float(fy0))]

    # Pad initial history with the seed fixation repeated 4× so DeepGaze III has its required window
    hist_x = [float(fx0)] * 4
    hist_y = [float(fy0)] * 4

    for _ in range(n_fixations - 1):
        x_hist = torch.tensor([hist_x[-4:]], dtype=torch.float32, device=device)
        y_hist = torch.tensor([hist_y[-4:]], dtype=torch.float32, device=device)
        with torch.no_grad():
            ld_step = iii(img_t, cb_t, x_hist, y_hist).squeeze().cpu().numpy()
        # inhibition of return
        ld_step = ld_step + _ior_penalty(h, w, fixations)
        fy, fx = np.unravel_index(ld_step.argmax(), ld_step.shape)
        fixations.append((float(fx), float(fy)))
        hist_x.append(float(fx))
        hist_y.append(float(fy))

    return SaliencyResult(
        name=name, image=img,
        log_density=log_density.astype(np.float32),
        density=density.astype(np.float32),
        scanpath=fixations,
    )


# ---------- metrics ----------

@dataclass
class Metrics:
    entropy_bits: float
    gini: float
    top5pct_mass: float             # fraction of total mass in top 5% of pixels
    com_x_norm: float               # 0–1 (left → right)
    com_y_norm: float               # 0–1 (top → bottom)
    mass_top_third: float
    mass_mid_third: float
    mass_bot_third: float
    n_peaks: int
    scanpath_length_px: float
    mean_saccade_px: float
    first_fix_x_norm: float
    first_fix_y_norm: float
    peak_dominance: float           # max_saliency / second_peak_after_suppression (1.0 = no winner, >>1 = clear focal point)
    composition_spread: float       # 0–1, stddev of top peak positions across canvas


@dataclass
class AttentionScore:
    """Single 0–100 verdict + 0–1 sub-scores it's made of.

    Tuned to discriminate AI-slop landing pages from creative/engaging designs.
    Weights live in WEIGHTS — adjust there to retune without touching the math."""
    total: float
    hero_pull: float            # does the top of the page earn attention?
    focal_dominance: float      # one clear focal element vs a cluster of competing ones?
    composition_spread: float   # focal elements distributed deliberately vs clumped in one zone?
    scan_rhythm: float          # purposeful eye travel?


WEIGHTS = {
    "hero_pull":          0.35,
    "focal_dominance":    0.30,
    "composition_spread": 0.20,
    "scan_rhythm":        0.15,
}


def _clip01(x: float) -> float:
    return float(max(0.0, min(1.0, x)))


def compute_attention_score(m: Metrics) -> AttentionScore:
    # Hero pull: top-third earns attention; plateaus at 40% mass.
    hero = _clip01(m.mass_top_third / 0.40)

    # Focal dominance: how much does the top peak outshine the second?
    # dominance=1 (no winner)→0, dominance≥3 (clear focal point)→1.
    focal = _clip01((m.peak_dominance - 1.0) / 2.0)

    # Composition spread: how distributed are the focal elements across the canvas?
    # Already normalized 0–1 in compute_metrics.
    spread = _clip01(m.composition_spread)

    # Scan rhythm: Gaussian peaked at ~350 px mean-saccade in a 1024-wide image.
    scan = float(math.exp(-((m.mean_saccade_px - 350.0) / 250.0) ** 2 / 2.0))

    total = 100.0 * (
        WEIGHTS["hero_pull"]          * hero
        + WEIGHTS["focal_dominance"]    * focal
        + WEIGHTS["composition_spread"] * spread
        + WEIGHTS["scan_rhythm"]        * scan
    )
    return AttentionScore(
        total=round(total, 1),
        hero_pull=round(hero, 3),
        focal_dominance=round(focal, 3),
        composition_spread=round(spread, 3),
        scan_rhythm=round(scan, 3),
    )


def _gini(arr: np.ndarray) -> float:
    """Gini coefficient of a non-negative array. 0 = uniform, 1 = all mass at one point."""
    a = np.sort(arr.ravel())
    n = a.size
    if a.sum() == 0:
        return 0.0
    cum = np.cumsum(a)
    return float((n + 1 - 2 * (cum.sum() / cum[-1])) / n)


def _count_peaks(density: np.ndarray, min_distance: int = 40, top_k: int = 5) -> int:
    """Roughly count the number of distinct saliency peaks via repeated argmax + suppression."""
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
    """Ratio of global max saliency to the next-best peak, after suppressing the
    connected region above `rel_thresh × max` around the global peak.
    Parameter-free in pixels: uses flood-fill on the actual peak shape rather than an
    arbitrary radius, so it's robust to blur halos.
    1.0 = cluster of competitors, ≥3 = one clear focal element. Capped at 5.0."""
    from scipy.ndimage import label
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
    """Mass-weighted spatial variance of the saliency distribution, normalized so
    that a uniform distribution scores ~1.0 and a single delta scores 0.0.
    Robust to "scattered noise" — it integrates the whole density rather than picking
    discrete peaks. Cluster-of-CTAs slop UIs score low because most mass concentrates
    in one zone even if a few stray peaks lie elsewhere."""
    h, w = density.shape
    ys, xs = np.mgrid[0:h, 0:w].astype(np.float32)
    total = float(density.sum())
    if total == 0:
        return 0.0
    com_x = (xs * density).sum() / total
    com_y = (ys * density).sum() / total
    var_x = ((xs - com_x) ** 2 * density).sum() / total
    var_y = ((ys - com_y) ** 2 * density).sum() / total
    # variance of uniform over [0, L] is L^2 / 12; normalize so uniform → 1.0
    norm = ((var_x / (w * w / 12.0)) + (var_y / (h * h / 12.0))) / 2.0
    return float(min(math.sqrt(norm), 1.0))


def compute_metrics(res: SaliencyResult) -> Metrics:
    d = res.density
    h, w = d.shape

    # entropy in bits
    p = d.ravel()
    p = p[p > 0]
    entropy = float(-(p * np.log2(p)).sum())

    # concentration
    gini_v = _gini(d)
    flat_sorted = np.sort(d.ravel())[::-1]
    top5_n = max(1, int(0.05 * flat_sorted.size))
    top5 = float(flat_sorted[:top5_n].sum())

    # center of mass (normalized)
    ys, xs = np.mgrid[0:h, 0:w]
    com_x = float((xs * d).sum() / d.sum())
    com_y = float((ys * d).sum() / d.sum())
    com_x_n = com_x / w
    com_y_n = com_y / h

    # vertical distribution: top / mid / bottom thirds
    third = h // 3
    mass_top = float(d[:third].sum())
    mass_mid = float(d[third:2 * third].sum())
    mass_bot = float(d[2 * third:].sum())

    n_peaks = _count_peaks(d)

    # slop-vs-creative signals
    dominance = _peak_dominance(d)
    spread = _composition_spread(d)

    # scanpath stats
    pts = np.array(res.scanpath, dtype=np.float32)
    diffs = np.diff(pts, axis=0)
    saccades = np.linalg.norm(diffs, axis=1) if len(diffs) else np.array([0.0])
    sp_len = float(saccades.sum())
    sp_mean = float(saccades.mean())

    fx0, fy0 = res.scanpath[0]
    return Metrics(
        entropy_bits=round(entropy, 3),
        gini=round(gini_v, 3),
        top5pct_mass=round(top5, 3),
        com_x_norm=round(com_x_n, 3),
        com_y_norm=round(com_y_n, 3),
        mass_top_third=round(mass_top, 3),
        mass_mid_third=round(mass_mid, 3),
        mass_bot_third=round(mass_bot, 3),
        n_peaks=n_peaks,
        scanpath_length_px=round(sp_len, 1),
        mean_saccade_px=round(sp_mean, 1),
        first_fix_x_norm=round(fx0 / w, 3),
        first_fix_y_norm=round(fy0 / h, 3),
        peak_dominance=round(dominance, 3),
        composition_spread=round(spread, 3),
    )


# ---------- visualization ----------

def _heatmap_overlay(ax, img, density):
    # smooth and normalize for display
    sm = gaussian_filter(density, sigma=8)
    sm = (sm - sm.min()) / (sm.max() - sm.min() + 1e-12)
    ax.imshow(img)
    ax.imshow(sm, cmap="inferno", alpha=0.55)
    ax.set_xticks([]); ax.set_yticks([])


def _scanpath_overlay(ax, img, scanpath):
    ax.imshow(img)
    pts = scanpath
    # arrows between consecutive fixations
    for i in range(1, len(pts)):
        x0, y0 = pts[i - 1]
        x1, y1 = pts[i]
        arr = FancyArrowPatch((x0, y0), (x1, y1),
                              arrowstyle="-|>", mutation_scale=14,
                              color="#ff4d2e", linewidth=2, alpha=0.85,
                              shrinkA=10, shrinkB=10)
        ax.add_patch(arr)
    # numbered dots
    for i, (x, y) in enumerate(pts, start=1):
        ax.scatter([x], [y], s=420, color="white", edgecolor="#ff4d2e", linewidth=2.5, zorder=3)
        ax.text(x, y, str(i), ha="center", va="center", fontsize=10,
                color="#0f1115", fontweight="bold", zorder=4)
    ax.set_xticks([]); ax.set_yticks([])


def _score_text(s: AttentionScore) -> str:
    return (
        f"hero pull          {s.hero_pull:>5.2f}  x {WEIGHTS['hero_pull']:.2f}\n"
        f"focal dominance    {s.focal_dominance:>5.2f}  x {WEIGHTS['focal_dominance']:.2f}\n"
        f"composition spread {s.composition_spread:>5.2f}  x {WEIGHTS['composition_spread']:.2f}\n"
        f"scan rhythm        {s.scan_rhythm:>5.2f}  x {WEIGHTS['scan_rhythm']:.2f}"
    )


def _score_color(total: float) -> str:
    # red < 40 < orange < 60 < amber < 75 < green
    if total >= 75:   return "#22c55e"
    if total >= 60:   return "#eab308"
    if total >= 40:   return "#f97316"
    return "#ef4444"


def render_comparison(results: list[tuple[SaliencyResult, Metrics, AttentionScore]], out_path: Path):
    n = len(results)
    fig, axes = plt.subplots(3, n, figsize=(7 * n, 14), constrained_layout=True)
    if n == 1:
        axes = axes[:, None]
    for col, (res, _m, s) in enumerate(results):
        axes[0, col].imshow(res.image)
        axes[0, col].set_title(
            f"{res.name}    AttentionScore: {s.total:.0f}/100",
            fontsize=15, fontweight="bold", color=_score_color(s.total),
        )
        axes[0, col].set_xticks([]); axes[0, col].set_yticks([])

        _heatmap_overlay(axes[1, col], res.image, res.density)
        axes[1, col].set_title("DeepGaze IIE — predicted attention", fontsize=11)

        _scanpath_overlay(axes[2, col], res.image, res.scanpath)
        axes[2, col].set_title(
            f"DeepGaze III — scanpath\n\n{_score_text(s)}",
            fontsize=9, fontfamily="monospace", loc="left",
        )

    # Bottom-of-figure verdict line
    if n >= 2:
        ranking = sorted(results, key=lambda r: r[2].total, reverse=True)
        order = "   >   ".join(f"{r[0].name} ({r[2].total:.0f})" for r in ranking)
        fig.suptitle(
            f"AutoDesign saliency benchmark — v1\nRanking by AttentionScore:   {order}",
            fontsize=14, fontweight="bold",
        )
    else:
        fig.suptitle("AutoDesign saliency benchmark — v1", fontsize=14, fontweight="bold")

    out_path.parent.mkdir(parents=True, exist_ok=True)
    fig.savefig(out_path, dpi=120, bbox_inches="tight")
    plt.close(fig)


# ---------- orchestration ----------

def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--no-cache", action="store_true",
                        help="re-capture screenshots even if cached versions exist")
    parser.add_argument("--n-fixations", type=int, default=N_FIXATIONS)
    args = parser.parse_args()

    device = torch.device("cuda" if torch.cuda.is_available() else "cpu")
    print(f"[init] device = {device}")

    print("[init] loading DeepGaze IIE + III (first run downloads ~hundreds of MB)…")
    t0 = time.time()
    iie = deepgaze_pytorch.DeepGazeIIE(pretrained=True).to(device).eval()
    iii = deepgaze_pytorch.DeepGazeIII(pretrained=True).to(device).eval()
    print(f"[init] models ready in {time.time() - t0:.1f}s")

    SHOT_DIR.mkdir(parents=True, exist_ok=True)
    results: list[tuple[SaliencyResult, Metrics, AttentionScore]] = []
    per_ui_output: dict[str, dict] = {}

    uis = discover_uis(UI_DIR, SHOT_DIR)
    if not uis:
        raise SystemExit(
            f"No UI inputs found.\n"
            f"  Drop .html files in {UI_DIR}  (they'll be auto-screenshotted), or\n"
            f"  Drop .png/.jpg in        {SHOT_DIR}  (used as-is)."
        )
    print(f"[init] found {len(uis)} UI(s): {[n for n, _ in uis]}")

    for name, source in uis:
        if source.suffix.lower() in HTML_EXTS:
            shot = SHOT_DIR / f"{name}.png"
            if args.no_cache or not shot.exists():
                print(f"[capture] {name}: screenshotting {source.name}")
                capture_screenshot(source, shot)
            else:
                print(f"[capture] {name}: cached at {shot}")
        else:
            shot = source
            print(f"[capture] {name}: using image as-is ({source.name})")

        print(f"[predict] {name}: running IIE + III ({args.n_fixations} fixations)...")
        t = time.time()
        res = predict_saliency_and_scanpath(name, shot, iie, iii, device,
                                            n_fixations=args.n_fixations)
        m = compute_metrics(res)
        s = compute_attention_score(m)
        print(f"[predict] {name}: done in {time.time() - t:.1f}s   AttentionScore = {s.total:.1f}/100")
        results.append((res, m, s))
        per_ui_output[name] = {"score": asdict(s), "metrics": asdict(m)}

    out_subdir = OUT_DIR / time.strftime("%Y%m%d-%H%M%S")
    out_subdir.mkdir(parents=True, exist_ok=True)
    viz_path = out_subdir / "comparison.png"
    metrics_path = out_subdir / "metrics.json"

    render_comparison(results, viz_path)
    metrics_path.write_text(json.dumps(per_ui_output, indent=2))

    # plain-text ranking summary
    print()
    ranking = sorted(per_ui_output.items(), key=lambda kv: kv[1]["score"]["total"], reverse=True)
    print("=== AttentionScore ranking ===")
    for i, (name, payload) in enumerate(ranking, start=1):
        print(f"  {i}. {name:<20s} {payload['score']['total']:>5.1f}/100")
    print()
    print(f"[done] viz     -> {viz_path}")
    print(f"[done] metrics -> {metrics_path}")


if __name__ == "__main__":
    main()
