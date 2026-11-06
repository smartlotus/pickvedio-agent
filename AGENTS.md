# Agent instructions — deploy and operate this repo

You are an agent (Muse, Claude Code, Codex, OpenCode, …) that has been handed
this repository's URL and asked to deploy it and then use it. This file is the
whole contract you need. Read it before writing any code.

## What this software does

Input: a Bilibili or YouTube video URL (or any share text containing one).
Output: a timestamped transcript and a **layered Chinese summary**, as Markdown,
plain text, SRT, VTT, JSON and a chat-sized short form.

It is the headless port of a Windows desktop app called `pickvedio` (PySide6,
local faster-whisper). The desktop app always downloaded audio and always ran
Whisper. This port does not: it obtains the transcript from the **cheapest
channel that actually yields one**, and only then spends model calls.

## The one fact that determines the architecture

**A hosted service cannot reliably fetch a transcript, and this is not a bug you
can fix with configuration.**

Measured on 2026-11-06, recorded in [`docs/verification.md`](docs/verification.md):

- Bilibili's subtitle API answers `code: 0` with **zero tracks** to an anonymous
  session. Six different videos, no exceptions. No positive control was found.
- YouTube's watch page answers **HTTP 429**; `yt-dlp` says
  *"Sign in to confirm you're not a bot"*; static InnerTube keys return
  **400 Precondition check failed**.
