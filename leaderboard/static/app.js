// AutoDesign leaderboard — vanilla SPA.

const form = document.getElementById("submit-form");
const submitBtn = document.getElementById("submit-btn");
const submitStatus = document.getElementById("submit-status");
const boardTop = document.getElementById("board-top");
const boardRecent = document.getElementById("board-recent");
const modal = document.getElementById("detail-modal");
const modalBody = document.getElementById("modal-body");
const modalClose = document.getElementById("modal-close");

const POLL_MS = 4000;
let pendingId = null;
let pendingPollTimer = null;

// ---- submit ----

form.addEventListener("submit", async (e) => {
  e.preventDefault();
  const fd = new FormData(form);
  const body = {
    url: (fd.get("url") || "").trim(),
    title: (fd.get("title") || "").trim(),
    submitter: (fd.get("submitter") || "").trim(),
  };
  if (!body.url) return;
  submitBtn.disabled = true;
  setStatus("queueing…", "");
  try {
    const res = await fetch("/api/submit", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(body),
    });
    if (!res.ok) {
      const err = await res.json().catch(() => ({}));
      throw new Error(err.detail || `submit failed (${res.status})`);
    }
    const data = await res.json();
    pendingId = data.id;
    setStatus(`queued as #${data.id} — scoring in progress`, "ok");
    form.querySelector("input[name=url]").value = "";
    refreshAll();
    pollPending();
  } catch (err) {
    setStatus(err.message || String(err), "err");
  } finally {
    submitBtn.disabled = false;
  }
});

function setStatus(text, cls) {
  submitStatus.className = cls || "";
  submitStatus.textContent = text;
}

async function pollPending() {
  clearTimeout(pendingPollTimer);
  if (!pendingId) return;
  try {
    const res = await fetch(`/api/submission/${pendingId}`);
    if (!res.ok) throw new Error("submission gone");
    const sub = await res.json();
    if (sub.status === "done") {
      setStatus(`#${sub.id} scored ${fmtScore(sub.combined)} / 10`, "ok");
      pendingId = null;
      refreshAll();
      openDetail(sub.id);
      return;
    }
    if (sub.status === "failed") {
      setStatus(`#${sub.id} failed: ${truncate(sub.error || "unknown", 200)}`, "err");
      pendingId = null;
      refreshAll();
      return;
    }
    setStatus(`#${sub.id} ${sub.status}…`, "");
    pendingPollTimer = setTimeout(pollPending, POLL_MS);
  } catch (err) {
    setStatus(err.message || String(err), "err");
    pendingId = null;
  }
}

// ---- tabs ----

document.querySelectorAll(".tab").forEach((tab) => {
  tab.addEventListener("click", () => {
    document.querySelectorAll(".tab").forEach((t) => t.classList.remove("active"));
    tab.classList.add("active");
    const which = tab.dataset.tab;
    boardTop.classList.toggle("active", which === "top");
    boardRecent.classList.toggle("active", which === "recent");
  });
});

// ---- helpers ----

function fmtScore(v) {
  if (v === null || v === undefined) return "—";
  return Number(v).toFixed(2);
}
function rankClass(i) {
  if (i === 0) return "gold";
  if (i === 1) return "silver";
  if (i === 2) return "bronze";
  return "";
}
function truncate(s, n) {
  s = String(s || "");
  return s.length > n ? s.slice(0, n - 1) + "…" : s;
}
function hostname(url) {
  try { return new URL(url).hostname; } catch { return url; }
}
function relTime(unixSec) {
  if (!unixSec) return "";
  const diff = Date.now() / 1000 - Number(unixSec);
  if (diff < 60) return "just now";
  if (diff < 3600) return `${Math.floor(diff / 60)}m ago`;
  if (diff < 86400) return `${Math.floor(diff / 3600)}h ago`;
  return `${Math.floor(diff / 86400)}d ago`;
}
function escapeHtml(s) {
  return String(s ?? "").replace(/[&<>"']/g, (c) => ({
    "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;",
  }[c]));
}
function rankLabel(i) {
  return String(i + 1).padStart(2, "0");
}

