# 实测证据 / Verification evidence

本文只记录**实际跑出来的结果**，并区分三件事：在本机验证过的、只在桩环境验证过的、以及尚未验证的。
This file records only what was actually run, and separates three things: what is
verified on this machine, what is verified against stubs only, and what is not
verified at all.

采集时间 / Captured: 2026-11-06, a Windows 11 24H2 desktop on residential egress
behind a local proxy chain, Python 3.11, `yt-dlp 2026.07.04`.

原始脚本与输出 / Raw scripts and outputs: three probe scripts
(`probe_subtitle_channels.py`, `probe2_innertube_bili.py`, `probe3_bili_tracks.py`).
They are deliberately not shipped: they hard-code this machine's paths, and the
responses they captured are quoted in full below, which is the part that matters.

---

## 1. Bilibili：匿名会话能看到什么 / what an anonymous session sees

`GET /x/web-interface/view?bvid=…` — **anonymous, no cookie**:

```
HTTP 200, code 0, message OK
title=… duration=4879 cid=41494382554 pages=1
```

So metadata is free and unauthenticated. Verified for 6 videos.

`GET /x/player/v2?bvid=…&cid=…` — anonymous, after a real homepage handshake that
populated `buvid3` and `b_nut`:

```
http=200 code=0 n_subtitles=0 lan=[]
data_keys=[… 'subtitle' present but empty …]
```

Sampled 6 valid videos (durations 202 s – 4879 s), **zero returned any subtitle
track**:

| bvid | duration | tracks |
| --- | --- | --- |
| BV1rjKn6CETw | 4879 | 0 |
| BV1GJ411x7h7 | 213 | 0 |
| BV1xx411c7mD | 2055 | 0 |
| BV1fCxp6aEpH | 1425 | 0 |
| BV1jfxB6qEzD | 202 | 0 |
| BV1y7411Q7Lu | 202 | 0 |

(2 further ids returned `code -404 啥都木有`, i.e. the videos are gone.)

**结论 / Conclusion.** An anonymous session sees no subtitle tracks at all. This
is *not* evidence that the videos lack captions — it is evidence about this
session. **No positive control was obtained**: we could not find a single video
whose captions are reachable anonymously, so this document does not claim
"UP-authored subtitles work without login". The design therefore treats
"0 tracks" as `needs_auth` and points at the logged-in browser, which is the
remedy that works under either explanation.

`GET /x/player/wbi/v2` (the signed variant) behaves identically: `code 0`, empty list.

## 2. YouTube：机器人门禁 / the bot gate

| 尝试 / attempt | 结果 / result |
| --- | --- |
| `GET /watch?v=…` (page HTML scrape) | **HTTP 429** |
| `yt-dlp --list-subs --skip-download` | `ERROR: [youtube] …: Sign in to confirm you're not a bot` |
| InnerTube `youtubei/v1/player` with the publicly documented static key, `WEB` | SSL handshake killed mid-stream |
| same, `ANDROID` / `IOS` | **HTTP 400 `Precondition check failed`** |
| same, `TVHTML5` | **HTTP 404 `Requested entity was not found`** |
| same, `WEB_EMBEDDED_PLAYER` | HTTP 200 but `playabilityStatus=ERROR / "This video is unavailable"` |
| `youtubei/v1/get_transcript` | **HTTP 400 `Precondition check failed`** |
| `GET /oembed?url=…&format=json` | reachable, returns title + author only |

**结论 / Conclusion.** There is no anonymous caption path for YouTube from this
kind of egress, and the widely copy-pasted static-key InnerTube trick is dead.
`sources/youtube.py` therefore does not hand-roll InnerTube; it delegates to
yt-dlp and converts the bot-gate into `needs_browser` with the relay instructions.

A later live run (section 5) additionally hit `_ssl.c:989 The handshake operation
timed out` for every YouTube request. That is this machine's proxy chain, not new
information about YouTube; the point stands that the CLI surfaced it as a typed
failure with a remedy instead of hanging.

## 3. 从本机 Chrome 取 Cookie / cookie extraction on this box

```
python -m yt_dlp --cookies-from-browser chrome --list-subs …
ERROR: Failed to decrypt with DPAPI. See https://github.com/yt-dlp/yt-dlp/issues/10927
```

Identical for `edge`. **结论 / Conclusion.** `--cookies-from-browser` is broken on
modern Chrome (app-bound encryption), so the desktop app's own cookie setting
`cookie_browser` cannot work here either. The hosted service therefore accepts
only `PVC_COOKIE_FILE` (Netscape format, exported by a browser extension) and
`PVC_BILIBILI_SESSDATA`, and the primary channel is the browser relay.

