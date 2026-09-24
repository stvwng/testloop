"""Generic fallback: any command, pass/fail from the exit code."""

from __future__ import annotations

from testloop.models import TestOutcome, TestReport, TestResult
from testloop.runners.base import TestRunner, run_command

SUITE_ID = "<suite>"


class ShellRunner(TestRunner):
    # Without knowledge of the runner we protect the conventional test locations.
    test_file_globs = ["tests/**", "test/**", "spec/**", "**/__tests__/**", "**/test_*.py", "**/*_test.py", "**/*.test.*", "**/*.spec.*"]
    config_file_globs = ["pytest.ini", "pyproject.toml", "setup.cfg", "tox.ini", "package.json", "jest.config.*", "vitest.config.*", "Makefile"]

    @property
    def default_command(self) -> str:
        raise ValueError("ShellRunner requires an explicit command")

    def run(self, selection: list[str] | None = None) -> TestReport:
        argv = self._base_argv()
        output = run_command(argv, self.repo_path, self.timeout_seconds)
        outcome = TestOutcome.PASSED if output.exit_code == 0 else TestOutcome.FAILED
        result = TestResult(
            test_id=SUITE_ID,
            outcome=outcome,
            message="" if outcome == TestOutcome.PASSED else f"command exited with {output.exit_code}",
            traceback=output.tail if outcome != TestOutcome.PASSED else "",
        )
        return self._report(output, [result], argv)
