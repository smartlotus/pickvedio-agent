"""Prompt profiles, ported verbatim from the desktop app, plus file overrides.

Why verbatim: these three templates are the product. They were tuned inside the
pickvedio UI (``PROMPT_PROFILE_DEFAULTS``) against real Bilibili long-form
content, and the summary quality of an already-deployed personal workflow is
worth more than a rewrite for taste. ``general`` / ``interview`` / ``tutorial``
are byte-identical to the desktop defaults.

Two additions:

* ``brief`` -- a short answer sized for a phone chat, because the Muse flow
  replies in a conversation rather than opening a document;
* ``custom`` -- empty, kept so an override file is the only source.

Any template can be overridden without touching code by dropping a file named
``<profile>_<slot>.md`` in the prompt directory (``PVC_PROMPT_DIR``, default
``prompts/`` next to this package). The ``{{placeholder}}`` tokens must survive;
:func:`render` leaves an unknown token in place so a typo shows up in the output
instead of silently vanishing.
"""
from __future__ import annotations

import re
from dataclasses import dataclass

from .errors import ConfigurationError

PLACEHOLDER_RE = re.compile(r"\{\{([a-z_]+)\}\}")

PROFILE_LABELS: dict[str, str] = {
    "general": "通用 · 全景研究笔记",
    "interview": "访谈 · 人物与观点脉络",
    "tutorial": "教程 · 可复现操作手册",
    "brief": "速览 · 手机对话可直接读",
    "custom": "自定义 · 空白模板",
}

