"""Offline tests for the distinctiveness reference acquisition module.

No network and no Playwright: we cover config merging, the disabled/no-credentials
graceful-skip paths, the per-run manifest cache (build once, reuse), and the tolerant
JSON parsing the research agent's output goes through. The live research call + render
are the only things not covered here (they need an API key and a browser).
"""
from __future__ import annotations

from pipeline.references import (
    _load_cfg,
    _parse_json_obj,
    acquire_references,
    ReferenceSet,
    _DEFAULTS,
)


def test_load_cfg_merges_defaults_with_overrides():
    cfg = _load_cfg({"originality": {"n_references": 3}})
    assert cfg["n_references"] == 3
    assert cfg["max_search_rounds"] == _DEFAULTS["max_search_rounds"]  # untouched default carried through


def test_disabled_writes_skip_manifest_and_no_screenshots(tmp_path):
    res = acquire_references("a brief", tmp_path, {"originality": {"enabled": False}})
    assert res.screenshots == [] and res.skipped and "disabled" in res.skipped
    assert (tmp_path / "references" / "references.json").exists()


def test_missing_credentials_skips_gracefully(tmp_path, monkeypatch):
    # No API key (and/or no anthropic SDK) -> research unavailable -> empty set, run continues.
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    res = acquire_references("a real-estate brief", tmp_path, {})
    assert res.screenshots == [] and res.skipped is not None


def test_manifest_is_cached_and_reused(tmp_path, monkeypatch):
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    first = acquire_references("brief", tmp_path, {})
    # Corrupt nothing; a second call must read the manifest, not re-run research.
    calls = {"n": 0}
    import pipeline.references as refs

    def _boom(*a, **k):
        calls["n"] += 1
        raise AssertionError("research should not run when a manifest exists")

    monkeypatch.setattr(refs, "_research_sites", _boom)
    second = acquire_references("brief", tmp_path, {})
    assert calls["n"] == 0
    assert second.skipped == first.skipped and second.screenshots == first.screenshots


def test_parse_json_obj_handles_fenced_bare_and_garbage():
    fenced = '```json\n{"topic": "x", "sites": [{"url": "https://a.com"}]}\n```'
    bare = 'Sure: {"topic": "y", "sites": []} done'
    assert _parse_json_obj(fenced)["topic"] == "x"
    assert _parse_json_obj(bare)["sites"] == []
    assert _parse_json_obj("no json here") is None


def test_reference_set_as_dict_roundtrips_paths():
    from pathlib import Path

    rs = ReferenceSet(topic="t", screenshots=[Path("/tmp/a.png")], sources=[{"url": "u"}])
    d = rs.as_dict()
    assert d["topic"] == "t" and d["screenshots"] == ["/tmp/a.png"]