// ---- entry rendering ----

function entryHtml(row, i, mode) {
  const title = row.title || hostname(row.url);
  const submitter = row.submitter ? `<span class="by">${escapeHtml(row.submitter)}</span><span class="sep">·</span>` : "";
  const time = mode === "recent" ? `${relTime(row.created_at)}<span class="sep">·</span>` : "";
  const scoreCell = row.combined !== null && row.combined !== undefined
    ? `<div class="score ${mode === 'recent' ? 'small' : ''}">${fmtScore(row.combined)}<sup>/ 10</sup></div>`
    : `<div class="status-chip ${row.status}">${row.status}</div>`;
  return `
    <div class="entry ${mode !== 'recent' ? rankClass(i) : ''}" data-id="${row.id}">
      <div class="rank">${rankLabel(i)}</div>
      <div class="entry-main">
        <div class="entry-title">${escapeHtml(title)}</div>
        <div class="entry-meta">${submitter}${time}${escapeHtml(hostname(row.url))}</div>
      </div>
      <div class="score-cell">${scoreCell}</div>
    </div>
  `;
}

async function refreshLeaderboard() {
  try {
    const res = await fetch("/api/leaderboard?limit=100");
    const rows = await res.json();
    if (!rows.length) {
      boardTop.innerHTML = `<div class="empty">No scored submissions yet — be the first.</div>`;
      return;
    }
    boardTop.innerHTML = rows.map((r, i) => entryHtml(r, i, "top")).join("");
    boardTop.querySelectorAll(".entry").forEach((el) =>
      el.addEventListener("click", () => openDetail(Number(el.dataset.id)))
    );
    document.getElementById("m-done").textContent = rows.length;
  } catch {
    boardTop.innerHTML = `<div class="empty">Couldn't load the leaderboard.</div>`;
  }
}

async function refreshRecent() {
  try {
    const res = await fetch("/api/recent?limit=30");
    const rows = await res.json();
    if (!rows.length) {
      boardRecent.innerHTML = `<div class="empty">Nothing submitted yet.</div>`;
      return;
    }
    boardRecent.innerHTML = rows.map((r, i) => entryHtml(r, i, "recent")).join("");
    boardRecent.querySelectorAll(".entry").forEach((el) =>
      el.addEventListener("click", () => openDetail(Number(el.dataset.id)))
    );
  } catch {
    boardRecent.innerHTML = `<div class="empty">Couldn't load activity.</div>`;
  }
}

async function refreshQueue() {
  try {
    const res = await fetch("/api/queue");
    const q = await res.json();
    document.getElementById("q-queued").textContent  = q.queued ?? 0;
    document.getElementById("q-running").textContent = q.running ?? 0;
    document.getElementById("q-done").textContent    = q.done ?? 0;
    document.getElementById("q-failed").textContent  = q.failed ?? 0;
  } catch { /* ignore */ }
}

function refreshAll() {
  refreshLeaderboard();
  refreshRecent();
  refreshQueue();
}

// ---- detail modal ----

modalClose.addEventListener("click", closeDetail);
modal.addEventListener("click", (e) => { if (e.target === modal) closeDetail(); });
document.addEventListener("keydown", (e) => { if (e.key === "Escape") closeDetail(); });

function closeDetail() {
  modal.classList.remove("open");
  modalBody.innerHTML = "";
}

async function openDetail(id) {
  try {
    const res = await fetch(`/api/submission/${id}`);
    if (!res.ok) return;
    const sub = await res.json();
    modalBody.innerHTML = detailHtml(sub);
    modal.classList.add("open");
  } catch { /* ignore */ }
}

