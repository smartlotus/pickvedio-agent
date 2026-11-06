# pickvedio-core

把一个视频链接变成带时间点的逐字稿和分层中文总结。这是 pickvedio 桌面程序的无界面移植版，可以部署成
一个 HTTP 服务，也可以作为一个 agent 技能（skill）安装。
Turn a video link into a timestamped transcript plus a layered Chinese summary.
This is the headless port of the `pickvedio` Windows app: deployable as an HTTP
service, and installable as an agent skill.

**给 agent 的部署说明在 [AGENTS.md](AGENTS.md)。** 一个 GitHub 仓库链接交给支持自动部署的
agent（例如 Meta Muse 的 Railway 连接器），它就能自己完成部署；之后你只要把视频链接发给它。
[Agent-facing deploy instructions are in AGENTS.md.](AGENTS.md) Hand this repo's
URL to an agent that can deploy from GitHub (Meta Muse's Railway connector does
exactly this) and it deploys itself; afterwards you just send it video links.

---

## 它和桌面版的区别 / how it differs from the desktop app

| | pickvedio 桌面版 | pickvedio-core |
| --- | --- | --- |
| 取文字 / how words are obtained | 总是下载音频 + 本地 Whisper | 字幕优先阶梯：回传字幕 → 平台字幕 → 音频转写 → （默认禁用的）页面简介 |
| 界面 | PySide6 窗口 | JSON CLI `pvc` + FastAPI 服务 + agent skill |
| 部署 | 本机 EXE | Dockerfile / Railway / Render / Fly / 本地 |
| 总结模型 | OpenAI 兼容 API 或本机 Ollama | 同左，另加 DeepSeek 等预设 |
| 提示词档位 | 通用 / 访谈 / 教程 / 自定义 | 同左，逐字移植，另加手机用的 `brief` |

四个档位的提示词是从桌面程序里**逐字节搬过来的**，不是重写。桌面版
`~/.pickvedio/settings.json` 里保存的 `profile_prompts` 也会被读取（优先级：内置 <
桌面版存过的 < 仓库 `prompts/` 目录里的文件）。
The four prompt profiles are ported byte-for-byte, not rewritten. Templates the
desktop app stored in `~/.pickvedio/settings.json` are read too, and a repo
`prompts/<profile>_<slot>.md` overrides both.

## 为什么云端服务通常拿不到字幕 / why a hosted service usually cannot fetch captions

实测结论（完整证据见 [docs/verification.md](docs/verification.md)）：

- B 站字幕接口对匿名会话返回 `code: 0` **但轨道列表为空**，抽样 6 个视频全部如此；
- YouTube 观看页直接 **HTTP 429**，`yt-dlp` 报 *"Sign in to confirm you're not a bot"*，
  网上流传的固定 InnerTube key 现在返回 **400 Precondition check failed**；
- 现代 Chrome 的 `--cookies-from-browser` 已经失效（*"Failed to decrypt with DPAPI"*）。

所以这套设计的主通道是：**由已登录的浏览器同源取得字幕，再交给服务做分块与分层总结**。
这不是备用路径，而是默认路径 —— 因为服务端拿不到文字时，唯一诚实的产物就是"拿不到"。
Six sampled videos returned zero subtitle tracks to an anonymous Bilibili
session; YouTube bot-gates the request outright. So the primary channel is:
**read the captions from inside a logged-in browser, then hand them to the
service** for chunking and summarising. That is the default path, not a fallback.

这也是它适合交给 Muse 这类 agent 的原因：它自己就带一台已登录的浏览器。
Which is exactly why it suits an agent platform: the agent brings its own
logged-in browser.

## 快速开始 / quick start

```bash
bash bootstrap.sh                # 建 venv + 装依赖 + 离线自检，最后一行应是 verdict: READY
python3 -m pip install -e '.[dev]'   # 手动等价做法（在仓库目录里）
python3 -m pip install "pickvedio-core @ git+https://github.com/smartlotus/pickvedio-agent@v1.0.10"
                                    # 不在仓库里时的安装法。注意：这个名字没有发到 PyPI，
                                    # `pip install pickvedio-core` 会报 No matching
                                    # distribution found —— 必须带 git+ 地址
python3 -m pip install "pickvedio-core[service] @ https://github.com/smartlotus/pickvedio-agent/releases/download/v1.0.10/pickvedio_core-1.0.10-py3-none-any.whl"
                                    # 机器上没有 git 时的等价做法：直接装 Release 附件，
                                    # 匿名可下载。Release 作业每次发版都按当时的 tag 把
                                    # 这条形状的命令真跑一遍（v1.0.10 已实测通过；1.0.4 时
                                    # 是手工实测）。这台机若连 github.com 都超时，见下面那段

pvc check                        # 离线自检，不联网、不调用模型
pvc status                       # 当前部署能用哪些通道，缺哪些环境变量
pvc resolve "看看这个 https://b23.tv/GUAy8Zm 讲得清楚"

export PVC_LLM_PROVIDER=deepseek
export PVC_LLM_API_KEY=sk-...
pvc summarize "https://www.bilibili.com/video/BV1rjKn6CETw/" --style brief
```

