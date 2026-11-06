# Muse 部署设置 Deploying with Meta's Muse (phone workflow)

Two working routes. Both are honest about what they cost and what key they
need. Pick one; you do not need both.

| | Route A: hosted service (Railway) | Route B: Muse's own VM, no hosting |
| --- | --- | --- |
| needs a public URL | yes | **no** |
| needs an LLM API key | **yes** (`PVC_LLM_API_KEY`), unless the summariser is a local endpoint | **yes** (`PVC_LLM_API_KEY` in the VM), unless `PVC_OLLAMA_MODEL`/`localai` points at a model on that machine |
| needs an inbound token | yes, self-generated `PVC_API_TOKEN` (free) | no |
| needs an ASR/paid transcription key | **no** by default (browser relay) | **no** by default |
| survives VM rebuild | yes | no |
| how Muse does it | Railway connector ("Deploy and host what you build, including a GitHub repo, from chat") | the VM terminal |
| good for | a phone-only habit, other devices, jobs that outlive a chat session | trying it once, zero hosting cost, no exposure to the internet |

Either way, the transcript normally comes from **Muse's logged-in browser**, not
from the server: YouTube answers datacenter IPs with "Sign in to confirm you're
not a bot" and Bilibili serves AI subtitles only to a logged-in session. So no
route needs ffmpeg, yt-dlp or an ASR provider to get a summary. ASR is the
opt-in paid path (`PVC_ASR_PROVIDER`) and it costs money -- say so before
enabling it.

---

## 0. Prepare the secrets yourself (2 minutes)

Do this before contacting Muse, and do not paste the values into chat if you can
avoid it: Muse only needs to know *which variable names are set*.

```bash
python3 -c "import secrets; print(secrets.token_hex(24))"   # -> PVC_API_TOKEN
```

- `PVC_API_TOKEN` - guards `/v1/*` on your service. Any random hex works.
- `PVC_LLM_API_KEY` - your own deepseek/openai/moonshot/zhipu/qwen/siliconflow
  key. `PVC_LLM_PROVIDER=deepseek` + that one key is the minimum working pair
  (the preset supplies base_url and model). A local endpoint (`localai`,
  Ollama) is the only case that needs no key.

Full variable list with defaults: `deploy/env.example`.

---

## 1. Route A - deploy the repo through the Railway connector

### (a) First message to Muse

```text
用你的 Railway 连接器把这个公开仓库部署成一个 HTTP 服务，仓库里有 FastAPI 应用和
pvc 命令行：https://github.com/smartlotus/pickvedio-agent

要求：
1. 用仓库里已有的 Dockerfile 构建（railway.json 已经写好，不要自己另写启动命令）。
   默认不需要 ffmpeg：字幕由你的浏览器取，服务器只做分段和总结。
2. 设置这些环境变量（值我自己给，你不要替我编造，也不要打印出来）：
   PVC_API_TOKEN、PVC_LLM_PROVIDER、PVC_LLM_API_KEY、PVC_LLM_MODEL、
   PVC_CHUNK_CHARS=18000、PVC_MAX_MINUTES=180、PVC_STYLE=general、
   PVC_KEEP_MEDIA=0、PVC_WORKDIR=/data。
   PVC_ASR_PROVIDER 先留空，等我确认要花钱再开。
3. 给服务挂一个 Volume，挂载路径 /data（不挂卷的话重新部署会丢掉任务状态）。
4. 部署完成后把服务域名告诉我，只给域名，不要带任何密钥。
5. 然后依次验证，每一步的原始输出都贴给我：
   curl -fsS "https://<域名>/healthz"
   curl -fsS "https://<域名>/openapi.json" | head -c 400
   pvc status
   pvc status --service "https://<域名>"
   pvc check
6. 任何一步失败：先贴命令、错误码 code 和 message，再说你的判断；不要连续重试超过两次。
```

If your Muse session runs in English, use the English block in
`../skills/video-summary/references/deploy-runbook.md` §1 instead -- same
content, and the Railway connector accepts either.

### (b) Variables

Send the values through the Railway dashboard (Variables tab, mark the secret
ones private) rather than in chat. Names and expected values:
`deploy/env.example`. If Muse offers to write them for you, let it write only
the non-secret ones; type the two secrets into the dashboard yourself.

### (c) Verify

