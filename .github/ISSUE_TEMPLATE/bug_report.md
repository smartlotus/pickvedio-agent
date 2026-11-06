---
name: Bug report
about: Something does not resolve, transcribe or summarise correctly
title: "[bug] "
labels: bug
assignees: smartlotus
---

<!-- Never paste a secret here: PVC_API_TOKEN, PVC_LLM_API_KEY, PVC_ASR_API_KEY,
     PVC_BILIBILI_SESSDATA or cookie contents. Mask them first. -->

## What happened

One or two sentences.

## Reproduce

```text
link (or video id) and share text:
exact command run:
```

## Actual vs expected

- Actual: `{"ok": false, "code": "...", "message": "...", "hint": "..."}` (paste the envelope)
- Expected:

## Which transcript channel was used

`pvc fetch` output `data.source` (and `data.confidence`) if you have it:
`up_subtitles` / `human_subtitles` / `relayed` / `platform_auto` / `whisper` /
`page_text` / unknown.

## Environment

- version (`pvc --version` or `pip show pickvedio-core`):
- where it ran (local Windows / Muse VM / Railway / Render / Fly / docker compose):
- `PVC_LLM_PROVIDER` name only (never the key):
- `PVC_ASR_PROVIDER` set or empty:
- ffmpeg present (yes/no/unknown):

## Extra context

Logs, `pvc status` output, `pvc check` result, screenshots. Anything you already
tried and whether it changed the `code`.
