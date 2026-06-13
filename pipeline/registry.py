"""Signal registry. Adding a signal = adding a file in `pipeline/signals/` and a
config entry. Nothing else changes.

Each `Signal` class self-registers via the `@register_signal` decorator at import
time. `pipeline/signals/__init__.py` imports every signal module, so importing the
package is enough to populate the registry.
"""

from __future__ import annotations

from typing import Any


# A "signal" is anything with a non-empty `key: str` and a `score(ctx) -> SignalResult`
# method. Kept as `Any` here to avoid pulling a Protocol module into the scaffold —
# the registry validates `key` at decorate time, which is the only invariant the
# combiner relies on.
_REGISTRY: dict[str, Any] = {}


def register_signal(cls):
    """Class decorator. Instantiates the class and registers it under `cls.key`.

    The decorated class is returned unchanged so it can still be subclassed or
    referenced by name. Re-registering the same key replaces the previous instance
    (later-imported modules win) — that is deliberate so tests can patch signals.

    TODO: validate that `key` is a non-empty str and warn on accidental re-register
    from a different module path (vs. the same module reloaded).
    """
    inst = cls()
    key = getattr(inst, "key", None)
    if not isinstance(key, str) or not key:
        raise ValueError(
            f"{cls.__name__} must define a non-empty `key: str` class attribute"
        )
    _REGISTRY[key] = inst
    return cls


def get_signals() -> dict[str, Any]:
    """Return a shallow copy of the registry so callers cannot mutate it in place."""
    return dict(_REGISTRY)


def clear_registry() -> None:
    """Test helper. Clears the registry so a fresh import populates it cleanly."""
    _REGISTRY.clear()
