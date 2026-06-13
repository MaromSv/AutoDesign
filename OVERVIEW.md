# AutoDesign — overview

**Describe a UI in one sentence → AutoDesign builds it, grades it on 8 criteria, and improves it every round.**

It starts from a deliberately *minimal* page, then a panel of evaluators (vision models,
an attention model, agentic browser tests, and a perceptual classifier) tells it what to
fix and — more importantly — what bold design moves to try next. Repeat until it's good.

## The loop

```mermaid
flowchart LR
  Brief([Brief]) --> Gen["① Generate minimal UI<br/><b>Claude Sonnet</b>"]
  Gen --> Render["② Render &amp; film<br/><b>Playwright · Chromium</b>"]
  Render --> Score["③ Score<br/><b>8 criteria</b>"]
  Score --> Feedback["④ Feedback<br/>creative direction + fixes"]
  Feedback -->|refine the winner, repeat| Gen
  Score -->|target hit / rounds done| Final([final.html])
```

## The 8 criteria — and the technology behind each

```mermaid
flowchart TB
  subgraph S [Score each candidate]
    direction TB
    A["<b>Attention</b> — does the eye land on the CTA?<br/><i>DeepGaze IIE/III · PyTorch attention model</i>"]
    M["<b>Motion</b> — does the entrance resolve onto the CTA?<br/><i>DeepGaze + Claude vision judge</i>"]
    H["<b>Hierarchy &amp; Layout</b> — one clear visual order?<br/><i>Claude vision judge</i>"]
    C["<b>Color &amp; Type</b> — cohesive palette, legible?<br/><i>Claude vision judge</i>"]
    D["<b>Distinctiveness</b> — not AI-slop, stands out?<br/><i>Claude vision judge + slop-detector + RBF-SVM classifier</i>"]
    B["<b>Brief Fidelity</b> — is everything asked-for present?<br/><i>Nemotron text check + Claude vision judge</i>"]
    U["<b>Usability</b> — are the actions obvious?<br/><i>Claude vision judge</i>"]
    F["<b>Function</b> — do the controls actually work?<br/><i>Nemotron sub-agents driving Playwright/Chromium</i>"]
  end
```

## Technology legend

| Tech | Role |
|---|---|
| **Claude** (Sonnet) | Generates the UI, and acts as the **vision judge** that drives most design criteria + the creative direction for the next round |
| **DeepGaze IIE/III** (PyTorch) | Predicts human visual **attention** (heatmap + scanpath) — powers Attention & Motion |
| **Nemotron** (via Nebius) | **Sub-agents** that autonomously drive a headless browser to **stress-test interactions** (Function), and a text check for Brief Fidelity |
| **Playwright · Chromium** | Renders each candidate, films the entrance animation, and is the browser the agents drive |
| **slop-detector + scikit-learn SVM** | Flags AI-builder fingerprints and scores the perceptual "award-winning vs slop" fingerprint (Distinctiveness) |
| **Claude research agent + web search** | Finds real competitor sites so the judge can score **originality** (part of Distinctiveness) |
