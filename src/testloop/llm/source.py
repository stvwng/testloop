"""Pull the source of a single test out of a test file, for triage and judge prompts."""

from __future__ import annotations

import re

_PY_DEF = re.compile(r"^(\s*)(?:async\s+)?def\s+(\w+)\s*\(")
_PY_BLOCK_START = re.compile(r"^(\s*)(?:async\s+def|def|class)\s")
_JS_CALL = re.compile(r"""^\s*(?:it|test|specify)(?:\.\w+)?\s*\(\s*(['"`])(.+?)\1""")


def extract_test_source(text: str, name: str, lang: str) -> str:
    """Return the test's source or '' when it cannot be located.

    `name` is the pytest function name or, for JS, the reporter's fullName ("describe ... it title");
    JS matching tries progressively shorter suffixes because only the it-title is verbatim in source.
    """
    if lang == "py":
        return _extract_python(text, name)
    return _extract_js(text, name)


def _extract_python(text: str, name: str) -> str:
    lines = text.splitlines()
    for idx, line in enumerate(lines):
        m = _PY_DEF.match(line)
        if not m or m.group(2) != name:
            continue
        indent = len(m.group(1))
        start = idx
        while start > 0 and lines[start - 1].strip().startswith("@") and _leading(lines[start - 1]) == indent:
            start -= 1
        end = idx + 1
        while end < len(lines):
            stripped = lines[end].strip()
            if stripped and _leading(lines[end]) <= indent and (_PY_BLOCK_START.match(lines[end]) or _leading(lines[end]) < indent or stripped.startswith("@")):
                break
            end += 1
        return "\n".join(lines[start:end]).rstrip() + "\n"
    return ""


def _leading(line: str) -> int:
    return len(line) - len(line.lstrip())


def _extract_js(text: str, full_name: str) -> str:
    words = full_name.split(" ")
    lines = text.splitlines()
    # Longest suffix first so "calc divides" does not match an it("divides ...") ambiguously.
    for cut in range(len(words)):
        title = " ".join(words[cut:])
        for idx, line in enumerate(lines):
            m = _JS_CALL.match(line)
            if m and m.group(2) == title:
                return _take_balanced(lines, idx)
    return ""


def _take_balanced(lines: list[str], start: int) -> str:
    depth = 0
    opened = False
    out: list[str] = []
    for line in lines[start:]:
        out.append(line)
        for ch in line:
            if ch == "(":
                depth += 1
                opened = True
            elif ch == ")":
                depth -= 1
        if opened and depth <= 0:
            break
    return "\n".join(l.strip() if i == 0 else l for i, l in enumerate(out)).rstrip() + "\n"