## 4. 离线自检与测试 / offline self-test and the suite

```
$ pvc check
{"ok": true, … "network_used": false, "model_calls": 0,
 "checks": [relay_accepts_good_payload, relay_rejects_sparse_payload,
            parse_srt, parse_vtt_speaker, parse_json3_skips_newline_events,
            chunk_plan, sparse_guard_present]}
```

`relay_rejects_sparse_payload` is a **negative control**: a 1-line payload
claimed as a 2-hour video's captions is refused rather than summarised.

```
$ python -m pytest -q
112 passed
$ ruff check src service tests
All checks passed!
```

Hermeticity, checked rather than assumed:

```
$ HTTP_PROXY=http://127.0.0.1:1 HTTPS_PROXY=http://127.0.0.1:1 ALL_PROXY=... \
  NO_PROXY=127.0.0.1,localhost python -m pytest -q
112 passed        # with all external egress pointed at a dead port
```

So no test depends on Bilibili, YouTube, or a model provider. (This was added
after CI exposed a test that *did* reach Bilibili and failed there with `-412`
while passing locally.)

### Defects CI found that local runs could not

Both were real, and both were invisible on the author's machine:

| 缺陷 / defect | 为什么本机看不出来 / why local passed |
| --- | --- |
| `service/` 在仓库根目录，不属于安装的包 → 安装后 `import service.app` 失败，`uvicorn service.app:app` 与 `pvc serve` 在真实主机上会崩 | 本机 pytest 与 `pvc serve` 的 cwd 就是仓库根，旁边正好有个 `service/` 目录;而 CI 里那句 `python -c "import service.app"` 同样在仓库根运行,所以它绿着发布了一个假的部署承诺 |
| `pvc push --transcript '<内联 JSON>'` 在 Linux 上抛 `OSError(ENAMETOOLONG)` | 代码用 `Path(整段文本).is_file()` 判断"这是不是文件路径";Windows 对超长路径返回 False,Linux 的 `stat()` 直接抛错。长度也刚好只在 CI 的 fixture 规模上越界 |
| `pvc --service URL status`（全局写法）悄悄丢掉 URL，`pvc --format md summarize …` 直接报错 | `--service/--token/--format` 同时声明在根解析器和子命令上时，子命令的默认值会覆盖根已经吃到的值；只声明在子命令上则子命令之前的写法被拒绝。本机手测都用的是"后置"写法，所以没暴露 |
| 没有 `HOME`/`USERPROFILE` 的容器里，服务在 import 阶段就 `RuntimeError: Could not determine home directory` 崩掉 | 可选的桌面版种子配置文件用了裸的 `Path.home()`。本机的 `Settings.load()` 永远能找到家目录，所以这条路径只在真实启动服务时才暴露——它是被一次本地端到端演示抓到的，不是被测试抓到的 |

修复与门禁 / fixes and gates:

- `service/` 移到 `src/service/`，并新增一条用空目录作 cwd 的回归测试
  (`test_service_is_importable_outside_the_checkout`)；CI 的导入断言也改成在 `mktemp -d` 里跑。
- 字符串是否为文件名改为显式判断 (`_looks_like_path`)，新增
  `test_inline_relay_json_larger_than_a_pathname_is_parsed_not_stat` 与
  `test_cli_push_accepts_a_large_inline_transcript` 两条正对照。
- 原先"轮询看到过 summarizing"的断言依赖桩模型速度，属于会随机变的测试；改为用 spy store
  确定性断言阶段序列 (`test_pipeline_reports_stage_transitions_in_order`)。
- 家目录查不到时改为返回 `None` 并跳过可选种子（`_desktop_seed_path`）；对应的
  `test_settings_load_survives_a_home_directory_that_cannot_be_resolved` 带**负对照**：
  已单独验证该探针环境里 `Path.home()` 确实抛 `RuntimeError`，否则这条测试只是空转。
- 重复声明的全局开关改用 `argparse.SUPPRESS` 作为子命令侧默认值，并新增
  `test_flags_before_the_subcommand_are_not_clobbered_by_its_defaults` 同时测前/后置两种写法。

一点测试基础设施的诚实说明 / one caveat about the harness: 这套测试会起**真实**的 localhost
服务（这是刻意的，见上表），所以同一个工作副本上不要并发跑两遍 pytest——第二次会因为本机
socket 压力随机报错（WinError 10055）。串行跑 114 项全绿。
The suite binds real localhost sockets on purpose, so two concurrent pytest runs against the
same checkout fail sporadically under this machine's socket pressure. Serial: 114 passed.

What the suite actually exercises, and how far:

