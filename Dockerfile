# syntax=docker/dockerfile:1
#
# pickvedio-core container image
# ============================================================================
# WHAT THIS IMAGE IS
#   A headless HTTP service (`service.app:app`) plus the `pvc` CLI inside it.
#   It does NOT transcribe video by default. The production default transcript
#   channel is the BROWSER RELAY: a logged-in agent browser (e.g. Muse) reads
#   the captions on the video page and POSTs them to `POST /v1/transcripts`.
#   That path needs no yt-dlp, no ffmpeg and no ASR provider, because
#   YouTube bot-gates datacenter IPs ("Sign in to confirm you're not a bot")
#   and Bilibili only serves AI subtitles to a logged-in session. Downloading
#   audio and running ASR is the OPTIONAL heavy path: build with
#   INCLUDE_FFMPEG=1 and set PVC_ASR_PROVIDER.
#
# BUILD
#   docker build -t pickvedio-core .
#   docker build -t pickvedio-core --build-arg INCLUDE_FFMPEG=1 .   # ASR path
#   docker build -t pickvedio-core --build-arg PYTHON_VERSION=3.11 .
#
# RUN
#   docker volume create pvc-data
#   docker run --rm -p 8080:8080 \
#     -e PORT=8080 \
#     -e PVC_API_TOKEN='<inbound bearer token>' \
#     -e PVC_LLM_PROVIDER=deepseek -e PVC_LLM_API_KEY='<your key>' \
#     -e PVC_WORKDIR=/data \
#     -v pvc-data:/data \
#     pickvedio-core
#
#   # smoke test, relay path only, costs nothing beyond one LLM call:
#   curl -fsS http://127.0.0.1:8080/healthz
#   curl -fsS -X POST http://127.0.0.1:8080/v1/transcripts \
#     -H "Authorization: Bearer $PVC_API_TOKEN" -H 'content-type: application/json' \
#     -d '{"url":"https://www.bilibili.com/video/BV1rjKn6CETw/","title":"smoke","duration":6,"segments":[{"start":0,"end":6,"text":"这是一条用于连通性验证的短句。"}]}'
#
# LOCAL COMPOSE (same image, named volume, optional asr service)
#   docker compose -f deploy/compose.yaml up --build
#   docker compose -f deploy/compose.yaml --profile asr up --build
#
# HOSTING NOTES
#   - Railway / Render / Fly all inject PORT; the entrypoint binds 0.0.0.0:$PORT.
#   - /data is a VOLUME. Without a volume/bind mount, job state, transcripts and
#     reports disappear on restart (free tiers are ephemeral by design).
#   - No dependency versions are pinned here on purpose: pyproject.toml owns the
#     version bounds, and `pip install .[service,media]` honours them.
#   - runtime.txt exists only for hosts that ignore a Dockerfile; this file is
#     the primary build path. See deploy/README.md.
# ============================================================================

ARG PYTHON_VERSION=3.12
FROM python:${PYTHON_VERSION}-slim

# ---------------------------------------------------------------------------
# Build-time options
#   INCLUDE_FFMPEG=1 pulls in ffmpeg (~400 MB installed, mostly shared libs and
#   codecs). It is ONLY needed to cut audio out of a downloaded video for the
#   ASR path. Leave it 0 for the relay/default deployment: the image stays
#   small and the heavy path is simply unavailable, which is exactly what the
#   browser-relay design expects.
# ---------------------------------------------------------------------------
ARG INCLUDE_FFMPEG=0
ARG APP_UID=10001
ARG APP_GID=10001

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    PORT=8080 \
    PVC_WORKDIR=/data \
    PVC_KEEP_MEDIA=0

WORKDIR /app

# ca-certificates is required for every HTTPS call the service makes
# (LLM providers, the optional ASR provider, the relay is browser-side).
# ffmpeg is conditional; nothing else is apt-installed.
RUN apt-get update \
 && if [ "$INCLUDE_FFMPEG" = "1" ]; then \
      apt-get install -y --no-install-recommends ca-certificates ffmpeg; \
    else \
      apt-get install -y --no-install-recommends ca-certificates; \
    fi \
 && rm -rf /var/lib/apt/lists/*

# README.md is not cosmetic: pyproject.toml declares `readme = "README.md"`, so
# the metadata build fails without it. prompts/ holds the optional per-profile
# prompt overrides (PVC_PROMPT_DIR); leaving PVC_PROMPT_DIR unset means the
# built-in templates in pickvedio_core.prompts are used instead.
COPY pyproject.toml README.md ./
COPY prompts ./prompts
COPY src ./src
COPY deploy/healthcheck.py ./deploy/healthcheck.py

# Library + console script (`pvc`) + the FastAPI/uvicorn extra. `media` adds
# yt-dlp for the optional download path; it is a pure-Python wheel, so it costs
# almost nothing and is kept in the default image.
RUN pip install --no-cache-dir --upgrade pip \
 && pip install --no-cache-dir '.[service,media]' \
 && python -c "import pickvedio_core, service.app" \
 && pvc --help >/dev/null

# Drop privileges. uvicorn only binds an unprivileged port (8080+), so no
# root tricks are needed.
RUN groupadd --system --gid ${APP_GID} pvc \
 && useradd --system --uid ${APP_UID} --gid ${APP_GID} --home-dir /app \
      --shell /usr/sbin/nologin --no-create-home pvc \
 && mkdir -p /data \
 && chown -R pvc:${APP_GID} /app /data

USER ${APP_UID}:${APP_GID}

# Declared so hosts create a real volume for it; `-v pvc-data:/data` keeps jobs
# (and their transcripts/reports) across restarts and redeploys.
# Caveat: a NAMED volume inherits the ownership baked in above, but a BIND
# mount (`-v $PWD/data:/data`) keeps host ownership and can then be unwritable
# by uid 10001 -- use a named volume, or `chown 10001:10001` the host dir.
VOLUME ["/data"]

EXPOSE 8080

# Exec-form health check: reads $PORT itself, so it works on Railway/Render/Fly
# and inside plain `docker run`. No curl in the image.
HEALTHCHECK --interval=30s --timeout=5s --start-period=40s --retries=3 \
  CMD ["python", "/app/deploy/healthcheck.py"]

# $PORT is the platform convention (Railway, Render, Fly, docker -e PORT).
# --proxy-headers + a wildcard forwarded-allow list is what makes the app see
# the real client IP and https:// scheme behind a platform router; uvicorn would
# otherwise trust only 127.0.0.1 and log every request as coming from the router.
ENTRYPOINT ["/bin/sh", "-c", "exec uvicorn service.app:app --host 0.0.0.0 --port \"${PORT:-8080}\" --proxy-headers --forwarded-allow-ips \"${FORWARDED_ALLOW_IPS:-*}\""]
