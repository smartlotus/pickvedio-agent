# Skill 用法 Skill usage

给"人"看的一页：怎么把这个 skill 装进 Muse、怎么在手机上说话、每次会拿到什么。
Everything here is the human side; the agent-facing contract lives in
`skills/video-summary/SKILL.md` and its `references/`.

装好之后你得到的东西：手机上发一条 B 站或 YouTube 链接，Muse 用它的云浏览器取到字幕，
交给你自己部署的 pickvedio 服务做分层总结，回一段中文速览（或者一份 Markdown 文档）。
After this you send one link from your phone and get back a Chinese layered summary.

## 1. 部署服务 Deploy the service first

The summarising happens on a service you own. Paste the block in
`skills/video-summary/references/deploy-runbook.md` §1 into Muse; it deploys
`https://github.com/smartlotus/pickvedio-agent` through the Railway connector, sets
the variables, and verifies with `/healthz`, `pvc status --service` and a smoke job.
You supply the two secrets (`PVC_API_TOKEN`, `PVC_LLM_API_KEY`); the agent should
never see them printed.

没有服务也能跑：`pvc summarize` 会在 Muse 的云 VM 上本地跑，只是每次要装依赖、冷启动慢、
且仍然拿不到 B 站登录态字幕——所以推荐还是先部署。 A service is not strictly required,
but without one every run pays a cold-start and installs dependencies again.

## 2. 安装 skill Install the skill into Muse

Muse skills are directories at `opt/hatch/skills/<name>/` with a `SKILL.md`, an
optional `manifest.yaml`, `references/`, `assets/` and `eval/`, and every skill is
listed in the registry `config/skills.yaml`. Copy this directory in and add one
registry entry:

```bash
# on the Muse VM
mkdir -p /opt/hatch/skills
cp -r skills/video-summary /opt/hatch/skills/
# registry entry
video-summary: {status: available}
```

诚实说明：以上是 Muse 自带 skill 的确切磁盘结构与命名约定（对照 `calendly` 的
`SKILL.md` / `manifest.yaml` 写的）。用户自备的 skill 能否被官方加载、以及在 VM 重建后是否
保留，我没有验证过——如果加载不了，仍然可用的路径是：把 §1 的服务部署好，然后在对话里直接让
Muse 克隆仓库、`bash bootstrap.sh`（它建 `.venv`、装 `.[dev]` 并离线自检；这个名字没有发到
PyPI，所以不要 `pip install pickvedio-core`），并让它读
`skills/video-summary/SKILL.md` 当作业指导书。这条退路只依赖终端和 pip，不依赖任何 skill
加载机制。 The documented fallback needs no skill loader at all: a terminal, `pip`, and the
`SKILL.md` read as instructions.

## 3. 手机上怎么说 How to phrase the phone message

一句话说清三件事：**链接、要多细、要不要文件**。 One link, one depth, one format.

- 只发链接也行：`总结这个 <url>` → 默认 `brief`，聊天里直接可读。
- 想细一点就点名档位：`用访谈的方式总结` / `整理成能照着做的教程笔记` / `全景研究笔记`
  （对应 `interview` / `tutorial` / `general`）。
- 想要文件就说：`要 Markdown 文件` / `发我文档`，长视频默认就该给文件。
- 多条链接一起发会被识别出来，Muse 会问你处理哪一条（或者按顺序都做并说明）。
- 分享文案（标题 + BV 号 + `b23.tv` 短链）可以直接整段粘，不用先清洗。
- 需要花钱的语音转写、或者需要登录态的内容，Muse 会先来问你；这是规则，不是 bug。

## 4. 五个例子 Five examples and what comes back

1. `总结这个 https://www.bilibili.com/video/BV1rjKn6CETw/`
   → 350 字内的中文速览：一句话结论 + 3-6 条要点 + 值得注意；末尾一行来源标注
   （`up_subtitles`/`platform_auto` + confidence + 时长 + 字幕条数）。走的是
   `resolve → fetch(needs_browser) → 浏览器提取 → push → job wait`。
2. `这个访谈做成纪要，谁说了什么都要保留 <url>`
   → `interview` 档位的完整中文纪要（总览/对话时间线/问答/分歧）；长视频时以
   Markdown 文件返回，附 `data.usage` 的 token 统计。
3. `要一份能照着敲的教程笔记 <youtube url>`
   → `tutorial` 档位：前置条件、按执行顺序重排的步骤、排错清单、复盘检查表；YouTube
   429 拦不住这条路，因为字幕从登录浏览器里读。
4. `这个视频没有字幕，试试别的办法`
   → Muse 报告 `no_subtitles_for_session`/`no_captions`，然后给你三个选择：登录浏览器
   重试、开付费 ASR（要你先同意）、回本地桌面版跑 faster-whisper。它**不会**给你一份
   从简介编出来的总结。
5. `重新部署一下服务，然后把上次那个视频再跑一遍`
   → 走 `references/deploy-runbook.md` §4，贴出 `/healthz`、`pvc status --service` 和
   冒烟 `pvc push` 的原始 JSON，全部通过才说部署完成。旧的 job id 在重建后可能失效，
   那时它会重跑而不是解释。

## 5. 它不会做什么 What it will not do

不绕付费墙、不碰 DRM、不登录别人的账号、不把你 Chrome 里的 Cookie 抽出来（现代 Chrome
上这条路本身就会 "Failed to decrypt with DPAPI" 失败）、不打印任何密钥。
No paywall or DRM bypass, no other account's session, no cookie harvesting from your
local browser, no secret printed anywhere.

## 6. 出问题先看哪里 When it misbehaves

- 先看 `skills/video-summary/references/troubleshooting.md` 的表：`code` 决定分支，
  `hint` 里就是修法。
- `needs_browser` 不是故障，是设计：说明该走浏览器中继。
- 结果不像视频里说的：查 `data.source`；`page_text` 意味着根本没人转写过语音。
- 想改档位模板或分块大小：`pvc profiles`、`PVC_CHUNK_CHARS`、`PVC_MAX_MINUTES`、
  `PVC_PROMPT_DIR`。
- 回归测试（不联网、不花钱）：`pvc check`；`eval/scenarios.yaml` 里的负例是这套流程
  最该被反复验证的部分。
