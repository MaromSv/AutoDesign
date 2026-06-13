"""StressTestSignal — score whether the site WORKS by letting subagents explore it.

This is the agentic stress test. Instead of one fixed pass that clicks every element,
several Nemotron *subagents* — each a persona with a goal (a first-time visitor chasing
the main CTA, a link auditor, a form tester) — autonomously drive a real headless browser
via tool calls (`pipeline.browser_agent`): they look, click, type, read, adapt, and report
structured findings. Each persona runs in its own fresh browser context; their findings are
merged into one 0-10 behavioral score plus a combined issue list.

Scope (no overlap with vlm_judge/saliency): this judges FUNCTION + BEHAVIORAL CONSISTENCY
only — do controls work, do similar controls behave alike — never looks, copy, or content.

Degradation ladder:
  - No renderable target -> skip.
  - Nemotron unavailable (no key/SDK) -> deterministic probe heuristic (clicks everything),
    so the criterion still contributes a behavioral score.
  - Playwright unavailable -> skip.
  - Nemotron + Playwright present -> multi-persona subagent exploration (the real path).
"""

from __future__ import annotations

from pathlib import Path

from pipeline.context import CandidateContext, SignalResult
from pipeline.interact import InteractionReport, probe
from pipeline.registry import register_signal
from pipeline.signals import _nemotron

# Default subagent personas. Each explores with a different goal so coverage is broad:
# the happy path, exhaustive link auditing, and form/widget interaction. Override or extend
# via `stress_test.personas` in autodesign.md.
DEFAULT_PERSONAS: list[dict] = [
    {"name": "first-time visitor",
     "goal": "Act like a brand-new visitor. Find the page's primary call-to-action (sign up, "
             "buy, play, get started, etc.) and try to actually follow it through. Report "
             "whether the main action works end to end."},
    {"name": "link auditor",
     "goal": "Methodically click through the navigation and every link/button you can find. "
             "Report any that are dead, broken, error out, or behave inconsistently with "
             "similar controls."},
    {"name": "form tester",
     "goal": "Find any forms, inputs, or interactive widgets and try to use them — type text, "
             "submit, toggle. Report whether they accept input and respond sensibly, and any "
             "that silently fail."},
]

# Per-persona severity penalties applied to its score.
_SEVERITY_PENALTY = {"critical": 3.0, "high": 2.5, "medium": 1.2, "low": 0.4}


@register_signal
class StressTestSignal:
    key = "stress_test"

    def score(self, ctx: CandidateContext) -> SignalResult:
        target = _resolve_target(ctx)
        if target is None:
            return SignalResult(score=None, skipped="no renderable target (url or html) to stress-test")

        cfg = (ctx.config or {}).get("stress_test") or {}

        # No Nemotron -> deterministic probe so the signal still yields a behavioral score.
        if not _nemotron.available(ctx.config):
            return _heuristic_fallback(target, cfg, note="nemotron unavailable — deterministic probe")

        results = _run_personas(target, _load_personas(cfg), ctx.config, cfg)
        if results is None:
            # Playwright couldn't run the agents; the probe needs it too, so this is a skip.
            return SignalResult(score=None, skipped="playwright unavailable for agentic stress test")
        results = [r for r in results if r is not None]
        if not results:
            return SignalResult(score=None, skipped="no agent sessions ran")

        overall, details = _merge(results)
        details["target"] = target
        details["model"] = _nemotron.resolve_model(ctx.config, cfg.get("model"))
        if overall is None:
            return SignalResult(score=None, skipped="agents produced no usable findings", details=details)
        return SignalResult(score=overall, details=details)


# --------------------------------------------------------------------------- target / personas
def _resolve_target(ctx: CandidateContext) -> str | None:
    """Prefer a live URL; else the captured/served DOM; else the source html file."""
    if ctx.html_url and ctx.html_url.startswith(("http://", "https://")):
        return ctx.html_url
    page_html = Path(ctx.candidate_dir) / "page.html"
    if page_html.exists():
        return str(page_html)
    if ctx.html_path and Path(ctx.html_path).exists():
        return str(ctx.html_path)
    return None


def _load_personas(cfg: dict) -> list[dict]:
    raw = cfg.get("personas")
    if not isinstance(raw, list) or not raw:
        return DEFAULT_PERSONAS
    personas = []
    for i, p in enumerate(raw):
        if isinstance(p, dict) and p.get("goal"):
            personas.append({"name": str(p.get("name", f"persona-{i}")), "goal": str(p["goal"])})
    return personas or DEFAULT_PERSONAS


