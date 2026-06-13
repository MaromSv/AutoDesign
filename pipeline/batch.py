"""Capture + score every candidate in a generation, all at once.

The evolution loop's gen-0 fan-out produces N independent candidates. Capturing
and scoring them is embarrassingly parallel — each candidate's frames and its
VLM-judge / saliency scores depend on nothing but that candidate. This module
runs the whole generation concurrently instead of one candidate at a time.

Why process-level fan-out (a subprocess per candidate) rather than threads:
  - Playwright's sync API is not safe to drive from multiple threads in one
    process, and the DeepGaze (torch) models behind the saliency signal are not
    guaranteed thread-safe either. Separate processes sidestep both.
  - The controlling threads here only block on `subprocess.run`, so a plain
    ThreadPoolExecutor is the right tool to supervise the pool.

Why references are acquired ONCE up front: `references.acquire_references`
caches its web-search + screenshots to `<run>/references/` and reuses them on
later calls. If all candidates ran with `--references` simultaneously on a cold
cache, they would each kick off the expensive research+render before any
manifest existed (a thundering herd). Pre-acquiring populates the cache so every
candidate's scoring process hits it instantly.

Usage:

    python -m pipeline.batch --gen-dir .autodesign/runs/<id>/gen-000 \
        --capture --references --winner
"""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

from pipeline.artifacts import SCORES_FILENAME, WINNER_FILENAME, write_winner
from pipeline.config import load_config


def _load_dotenv(path: str | Path) -> None:
    """Populate os.environ from a `.env` (KEY=VALUE lines), not overwriting set keys.

    We load it in THIS process so the per-candidate scoring subprocesses inherit the
    keys (subprocess inherits the parent environment). Thin wrapper over the shared loader.
    """
    from pipeline.envfile import load_dotenv
    load_dotenv(path)


def _candidate_dirs(gen_dir: Path) -> list[Path]:
    """The `cand-*` directories of a generation, in stable order."""
    return sorted(p for p in gen_dir.glob("cand-*") if p.is_dir())


def _already_scored(cand: Path, enabled_keys: set[str]) -> float | None:
    """Return the combined score iff `cand` is already fully scored, else None.

    "Fully scored" = a scores.json exists and EVERY enabled criterion has a non-None
    score. A run that failed for lack of an API key leaves those signals null, so this
    correctly returns None for it (it WILL be re-scored), while a complete, valid result
    is reused — so `--skip-scored` re-runs only redo work that actually needs redoing.
    """
    f = cand / SCORES_FILENAME
    if not f.exists():
        return None
    try:
        data = json.loads(f.read_text())
    except json.JSONDecodeError:
        return None
    per_criterion = data.get("per_criterion") or {}
    for key in enabled_keys:
        if per_criterion.get(key) is None:
            return None
    return data.get("combined")


def _process_one(
    cand: Path,
    *,
    config: str,
    do_capture: bool,
    references: bool,
    run_root: Path,
) -> dict:
    """Capture (optional) then score a single candidate in subprocesses.

    Runs in a worker thread; all real work happens in the spawned processes, so
    nothing here touches Playwright or torch in-process. The subprocesses inherit
    this process's environment (incl. ANTHROPIC_API_KEY loaded from .env upstream).
    Returns a result dict with the candidate name, combined score, and any error.
    """
    py = sys.executable
    steps: list[list[str]] = []
    if do_capture:
        steps.append([py, "-m", "pipeline.capture", "--candidate", str(cand), "--config", config])
    score_cmd = [py, "-m", "pipeline.benchmark", "--candidate", str(cand), "--config", config]
    if references:
        score_cmd += ["--references", "--run-dir", str(run_root)]
    steps.append(score_cmd)

    for cmd in steps:
        proc = subprocess.run(cmd, capture_output=True, text=True)
        if proc.returncode != 0:
            return {"candidate": cand.name, "combined": None,
                    "error": f"{cmd[2:4]} failed: {proc.stderr.strip()[-500:] or proc.stdout.strip()[-500:]}"}

    scores_file = cand / SCORES_FILENAME
    if not scores_file.exists():
        return {"candidate": cand.name, "combined": None, "error": "no scores.json written"}
    try:
        scores = json.loads(scores_file.read_text())
    except json.JSONDecodeError as exc:
        return {"candidate": cand.name, "combined": None, "error": f"bad scores.json: {exc}"}
    return {"candidate": cand.name, "combined": scores.get("combined"), "error": None}


