"""Shared dataclasses passed between the engine and every signal.

These two types are the contract that lets signals plug in without coupling. A signal
receives a `CandidateContext` (uniform input) and returns a `SignalResult` (uniform
output). Add fields here only when a new signal genuinely needs them — keep the
surface small.
"""

from dataclasses import dataclass, field
from pathlib import Path


@dataclass
class CandidateContext:
    """Everything a signal might need about one design candidate. Uniform input.

    Every field is populated by the engine before signals run. Signals must not
    mutate the context — treat it as read-only.
    """

    candidate_dir: Path
    html_path: Path | None
    html_url: str | None
    frames: list[Path]
    code_text: str
    brief: str
    config: dict
    # TODO: add fields as new signals need them (focal_bbox, viewport, persona, etc.).
    # Keep additions backwards-compatible (default values) so older signals keep working.


@dataclass
class SignalResult:
    """Uniform output of every signal.

    `score` is on a 0-10 scale, or `None` if the signal was skipped (e.g. missing
    inputs, model unreachable, deliberately disabled). `details` is a free-form
    dict for signal-specific diagnostics — the dashboard renders it as-is.
    `skipped` is a short human-readable reason string, present iff `score is None`.
    """

    score: float | None
    details: dict = field(default_factory=dict)
    skipped: str | None = None
