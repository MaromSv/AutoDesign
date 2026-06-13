"""Agentic browser exploration: a model drives a real browser via tool calls.

This is the "subagent" half of the stress test. Instead of a fixed script that clicks
every element once (`pipeline.interact.probe`), here a Nemotron model is given a persona +
goal and a set of browser TOOLS (list_interactive, click, type_text, read_page, go_back)
and explores the site step by step — look, act, observe, adapt — until it achieves the
goal or gives up, then reports structured findings via a `finish` tool.

The loop (`run_agent`) is provider- and browser-agnostic so it can be unit-tested offline:

  - `tools` is any object exposing the tool methods; `BrowserTools` is the live Playwright
    implementation, but tests pass a fake in-memory one.
  - `model_call(messages) -> AssistantTurn` is injected; `nemotron_model_call` adapts the
    Nebius/Nemotron tool-calling API, but tests pass a scripted function.

`stress_test` runs several personas (each its own browser context) and merges their findings.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field

from pipeline.interact import _ENUMERATE_JS, _goto, _same_page  # reuse the probe's primitives

# ----------------------------------------------------------------- tool schemas (OpenAI fmt)
TOOL_SCHEMAS: list[dict] = [
    {
        "type": "function",
        "function": {
            "name": "list_interactive",
            "description": "List the clickable/interactive elements on the CURRENT page, each "
                           "with a stable integer index you pass to click/type_text. Call this "
                           "first, and again after the page changes.",
            "parameters": {"type": "object", "properties": {}},
        },
    },
    {
        "type": "function",
        "function": {
            "name": "click",
            "description": "Click an interactive element by its index from the latest "
                           "list_interactive (or a CSS selector). Returns what happened: "
                           "navigation, DOM change, dialog, console errors, or a dead no-op.",
            "parameters": {
                "type": "object",
                "properties": {
                    "index": {"type": "integer", "description": "index from list_interactive"},
                    "selector": {"type": "string", "description": "CSS selector (alternative to index)"},
                },
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "type_text",
            "description": "Type text into an input/textarea by index or selector.",
            "parameters": {
                "type": "object",
                "properties": {
                    "index": {"type": "integer"},
                    "selector": {"type": "string"},
                    "text": {"type": "string"},
                },
                "required": ["text"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "read_page",
            "description": "Read the current page's URL, title, and visible text so you can "
                           "decide what to do next.",
            "parameters": {"type": "object", "properties": {}},
        },
    },
    {
        "type": "function",
        "function": {
            "name": "go_back",
            "description": "Navigate back to the previous page.",
            "parameters": {"type": "object", "properties": {}},
        },
    },
    {
        "type": "function",
        "function": {
            "name": "finish",
            "description": "End the session and report findings. Call this when you have "
                           "achieved the goal or are confident you cannot.",
            "parameters": {
                "type": "object",
                "properties": {
                    "goal_achieved": {"type": "boolean",
                                      "description": "did you accomplish the persona's goal?"},
                    "summary": {"type": "string",
                                "description": "1-3 sentences on what you did and what happened"},
                    "issues": {
                        "type": "array",
                        "description": "real problems you observed (broken/dead controls, JS "
                                       "errors, dead links, inconsistent behavior). Empty if none.",
                        "items": {
                            "type": "object",
                            "properties": {
                                "severity": {"type": "string", "enum": ["high", "medium", "low"]},
                                "description": {"type": "string"},
                            },
                            "required": ["severity", "description"],
                        },
                    },
                },
                "required": ["goal_achieved", "summary"],
            },
        },
    },
]

_INTERACTIVE_TOOLS = {"list_interactive", "click", "type_text", "read_page", "go_back"}


# ----------------------------------------------------------------- normalized model turn types
@dataclass
class ToolCall:
    id: str
    name: str
    arguments: dict


@dataclass
class AssistantTurn:
    content: str | None = None
    tool_calls: list[ToolCall] = field(default_factory=list)


@dataclass
class AgentStep:
    tool: str
    args: dict
    observation: dict


@dataclass
class AgentResult:
    persona: dict
    findings: dict | None          # the `finish` args, or None if it never finished
    steps: list[AgentStep]
    stopped: str                   # "finish" | "max_steps" | "error"
    error: str | None = None
    session: dict = field(default_factory=dict)   # objective browser stats, filled by caller


# ----------------------------------------------------------------- the loop
def run_agent(tools, model_call, persona: dict, *, max_steps: int = 12) -> AgentResult:
    """Drive `tools` with `model_call` to pursue `persona['goal']`. Returns AgentResult.

    `model_call(messages: list[dict]) -> AssistantTurn`. The loop appends provider-agnostic
    message dicts (assistant tool_calls + tool results) so any tool-calling backend works.
    """
    steps: list[AgentStep] = []
    messages: list[dict] = [{"role": "system", "content": _system_prompt(persona)}]

    # Seed with the first listing so the model starts from a concrete view of the page.
    seed = _safe(tools, "list_interactive", {})
    steps.append(AgentStep("list_interactive", {}, seed))
    messages.append({"role": "user", "content": _initial_prompt(persona, seed)})

    for _ in range(max_steps):
        try:
            turn = model_call(messages)
        except Exception as exc:  # noqa: BLE001 - model/transport failure ends the session
            return AgentResult(persona, _findings_from_steps(steps), steps, "error", str(exc))

        messages.append(_assistant_dict(turn))

        if not turn.tool_calls:
            # The model talked instead of acting. Nudge once; max_steps bounds the loop.
            messages.append({"role": "user",
                             "content": "Use a tool to act, or call finish with your findings."})
            continue

        finish_args = None
        for tc in turn.tool_calls:
            if tc.name == "finish":
                finish_args = tc.arguments if isinstance(tc.arguments, dict) else {}
                messages.append(_tool_dict(tc.id, "finish", {"ok": True}))
                continue
            obs = _dispatch(tools, tc.name, tc.arguments)
            steps.append(AgentStep(tc.name, tc.arguments, obs))
            messages.append(_tool_dict(tc.id, tc.name, obs))

        if finish_args is not None:
            return AgentResult(persona, finish_args, steps, "finish")

    return AgentResult(persona, _findings_from_steps(steps), steps, "max_steps")


def _dispatch(tools, name: str, args: dict) -> dict:
    if name not in _INTERACTIVE_TOOLS:
        return {"error": f"unknown tool: {name}"}
    return _safe(tools, name, args if isinstance(args, dict) else {})


def _safe(tools, name: str, args: dict) -> dict:
    fn = getattr(tools, name, None)
    if fn is None:
        return {"error": f"tool not available: {name}"}
    try:
        out = fn(**args)
    except TypeError as exc:
        return {"error": f"bad arguments for {name}: {exc}"}
    except Exception as exc:  # noqa: BLE001 - a tool failing must not kill the loop
        return {"error": f"{name} failed: {str(exc).splitlines()[0][:160]}"}
    return out if isinstance(out, dict) else {"result": out}


# ----------------------------------------------------------------- message construction
def _assistant_dict(turn: AssistantTurn) -> dict:
    d: dict = {"role": "assistant", "content": turn.content or ""}
    if turn.tool_calls:
        d["tool_calls"] = [
            {"id": tc.id, "type": "function",
             "function": {"name": tc.name, "arguments": json.dumps(tc.arguments)}}
            for tc in turn.tool_calls
        ]
    return d


def _tool_dict(tool_call_id: str, name: str, observation: dict) -> dict:
    return {"role": "tool", "tool_call_id": tool_call_id, "name": name,
            "content": json.dumps(observation)[:4000]}


def _findings_from_steps(steps: list[AgentStep]) -> dict | None:
    """Synthesize minimal findings when the agent never called finish (timeout/error)."""
    if not steps:
        return None
    dead = sum(1 for s in steps if s.tool == "click" and s.observation.get("dead"))
    errs = sum(1 for s in steps if s.observation.get("error"))
    issues = []
    if dead:
        issues.append({"severity": "medium", "description": f"{dead} click(s) had no observable effect"})
    if errs:
        issues.append({"severity": "low", "description": f"{errs} tool call(s) errored during exploration"})
    return {"goal_achieved": False,
            "summary": "Session ended without an explicit finish (timeout or error).",
            "issues": issues}


# ----------------------------------------------------------------- prompts
def _system_prompt(persona: dict) -> str:
    name = persona.get("name", "tester")
    goal = persona.get("goal", "explore the site and report anything broken")
    return (
        f"You are a QA tester exploring a live website as the persona \"{name}\". "
        f"Your goal: {goal}\n\n"
        "Act with the tools: list_interactive (see clickable elements with indices), click, "
        "type_text, read_page, go_back. Work step by step — look, act, observe the result, "
        "adapt. Re-run list_interactive whenever the page changes. Stay focused on your goal.\n\n"
        "Watch for real defects: dead controls (a click with no effect), JS/console errors, "
        "links that go nowhere, controls that behave inconsistently with similar ones. Only "
        "report problems you actually observed — do not speculate, and do not judge looks or "
        "copy quality (other systems handle those).\n\n"
        "When you achieve the goal, or are confident you cannot, call finish with "
        "goal_achieved, a short summary, and an issues list (each {severity, description})."
    )


def _initial_prompt(persona: dict, listing: dict) -> str:
    return (
        f"You are on the page now. Goal: {persona.get('goal', '')}\n\n"
        f"Current interactive elements:\n{json.dumps(listing, indent=2)[:3000]}\n\n"
        "Begin. Decide your first action and call a tool."
    )


# ----------------------------------------------------------------- live Playwright tools
class BrowserTools:
    """Step-wise browser control over one Playwright `page`. Each method returns JSON-safe dict.

    Unlike the one-shot probe, this does NOT reset to a base URL between actions — the agent
    explores freely and the page state carries across calls. Objective defects encountered
    (dead clicks, console errors) are tallied for the merge/scoring step.
    """

    def __init__(self, page, console_errors: list, dialog_box: dict, *,
                 max_interactions: int = 25, settle_ms: int = 600, dom_threshold: int = 24):
        self.page = page
        self.console_errors = console_errors
        self.dialog_box = dialog_box
        self.max_interactions = max_interactions
        self.settle_ms = settle_ms
        self.dom_threshold = dom_threshold
        self._last: list[dict] = []
        self.clicks = 0
        self.dead_clicks = 0

    def _dom_len(self) -> int:
        try:
            return int(self.page.evaluate("() => document.body ? document.body.innerHTML.length : 0"))
        except Exception:  # noqa: BLE001
            return 0

    def _sel(self, index, selector) -> str | None:
        if selector:
            return str(selector)
        if index is None:
            return None
        try:
            index = int(index)
        except (TypeError, ValueError):
            return None
        return self._last[index]["selector"] if 0 <= index < len(self._last) else None

    def list_interactive(self) -> dict:
        descs = self.page.evaluate(_ENUMERATE_JS, self.max_interactions) or []
        self._last = descs
        return {
            "url": self.page.url,
            "count": len(descs),
            "elements": [{"index": i, "tag": d.get("tag"), "text": d.get("text"),
                          "href": d.get("href"), "type": d.get("type")}
                         for i, d in enumerate(descs)],
        }

    def click(self, index=None, selector=None) -> dict:
        sel = self._sel(index, selector)
        if not sel:
            return {"error": "no such element; call list_interactive and pass a valid index"}
        before_url = self.page.url
        before_dom = self._dom_len()
        before_err = len(self.console_errors)
        self.dialog_box["text"] = None
        el = self.page.query_selector(sel)
        if el is None:
            return {"error": "element not found on current page; re-run list_interactive"}
        try:
            el.click(timeout=3000)
            self.clicks += 1
        except Exception as exc:  # noqa: BLE001
            return {"error": f"click failed: {str(exc).splitlines()[0][:160]}"}
        self.page.wait_for_timeout(self.settle_ms)

        cur = self.page.url
        navigated, hash_only = _same_page(before_url, cur)
        new_errs = self.console_errors[before_err:][:5]
        dom_changed = False
        if not navigated:
            dom_changed = abs(self._dom_len() - before_dom) >= self.dom_threshold
        dialog = self.dialog_box["text"]
        dead = not (navigated or hash_only or dom_changed or dialog or new_errs)
        if dead:
            self.dead_clicks += 1
        return {"navigated": navigated, "hash_only": hash_only, "url": cur,
                "dom_changed": dom_changed, "dialog": dialog,
                "console_errors": new_errs, "dead": dead}

    def type_text(self, index=None, selector=None, text="") -> dict:
        sel = self._sel(index, selector)
        if not sel:
            return {"error": "no such element; call list_interactive first"}
        try:
            self.page.fill(sel, str(text), timeout=3000)
        except Exception as exc:  # noqa: BLE001
            return {"error": f"type failed: {str(exc).splitlines()[0][:160]}"}
        return {"ok": True, "filled": str(text)[:80]}

    def read_page(self) -> dict:
        try:
            title = self.page.title()
            text = self.page.evaluate(
                "() => document.body ? document.body.innerText.slice(0, 1500) : ''")
        except Exception as exc:  # noqa: BLE001
            return {"error": f"read failed: {str(exc).splitlines()[0][:160]}"}
        return {"url": self.page.url, "title": title, "text": text}

    def go_back(self) -> dict:
        try:
            self.page.go_back(timeout=5000)
            self.page.wait_for_timeout(self.settle_ms)
        except Exception as exc:  # noqa: BLE001
            return {"error": f"go_back failed: {str(exc).splitlines()[0][:160]}"}
        return {"url": self.page.url}

    def session_summary(self) -> dict:
        return {"clicks": self.clicks, "dead_clicks": self.dead_clicks,
                "console_errors": len(self.console_errors)}


# ----------------------------------------------------------------- Nemotron adapter
def nemotron_model_call(config: dict | None, model: str | None, max_tokens: int = 1024):
    """Return a `model_call(messages) -> AssistantTurn` backed by Nemotron tool-calling."""
    from pipeline.signals import _nemotron

    def call(messages: list[dict]) -> AssistantTurn:
        msg = _nemotron.chat_raw(config, messages, tools=TOOL_SCHEMAS,
                                 model=model, max_tokens=max_tokens)
        return normalize_message(msg)

    return call


def normalize_message(msg) -> AssistantTurn:
    """Convert an OpenAI-SDK assistant message into the provider-agnostic AssistantTurn."""
    calls: list[ToolCall] = []
    for i, tc in enumerate(getattr(msg, "tool_calls", None) or []):
        fn = getattr(tc, "function", None)
        name = getattr(fn, "name", "") or ""
        raw_args = getattr(fn, "arguments", "") or "{}"
        try:
            args = json.loads(raw_args)
        except (json.JSONDecodeError, TypeError):
            args = {}
        calls.append(ToolCall(id=getattr(tc, "id", "") or f"call_{i}", name=name,
                              arguments=args if isinstance(args, dict) else {}))
    return AssistantTurn(content=getattr(msg, "content", None), tool_calls=calls)
