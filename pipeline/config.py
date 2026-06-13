"""Load the yaml control block from `autodesign.md`.

`autodesign.md` is the project's single control surface. The yaml block embedded
in it (a fenced ```yaml ... ``` section) is the only configuration this code
reads. Everything downstream — which signals run, their weights, model
assignments, capture settings — comes from that dict.
"""

from __future__ import annotations

import re
from pathlib import Path


_DEFAULT_CONFIG_PATH = Path("autodesign.md")
_YAML_BLOCK_RE = re.compile(
    r"```yaml\s*\n(?P<body>.*?)\n```",
    re.DOTALL,
)


def load_config(path: str | Path = _DEFAULT_CONFIG_PATH) -> dict:
    """Read `autodesign.md`, extract the first ```yaml block, return it parsed.

    Returns an empty dict if the file has no yaml block (so the scaffold still
    imports cleanly when the user has not filled in the config yet).

    TODO: validate the parsed dict against an expected schema (criteria weights
    sum sanity-check, models tier assignments present, capture viewport is 2
    ints, etc.) and raise a clear error pointing the user back at autodesign.md.
    """
    import yaml  # local import so tests can run without yaml when not loading config

    p = Path(path)
    if not p.exists():
        return {}
    text = p.read_text(encoding="utf-8")
    match = _YAML_BLOCK_RE.search(text)
    if not match:
        return {}
    parsed = yaml.safe_load(match.group("body")) or {}
    if not isinstance(parsed, dict):
        # TODO: tell the user the yaml block must be a mapping, not a list/scalar.
        return {}
    return parsed


def criteria_weights(config: dict) -> dict[str, float]:
    """Convenience accessor: `criteria` map with numeric coercion.

    Signals not present in this map are skipped by the benchmark combiner.
    """
    raw = config.get("criteria") or {}
    out: dict[str, float] = {}
    for k, v in raw.items():
        try:
            out[str(k)] = float(v)
        except (TypeError, ValueError):
            # TODO: surface this as a config error instead of silently dropping.
            continue
    return out
