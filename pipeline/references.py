"""Acquire similar-use-case reference UIs for the originality criterion.

The `vlm_judge` signal scores a candidate's *originality* by comparing it against the
real competitive landscape — currently-deployed sites that serve the same use case. This
module finds and captures those peers, once per run (or per candidate), and caches them so
the comparison set is stable.

Pipeline (every step degrades gracefully — any failure yields an empty `ReferenceSet`
and the originality comparison simply doesn't run):

  1. Research agent (Anthropic Messages API + the `web_search` server tool) reads the
     brief and/or the site under review, names the product use case, and returns URLs of
     similar live products / direct competitors in that space (the candidate excluded).
  2. Each peer URL is screenshotted with AutoDesign's own renderer (pipeline.capture).
  3. The screenshots + a manifest are written under `<dir>/references/` and returned.
     A subsequent call reuses the manifest.

Config lives under the `originality:` block in `autodesign.md` (see `_load_cfg`). The
whole module is optional: with no `ANTHROPIC_API_KEY`, no `anthropic` SDK, or no
Playwright, acquisition returns an empty set and the run proceeds unchanged.
"""

from __future__ import annotations

import json
import os
import re
from dataclasses import dataclass, field
from pathlib import Path
from urllib.parse import urlparse

# Reuse the judge's tier resolution so model assignment stays in one place.
from pipeline.signals.vlmjudge import _resolve_model

_DEFAULTS = {
    "enabled": True,
    "n_references": 5,          # peer screenshots to keep (caps the expensive render step)
    "n_candidates": 10,         # URLs the agent returns; we over-fetch then keep what renders
    "max_search_rounds": 6,     # server-tool (web_search) continuation cap
}

_MANIFEST = "references.json"


@dataclass
class ReferenceSet:
    """The per-run reference material for the originality criterion. `screenshots` is what
    the VLM judge consumes; the rest is diagnostics for the dashboard / manifest."""

    topic: str = ""
    screenshots: list[Path] = field(default_factory=list)
    sources: list[dict] = field(default_factory=list)   # [{url, why, screenshot}]
    skipped: str | None = None

    def as_dict(self) -> dict:
        return {
            "topic": self.topic,
            "screenshots": [str(p) for p in self.screenshots],
            "sources": self.sources,
            "skipped": self.skipped,
        }


def _load_cfg(config: dict) -> dict:
    cfg = dict(_DEFAULTS)
    cfg.update((config or {}).get("originality") or {})
    return cfg


def acquire_references(brief: str, run_dir: Path, config: dict,
                       seed_url: str | None = None) -> ReferenceSet:
    """Return the originality reference set, building (and caching) it on first call.

    `run_dir` is where references land (`run_dir/references/`). `seed_url` is the site
    under review, when there's no brief to infer the use case from (e.g. ranking live
    URLs) — the agent infers the category from it and finds peers, excluding it. Safe to
    call repeatedly: a populated manifest is reused, so the agent + renders run at most once.
    """
    cfg = _load_cfg(config)
    refs_dir = Path(run_dir) / "references"

    cached = _load_manifest(refs_dir)
    if cached is not None:
        return cached

    if not cfg.get("enabled", True):
        return _persist(refs_dir, ReferenceSet(skipped="originality disabled in config"))

    try:
        topic, candidates = _research_sites(brief, seed_url, cfg, config)
    except _ResearchUnavailable as exc:
        return _persist(refs_dir, ReferenceSet(skipped=str(exc)))
    except Exception as exc:  # noqa: BLE001 - research is best-effort; never break the run
        return _persist(refs_dir, ReferenceSet(skipped=f"research agent error: {exc}"))

    if not candidates:
        return _persist(refs_dir, ReferenceSet(topic=topic, skipped="agent found no similar sites"))

    result = _capture_references(topic, candidates, refs_dir, cfg, config, seed_url)
    return _persist(refs_dir, result)


# --------------------------------------------------------------------------- research
class _ResearchUnavailable(RuntimeError):
    """Raised when the research agent can't run for an expected reason (no SDK/key)."""


_RESEARCH_PROMPT = (
    "You are assembling a competitive set of reference UIs to judge a new design's originality.\n\n"
    "Design brief for the site under review:\n<brief>\n{brief}\n</brief>\n{seed_line}\n"
    "Do two things:\n"
    "1. Name the product use case / category in 2-5 words (e.g. \"real-estate marketplace\", "
    "\"AI note-taking app\", \"crypto L1 marketing site\", \"team alerting tool\").\n"
    "2. Use web search to find {n} DISTINCT, currently-live websites that are similar products "
    "or direct competitors serving that SAME use case — the design landscape this site competes "
    "in. Prefer real, deployed product/marketing sites; avoid builder galleries and app stores. "
    "Exclude the site under review itself.\n\n"
    "Return ONLY a JSON object, no prose:\n"
    "{{\"topic\": \"<use case>\", \"sites\": [{{\"url\": \"https://...\", \"why\": \"<one phrase>\"}}]}}"
)


