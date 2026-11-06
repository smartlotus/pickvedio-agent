---
name: "video-summary"
description: "Turn a Bilibili or YouTube link into a timestamped transcript and a layered Chinese summary. Use when the user sends a video URL or B站 share text and asks 总结/摘要/提炼/逐字稿/字幕/transcribe/summarize this video, including long videos that must be summarised asynchronously on the user's own deployed pickvedio service."
icon: "video"
metadata: { "includeInPrompt": false }
---

# Video Summary

## Purpose

Use the `pvc` CLI (package `pickvedio-core`) and, when configured, the user's
deployed pickvedio HTTP service to go from one video link to a Chinese layered
summary with timestamps. `pvc fetch` walks a transcript ladder;
`assets/browser-extract.js` is the relay tier that works when a datacenter IP
cannot reach the captions; the service does the chunking and the layered
summarising. 输入一条链接，输出带时间戳的中文分层总结。

## Tooling

Prefer the CLI on `PATH`. When this repo has been cloned into the agent's own
machine (the normal Muse-VM case), the CLI lives at `.venv/bin/pvc` after
`bash bootstrap.sh`, and there is no HTTP service at all: `pvc push` runs the
whole job locally. `--service`/`PVC_SERVICE_URL` is only for a hosted deployment.

If `pvc` is missing, install from this repository -- `pickvedio-core` is not on
PyPI, so a bare `pip install pickvedio-core` fails with "No matching distribution
found" and that is an install-path mistake, not a bug in the tool:

```bash
python3 -m pip install "pickvedio-core @ git+https://github.com/smartlotus/pickvedio-agent@v1.0.6"
```

Extras go inside the bracket before the `@`:
`pickvedio-core[service]` for the FastAPI app, `[media]` for yt-dlp,
`[local-asr]` for faster-whisper. From a clone, `bash bootstrap.sh` (or
`pip install -e '.[dev]'`) is cheaper because it also runs the self-test.

Core commands:
- `pvc status [--service <url>]`
- `pvc resolve --url "<video url or share text>"`
- `pvc fetch --url "<url>" [--out FILE] [--cookie-file FILE] [--sessdata VALUE]`
- `pvc summarize --url "<url>" [--style general|interview|tutorial|brief] [--out DIR] [--service URL] [--token VALUE]`
- `pvc push --url "<url>" --transcript FILE.json|- [--title T] [--duration S] [--style brief] [--out DIR]`
- `pvc job create --url "<url>" [--style S]`
- `pvc job status --id <ID>` / `pvc job wait --id <ID> [--timeout SECONDS]` / `pvc job list`
- `pvc models [--provider <P>]` / `pvc profiles` / `pvc check`

curl fallbacks, one per service route (all need the bearer token):
- health: `curl -fsS "$PVC_SERVICE_URL/healthz"`
- job: `curl -fsS -X POST "$PVC_SERVICE_URL/v1/jobs" -H "Authorization: Bearer $PVC_API_TOKEN" -H 'content-type: application/json' -d '{"url":"<canonical url>","style":"brief"}'`
- relayed transcript straight into a job: same POST with `"segments":[{"start":0,"end":4.4,"text":"…"}],"title":"…","duration":3121.0`
- transcript only: `curl -fsS -X POST "$PVC_SERVICE_URL/v1/transcripts" -H "Authorization: Bearer $PVC_API_TOKEN" -H 'content-type: application/json' --data @relay.json`
- status: `curl -fsS "$PVC_SERVICE_URL/v1/jobs/<id>" -H "Authorization: Bearer $PVC_API_TOKEN"`
- report: `curl -fsS "$PVC_SERVICE_URL/v1/jobs/<id>/report" -H "Authorization: Bearer $PVC_API_TOKEN"`
- discovery: `curl -fsS "$PVC_SERVICE_URL/openapi.json"`

Output contract:
- Every command prints exactly one JSON object on stdout with exactly three
  top-level keys -- `{"ok": ..., "code": ..., "message": ...}` -- plus `data`.
  Everything else lives inside `data`, on failures as well as successes: the
  remedy is `data.hint`, never a top-level `hint`. Parse `ok` first; on `false`,
  read `code`, `message` and `data.hint` before deciding anything. Progress
  lines go to stderr and are not part of the envelope.
