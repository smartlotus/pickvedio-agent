# Muse 部署设置 / Deploying with Meta's Muse (phone workflow)

Muse 的每台云电脑本身就带终端、文件系统和一个已登录浏览器，所以**默认路线是"在你的云电脑里本地跑"**：
不需要公网 URL、不需要注册 Railway、不需要入站令牌，而且取字幕的浏览器和干活的 CLI 在同一台机器上。
托管（Railway）是第二步的升级项，只有当你想跨设备共享、或想让任务在会话结束后仍可查询时才需要。

Muse gives every user a cloud VM with a terminal, a filesystem and a logged-in browser.
So the **default route is "run it locally inside that VM"**: no public URL, no Railway
signup, no inbound token, and the browser that reads captions sits on the same machine as
the CLI that summarises. Hosting is the upgrade, worth taking only for cross-device use or
jobs that must outlive the chat.

| | 路线 1：云电脑本地跑（先做这个） | 路线 2：Railway 托管 |
| --- | --- | --- |
| 需要公网 URL | **不需要** | 需要 |
| 需要注册别的平台 | 不需要 | 需要 Railway（连接器原话："Deploy and host what you build, including a GitHub repo, from chat"） |
| 需要模型 API key | **需要**（除非那台机器上有本地模型服务） | 需要 |
| 需要入站令牌 `PVC_API_TOKEN` | 不需要 | 需要（不设服务就故意 503 拒绝 `/v1`） |
| 需要付费 ASR key | 不需要（默认走浏览器回传） | 不需要（同上） |
| 谁来部署 | Muse 在 VM 终端里 `bash bootstrap.sh` | Muse 的 Railway 连接器 |
| 重建后任务状态 | 丢失（就是 `PVC_WORKDIR` 下的文件） | 挂卷则保留 |
| 适合 | 手机端"发链接→拿文稿"的主用途，零成本试通 | 多设备、长期跑、给别人用 |

两条路线的字幕都主要来自 **Muse 自己的浏览器**。实测：B 站对匿名会话一条字幕轨道都不下发
（6/6 个视频为空，而且**页面自己的 `player/v2` 请求同样拿到空列表**——这条对照排除了"是我请求
写法错了"）；YouTube 观看页 429、yt-dlp 报机器人校验。所以两边都不需要 ffmpeg、yt-dlp 或 ASR
就能出总结。ASR 是花钱的可选通道（`PVC_ASR_PROVIDER`），开之前必须明确同意。
证据见 [../docs/verification.md](../docs/verification.md)。

英文界面的 Muse 会话：路线 1（云电脑本地跑）与路线 2（Railway 托管）的英文块都在 [../skills/video-summary/references/deploy-runbook.md](../skills/video-summary/references/deploy-runbook.md) 的 §1a 与 §1，与本文件内容一致。

---

## 路线 1（推荐第一步）：让 Muse 在云电脑里本地部署

### 1.1 先自己准备

只需要一个模型 key（DeepSeek / OpenAI / Moonshot / 智谱 / 通义 / SiliconFlow 任一）。
`PVC_LLM_PROVIDER` 预设自带 base_url 与 model，所以最小可用组合就是两个变量；本地端点
（`localai` / Ollama）是唯一免 key 的形态。

> Key 怎么给：不要让 Muse 替你编一个，也不要发到群聊里。最稳妥是你自己在云电脑终端
> `export`，或让它写进那台机器上的环境文件；任何情况下都不要允许它在回复里打印 key。

### 1.2 复制这一段发给 Muse

