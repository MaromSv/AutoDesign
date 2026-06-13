"""Resolve the *real* brief and per-candidate prompt for scoring.

Two problems this fixes:

1. `autodesign.md`'s yaml `brief:` ships as a placeholder ("TODO: paste the design brief
   here, or leave blank to use the prose above."). When a run actually starts, the real
   brief is written to `<run>/brief.txt`. Scoring that reads `config["brief"]` blindly hands
   the signals the literal TODO text — which is why `prompt_consistency` reported "the brief
   was not included (marked as TODO)". `resolve_brief` walks: a real config brief → the
   run's `brief.txt` (found by walking up from the candidate dir) → the prose `## Brief`
   block at the top of `autodesign.md` → "".

2. Each candidate is generated to test a specific idea, embedded as the first line inside
   `<body>`: `<!-- hypothesis: ... -->`. That per-candidate directive is the actual prompt
   that produced the candidate. `extract_hypothesis` pulls it out so `prompt_consistency`
   can check the build against what *this* candidate was asked to be, not just the run goal.
"""

from __future__ import annotations

import re
from pathlib import Path

# A brief is "missing" if it's blank or still the shipped placeholder.
_PLACEHOLDER_PREFIXES = ("todo:", "todo ", "paste the design brief")
_HYPOTHESIS_RE = re.compile(r"<!--\s*hypothesis:\s*(.*?)\s*-->", re.IGNORECASE | re.DOTALL)
# The prose brief lives under a `## Brief` heading as a markdown blockquote (`> ...`).
_PROSE_BRIEF_RE = re.compile(
    r"^##\s+Brief\s*\n(?P<body>.*?)(?:\n##\s|\n```|\Z)",
    re.DOTALL | re.MULTILINE,
)


def is_placeholder(brief: str | None) -> bool:
    """True if `brief` is empty/whitespace or the shipped TODO placeholder."""
    s = (brief or "").strip().lower()
    if not s:
        return True
    return any(s.startswith(p) for p in _PLACEHOLDER_PREFIXES)


def extract_hypothesis(html: str | None) -> str:
    """Return the `<!-- hypothesis: ... -->` text from the candidate HTML, or ""."""
    if not html:
        return ""
    m = _HYPOTHESIS_RE.search(html)
    return " ".join(m.group(1).split()) if m else ""


def find_run_brief(candidate_dir: Path | str) -> str:
    """Walk up from a candidate dir to a run root holding `brief.txt`; return its text.

    A candidate lives at `<run>/gen-GGG/cand-NN/`, so `brief.txt` is a couple levels up.
    We check the dir and a bounded number of ancestors so this stays cheap and never
    wanders off to the filesystem root.
    """
    p = Path(candidate_dir).resolve()
    for d in (p, *p.parents[:6]):
        bf = d / "brief.txt"
        if bf.is_file():
            try:
                text = bf.read_text(encoding="utf-8").strip()
            except OSError:
                return ""
            if text and not is_placeholder(text):
                return text
    return ""


def prose_brief(config_path: str | Path = "autodesign.md") -> str:
    """Extract the `## Brief` blockquote from autodesign.md as plain text, or ""."""
    p = Path(config_path)
    if not p.is_file():
        return ""
    m = _PROSE_BRIEF_RE.search(p.read_text(encoding="utf-8"))
    if not m:
        return ""
    lines = []
    for line in m.group("body").splitlines():
        s = line.strip()
        if s.startswith(">"):
            s = s[1:].strip()
        if s:
            lines.append(s)
    text = " ".join(lines).strip()
    return "" if is_placeholder(text) else text


def resolve_brief(
    brief: str | None,
    candidate_dir: Path | str | None,
    config_path: str | Path = "autodesign.md",
) -> str:
    """Best real brief: a non-placeholder `brief` wins; else run `brief.txt`; else prose."""
    if not is_placeholder(brief):
        return (brief or "").strip()
    if candidate_dir is not None:
        run_brief = find_run_brief(candidate_dir)
        if run_brief:
            return run_brief
    return prose_brief(config_path)
