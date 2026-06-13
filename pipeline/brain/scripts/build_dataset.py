"""Render the harvested URLs to screenshots and encode them to brain vectors.

    python -m pipeline.brain.scripts.build_dataset --root data/brain

Reads `<root>/good.txt` + `<root>/bad.txt`, writes `<root>/brain_dataset.npz`.
Requires Playwright (`pip install playwright && playwright install chromium`).
"""

from __future__ import annotations

import argparse

from pipeline.brain import dataset


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--root", default="data/brain")
    ap.add_argument("--width", type=int, default=1280)
    ap.add_argument("--height", type=int, default=800)
    args = ap.parse_args()
    dataset.build(args.root, viewport=(args.width, args.height))


if __name__ == "__main__":
    main()