// The 8 themed buckets. Each bucket lists the metric keys it contains and where
// to pull each from. `source` controls which slot of the score JSON to read:
//   "saliency"     → raw.saliency.details.subscores[key]   (0..1, * 10 for display)
//   "vlm"          → raw.vlm_judge.details.subscores[key]
//   "signal"       → score.per_criterion[key]              (already 0..10)
const BUCKETS = [
  { name: "Attention",         source_label: "saliency",
    metrics: [
      { key: "intent_alignment", wt: 0.45, source: "saliency",
        blurb: "Predicted attention vs. intended focal region — share & whether the first fixation lands there." },
      { key: "focus_clarity",    wt: 0.20, source: "saliency",
        blurb: "Peak dominance — is there one clear focal point, or is attention flat?" },
      { key: "reading_order",    wt: 0.20, source: "saliency",
        blurb: "Scanpath flow — share of saccades moving down/right, magnitude-weighted." },
    ],
  },
  { name: "Motion",             source_label: "saliency + vlm",
    metrics: [
      { key: "animation_focus",  wt: 0.15, source: "saliency",
        blurb: "Multi-frame: does motion resolve attention onto the target by the settled frame?" },
      { key: "motion",           wt: 2.0,  source: "vlm",
        blurb: "Purposeful entrance that terminates on the CTA; no distracting looping motion." },
    ],
  },
  { name: "Hierarchy & layout", source_label: "vlm",
    metrics: [
      { key: "visual_hierarchy", wt: 1.2, source: "vlm",
        blurb: "A dominant focal point and clear visual order." },
      { key: "layout_spacing",   wt: 0.6, source: "vlm",
        blurb: "Spacing, alignment, grid discipline." },
    ],
  },
  { name: "Color & type",       source_label: "vlm",
    metrics: [
      { key: "color_contrast",   wt: 1.1, source: "vlm",
        blurb: "Palette cohesion + legible contrast." },
      { key: "typography",       wt: 0.7, source: "vlm",
        blurb: "Type pairing, scale, rhythm." },
      { key: "consistency",      wt: 0.5, source: "vlm",
        blurb: "Uniform styling across elements/frames." },
    ],
  },
  { name: "Distinctiveness",    source_label: "vlm + signal",
    metrics: [
      { key: "creativity",       wt: 3.5, source: "vlm",
        blurb: "Distance from the AI-slop baseline — personality, would-screenshot-it distinctiveness." },
      { key: "originality",      wt: 3.0, source: "vlm",
        blurb: "How much it stands out vs. real competitor screenshots (only when references found)." },
      { key: "ai_pitfalls",      wt: 2.5, source: "vlm",
        blurb: "Avoidance of AI-builder fingerprints / similarity to known AI-template sites." },
      { key: "brain_judge",      wt: 0.2, source: "signal",
        blurb: "Perceptual classifier P(award-winning vs slop): RBF SVM over clutter, color, whitespace, contrast, symmetry, hue-entropy." },
    ],
  },
];
// Usability (affordance_clarity) and Function (stress_test) are intentionally
// not shown on the public leaderboard — they only make sense against a brief
// and on synthetic demos, not arbitrary deployed sites.

function readMetric(metric, judge, saliency, perCriterion, judgeWhy, salWhy, judgeWeights) {
  // Returns { score10, why, weight, present }.
  // Skipped metrics return { present: false }.
  if (metric.source === "saliency") {
    const v = (saliency.subscores || {})[metric.key];
    if (v === null || v === undefined) return { present: false };
    return {
      present: true,
      score10: Number(v) * 10,
      why: (saliency.explanations || {})[metric.key] || metric.blurb,
      weight: (saliency.weights || {})[metric.key] ?? metric.wt,
    };
  }
  if (metric.source === "vlm") {
    const v = (judge.subscores || {})[metric.key];
    if (v === null || v === undefined) return { present: false };
    return {
      present: true,
      score10: Number(v) * 10,
      why: (judge.explanations || {})[metric.key] || metric.blurb,
      weight: (judge.weights || {})[metric.key] ?? metric.wt,
    };
  }
  if (metric.source === "signal") {
    const v = perCriterion[metric.key];
    if (v === null || v === undefined) return { present: false };
    return {
      present: true,
      score10: Number(v),
      why: metric.blurb,
      weight: metric.wt,
    };
  }
  return { present: false };
}