- `resolve` -> `data.platform`, `data.video_id`, `data.title`, `data.duration`,
  plus the canonical `data.url`. Use the canonical URL in every later call.
  For a `b23.tv` short link `data.short_link` is `true` and `data.video_id` is
  empty -- that is the documented offline behaviour, not a parse failure: the
  later commands follow the redirect themselves.
- `fetch` -> `data.transcript` (`[{start, end, text}]`), `data.source`,
  `data.confidence` (0-5). Blocked-unauthenticated is
  `ok:false, code:"needs_browser"` with the relay steps in `hint`.
- `summarize` / `push` / `job wait` -> `data.summary` (Markdown),
  `data.report_markdown`, `data.usage`. With `--out DIR` they also return
  `data.artifacts`, a map of the eight file names actually written
  (`summary.md/.txt/.json`, `chat.md`, `transcript.json/.txt/.srt/.vtt`) --
  on the local pipeline and on a deployed service alike. No `--out`, no files:
  attach nothing and say so rather than implying a document exists. `push` and `job create` also return
  `data.job_id`; if the object does not already carry a finished `data.summary`,
  continue with `pvc job wait --id <job_id> --timeout 600`.
- `data.source` values and what they are worth: `up_subtitles` 5,
  `human_subtitles` 4, `relayed` 4, `platform_auto` 3, `whisper` 3,
  `page_text` 1, `unknown` 0.
- The transcript source belongs in the reply (see rule 6). A report whose source
  you cannot name is not finished.
- `check` runs the pipeline against a bundled fixture with no network; use it to
  separate "my config is broken" from "the platform is blocking me".

## Decision Ladder

Follow in order, stop at the first tier that yields real speech text. 逐级降级，
一旦拿到逐字稿就停止。

1. **Resolve.** `pvc resolve --url "<whatever the user pasted>"`. Share text,
   `b23.tv`, `?bvid=`, bare `BV…`, Shorts and `youtu.be` all canonicalise here.
   Several links in one message -> ask which one, or handle in reading order and
   say so.
2. **Ask the local resolver, or the service if there is one.**
   `pvc fetch --url "<canonical url>"` (add `--service` / set `PVC_SERVICE_URL`
   only when the user has a deployment). Ready to summarise straight away:
   `pvc summarize --url … --style brief`. Before spending on a long video,
   `pvc summarize --url … --estimate-only` reports chunk count and expected
   model calls and touches no provider.
3. **On `needs_browser`, relay.** Do not retry `fetch` and do not add cookies to
   it. Open the canonical URL in the logged-in cloud browser, evaluate
   `assets/browser-extract.js`, save the result as `relay.json`, then
   `pvc push --url "<canonical url>" --transcript relay.json --title "<title>" --duration <durationSeconds> --style brief`.
   Details and the empty-result taxonomy: `references/browser-extract.md`.
   No captions on the page -> tier 5, not a retry loop.
4. **Poll.** Long videos exceed free-tier request timeouts, so the service is
   asynchronous: `pvc job wait --id <job_id> --timeout 600` (it polls
   internally). Surface `data.stage`/`data.progress` while waiting; a stage stuck
   at `needs_browser` means the relay never landed.
5. **Emit.** Deliver the Chinese summary itself, then one provenance line: title,
   duration, `data.source` + `data.confidence`, segment/character count, model,
   token usage, and any `data.warnings`. Save `data.report_markdown` as a file
   and attach it when the user asked for a document.
6. **Honest failure.** If every tier is empty, report `code`, `message`, the
   extraction `reason`, and the two things that could work: a logged-in session
   for the relay tier, or ASR (`PVC_ASR_PROVIDER`, costs money, needs consent).
   `references/troubleshooting.md` maps every code to a fix.

## Auth