| 层 / layer | 方式 / how | 距离真实 / realism |
| --- | --- | --- |
| URL 归一化 | 27 个真实分享文案形态 | 纯本地，输入即真实文本 |
| 字幕解析 (json3/srt/vtt/srv3/bilibili/lrc) | 构造的真实格式文本，含滚动字幕重复、`<v>` 说话人标签、`"\n"` 事件 | 格式真实，站点响应是构造的 |
| 回传门禁 | 稀疏/无时间/空内容三类拒绝 + 放行路径 | 逻辑真实，来源是构造 payload |
| 分层总结 | 桩模型 + "总是把输入写大"的对抗桩模型（证明收敛） | 模型是桩，循环与预算是真的 |
| LLM 传输 | **本机起真实 HTTP 服务**（OpenAI 兼容桩），走真 socket：端点拼接、usage 解析、429/503 退避重试、reasoning 模型回退字段、Ollama `think` 400 回退 | 传输是真的；模型不是真的 |
| HTTP 服务 | `TestClient` + 一个真 uvicorn 端口给 CLI 远程路径 | 协议与鉴权是真的 |
| 原缺陷回归 | `services.py:871` 的 `5xx_placeholder` NameError → 现在必须给出内存不足的建议 | 是 |

**未覆盖 / Not covered here (named, not hidden):**

1. **No call to a real LLM or ASR provider was made.** Both cost money and the
   user's keys were not supplied for this session. The wire format is verified
   against a stub server; quality of a real model's summary is not re-verified.
2. **`skills/video-summary/assets/browser-extract.js` has not been run inside a
   real logged-in browser.** It passes `node --check` and executes correctly
   against 7 stubbed page scenarios (creator track, ASR track with `tlang`,
   protocol-relative `subtitle_url`, empty anonymous list, `timedtext` 429,
   unsupported host, members-only). The in-app browser available to this session
   refuses navigation ("Browser operation timed out after 250ms"), so the live
   page-context test is **not signed off** and must be run once by the user in a
   browser that is logged into Bilibili.
3. **No deployment has been created yet** — see section 6.

## 5. 真实 CLI 输出（活网络）/ live CLI behaviour against the real network

```
$ pvc fetch "https://www.bilibili.com/video/BV1xx411c7mD/"
{
  "ok": false,
  "code": "needs_browser",
  "message": "没有可用通道取得该视频的逐字稿。bilibili_subtitles: B 站未向当前匿名会话下发任何字幕轨道；AI 字幕需要已登录 Cookie。（字幕君交流场所，时长 34:15） || audio_asr: 未设置 PVC_ASR_PROVIDER：没有字幕的视频将无法转写。",
  "data": { "hint": "1) 在已登录的浏览器里… 2) 配置 PVC_BILIBILI_SESSDATA… 3) 配置音频转写通道…" }
}
exit=1

$ pvc resolve "【硬核】大学物理 https://www.bilibili.com/video/BV1rjKn6CETw/?spm_id_from=333.788 值得一看"
{"ok": true, "data": {"url": "https://www.bilibili.com/video/BV1rjKn6CETw/", "platform": "bilibili", "video_id": "BV1rjKn6CETw", "identity": "bilibili:BV1rjKn6CETw"}}

$ pvc fetch "https://youtu.be/jNQXAC9IVRw"
（oembed 与 yt-dlp 各重试 3 次后）
{"ok": false, "code": "needs_browser", … "youtube_captions: YouTube 提取失败：…"}
```

These are the intended shapes: **the failure is the verified behaviour**, because
on an unauthenticated session no transcript exists to obtain, and inventing one
is the failure mode this design is built to prevent. The success path for a
logged-in session is section 4's relay test plus the still-unsigned-off item 2.

## 5b. 一次真实的 HTTP 往返 / one real HTTP exchange

本机用真实 uvicorn 监听端口 + 真实 FastAPI 应用跑通；只有总结模型是 localhost 上的
OpenAI 兼容桩（本会话没有可用的付费 key）。

```
[3] GET /healthz -> 200 {"ok":true,"auth_required":true,"llm_configured":true,
                         "asr_configured":false,"workers":2,"inbound_token_missing":false}
[4] POST /v1/jobs 无 token -> 401 {"code":"unauthorized","message":"缺少或错误的 Authorization Bearer 令牌。"}
[5] POST /v1/transcripts（30 段、1540 字、声明 up_subtitles）-> 200 任务已开始
[6] GET /v1/jobs/<id> -> completed；GET /v1/jobs/<id>/report ->
    transcript_source=up_subtitles segments=30 chars=1540
    usage={"llm_calls":2,"prompt_tokens":894,"completion_tokens":120,
           "chunk_calls":1,"reduce_calls":0,"final_calls":1,"total":2}
    渲染出 report_markdown / report_text / chat_message
[7] pvc --service <url> status -> ok=true, service.reachable=true
```

