"""Auto-import every signal module so its `@register_signal` decorator runs.

Adding a new signal:
  1. Create `pipeline/signals/<name>.py` with a `@register_signal` class.
  2. Add the import below (one line — keep it alphabetical).
  3. Add the matching key + weight to the `criteria:` block in `autodesign.md`.

Nothing else changes.
"""

from pipeline.signals import saliency  # noqa: F401
from pipeline.signals import vlmjudge  # noqa: F401

__all__ = ["saliency", "vlmjudge"]