Auth contract:
- Run `pvc status` first. It lists configured channels, which `PVC_*` variables
  are set (names and booleans, never values), service reachability, and what is
  missing. Connection state comes from this turn's check, never from memory.
- Inbound service calls need `Authorization: Bearer $PVC_API_TOKEN`; pass it with
  `--token` or the environment. A 401 means the token is wrong or unset -- say so
  and ask the user; never guess or brute-force tokens.
- `code:"configuration"` means an environment variable is missing. Quote the
  `message`, which names the exact variable, and ask the user to set it.
- `code:"needs_auth"` means the content itself requires a logged-in account the
  user owns. Ask them to sign in in the Muse browser, then retry once.
- No logged-in video session: Bilibili AI subtitles do not exist for that
  request, so tier 3 returns `no_subtitles_for_session`. Tell the user plainly
  and offer (a) creator/human subtitles if the video has them, (b) paid ASR with
  their explicit approval, (c) the local desktop app with faster-whisper.

Credential storage:
- Secrets live in the environment or the service's secret store. Never in a
  transcript, a job directory, a report, a commit, or the chat log.
- Never print `$PVC_LLM_API_KEY`, `$PVC_ASR_API_KEY`, `$PVC_API_TOKEN`,
  `$PVC_BILIBILI_SESSDATA`, or cookie-file contents. Quote `code`/`message`/
  `hint` instead; if an error text contains a token, mask it before replying.
- Cookies are only ever the user's own, supplied as `--cookie-file` /
  `PVC_COOKIE_FILE` for a task they are entitled to run. Do not extract cookies
  from the user's local Chrome: on modern Chrome that fails with "Failed to
  decrypt with DPAPI" (yt-dlp issue #10927) and it is not the right pattern --
  use the Muse browser relay instead.

## Operating Rules

1. Only process content the user is entitled to access, and only through
   interfaces that are publicly reachable or that their own logged-in session
   already permits.
2. Never attempt to bypass a paywall, membership gate, region lock, login
   requirement, or DRM. `not_playable`, `caption_forbidden` and `needs_auth` are
   stop signals: report them, do not work around them.
3. Work the ladder in order and stop as soon as real caption or ASR text is in
   hand. Retrying a blocked anonymous request with the same parameters wastes
   rate budget and changes nothing.
4. Relay rather than re-probe when the answer is `needs_browser`. The browser
   tier is the designed path, not a fallback hack.
5. Prefer `--style brief` for a phone conversation and `general` / `interview` /
   `tutorial` when the user asks for a document, a study note, or a long video;
   run `pvc profiles` if unsure which is configured.
6. Disclose provenance. If `data.source` is `platform_auto`, `whisper` or
   `page_text`, say so in the reply: machine transcription propagates errors into
   names, numbers and quotes, and the user must know which tier they are reading.
7. Hard rule: a result derived from a page description, title, comments or an
   article (`page_text`) must be labelled as page text and never presented as a
   summary of what was said. If the user wanted the speech and only page text is
   available, that is a failure to report, not a document to deliver.
8. Ask before spending money. ASR over downloaded audio (`PVC_ASR_PROVIDER`) and
   paid LLM calls on very long videos both cost the user's budget. State the
   expected tier, the length, and ask once.
9. Use the async job API for anything long. Never hold a single synchronous
   HTTP request open past a free-tier timeout: `job create` then `job wait`.
10. On `rate_limited` or `network`, retry at most twice with backoff, then report
    the partial state. These are the only retryable codes; `needs_browser`,
    `needs_auth`, `configuration` and `dependency_missing` never become better by
    repeating them.
11. Never fabricate. No invented timestamps, no quotes the transcript does not
    contain, no "the video probably continues with". Gaps are reported as gaps.
12. Reply in the user's language, and write summaries in Chinese by default; the
    prose around the report stays in the language the user used.
13. Confirm before actions that cost money or mutate the deployment: creating a
    summarisation job, changing service variables, redeploying (see
    `references/deploy-runbook.md`).
14. If the same failure occurs three times, stop iterating and report the exact
    command, the `code`, the `message`, the extraction `reason`, and the one
    change that would unblock it.