## 5c. 真浏览器验证与"正对照"补测抓到的三个真缺陷

用 Playwright 的真实 Chromium 访问真实页面（ logged-out ），并补写以真实响应结构为输入的
正对照测试之后，发现并修掉了三个**只有真环境才会暴露**的问题：

| 缺陷 | 症状 | 为什么会到生产才炸 |
| --- | --- | --- |
| B 站 `subtitle_url` 是根相对路径（`/x/player/v2subtitle?…`） | `ValueError: unknown url type`，取字幕直接抛异常 | 代码只处理了 `//` 前缀的形式。我第一次的真浏览器样本里三种形式都出现了 |
| YouTube 轨道排序写反：`manual = 0 if track.generated else 1` | **自动字幕永远排在人工字幕前面**，总结静默继承 ASR 的听错 | 只有同时存在两种轨道时才看得出，而本机一条正文都拿不到 |
| `YoutubeClient.transcript()` 只吃已经填好 `segments` 的轨道 | 拿到"清单但没正文"的轨道时报"内容过短"，其实根本没去 fetch | `tracks()` 内部会顺手 fetch，掩盖了这条路径的脆弱 |

同时把来源标签收敛成单一出处（`report_labels.py` + `Report.transcript_source_label`），
避免 JSON、Markdown、聊天短版三处说法不一致。

**这些测试证明了"当平台真的下发字幕时整条链路是对的"**（分块 → 逐块提示 → 是否压缩的判定 →
合成 → 六种渲染 → 落盘），但**仍然没有证明真平台会下发**。logged-out 真浏览器的实测结果是：
B 站页面自己的 `player/v2` 请求也拿到空列表（这是关键对照：排除了"我请求写法错了"），
YouTube 列出了 6 条轨道但每一次正文请求（包括页面自己的）都返回 0 字节。

## 5d. 安装路径本身是个缺陷 / the documented install route did not exist

需求审计（不是 CI）发现的：交付物里九处写着 `pip install pickvedio-core`
（七处文档 + `pvc serve` 与 `create_app()` 两处运行时 hint），而这个名字
从来没发到 PyPI。实测证据：

```
$ curl -o /dev/null -w '%{http_code}' https://pypi.org/pypi/pickvedio-core/json
404
$ curl -o /dev/null -w '%{http_code}' https://pypi.org/pypi/pickvedio/json
404
```

对 Muse 这类 agent 来说这不是笔误，而是死路：它会看到
`ERROR: No matching distribution found for pickvedio-core`，并按 `SKILL.md` 的规则
把它当成工具本身的 bug 来汇报。更糟的是两处运行时报错（`pvc serve` 缺 uvicorn、
`create_app()` 缺 fastapi）自己也在推荐这条死路。

替代命令实测可用（在本机，非 CI）：

```
$ python -m pip install --dry-run --no-deps "pickvedio-core[service] @ git+https://github.com/smartlotus/pickvedio-agent@main"
  Resolved https://github.com/smartlotus/pickvedio-agent to commit b200c8c8ef5ca621a97c88f4874aa2675bcc1f35
Would install pickvedio-core-1.0.0
```

改动与门禁：

- 文档与两处运行时 hint 改为带 `git+` 的版本号锁定地址；`.[media]` / `.[local-asr]`
  这类相对 extras 形式**保留不动**，因为它们只在 checkout 内被触发，那种场景下它们是对的。
- `pickvedio_core.install_spec(extra)` 成为唯一的拼接口径，pin 直接跟 `__version__`，
  所以版本号与安装地址不可能各说各话。
- CI 新增 `Install (documented route)` job，三件事：
  (a) 扫描 10 份随包文档，任何"看起来像 shell 命令"的行（`#` 注释与散文里的警告除外）
  若安装裸名字就红；负对照 `python3 -m pip install "pickvedio-core[service]"` 必须被
  同一个分类器抓住，否则这一步自己先失败——防止量具空转。
  (b) 在干净 venv 里、checkout 之外，逐字执行文档给的那条命令，再跑 `pvc --help`
  与 `pvc check`（离线自检，`network_used: false`）。
  (c) `grep -F` 断言 README 快速开始里出现的就是 CI 用的那个字符串——文档与门禁不许漂移。
- `release.yml` 的 PyPI job 现在要 `vars.PYPI_PUBLISH == 'true'` 才跑。旧的写法是"tag 一推
  pypi job 必红，因为 trusted publishing 还没配"，那会让一次正常发布看起来像失败发布；
  默认跳过、配好变量后一键启用，才是诚实的默认态。

