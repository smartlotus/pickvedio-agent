# Browser relay extraction 浏览器字幕提取

The primary transcript channel. Muse opens the video page in its own logged-in
cloud browser and reads the caption data from inside the page, which is the one
place where Bilibili's login-gated AI subtitles and YouTube's bot-gated
`timedtext` endpoint both answer. 这是主通道：只有从浏览器页面内部同源发起请求，
B 站的登录态 AI 字幕和 YouTube 的 `timedtext` 才会返回数据。

Script: `assets/browser-extract.js` (path inside the skill, i.e.
`/opt/hatch/skills/video-summary/assets/browser-extract.js` on the Muse VM).

## How Muse runs it

1. `pvc resolve --url "<link>"` first, so the page opened is the canonical
   watch URL (`https://www.bilibili.com/video/BVxxxxxxxxx/`,
   `https://www.youtube.com/watch?v=<11 chars>`), not a `b23.tv` redirect or a
   Shorts/embed URL.
2. Open that URL in the browser. Wait until the player state exists --
   `window.__INITIAL_STATE__` / `window.ytInitialPlayerResponse` are present
   roughly when the first frame paints. A page still showing a consent or
   risk-control interstitial yields `reason: "no_player_response"`.
3. Evaluate `assets/browser-extract.js` with `returnByValue: true` and
   `awaitPromise: true`. Its final statement is a Promise resolving to the
   result object, so the evaluation output *is* the JSON; nothing needs to be
   scraped from the console.
4. To pass options (rare), evaluate the file once to install
   `window.pickvedioExtract`, then evaluate
   `pickvedioExtract({targetLangs: ["zh-CN"]})` or
   `pickvedioExtract({translateTo: "zh-CN"})` or
   `pickvedioExtract({maxSeconds: 5400})`.
5. Write the returned object to the VM as `relay.json` before doing anything
   else. It is the only copy: the browser tab can be closed, and re-running the
   extraction costs another request to the platform.

Manual/console equivalent, for a human reproducing a failure:

```js
copy(JSON.stringify(await pickvedioExtract({})));  // also kept in window.__pvcRelay
```

## Result shape

```json
{
  "platform": "bilibili",
  "url": "https://www.bilibili.com/video/BV1rjKn6CETw/",
  "title": "…",
  "durationSeconds": 3121,
  "sourceKind": "up_subtitles",
  "language": "zh-CN",
  "trackLabel": "中文",
  "trackCount": 2,
  "segments": [{ "start": 0.0, "end": 4.36, "text": "…" }],
  "charCount": 18432,
  "warnings": [],
  "reason": "",
  "extractedAt": "2026-11-06T06:41:12.882Z"
}
```

`segments` uses the same `{start, end, text}` field names as the CLI envelope and
`POST /v1/jobs`, so it is passed through without renaming. Extra keys
(`lineCount`, `eventCount`, `trackKind`, `translatedTo`) are diagnostic only.

`sourceKind` is always one of `up_subtitles`, `human_subtitles`,
`platform_auto` -- i.e. only real caption data. The script has no fallback that
reads the description, deliberately.

## What each platform actually does

**Bilibili** (`*.bilibili.com`, `b23.tv`)

1. `bvid`/`aid` from `window.__INITIAL_PROFILE__` / `__INITIAL_STATE__` /
   `__INITIAL__INFO__`, else from `/video/BV…` in the path, else `?bvid=`.
2. `cid` from the same state, else `GET /x/web-interface/view?bvid=…` and
   `data.cid` (or `data.pages[p-1].cid` for a multi-part video, `?p=` honoured).
   This endpoint works anonymously -- verified HTTP 200 / `code: 0`.
3. `GET /x/player/v2?bvid=&cid=` from `https://api.bilibili.com`, falling back
   to the page origin, and to `/x/player/wbi/v2` if `v2` returns non-zero.
   `credentials: "include"` is what makes this differ from a server-side probe.
4. Rank `data.subtitle.subtitles[]`: non-`ai_type` (creator) tracks above
   platform-generated ones, page language above other languages; then fetch the
   winner's `subtitle_url` (protocol-relative `//…` is completed with the page
   protocol) and map `body[{from, to, content}]` to `[{start, end, text}]`.
5. `ai_type` truthy -> `sourceKind: "platform_auto"`, else `up_subtitles`.

**YouTube** (`youtube.com`, `youtu.be`)

1. `window.ytInitialPlayerResponse.captions.playerCaptionsTracklistRenderer.captionTracks`.
2. Prefer a `kind !== "asr"` track whose `languageCode` matches
   `defaultAudioLanguage` / `document.documentElement.lang` / `targetLangs`;
   then any manual track; then the first track at all.
3. Fetch `baseUrl` with `fmt=json3` (an existing `fmt` is replaced, not
   duplicated) from the page origin, with credentials.