```text
不要部署到任何云平台，就在你自己的云电脑里把它装好并跑通。仓库是公开的：
https://github.com/smartlotus/pickvedio-agent

请按顺序做，每一步都把原始命令和原始输出贴给我：

1. git clone https://github.com/smartlotus/pickvedio-agent
   cd pickvedio-agent
   bash bootstrap.sh
   （这个脚本会建 venv、装依赖、跑离线自检 pvc check、并报告还缺哪些配置。
     必须看到最后一行 "verdict: READY"。没看到就把输出贴给我并停下，不要自己另想办法装。）

2. 把技能装进你的技能目录：把仓库里的 skills/video-summary/ 整个拷到
   /opt/hatch/skills/video-summary/（注意开头的斜杠，这是绝对路径），
   再在 /home/hatch/config/skills.yaml 里登记，形状照它已有的条目：
   version: 1
   entries:
     video_summary:
       status: available
   （登记键用下划线 video_summary，和技能目录名 video-summary 不同——照本机其它条目学。）
   装完先自查并把结果贴给我：ls /opt/hatch/skills/video-summary/、
   grep -A2 video_summary /home/hatch/config/skills.yaml。
   如果你这台机的技能目录不是 /opt/hatch/skills，就照本机已有技能的实际写法来，
   并把 `ls /opt/hatch/skills | head` 的输出贴给我，不要猜第三个路径。
   然后重读一遍 SKILL.md，之后都按它写的判定阶梯办事。

3. 我会给你一个 B 站或 YouTube 链接。你要：
   a. 先 .venv/bin/pvc resolve --url "<我发的原文>" 拿到规范链接；
   b. 再 .venv/bin/pvc fetch --url "<规范链接>" 试着直接取字幕；
   c. 如果返回 code 是 needs_browser 或 needs_auth：打开那个视频页，在页面里执行
      skills/video-summary/assets/browser-extract.js，把它返回的整个对象存成 relay.json，
      然后执行 .venv/bin/pvc push --url "<规范链接>" --transcript relay.json --style brief；
   d. 如果 push 回来 ok=false：把 data.hint 当成下一步的权威指令照做，并把 code、
      message、data.hint 原样贴给我。hint 会区分"当前会话没登录"、"这视频根本没字幕"、
      "不可播/会员专享，停下别绕"这三种完全不同的去向。
      绝对不许改用视频简介、标题、评论或搜索结果来凑一份"总结"。

4. 输出给我时正文就是 data.summary（手机对话用 --style brief 的短版），
   并且必须带一行出处：字幕来自哪一层（source）、可信度、段数、用了哪个模型；
   data.warnings 非空时把它们也念出来。长视频先用
   `pvc push --url "<规范链接>" --transcript relay.json --estimate-only`
   把切块数与"至少几次模型调用"报给我，我点头之后再真正总结；
   执行时用 pvc job create + pvc job wait，不要同步等一个会超时的请求。

5. 任何失败先贴命令、code、message、data.hint，再说你的判断；同一个错误不要连续重试超过两次。

模型 key 我会给你（PVC_LLM_PROVIDER 和 PVC_LLM_API_KEY）。
你不得在任何回复里打印 key 的值，也不得把它写进仓库文件、日志或提交里。
```

### 1.3 之后每天怎么用

手机上直接发：

```text
总结这个视频：https://www.bilibili.com/video/BV1rjKn6CETw/
```

要长文说"按完整研究笔记整理，不要短版"（`general`）；访谈说"按访谈档"；教程说
"按教程档，给我可复现步骤"。长视频可以问"这个任务到哪一步了"，它会去 `pvc job wait`。
更多例子与返回形状见 [../docs/skill-usage.md](../docs/skill-usage.md)，agent 合同在
[../skills/video-summary/SKILL.md](../skills/video-summary/SKILL.md)。

### 1.4 这条路线的诚实限制

- **仍然需要模型 API key。** `pvc` 通过 HTTP 调 `PVC_LLM_*`，不会白用 Muse 正在跟你聊天
  的那个模型。免 key 的唯一形态是把 `PVC_LLM_BASE_URL` 指向那台机器上的本地服务。
- **不需要 `PVC_API_TOKEN`**，因为没有公开端口。如果顺手起了本地 HTTP 服务
  （`uvicorn service.app:app --port 8080`），请只监听 `127.0.0.1` 并且照样设令牌——
  共享 VM 上不能裸奔。
- **任务状态就是 `PVC_WORKDIR`（默认 `./pickvedio-work`）下的文件**，云电脑重建会一起消失，
  旧 `job_id` 别当真，重跑。
- 那台 VM 默认没登录 B 站/YouTube，所以 `pvc fetch` 会一直回 `needs_browser`——这是预期，
  不是故障；真正取稿靠 1.2 的第 3.c 步。
- 别让 Muse 装 `[local-asr]` 或 ffmpeg，除非你明确要跑音频转写；不配 ASR 时那条通道就是不可用。

---

## 路线 2（可选升级）：让 Muse 用 Railway 连接器托管

只有当你需要跨设备、或想让任务在关掉对话后仍可查询时才做。

### 2.1 先备两样

```bash
python3 -c "import secrets; print(secrets.token_hex(24))"   # -> PVC_API_TOKEN
```

- `PVC_API_TOKEN`：`/v1/*` 的入站令牌，任意随机 hex。**不设的话服务故意用 503 拒绝所有
  `/v1` 请求**——一个匿名可写的端点等于任何路人都在花你的模型额度。
- `PVC_LLM_API_KEY`：你自己的模型 key。变量全表与默认值见 [env.example](env.example)。

### 2.2 复制这一段发给 Muse