## 5e. 第一次真推 tag 抓到的两个发布缺陷 / the first real tag push

v1.0.0 之前从未被推过，所以 `release.yml` 里两条路径都只是"看起来对"。真推之后：

- `Publish the GitHub Release` 直接红：`failed to run git: fatal: not a git repository`。
  那个 job 故意不做 checkout（它只需要 artifact），而 `gh` 在没有仓库上下文时会去调
  git 来推断仓库。**修法**：三处 `gh` 都显式 `-R "$GITHUB_REPOSITORY"`，并在上传后用
  `gh release view --json ... --jq` 把 tag/target/assets 念出来——只断言"上传命令返回 0"
  不足以说明读者真能找到一个完整 release。
- `--target "$GITHUB_SHA"` 在 `workflow_dispatch` 路径上是**分支头**而不是 tag 的提交；
  而 `GITHUB_REF_NAME` 在 dispatch 时等于 `main`，也就是说手动重发一次会造出一个名叫
  "main" 的 release 并挂在错误的提交上。**修法**：workflow 级 `env.RELEASE_TAG`
  = `inputs.tag || github.ref_name`，tag 推送与手动重发共用同一个值。
- 我上一条里写的"`--target` 用 tag 名就行"是**错的**，而且是我自己刚写下的错：
  本地用账号 token 真发一次 release 时，API 直接拒绝
  `HTTP 422: Release.target_commitish is invalid`。`target_commitish` 只接受分支名或
  commit SHA，不接受 tag 名。改成先 `gh api repos/<repo>/commits/<tag>` 把（带注释的）
  tag 解析成提交，再 `--target $TARGET`——这条在 tag 推送与手动重发两种事件下同形。
- `Publish the GitHub Release` 即便把 `contents: write` 同时写在 workflow 级和 job 级，
  在 **workflow_dispatch** 触发的那次仍然 `HTTP 403`；当时我据此写成"卡住的是仓库的
  Actions 设置"。**这句结论已被推翻**：v1.0.1 用同一份 `release.yml` 由 **tag 推送**触发，
  `Publish the GitHub Release` 自己就成功了，三个附件也是它挂上去的。三次观测的差别是
  "workflow 级 read 封顶"（第一次）与"事件是 dispatch 而非 tag push"（第二次），
  我没能证实为什么手动运行被拒而 tag 推送不被拒，所以文档与 yml 里现在只列观测、不编因果，
  失败分支同时给出两条出路（改默认权限 / 用账号 token 发），不再断言哪条是"真正的"原因。

### v1.0.0 实际发布出去了什么

CI 的 `Build sdist, wheel and skill pack` 全绿；三个下载件由我用账号 token 挂到了
`releases/tag/v1.0.0`（target 是 9b6cdf2，即 tag 指向的那个提交）：

```
gh release view v1.0.0 --json assets -> whl 101262 B / tar.gz 110867 B / skill zip 34765 B
curl -L .../releases/download/v1.0.0/video-summary-skill-1.0.0.zip -> 200, 34765 bytes
curl -L .../releases/download/v1.0.0/pickvedio_core-1.0.0-py3-none-any.whl -> 200, 101262 bytes
```

匿名可下载（Muse 不需要任何凭据就能取），字节数与 API 报告的 size 逐个相等。
装法也真验过：`pip install ./pickvedio_core-1.0.0-py3-none-any.whl[service]` 在干净 venv 里
成功，`import service.app` 从无关 cwd 构造出 `FastAPI` 对象，`pvc resolve` 直接可用——
这条路径不需要 git，是给"机器上没有 git"的用户的退路。

`Publish to PyPI` 在这次真推里是 `skipped`（预期的默认态，因为 `PYPI_PUBLISH` 未设置），
不再是"每次发布必红一格"。

## 5f. 桌面种子文件会跨 provider 污染传输层

为了跑 §2.3 的自检，我在本机起了一个 `PVC_LLM_PROVIDER=deepseek` 的服务，`/healthz` 却回答
`"llm_model": "gemma4:e4b"`。这不是显示问题，是配置真的被换掉了：

- 本机 `~/.pickvedio/settings.json` 的 `summary` 块是桌面版写的，里面
  `provider=ollama`、`base_url=http://127.0.0.1:11434/v1`、`model=gemma4:e4b`（我用只打印
  结构和长度的方式核过，没打印任何密钥）。
