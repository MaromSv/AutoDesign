"""Render a candidate HTML page to screenshots + a video.

Drives a headless Chromium (Playwright) at the configured viewport:

  1. Open the page, wait for `networkidle` so initial paint is settled.
  2. Step through `keyframes` (fractions of `animation_seconds`) and screenshot
     each one — gives the saliency signal an ordered list rest → … → settled.
  3. Record the whole session as an autoplaying webm via Playwright's
     `record_video_dir`, then rename it to `animation.webm` in the candidate dir.

Why time-based scrubbing (wait_for_timeout) rather than
`document.timeline.currentTime`: timeline scrubbing only works for declarative
CSS / Web Animations API animations, not requestAnimationFrame loops or any
animation driven by JS. Letting the clock run captures any animation kind
without making assumptions about how it was authored.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path

from pipeline.artifacts import VIDEO_FILENAME


@dataclass
class CaptureResult:
    """Where screenshots + video were written and metadata about the capture pass."""

    frames_dir: Path
    frames: list[Path] = field(default_factory=list)
    video: Path | None = None
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
        out_dir: candidate directory; frames are written under `out_dir/frames/`
            and the recorded video is moved to `out_dir/animation.webm`.
        viewport: (width, height) in CSS pixels.
        animation_seconds: total animation length. Used to convert each entry
            in `keyframes` (fractions of total) into a wall-clock wait.
        keyframes: fractional timestamps in [0, 1] at which to screenshot.
            Default `[0.0, 1.0]` (entry + settled). The list is sorted and
            deduplicated before sampling.

    Returns:
        `CaptureResult` listing the frames (in keyframe order) and the recorded
        video path. `skipped` is set if capture could not run (missing
        dependency, no html, etc.) — the loop tolerates skipped captures and
        downstream signals can still produce useful judgments.
    """
    frames_dir = Path(out_dir) / "frames"
    frames_dir.mkdir(parents=True, exist_ok=True)
    out_dir = Path(out_dir)

    html_path = Path(html_path)
    if not html_path.exists():
        return CaptureResult(
            frames_dir=frames_dir, frames=[], viewport=viewport,
            skipped=f"html missing: {html_path}",
        )

    # Normalize keyframes: keep values in [0, 1], dedupe, sort. Always include
    # 0.0 (entry) and 1.0 (settled) so the saliency animation_focus subscore
    # has a meaningful pair to compare.
    if not keyframes:
        keyframes = [0.0, 1.0]
    kfs = sorted({max(0.0, min(1.0, float(k))) for k in keyframes})
    if kfs[0] > 0.0:
        kfs = [0.0, *kfs]
    if kfs[-1] < 1.0:
        kfs = [*kfs, 1.0]

    try:
        from playwright.sync_api import sync_playwright  # local import: optional dep
    except ImportError as e:
        return CaptureResult(
            frames_dir=frames_dir, frames=[], viewport=viewport,
            skipped=f"playwright not installed: {e}",
        )

    url = html_path.resolve().as_uri()
    frames: list[Path] = []
    video_target = out_dir / VIDEO_FILENAME
    recorded_video_src: Path | None = None
    total_ms = max(0.0, float(animation_seconds)) * 1000.0

    try:
        with sync_playwright() as p:
            browser = p.chromium.launch()
            ctx = browser.new_context(
                viewport={"width": viewport[0], "height": viewport[1]},
                device_scale_factor=1,
                # Record the whole session. Playwright writes a temp webm into
                # this dir and finalizes it when the context closes.
                record_video_dir=str(out_dir),
                record_video_size={"width": viewport[0], "height": viewport[1]},
            )
            page = ctx.new_page()
            page.goto(url, wait_until="networkidle")

            # Wall-clock walk through the keyframes. Track elapsed time so each
            # `wait_for_timeout` is the delta since the last screenshot.
            elapsed_ms = 0.0
            for i, kf in enumerate(kfs):
                target_ms = kf * total_ms
                wait_ms = target_ms - elapsed_ms
                if wait_ms > 0:
                    page.wait_for_timeout(wait_ms)
                    elapsed_ms = target_ms
                frame_path = frames_dir / f"{i:04d}.png"
                page.screenshot(path=str(frame_path), full_page=False)
                frames.append(frame_path)

            # Capture video path BEFORE closing — Playwright guarantees the
            # file exists at this path once the context is closed.
            if page.video:
                recorded_video_src = Path(page.video.path())
            page.close()
            ctx.close()
            browser.close()
    except Exception as e:
        return CaptureResult(
            frames_dir=frames_dir, frames=frames, viewport=viewport,
            skipped=f"playwright error: {e}",
        )

    # Move the recorded webm to a deterministic filename so the dashboard
    # and the engine can find it without globbing.
    video_path: Path | None = None
    if recorded_video_src and recorded_video_src.exists():
        try:
            if video_target.exists():
                video_target.unlink()
            recorded_video_src.rename(video_target)
            video_path = video_target
        except OSError:
            # Renaming across volumes can fail — fall back to leaving the
            # auto-named webm in place. Better to report nothing than crash.
            video_path = recorded_video_src if recorded_video_src.exists() else None

    return CaptureResult(
        frames_dir=frames_dir, frames=frames, video=video_path,
        viewport=viewport, skipped=None,
    )
