"""Record a short scroll-through video of a web page as a TRIBE-v2 stimulus.

TRIBE-v2 ingests naturalistic video (it was trained on movies), so a static
screenshot is the wrong modality. A slow scroll down the page is a reasonable
"someone viewing this website" stimulus: it has motion, reveals the layout over
time, and gives TRIBE several seconds of frames to encode.

Uses Playwright's built-in video recording. Produces a `.webm` (chromium's native
format); `as_mp4` transcodes via imageio-ffmpeg if a downstream tool needs mp4.
Degrades gracefully: returns None if Playwright/chromium is unavailable.
"""

from __future__ import annotations

from pathlib import Path


def capture_scroll_video(
    url: str,
    dest: Path,
    viewport: tuple[int, int] = (1280, 800),
    duration_s: float = 6.0,
    steps: int = 30,
) -> Path | None:
    """Record `url` scrolling top→bottom over `duration_s` into `dest` (.webm).

    Returns the written video path, or None on any capture failure.
    """
    dest = Path(dest)
    dest.parent.mkdir(parents=True, exist_ok=True)
    try:
        from playwright.sync_api import sync_playwright
    except ImportError:
        return None

    try:
        with sync_playwright() as p:
            browser = p.chromium.launch()
            # record_video_dir makes Playwright save a .webm per page on close.
            context = browser.new_context(
                viewport={"width": int(viewport[0]), "height": int(viewport[1])},
                record_video_dir=str(dest.parent),
                record_video_size={"width": int(viewport[0]), "height": int(viewport[1])},
            )
            page = context.new_page()
            try:
                page.goto(url, wait_until="networkidle", timeout=30000)
            except Exception:
                page.goto(url, wait_until="load", timeout=30000)
            page.wait_for_timeout(400)

            # Smoothly scroll from top to bottom across the duration.
            try:
                total = page.evaluate("document.body.scrollHeight") or viewport[1]
            except Exception:
                total = viewport[1]
            reachable = max(0, int(total) - int(viewport[1]))
            per_step_ms = int((duration_s * 1000) / max(1, steps))
            for s in range(steps + 1):
                y = int(reachable * (s / steps))
                try:
                    page.evaluate(f"window.scrollTo(0, {y})")
                except Exception:
                    pass
                page.wait_for_timeout(per_step_ms)

            video = page.video
            context.close()  # finalizes the .webm
            browser.close()
            if video is None:
                return None
            src = Path(video.path())
            if src != dest:
                src.replace(dest)
            return dest if dest.exists() else None
    except Exception:  # noqa: BLE001 - launch/record failure -> skip
        return None


def capture_static_clip(
    url: str,
    dest: Path,
    viewport: tuple[int, int] = (1280, 800),
    n_frames: int = 10,
    duration_s: float = 6.0,
    animation_seconds: float = 10.0,
) -> Path | None:
    """Capture `n_frames` STATIC viewport screenshots (no scroll) and wrap them in a clip.

    This is the SAME stimulus the VLM judge sees: it reuses `pipeline.capture.capture`
    (fixed top-of-page viewport, no scrolling), so both signals judge the identical frames.
    TRIBE's runner reads a video *file*, so we encode the `n_frames` PNGs into a short clip
    at `dest` (spanning `duration_s` for the temporal axis). Returns the clip path, or None.

    No scroll, no ~150-frame video — just the 10 frames, held as a near-static clip.
    """
    from pipeline.capture import capture

    dest = Path(dest)
    dest.parent.mkdir(parents=True, exist_ok=True)
    work = dest.parent / f"{dest.stem}_frames"
    keyframes = [i / (n_frames - 1) for i in range(n_frames)] if n_frames > 1 else [0.0]

    res = capture(url, work, viewport=viewport,
                  animation_seconds=animation_seconds, keyframes=keyframes)
    if not res.frames:
        return None  # capture skipped (no Playwright/chromium) or page failed

    try:
        import subprocess

        import imageio_ffmpeg

        ffmpeg = imageio_ffmpeg.get_ffmpeg_exe()
        framerate = max(0.5, n_frames / max(0.1, duration_s))
        subprocess.run(
            [ffmpeg, "-y", "-framerate", f"{framerate}",
             "-i", str(res.frames_dir / "%04d.png"),
             "-c:v", "libx264", "-pix_fmt", "yuv420p", str(dest)],
            check=True, capture_output=True,
        )
        return dest if dest.exists() and dest.stat().st_size > 0 else None
    except Exception:  # noqa: BLE001 - ffmpeg/encode failure -> skip
        return None


def as_mp4(webm: Path, mp4: Path | None = None) -> Path | None:
    """Transcode a .webm to .mp4 using imageio-ffmpeg's bundled ffmpeg. None on failure."""
    webm = Path(webm)
    mp4 = Path(mp4) if mp4 else webm.with_suffix(".mp4")
    try:
        import subprocess

        import imageio_ffmpeg

        ffmpeg = imageio_ffmpeg.get_ffmpeg_exe()
        subprocess.run(
            [ffmpeg, "-y", "-i", str(webm), "-pix_fmt", "yuv420p", str(mp4)],
            check=True, capture_output=True,
        )
        return mp4 if mp4.exists() else None
    except Exception:  # noqa: BLE001
        return None