def _research_sites(brief: str, seed_url: str | None, cfg: dict,
                    config: dict) -> tuple[str, list[dict]]:
    """Run the web-search research agent; return (topic, [{url, why}, ...])."""
    try:
        import anthropic
    except ImportError as exc:
        raise _ResearchUnavailable("anthropic SDK not installed (pip install anthropic)") from exc
    if not os.getenv("ANTHROPIC_API_KEY"):
        raise _ResearchUnavailable("ANTHROPIC_API_KEY not set")

    model = cfg.get("search_model") or _resolve_model(config)
    seed_line = (
        f"Site under review (find peers/competitors for the same use case, and EXCLUDE it): {seed_url}\n"
        if seed_url else ""
    )
    prompt = _RESEARCH_PROMPT.format(
        brief=(brief or "(no brief — infer the use case from the site under review)").strip(),
        seed_line=seed_line,
        n=int(cfg["n_candidates"]),
    )

    client = anthropic.Anthropic()
    tools = [{"type": "web_search_20260209", "name": "web_search"}]
    messages = [{"role": "user", "content": prompt}]

    # web_search is a server-side tool: the API runs its own search loop and may return
    # stop_reason="pause_turn" when it hits the per-request iteration cap. Re-send to resume.
    raw = ""
    for _ in range(int(cfg["max_search_rounds"])):
        resp = client.messages.create(model=model, max_tokens=2000, tools=tools, messages=messages)
        raw = "".join(b.text for b in resp.content if getattr(b, "type", None) == "text")
        if resp.stop_reason != "pause_turn":
            break
        messages = [{"role": "user", "content": prompt},
                    {"role": "assistant", "content": resp.content}]

    data = _parse_json_obj(raw)
    if not isinstance(data, dict):
        return "", []
    topic = str(data.get("topic", "")).strip()
    sites = []
    for s in data.get("sites", []) or []:
        url = (s or {}).get("url") if isinstance(s, dict) else None
        if isinstance(url, str) and url.startswith("http"):
            sites.append({"url": url.strip(), "why": str((s or {}).get("why", "")).strip()})
    return topic, sites


# --------------------------------------------------------------------------- capture peers
def _peer_slug(url: str) -> str:
    parsed = urlparse(url)
    raw = (parsed.netloc + parsed.path).strip("/") or "peer"
    return re.sub(r"[^a-zA-Z0-9._-]+", "-", raw).strip("-")[:50] or "peer"


def _capture_references(topic: str, candidates: list[dict], refs_dir: Path,
                        cfg: dict, config: dict, seed_url: str | None) -> ReferenceSet:
    """Screenshot each peer with AutoDesign's renderer; keep the ones that render."""
    from pipeline.capture import capture as _capture

    refs_dir.mkdir(parents=True, exist_ok=True)
    vp = ((config or {}).get("capture") or {}).get("viewport") or [1280, 800]
    viewport = (int(vp[0]), int(vp[1]))
    want = int(cfg["n_references"])
    seed_norm = (seed_url or "").rstrip("/")

    screenshots: list[Path] = []
    sources: list[dict] = []
    for cand in candidates:
        if len(screenshots) >= want:
            break
        url = cand["url"]
        if url.rstrip("/") == seed_norm:
            continue  # never compare the candidate against itself
        peer_dir = refs_dir / _peer_slug(url)
        res = _capture(url, peer_dir, viewport=viewport, animation_seconds=0.0, keyframes=[0.0])
        if not res.frames:
            continue  # dead link / render failure / no Playwright — skip
        shot = res.frames[0]
        screenshots.append(shot)
        sources.append({"url": url, "why": cand.get("why", ""), "screenshot": str(shot)})

    skipped = None if screenshots else "no similar site could be rendered"
    return ReferenceSet(topic=topic, screenshots=screenshots, sources=sources, skipped=skipped)


# --------------------------------------------------------------------------- cache + parse
def _persist(refs_dir: Path, result: ReferenceSet) -> ReferenceSet:
    refs_dir.mkdir(parents=True, exist_ok=True)
    (refs_dir / _MANIFEST).write_text(json.dumps(result.as_dict(), indent=2), encoding="utf-8")
    return result


def _load_manifest(refs_dir: Path) -> ReferenceSet | None:
    path = refs_dir / _MANIFEST
    if not path.exists():
        return None
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return None
    shots = [Path(p) for p in data.get("screenshots", []) if Path(p).exists()]
    return ReferenceSet(
        topic=data.get("topic", ""),
        screenshots=shots,
        sources=data.get("sources", []),
        skipped=data.get("skipped"),
    )


def _parse_json_obj(text: str) -> dict | None:
    """Extract the JSON object from the agent response, tolerant of fences/prose."""
    for candidate in (text, _strip_fence(text), _first_brace_block(text)):
        if not candidate:
            continue
        try:
            data = json.loads(candidate)
        except (json.JSONDecodeError, TypeError):
            continue
        if isinstance(data, dict):
            return data
    return None


def _strip_fence(text: str) -> str | None:
    m = re.search(r"```(?:json)?\s*(\{.*?\})\s*```", text, re.DOTALL)
    return m.group(1) if m else None


def _first_brace_block(text: str) -> str | None:
    start = text.find("{")
    end = text.rfind("}")
    return text[start : end + 1] if 0 <= start < end else None
