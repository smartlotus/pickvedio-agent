# Deployment files (deploy/)

Everything in this directory exists so one thing is possible: send Muse a GitHub
repo URL and get back a running `pvc` HTTP service.

| file | what it is |
| --- | --- |
| `README.md` | this file: which host to pick, what breaks, where job state lives, where the logs are |
| `compose.yaml` | the same image run locally with a named volume, plus the optional self-hosted ASR server |
| `env.example` | every `PVC_*` variable with a one-line meaning and a safe default |
| `muse-setup.md` | the paste-ready instructions for the phone workflow, incl. the no-hosting fallback |
| `healthcheck.py` | the container health probe (`/healthz`, reads `$PORT`, no curl in the image) |

The host-side config files live at the repository root because that is where
those platforms look: `Dockerfile`, `railway.json`, `render.yaml`, `fly.toml`,
`runtime.txt`.

## Read this first: the container does not transcribe by default

Verified against the real platforms from a datacenter IP:

- YouTube answers an anonymous request with a bot check ("Sign in to confirm
  you're not a bot", HTTP 429), and static InnerTube keys return 400
  "Precondition check failed".
- Bilibili returns an **empty** subtitle list for an anonymous session; AI
  subtitles are only served to a logged-in user.

So the DEFAULT production path is the **browser relay**: Muse opens the video in
its own logged-in cloud browser, runs
`skills/video-summary/assets/browser-extract.js`, and POSTs the segments to
`POST /v1/transcripts`. That route needs **no yt-dlp, no ffmpeg and no ASR
provider** -- the deployed container only chunks the transcript and calls your
LLM. Downloading audio and running ASR is the **optional heavy path**: build the
image with `--build-arg INCLUDE_FFMPEG=1` and set `PVC_ASR_PROVIDER`, and expect
it to fail on hosts with no apt and a read-only filesystem.

Because a 3-hour video takes minutes, **every hosted route here uses async
jobs**: `POST /v1/jobs` returns a `job_id` immediately and the client polls
`GET /v1/jobs/{id}` (the CLI wraps that in `pvc job wait`). No timeout increase
changes that.

## Host comparison

| host | how Muse deploys it | what breaks on the free tier | where job state lives | how to see logs |
| --- | --- | --- | --- | --- |
| **Railway** (primary) | "用你的 Railway 连接器部署这个公开仓库 …" -- the connector builds the committed `Dockerfile`; exact script in `muse-setup.md` and in `../skills/video-summary/references/deploy-runbook.md` | trial/plan credit runs out (service stops); gateway cuts long requests (~15-60 s) so jobs MUST be async; `/data` is wiped on redeploy **unless you attach a Volume** | `$PVC_WORKDIR` (`/data`) with an attached volume; otherwise ephemeral | Railway dashboard -> Deployments -> select deployment -> `View Logs`; or `railway logs`; rebuild logs in the same panel |
| **Render** (Blueprint, `render.yaml`) | no connector -- paste the blueprint or let Render read `render.yaml` from the repo; alternatively ask Muse to do it through its browser | free web services sleep after ~15 min idle (first request 10-60 s); ~512 MB RAM and a ~100 s request cap; **`disk:` in `render.yaml` is ignored on `plan: free`** (persistent disks need a paid instance type, and Render's minimum is 1 GB, not "a few hundred MB") | environment disk at `/data` when paid; ephemeral on free | service -> Logs (build vs runtime tabs); `Render -> Events` for restarts |
| **Fly.io** (`fly.toml`) | ask Muse to run the `fly` CLI in its VM (`fly launch && fly deploy`) -- works only where a terminal plus a Fly token exist | no permanently free app tier (trial credit only); shared CPU is tight for a 40-chunk summary; volumes need a running machine first (`fly volume create pvc_data`) | Fly volume mounted at `/data` (`[mounts]`) -> survives restarts and redeploys | `fly logs` (or `fly machine events`); dashboard -> Monitoring |
| **Vercel / Replit** (Muse has connectors for both) | possible, but Vercel is a poor fit: Python runs in request-scoped functions with no apt, no ffmpeg, a read-only FS and a hard function duration cap, so the ASR path does not exist and job state must live in external storage | cold start + function timeout; the `[mounts]`-style local disk does not exist | nowhere durable unless you add a DB/blob | platform function logs (`vercel logs`, or the connector's log view) |
| **Muse's own VM** (no hosting at all) | `git clone` + `pip install -e '.[service]'`, then run `pvc` locally or `uvicorn` on localhost -- see the fallback in `muse-setup.md` | the VM is recreated periodically: everything under `$PVC_WORKDIR` goes with it, and there is no public URL for other devices | the VM filesystem (`$HOME/pickvedio-work`) | the terminal session Muse shows you; nothing is persisted centrally |
| **Local Docker** (`compose.yaml`) | `docker compose -f deploy/compose.yaml up --build` | none -- this is the debugging environment, and the closest thing to Railway's runtime | named volume `pvc-data` -> `/data` | `docker compose logs -f pvc`; `docker inspect --format '{{json .State.Health}}' pickvedio-core` |

## Where job state actually lives

The service writes everything under `PVC_WORKDIR`: a `jobs.sqlite3` index plus
one directory per job (`jobs/<id>/{source,audio,transcript,summary}`). In the
container `PVC_WORKDIR=/data`, so a volume at `/data` is exactly what preserves
job ids and finished reports across a restart or a redeploy; without one, the
index disappears and a `job_id` from an earlier deployment is meaningless.
`PVC_WORKERS` (default 2) sizes the in-process job pool, and `PVC_AUTOCREATE`
must stay `1` because `uvicorn service.app:app` builds the app at import time.
`GET /healthz` reports the pool size and whether `PVC_API_TOKEN` is missing --
both without a token and without leaking any value.

## Why the Dockerfile is the primary path (and what `runtime.txt` is for)

Every host above except a bare VM can build `Dockerfile`, and the Dockerfile is
the only place where the image contents are *decidable*: the base Python
version, the extras that get installed (`.[service,media]`), whether ffmpeg
exists at all (`INCLUDE_FFMPEG`), the non-root user, `/data` as a volume, and
the health check. A platform-selected "Python runtime" is not equivalent -- it
chooses its own dependency resolution, gets no ffmpeg, has no health check and
no volume, and it is the path on which the ASR feature quietly disappears.

`runtime.txt` (`python-3.12`) therefore exists only as a fallback hint for hosts
that ignore the Dockerfile and pick a Python build themselves (Railpack/Nixpacks
fallback builds, Heroku-style detectors, Muse's VM bootstrapping). It pins the
same minor version the Dockerfile uses, so the two paths cannot drift silently
-- but if both a Dockerfile and `runtime.txt` are honoured, the Dockerfile wins,
and that is the intended behaviour.

## Local: compose

```bash
cp deploy/env.example .env    # fill PVC_API_TOKEN + PVC_LLM_API_KEY only
docker compose -f deploy/compose.yaml up --build
curl -fsS http://127.0.0.1:8080/healthz

# relay path smoke test (no download, no ASR):
curl -fsS -X POST http://127.0.0.1:8080/v1/transcripts \
  -H "Authorization: Bearer $PVC_API_TOKEN" -H 'content-type: application/json' \
  -d '{"url":"https://www.bilibili.com/video/BV1rjKn6CETw/","title":"smoke","duration":6,"segments":[{"start":0,"end":6,"text":"这是一条用于连通性验证的短句。"}]}'

# optional heavy ASR path (multi-GB image, CPU-bound, model download on first run):
docker compose -f deploy/compose.yaml --profile asr up --build
```

`deploy/compose.yaml` deliberately starts the ASR server behind a `profile`
(`--profile asr`), because it is not part of the default flow and its image is
heavy. The image name used there is an operator choice, not a dependency: check
the upstream README for the tag and port before relying on it, and CI never
builds it.

## Secrets

No secret belongs in this repository, in a chat transcript, in a build log, or
in a `Dockerfile`/`railway.json`/`render.yaml`/`fly.toml` value. Set them in the
host's own store:

```bash
python3 -c "import secrets; print(secrets.token_hex(24))"   # PVC_API_TOKEN
```

- Railway: Variables tab (private variables).
- Render: `sync: false` entries in the blueprint / Environment -> Secret Env.
- Fly: `fly secrets set PVC_API_TOKEN=... PVC_LLM_API_KEY=...`.
- Compose: a local `.env` (git-ignored) -- `deploy/env.example` is the template
  and is committed on purpose.

`PVC_LLM_API_KEY` and `PVC_ASR_API_KEY` are the user's own provider keys: a
deployment is impossible without an LLM key unless the summariser is a local
endpoint (no key required for `localai`/Ollama-style base URLs). `PVC_API_TOKEN`
is the inbound token guarding `/v1/*`.
