"""Render a candidate HTML page to screenshots.

The real implementation will drive a headless browser (Playwright) at a configured
viewport, capture a screenshot at rest plus a handful of keyframes through any
declared animation, and write them as PNGs into `out_dir/frames/`.

This module is a stub. The signature is the contract — fill in the body later.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path


@dataclass
class CaptureResult:
    """Where screenshots were written and metadata about the capture pass."""

    frames_dir: Path
    frames: list[Path] = field(default_factory=list)
    viewport: tuple[int, int] = (1280, 800)
    skipped: str | None = None


def capture(
    html_path: Path,
    out_dir: Path,
    viewport: tuple[int, int] = (1280, 800),
    animation_seconds: float = 0.0,
    keyframes: list[float] | None = None,
) -> CaptureResult:
    """Render `html_path` and screenshot it into `out_dir/frames/`.

    Args:
        html_path: file:// URL or local path to the candidate HTML.
        out_dir: generation directory; frames are written under `out_dir/frames/`.
        viewport: (width, height) in CSS pixels.
        animation_seconds: total animation length to sample over.
        keyframes: fractional timestamps in [0.0, 1.0] to screenshot.

    Returns:
        A `CaptureResult` listing the frames written.

    TODO: implement with Playwright — launch chromium, set viewport, navigate to
    the html, wait for `load` and any web fonts, screenshot at t=0, then advance
    the page time for each keyframe and screenshot again. Write PNGs as
    `0000.png`, `0001.png`, ... in `out_dir/frames/`. Until then, return an
    empty `CaptureResult` so callers can plumb the data flow end-to-end.
    """
    _ = (html_path, viewport, animation_seconds, keyframes)
    frames_dir = Path(out_dir) / "frames"
    return CaptureResult(
        frames_dir=frames_dir,
        frames=[],
        viewport=viewport,
        skipped="not implemented",
    )