function bucketHtml(bucket, i, judge, saliency, perCriterion) {
  // Only render metrics that actually produced a score. Skipped metrics are
  // EXCLUDED from the weighted average — they're not penalties — so hiding
  // them keeps the modal honest about what contributed to the bucket score.
  const present = bucket.metrics
    .map((m) => ({ m, r: readMetric(m, judge, saliency, perCriterion) }))
    .filter((x) => x.r.present);

  // Hide the entire bucket if nothing in it scored — better than showing an
  // "n/a" bucket header that looks like a missing grade.
  if (present.length === 0) return "";

  const rows = present.map(({ m, r }) => `
    <div class="metric">
      <div class="left">
        <div class="name">${escapeHtml(m.key)}<span class="wt">wt ${m.wt.toFixed(2)}</span></div>
        <div class="why">${escapeHtml(r.why || m.blurb)}</div>
      </div>
      <div class="val">${r.score10.toFixed(1)}<sup>/ 10</sup></div>
    </div>
  `).join("");

  const wsum = present.reduce((a, x) => a + x.r.weight, 0);
  const num  = present.reduce((a, x) => a + x.r.weight * x.r.score10, 0);
  const agg = wsum > 0 ? num / wsum : 0;

  return `
    <section class="bucket">
      <div class="bucket-head">
        <h4 class="bucket-h">
          <span class="num">${String(i + 1).padStart(2, "0")}</span>${escapeHtml(bucket.name)}
          <span class="src">${escapeHtml(bucket.source_label)}</span>
        </h4>
        <div class="bucket-score">${agg.toFixed(1)}<sup>/ 10</sup></div>
      </div>
      <div class="bucket-rows">${rows}</div>
    </section>
  `;
}

