"""Shared subprocess plumbing and the runner interface."""

from __future__ import annotations

import logging
import shlex
import subprocess
from abc import ABC, abstractmethod
from dataclasses import dataclass
from pathlib import Path

from testloop.models import TestReport, TestResult

log = logging.getLogger(__name__)

STDOUT_TAIL_CHARS = 8000


@dataclass
class CommandOutput:
    exit_code: int
    stdout: str
    stderr: str
    timed_out: bool

    @property
    def tail(self) -> str:
        combined = self.stdout + ("\n--- stderr ---\n" + self.stderr if self.stderr.strip() else "")
        return combined[-STDOUT_TAIL_CHARS:]


def run_command(argv: list[str], cwd: Path, timeout_seconds: int) -> CommandOutput:
    log.debug("[DEBUG] run_command cwd=%s argv=%s timeout=%ss", cwd, argv, timeout_seconds)
    try:
        proc = subprocess.run(
            argv,
            cwd=cwd,
            capture_output=True,
            text=True,
            timeout=timeout_seconds,
            check=False,
        )
    except subprocess.TimeoutExpired as exc:
        out = exc.stdout.decode() if isinstance(exc.stdout, bytes) else (exc.stdout or "")
        err = exc.stderr.decode() if isinstance(exc.stderr, bytes) else (exc.stderr or "")
        log.warning("test command timed out after %ss: %s", timeout_seconds, argv)
        return CommandOutput(exit_code=124, stdout=out, stderr=err + f"\n[testloop] timed out after {timeout_seconds}s", timed_out=True)
    except FileNotFoundError as exc:
        log.error("test command not found: %s (%s)", argv[0], exc)
        return CommandOutput(exit_code=127, stdout="", stderr=f"[testloop] command not found: {argv[0]}: {exc}", timed_out=False)
    return CommandOutput(exit_code=proc.returncode, stdout=proc.stdout, stderr=proc.stderr, timed_out=False)


class TestRunner(ABC):
    """Runs a repo's test suite and reports per-test outcomes.

    `test_file_globs` tells the write guard which files are tests and must never be edited.
    """

    test_file_globs: list[str] = []
    config_file_globs: list[str] = []

    def __init__(self, repo_path: Path, timeout_seconds: int, command: str | None = None) -> None:
        self.repo_path = Path(repo_path)
        self.timeout_seconds = timeout_seconds
        self.command = command or self.default_command

    @property
    @abstractmethod
    def default_command(self) -> str: ...

    @abstractmethod
    def run(self, selection: list[str] | None = None) -> TestReport:
        """Run the whole suite, or only the given test ids."""

    def locate(self, test_id: str) -> tuple[str | None, str | None]:
        """Best-effort (file, test name) for a test id, used to pull test source for triage."""
        return None, None

    def _base_argv(self) -> list[str]:
        return shlex.split(self.command)

    def _report(self, output: CommandOutput, results: list[TestResult], argv: list[str]) -> TestReport:
        return TestReport(
            results=results,
            exit_code=output.exit_code,
            stdout_tail=output.tail,
            command=shlex.join(argv),
            timed_out=output.timed_out,
        )
