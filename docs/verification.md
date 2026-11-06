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

## 6. 尚未验证 / still to verify after deployment

- A real Railway (or other) deploy answering `/healthz` over the public internet.
- Muse actually reading this repo and deploying it from a GitHub URL.
- Muse's browser actually running `browser-extract.js` on a logged-in page.
- A real model call producing a summary the user considers good (the prompts are
  ported byte-for-byte from the desktop app, and the desktop app's own stored
  `profile_prompts` in `~/.pickvedio/settings.json` were confirmed **identical**
  to the ported built-ins, which is the closest available proxy for "same output
  as before").