```bash
curl -fsS "https://<域名>/healthz"                      # -> 200, no token needed
curl -fsS "https://<域名>/v1/profiles" -H "Authorization: Bearer $PVC_API_TOKEN"
pip install --user pickvedio-core                        # or: pip install -e . from a clone
export PVC_SERVICE_URL="https://<域名>"
export PVC_API_TOKEN="<the same token>"
pvc status
pvc status --service "$PVC_SERVICE_URL"
pvc check                                                # offline self-test, no network
```

Then one cheap end-to-end relay smoke test (6 seconds of made-up transcript):

```bash
printf '{"segments":[{"start":0,"end":6,"text":"这是一条用于连通性验证的短句。"}]}' > smoke.json
pvc push --url "https://www.bilibili.com/video/BV1rjKn6CETw/" \
  --transcript smoke.json --title "smoke" --duration 6 --style brief
```

`ok:true` plus a `data.job_id` means the service is live. `code:"configuration"`
at this point is a missing `PVC_LLM_*`, not a platform problem;
`code:"dependency_missing"` means the image has no ffmpeg/yt-dlp, which is the
expected default and not a bug.

### (d) Then: just send links

From your phone, in the same Muse chat:

```text
总结这个视频：https://www.bilibili.com/video/BV1rjKn6CETw/
```

What happens: Muse resolves the link, opens it in its logged-in browser, runs
`skills/video-summary/assets/browser-extract.js`, POSTs the captions to your
service, creates a job, polls it, and replies with the Chinese summary plus a
provenance line. For a long video the reply arrives in stages; ask "这个任务
到哪一步了" and Muse runs `pvc job wait`. See
`../docs/skill-usage.md` for the human side and
`../skills/video-summary/SKILL.md` for the agent contract.

Redeploy after a push: Railway auto-deploys a connected repo when auto-deploy is
on; otherwise `重新部署 video-summary 服务，部署完把 /healthz 的返回贴给我`.
Env-var changes need a restart to take effect.

---

## 2. Route B - fallback: no hosting at all, `pvc` inside Muse's VM

Muse's VM has a terminal, so the repo can run there directly with no public URL,
no domain, no Railway. This is a legitimate second route, not a degraded one:
the same code, the same CLI, the same relay path.

```text
不要部署到任何云平台。在你的虚拟机里直接跑：

git clone https://github.com/smartlotus/pickvedio-agent && cd pickvedio-agent
python3 -m venv .venv && . .venv/bin/activate
pip install -e '.[dev]'          # service extra 只在你要起 HTTP 服务时才需要
pvc check
pvc status

然后我发链接你就用本地命令处理：
  pvc resolve --url "<链接>"
  pvc fetch --url "<规范链接>"     # 拿不到字幕就按 skill 的规则用浏览器取字幕再 pvc push 到本地
  pvc summarize --url "<规范链接>" --style brief
```

Honest constraints of Route B:

- **An LLM key is still required.** `pickvedio_core` calls `PVC_LLM_*` over
  HTTP; it does not silently use the model Muse itself is chatting with. So
  either `export PVC_LLM_PROVIDER=deepseek` + `export PVC_LLM_API_KEY=...` in
  the VM, or point `PVC_LLM_BASE_URL` at a local Ollama/`localai` endpoint
  running in that VM (that is the only key-free variant).
- `PVC_API_TOKEN` is not needed -- nothing listens on a public port. If you do
  start the service locally (`uvicorn service.app:app --port 8080`) keep it on
  `127.0.0.1` and still set a token, so a shared VM cannot be called by
  someone else.
- Job state is just files under `PVC_WORKDIR` (default `./pickvedio-work`). A VM
  rebuild deletes them, and with them any `job_id` you quoted. Re-run instead of
  trusting an old id.
- The VM has no logged-in Bilibili/YouTube session by itself, so captions still
  come from Muse's browser relay; `pvc fetch` from a datacenter IP will keep
  returning `needs_browser`, which is expected, not a failure.
- No `pip install .[local-asr]` / no ffmpeg unless you ask Muse to install them;
  the ASR path is then simply unavailable.

Both routes: never paste `PVC_LLM_API_KEY`, `PVC_API_TOKEN` or a cookie into a
group chat or a log, and never let the agent echo a value -- `pvc status` shows
names and booleans only.
