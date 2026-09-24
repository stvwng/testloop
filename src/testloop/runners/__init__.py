from __future__ import annotations

from pathlib import Path

from testloop.runners.base import TestRunner
from testloop.runners.js_runner import JestRunner, VitestRunner
from testloop.runners.pytest_runner import PytestRunner
from testloop.runners.shell_runner import ShellRunner

__all__ = ["JestRunner", "PytestRunner", "ShellRunner", "TestRunner", "VitestRunner", "make_runner"]


def make_runner(kind: str, repo_path: Path, timeout_seconds: int, command: str | None) -> TestRunner:
    classes: dict[str, type[TestRunner]] = {
        "pytest": PytestRunner,
        "vitest": VitestRunner,
        "jest": JestRunner,
        "shell": ShellRunner,
    }
    try:
        cls = classes[kind]
    except KeyError as exc:
        raise ValueError(f"unknown runner '{kind}'; expected one of {sorted(classes)}") from exc
    return cls(repo_path=repo_path, timeout_seconds=timeout_seconds, command=command)