4. Flatten `events[]` that have `segs[]`: `tStartMs/1000` -> `start`,
   `dDurationMs/1000` -> duration, `segs[].utf8` joined -> text. Events without
   `segs` (timing-only window events) are dropped.
5. `kind: "asr"` -> `platform_auto`, otherwise `human_subtitles`.
6. `opts.translateTo` appends `&tlang=<lang>` -- only valid for `asr` tracks; on
   a manual track it is ignored and a warning is recorded.

Normalization applied to both: carriage returns stripped, embedded newlines
(real `\n` and the literal two-character `\n`) split into sentences and rejoined,
whitespace collapsed, empty lines dropped, inverted or missing end clocks
reconstructed, overlapping windows clamped to the next start.

## When `segments` is empty

Read `reason`, do not retry blindly. 空结果不是错误，是信息：

| `reason` | Meaning | What to do |
|---|---|---|
| `no_subtitles_for_session` | Bilibili returned an empty track list -- not logged in, or the video genuinely has no subtitles | Check the browser session; if logged in and still empty, the video has no subtitles -> ASR tier or honest failure |
| `subtitle_track_without_body` | Tracks listed but none carries `subtitle_url` | Treat as no subtitles |
| `subtitle_body_forbidden` / `subtitle_body_failed` | Signed subtitle URL rejected or network | Re-open the page fresh and re-evaluate once; then report |
| `no_captions` | YouTube: no `captionTracks` at all | ASR tier or honest failure |
| `captions_translate_only` | Only translation metadata, no track list | Re-evaluate with `translateTo` on the audio-language track if one appears; else ASR |
| `rate_limited` | `timedtext` answered 429 | Back off; wait for the browser to play the video once, retry once |
| `caption_forbidden` | 401/403 on `timedtext` | Session is not entitled to that caption set; report |
| `no_player_response` | Wrong page, or evaluated too early | Re-open canonical watch URL, wait for player state, re-evaluate |
| `not_playable` | `playabilityStatus` is neither `OK` nor `ALLOWED` (region lock, sign-in required, members-only) | Stop; tell the user why. Do not attempt to unlock |
| `no_video_id` / `no_cid` / `view_api_failed` | Page state unreadable | Re-open canonical URL once; then report |
| `subtitle_body_empty` / `caption_body_empty` / `empty_segments` | Track exists but has no text | Treat as no transcript |
| `subtitle_track_without_body` | Track list non-empty, every body fetch returned nothing | Re-fetch once from a freshly opened page; then report and offer ASR (costs money, needs consent) |
| `caption_track_without_baseurl` | YouTube listed a track with no usable base URL | Re-open the canonical watch URL and re-evaluate; the player builds the URL only after playback state exists |
| `extraction_failed` | Fallback: nothing more specific applied | Re-open the canonical URL, wait until playback actually starts, re-evaluate once; then report the warnings verbatim |
| `unsupported_page` | Not bilibili/youtube | `pvc fetch` with yt-dlp's generic extractor, or decline |

An empty `segments` array ends the ladder. The correct reply is the failure plus
the reason and the next tier the user could enable -- never a summary of the
title, the description, or the comments.

## Feeding the result back

Preferred, one command (stdin, no temp file juggling):

```bash
pvc push --url "https://www.bilibili.com/video/BV1rjKn6CETw/" \
  --transcript relay.json --title "<result.title>" \
  --duration <result.durationSeconds> --style brief
```

`relay.json` may also be `-` to read the JSON on stdin, an SRT/VTT/JSON3 file, or
a file containing only a `segments[]` array.

Direct HTTP equivalent (`durationSeconds` -> `duration`):

```bash
python3 - <<'PY' > body.json
import json
r = json.load(open("relay.json", encoding="utf-8"))
json.dump({"url": r["url"], "style": "brief", "title": r.get("title", ""),
           "duration": r.get("durationSeconds"), "segments": r["segments"]},
          open("/dev/stdout", "w", encoding="utf-8"), ensure_ascii=False)
PY
curl -fsS -X POST "$PVC_SERVICE_URL/v1/jobs" \
  -H "Authorization: Bearer $PVC_API_TOKEN" -H 'content-type: application/json' \
  --data @body.json
```

Then poll: `pvc job wait --id <job_id> --timeout 600`.

`POST /v1/transcripts` is the hot path for "transcript in, transcript out" with
no summarisation, and is what to use when the user only wants the逐字稿 saved.

## Limits worth stating

- The relay covers caption text only: no speaker labels, no on-screen text, no
  audio-only passages that were never captioned.
- Very long videos: prefer capping with `maxSeconds` and telling the user which
  portion was summarised over silently truncating on the service side
  (`PVC_MAX_MINUTES` is the service's own ceiling).
- Multi-part Bilibili videos need one page and one `cid` per part; the script
  warns when it picks one part out of several.
- Everything here assumes the user is entitled to the content. No DRM, no
  paywall, no access-control bypass is implemented and none should be added.
