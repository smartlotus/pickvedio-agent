# Troubleshooting 排错

Read the envelope, not the stack trace: `code` chooses the branch, `message` names
the variable or endpoint, `hint` carries the fix that actually works.
先读 `code` 与 `hint`，再看 `message`。

Triage order: `pvc check` (is my own install/config sound?) -> `pvc status` (which
channels and variables exist?) -> the failing command once -> the table below. Never
repeat a blocked anonymous request hoping for a different answer.

## Failure map

| Observed text / symptom | `code` (HTTP) | Real cause | Fix |
|---|---|---|---|
| `needs_browser` + hint with relay steps; Bilibili `player/v2` returns HTTP 200 `code:0` with an **empty** `subtitles` array | `needs_browser` (424) | AI subtitles are served only to a logged-in session; a datacenter request sees zero tracks | Stop probing. Open the canonical URL in the logged-in browser, evaluate `assets/browser-extract.js`, then `pvc push --url … --transcript relay.json` |
| `yt-dlp … ERROR: Sign in to confirm you're not a bot` / watch page answers **HTTP 429** | `needs_browser` (424) | YouTube bot gate on the hosting IP | Browser relay tier. Not cookies, not a proxy swap, not another yt-dlp client string |
| InnerTube/manual API returns **HTTP 400 `Precondition check failed`** | `needs_browser` (424) | Hard-coded public InnerTube keys are no longer accepted | Do not rebuild the request by hand; the in-page `ytInitialPlayerResponse` + `timedtext&fmt=json3` path is the working one |
| `Failed to decrypt with DPAPI` from `yt-dlp --cookies-from-browser chrome` | `needs_auth` (401) | Modern Chrome encrypts its cookie DB per-profile (yt-dlp issue #10927) | Abandon cookie extraction. Use the Muse browser session, or a `PVC_COOKIE_FILE` the user exported themselves and offers explicitly |
| `Login required` / `members-only` / `Premium video` / `playabilityStatus` not `ALLOWED` / `reason: "not_playable"` | `needs_auth` (401) | The content needs an entitled account | Ask the user to sign in with their own account and retry once. Never bypass a paywall, region lock or DRM |
| `code:-352` / `-403` or a slider/CAPTCHA page appears in the browser | `needs_browser` (424) | Platform risk control on that session/IP | Back off for minutes, load the site's home page once, retry once; then tell the user the platform is throttling this session |
| `rate_limited`, HTTP 429 from the service or the model provider, `retry_after_seconds` present | `rate_limited` (429) | Provider ceiling, transient | Honour `retry_after_seconds`; at most two backoff retries, then report partial progress. Do not fan out parallel jobs to "get through" |
| `network` / HTTP 502 / connection reset / cold-start stall after an idle period | `network` (502) | Free tier asleep, or an upstream blip | One retry after ~10 s. If the first call after idle is always slow, that is normal cold start, not breakage |
| `dependency_missing`, HTTP 501: `ffmpeg`, `yt_dlp`, `faster_whisper`, `fastapi` not found | `dependency_missing` (501) | Optional extra or system binary absent in that environment | Agent side: `python3 -m pip install "pickvedio-core[media] @ git+https://github.com/smartlotus/pickvedio-agent@v1.0.0"` (or `[local-asr]` / `[service]`; the extras go before the `@`, and there is no PyPI name to install). Service side: the extra must be in the image build -- some platforms cannot install ffmpeg at all, so use the relay tier |
| `未配置总结服务地址…` / `未配置总结模型名称` / `未配置总结服务的 API Key` | `configuration` (400) | Missing `PVC_LLM_BASE_URL` / `PVC_LLM_MODEL` / `PVC_LLM_API_KEY` | Set the named variable. A provider preset (`PVC_LLM_PROVIDER=deepseek`) supplies base_url+model, so the key is then the only missing piece |
| `未设置 PVC_ASR_PROVIDER：没有字幕的视频将无法转写。` | `configuration` (400) | A caption-less video reached the ASR tier with ASR disabled | Ask the user before enabling `PVC_ASR_PROVIDER` (it costs money), or report that only the browser/ASR-free tiers are available |
| `PVC_ASR_PROVIDER=openai 但缺少 PVC_ASR_BASE_URL、PVC_ASR_MODEL、PVC_ASR_API_KEY` | `configuration` (400) | ASR preset incomplete | Set exactly the variables named; a preset fills base_url+model, the key never |
| `empty_transcript`, HTTP 422, `segments: []` after a seemingly successful fetch | `empty_transcript` (422) | Caption track existed but had no text (or was pure music/no speech) | Re-extract from the browser once; if still empty, report honestly. Do not summarise the description instead |
| `job_not_found`, HTTP 404, for a `job_id` from before the last redeploy | `job_not_found` (404) | Ephemeral filesystem lost the job store | Re-create the job. Attach a volume for durability, and never quote stale job ids to the user |
| `model_error`, HTTP 502: empty completion, truncated JSON, refusal, `{{chunk_summaries}}` visible in output | `model_error` (502) | The provider answered with something unusable; a literal `{{token}}` means a prompt template typo | Lower `PVC_CHUNK_CHARS` or switch model; fix the override file in `PVC_PROMPT_DIR` so the placeholders survive. Already retried ~4 times internally, so don't loop |
| HTTP 401 from `$PVC_SERVICE_URL/v1/*` | inbound auth | `PVC_API_TOKEN` mismatch between agent and service | Compare the two (presence only, via `pvc status`); re-set the variable, restart the service, re-test `/healthz` |
| `ok:false` with a Chinese message about 未识别到有效视频链接 | `error` (500) | Link isn't Bilibili/YouTube, or it's a malformed/truncated paste | Ask for the full link. Other sites need `PVC_ALLOW_UNKNOWN_PLATFORM=1` and the service's yt-dlp generic extractor -- say that it is untested for captions |
| Summary arrived but numbers/names look invented | not an error | Source tier was `platform_auto` / `whisper` | Check `data.source` + `data.confidence` and re-read `data.warnings`; re-extract with the browser tier if a creator track exists; label ASR errors in the reply |
| Result has no timestamps, or is obviously about the video's topic rather than its speech | not an error, a violation | Fell back to `page_text` | Regenerate from a real transcript or deliver nothing. Per SKILL.md rule 7 the page text must be labelled as page text, never presented as a summary |

## Provenance traps

`page_text` and a fabricated summary are the same bug. If `data.source` is not in the
answer, the answer is incomplete. Confidence is not decoration: `up_subtitles` (5)
means the creator's own words; `platform_auto` (3) means a machine misheard names and
numbers; `page_text` (1) means nobody transcribed anything.

A relayed transcript (`sourceKind` from the browser) inherits the tier of the track it
came from: `platform_auto` if it was bilibili AI or YouTube `kind: "asr"` captions,
`human_subtitles` / `up_subtitles` otherwise. Say which.

## Service-side traps

- Deploy "green" but `pvc status --service` fails: the service binds the wrong port.
  It must listen on `0.0.0.0:$PORT`.
- `/healthz` returns HTML instead of JSON: a CDN/login wall sits in front. The routes
  must be reachable without a browser session, or the agent cannot use them.
- Every job dies at `transcribing`: the image has no ffmpeg/yt-dlp. Expected on
  serverless; that is the signal to use the relay tier as the default.
- Reports stop mid-sentence on long videos: raised `PVC_MAX_CHUNKS` /
  `PVC_CHUNK_CHARS` pushed a single completion past the provider's limit. Lower the
  chunk size before raising the timeout.
- Summaries changed tone after a redeploy: `PVC_STYLE` or `PVC_LLM_MODEL` drifted.
  Diff the variables, not the code.
