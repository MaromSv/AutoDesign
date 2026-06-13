"""Read-only manifest API + static server for AutoDesign runs.

This is the *only* contract between the engine and the UI. The engine writes
artifacts under `.autodesign/runs/<id>/`; this server reads them and serves a
JSON manifest. The dashboard html is a thin client over that manifest.

Swapping the UI = editing `dashboard/` only. Do not import from `pipeline/`
beyond `pipeline.artifacts` (the disk-layout contract).

## Endpoints

    GET /                  -> dashboard.html
    GET /api/runs          -> { "runs": [<id>, ...] }
    GET /api/run/<id>      -> {
        "run": <id>,
        "generations": [
          { "id": "gen-NNN",
            "winner": "cand-XX" | null,
            "candidates": [
              { "id": "cand-XX",
                "html": "/.autodesign/.../index.html" | null,
                "frames": ["/.autodesign/.../frames/0000.png", ...],
                "saliency": "/.autodesign/.../saliency.png" | null,
                "video": "/.autodesign/.../animation.webm" | null,
                "combined": <float|null>,
                "per_criterion": { "<key>": <float|null>, ... },
                "critique": "...",
                "nameable_decisions": [...] } ] } ] }
    GET /.autodesign/...   -> static files from the run tree
"""

from __future__ import annotations

import json
import sys
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import unquote, urlparse

# Allow running as `python dashboard/serve.py`: Python puts the script's dir on
# sys.path, not the cwd, so we add the project root explicitly.
_PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(_PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(_PROJECT_ROOT))

from pipeline.artifacts import (
    FRAMES_DIRNAME,
    HTML_FILENAME,
    LINEAGE_FILENAME,
    RUNS_ROOT,
    SALIENCY_FILENAME,
    SCORES_FILENAME,
    VIDEO_FILENAME,
    WINNER_FILENAME,
)

HOST = "127.0.0.1"
PORT = 8765
ROOT = Path(__file__).resolve().parent
PROJECT_ROOT = ROOT.parent


def list_runs(runs_root: Path = RUNS_ROOT) -> list[str]:
    """Return run ids (directory names) sorted newest-first by name."""
    if not runs_root.exists():
        return []
    return sorted(
        (p.name for p in runs_root.iterdir() if p.is_dir()),
        reverse=True,
    )


def _read_json(path: Path) -> dict | None:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None


def _url_for(path: Path) -> str | None:
    """Build a project-root-relative URL for a file path, or None if it is outside the project."""
    try:
        rel = path.resolve().relative_to(PROJECT_ROOT.resolve())
    except ValueError:
        return None
    return f"/{rel.as_posix()}"


def _list_frames(gen_dir: Path) -> list[str]:
    frames_dir = gen_dir / FRAMES_DIRNAME
    if not frames_dir.exists():
        return []
    urls = (_url_for(p) for p in frames_dir.glob("*.png"))
    return sorted(u for u in urls if u is not None)


def _extract_hypothesis(html_path: Path) -> str | None:
    """Pull the leading `<!-- hypothesis: ... -->` (or first HTML comment) from an html file."""
    try:
        text = html_path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return None
    import re

    m = re.search(r"<!--\s*(.*?)\s*-->", text, re.DOTALL)
    return m.group(1).strip() if m else None


def _flat_criteria(raw: dict, per_criterion: dict) -> list[dict]:
    """Decompose signal outputs into a flat list of individual criteria.

    Each entry is `{key, score (0-1), weight, source}`. A signal that exposes
    `details.subscores` + `details.weights` (SaliencySignal) or
    `details.per_principle` (JudgeSignal) becomes multiple flat criteria — one
    per sub-score / UX principle. A signal that exposes neither appears as a
    single criterion under its own key, with weight left null.
    """
    out: list[dict] = []
    seen_sources: set[str] = set()
    for source, payload in raw.items():
        details = payload.get("details") or {}
        subs = details.get("subscores") or {}
        weights = details.get("weights") or {}
        # The VLM judge decomposes into one criterion per UX principle. Its
        # per_principle scores are 0-10 (rescaled to 0-1 here), each with its own
        # rubric weight — mirroring how saliency decomposes via `subscores`.
        per_principle = details.get("per_principle") or {}
        # Decomposed criteria: prefer these and skip the coarse signal score.
        if subs:
            for sub_key, sub_val in subs.items():
                if sub_key == "total":
                    continue
                out.append(
                    {
                        "key": sub_key,
                        "score": sub_val,  # already 0-1
                        "weight": weights.get(sub_key),
                        "source": source,
                    }
                )
            seen_sources.add(source)
        elif per_principle:
            for p_key, p_val in per_principle.items():
                score = p_val.get("score") if isinstance(p_val, dict) else p_val
                weight = p_val.get("weight") if isinstance(p_val, dict) else None
                out.append({
                    "key": p_key,
                    "score": (score / 10.0) if isinstance(score, (int, float)) else None,
                    "weight": weight,
                    "source": source,
                })
            seen_sources.add(source)
    # Signals with no decomposition: surface as a single flat criterion.
    for k, v in per_criterion.items():
        if k in seen_sources:
            continue
        out.append(
            {
                "key": k,
                "score": (v / 10.0) if isinstance(v, (int, float)) else None,
                "weight": None,
                "source": k,
            }
        )
    return out


