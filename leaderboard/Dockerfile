# AutoDesign Leaderboard image.
#
# Build context is the AutoDesign repo root (see docker-compose.yml: context: ..)
# so we can COPY the pipeline/ package into the image and `import pipeline.rank`
# without vendoring the code into the leaderboard/ folder.
#
# Base: Playwright's official Python image. It already has Chromium and all the
# system libs Playwright needs, on both amd64 and arm64 — which matters because
# Mac mini (M-series) builds for linux/arm64.

FROM mcr.microsoft.com/playwright/python:v1.49.1-jammy

ENV PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    LEADERBOARD_DATA_DIR=/data \
    LEADERBOARD_CONFIG=/app/leaderboard/config.yaml

# git is needed for the DeepGaze VCS install; the rest is already in the base.
RUN apt-get update \
 && apt-get install -y --no-install-recommends git \
 && rm -rf /var/lib/apt/lists/*

WORKDIR /app

# Torch first (separately from requirements.txt so the wheel selection is explicit
# and the layer caches independently of small requirement changes).
# CPU-only wheels keep the image down to a manageable size — the Mac mini's CPU
# is plenty for DeepGaze on a single page at a time.
RUN pip install --index-url https://download.pytorch.org/whl/cpu \
        torch==2.5.1 torchvision==0.20.1

# Python deps.
COPY leaderboard/requirements.txt /app/leaderboard/requirements.txt
RUN pip install -r /app/leaderboard/requirements.txt

# Application code. The pipeline/ package is needed because the leaderboard
# delegates scoring to pipeline.rank.rank_urls. CLAUDE.md / autodesign.md are
# copied too so the project root is a recognizable AutoDesign checkout (some
# pipeline helpers walk the tree looking for it).
COPY pipeline/        /app/pipeline/
COPY leaderboard/     /app/leaderboard/
COPY autodesign.md    /app/autodesign.md
COPY CLAUDE.md        /app/CLAUDE.md
# Trained brain_judge classifier (RBF SVM over perceptual stats). Without this
# the brain_judge signal silently skips. The file is ~1.4MB so it's fine to
# bake in rather than mount a volume.
COPY data/brain/      /app/data/brain/

# Pre-download the DeepGaze model weights so the first submission doesn't
# pay a 100MB+ cold-start cost (and so it doesn't hit bitbucket's flaky
# single-connection limits at runtime). The Geirhos ResNet-50 SIN+IN file
# served from bitbucket is the one that EOFs mid-stream when fetched via
# torch.hub; using curl with retries makes the build deterministic.
ENV TORCH_HUB_CACHE=/root/.cache/torch/hub/checkpoints
RUN mkdir -p "$TORCH_HUB_CACHE" \
 && curl -L --retry 12 --retry-delay 5 --retry-all-errors \
        --max-time 1200 --connect-timeout 30 -fSso \
        "$TORCH_HUB_CACHE/resnet50_finetune_60_epochs_lr_decay_after_30_start_resnet50_train_45_epochs_combined_IN_SF-ca06340c.pth.tar" \
        "https://bitbucket.org/robert_geirhos/texture-vs-shape-pretrained-models/raw/60b770e128fffcbd8562a3ab3546c1a735432d03/resnet50_finetune_60_epochs_lr_decay_after_30_start_resnet50_train_45_epochs_combined_IN_SF-ca06340c.pth.tar" \
 && python -c "import deepgaze_pytorch as d; d.DeepGazeIIE(pretrained=True); d.DeepGazeIII(pretrained=True); print('deepgaze pretrained models loaded ok')"

EXPOSE 8080
VOLUME ["/data"]

# uvicorn with one worker — the background scorer thread is per-process, and we
# only want one scorer running at a time anyway (torch + Playwright contention).
CMD ["uvicorn", "leaderboard.app:app", "--host", "0.0.0.0", "--port", "8080", "--workers", "1"]
