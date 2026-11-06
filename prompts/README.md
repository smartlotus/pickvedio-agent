# 提示词覆盖目录 / prompt overrides

这个目录里的文件会**覆盖**内置提示词模板，不需要改代码。
Files here override the built-in prompt templates. No code change required.

## 命名规则 / file naming

```
<profile>_<slot>.md
```

- `profile` ∈ `general` · `interview` · `tutorial` · `brief` · `custom`
- `slot` ∈ `chunk`（逐块整理） · `final`（最终汇总） · `chat`（追问回答）

例如 `prompts/tutorial_final.md` 只替换"教程"档位的最终总结提示词；其余槽位仍用内置模板。
For example `prompts/tutorial_final.md` replaces only the tutorial profile's
final-synthesis prompt; its other slots keep the built-ins.

## 必须保留的占位符 / placeholders you must keep

| slot | 占位符 |
| --- | --- |
| `chunk` | `{{video_title}}` `{{chunk_start}}` `{{chunk_end}}` `{{transcript}}` |
| `final` | `{{video_title}}` `{{duration}}` `{{chunk_summaries}}` |
| `chat` | 无（作为前缀使用，问题与资料由程序追加） |

未替换的 `{{token}}` 会原样出现在提示词里（这是故意的：宁可让缺少的内容显眼，也不要静默留下一个空洞）。
An unfilled `{{token}}` stays visible in the prompt. That is deliberate: a hole
you can see beats a prompt that quietly lost a section.

校验方式 / to check a template:

```python
from pickvedio_core.prompts import load, missing_tokens, PROFILE_DEFAULTS
missing_tokens(load("general").chunk, {"video_title":"t","chunk_start":"0","chunk_end":"1","transcript":"x"})
```

## 优先级 / precedence

内置（`src/pickvedio_core/prompts.py`，逐字节移植自桌面程序）
→ 桌面程序存在 `~/.pickvedio/settings.json` 里的 `profile_prompts`
→ 本目录的文件（最高）。
Built-ins → the desktop app's stored `profile_prompts` → files in this
directory (highest).

## 部署注意 / deployment note

装成 wheel 后本目录不存在，`PVC_PROMPT_DIR` 可以指向别处；容器里直接把这两个目录挂载进去即可改提示词而不用重建镜像。
After `pip install` this directory is not part of the package. Point
`PVC_PROMPT_DIR` anywhere you like; in a container, mount it so prompt edits do
not require a rebuild.

## 为什么默认文件不在这里

内置模板就是当前生效的版本，重复一份到本目录只会造成两处漂移。要定制再新建文件。
The built-ins are the live version; shipping duplicates here would only let the
two drift. Add a file when you actually want a change.