def _candidate_manifest(cand_path: Path) -> dict:
    """One candidate entry in the per-generation `candidates` list."""
    scores = _read_json(cand_path / SCORES_FILENAME) or {}
    html = cand_path / HTML_FILENAME
    saliency = cand_path / SALIENCY_FILENAME
    video = cand_path / VIDEO_FILENAME
    raw = scores.get("raw", {})
    per_criterion = scores.get("per_criterion", {})
    subscores = {}
    for key, payload in raw.items():
        sub = (payload.get("details") or {}).get("subscores")
        if sub:
            subscores[key] = sub
    return {
        "id": cand_path.name,
        "html": _url_for(html) if html.exists() else None,
        "frames": _list_frames(cand_path),
        "saliency": _url_for(saliency) if saliency.exists() else None,
        "video": _url_for(video) if video.exists() else None,
        "combined": scores.get("combined"),
        # New flat shape — the primary surface for the dashboard.
        "criteria": _flat_criteria(raw, per_criterion),
        # Legacy nested shape — kept for now so older clients still work.
        "per_criterion": per_criterion,
        "subscores": subscores,
        "details": raw,
        "hypothesis": _extract_hypothesis(html) if html.exists() else None,
        "critique": scores.get("critique", ""),
        "nameable_decisions": scores.get("nameable_decisions", []),
    }


def _read_lineage(run_dir: Path) -> list[dict]:
    """Parse lineage.jsonl into a list of generation records, ordered by `generation`."""
    p = run_dir / LINEAGE_FILENAME
    if not p.exists():
        return []
    out: list[dict] = []
    for line in p.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            out.append(json.loads(line))
        except json.JSONDecodeError:
            continue
    out.sort(key=lambda r: r.get("generation", 0))
    return out


def build_run_manifest(run_id: str, runs_root: Path = RUNS_ROOT) -> dict | None:
    """Read one run directory and produce the dashboard manifest object.

    Returns None if the run does not exist; otherwise a fully-formed manifest,
    even when individual generations are missing files (the fields are filled
    with empty / null defaults so the UI can render an empty state).

    TODO: include `lineage.jsonl` once it carries content beyond the score.
    """
    run_dir = runs_root / run_id
    if not run_dir.exists():
        return None

    generations: list[dict] = []
    for gen_path in sorted(
        p for p in run_dir.iterdir() if p.is_dir() and p.name.startswith("gen-")
    ):
        cand_dirs = sorted(
            p for p in gen_path.iterdir() if p.is_dir() and p.name.startswith("cand-")
        )
        candidates = [_candidate_manifest(c) for c in cand_dirs]
        winner_obj = _read_json(gen_path / WINNER_FILENAME) or {}
        generations.append(
            {
                "id": gen_path.name,
                "winner": winner_obj.get("winner"),
                "winner_combined": winner_obj.get("combined"),
                "candidates": candidates,
            }
        )

    lineage = _read_lineage(run_dir)
    brief_path = run_dir / "brief.txt"
    brief = (
        brief_path.read_text(encoding="utf-8").strip() if brief_path.exists() else ""
    )
    final_path = run_dir / "final.html"
    final_url = _url_for(final_path) if final_path.exists() else None

    # Optional per-run config snapshot — currently carries the capture viewport so
    # the dashboard can size the stage (mobile portrait vs. desktop landscape).
    # Older runs without this file fall back to the default landscape size.
    meta = _read_json(run_dir / "run_meta.json") or {}
    viewport = meta.get("viewport") or [1280, 800]

    return {
        "run": run_id,
        "brief": brief,
        "viewport": viewport,
        "lineage": lineage,
        "final": final_url,
        "generations": generations,
    }


class Handler(BaseHTTPRequestHandler):
    """Minimal manifest API. No write endpoints — everything is read-only."""

    def log_message(self, format: str, *args) -> None:  # noqa: A002 — stdlib signature
        # Keep stdout quiet by default. Override in dev if you want request logs.
        return

    def _json(self, payload: dict, status: int = 200) -> None:
        body = json.dumps(payload).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _file(self, path: Path, content_type: str | None = None) -> None:
        try:
            data = path.read_bytes()
        except OSError:
            self.send_error(404)
            return
        self.send_response(200)
        if content_type:
            self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def do_GET(self) -> None:  # noqa: N802 — stdlib signature
        parsed = urlparse(self.path)
        path = unquote(parsed.path)

        if path == "/":
            self._file(ROOT / "dashboard.html", "text/html; charset=utf-8")
            return

        if path == "/logo.png":
            self._file(ROOT / "logo.png", "image/png")
            return

        if path == "/api/runs":
            self._json({"runs": list_runs()})
            return

        if path.startswith("/api/run/"):
            run_id = path[len("/api/run/") :].strip("/")
            manifest = build_run_manifest(run_id)
            if manifest is None:
                self._json({"error": f"unknown run: {run_id}"}, status=404)
                return
            self._json(manifest)
            return

        if path.startswith("/.autodesign/"):
            # Static files under the run tree. Resolve safely and refuse traversal.
            rel = path.lstrip("/")
            target = (PROJECT_ROOT / rel).resolve()
            if not str(target).startswith(str(PROJECT_ROOT.resolve())):
                self.send_error(403)
                return
            if not target.exists() or not target.is_file():
                self.send_error(404)
                return
            self._file(target)
            return

        self.send_error(404)


def serve(host: str = HOST, port: int = PORT) -> None:
    """Block, serving the dashboard on host:port. Ctrl-C to stop."""
    with ThreadingHTTPServer((host, port), Handler) as httpd:
        print(f"AutoDesign dashboard on http://{host}:{port}")
        try:
            httpd.serve_forever()
        except KeyboardInterrupt:
            pass


if __name__ == "__main__":
    serve()