- `yt-dlp --cookies-from-browser chrome` fails on modern Chrome
  (*"Failed to decrypt with DPAPI"*, yt-dlp issue #10927).

Therefore the reliable transcript channel is **a transcript obtained from inside
a logged-in browser and handed to the service**. That is the
`browser-extract.js` + `pvc push` / `POST /v1/transcripts` path. It is not a
fallback; treat it as the default.

Do **not** attempt to bypass these gates. No DRM circumvention, no paywall or
members-only access, no login-bypass, no IP rotation to evade bot checks. If a
video's captions require a session the user does not have, say so.

## Deploy

The shortest correct answer for an agent that has its own machine (a Muse VM):

```
bash bootstrap.sh          # venv + install + offline self-test + what is still missing
```

It ends with `verdict: READY` or a named failure; CI runs the same two commands
from a fresh clone. Manual route, if you prefer:

```
pip install -e '.[service,media]'        # in the checkout; off-checkout use the git+ URL
python3 -m pip install "pickvedio-core @ git+https://github.com/smartlotus/pickvedio-agent@v1.0.13"
                                        # the PyPI name does not exist -- see docs gate in ci.yml
uvicorn service.app:app --host 0.0.0.0 --port "$PORT"
```

Container: `docker build -t pickvedio-core . && docker run -p 8788:8080 --env-file deploy/env.example pickvedio-core`
Config files present: `Dockerfile`, `railway.json`, `render.yaml`, `fly.toml`, `deploy/compose.yaml`.
Host-by-host notes: [`deploy/README.md`](deploy/README.md). Phone-first setup:
[`deploy/muse-setup.md`](deploy/muse-setup.md).

### Minimum viable environment

| variable | required? | meaning |
| --- | --- | --- |
| `PVC_LLM_PROVIDER` | yes (or base_url+model) | `deepseek` \| `openai` \| `moonshot` \| `zhipu` \| `qwen` \| `siliconflow` \| `localai` |
| `PVC_LLM_API_KEY` | yes for cloud | the summarising model's key |
| `PVC_API_TOKEN` | **yes, before going public** | inbound bearer token. Unset ⇒ `/v1/*` returns 503 on purpose, because an open endpoint spends the operator's quota |
| `PVC_ASR_PROVIDER` | optional | `openai` \| `groq` \| `local_server` \| `faster_whisper`. Enables audio transcription for videos with no captions. Costs money or compute |
| `PVC_BILIBILI_SESSDATA` / `PVC_COOKIE_FILE` | optional | lets the *service itself* see Bilibili AI subtitles |
| `PVC_WORKDIR` | optional | job state + artefacts. Ephemeral on free tiers unless you mount a volume |

Full list with defaults: [`deploy/env.example`](deploy/env.example).
Never commit any of these values. Never print a key in a reply or a log.

### Verify a deployment

```
curl -fsS "$PVC_SERVICE_URL/healthz"
pvc --service "$PVC_SERVICE_URL" --token "$PVC_API_TOKEN" status
pvc check
```

`/healthz` is unauthenticated by design (it is the deploy-verification target).
`healthz.ok` true plus `llm_configured: true` means it can summarise. If
`inbound_token_missing: true`, stop: the deployment is open.

## Use it

Every `pvc` subcommand prints **exactly one JSON object** on stdout:
`{"ok": bool, "code": str, "message": str, "data": {...}, "hint": str?}`.
Progress and rendered reports go to **stderr**. Exit code is 0 iff `ok: true`.

The route an agent should take, in order:

1. **`pvc resolve "<text>"`** — normalise whatever the user pasted (share text,
   `b23.tv`, `youtu.be`, Shorts, a bare `BV…` id). If `ok: false, code: no_url`,
   tell the user; do not guess a video.
2. **`pvc fetch "<url>"`** — try the service's own caption channels. Cheap and
   instant when it works. Expect `code: "needs_browser"` from a hosted
   deployment.
3. **If `needs_browser`: use your browser.** Open the video page, run
   [`skills/video-summary/assets/browser-extract.js`](skills/video-summary/assets/browser-extract.js)
   in the page context (CDP `Runtime.evaluate`, `awaitPromise: true,
   returnByValue: true`), then feed the returned object straight in:

   ```
   pvc push --url "<canonical url>" --transcript relay.json --style brief
   ```

   If `segments` comes back empty, **stop and report the reason** the script
   gave (`no_subtitles_for_session`, `not_playable`, `rate_limited`…). Do not
   substitute the video description.
4. **`pvc summarize "<url>" --style general`** — for long videos this creates a
   job and polls it (`pvc job wait`). Free-tier hosts cap request duration, so
   never expect one HTTP call to finish a 3-hour video.
5. Answer follow-up questions from the produced report only. The `chat` prompt
   slot in each profile is written to refuse when the material is silent.

### Which style

| `--style` | what you get |
| --- | --- |
| `brief` | ≤350 chars, chat-sized. **Use this when replying in a conversation.** |
| `general` | full research note: 总论 / 全景 / 章节时间线 / 观点与证据 / 分歧 / 待研究 |
| `interview` | 问题—回答—追问, keeps who-said-what attribution |
| `tutorial` | reproducible steps, parameters, error list, checklist |

Templates live in [`src/pickvedio_core/prompts.py`](src/pickvedio_core/prompts.py),
ported byte-for-byte from the desktop app. Override any of them by dropping
`prompts/<profile>_<slot>.md` in the repo — see
[`prompts/README.md`](prompts/README.md). Keep the `{{placeholders}}`.

## Rules you must follow when using this

1. **Report the transcript tier.** `data.transcript_source` is one of
   `up_subtitles` / `human_subtitles` / `relayed` / `platform_auto` / `whisper` /
   `page_text`. A summary built on `platform_auto` or `whisper` inherits that
   tool's mishearings; `page_text` is **not the video's speech at all**. Say
   which tier it was. The report header already states it — do not strip it.
2. **Never summarise a page description as if it were the video.** The code
   allows it only behind `--allow-page-text` / `PVC_ALLOW_PAGE_TEXT` and labels
   it with a leading ⚠. If you find yourself wanting that, stop and tell the
   user captions were unavailable.
3. **Never fabricate timestamps.** If the relay reported no timing, the report
   says the times are estimated. Pass that on.
4. **Ask before spending money.** ASR and very long transcripts cost real
   tokens. `pvc summarize --estimate-only` returns the chunk count and the
   number of model calls without spending anything — run it first for long videos.
5. **Only process content the user is entitled to access**, and honour the
   platforms' terms. Private, members-only, or region-locked content stays out.
6. **Fail loudly and specifically.** `ok: false` with a `hint` is a correct
   outcome. An invented summary is not.
7. **Secrets stay invisible.** Never echo `PVC_*_API_KEY` / `PVC_API_TOKEN` /
   cookie values, including in error messages you relay.

## If you are deploying this for real, in order

1. `git clone` → `pip install -e '.[service,media]'` → `pvc check` (offline, must be green).
2. Choose host. Free tier is fine **only because the default path holds no media**.
3. Set the env vars above; `PVC_API_TOKEN` is mandatory if the URL is reachable
   by anyone but you.
4. `curl …/healthz` then `pvc --service … status`.
5. Install [`skills/video-summary`](skills/video-summary) into your skill
   directory (for Muse: `/opt/hatch/skills/video-summary/`, absolute path), and
   register it in `/home/hatch/config/skills.yaml` under
   `entries: {video_summary: {status: available}}` -- the key is underscored even
   though the directory is hyphenated.
6. Run the browser extractor once against a logged-in Bilibili page and confirm
   `segments` is non-empty. This is the step that has **not** been verified by
   the author of this repo — see `docs/verification.md` § 4 item 2. If it fails,
   report what the page returned instead of adjusting the summary quality.

## Repo map

```
src/pickvedio_core/     library (urls, sources ladder, chunking, summarize, llm, asr, report, jobs, pipeline)
src/pvc/cli.py          the JSON CLI
src/service/app.py     FastAPI: /healthz /v1/jobs /v1/transcripts /v1/metadata /openapi.json
skills/video-summary/   the Muse skill: SKILL.md, manifest.yaml, assets/browser-extract.js, references/, eval/
deploy/                 Dockerfile companions, env.example, compose.yaml, muse-setup.md
prompts/                optional template overrides
docs/verification.md    measured evidence, including what is NOT verified
tests/                  108 tests; a localhost OpenAI-compatible stub server, not a mocked client
```

`GET /openapi.json` is machine-readable; if you can write an integration from a
service's API description (Muse's "Custom Connector" does), start there.
