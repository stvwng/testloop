"""pytest adapter. Uses JUnit XML (xunit1 family) so each test carries its file path."""

from __future__ import annotations

import re
import sys
import tempfile
import xml.etree.ElementTree as ET
from pathlib import Path

from testloop.models import TestOutcome, TestReport, TestResult
from testloop.runners.base import TestRunner, run_command

_PARAM_SUFFIX = re.compile(r"\[.*\]$")


def _nodeid(classname: str, name: str, file: str | None) -> str:
    if not file:
        return f"{classname}::{name}"
    module_dotted = file[:-3].replace("/", ".").replace("\\", ".") if file.endswith(".py") else file
    class_chain = ""
    if classname.startswith(module_dotted):
        remainder = classname[len(module_dotted) :].lstrip(".")
        class_chain = "::".join(remainder.split(".")) if remainder else ""
    else:
        # Module path did not match (e.g. rootdir differs); keep the last segments that are not the module.
        class_chain = "::".join(p for p in classname.split(".") if p[:1].isupper())
    parts = [file] + ([class_chain] if class_chain else []) + [name]
    return "::".join(parts)


def junit_to_results(xml_text: str) -> list[TestResult]:
    root = ET.fromstring(xml_text)
    results: list[TestResult] = []
    for case in root.iter("testcase"):
        classname = case.get("classname", "")
        name = case.get("name", "")
        file = case.get("file")
        test_id = _nodeid(classname, name, file)
        duration = float(case.get("time", "0") or 0)
        outcome, message, traceback = TestOutcome.PASSED, "", ""
        for child in case:
            if child.tag == "failure":
                outcome, message, traceback = TestOutcome.FAILED, child.get("message", ""), child.text or ""
            elif child.tag == "error":
                outcome, message, traceback = TestOutcome.ERROR, child.get("message", ""), child.text or ""
            elif child.tag == "skipped":
                outcome, message = TestOutcome.SKIPPED, child.get("message", "")
        results.append(
            TestResult(test_id=test_id, outcome=outcome, message=message, traceback=traceback, file=file, duration_s=duration)
        )
    return results


class PytestRunner(TestRunner):
    test_file_globs = ["tests/**", "test/**", "**/test_*.py", "**/*_test.py", "**/conftest.py"]
    config_file_globs = ["pytest.ini", "pyproject.toml", "setup.cfg", "tox.ini", "setup.py"]

    @property
    def default_command(self) -> str:
        return f"{sys.executable} -m pytest -p no:cacheprovider"

    def run(self, selection: list[str] | None = None) -> TestReport:
        with tempfile.TemporaryDirectory(prefix="testloop-junit-") as tmp:
            junit_path = Path(tmp) / "junit.xml"
            argv = self._base_argv() + [
                f"--junitxml={junit_path}",
                "-o",
                "junit_family=xunit1",
                "-q",
            ] + list(selection or [])
            output = run_command(argv, self.repo_path, self.timeout_seconds)
            results = junit_to_results(junit_path.read_text()) if junit_path.exists() else []
        if not results and output.exit_code != 0:
            # Collection error, crash or timeout: surface it as a single error so callers see something.
            results = [
                TestResult(
                    test_id=selection[0] if selection and len(selection) == 1 else "<suite>",
                    outcome=TestOutcome.ERROR,
                    message="pytest produced no results (collection error, crash or timeout)",
                    traceback=output.tail,
                )
            ]
        return self._report(output, results, argv)

    def locate(self, test_id: str) -> tuple[str | None, str | None]:
        parts = test_id.split("::")
        if len(parts) < 2:
            return None, None
        return parts[0], _PARAM_SUFFIX.sub("", parts[-1])
