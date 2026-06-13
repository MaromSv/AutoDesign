"""Auto-import every signal module so its `@register_signal` decorator runs.

Adding a new signal:
  1. Create `pipeline/signals/<name>.py` with a `@register_signal` class.
  2. Add the import below (one line — keep it alphabetical).
  3. Add the matching key + weight to the `criteria:` block in `autodesign.md`.

Nothing else changes.

NOTE: this `brain` branch includes brain_judge (heavy TRIBE-v2 deps). `main` does not.
"""

from pipeline.signals import brain_judge  # noqa: F401
from pipeline.signals import prompt_consistency  # noqa: F401
from pipeline.signals import saliency  # noqa: F401
from pipeline.signals import stress_test  # noqa: F401
from pipeline.signals import vlmjudge  # noqa: F401

__all__ = ["brain_judge", "prompt_consistency", "saliency", "stress_test", "vlmjudge"]
