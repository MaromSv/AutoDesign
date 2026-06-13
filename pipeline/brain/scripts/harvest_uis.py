"""Harvest good (awwwards) and bad (AI-slop) website URL lists for training.

Writes `<root>/good.txt` and `<root>/bad.txt` (URL per line) which
`build_dataset.py` then turns into brain vectors.

  GOOD  — awwwards winners. We try to scrape the public "sites of the day" pages;
          awwwards is JS-heavy and rate-limits scrapers, so we fall back to a
          curated seed list of well-known winners when scraping yields too few.
  BAD   — madewithlovable / AI-slop. Reuses the already-harvested
          `slop-detector/data/slop_sites.txt` if present (457 live Lovable sites),
          otherwise the seed list below.

    python -m pipeline.brain.scripts.harvest_uis --root data/brain --limit 60

Polite scraping: real UA, low volume, best-effort. The point is a balanced,
clearly-separated good/bad set — quality over raw count.
"""

from __future__ import annotations

import argparse
import re
from pathlib import Path
from urllib.parse import urlparse

# Curated awwwards-calibre winners — used as a fallback / supplement when live
# scraping is blocked. Hand-picked, considered, deployed design.
GOOD_SEED = [
    "https://ref.digital/",
    "https://www.sui.io/",
    "https://sen-knife.com/2.0/",
    "https://www.stripe.com/",
    "https://linear.app/",
    "https://www.apple.com/",
    "https://vercel.com/",
    "https://www.awwwards.com/",
    "https://basement.studio/",
    "https://www.locomotive.ca/en",
    "https://www.active-theory.com/",
    "https://resn.co.nz/",
    "https://igloo.inc/",
    "https://www.obys.agency/",
    "https://www.dustinstout.com/",
    "https://www.unseen.co/",
    "https://www.cuberto.com/",
    "https://www.aristidebenoist.com/",
    "https://www.diagram.com/",
    "https://www.framer.com/",
]

BAD_SEED = [
    "https://roll-a-path.lovable.app/",
    "https://www.ninja-alert.com/",
    "https://madewithlovable.com/projects/ready-steady-find",
]

UA = ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/124.0 Safari/537.36")

# Hosts that appear as nofollow links on awwwards but are never the winner site.
_SKIP_HOSTS = ("awwwards.com", "twitter.com", "x.com", "facebook.com", "instagram.com",
               "linkedin.com", "youtube.com", "github.com", "dribbble.com", "behance.net",
               "fonts.googleapis.com", "fonts.gstatic.com", "google.com", "pinterest.com")


def scrape_awwwards(limit: int) -> list[str]:
    """Best-effort scrape of awwwards winner external URLs. Returns [] on failure."""
    try:
        import requests
    except ImportError:
        return []
    found: list[str] = []
    seen: set[str] = set()
    sess = requests.Session()
    sess.headers.update({"User-Agent": UA})
    # The winners listing paginates ~32 external winner links per page. Walk pages
    # until we hit `limit` or a page yields nothing new (end of useful results).
    max_pages = max(5, (limit // 25) + 4)
    for page in range(1, max_pages + 1):
        try:
            url = f"https://www.awwwards.com/websites/?page={page}"
            html = sess.get(url, timeout=20).text
        except Exception:  # noqa: BLE001
            break
        before = len(found)
        # External winner links carry rel="...nofollow"; skip awwwards/social/cdn hosts.
        for m in re.findall(r'href="(https?://[^"]+)"[^>]*rel="[^"]*nofollow', html):
            host = urlparse(m).netloc.lower()
            if any(skip in host for skip in _SKIP_HOSTS) or m in seen:
                continue
            seen.add(m)
            found.append(m)
        if len(found) >= limit:
            break
        if len(found) == before:  # page added nothing new -> stop paging
            break
    return found[:limit]


def load_slop_list(repo_root: Path) -> list[str]:
    """Reuse slop-detector's harvested Lovable site list if available."""
    candidates = [
        repo_root / "slop-detector" / "data" / "slop_sites.txt",
        repo_root.parent / "slop-detector" / "data" / "slop_sites.txt",
    ]
    for p in candidates:
        if p.exists():
            urls = [
                ln.strip()
                for ln in p.read_text(encoding="utf-8").splitlines()
                if ln.strip() and not ln.strip().startswith("#")
            ]
            if urls:
                return urls
    return []


def _write(path: Path, urls: list[str], header: str) -> None:
    # Dedup, preserve order.
    seen, ordered = set(), []
    for u in urls:
        if u not in seen:
            seen.add(u)
            ordered.append(u)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(f"# {header}\n" + "\n".join(ordered) + "\n", encoding="utf-8")
    print(f"wrote {path}  ({len(ordered)} urls)")


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--root", default="data/brain", help="output dir for good.txt/bad.txt")
    ap.add_argument("--limit", type=int, default=60, help="max URLs per class")
    ap.add_argument("--no-scrape", action="store_true", help="seed lists only, skip live scraping")
    args = ap.parse_args()

    root = Path(args.root)
    # repo root = .../AutoDesign (the inner project); slop-detector is a sibling of it.
    repo_root = Path(__file__).resolve().parents[3]

    good = list(GOOD_SEED)
    if not args.no_scrape:
        scraped = scrape_awwwards(args.limit)
        print(f"awwwards scrape returned {len(scraped)} urls")
        good = scraped + [g for g in GOOD_SEED if g not in scraped]
    good = good[: args.limit]

    bad = load_slop_list(repo_root) or BAD_SEED
    print(f"slop list: {len(bad)} urls")
    bad = bad[: args.limit]

    _write(root / "good.txt", good, "GOOD — awwwards winners (scraped + curated seed)")
    _write(root / "bad.txt", bad, "BAD — madewithlovable / AI-slop sites")


if __name__ == "__main__":
    main()
