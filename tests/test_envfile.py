"""Tests for the shared .env loader."""
from __future__ import annotations

import os

import pipeline.envfile as envfile


def test_load_sets_only_missing_keys(tmp_path, monkeypatch):
    env = tmp_path / ".env"
    env.write_text('NEBIUS_API_KEY="sk-test-123"\n# comment\nALREADY=should-not-win\n')
    monkeypatch.setenv("ALREADY", "fromenv")
    monkeypatch.delenv("NEBIUS_API_KEY", raising=False)
    envfile.load_dotenv(env)
    assert os.environ["NEBIUS_API_KEY"] == "sk-test-123"  # quotes stripped, set
    assert os.environ["ALREADY"] == "fromenv"             # pre-set value not overwritten


def test_missing_file_is_noop(tmp_path):
    envfile.load_dotenv(tmp_path / "nope.env")  # must not raise


def test_ensure_loaded_runs_once(tmp_path, monkeypatch):
    monkeypatch.setattr(envfile, "_loaded", False)
    env = tmp_path / ".env"
    env.write_text("FOO_ONCE=first\n")
    monkeypatch.delenv("FOO_ONCE", raising=False)
    envfile.ensure_loaded(env)
    assert os.environ["FOO_ONCE"] == "first"
    # second call is a no-op even if the file changed
    env.write_text("FOO_ONCE=second\nBAR_NEW=x\n")
    monkeypatch.delenv("BAR_NEW", raising=False)
    envfile.ensure_loaded(env)
    assert os.environ["FOO_ONCE"] == "first"      # not reloaded
    assert "BAR_NEW" not in os.environ