PROFILE_DEFAULTS: dict[str, dict[str, str]] = {
    "general": {
        "chunk": (
            "你是视频逐字稿的证据整理师。只依据下方片段，不补写、不脑补。\n"
            "视频标题：{{video_title}}\n片段时间：{{chunk_start}} - {{chunk_end}}\n\n"
            "请用 Markdown 完整提炼：\n"
            "1. 本段主题与叙事推进\n"
            "2. 所有可验证的主张、推理链、因果关系与限定条件\n"
            "3. 人物、概念、术语、数据、案例和原话要点\n"
            "4. 关键反例、异议、不确定处或待核实信息\n"
            "5. 可追溯的时间点；不要遗漏看似次要但影响结论的细节\n\n"
            "逐字稿：\n{{transcript}}"
        ),
        "final": (
            "你是长视频研究总编。仅根据下方分段证据写出高保真、信息覆盖尽可能完整的中文总结；"
            "不得把推断写成事实。\n"
            "视频标题：{{video_title}}\n时长：{{duration}}\n\n"
            "必须用 Markdown 输出：\n"
            "# 一句话总论\n"
            "# 内容全景与叙事主线\n"
            "# 章节时间线（每章标注时间范围）\n"
            "# 全部核心观点、推理链与支撑证据\n"
            "# 人物、概念、案例、数据与重要细节\n"
            "# 分歧、限制条件、反例与尚未解决的问题\n"
            "# 可执行启发 / 值得继续研究的问题\n\n"
            "要求：保留观点之间的关系与前后转折；对重复内容做合并但不丢失独特信息；"
            "信息不足时明确标注。\n\n"
            "分段摘要：\n{{chunk_summaries}}"
        ),
        "chat": (
            "你是此视频的深度研究助理。只能以“视频总结资料”为主要证据回答问题；"
            "资料没有说明时要明确说“总结资料未提供”，并可提出需要查看逐字稿的建议。"
            "请用中文作答，区分【资料明确说明】与【基于资料的推断】，"
            "优先引用资料中的章节或时间线。回答要直接、严谨、尽可能完整。"
        ),
    },
    "interview": {
        "chunk": (
            "你是深度访谈的逐段编辑。只基于本段逐字稿，完整保留访谈双方的立场和语境。\n"
            "视频标题：{{video_title}}\n片段时间：{{chunk_start}} - {{chunk_end}}\n\n"
            "请输出：\n"
            "1. 发言者 / 角色与本段议题\n"
            "2. 每个重要提问及其完整回答要点\n"
            "3. 受访者的经历、判断、价值观、方法或预测\n"
            "4. 论证依据、案例、数据、关键措辞与观点转折\n"
            "5. 主持人的追问、质疑、分歧、保留意见和未回答问题\n"
            "6. 可追溯时间点\n\n"
            "逐字稿：\n{{transcript}}"
        ),
        "final": (
            "你是高质量人物访谈的总编辑。依据分段摘要，写一份可替代观看的完整中文访谈纪要，不得虚构。\n"
            "视频标题：{{video_title}}\n时长：{{duration}}\n\n"
            "必须包含：\n"
            "# 访谈总览与人物画像\n"
            "# 对话时间线\n"
            "# 关键问题—回答—追问（按主题完整编排）\n"
            "# 受访者的核心论点、证据、经历与方法论\n"
            "# 观点变化、矛盾、分歧与限制条件\n"
            "# 最值得记住的原意与后续可追问问题\n\n"
            "保留“谁说了什么”及上下文，合并重复但不遗漏独有事实。\n\n"
            "分段摘要：\n{{chunk_summaries}}"
        ),
        "chat": (
            "你是这场访谈的研究型编辑。只以“视频总结资料”为依据回答，必须区分主持人、受访者与编辑推断；"
            "需要时按问题—回答—证据的结构展开，并标出资料缺口。用中文严谨回答。"
        ),
    },
    "tutorial": {
        "chunk": (
            "你是教程逐字稿的技术编辑。只依据本段，提取所有可复现的信息，不跳过前置条件或失败经验。\n"
            "视频标题：{{video_title}}\n片段时间：{{chunk_start}} - {{chunk_end}}\n\n"
            "请输出：\n"
            "1. 当前目标、输入、前置条件与工具 / 版本\n"
            "2. 按顺序列出的具体步骤、参数、命令、设置与关键操作\n"
            "3. 每步的原因、预期结果和验证方式\n"
            "4. 常见错误、限制、替代方案、效率技巧与安全提醒\n"
            "5. 例子、数据和对应时间点\n\n"
            "逐字稿：\n{{transcript}}"
        ),
        "final": (
            "你是专业教程的课程设计师。根据分段摘要写一份可独立执行、信息覆盖完整的中文教程笔记；"
            "不添加资料外步骤。\n"
            "视频标题：{{video_title}}\n时长：{{duration}}\n\n"
            "必须包含：\n"
            "# 最终目标与适用场景\n"
            "# 前置知识、工具、材料与环境\n"
            "# 章节时间线\n"
            "# 从零到结果的完整步骤（操作、参数、原因、预期输出）\n"
            "# 关键概念与决策依据\n"
            "# 排错清单、常见坑、限制与替代路线\n"
            "# 复盘检查表与可进一步练习\n\n"
            "把分散在不同片段的操作重新按执行顺序组织，仍保留资料中的时间线和不确定处。\n\n"
            "分段摘要：\n{{chunk_summaries}}"
        ),
        "chat": (
            "你是此教程的耐心技术助教。以“视频总结资料”为依据，给出可执行、按步骤的中文回答，"
            "说明前置条件、预期结果和排错点。资料未覆盖的操作必须明确说明，不能假装视频讲过。"
        ),
    },
    "brief": {
        "chunk": (
            "你在为手机端阅读整理视频逐字稿。只用本段内容，不加推测。\n"
            "视频标题：{{video_title}}\n片段时间：{{chunk_start}} - {{chunk_end}}\n\n"
            "输出不超过 200 字：\n"
            "1. 本段在讲什么（一句话）\n"
            "2. 3-5 条最关键的要点，每条一行，带必要的时间点\n"
            "3. 如有数字、结论、转折，必须保留\n\n"
            "逐字稿：\n{{transcript}}"
        ),
        "final": (
            "把下面的分段摘要合并成一条可以直接发在聊天里的中文速览。不要客套，不要元叙述。\n"
            "视频标题：{{video_title}}\n时长：{{duration}}\n\n"
            "格式（Markdown，总长 350 字以内）：\n"
            "**一句话结论**\n\n"
            "**要点**（3-6 条，每条一行，可带时间点）\n\n"
            "**值得注意**（数据/转折/争议，没有就省略本节）\n\n"
            "分段摘要：\n{{chunk_summaries}}"
        ),
        "chat": (
            "你是这个视频摘要的追问助手。只用给出的摘要与逐字稿回答，答案控制在 150 字内；"
            "资料没有的内容明确说“摘要里没有”，不要编。"
        ),
    },
    "custom": {"chunk": "", "final": "", "chat": ""},
}