function detailHtml(sub) {
  const score = sub.score || {};
  const raw = score.raw || {};
  const judge = raw.vlm_judge?.details || {};
  const saliency = raw.saliency?.details || {};
  const perCriterion = score.per_criterion || {};

  const bucketsHtml = BUCKETS.map((b, i) =>
    bucketHtml(b, i, judge, saliency, perCriterion)
  ).join("");
  const judgeRows = "";  // legacy variable kept so existing template string compiles
  const salRows = "";

  const issues = (judge.issues || []).slice(0, 6);
  const issuesHtml = issues.length ? `
    <div class="issues">
      <div class="eyebrow">Top issues</div>
      <ol>${issues.map((it) => `
        <li><strong>${escapeHtml(it.where || "")}</strong> — ${escapeHtml(it.problem || "")}
        ${it.fix ? `<em>fix: ${escapeHtml(it.fix)}</em>` : ""}</li>
      `).join("")}</ol>
    </div>` : "";

  const explorations = (judge.explorations || []).slice(0, 4);
  const explorationsHtml = explorations.length ? `
    <div class="issues">
      <div class="eyebrow">Creative explorations</div>
      <ol>${explorations.map((it) => `
        <li><strong>${escapeHtml(it.lacks || "—")}</strong> — ${escapeHtml(it.idea || "")}
        ${it.principle ? `<em>lifts: ${escapeHtml(it.principle)}</em>` : ""}</li>
      `).join("")}</ol>
    </div>` : "";

  const nFrames = Number(score.n_frames || 0);
  const lastIdx = nFrames > 0 ? nFrames - 1 : 0;
  const settledUrl = `/artifacts/${sub.id}/frames/${String(lastIdx).padStart(4, "0")}.png`;
  const saliencyUrl = `/artifacts/${sub.id}/saliency.png`;

  const stageHtml = `
    <div class="stage-block">
      <div class="eyebrow">View</div>
      <div class="stage" data-sub="${sub.id}">
        <img class="layer captured-layer" src="${settledUrl}" alt="settled frame" data-role="captured">
        <iframe class="layer iframe-layer" data-role="live" data-src="${escapeHtml(sub.url)}" hidden
                referrerpolicy="no-referrer" sandbox="allow-same-origin allow-scripts allow-forms"></iframe>
        <img class="layer saliency-layer" src="${saliencyUrl}" alt="saliency overlay" data-role="saliency" onerror="this.style.display='none'">
      </div>
      <div class="stage-bar">
        <div class="seg" data-role="view-seg">
          <button data-action="view-captured" class="active">Captured</button>
          <button data-action="view-live">Live iframe</button>
        </div>
        <div class="toggle on" data-action="toggle-saliency">
          <span class="label-no">Saliency</span>
          <span class="switch" aria-hidden="true"></span>
          <span class="label-yes">On</span>
        </div>
        <div class="spacer"></div>
        <a class="open-live" href="${escapeHtml(sub.url)}" target="_blank" rel="noopener noreferrer">open in new tab ↗</a>
      </div>
    </div>
  `;

  const errorBlock = sub.status === "failed"
    ? `<div class="errorblock"><div class="eyebrow">Error</div><pre>${escapeHtml(sub.error || "unknown")}</pre></div>`
    : "";

  const headerScore = (sub.combined !== null && sub.combined !== undefined)
    ? `<div class="big-score">${fmtScore(sub.combined)}<sup>/ 10</sup></div>` : "";

  const critique = judge.critique
    ? `<p class="critique">${escapeHtml(judge.critique)}</p>` : "";

  return `
    <div class="eyebrow">Submission #${sub.id} · ${escapeHtml(sub.status)}</div>
    <h3 class="title">${escapeHtml(sub.title || hostname(sub.url))}</h3>
    <div class="url"><a href="${escapeHtml(sub.url)}" target="_blank" rel="noopener">${escapeHtml(sub.url)}</a></div>
    ${headerScore}
    ${critique}
    ${bucketsHtml ? `<div class="buckets">${bucketsHtml}</div>` : ""}
    ${explorationsHtml}
    ${issuesHtml}
    ${stageHtml}
    ${errorBlock}
  `;
}

// Delegated stage controls.
modalBody.addEventListener("click", (e) => {
  // toggle (whole element is clickable, label or switch)
  const toggle = e.target.closest(".toggle[data-action=toggle-saliency]");
  if (toggle) {
    const stage = modalBody.querySelector(".stage");
    if (!stage) return;
    const saliency = stage.querySelector("[data-role=saliency]");
    const on = !toggle.classList.contains("on");
    toggle.classList.toggle("on", on);
    toggle.querySelector(".label-yes").textContent = on ? "On" : "Off";
    if (saliency) saliency.classList.toggle("off", !on);
    return;
  }
  // segmented view (captured | live)
  const segBtn = e.target.closest(".seg button");
  if (segBtn) {
    const seg = segBtn.parentElement;
    const stage = modalBody.querySelector(".stage");
    if (!stage) return;
    const captured = stage.querySelector("[data-role=captured]");
    const live = stage.querySelector("[data-role=live]");
    seg.querySelectorAll("button").forEach((b) => b.classList.toggle("active", b === segBtn));
    if (segBtn.dataset.action === "view-captured") {
      captured.hidden = false;
      live.hidden = true;
      if (live.src) live.removeAttribute("src");
    } else {
      captured.hidden = true;
      live.hidden = false;
      if (!live.src && live.dataset.src) live.src = live.dataset.src;
    }
  }
});

// ---- boot ----

refreshAll();
setInterval(refreshAll, 8000);
