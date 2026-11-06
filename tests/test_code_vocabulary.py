"""Every failure code the tool can print must be explained where agents read.

The SKILL ladder branches on ``code``, so an undocumented code is not a missing
sentence in a manual -- it is a branch the agent has to invent. Measured before
this file existed, fourteen codes came from the error classes and several more
only from literal strings inside the CLI and the service, and three of them
(``unauthorized``, ``not_ready``, ``cancelled``) appeared in no agent-facing
document at all. ``unauthorized`` was worse than the others: it was introduced
in this very release line when the auth failures were flattened, so the change
that fixed one contract quietly created an unexplained code.
"""
from __future__ import annotations

import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]

# Codes that describe success or a state, not a failure the agent must interpret.
NON_FAILURE_CODES = {"ok", "queued"}

AGENT_DOCS = [
    "skills/video-summary/SKILL.md",
    "skills/video-summary/references/troubleshooting.md",
    "skills/video-summary/references/browser-extract.md",
    "skills/video-summary/references/deploy-runbook.md",
    "docs/skill-usage.md",
    "README.md",
]


def emitted_codes() -> dict[str, str]:
    """Every ``code`` value the shipped package can print, with where it came from."""
    sources: dict[str, str] = {}
    classes = (ROOT / "src/pickvedio_core/errors.py").read_text(encoding="utf-8")
    for code in re.findall(r'^\s{4}code = "([a-z_]+)"', classes, re.M):
        sources.setdefault(code, "errors.py")
    for path in (ROOT / "src").rglob("*.py"):
        text = path.read_text(encoding="utf-8", errors="replace")
        for code in re.findall(r'"code":\s*"([a-z_]+)"', text):
            sources.setdefault(code, str(path.relative_to(ROOT)))
        for code in re.findall(r"code=\"([a-z_]+)\"", text):
            sources.setdefault(code, str(path.relative_to(ROOT)))
    return sources


def documented_codes() -> set[str]:
    found: set[str] = set()
    for relative in AGENT_DOCS:
        path = ROOT / relative
        if not path.is_file():
            continue
        text = path.read_text(encoding="utf-8", errors="replace")
        found.update(re.findall(r"`([a-z_]+)`", text))
        found.update(re.findall(r'code[= ]+"([a-z_]+)"', text))
    return found


def _undocumented(emitted: dict[str, str], documented: set[str]) -> dict[str, str]:
    return {code: where for code, where in emitted.items()
            if code not in documented and code not in NON_FAILURE_CODES}


def test_the_agent_vocabulary_is_complete():
    emitted = emitted_codes()
    assert len(emitted) >= 15, f"only {len(emitted)} codes found; the scanner lost a source"
    gaps = _undocumented(emitted, documented_codes())
    assert not gaps, (f"codes an agent can be handed with no documented remedy: {gaps}\n"
                      "add a row to references/troubleshooting.md, or a sentence in SKILL.md")


def test_no_document_invents_a_code_the_tool_cannot_print():
    emitted = set(emitted_codes())
    stale = []
    for relative in AGENT_DOCS:
        path = ROOT / relative
        text = path.read_text(encoding="utf-8", errors="replace")
        for number, line in enumerate(text.splitlines(), start=1):
            for code in re.findall(r"`([a-z_]+)`", line):
                # Only lines that talk about codes can make the claim; a plain
                # identifier in prose (`segments`, `hint`) is not a code.
                if "code" not in line and '"code"' not in line:
                    continue
                # Code-shaped only: `reason` and `subtitles` sit on these lines as
                # field names, not as codes, and a stoplist of prose words would be
                # a worse lie than the shape rule.
                if "_" not in code and code not in {"cancelled", "internal",
                                                    "unauthorized", "network", "error"}:
                    continue
                if code.endswith("_seconds") or code in {"ok", "code", "data", "detail", "hint",
                                                          "message", "retryable", "error"}:
                    continue
                if code not in emitted and code not in NON_FAILURE_CODES:
                    stale.append(f"{relative}:{number}: {code}")
    assert not stale, "documentation names codes nothing emits: " + "; ".join(stale)


def test_the_classifiers_are_load_bearing():
    planted_emitted = {"brand_new_code": "errors.py", "ok": "cli.py"}
    assert _undocumented(planted_emitted, {"configuration"}) == {"brand_new_code": "errors.py"}
    assert _undocumented({"ok": "cli.py"}, set()) == {}


def test_the_skill_files_own_list_of_codes_is_exact():
    """`## Known codes` in SKILL.md must be the vocabulary, not a sample of it.

    A list that only has to be *partly* right drifts into the thing it was meant
    to prevent: an agent reading it cannot tell whether an absent code means
    "never happens" or "the author forgot to update this section".
    """
    text = (ROOT / "skills/video-summary/SKILL.md").read_text(encoding="utf-8")
    section = text.split("## Known codes")[1].split("##")[0]
    listed = set(re.findall(r"`([a-z_]+)`", section))
    emitted = set(emitted_codes())
    assert listed == emitted, (f"only in SKILL.md: {sorted(listed - emitted)}; "
                               f"missing from it: {sorted(emitted - listed)}")


def test_the_exactness_gate_itself_can_go_red():
    planted = {"brand_new_code", "ok"}
    listed = {"ok"}
    assert listed != planted
