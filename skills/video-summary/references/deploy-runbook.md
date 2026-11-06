# Deploy runbook 部署手册

Goal: a public GitHub repo -> a hosted `pvc` HTTP service that Muse can call,
deployed by Muse itself through the Railway connector. 目标：把仓库变成 Muse 可以直接
调用的在线服务，由 Muse 通过 Railway 连接器完成部署。

Everything below is what to tell Muse and what to check afterwards. The service
code, `Dockerfile`/`Procfile`/`railway.json` and the README are owned by the main
session; this runbook only drives them.

## 1. The instruction to paste into Muse

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
   curl -fsS "https://<域名>/openapi.json" | head -c 400
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
python3 -m pip install "pickvedio-core @ git+https://github.com/smartlotus/pickvedio-agent@v1.0.1"
# or, from a clone: bash bootstrap.sh   (creates ./.venv and self-tests it)
# or, with no git on the machine: the wheel attached to the tag (anonymous GET)
python3 -m pip install "pickvedio-core[service] @ https://github.com/smartlotus/pickvedio-agent/releases/download/v1.0.1/pickvedio_core-1.0.1-py3-none-any.whl"
cat > ~/.pvc.env <<'ENV'
export PVC_SERVICE_URL="https://<hostname>"
export PVC_API_TOKEN="<inbound token>"
ENV
chmod 600 ~/.pvc.env
```

Source it in the shell that runs `pvc`, or pass `--service`/`--token` per call. The
agent needs no LLM key at all when it always uses `--service`: the key stays on the
host, which is the whole point of that split.