```text
用你的 Railway 连接器把这个公开仓库部署成一个 HTTP 服务，仓库里有 FastAPI 应用和
pvc 命令行：https://github.com/smartlotus/pickvedio-agent

要求：
1. 用仓库里已有的 Dockerfile 构建（railway.json 已经写好，不要自己另写启动命令）。
   默认不需要 ffmpeg：字幕由你的浏览器取，服务器只做分段和总结。
2. 设置这些环境变量（值我自己给，你不要替我编造，也不要打印出来）：
   PVC_API_TOKEN、PVC_LLM_PROVIDER、PVC_LLM_API_KEY、PVC_LLM_MODEL、
   PVC_CHUNK_CHARS=9000、PVC_MAX_MINUTES=180、PVC_STYLE=general、
   PVC_KEEP_MEDIA=0、PVC_WORKDIR=/data。
   （PVC_CHUNK_CHARS 的代码默认是 18000；这里显式写 9000 是因为部分 provider 会把
     一次 18000 字的补全截断成不可用的 JSON，troubleshooting 里那条 502 就是这个。）
   PVC_ASR_PROVIDER 先留空，等我确认要花钱再开。
3. 给服务挂一个 Volume，挂载路径 /data（不挂卷的话重新部署会丢掉任务状态）。
4. 部署完成后把服务域名告诉我，只给域名，不要带任何密钥。
5. 然后依次验证。先在你自己的云电脑里按路线 1 的方法装上命令行
   （git clone + bash bootstrap.sh，看到 "verdict: READY"），再用它指向刚部署的域名，
   每一步的原始输出都贴给我：
   curl -fsS "https://<域名>/healthz"
   curl -fsS "https://<域名>/openapi.json" | python3 -c "import json,sys; d=json.load(sys.stdin); print('bearer scheme:', 'BearerAuth' in (d.get('components') or {}).get('securitySchemes', {}))"
   # 这个文档已经声明 BearerAuth 与两个 POST 的例子，连接器可以直接照着它生成；
   # 若生成出来的客户端一律 401，八成是没把 Authorization: Bearer 头带上
   .venv/bin/pvc status
   .venv/bin/pvc status --service "https://<域名>" --token "<PVC_API_TOKEN>"
   .venv/bin/pvc check
   （pvc 装在 Railway 服务里是查不了的：那个容器只跑 HTTP 服务。）
6. 任何一步失败：先贴命令、错误码 code 和 message，再说你的判断；不要连续重试超过两次。
```

密钥建议自己在 Railway 控制台 Variables 页填（标成 secret）。如果 Muse 主动要替你写变量，
只允许它写非密钥那几个，两个密钥你自己输。

### 2.3 部署后自检

```bash
curl -fsS "https://<域名>/healthz"                      # -> 200，免令牌
curl -fsS "https://<域名>/v1/profiles" -H "Authorization: Bearer $PVC_API_TOKEN"
python3 -m pip install "pickvedio-core @ git+https://github.com/smartlotus/pickvedio-agent@v1.0.15"
                                                         # 这个名字不在 PyPI 上，必须带 git+ 地址；
                                                         # clone 过的话用 bash bootstrap.sh 更省事
export PVC_SERVICE_URL="https://<域名>"
export PVC_API_TOKEN="<同一个令牌>"
pvc status
pvc status --service "$PVC_SERVICE_URL"
pvc check                                                # 离线自检，不联网、不调模型
```

再来一次廉价端到端（6 秒编造文字，只验连通与计费链路，不是真总结）：

```bash
printf '{"segments":[{"start":0,"end":6,"text":"这是一条用于连通性验证的短句。"}]}' > smoke.json
pvc push --url "https://www.bilibili.com/video/BV1rjKn6CETw/" \
  --transcript smoke.json --title "smoke" --duration 6 --style brief
```

`ok:true` 加 `data.job_id` 说明服务活着。此时 `code:"configuration"` 是缺 `PVC_LLM_*`，
不是平台问题；`code:"dependency_missing"` 说明镜像没带 ffmpeg/yt-dlp——那是默认镜像的正确
状态，不是故障。

### 2.4 之后与重部署

日常用法与路线 1 完全一样（发链接即可）。push 之后：Railway 开了 auto-deploy 会随推送自动
重建；否则说 `重新部署 pickvedio 服务，部署完把 /healthz 的返回贴给我`。
改环境变量需要重启才生效。

---

## 无论走哪条，都守这三条

1. **不要把 key、`PVC_API_TOKEN`、Cookie 值发进聊天或写进日志。** `pvc status` 只报名字与
   布尔值，不报值。
2. **花钱前先问。** ASR 与超长视频的分层总结会真扣钱。手机端（字幕靠浏览器回传）真正能用的
   估算是 `pvc push --url "<规范链接>" --transcript relay.json --estimate-only`：
   它跑与真实总结完全相同的校验，报出切块数与"至少几次"模型调用，一次模型都不调。
   `pvc summarize --estimate-only` 只在字幕通道本来就可达时才有意义——它得先拿到逐字稿，
   而匿名会话里那一步就会回 `needs_browser`。
3. **拿不到字幕就报失败**，不许用简介/标题/评论冒充视频内容。这条既是代码门禁
   （`--allow-page-text` 默认关；回传内容太稀会拿到 `sparse_transcript`，
   空内容会拿到 `empty_transcript`，两者都带 `data.hint` 说清下一步），也是
   [SKILL.md](../skills/video-summary/SKILL.md) 的第 7 条硬规则。
