"""Render a candidate HTML page to screenshots.

Drives a headless Chromium (Playwright) at the configured viewport, captures
the at-rest above-the-fold view, and writes it as a PNG into `out_dir/frames/`.

Animation keyframes are accepted as a parameter for API stability but not yet
sampled — the current implementation captures only the at-rest frame.
TODO: drive `page.evaluate("document.timeline.currentTime = ...")` to sample
keyframes through any declared animation.
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
        html_path: local path to the candidate HTML.
        out_dir: candidate directory; frames are written under `out_dir/frames/`.
        viewport: (width, height) in CSS pixels.
        animation_seconds: total animation length (currently unused, see TODO above).
        keyframes: fractional timestamps in [0, 1] (currently unused, see TODO above).

    Returns:
        `CaptureResult` listing the frames written. `skipped` is set if capture
        could not run (missing dependency, no html, etc.) — the loop tolerates
        skipped captures and downstream signals can still produce useful judgments.
    """
    _ = (animation_seconds, keyframes)
    frames_dir = Path(out_dir) / "frames"
    frames_dir.mkdir(parents=True, exist_ok=True)

    html_path = Path(html_path)
    if not html_path.exists():
        return CaptureResult(
            frames_dir=frames_dir, frames=[], viewport=viewport,
            skipped=f"html missing: {html_path}",
        )

    try:
        from playwright.sync_api import sync_playwright  # local import: optional dep
    except ImportError as e:
        return CaptureResult(
            frames_dir=frames_dir, frames=[], viewport=viewport,
            skipped=f"playwright not installed: {e}",
        )

    out_png = frames_dir / "0000.png"
    url = html_path.resolve().as_uri()
    try:
        with sync_playwright() as p:
            browser = p.chromium.launch()
            ctx = browser.new_context(
                viewport={"width": viewport[0], "height": viewport[1]},
                device_scale_factor=1,
            )
            page = ctx.new_page()
            page.goto(url, wait_until="networkidle")
            page.screenshot(path=str(out_png), full_page=False)
            browser.close()
    except Exception as e:
        return CaptureResult(
            frames_dir=frames_dir, frames=[], viewport=viewport,
            skipped=f"playwright error: {e}",
        )

    return CaptureResult(
        frames_dir=frames_dir, frames=[out_png], viewport=viewport, skipped=None,
    )