# Fallbacks used when a profile omits a slot (notably ``custom``), so an
# unfinished override file still produces a report instead of an empty prompt.
_BASELINE = PROFILE_DEFAULTS["general"]


@dataclass(slots=True)
class PromptSet:
    profile: str
    label: str
    chunk: str
    final: str
    chat: str
    source: str = "builtin"

    def slot(self, name: str) -> str:
        value = getattr(self, name, "") or ""
        if value.strip():
            return value
        baseline = _BASELINE.get(name, "")
        if not baseline.strip():
            raise ConfigurationError(f"提示词槽位 {name} 为空，且没有可用的内置默认值。")
        return baseline


def _override_dir(prompt_dir=None):
    from pathlib import Path

    if prompt_dir:
        return Path(prompt_dir)
    return Path(__file__).resolve().parent.parent.parent / "prompts"


def load(profile: str = "general", prompt_dir=None, seeded: dict | None = None) -> PromptSet:
    """Resolve a profile.

    Precedence, lowest to highest: this repo's built-in defaults, the desktop
    app's ``profile_prompts`` from ``~/.pickvedio/settings.json`` (which holds
    templates the user edited by hand -- losing them would quietly change the
    output of an already-working personal workflow), then files in
    ``PVC_PROMPT_DIR``, which win because a deployed container reads the repo,
    not a home directory.
    """
    key = (profile or "general").strip().lower()
    if key not in PROFILE_DEFAULTS:
        raise ConfigurationError(
            f"未知的总结档位 {profile!r}。可选：{'、'.join(PROFILE_LABELS)}。"
        )
    values = dict(PROFILE_DEFAULTS[key])
    stored = ((seeded or {}).get(key) or {}) if isinstance(seeded, dict) else {}
    if isinstance(stored, dict):
        for slot in ("chunk", "final", "chat"):
            text = str(stored.get(slot) or "").strip()
            if text:
                values[slot] = text
    directory = _override_dir(prompt_dir)
    used_files = []
    if directory.is_dir():
        for slot in ("chunk", "final", "chat"):
            candidate = directory / f"{key}_{slot}.md"
            if candidate.is_file():
                text = candidate.read_text(encoding="utf-8").strip()
                if text:
                    values[slot] = text
                    used_files.append(candidate.name)
    origin = "file" if used_files else ("desktop_settings" if stored else "builtin")
    return PromptSet(
        profile=key,
        label=PROFILE_LABELS[key],
        chunk=values["chunk"],
        final=values["final"],
        chat=values["chat"],
        source=origin,
    )


def render(template: str, **values: str) -> str:
    """Fill ``{{token}}`` placeholders, leaving unknown tokens visible.

    A missing value must be loud: silently producing a prompt with a hole in it
    is how a summary ends up confident about a section it never saw.
    """
    rendered = template
    for key, value in values.items():
        rendered = rendered.replace("{{" + key + "}}", str(value))
    return rendered


def missing_tokens(template: str, provided: dict[str, str]) -> list[str]:
    return sorted({token for token in PLACEHOLDER_RE.findall(template) if token not in provided})


def list_profiles() -> list[dict[str, str]]:
    return [
        {
            "profile": key,
            "label": PROFILE_LABELS[key],
            "chunk_chars": len(value["chunk"]),
            "final_chars": len(value["final"]),
            "has_chat": bool(value["chat"].strip()),
        }
        for key, value in PROFILE_DEFAULTS.items()
    ]
