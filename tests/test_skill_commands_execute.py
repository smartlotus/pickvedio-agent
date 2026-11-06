"""The keyless command line in the docs is executed as written, not paraphrased.

Everything else in this suite builds argv from a Python literal, so a doc that
drifts from the code -- a renamed flag, a missing --url, a placeholder nobody
substitutes -- stays green while the agent on the Muse VM reads the doc, runs
what it says, and fails. The paste blocks are the actual product surface here.

So this file *extracts* the command from the shipped prose, substitutes its own
placeholders, and runs it. Two fixtures, one command: a thin relay must come
back refused and a real one must come back with segments. If the sentence is
reworded into unrecognisability, or dropped from any of the three files, the
count assertion fails rather than the test quietly doing nothing.
"""
from __future__ import annotations

import json
import re
import shlex
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
DOCS = [
    ROOT / "skills" / "video-summary" / "SKILL.md",
    ROOT / "skills" / "video-summary" / "references" / "deploy-runbook.md",
    ROOT / "deploy" / "muse-setup.md",
]
URL = "https://www.bilibili.com/video/BV1pa5Ez5E2e/"

# The documented command, with whatever placeholder the prose used, in whatever
# order. Matching is on the tokens the agent has to get right.
COMMAND = re.compile(r"pvc\s+fetch\b[^\n`]*?--transcript[^\n`]*?--out\s+[\w./-]+")
PLACEHOLDER = re.compile(r"<[^<>\n]{1,80}>")


def relay(segments: list[dict], duration: int) -> str:
    return json.dumps({
        "platform": "bilibili", "url": URL, "title": "四十分钟实测讲座",
        "durationSeconds": duration, "sourceKind": "up_subtitles", "language": "zh-CN",
        "trackCount": 1, "segments": segments,
        "charCount": sum(len(item["text"]) for item in segments),
        "warnings": [], "reason": None,
    }, ensure_ascii=False)


def cues(count: int, size: int) -> list[dict]:
    return [{"start": float(i * 15), "end": float(i * 15 + 14),
             "text": "这句话是用来填满切块的字幕内容。" * size}
            for i in range(1, count + 1)]


def documented_commands() -> list[tuple[str, str]]:
    """Runnable examples only.

    The command *reference* is written in grammar notation -- `[--out FILE]` --
    and is not a command line anybody can execute. Only lines with no optional
    brackets and no FILE stand-in are candidates here, so the gate runs the
    prose the agent actually copies.
    """
    found: list[tuple[str, str]] = []
    for doc in DOCS:
        for line in doc.read_text(encoding="utf-8").splitlines():
            if "[" in line or "FILE" in line or "…" in line:
                continue
            for match in COMMAND.finditer(line):
                found.append((doc.name, " ".join(match.group(0).split())))
    return found


def to_argv(raw: str, tmp_path: Path, payload_text: str) -> list[str]:
    """Substitute the prose's own placeholders, then split the way a shell would.

    The docs spell the target as `<canonical url>` / `<canonical link>` /
    `<规范链接>` and the files as bare relative names (relay.json, segments.json),
    so the substitution is: angle brackets -> the fixture URL, *.json -> under
    tmp_path. Nothing else is rewritten, so a doc that starts inventing flags
    fails at argparse rather than here.
    """
    def swap(match: re.Match) -> str:
        token = match.group(0)[1:-1].strip()
        low = token.lower()
        if "链接" in token or "url" in low or "link" in low:
            return URL
        return token

    argv = shlex.split(PLACEHOLDER.sub(swap, raw))
    if argv and argv[0] != "pvc" and not argv[0].endswith("pvc"):
        raise AssertionError(f"extracted text is not a pvc command: {argv[:3]}")
    argv = argv[1:] if argv else argv
    out: list[str] = []
    for token in argv:
        if token.endswith(".json"):
            path = tmp_path / Path(token).name
            if Path(token).name == "relay.json":
                path.write_text(payload_text, encoding="utf-8")
            out.append(str(path))
        else:
            out.append(token)
    return out


@pytest.fixture()
def offline(monkeypatch, tmp_path):
    """The documented keyless command must not reach the network or a model."""
    from pickvedio_core.sources import ladder

    def boom(*args, **kwargs):
        raise AssertionError("the documented keyless command touched the model path")

    monkeypatch.setattr("pickvedio_core.pipeline.build_client", boom)
    monkeypatch.setattr(ladder, "resolve", boom)
    monkeypatch.setenv("PVC_WORKDIR", str(tmp_path / "work"))
    monkeypatch.delenv("PVC_SERVICE_URL", raising=False)


def test_the_shipped_docs_all_advertise_an_executable_keyless_command():
    found = documented_commands()
    assert len(found) >= 3, f"expected the command in SKILL.md, the runbook and the paste block, got {found}"
    assert {name for name, _ in found} == {"SKILL.md", "deploy-runbook.md", "muse-setup.md"}


def test_the_documented_command_line_runs_and_returns_segments(offline, tmp_path, capsys):
    from pvc import cli

    raw = next(cmd for _, cmd in documented_commands() if "--out" in cmd)
    argv = to_argv(raw, tmp_path, relay(cues(120, 4), 2400))
    assert argv[0] == "fetch" and "--transcript" in argv, argv

    rc = cli.main(argv)
    assert rc == 0, capsys.readouterr().out
    data = json.loads(capsys.readouterr().out)["data"]
    assert len(data["segments"]) == 120
    out_file = Path(argv[argv.index("--out") + 1])
    assert out_file.is_file() and json.loads(out_file.read_text(encoding="utf-8")) == data["segments"]


def test_the_documented_command_refuses_the_payload_the_docs_warn_about(offline, tmp_path, capsys):
    """Same argv, thin payload: the sentence's promise that the guard still fires."""
    from pvc import cli

    raw = next(cmd for _, cmd in documented_commands() if "--out" in cmd)
    argv = to_argv(raw, tmp_path, relay(cues(20, 1), 1800))

    rc = cli.main(argv)
    assert rc == 1, "the docs claim this path is guarded; an rc of 0 here falsifies them"
    document = json.loads(capsys.readouterr().out)
    assert document["code"] == "sparse_transcript", document
    assert document["data"]["hint"]
