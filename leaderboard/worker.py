"""Background scoring worker.

A single daemon thread that loops:
  1. Claim the next queued submission.
  2. Run the scorer (capture + signals).
  3. Mark done with the score, or failed with the exception text.

One-at-a-time on purpose — the saliency DeepGaze models and Playwright don't
parallelize cleanly inside a single process, and we want one URL's run to use
all available CPU/GPU rather than fight three others for it.
"""

from __future__ import annotations

import logging
import threading
import time
import traceback
from pathlib import Path
from typing import Any

from . import db
from . import scorer


log = logging.getLogger("leaderboard.worker")


class Worker:
    def __init__(
        self,
        db_path: Path,
        artifacts_root: Path,
        config: dict[str, Any],
        poll_interval: float = 2.0,
    ):
        self.db_path = db_path
        self.artifacts_root = artifacts_root
        self.config = config
        self.poll_interval = poll_interval
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None

    def start(self) -> None:
        if self._thread and self._thread.is_alive():
            return
        self._stop.clear()
        self._thread = threading.Thread(target=self._run, name="scorer", daemon=True)
        self._thread.start()
        log.info("worker started")

    def stop(self) -> None:
        self._stop.set()
        if self._thread:
            self._thread.join(timeout=5.0)

    def _run(self) -> None:
        while not self._stop.is_set():
            try:
                row = db.claim_next_queued(self.db_path)
            except Exception:
                log.exception("failed to claim next submission")
                time.sleep(self.poll_interval)
                continue

            if row is None:
                time.sleep(self.poll_interval)
                continue

            self._process(row)

    def _process(self, row: dict[str, Any]) -> None:
        sub_id = int(row["id"])
        url = row["url"]
        brief = row.get("brief", "") or ""
        out_root = self.artifacts_root / f"sub-{sub_id:06d}"
        log.info("scoring submission %s: %s", sub_id, url)
        try:
            record = scorer.score_url(
                url=url, out_root=out_root, config=self.config, brief=brief
            )
            scored = record.get("scored_criteria") or []
            if not scored:
                # All signals skipped (e.g. capture failed, no API key, …).
                # Treat that as a failure so the row shows up red in the UI.
                cap = record.get("capture_skipped") or ""
                raw = record.get("raw", {})
                reasons = [
                    f"{k}: {(raw.get(k) or {}).get('skipped')}"
                    for k in record.get("skipped_criteria", [])
                ]
                msg = cap or "; ".join(reasons) or "no signal produced a score"
                db.mark_failed(self.db_path, sub_id, msg)
                log.warning("submission %s not scored: %s", sub_id, msg)
                return

            combined = float(record.get("combined") or 0.0)
            artifacts_dir = record.get("artifacts_subdir", str(out_root))
            db.mark_done(self.db_path, sub_id, combined, record, artifacts_dir)
            log.info("submission %s done: %.2f", sub_id, combined)
        except Exception as exc:
            tb = traceback.format_exc(limit=4)
            log.exception("submission %s failed", sub_id)
            db.mark_failed(self.db_path, sub_id, f"{exc}\n{tb}")