def run_generation(
    gen_dir: Path,
    *,
    config_path: str = "autodesign.md",
    env_path: str = ".env",
    jobs: int | None = None,
    do_capture: bool = False,
    references: bool = False,
    write_winner_json: bool = False,
    skip_scored: bool = False,
) -> dict:
    """Capture + score every candidate in `gen_dir` concurrently.

    Returns `{"results": [...], "winner": <name|None>, "combined": <float|None>}`.
    `results` is one dict per candidate (name, combined, error), in finish order.
    """
    gen_dir = Path(gen_dir)
    run_root = gen_dir.parent
    cands = _candidate_dirs(gen_dir)
    if not cands:
        return {"results": [], "winner": None, "combined": None}

    _load_dotenv(env_path)
    config = load_config(config_path)

    # Report which enabled signals can actually run BEFORE spending time/credits, so a
    # missing SDK or key surfaces loudly instead of silently nulling scores.
    from pipeline.preflight import check as preflight_check, format_report
    print(format_report(preflight_check(config)), flush=True)

    # Optionally skip candidates already fully scored, so a re-run only redoes work that
    # needs it (e.g. after a partial failure) instead of re-spending on valid results.
    results: list[dict] = []
    todo = cands
    if skip_scored:
        enabled = set((config.get("criteria") or {}).keys())
        todo = []
        for c in cands:
            combined = _already_scored(c, enabled)
            if combined is None:
                todo.append(c)
            else:
                results.append({"candidate": c.name, "combined": combined, "error": None})
                print(f"  {c.name}: combined={combined:.2f} (cached, skipped)", flush=True)
        if not todo:
            print("all candidates already scored; nothing to do.", flush=True)

    # Acquire references ONCE before fan-out so the parallel scorers hit the cache.
    if references and todo:
        from pipeline.references import acquire_references
        brief = (config.get("brief") or "").strip()
        ref = acquire_references(brief, run_root, config)
        note = ref.topic or ref.skipped or "acquired"
        print(f"references: {note} ({len(ref.screenshots)} peer(s))", flush=True)

    workers = jobs or len(todo) or 1
    with ThreadPoolExecutor(max_workers=workers) as pool:
        futs = {
            pool.submit(_process_one, c, config=config_path,
                        do_capture=do_capture, references=references, run_root=run_root): c
            for c in todo
        }
        for fut in as_completed(futs):
            res = fut.result()
            results.append(res)
            if res["error"]:
                print(f"  {res['candidate']}: ERROR — {res['error']}", flush=True)
            else:
                c = res["combined"]
                print(f"  {res['candidate']}: combined={c:.2f}" if c is not None
                      else f"  {res['candidate']}: combined=—", flush=True)

    scored = [r for r in results if isinstance(r["combined"], (int, float))]
    winner = max(scored, key=lambda r: r["combined"], default=None)
    win_name = winner["candidate"] if winner else None
    win_score = winner["combined"] if winner else None

    if write_winner_json and win_name:
        write_winner(gen_dir / WINNER_FILENAME, win_name, win_score)

    return {"results": results, "winner": win_name, "combined": win_score}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Capture + score all candidates in a generation, in parallel.")
    parser.add_argument("--gen-dir", required=True,
                        help="Generation dir, e.g. .autodesign/runs/<id>/gen-000")
    parser.add_argument("--config", default="autodesign.md", help="Path to autodesign.md.")
    parser.add_argument("--env", default=".env", help="Path to .env (ANTHROPIC_API_KEY).")
    parser.add_argument("--jobs", type=int, default=None,
                        help="Max concurrent candidates (default: all of them).")
    parser.add_argument("--capture", action="store_true",
                        help="Capture frames first (else assumes frames already on disk).")
    parser.add_argument("--references", action="store_true",
                        help="Acquire similar-use-case competitors once, then score originality.")
    parser.add_argument("--winner", action="store_true",
                        help="Write gen winner.json (argmax combined) after scoring.")
    parser.add_argument("--skip-scored", action="store_true",
                        help="Skip candidates already fully scored (every enabled criterion "
                             "non-null); only re-score the rest. Cheap re-runs after a failure.")
    args = parser.parse_args(argv)

    out = run_generation(
        Path(args.gen_dir),
        config_path=args.config,
        env_path=args.env,
        jobs=args.jobs,
        do_capture=args.capture,
        references=args.references,
        skip_scored=args.skip_scored,
        write_winner_json=args.winner,
    )
    if out["winner"]:
        print(f"winner: {out['winner']} | combined={out['combined']:.2f}", flush=True)
    else:
        print("winner: none (no candidate scored)", flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
