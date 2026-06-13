"""Drive a rendered page and probe whether its interactions actually work.

`capture.py` only screenshots — it never clicks anything. This module is the
interaction half: it loads the page in headless Chromium (Playwright), enumerates the
interactive elements (links, buttons, role=button, submit/button inputs, onclick
handlers), then exercises each one and records what happened — did it navigate, did the
DOM change, did a dialog open, did it throw a JS error, or was it a dead no-op?

The output (`InteractionReport`) is plain data. The `stress_test` signal feeds it to a
Nemotron model to turn the raw observations into a 0-10 "do things work and behave
consistently" score. Keeping the probing here (deterministic) and the judgment there
(model) means the report is also useful on its own / in tests.

We reload the base page before each interaction so a navigation triggered by one element
doesn't contaminate the next — elements are addressed by a CSS path computed once up
front, so reloading and re-locating is reliable.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from urllib.parse import urlparse, urldefrag

# JS run once on first load: returns a stable descriptor (CSS path + label) for each
# interactive element, capped at MAX. We address elements by `selector` after reloads.
_ENUMERATE_JS = """
(MAX) => {
  const cssPath = (el) => {
    if (el.id) return '#' + CSS.escape(el.id);
    const parts = [];
    while (el && el.nodeType === 1 && el.tagName !== 'HTML' && el.tagName !== 'BODY') {
      let part = el.tagName.toLowerCase();
      const parent = el.parentElement;
      if (parent) {
        const sibs = Array.from(parent.children).filter(c => c.tagName === el.tagName);
        if (sibs.length > 1) part += ':nth-of-type(' + (sibs.indexOf(el) + 1) + ')';
      }
      parts.unshift(part);
      el = el.parentElement;
    }
    return parts.join(' > ');
  };
  const sel = 'a, button, [role=button], input[type=submit], input[type=button], [onclick]';
  const nodes = Array.from(document.querySelectorAll(sel));
  return nodes.slice(0, MAX).map((el) => ({
    selector: cssPath(el),
    tag: el.tagName.toLowerCase(),
    text: (el.innerText || el.value || el.getAttribute('aria-label') || '').trim().slice(0, 80),
    href: el.getAttribute('href'),
    type: el.getAttribute('type'),
  }));
}
"""


@dataclass
class Interaction:
    """One element exercised, and what happened when we did."""

    label: str
    tag: str
    selector: str
    href: str | None = None
    found: bool = True
    clicked: bool = False
    navigated: bool = False          # left the page (path/query changed)
    hash_only: bool = False          # in-page anchor (only the #fragment changed)
    dom_changed: bool = False        # innerHTML length moved meaningfully
    dialog: str | None = None        # alert/confirm/prompt text, if one opened
    new_console_errors: list[str] = field(default_factory=list)
    error: str | None = None         # exception while interacting
    dead: bool = False               # heuristic: clicked but produced no observable effect


@dataclass
class InteractionReport:
    """Everything the stress-test probe observed for one candidate."""

    target: str
    n_interactive: int = 0
    interactions: list[Interaction] = field(default_factory=list)
    load_console_errors: list[str] = field(default_factory=list)
    skipped: str | None = None

    def summary(self) -> dict:
        clicked = [i for i in self.interactions if i.clicked]
        dead = [i for i in clicked if i.dead]
        errored = [i for i in self.interactions if i.error]
        with_console = [i for i in clicked if i.new_console_errors]
        return {
            "n_interactive": self.n_interactive,
            "n_clicked": len(clicked),
            "n_dead": len(dead),
            "n_errored": len(errored),
            "n_with_console_errors": len(with_console),
            "n_load_console_errors": len(self.load_console_errors),
        }


def _to_target_url(target: str | Path) -> str | None:
    """Normalize a live URL / file URL / local path into a goto target."""
    s = str(target)
    if urlparse(s).scheme in ("http", "https", "file"):
        return s
    p = Path(s)
    return p.resolve().as_uri() if p.exists() else None


def _same_page(base: str, current: str) -> tuple[bool, bool]:
    """Return (navigated_away, hash_only_change) comparing current url to base."""
    b_nohash, _ = urldefrag(base)
    c_nohash, c_frag = urldefrag(current)
    if c_nohash != b_nohash:
        return True, False
    base_frag = urldefrag(base)[1]
    return False, (c_frag != base_frag and bool(c_frag))


def probe(
    target: str | Path,
    *,
    max_interactions: int = 25,
    settle_ms: int = 600,
    dom_change_threshold: int = 24,
) -> InteractionReport:
    """Load `target` and exercise up to `max_interactions` interactive elements.

    Returns an `InteractionReport`. Never raises for expected problems (Playwright
    missing, page unreachable, bad target) — those land in `report.skipped` so the
    caller can degrade gracefully.
    """
    url = _to_target_url(target)
    if url is None:
        return InteractionReport(target=str(target), skipped=f"target not found: {target}")

    try:
        from playwright.sync_api import sync_playwright  # local import: optional dep
    except ImportError as exc:
        return InteractionReport(target=url, skipped=f"playwright not installed: {exc}")

    report = InteractionReport(target=url)
    try:
        with sync_playwright() as p:
            browser = p.chromium.launch()
            ctx = browser.new_context(viewport={"width": 1280, "height": 800})
            page = ctx.new_page()

            # Auto-dismiss dialogs so a click that opens alert/confirm doesn't hang the run.
            dialog_box = {"text": None}
            page.on("dialog", lambda d: (dialog_box.__setitem__("text", d.message), d.dismiss()))

            # Track console errors as a running list; we slice per-interaction deltas.
            console_errors: list[str] = []
            page.on("console", lambda m: console_errors.append(m.text) if m.type == "error" else None)
            page.on("pageerror", lambda e: console_errors.append(str(e)))

            _goto(page, url)
            report.load_console_errors = list(console_errors)

            descriptors = page.evaluate(_ENUMERATE_JS, max_interactions) or []
            report.n_interactive = len(descriptors)

            for d in descriptors:
                report.interactions.append(
                    _exercise(page, url, d, console_errors, dialog_box,
                              settle_ms, dom_change_threshold)
                )

            page.close()
            ctx.close()
            browser.close()
    except Exception as exc:  # noqa: BLE001 - any transport/launch error -> graceful skip
        report.skipped = f"playwright error: {exc}"
    return report


def _goto(page, url: str) -> None:
    try:
        page.goto(url, wait_until="networkidle", timeout=15000)
    except Exception:  # noqa: BLE001 - pages that poll forever never hit networkidle
        page.goto(url, wait_until="load", timeout=15000)


def _exercise(page, base: str, d: dict, console_errors: list[str], dialog_box: dict,
              settle_ms: int, dom_change_threshold: int) -> Interaction:
    """Reload the base page, then click one element and record the effect."""
    label = d.get("text") or d.get("href") or d.get("selector") or "(unlabeled)"
    href = d.get("href")
    it = Interaction(label=label, tag=d.get("tag", ""), selector=d.get("selector", ""),
                     href=href)

    # Anchors pointing nowhere are dead by definition — no need to click.
    if it.tag == "a" and (href is None or href.strip() in ("", "#", "javascript:void(0)",
                                                            "javascript:;")):
        it.dead = True
        return it

    try:
        _goto(page, base)
        before_errs = len(console_errors)
        dialog_box["text"] = None
        before_dom = page.evaluate(
            "() => document.body ? document.body.innerHTML.length : 0"
        )

        el = page.query_selector(it.selector)
        if el is None:
            it.found = False
            return it

        try:
            el.click(timeout=3000)
            it.clicked = True
        except Exception as exc:  # noqa: BLE001 - obstructed / detached / timeout
            it.error = f"click failed: {str(exc).splitlines()[0][:160]}"
            return it

        page.wait_for_timeout(settle_ms)

        current = page.url
        it.navigated, it.hash_only = _same_page(base, current)
        it.dialog = dialog_box["text"]
        it.new_console_errors = console_errors[before_errs:][:5]

        if not it.navigated:
            after_dom = page.evaluate(
                "() => document.body ? document.body.innerHTML.length : 0"
            )
            it.dom_changed = abs(int(after_dom) - int(before_dom)) >= dom_change_threshold

        # Dead = a click that produced nothing observable: no nav, no in-page anchor,
        # no DOM mutation, no dialog. (A new console error still counts as "did
        # something", but it'll be flagged separately as a broken interaction.)
        it.dead = not (it.navigated or it.hash_only or it.dom_changed
                       or it.dialog or it.new_console_errors)
    except Exception as exc:  # noqa: BLE001
        it.error = f"probe error: {str(exc).splitlines()[0][:160]}"
    return it
