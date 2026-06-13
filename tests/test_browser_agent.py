"""Offline tests for the agentic browser loop.

No browser, no network: a FakeTools (in-memory) stands in for Playwright and a scripted
model_call stands in for Nemotron. We verify the loop dispatches tool calls, feeds
observations back, terminates on finish, caps at max_steps, handles model errors, and
that message/tool normalization is correct.
"""
from __future__ import annotations

from pipeline.browser_agent import (
    AssistantTurn, ToolCall, run_agent, normalize_message, _assistant_dict, _tool_dict,
)


class FakeTools:
    """Records calls; returns canned observations."""

    def __init__(self):
        self.calls = []

    def list_interactive(self):
        self.calls.append(("list_interactive", {}))
        return {"url": "http://x", "count": 1,
                "elements": [{"index": 0, "tag": "a", "text": "Buy", "href": "/buy"}]}

    def click(self, index=None, selector=None):
        self.calls.append(("click", {"index": index, "selector": selector}))
        return {"navigated": True, "url": "http://x/buy", "dead": False}

    def read_page(self):
        self.calls.append(("read_page", {}))
        return {"url": "http://x/buy", "title": "Buy", "text": "checkout"}


def _script(*turns):
    """Return a model_call that yields the given AssistantTurns in order."""
    seq = iter(turns)

    def call(messages):
        return next(seq)

    return call


def test_finish_terminates_with_findings():
    tools = FakeTools()
    model = _script(
        AssistantTurn(tool_calls=[ToolCall("1", "click", {"index": 0})]),
        AssistantTurn(tool_calls=[ToolCall("2", "finish",
                      {"goal_achieved": True, "summary": "bought it", "issues": []})]),
    )
    res = run_agent(tools, model, {"name": "shopper", "goal": "buy"}, max_steps=5)
    assert res.stopped == "finish"
    assert res.findings["goal_achieved"] is True
    # seed list_interactive + the click both recorded
    assert ("click", {"index": 0, "selector": None}) in tools.calls


def test_max_steps_cap_without_finish():
    tools = FakeTools()
    # model always clicks, never finishes
    def model(messages):
        return AssistantTurn(tool_calls=[ToolCall("c", "click", {"index": 0})])
    res = run_agent(tools, model, {"name": "x", "goal": "y"}, max_steps=3)
    assert res.stopped == "max_steps"
    assert res.findings is not None  # synthesized fallback findings
    assert res.findings["goal_achieved"] is False


def test_unknown_tool_is_reported_not_raised():
    tools = FakeTools()
    model = _script(
        AssistantTurn(tool_calls=[ToolCall("1", "teleport", {})]),
        AssistantTurn(tool_calls=[ToolCall("2", "finish", {"goal_achieved": False, "summary": "n/a"})]),
    )
    res = run_agent(tools, model, {"name": "x", "goal": "y"}, max_steps=5)
    # the unknown tool produced an error observation step, loop continued to finish
    err_steps = [s for s in res.steps if s.observation.get("error")]
    assert any("unknown tool" in s.observation["error"] for s in err_steps)
    assert res.stopped == "finish"


def test_no_tool_call_nudges_then_finishes():
    tools = FakeTools()
    model = _script(
        AssistantTurn(content="I think the page looks fine."),  # no tool call -> nudge
        AssistantTurn(tool_calls=[ToolCall("2", "finish", {"goal_achieved": True, "summary": "ok"})]),
    )
    res = run_agent(tools, model, {"name": "x", "goal": "y"}, max_steps=5)
    assert res.stopped == "finish"


def test_model_error_ends_session_gracefully():
    tools = FakeTools()
    def model(messages):
        raise RuntimeError("nebius 500")
    res = run_agent(tools, model, {"name": "x", "goal": "y"}, max_steps=5)
    assert res.stopped == "error"
    assert "nebius 500" in res.error


def test_bad_tool_arguments_reported():
    tools = FakeTools()
    model = _script(
        AssistantTurn(tool_calls=[ToolCall("1", "click", {"nonexistent_arg": 1})]),
        AssistantTurn(tool_calls=[ToolCall("2", "finish", {"goal_achieved": False, "summary": "x"})]),
    )
    res = run_agent(tools, model, {"name": "x", "goal": "y"}, max_steps=5)
    bad = [s for s in res.steps if s.tool == "click" and s.observation.get("error")]
    assert bad and "bad arguments" in bad[0].observation["error"]


# ----------------------------------------------------------------- normalization
class _FakeFn:
    def __init__(self, name, arguments):
        self.name = name
        self.arguments = arguments


class _FakeTC:
    def __init__(self, id, name, arguments):
        self.id = id
        self.function = _FakeFn(name, arguments)


class _FakeMsg:
    def __init__(self, content, tool_calls):
        self.content = content
        self.tool_calls = tool_calls


def test_normalize_message_parses_tool_calls():
    msg = _FakeMsg("thinking", [_FakeTC("abc", "click", '{"index": 2}')])
    turn = normalize_message(msg)
    assert turn.content == "thinking"
    assert turn.tool_calls[0].name == "click"
    assert turn.tool_calls[0].arguments == {"index": 2}


def test_normalize_message_handles_bad_json_args():
    msg = _FakeMsg(None, [_FakeTC("", "finish", "not json")])
    turn = normalize_message(msg)
    assert turn.tool_calls[0].arguments == {}
    assert turn.tool_calls[0].id == "call_0"  # fallback id assigned


def test_assistant_and_tool_dict_roundtrip():
    turn = AssistantTurn(content="", tool_calls=[ToolCall("id1", "click", {"index": 0})])
    ad = _assistant_dict(turn)
    assert ad["role"] == "assistant"
    assert ad["tool_calls"][0]["function"]["name"] == "click"
    td = _tool_dict("id1", "click", {"navigated": True})
    assert td["role"] == "tool" and td["tool_call_id"] == "id1"
