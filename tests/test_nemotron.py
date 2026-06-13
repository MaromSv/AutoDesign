"""Offline tests for the shared Nemotron/Nebius client helpers.

No network: we cover config resolution, the typed unavailable error when no key/SDK,
and the lenient JSON extraction.
"""
from __future__ import annotations

import pytest

from pipeline.signals import _nemotron as nm


def test_resolve_model_default():
    assert nm.resolve_model({}) == nm.DEFAULT_MODEL


def test_resolve_model_block_and_override():
    cfg = {"nemotron": {"model": "from-block"}}
    assert nm.resolve_model(cfg) == "from-block"
    assert nm.resolve_model(cfg, override="from-signal") == "from-signal"


def test_chat_raises_unavailable_without_key(monkeypatch):
    # Stub the .env autoloader so the developer's real NEBIUS_API_KEY can't leak in and
    # mask the "no key" path we're testing.
    monkeypatch.setattr("pipeline.envfile.ensure_loaded", lambda: None)
    monkeypatch.delenv("NEBIUS_API_KEY", raising=False)
    with pytest.raises(nm.NemotronUnavailable):
        nm.chat({}, messages=[{"role": "user", "content": "hi"}])


def test_custom_key_env(monkeypatch):
    monkeypatch.setattr("pipeline.envfile.ensure_loaded", lambda: None)
    monkeypatch.delenv("MY_KEY", raising=False)
    cfg = {"nemotron": {"api_key_env": "MY_KEY"}}
    with pytest.raises(nm.NemotronUnavailable) as exc:
        nm.chat(cfg, messages=[{"role": "user", "content": "hi"}])
    assert "MY_KEY" in str(exc.value)


def test_extract_json_plain():
    assert nm.extract_json('{"score": 7}') == {"score": 7}


def test_extract_json_fenced_with_prose():
    text = 'Here you go:\n```json\n{"score": 8, "issues": []}\n```\nthanks'
    assert nm.extract_json(text) == {"score": 8, "issues": []}


def test_extract_json_embedded():
    assert nm.extract_json('noise {"score": 5} trailing')["score"] == 5


def test_extract_json_garbage_returns_none():
    assert nm.extract_json("not json at all") is None
    assert nm.extract_json("") is None