# --------------------------------------------------------------------------- agentic run
def _run_personas(target: str, personas: list[dict], config: dict, cfg: dict):
    """Run each persona as a subagent in its own browser context. Sequential because the
    sync Playwright API is not safe to share across threads. Returns a list of AgentResult,
    or None if Playwright itself can't run (so the caller can skip)."""
    try:
        from playwright.sync_api import sync_playwright  # local import: optional dep
    except ImportError:
        return None

    from pipeline.browser_agent import AgentResult, BrowserTools, nemotron_model_call, run_agent
    from pipeline.interact import _goto, _to_target_url

    url = _to_target_url(target)
    if url is None:
        return []

    max_steps = int(cfg.get("max_steps", 12))
    max_interactions = int(cfg.get("max_interactions", 25))
    settle_ms = int(cfg.get("settle_ms", 600))
    agent_max_tokens = int(cfg.get("agent_max_tokens", 1024))

    results: list = []
    try:
        with sync_playwright() as p:
            browser = p.chromium.launch()
            for persona in personas:
                console: list[str] = []
                dialog = {"text": None}
                context = browser.new_context(viewport={"width": 1280, "height": 800})
                page = context.new_page()
                page.on("console", lambda m, c=console: c.append(m.text) if m.type == "error" else None)
                page.on("pageerror", lambda e, c=console: c.append(str(e)))
                page.on("dialog", lambda d, box=dialog: (box.__setitem__("text", d.message), d.dismiss()))
                try:
                    _goto(page, url)
                except Exception as exc:  # noqa: BLE001
                    results.append(AgentResult(persona, None, [], "error", f"could not load page: {exc}"))
                    context.close()
                    continue
                tools = BrowserTools(page, console, dialog, max_interactions=max_interactions,
                                     settle_ms=settle_ms)
                caller = nemotron_model_call(config, cfg.get("model"), agent_max_tokens)
                res = run_agent(tools, caller, persona, max_steps=max_steps)
                res.session = tools.session_summary()
                results.append(res)
                context.close()
            browser.close()
    except Exception:  # noqa: BLE001 - launch/teardown failure
        if not results:
            return None
    return results


# --------------------------------------------------------------------------- merge / scoring
def _merge(results: list) -> tuple[float | None, dict]:
    """Merge subagent findings into one score + combined details."""
    per: list[dict] = []
    issues: list[dict] = []
    for r in results:
        f = r.findings or {}
        s = _persona_score(r)
        per.append({
            "persona": r.persona.get("name"),
            "score": s,
            "goal_achieved": bool(f.get("goal_achieved")),
            "summary": str(f.get("summary", "")).strip(),
            "stopped": r.stopped,
            "steps": len(r.steps),
            "session": r.session,
            "error": r.error,
        })
        for it in _norm_issues(f.get("issues")):
            issues.append({**it, "persona": r.persona.get("name")})

    if not per:
        return None, {"mode": "subagents", "n_personas": 0, "personas": [], "issues": []}
    overall = round(sum(p["score"] for p in per) / len(per), 2)
    details = {"mode": "subagents", "n_personas": len(per), "personas": per, "issues": issues}
    return overall, details


def _persona_score(r) -> float:
    """0-10 for one subagent session: goal achievement minus issue/defect penalties."""
    f = r.findings or {}
    achieved = bool(f.get("goal_achieved"))
    issues = _norm_issues(f.get("issues"))
    sess = r.session or {}

    base = 9.5 if (achieved and not issues) else (7.0 if achieved else 3.5)
    # An agent that never finished (timed out / errored) without achieving the goal couldn't
    # confirm the site works — cap it low.
    if r.stopped in ("max_steps", "error") and not achieved:
        base = min(base, 3.0)

    penalty = sum(_SEVERITY_PENALTY.get(it["severity"], 1.2) for it in issues)
    penalty += min(2.0, 0.5 * int(sess.get("dead_clicks", 0)))
    penalty += min(2.0, 0.7 * int(sess.get("console_errors", 0)))
    return round(max(0.0, min(10.0, base - penalty)), 2)


def _norm_issues(v) -> list[dict]:
    """Normalize an issues list (dicts or bare strings) to [{severity, description}]."""
    out: list[dict] = []
    if not isinstance(v, list):
        return out
    for it in v[:20]:
        if isinstance(it, dict):
            desc = str(it.get("description") or it.get("issue") or "").strip()
            if desc:
                sev = str(it.get("severity", "medium")).strip().lower()
                out.append({"severity": sev if sev in _SEVERITY_PENALTY else "medium",
                            "description": desc})
        elif str(it).strip():
            out.append({"severity": "medium", "description": str(it).strip()})
    return out


# --------------------------------------------------------------------------- heuristic fallback
def _heuristic_fallback(target: str, cfg: dict, *, note: str) -> SignalResult:
    """Deterministic probe (clicks every element) when no Nemotron is available."""
    report = probe(target, max_interactions=int(cfg.get("max_interactions", 25)),
                   settle_ms=int(cfg.get("settle_ms", 600)))
    if report.skipped:
        return SignalResult(score=None, skipped=report.skipped)
    if report.n_interactive == 0:
        return SignalResult(score=None, skipped="no interactive elements found on the page",
                            details={"summary": report.summary()})
    score = _heuristic_score(report)
    return SignalResult(score=score, details={
        "mode": "heuristic-probe", "note": note, "target": report.target,
        "summary": report.summary(),
        "interactions": [_interaction_dict(i) for i in report.interactions],
    })


def _interaction_dict(i) -> dict:
    return {
        "label": i.label, "tag": i.tag, "href": i.href, "found": i.found,
        "clicked": i.clicked, "navigated": i.navigated, "hash_only": i.hash_only,
        "dom_changed": i.dom_changed, "dialog": i.dialog,
        "console_errors": i.new_console_errors, "error": i.error, "dead": i.dead,
    }


def _heuristic_score(report: InteractionReport) -> float:
    """Working fraction of clicked controls, penalized for JS/console errors. 0-10."""
    s = report.summary()
    clicked = s["n_clicked"]
    if clicked == 0:
        return 0.0
    working = clicked - s["n_dead"]
    base = 10.0 * working / clicked
    defects = s["n_errored"] + s["n_with_console_errors"]
    penalty = min(base, 1.5 * defects)
    if s["n_load_console_errors"]:
        penalty += min(2.0, 0.5 * s["n_load_console_errors"])
    return round(max(0.0, base - penalty), 2)
