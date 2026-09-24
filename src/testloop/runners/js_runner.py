"""vitest and jest adapters. Both emit the same JSON reporter shape."""

from __future__ import annotations

import json
import os
import re
import tempfile
from pathlib import Path
from typing import Any

from testloop.models import TestOutcome, TestReport, TestResult
from testloop.runners.base import TestRunner, run_command

_STATUS_MAP = {
    "passed": TestOutcome.PASSED,
    "failed": TestOutcome.FAILED,
    "pending": TestOutcome.SKIPPED,
    "skipped": TestOutcome.SKIPPED,
    "todo": TestOutcome.SKIPPED,
    "disabled": TestOutcome.SKIPPED,
}


def _relative(path: str, repo_path: Path) -> str:
    try:
        return os.path.relpath(path, repo_path)
    except ValueError:
        return path


def _escape_regex(text: str) -> str:
    # re.escape escapes spaces, which JS regexes in unicode mode reject; keep them literal.
    return re.escape(text).replace("\\ ", " ")


def parse_js_json_report(data: dict[str, Any], repo_path: Path) -> list[TestResult]:
    results: list[TestResult] = []
    for file_result in data.get("testResults", []):
        file = _relative(file_result.get("name", ""), repo_path)
        assertions = file_result.get("assertionResults", [])
        if not assertions and file_result.get("status") == "failed":
            # Whole file failed to load (syntax error, missing import).
            results.append(
                TestResult(
                    test_id=f"{file}::<file>",
                    outcome=TestOutcome.ERROR,
                    message=file_result.get("message", "") or "test file failed to run",
                    file=file,
                )
            )
            continue
        for assertion in assertions:
            full_name = assertion.get("fullName") or assertion.get("title", "")
            failures = assertion.get("failureMessages") or []
            results.append(
                TestResult(
                    test_id=f"{file}::{full_name}",
                    outcome=_STATUS_MAP.get(assertion.get("status", ""), TestOutcome.ERROR),
                    message=failures[0][:1000] if failures else "",
                    traceback="\n\n".join(failures),
                    file=file,
                    duration_s=(assertion.get("duration") or 0) / 1000.0,
                )
            )
    return results


class _JsRunner(TestRunner):
    test_file_globs = [
        "**/*.test.ts", "**/*.test.tsx", "**/*.test.js", "**/*.test.jsx", "**/*.test.mjs",
        "**/*.spec.ts", "**/*.spec.tsx", "**/*.spec.js", "**/*.spec.jsx",
        "**/__tests__/**", "**/__mocks__/**", "test/**", "tests/**",
    ]
    config_file_globs = [
        "vitest.config.*", "vite.config.*", "jest.config.*", "package.json", "tsconfig*.json", "babel.config.*", ".babelrc",
    ]
    output_flag: str

    def selection_args(self, selection: list[str]) -> list[str]:
        files: list[str] = []
        names: list[str] = []
        for test_id in selection:
            file, _, name = test_id.partition("::")
            if file not in files:
                files.append(file)
            if name and name != "<file>":
                names.append(_escape_regex(name))
        args = files
        if names:
            args += ["-t", "^(" + "|".join(names) + ")$"]
        return args

    def run(self, selection: list[str] | None = None) -> TestReport:
        with tempfile.TemporaryDirectory(prefix="testloop-js-") as tmp:
            out_path = Path(tmp) / "report.json"
            argv = self._base_argv() + [f"{self.output_flag}={out_path}"] + (self.selection_args(selection) if selection else [])
            output = run_command(argv, self.repo_path, self.timeout_seconds)
            results: list[TestResult] = []
            if out_path.exists():
                try:
                    results = parse_js_json_report(json.loads(out_path.read_text()), self.repo_path)
                except json.JSONDecodeError as exc:
                    output.stderr += f"\n[testloop] could not parse JSON report: {exc}"
        if not results and output.exit_code != 0:
            results = [
                TestResult(
                    test_id="<suite>",
                    outcome=TestOutcome.ERROR,
                    message="test runner produced no results (crash, missing deps or timeout)",
                    traceback=output.tail,
                )
            ]
        return self._report(output, results, argv)

    def locate(self, test_id: str) -> tuple[str | None, str | None]:
        file, _, full_name = test_id.partition("::")
        if not full_name or full_name == "<file>":
            return file or None, None
        # fullName is "describe title ... it title". Only a suffix appears verbatim in source;
        # callers that need the source line try progressively shorter suffixes.
        return file, full_name


class VitestRunner(_JsRunner):
    output_flag = "--outputFile"

    @property
    def default_command(self) -> str:
        return "npx vitest run --reporter=json"


class JestRunner(_JsRunner):
    output_flag = "--outputFile"

    @property
    def default_command(self) -> str:
        return "npx jest --json --ci"
