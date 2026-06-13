"""FastAPI app — public leaderboard for AutoDesign-scored URLs.

No auth, no rate limit, no dedupe — wide open by user request. Designed to run
in a single Docker container on a Mac mini behind Cloudflare Tunnel.

Endpoints:
  GET  /                         the leaderboard SPA
  POST /api/submit               queue a URL; returns {id, status}
  GET  /api/leaderboard          ranked list of done submissions
  GET  /api/recent               recent activity (any status)
  GET  /api/queue                queue counts
  GET  /api/submission/{id}      single submission, with score breakdown if done
  GET  /artifacts/{id}/...       serve frames / video for a submission
"""

from __future__ import annotations

import logging
import os
import re
import sys
from pathlib import Path
from urllib.parse import urlparse

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

# Make the AutoDesign root importable so `from pipeline.rank import rank_urls`
# resolves whether we run via `python -m leaderboard.app` from /app or via
# uvicorn from inside the leaderboard/ dir.
_HERE = Path(__file__).resolve().parent
_REPO_ROOT = _HERE.parent
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

from leaderboard import db
from leaderboard.scorer import load_leaderboard_config
from leaderboard.worker import Worker

logging.basicConfig(
    level=os.getenv("LOG_LEVEL", "INFO"),
    format="%(asctime)s %(name)s %(levelname)s %(message)s",
)
log = logging.getLogger("leaderboard.app")


# ---- paths (configurable via env so the Docker volume can move) ----

DATA_DIR = Path(os.getenv("LEADERBOARD_DATA_DIR", "/data"))
DB_PATH = DATA_DIR / "leaderboard.db"
ARTIFACTS_ROOT = DATA_DIR / "runs"
CONFIG_PATH = Path(os.getenv("LEADERBOARD_CONFIG", str(_HERE / "config.yaml")))
STATIC_DIR = _HERE / "static"


# ---- request/response models ----

class SubmitIn(BaseModel):
    url: str = Field(..., min_length=4, max_length=2048)
    title: str = Field("", max_length=120)
    brief: str = Field("", max_length=2000)
    submitter: str = Field("", max_length=60)


class SubmitOut(BaseModel):
    id: int
    status: str
    url: str


# ---- app factory ----

app = FastAPI(title="AutoDesign Leaderboard", docs_url=None, redoc_url=None)


@app.on_event("startup")
def _startup() -> None:
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    ARTIFACTS_ROOT.mkdir(parents=True, exist_ok=True)
    db.init_db(DB_PATH)
    cfg = load_leaderboard_config(CONFIG_PATH)
    app.state.config = cfg
    app.state.worker = Worker(
        db_path=DB_PATH,
        artifacts_root=ARTIFACTS_ROOT,
        config=cfg,
    )
    app.state.worker.start()
    log.info("ready — db=%s artifacts=%s", DB_PATH, ARTIFACTS_ROOT)


@app.on_event("shutdown")
def _shutdown() -> None:
    if hasattr(app.state, "worker"):
        app.state.worker.stop()


# ---- helpers ----

_URL_RE = re.compile(r"^https?://", re.IGNORECASE)


def _normalize_url(raw: str) -> str:
    s = raw.strip()
    if not s:
        raise HTTPException(status_code=400, detail="url is empty")
    if not _URL_RE.match(s):
        s = "https://" + s
    parsed = urlparse(s)
    if not parsed.netloc or "." not in parsed.netloc:
        raise HTTPException(status_code=400, detail="not a valid URL")
    if parsed.scheme not in ("http", "https"):
        raise HTTPException(status_code=400, detail="only http(s) URLs allowed")
    return s


def _public_submission(row: dict) -> dict:
    """Shape a DB row for the API. Drops internal columns."""
    keep = (
        "id", "url", "title", "brief", "submitter", "status",
        "combined", "created_at", "finished_at", "error",
    )
    out = {k: row.get(k) for k in keep}
    if row.get("score_json"):
        import json
        try:
            out["score"] = json.loads(row["score_json"])
        except Exception:
            out["score"] = None
    return out


# ---- API ----

@app.post("/api/submit", response_model=SubmitOut)
def submit(payload: SubmitIn) -> SubmitOut:
    url = _normalize_url(payload.url)
    sub_id = db.insert_submission(
        db_path=DB_PATH,
        url=url,
        title=payload.title.strip(),
        brief=payload.brief.strip(),
        submitter=payload.submitter.strip(),
    )
    return SubmitOut(id=sub_id, status="queued", url=url)


@app.get("/api/leaderboard")
def get_leaderboard(limit: int = 200) -> list[dict]:
    limit = max(1, min(int(limit), 1000))
    return db.leaderboard(DB_PATH, limit=limit)


@app.get("/api/recent")
def get_recent(limit: int = 20) -> list[dict]:
    limit = max(1, min(int(limit), 100))
    return db.recent(DB_PATH, limit=limit)


@app.get("/api/queue")
def get_queue() -> dict:
    return db.queue_state(DB_PATH)


@app.get("/api/submission/{sub_id}")
def get_submission(sub_id: int) -> dict:
    row = db.get_submission(DB_PATH, sub_id)
    if not row:
        raise HTTPException(status_code=404, detail="submission not found")
    return _public_submission(row)


# ---- artifact serving (frames, videos, saliency overlay) ----

@app.get("/artifacts/{sub_id}/{name:path}")
def get_artifact(sub_id: int, name: str) -> FileResponse:
    row = db.get_submission(DB_PATH, sub_id)
    if not row or not row.get("artifacts_dir"):
        raise HTTPException(status_code=404, detail="no artifacts for this submission")
    base = Path(row["artifacts_dir"]).resolve()
    target = (base / name).resolve()
    # Path traversal guard — target must stay under base.
    if not str(target).startswith(str(base)):
        raise HTTPException(status_code=400, detail="invalid path")
    if not target.exists() or not target.is_file():
        raise HTTPException(status_code=404, detail="file not found")
    return FileResponse(str(target))


# ---- static frontend ----

if STATIC_DIR.exists():
    app.mount("/static", StaticFiles(directory=str(STATIC_DIR)), name="static")


@app.get("/")
def index() -> FileResponse:
    index_html = STATIC_DIR / "index.html"
    if not index_html.exists():
        raise HTTPException(status_code=500, detail="static/index.html missing")
    return FileResponse(str(index_html))


@app.exception_handler(404)
async def _not_found(request: Request, exc: HTTPException) -> JSONResponse:
    if request.url.path.startswith("/api/"):
        return JSONResponse({"detail": exc.detail}, status_code=404)
    # SPA fallback for unknown non-API routes.
    return FileResponse(str(STATIC_DIR / "index.html"))
