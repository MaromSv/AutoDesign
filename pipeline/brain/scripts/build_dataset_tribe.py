"""Build a brain dataset using the REAL Meta TRIBE-v2 model (not the fallback).

Pipeline per URL:  10 static screenshots (Playwright, no scroll) -> short clip
->  TRIBE-v2 cortical prediction (~20k fsaverage5 vertices, time-averaged) -> one vector.
The stimulus is the SAME static capture the VLM judge uses — not a scroll-through.

TRIBE runs in its own Python 3.11 venv (it pins torch<2.7, no 3.14 wheels), so we
shell out to `external/tribev2/tribe_runner.py` with a JSON manifest of videos and
read back an .npz of vectors. Videos and vectors are cached so re-runs are cheap.

    python -m pipeline.brain.scripts.build_dataset_tribe --root data/brain

Writes `<root>/brain_dataset_tribe.npz` (X, y, urls, backend="tribev2"). Train the
classifier on it exactly as with the fallback dataset:

    python -m pipeline.brain.scripts.train --root data/brain \
        --dataset brain_dataset_tribe.npz --compare
"""

from __future__ import annotations

import argparse
import json
import os
import subprocess
from pathlib import Path

import numpy as np

from pipeline.brain.classifier import BAD, GOOD
from pipeline.brain.dataset import read_url_list
from pipeline.brain.video_capture import capture_static_clip

# Project root: pipeline/brain/scripts/build_dataset_tribe.py -> up 3.
_ROOT = Path(__file__).resolve().parents[3]
TRIBE_DIR = Path(os.environ.get("TRIBE_DIR", _ROOT / "external" / "tribev2"))
TRIBE_PYTHON = Path(os.environ.get("TRIBE_PYTHON", TRIBE_DIR / ".venv" / "bin" / "python"))
TRIBE_RUNNER = TRIBE_DIR / "tribe_runner.py"


def _video_for(url: str, videos_dir: Path, duration_s: float) -> Path | None:
    import hashlib

    h = hashlib.sha1(url.encode()).hexdigest()[:16]
    dest = videos_dir / f"{h}.mp4"
    if dest.exists() and dest.stat().st_size > 0:
        return dest
    # 10 STATIC viewport screenshots (no scroll) — the same stimulus the VLM judge
    # sees — wrapped into a short clip for TRIBE. Not a 150-frame scroll-through.
    return capture_static_clip(url, dest, n_frames=10, duration_s=duration_s)


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--root", default="data/brain")
    ap.add_argument("--duration", type=float, default=6.0, help="length of the static clip (s)")
    ap.add_argument("--limit", type=int, default=0, help="cap URLs per class (0 = all)")
    args = ap.parse_args()

    root = Path(args.root)
    videos_dir = root / "videos"
    videos_dir.mkdir(parents=True, exist_ok=True)

    if not TRIBE_PYTHON.exists():
        raise SystemExit(f"TRIBE venv python not found at {TRIBE_PYTHON} — see pipeline/brain/README.md")

    # 1. collect labeled URLs
    pairs: list[tuple[str, int]] = []
    for fname, label in (("good.txt", GOOD), ("bad.txt", BAD)):
        urls = read_url_list(root / fname)
        if args.limit:
            urls = urls[: args.limit]
        pairs += [(u, label) for u in urls]

    # 2. record (or reuse) a scroll video per URL
    manifest: dict[str, str] = {}
    labels: dict[str, int] = {}
    for url, label in pairs:
        vid = _video_for(url, videos_dir, args.duration)
        if vid is None:
            print(f"  skip (no video): {url}")
            continue
        manifest[url] = str(vid.resolve())
        labels[url] = label
        print(f"  video ok [{'good' if label == GOOD else 'bad '}]: {url}")

    if not manifest:
        raise SystemExit("no videos captured — is Playwright/chromium installed?")

    manifest_path = root / "tribe_manifest.json"
    vectors_path = root / "tribe_vectors.npz"
    manifest_path.write_text(json.dumps(manifest))

    # 3. run TRIBE-v2 in its own venv over the whole manifest (model loads once)
    print(f"\nrunning TRIBE-v2 on {len(manifest)} videos via {TRIBE_PYTHON} ...")
    env = dict(os.environ, HF_HUB_DISABLE_IMPLICIT_TOKEN="1", HF_TOKEN="")
    subprocess.run(
        [str(TRIBE_PYTHON), str(TRIBE_RUNNER),
         "--manifest", str(manifest_path), "--out", str(vectors_path),
         "--cache", str(TRIBE_DIR / "cache")],
        cwd=str(TRIBE_DIR), env=env, check=True,
    )

    # 4. assemble X / y / urls in the labeled order, keeping only what TRIBE returned
    vecs = np.load(vectors_path)
    X, y, urls = [], [], []
    for url, label in labels.items():
        if url in vecs.files:
            X.append(vecs[url])
            y.append(label)
            urls.append(url)
    X = np.vstack(X)
    y = np.asarray(y, dtype=int)
    out = root / "brain_dataset_tribe.npz"
    np.savez(out, X=X, y=y, urls=np.asarray(urls, dtype=object), backend="tribev2")
    print(f"\nwrote {out}  (X={X.shape}, good={int((y==GOOD).sum())}, bad={int((y==BAD).sum())})")
    print("backend: tribev2 (real Meta TRIBE-v2 cortical predictions)")


if __name__ == "__main__":
    main()