- `LLMSettings.from_env` 的取值顺序是 `env → seed → preset`，而 seed 的 `base_url`/`model`
  **不看 provider 是否一致**就被采纳。于是 deepseek 的名字配上 Ollama 的端点和模型名——
  请求会打到 127.0.0.1:11434 要一个 `gemma4:e4b`，失败信息完全没法反推出原因。
- 复刻旧表达式的对照（同 seed、同环境变量）：
  `OLD -> http://127.0.0.1:11434/v1 gemma4:e4b` / `NEW -> https://api.deepseek.com/v1 deepseek-chat`。

修法是把 seed 当作"传输层来源"的资格绑在 provider 一致上（`transport = seed if 一致 else {}`），
`temperature`/`chunk_chars`/`profile_prompts` 这些与端点无关的字段照旧从 seed 读。
两条测试一起进：一条断言换 provider 后必须回到预设，另一条断言"什么都不设"时
桌面版那条本地 Ollama 路径仍然按 seed 走——防止修过头。seed 只在装有桌面版的机器上存在，
所以 Muse 云电脑与 Railway 容器本来不受影响；受影响的是用户自己的这台机。

顺带把 `/healthz` 的两个字段说清楚（我第一次把它们读错了）：
`llm_configured = bool(settings.llm.api_key or settings.ollama.enabled)`，
`llm_model = settings.llm.model or settings.ollama.model`。所以那次 `llm_configured: true`
**不是**本机有什么隐藏 key —— 实测这台机 shell 里
`PVC_LLM_API_KEY`/`PVC_API_KEY`/`DEEPSEEK_API_KEY`/`OPENAI_API_KEY` 四个名字全部未设
（只查名字与长度，不打印值），true 来自 seed 让 `OllamaSettings.enabled` 成立
（`enabled = bool(model)`）。也就是说这两个字段在"本地 Ollama 可用"与"远程 key 已配"之间
本来就是同一个布尔，读的人容易误会；要区分就看 `pvc status` 的 `missing` 数组。

### 同一处泄漏的第二种形态（用发布件跑端到端时撞上）

第一次修复只挡住了 `base_url`/`model` 的跨 provider 污染。用 v1.0.0 的 wheel 做端到端时，
我只设了 `PVC_LLM_BASE_URL`/`PVC_LLM_API_KEY`/`PVC_LLM_MODEL`（**没有**设 `PVC_LLM_PROVIDER`），
任务直接失败：`无法连接 Ollama`。原因在 `LLMSettings.from_env` 的第一行取值——
provider 本身也是"env 没有就读 seed"，于是 seed 的 `ollama` 把整个通道选走了，
而 `build_client` 又因为 `ollama.enabled` 命中本地分支。

现在的规则收成一句话：**环境里对 LLM 有任何表态（provider/base_url/model/api_key 之一），
seed 就不再对总结器说话**；提示词、`chunk_chars`、`profile` 照旧从 seed 延续。
`OllamaSettings` 用同一个判据，避免"文件让 Ollama 恒为 enabled"。

两条端到端实测（本机，零网络零花费：桩模型在 127.0.0.1:8931，字幕走 relay 回传）：

| 场景 | 代码 | 是否显式给 provider | 结果 |
| --- | --- | --- | --- |
| A | v1.0.0 发布件 wheel（`pip install .whl[service]`） | 是 | `ok=true`，job 09003989…，`usage.llm_calls=2`（chunk 1 + final 1），摘要为桩文本 |
| B | 当前 checkout（含本次修复） | **否** | `ok=true`，job aa3bfc8d…，`usage.llm_calls=2`，同样的桩摘要 |

A 证明"只用公开下载件、不装 git、不 clone"也能走完 链接→回传字幕→建任务→分段→总结→报告；
B 证明修复确实把"没设 provider 但设了远程端点"这一形态从 Ollama 手里拿回来了。
服务鉴权门禁同时复验：不带令牌 POST `/v1/jobs` → 401。
三个测试进程用完即杀，端口 8931-8933 复查为 0 监听。

## 5g. 为什么出 v1.0.1：发布件会比 HEAD 陈旧

v1.0.0 的三个附件是从 tag 提交 9b6cdf2 构建的，而 tag 之后 main 上落了四处修复。真正的风险不是
"版本号不好看"，是 **skill zip 里那份 `SKILL.md` 是旧的**：它仍然写着"失败时读
`code`、`message` 和 `hint`"，而 `hint` 一直在 `data` 里面——照那份 zip 装技能的 agent
会去读一个不存在的键，然后把它遇到的每个失败都当成"服务没给提示"。同理，wheel 里既没有
`short_link` 表态，也还带着桌面 seed 抢通道的配置缺陷。

