# Deploy runbook 部署手册

Goal: a public GitHub repo -> a hosted `pvc` HTTP service that Muse can call,
deployed by Muse itself through the Railway connector. 目标：把仓库变成 Muse 可以直接
调用的在线服务，由 Muse 通过 Railway 连接器完成部署。

Everything below is what to tell Muse and what to check afterwards. The service
code, `Dockerfile`/`Procfile`/`railway.json` and the README are owned by the main
session; this runbook only drives them.

## 1. Route 2 (Railway hosting) — the instruction to paste into Muse

```text
用你的 Railway 连接器把这个公开仓库部署成一个 HTTP 服务，仓库里有 FastAPI 应用和
`pvc` 命令行：https://github.com/smartlotus/pickvedio-agent

要求：
1. 用 Python 3.11+ 运行时，安装带 service extra 的包（`pip install -e '.[service]'`），
   启动命令用 README 里给出的 uvicorn 入口，监听 Railway 注入的 $PORT，绑定 0.0.0.0。
2. 设置这些环境变量（值我会自己给你，你不要替我编造，也不要把它们打印出来）：
   PVC_API_TOKEN、PVC_LLM_PROVIDER、PVC_LLM_BASE_URL、PVC_LLM_API_KEY、PVC_LLM_MODEL、
   PVC_CHUNK_CHARS=9000、PVC_MAX_MINUTES=180、PVC_STYLE=general、PVC_KEEP_MEDIA=0。
   PVC_ASR_PROVIDER 先留空，等我确认要花钱再开。
3. 部署完成后把服务域名告诉我，只给域名，不要带任何密钥。
4. 然后依次验证并把每一步的 JSON 原样贴给我：
   curl -fsS "https://<域名>/healthz"
   curl -fsS "https://<域名>/openapi.json" | python3 -c "import json,sys; d=json.load(sys.stdin); print('bearer scheme:', 'BearerAuth' in (d.get('components') or {}).get('securitySchemes', {}))"
   # 这份文档已经声明 BearerAuth、给每个 /v1 操作带上 security，并给两个 POST 各带命名例子；
   # 照着它生成连接器即可。若生成出来的客户端一律 401，是没带 Authorization: Bearer 头
   pvc status
   pvc status --service "https://<域名>"
   pvc check
5. 任何一步失败：先贴出命令、错误码 code 和 message，再说你的判断；不要连续重试超过两次。
```

English equivalent, for a non-Chinese Muse session:

```text
Deploy this public repo as an HTTP service with your Railway connector:
https://github.com/smartlotus/pickvedio-agent
Use Python 3.11+, install the `service` extra, start the uvicorn entrypoint named in
the README on 0.0.0.0:$PORT. Set PVC_API_TOKEN, PVC_LLM_PROVIDER, PVC_LLM_BASE_URL,
PVC_LLM_API_KEY, PVC_LLM_MODEL, PVC_CHUNK_CHARS=9000, PVC_MAX_MINUTES=180,
PVC_STYLE=general, PVC_KEEP_MEDIA=0 -- I will supply the secret values; never echo
them. Leave PVC_ASR_PROVIDER unset until I approve paid transcription. Then report the
service hostname and the raw output of: curl /healthz, curl /openapi.json, pvc status,
pvc status --service <url>, pvc check. On failure show the command plus code and
message; at most two retries.
```

## 1a. Route 1 (the VM itself) — the instruction to paste into an English Muse session