如果那台机器到 `github.com` 的 443 直接被 reset 或超时（不是这个项目的问题，是它自己的
出口问题；本仓库的 `releases/download` 是不需要登录的），有两条不改结论的替代路：
一条是给上面那条 pip 命令挂上该机器本来就配好的代理；另一条是先取字节再装本地文件——
`gh api -H "Accept: application/octet-stream" repos/smartlotus/pickvedio-agent/releases/assets/<id>`
能把同一份 wheel 原样取下来（`<id>` 从 `gh api repos/smartlotus/pickvedio-agent/releases/tags/<tag>`
的 `assets[].id` 里读），同一份 JSON 的 `digest` 字段就是它的 sha256，先核对再装。
两条路装的是同一个文件，`Release` workflow 里还有一道门每次发版都从公网匿名 pip 装一次那个 URL。

云端部署 / deployed:

```bash
export PVC_API_TOKEN=$(openssl rand -hex 16)
uvicorn service.app:app --host 0.0.0.0 --port 8788

curl -fsS localhost:8788/healthz
pvc --service http://localhost:8788 --token "$PVC_API_TOKEN" summarize "<链接>"
```

回传字幕（agent 从浏览器里取到文字后）/ relaying captions the agent extracted:

```bash
pvc push --url "<链接>" --transcript relay.json --style general
# relay.json 可以是 browser-extract.js 的完整输出、segments 数组、SRT、VTT 或 JSON3 原文
```

## 命令 / commands

每个子命令的 stdout 恒为一个 JSON 对象：`{"ok", "code", "message", "data"}`；失败时额外带
`hint`。进度与渲染好的报告走 stderr。`ok: true` 时退出码为 0。
Every subcommand emits exactly one JSON envelope on stdout; failures carry a
`hint`. Progress and rendered reports go to stderr; exit code 0 iff `ok: true`.

```
pvc status [--service URL]      通道与配置自检（不打印任何密钥值）
pvc resolve "任意文本"           识别并归一化链接
pvc fetch URL [--out f.json]    只取逐字稿
pvc push --url U --transcript F 接受浏览器回传的字幕并总结
pvc summarize URL [--style S] [--estimate-only] [--out DIR]
pvc job create|status|wait|report|list|cancel|prune
pvc profiles                    总结档位
pvc models [--ollama]           可用模型
pvc serve [--host --port]       本地起 HTTP 服务
pvc check                       离线自检（含负对照）
```

`--format md|text|chat` 会把渲染结果写到 stderr，方便人读；机器接口永远是 `json`。

## HTTP 接口 / HTTP API

```
GET  /healthz                    免鉴权，用于验证部署是否活着
GET  /openapi.json               机器可读接口（agent 可据此自建连接器）
GET  /v1/profiles                档位列表
GET  /v1/capabilities            当前可用通道与缺失配置
GET  /v1/metadata?url=…          标题/时长/字幕轨道情况
POST /v1/jobs                    {"url": …, "style": …} → 异步任务
GET  /v1/jobs/{id}               状态与进度（不含完整报告）
GET  /v1/jobs/{id}/report        报告
POST /v1/jobs/{id}/cancel        取消
POST /v1/transcripts             回传字幕直接总结（最快、零媒体开销；`"wait": true` 可同步返回）
```

`/v1/*` 需要 `Authorization: Bearer $PVC_API_TOKEN`。**未设置该变量时 `/v1/*` 返回 503**，
因为一个匿名可写的端点等于任何人都在花你的模型额度。
If `PVC_API_TOKEN` is unset, `/v1/*` refuses with 503 rather than letting the
whole internet spend your model quota.

## 输出物 / artefacts

每个任务产出：`summary.md`、`summary.txt`、`summary.json`、`transcript.{json,txt,srt,vtt}`、
`chat.md`（手机对话尺寸）。报告头部会写明文字来自哪一层、可信度如何、用了哪个模型。
Each job writes the full set plus a chat-sized short form. The report header
states which transcript tier produced the words and how much to trust it.

## 作为 Muse 技能安装 / installing as a Muse skill

`skills/video-summary/` 就是一个完整的技能包（`SKILL.md` + `manifest.yaml` + `references/` +
`assets/browser-extract.js` + `eval/scenarios.yaml`），格式对齐 Muse 自带技能的结构。
拷到绝对路径 `/opt/hatch/skills/video-summary/`，再到 `/home/hatch/config/skills.yaml`
的 `entries:` 下加一条 `video_summary: {status: available}` 即可
（登记键是下划线，技能目录是连字符——照本机已有条目的写法学，别猜）。
手机侧的一步步说法见 [deploy/muse-setup.md](deploy/muse-setup.md)；用法例子见
[docs/skill-usage.md](docs/skill-usage.md)。

## 限制（诚实版）/ limitations, honestly

- 没有字幕、又没配置 ASR、也没浏览器回传 ⇒ 拿不到逐字稿，工具会明确失败，不会硬写总结。
- 云端 IP 访问 YouTube 大概率被拦；B 站 AI 字幕需要登录态。
- 音频转写需要 ffmpeg + yt-dlp，免费档的请求时长上限不足以跑完长视频，所以是异步任务。
- 分层总结的质量取决于你接入的模型；`brief` 档在弱模型上会漏细节。
- `docs/verification.md` §4 列了尚未验证的项，其中最重要的是：**浏览器提取脚本还没在
  真实已登录页面里跑过**。
- 请只处理你有权访问与下载的内容，并遵守平台条款。

## 许可 / license

MIT（见 [LICENSE](LICENSE)）。第三方依赖保留各自许可。本仓库不含任何密钥、Cookie、模型权重
或个人数据。