所以把版本推到 1.0.1，让"文档里那条 pin"、`install_spec()`、CI 的 `PVC_INSTALL_SPEC` 与
发布件四者重新对齐。机制上这次对齐是可检查的，不是靠人记得：

- `pickvedio_core.install_spec()` 直接拼 `__version__`，运行时报的安装地址不会落后；
- CI 的 install job 断言 README 里出现的就是它自己要执行的那一整串；
- 文档门禁扫描全部随包 md，任何"像命令"的安装行都必须带可解析的来源。

`verification.md` 上面 §5d–§5f 里那些 `v1.0.0` 字面量是**历史记录**，不跟着 bump 改，
它们描述的就是当时那次发布。

v1.0.1 的结果（不是计划，是已发生）：tag 推送触发的 Release 工作流自己把发布做完了
（`Build sdist, wheel and skill pack` success、`Publish the GitHub Release` success、
PyPI 按 `vars.PYPI_PUBLISH` 未设而 skipped）。把三个附件匿名下载回来逐一验过内容：

```
video-summary-skill-1.0.1.zip  35000 B  7 个条目
  SKILL.md 里出现 "data.hint" 与 "short_link"      -> True / True
  旧的误导性措辞 "read `code`, `message` and `hint`" -> 不存在
  文内安装 pin                                      -> pickvedio-agent@v1.0.1
pickvedio_core-1.0.1-py3-none-any.whl 102217 B
  pickvedio_core/__init__.py 的 __version__          -> 1.0.1
  config.py 里含 seed_speaks（seed 抢通道的那道闸）    -> True
pickvedio_core-1.0.1.tar.gz 113582 B
```

也就是说"发布件教 agent 去读一个不存在的键"这个坑，现在是靠 zip 里的原文消除的，
不是靠我在 README 里多说一句话。

## 5h. 装配脚本的三处真故障，和 CI 抓住的我自己的 bug

推荐路线的第一条命令是 `bash bootstrap.sh`，而它自己有三个失败面：

1. **Debian/Ubuntu 云镜像上 `python3 -m venv` 根本不可能成功**（ensurepip 在独立的
   `python3-venv` 包里）。现在会保留原始错误、改用 `venv --without-pip` 再从 PyPA 的
   `get-pip.py` 装 pip；两条兜底各自失败时都点名 `apt-get install -y python3-venv`。
   这条分支本机无法复现（Windows 没有这种坏镜像），所以它进了 CI：`ubuntu:24.04` +
   发行版 python3，并且**先断言镜像真的是坏的**（`python3 -c "import ensurepip"` 必须失败），
   否则那次绿什么也没证明。
2. **Windows 上 `python3` 常常是 Microsoft Store 的执行别名**：`command -v` 找得到它，
   一执行就退出码 49、什么都不打印。本机实测：
   `which python3 = .../WindowsApps/python3`，`python3 -V` 与 `python3 -c ...` 均 rc=49。
   旧代码只看"在不在 PATH"，于是选中它并报 `needs Python >= 3.10, found `（版本是空的）。
   现在候选解释器必须真跑起来（`sys.version_info >= (3,10)` 探针）才会被选中。
3. **我自己这次改动引入的 bug，被 CI 抓住**：把 `VENV_PY` 的判定挪到建 venv 之前，
   Linux 上那时两个布局都还不存在，于是它落到 `Scripts/python.exe` 并且之后不再重算，
   下一个命令直接 `No such file or directory`。CI 的
   `Bootstrap (agent VM path)` 就是红在这里（`bootstrap.sh: line 99`）——不是新步骤红，
   是我把主路径改坏了。修法是把解析收进 `find_venv_python()`，每次创建之后重新解析；
   并在结尾再判一次空值给出可读错误。
   同一个坑的第二个变体也当场出现：`set -euo pipefail` 下，赋值语句右侧的
   `$(python3 -V …)` 失败会让整个脚本以 49 退出、连一行日志都没有——收集"为什么失败"的
   那行自己把脚本杀了。现在用 `{ … || true; } | head -c 40` 包住。

顺带清掉一个打包层的定时炸弹：`project.license` 写成 TOML 表已被 setuptools 弃用
（警告里写明 2027-02-18 起不再支持），而且它会在**每一次**安装的输出里刷十几行，
恰好是 Muse 会被迫读的那段。改成 `license = "MIT"` + `license-files`，
`[build-system] requires` 提到 `setuptools>=77`。本机复验：`pip wheel .` 输出里
`grep -ci deprecated` = 0，METADATA 出现 `License-Expression: MIT` 与 `License-File: LICENSE`，
wheel 内含 LICENSE 文件。

最后是这次踩到的"发布件落后于 HEAD"这一类问题的机制化防呆：