```text
Do not deploy this to any hosting platform -- install and run it inside your own cloud
computer. The repo is public:
https://github.com/smartlotus/pickvedio-agent

Do these in order, and show me the raw command and the raw output of every step:

1. git clone https://github.com/smartlotus/pickvedio-agent
   cd pickvedio-agent
   bash bootstrap.sh
   (It creates ./.venv, installs the package, runs the offline self-test `pvc check`
    and prints what is still missing. The last line must be "verdict: READY". If it
    is not, paste the output and stop -- do not improvise another install method.)

2. Install the skill: copy the repo's skills/video-summary/ directory to the
   absolute path /opt/hatch/skills/video-summary/ (note the leading slash -- a
   relative one would land inside the repo and load nothing), then register it in
   /home/hatch/config/skills.yaml in the shape the file already uses:
   version: 1 / entries: / video_summary: / status: available
   (the registration key is `video_summary` with an underscore, while the skill
   directory is `video-summary` with a hyphen; copy the convention from the
   entries already in that file). Afterwards show me the raw output of
   `ls /opt/hatch/skills/video-summary/` and
   `grep -A2 video_summary /home/hatch/config/skills.yaml`.
    Then verify the extractor was copied whole -- not optional:
    `sha256sum skills/video-summary/assets/browser-extract.js /opt/hatch/skills/video-summary/assets/browser-extract.js`
    The two hashes must match. If they do not, delete the copied directory, re-copy
    skills/video-summary/, and paste me both hash lines. Measured reason: deleting the
    last 209 bytes of this .js -- which removes the entire trailing
    pickvedioExtract({}).then(...) call, the statement that actually runs the
    extraction and fills window.__pvcRelay -- still leaves `node --check` at rc=0, and
    so does deleting 120 bytes. Such a copy parses, evaluates, and then silently
    extracts nothing: __pvcRelay stays undefined, and I would tell you the video has no
    subtitles when what is broken is the copy. Cutting 20 or 60 bytes does fail the
    check, so it is a clean statement boundary that slips through, not a comment.
    If this machine's
   skill directory is not /opt/hatch/skills, follow whatever the machine actually
   uses and paste `ls /opt/hatch/skills | head` -- do not invent a third path.
   Then read SKILL.md again and follow the ladder it describes for the rest of
   this session.

3. I will send you a Bilibili or YouTube link. You should:
   a. run `.venv/bin/pvc resolve --url "<what I sent>"` to get the canonical link;
   b. run `.venv/bin/pvc fetch --url "<canonical link>"` and try to take captions directly;
   c. if it answers needs_browser or needs_auth: open the video page in your browser,
      execute skills/video-summary/assets/browser-extract.js inside the page, save the
      whole returned object as relay.json, then run
      `.venv/bin/pvc push --url "<canonical link>" --transcript relay.json --style brief`;
      if I have NOT given you a model key (`pvc status` says data.llm.api_key_present is
      false and no service is configured), do not run push -- it queues a job that cannot
      finish and still bills. Run
      `.venv/bin/pvc fetch --url "<canonical link>" --transcript relay.json --out segments.json`
      instead: no network, no model call, and the same sparse-content gate refuses a relay
      whose cues never loaded, before you read it. Then write the Chinese summary yourself
      from the real start/end times in segments, and say plainly in your reply that this
      summary is yours rather than the pipeline's. Timestamps may only be copied from
      segments.
   d. if that push answers ok=false: treat data.hint as the authoritative next step and
      follow it, and paste me code, message and data.hint verbatim. The hint separates
      "this session is not logged in", "this video has no captions at all" and
      "not playable / members-only -- stop, do not work around it".
      Never substitute the video description, title, comments or search results for a
      "summary".

4. When you answer me, the body is data.summary when you went through push/a service
   (the short --style brief version for a phone chat), or your own write-up when you took
   the keyless branch in 3.c. Either way include one provenance line: which caption tier
   it came from (source), confidence, segment count, duration. If a model ran, name it;
   if none did, say "summary written by the agent, no model call" -- do not present a
   pipeline field as if it had a value.
   Read out data.warnings if it is non-empty.
   For a long video, run
   `.venv/bin/pvc push --url "<canonical link>" --transcript relay.json --estimate-only`
   first and tell me the chunk count and the minimum number of model calls; only
   summarise after I agree. Then use `pvc job create` and `pvc job wait` instead of
   sitting on a request that will time out.

5. On any failure: show the command, code, message and data.hint before you give me
   your own judgement, and never retry the same error more than twice in a row.

I will give you the model key myself (PVC_LLM_PROVIDER and PVC_LLM_API_KEY). Until I do,
the keyless branch in step 3.c is the default route: obtain the transcript, write the
summary yourself, spend nothing. You must
never print its value in any reply, and never write it into a repo file, a log or a commit.
```

## 2. Before pasting, prepare the two secrets

```bash
# inbound token the CLI/agent must present
python3 -c "import secrets; print(secrets.token_hex(24))"
```

- `PVC_API_TOKEN` guards every `/v1/*` route (`Authorization: Bearer …`). Paste the
  generated value into Railway's Variables, and keep a second copy for the agent
  side as `PVC_SERVICE_URL` + `PVC_API_TOKEN`.
- `PVC_LLM_API_KEY` is the user's own provider key. deepseek/openai/moonshot/zhipu/
  qwen/siliconflow/localai are presets in `src/pickvedio_core/config.py`; a preset
  alone gives base_url and model, so `PVC_LLM_PROVIDER=deepseek` + the key is the
  minimum working pair. `localai`/Ollama-style endpoints are the only case where an
  API key is not required.

Never put either value in the chat transcript if it can be avoided: paste them into
the hosting dashboard yourself, and tell Muse only which variable names are set.

## 3. Verify from the CLI instead of by eye

