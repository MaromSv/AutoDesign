"""Capture and rank live websites with the configured signals — a thin test harness.

Point it at a few URLs; it renders each (capture.py), scores each through the signals
in `autodesign.md`'s `criteria` block (benchmark.py), and prints them ranked by combined
score. This is the manual way to see the VLM judge's ranking on real sites, independent
of the (still-stubbed) evolution loop.

    python -m pipeline.rank https://a.com https://b.com https://c.com
    python -m pipeline.rank --brief "a calm meditation app" --out /tmp/rank https://a.com https://b.com

For validation against a labeled set, pass `--file`: a text file with `Good:` / `Bad:`
section headers and one URL per line under each. The harness ranks them all and reports
whether the judge orders the "good" sites above the "bad" ones (a separation verdict),
turning the ranking into a pass/fail eval:

    python -m pipeline.rank --file good_bad_examples.txt --brief ""

Artifacts (frames + per-site scores.json) are written under `--out` (default
`.autodesign/rank/`). Needs `playwright` (+ `playwright install chromium`) to render and,
for the vlm_judge signal, `anthropic` + `ANTHROPIC_API_KEY`. Sites whose capture or judge
step is unavailable are listed as skipped rather than crashing the run.
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path
from urllib.parse import urlparse

from pipeline.capture import capture
from pipeline.config import load_config
from pipeline.benchmark import build_context, score_candidate
from pipeline.references import acquire_references


def parse_labeled_file(path: str | Path) -> dict[str, str]:
    """Parse a `Good:`/`Bad:` sectioned URL list into {url: label}.

    Lines ending in ':' start a section (the word before the colon, lowercased, is the
    label — e.g. "Good:", "Bad:"). Every non-empty line after that is a URL with that
    label until the next header. Lines before any header are labeled "" (unlabeled).
    """
    labels: dict[str, str] = {}
    current = ""
    for line in Path(path).read_text(encoding="utf-8").splitlines():
        s = line.strip()
        if not s:
            continue
        if s.endswith(":") and " " not in s.rstrip(":"):
            current = s[:-1].strip().lower()
            continue
        labels[s] = current
    return labels


def _slug(url: str) -> str:
    parsed = urlparse(url)
    raw = (parsed.netloc + parsed.path).strip("/") or "site"
    return re.sub(r"[^a-zA-Z0-9._-]+", "-", raw).strip("-")[:60] or "site"


def _capture_settings(config: dict) -> dict:
    cap = (config or {}).get("capture") or {}
    vp = cap.get("viewport") or [1280, 800]
    return {
        "viewport": (int(vp[0]), int(vp[1])),
        "animation_seconds": float(cap.get("animation_seconds", 0.0)),
        "keyframes": list(cap.get("keyframes") or [0.0]),
    }


def rank_urls(urls: list[str], config: dict, out_dir: Path, brief: str,
              labels: dict[str, str] | None = None, use_references: bool = False) -> list[dict]:
    """Capture + score each URL; return records sorted by combined score (desc).

    With `use_references`, a research agent finds similar-use-case competitors for each
    URL (inferring the use case from the site itself) and feeds them to the judge, which
    then also scores the `originality` principle.
    """
    cap_cfg = _capture_settings(config)
    labels = labels or {}
    records: list[dict] = []

    for url in urls:
        candidate_dir = out_dir / _slug(url)
        candidate_dir.mkdir(parents=True, exist_ok=True)
        cap = capture(url, candidate_dir, **cap_cfg)

        references: list[Path] = []
        topic = ""
        if use_references:
            ref = acquire_references(brief, candidate_dir, config, seed_url=url)
            references, topic = ref.screenshots, ref.topic

        ctx = build_context(candidate_dir, brief=brief, config=config,
                            frames=cap.frames, html_url=url,
                            references=references, topic=topic)
        result = score_candidate(ctx)
        result["url"] = url
        result["label"] = labels.get(url, "")
        result["capture_skipped"] = cap.skipped
        result["n_frames"] = len(cap.frames)
        result["n_references"] = len(references)
        result["topic"] = topic
        (candidate_dir / "scores.json").write_text(
            json.dumps(result, indent=2, sort_keys=True), encoding="utf-8")
        records.append(result)

    # Highest combined first; sites with no scored signal sink to the bottom.
    records.sort(key=lambda r: (bool(r["scored_criteria"]), r["combined"]), reverse=True)
    return records


def _print_table(records: list[dict]) -> None:
    show_label = any(r.get("label") for r in records)
    lbl_h = f"{'label':>6}  " if show_label else ""
    print(f"\n{'#':<3} {'score':>6}  {lbl_h}{'frames':>6}  url")
    print("-" * 72)
    for i, r in enumerate(records, 1):
        scored = r["scored_criteria"]
        score = f"{r['combined']:.2f}" if scored else "  -  "
        lbl = f"{r.get('label', ''):>6}  " if show_label else ""
        print(f"{i:<3} {score:>6}  {lbl}{r['n_frames']:>6}  {r['url']}")
        if not scored:
            why = r.get("capture_skipped") or "; ".join(
                f"{k}: {(r['raw'].get(k) or {}).get('skipped')}" for k in r["skipped_criteria"])
            print(f"       (not scored — {why})")
        else:
            judge = (r["raw"].get("vlm_judge") or {}).get("details") or {}
            crit = judge.get("critique")
            if crit:
                print(f"       {crit}")
            per = judge.get("per_principle") or {}
            if per:
                # One compact line of "principle score" pairs (the criteria breakdown).
                cells = " · ".join(f"{k} {v.get('score')}" for k, v in per.items())
                print(f"       criteria: {cells}")
            if r.get("n_references"):
                print(f"       originality vs {r['n_references']} peers"
                      + (f" ({r.get('topic')})" if r.get("topic") else ""))
            # Multi-criterion runs (e.g. once saliency lands): show each signal's score too.
            if len(r["scored_criteria"]) > 1:
                sig = " · ".join(f"{k} {r['per_criterion'][k]:.1f}" for k in r["scored_criteria"])
                print(f"       signals:  {sig}")
    print()


def _separation_inversions(good: list[float], bad: list[float]) -> int:
    """Count (good, bad) pairs where the bad site scored >= the good one. 0 == clean."""
    return sum(1 for g in good for b in bad if b >= g)


def _print_verdict(records: list[dict]) -> None:
    """When records carry good/bad labels, report how well the score separated them."""
    good = [r["combined"] for r in records if r.get("label") == "good" and r["scored_criteria"]]
    bad = [r["combined"] for r in records if r.get("label") == "bad" and r["scored_criteria"]]
    if not good or not bad:
        return
    inversions = _separation_inversions(good, bad)
    total = len(good) * len(bad)
    print("Separation verdict (good should outrank bad):")
    print(f"  good: mean {sum(good)/len(good):.2f}  (n={len(good)})")
    print(f"  bad:  mean {sum(bad)/len(bad):.2f}  (n={len(bad)})")
    print(f"  inversions: {inversions}/{total} good-vs-bad pairs where bad scored >= good "
          f"({'PASS — clean separation' if inversions == 0 else 'see misorderings above'})")
    print()


def _load_dotenv(path: str | Path) -> None:
    """Populate os.environ from a `.env` file (KEY=VALUE lines) without overwriting
    anything already set. Minimal — no quoting/expansion magic, no extra dependency.
    Lets `ANTHROPIC_API_KEY=...` in AutoDesign/.env flow to the judge automatically."""
    import os

    p = Path(path)
    if not p.exists():
        return
    for line in p.read_text(encoding="utf-8").splitlines():
        s = line.strip()
        if not s or s.startswith("#") or "=" not in s:
            continue
        key, _, val = s.partition("=")
        key, val = key.strip(), val.strip().strip('"').strip("'")
        if key and key not in os.environ:
            os.environ[key] = val


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="Capture and rank live websites with the configured signals.")
    ap.add_argument("urls", nargs="*", help="website URLs to rank (or use --file)")
    ap.add_argument("--file", default=None, help="text file of URLs with Good:/Bad: section headers")
    ap.add_argument("--config", default="autodesign.md", help="path to autodesign.md (default: ./autodesign.md)")
    ap.add_argument("--out", default=".autodesign/rank", help="artifact directory (default: .autodesign/rank)")
    ap.add_argument("--brief", default=None, help="override the brief (default: config's brief)")
    ap.add_argument("--references", action="store_true",
                    help="acquire similar-use-case competitors per URL (web search) and score originality")
    ap.add_argument("--env", default=".env", help="path to a .env file with ANTHROPIC_API_KEY (default: ./.env)")
    args = ap.parse_args(argv)

    _load_dotenv(args.env)
    config = load_config(args.config)
    brief = args.brief if args.brief is not None else (config.get("brief") or "").strip()
    out_dir = Path(args.out)
    out_dir.mkdir(parents=True, exist_ok=True)

    labels = parse_labeled_file(args.file) if args.file else {}
    urls = list(dict.fromkeys(list(labels.keys()) + list(args.urls)))  # de-dup, preserve order
    if not urls:
        ap.error("no URLs given — pass them as arguments or via --file")

    records = rank_urls(urls, config, out_dir, brief, labels=labels,
                        use_references=args.references)
    _print_table(records)
    _print_verdict(records)

    if not any(r["scored_criteria"] for r in records):
        print("No site was scored. Check: playwright installed + `playwright install chromium`, "
              "and for vlm_judge an ANTHROPIC_API_KEY (env or .env).", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