- `__version__` 与 `pyproject.toml` 的 version 由一条测试钉住（两者不一致就红）；
- CI 的安装 job 不再自己写死版本号——它从 `pyproject.toml` 读出版本、拼出
  `PVC_INSTALL_SPEC`，并扫描 10 份随包文档，任何 git+ pin 或 Release 附件 pin 与之不符就红；
  负对照是同一段分类器必须能抓出一行故意留下的 `@v1.0.1`；
- `docs/verification.md` 自己在白名单里（那里的旧版本号是历史证据，不是指令）。

出 1.0.2 就是为了让 pin、`install_spec()`、CI 拼出的地址与发布附件四者重新相等。

## 5i. 门自己也会坏：CI 抓住的第二轮，和发布件的事后实测

§5h 那道"文档 pin 必须等于 pyproject 版本"的 gate，第一次上 CI 就红了自己：
扫描步骤的 unquoted heredoc 里插值的是 `$VERSION`，而 derive 步骤写进 `GITHUB_ENV`
的名字是 `PVC_VERSION`——后者才会导出给后续步骤，前者在那个 step 里展开为空串，
gate 于是报 "could not read a version out of pyproject.toml"（run 42246378362 的
`Install (documented route)`，4060e12；工作区里一度出现的半截修复 `$PVC_VERSION`
拼写也被同一个 run 判定不足）。修法不修拼写、修整类：heredoc 加引号，版本改从
`pyproject.toml` 直接读，删掉"shell 传版本"这一整条路（b6ce80a）。同一个提交上
CI 与 Docker 两个工作流全绿（run 42247626605 / 42247626406）。规律与 §5h 第 3 条
一致：bug 总是长在"验证别人的那一侧"。

v1.0.2 tag 在 4060e12 上推送，release.yml 于 41 秒后自行发布三件附件。b6ce80a
只改了 `.github/workflows/`，而附件内容不包含 workflow 文件，所以发布件与修复后
HEAD 构建结果一致，不重切。但"应该一致"不是证据，发布之后补了两条独立实测
（与 CI 完全无关，本机执行）：

1. 干净 venv、无任何令牌，`pip install "pickvedio-core[service] @
   https://github.com/smartlotus/pickvedio-agent/releases/download/v1.0.2/pickvedio_core-1.0.2-py3-none-any.whl"`
   —— 安装成功，`pvc --help` 正常，`pvc check` 报 `ok:true` / `network_used:false`，
   `pickvedio_core.__version__ == "1.0.2"`。README 那句"匿名可下载，已实测 pip 能
   取到并装上 1.0.2"在 4060e12 写下时是预测，此刻起才是测量。
2. skill zip 匿名下载原样解包：正好 7 个条目；`SKILL.md` frontmatter 含 Muse 必需的
   `name` / `description` / `icon` / `metadata` 四键；包内全部安装 pin 均为 `@v1.0.2`。

dependabot 的 actions 升级 PR（#1）rebase 到 4060e12 之后，继承了同一个红 job；
让它再 rebase 到 b6ce80a 上重跑，等绿再合，不盲并。

这一节自己又当场添了一笔：它第一次落库没走 git，走了 Contents API——当时
github.com:443 从写作者的机器上不可达，而 api.github.com 可达（提交信息里有括注）。
API 直传磁盘原始字节，绕过 clean filter，blob 里带进了 505 个 CRLF；而
`.gitattributes` 对 `*.md` 钉死 `eol=lf`，且文件开头自称 load-bearing。后果精确命中
CI 里唯一会跨 commit checkout 的 job：`Install (documented route)` 用 pip 装这个
git 依赖时，在**全新 clone** 里 `git checkout v1.0.2` 被 "docs/verification.md 有
本地改动" 拒绝（run 42249523515）——全新 clone 不可能有本地改动，可能的只有磁盘
字节与过滤器约定不一致。修复即本提交：重传 LF 归一后的字节。教训并入同一条：
**绕过 git 的写入路径，同时绕过了 git 替你守住的字节约定**；上一条里"修类不修拼写"
的原则，在这里的对应形态是"写入必须走会被过滤器看见的路"。

## 6. 尚未验证 / still to verify after deployment
- A real Railway (or other) deploy answering `/healthz` over the public internet.
- Muse actually reading this repo and deploying it from a GitHub URL.
- Muse's browser actually running `browser-extract.js` on a logged-in page.
- A real model call producing a summary the user considers good (the prompts are
  ported byte-for-byte from the desktop app, and the desktop app's own stored
  `profile_prompts` in `~/.pickvedio/settings.json` were confirmed **identical**
  to the ported built-ins, which is the closest available proxy for "same output
  as before").