```bash
export PVC_SERVICE_URL="https://<hostname>"
export PVC_API_TOKEN="<same value as the service>"

pvc status                      # local channels: which env vars are set, what is missing
pvc status --service "$PVC_SERVICE_URL"   # reachability + the service's own view
pvc check                       # self-test on the bundled fixture, no network at all
pvc resolve --url "https://www.bilibili.com/video/BV1rjKn6CETw/"
curl -fsS "$PVC_SERVICE_URL/openapi.json" >/dev/null && echo routes-ok
```

Then one end-to-end smoke test that costs a fraction of a real job -- a short
fixture transcript through the relay path:

```bash
printf '{"segments":[{"start":0,"end":6,"text":"这是一条用于连通性验证的短句。"}]}' > smoke.json
pvc push --url "https://www.bilibili.com/video/BV1rjKn6CETw/" \
  --transcript smoke.json --title "smoke" --duration 6 --style brief
```

A `ok:true` envelope with a `data.job_id` (and eventually `data.summary`) means the
service, the queue and the LLM wiring are all live. `code:"configuration"` at this
point is a missing `PVC_LLM_*`, not a platform problem.

## 4. Re-deploy after a push

Railway rebuilds the connected repo on push when auto-deploy is on; otherwise ask
Muse: `重新部署 video-summary 服务，部署完把 /healthz 的返回贴给我`.
Then re-run section 3. Treat the deploy as done only when `/healthz`, `pvc status
--service` and the smoke `pvc push` all pass -- a green build is not a working
service.

Env-var changes need a redeploy/restart to take effect; a stale `PVC_LLM_MODEL`
after a "successful" redeploy is the usual cause of summaries that changed style
without anyone asking for it.

## 5. Hosting constraints that actually bite

Honest list, because each one changes how the agent must behave:

- **Free tiers sleep.** A cold container turns the first request into a 10-60 s
  stall. Expect it, do not report it as a failure, and never design the flow around
  a synchronous request being fast.
- **Request timeouts.** Platform gateways cut long requests (roughly 15-60 s on
  free/serverless tiers, and shorter on Vercel-style functions). This is exactly
  why the API is job-based: `POST /v1/jobs` returns a `job_id` immediately, and the
  agent polls with `pvc job wait`. A multi-hour video cannot be summarised inside
  one HTTP call, and no timeout increase fixes that.
- **No ffmpeg / yt-dlp on some platforms.** Serverless runtimes have no apt, no
  system ffmpeg, read-only or tiny filesystems, and a stripped PATH, so audio
  download and local ASR simply do not exist there. `code:"dependency_missing"` from
  a hosted `pvc fetch` is expected on those platforms; only the extras installed at
  build time are available.
- **Ephemeral filesystem.** `PVC_WORKDIR` does not survive a redeploy or a scale-in
  on most free tiers: job ids, transcripts and reports can vanish. Attach a volume
  if the plan includes one, and otherwise re-run rather than quote an old `job_id`.
- **Datacenter IPs are blocked at the source.** This is the reason the browser relay
  is the default transcript channel, not an emergency hatch: Bilibili's subtitle
  list comes back empty for an anonymous request, and YouTube's watch page answers
  HTTP 429 with "Sign in to confirm you're not a bot". Static InnerTube keys now
  return 400 "Precondition check failed". Deploying harder cannot outvote any of
  these; the logged-in browser can.
- **Cookie extraction is not a deployment option.** `yt-dlp --cookies-from-browser
  chrome` fails against modern Chrome ("Failed to decrypt with DPAPI", yt-dlp
  issue #10927), and shipping the user's cookies to a public host is a worse idea
  than the relay path that avoids it.
- **Costs are visible.** Set `PVC_CHUNK_CHARS`, `PVC_MAX_MINUTES` and `PVC_MAX_CHUNKS`
  deliberately; report `data.usage` with every summary so a runaway job cannot hide.

## 6. Agent-side configuration (the Muse VM, not the service)

```bash
# pickvedio-core is not on PyPI; install it from this repository.
python3 -m pip install "pickvedio-core @ git+https://github.com/smartlotus/pickvedio-agent@v1.0.19"
# or, from a clone: bash bootstrap.sh   (creates ./.venv and self-tests it)
# or, with no git on the machine: the wheel attached to the tag (anonymous GET)
python3 -m pip install "pickvedio-core[service] @ https://github.com/smartlotus/pickvedio-agent/releases/download/v1.0.19/pickvedio_core-1.0.19-py3-none-any.whl"
cat > ~/.pvc.env <<'ENV'
export PVC_SERVICE_URL="https://<hostname>"
export PVC_API_TOKEN="<inbound token>"
ENV
chmod 600 ~/.pvc.env
```

Source it in the shell that runs `pvc`, or pass `--service`/`--token` per call. The
agent needs no LLM key at all when it always uses `--service`: the key stays on the
host, which is the whole point of that split.
