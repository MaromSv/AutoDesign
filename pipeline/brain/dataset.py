"""Build a labeled brain-activity dataset: URLs -> screenshots -> parcel vectors.

A dataset lives in a directory with two text files of URLs (one per line, `#`
comments allowed):

    good.txt   # awwwards-quality sites
    bad.txt    # madewithlovable / AI-slop sites

`build` renders each URL to an at-rest screenshot (reusing `pipeline.capture`),
runs the TRIBE encoder, and writes `brain_dataset.npz` with arrays:

    X      (n, N_PARCELS)  predicted cortical parcel vectors
    y      (n,)            GOOD=1 / BAD=0
    urls   (n,)            source URL per row

Screenshots are cached under `<root>/shots/` so re-runs are cheap.
"""

from __future__ import annotations

import hashlib
import html
from pathlib import Path

import numpy as np

from pipeline import capture
from pipeline.brain import tribe_encoder
from pipeline.brain.classifier import BAD, GOOD


def read_url_list(path: str | Path) -> list[str]:
    """Read a URL-per-line file, ignoring blanks and `#` comments."""
    out: list[str] = []
    for line in Path(path).read_text(encoding="utf-8").splitlines():
        s = line.strip()
        if s and not s.startswith("#"):
            out.append(html.unescape(s))  # decode &amp; etc. from scraped URLs
    return out


def _shot_path(shots_dir: Path, url: str) -> Path:
    h = hashlib.sha1(url.encode("utf-8")).hexdigest()[:16]
    return shots_dir / f"{h}.png"


def capture_screenshot(url: str, shots_dir: Path, viewport=(1280, 800)) -> Path | None:
    """Render `url` to a single at-rest screenshot, cached by URL hash."""
    dest = _shot_path(shots_dir, url)
    if dest.exists():
        return dest
    # capture writes frames into <out_dir>/frames/0000.png; point out_dir at a temp
    # work dir per URL, then move the at-rest frame to the cache path.
    work = shots_dir / ("_work_" + dest.stem)
    res = capture.capture(url, work, viewport=viewport, animation_seconds=0.0, keyframes=[0.0])
    if res.skipped or not res.frames:
        return None
    shots_dir.mkdir(parents=True, exist_ok=True)
    Path(res.frames[0]).replace(dest)
    return dest


def build(root: str | Path, viewport=(1280, 800), verbose: bool = True) -> Path:
    """Build `<root>/brain_dataset.npz` from `<root>/good.txt` and `<root>/bad.txt`."""
    root = Path(root)
    shots_dir = root / "shots"
    pairs: list[tuple[str, int]] = []
    for fname, label in (("good.txt", GOOD), ("bad.txt", BAD)):
        fpath = root / fname
        if not fpath.exists():
            raise FileNotFoundError(f"missing URL list: {fpath}")
        for url in read_url_list(fpath):
            pairs.append((url, label))

    X, y, urls = [], [], []
    for url, label in pairs:
        shot = capture_screenshot(url, shots_dir, viewport=viewport)
        if shot is None:
            if verbose:
                print(f"  skip (no screenshot): {url}")
            continue
        try:
            vec = tribe_encoder.encode_image(shot)
        except Exception as exc:  # noqa: BLE001
            if verbose:
                print(f"  skip (encode failed: {exc}): {url}")
            continue
        X.append(vec)
        y.append(label)
        urls.append(url)
        if verbose:
            print(f"  ok [{'good' if label == GOOD else 'bad '}]: {url}")

    if not X:
        raise RuntimeError("no usable samples — is Playwright installed and are URLs reachable?")

    X = np.vstack(X)
    y = np.asarray(y, dtype=int)
    out = root / "brain_dataset.npz"
    np.savez(out, X=X, y=y, urls=np.asarray(urls, dtype=object),
             backend=tribe_encoder.active_backend())
    if verbose:
        print(f"\nwrote {out}  (X={X.shape}, good={int((y==GOOD).sum())}, bad={int((y==BAD).sum())})")
        print(f"encoder backend: {tribe_encoder.active_backend()}")
    return out
