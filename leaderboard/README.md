# AutoDesign Leaderboard

Public-facing web app: anyone submits a URL, it gets captured + scored by the
same saliency + VLM pipeline AutoDesign uses internally, and ranks on a public
leaderboard.

Designed to run in Docker on a Mac mini and be exposed to the internet via a
Cloudflare Tunnel.

## Architecture

```
   browser  ──HTTP──▶  FastAPI ──▶  SQLite ◀── background worker
                          │                          │
                          └──── /artifacts/* ────────┴── /data/runs/sub-*/
                                                          frames/, video, saliency.png

   pipeline.rank.rank_urls  is reused as-is for the actual scoring.
```

- **app.py** — FastAPI: submit, leaderboard, recent, queue, submission detail, artifact serving.
- **worker.py** — daemon thread that processes one submission at a time.
- **scorer.py** — thin wrapper over `pipeline.rank.rank_urls`.
- **db.py** — SQLite (WAL mode) with a single `submissions` table.
- **static/** — vanilla HTML/CSS/JS, no build step.
- **config.yaml** — scoring config (signals, weights, capture). Independent from `autodesign.md`.

No auth, no rate limit, no URL dedupe — wide open by design. Anyone who hits
`/api/submit` burns your Anthropic credits. Run it in front of Cloudflare and
add IP rules at the edge if abuse shows up.

## First-time setup on the Mac mini

1. Install Docker Desktop for Mac. Make sure it's running.
2. Clone (or already have) the AutoDesign repo on the mini.
3. From this folder:
   ```bash
   cp .env.example .env
   # edit .env, paste your ANTHROPIC_API_KEY
   docker compose up -d --build
   ```
4. First build takes a while (Torch + Chromium + DeepGaze weights ≈ a few GB).
   Watch progress:
   ```bash
   docker compose logs -f
   ```
5. When healthy, visit http://localhost:8080. Submit a URL to smoke-test.

The SQLite DB lives at `./data/leaderboard.db`. Captured frames + saliency
overlays live at `./data/runs/sub-NNNNNN/`. Both survive container restarts.

## Going public via Cloudflare Tunnel (recommended)

You don't want to forward a port on your home router. Cloudflare Tunnel gives
you a permanent public hostname (`leaderboard.yourdomain.com`) that routes to
`localhost:8080` on your Mac mini — encrypted, no firewall config, free.

1. Sign in to https://dash.cloudflare.com (free plan is fine). You need a
   domain pointed at Cloudflare's nameservers — any domain you own works.
2. Go to **Zero Trust → Networks → Tunnels → Create a tunnel**.
3. Pick **Cloudflared**, name it `mac-mini`, and copy the install command shown.
   On the Mac mini:
   ```bash
   brew install cloudflare/cloudflare/cloudflared
   # paste the `cloudflared service install <token>` command from the dashboard
   ```
4. Back in the dashboard, add a **Public Hostname**:
   - Subdomain: `leaderboard`
   - Domain: `yourdomain.com`
   - Service: `HTTP` → `localhost:8080`
5. Save. Visit `https://leaderboard.yourdomain.com`. Done.

Cloudflare gives you HTTPS, DDoS protection, and an edge cache for static
assets without any extra config. If you ever want to lock the leaderboard
behind a login, add a Cloudflare Access policy on the same hostname.

## Alternative public-access options

- **Tailscale Funnel** — same idea, simpler if you already use Tailscale, but
  you need a `*.ts.net` hostname (no custom domain).
- **ngrok** — easiest to start but the free tier rotates URLs and rate-limits.
- **Direct port forward + DDNS** — works, but exposes your home IP and forces
  you to deal with HTTPS certs yourself.

## Day-to-day commands

```bash
# Tail logs
docker compose logs -f

# Restart after editing source
docker compose up -d --build

# Stop
docker compose down

# Wipe everything (including the SQLite DB!)
docker compose down -v && rm -rf data/
```

## Updating the scoring rubric

Edit `config.yaml`, then `docker compose restart leaderboard`. The new config
takes effect on the next submission. Existing scores are not re-computed.

If you want to use a different rubric than the research loop, override
`vlm_judge.principles` here — same schema AutoDesign supports.

## What happens per submission

1. `POST /api/submit` inserts a row with `status = queued`.
2. The worker thread claims it (`status = running`), then calls
   `pipeline.rank.rank_urls([url], config)`.
3. `rank_urls` → `capture.capture(url)` → Playwright loads the URL, takes
   5 keyframe screenshots and records a 5s video, dumps `page.html`.
4. Each enabled signal in `config.yaml`'s `criteria:` runs against the
   captured frames + DOM.
5. Combined score = weighted mean over signals that returned a score.
6. Worker writes `status = done`, `combined`, full `score_json` to SQLite.
7. Frontend's poll picks up the result and the leaderboard refreshes.

A failure at any step marks the row `failed` with the exception text, visible
in the submission detail modal.

## Known limits

- **One submission at a time.** Torch + Chromium don't share a single process
  gracefully, and the saliency models are heavy. A queue of 10 may take 5–10
  minutes to drain depending on URL load times.
- **No focal-bbox per URL.** The submitter can give a brief but not a CTA
  region, so saliency's `intent_alignment` and `animation_focus` subscores
  always skip. `focus_clarity` and `reading_order` still run.
- **Cost per submission.** vlm_judge calls Claude Opus by default — roughly a
  few cents per scoring. Switch to Sonnet in `config.yaml` (`models.judge: sonnet`)
  for ~10× cheaper at some loss of taste.
