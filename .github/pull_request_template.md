## What changes

One paragraph. Link the issue if there is one (`Fixes #`, `Refs #`).

## Contract impact

Tick what this PR touches, and say what the new value is:

- [ ] service route added/changed (`GET /healthz`, `GET /v1/profiles`,
      `GET /v1/capabilities`, `GET /v1/metadata`, `POST /v1/jobs`, `GET /v1/jobs`,
      `GET /v1/jobs/{id}`, `GET /v1/jobs/{id}/report`, `POST /v1/jobs/{id}/cancel`,
      `POST /v1/transcripts`, `GET /openapi.json`)
- [ ] `/v1/*` auth still required (`Authorization: Bearer $PVC_API_TOKEN`), and
      `/healthz` + `/openapi.json` still reachable without a token
- [ ] job pool / storage changed -> `PVC_WORKERS`, `PVC_AUTOCREATE`, or the
      `$PVC_WORKDIR/jobs.sqlite3` index
- [ ] JSON envelope fields (`ok` / `code` / `message` / `hint` / `data.*`)
- [ ] `PVC_*` variable added or its default changed -> `deploy/env.example` updated
- [ ] ASGI import path changed -> `Dockerfile`, `railway.json`, `render.yaml`, `fly.toml`,
      `deploy/compose.yaml` updated
- [ ] prompt template / `prompts/*.md` override behaviour changed
- [ ] skill pack changed -> `skills/video-summary/SKILL.md` + `manifest.yaml` still valid

## Why the transcript still comes from the browser by default

If this PR touches fetching at all, state here what happens from a datacenter IP
(YouTube bot-check, Bilibili empty subtitle list for an anonymous session) and
whether the relay path is still the default. Do not make the container look like
it transcribes on its own when it does not.

## Evidence

- [ ] `pip install -e '.[dev]'` then `pytest` passes
- [ ] `pvc --help` and `pvc check` pass
- [ ] `ruff check .` clean
- [ ] `docker build -t pickvedio-core .` succeeds, and
      `docker run -e PORT=8080 -e PVC_API_TOKEN=x -e PVC_LLM_PROVIDER=deepseek -e PVC_WORKDIR=/tmp/w pickvedio-core`
      answers `GET /healthz` without a token
- [ ] YAML/JSON files touched here parse (`python -c "import yaml; yaml.safe_load(open(...))"`)
- [ ] a relay smoke test (`pvc push --transcript smoke.json`) returns a job and a summary

## Cost / privacy

Any new paid call path (LLM tokens, ASR minutes), any new secret, and where it
lives. No secret value in the diff, the description, or a commit message.

## Docs kept in step

`README.md`, `docs/`, `deploy/README.md`, `deploy/muse-setup.md`,
`skills/video-summary/references/*`
