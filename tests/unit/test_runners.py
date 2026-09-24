import json
import shutil
import sys
from pathlib import Path

import pytest

from testloop.models import TestOutcome
from testloop.runners import make_runner
from testloop.runners.js_runner import JestRunner, VitestRunner, parse_js_json_report
from testloop.runners.pytest_runner import PytestRunner, junit_to_results
from testloop.runners.shell_runner import ShellRunner

FIXTURES = Path(__file__).resolve().parents[1] / "fixtures"
DATA = Path(__file__).resolve().parent / "data"


@pytest.fixture
def py_calc(tmp_path: Path) -> Path:
    dest = tmp_path / "py_calc"
    shutil.copytree(FIXTURES / "py_calc", dest)
    return dest


def test_pytest_runner_reports_per_test_outcomes(py_calc: Path) -> None:
    runner = PytestRunner(repo_path=py_calc, timeout_seconds=120)
    report = runner.run()
    ids = {r.test_id: r for r in report.results}
    assert ids["tests/test_calc.py::test_add"].outcome == TestOutcome.PASSED
    failed = ids["tests/test_calc.py::test_divide_true_division"]
    assert failed.outcome == TestOutcome.FAILED
    assert "3.5" in failed.message or "3.5" in failed.traceback
    assert report.exit_code != 0
    assert report.failing_ids == ["tests/test_calc.py::test_divide_true_division"]


def test_pytest_runner_runs_only_selected_tests(py_calc: Path) -> None:
    runner = PytestRunner(repo_path=py_calc, timeout_seconds=120)
    report = runner.run(["tests/test_calc.py::test_add"])
    assert [r.test_id for r in report.results] == ["tests/test_calc.py::test_add"]
    assert report.exit_code == 0


def test_pytest_runner_uses_custom_command(py_calc: Path) -> None:
    runner = PytestRunner(repo_path=py_calc, timeout_seconds=120, command=f"{sys.executable} -m pytest -p no:cacheprovider")
    report = runner.run(["tests/test_calc.py::test_add"])
    assert report.exit_code == 0


def test_pytest_runner_reports_timeout(py_calc: Path) -> None:
    (py_calc / "tests" / "test_slow.py").write_text("import time\n\ndef test_slow():\n    time.sleep(5)\n")
    runner = PytestRunner(repo_path=py_calc, timeout_seconds=1)
    report = runner.run(["tests/test_slow.py::test_slow"])
    assert report.timed_out
    assert report.exit_code != 0


def test_junit_nodeid_reconstruction_handles_classes() -> None:
    xml = """<?xml version="1.0"?><testsuites><testsuite>
    <testcase classname="tests.test_x.TestThing" name="test_a[1-2]" file="tests/test_x.py" line="3" time="0.1"/>
    <testcase classname="tests.test_x" name="test_b" file="tests/test_x.py" line="9" time="0.1">
      <failure message="assert 1 == 2">Traceback...</failure></testcase>
    <testcase classname="tests.test_x" name="test_c" file="tests/test_x.py" line="12" time="0.1"><skipped message="nope"/></testcase>
    </testsuite></testsuites>"""
    results = junit_to_results(xml)
    assert [r.test_id for r in results] == [
        "tests/test_x.py::TestThing::test_a[1-2]",
        "tests/test_x.py::test_b",
        "tests/test_x.py::test_c",
    ]
    assert results[1].outcome == TestOutcome.FAILED and results[1].message == "assert 1 == 2"
    assert results[2].outcome == TestOutcome.SKIPPED


def test_pytest_locates_test_source(py_calc: Path) -> None:
    runner = PytestRunner(repo_path=py_calc, timeout_seconds=120)
    file, name = runner.locate("tests/test_calc.py::TestX::test_divide_true_division[a]")
    assert file == "tests/test_calc.py" and name == "test_divide_true_division"


@pytest.mark.parametrize("filename", ["vitest_output.json", "jest_output.json"])
def test_js_json_report_parsing(filename: str) -> None:
    data = json.loads((DATA / filename).read_text())
    results = parse_js_json_report(data, repo_path=Path("/repo"))
    outcomes = {r.test_id: r.outcome for r in results}
    failed = [r for r in results if r.outcome == TestOutcome.FAILED][0]
    assert "3.5" in failed.message
    assert all("/repo" not in tid for tid in outcomes)
    if filename.startswith("vitest"):
        assert outcomes["calc.test.ts::calc divides with true division"] == TestOutcome.FAILED
        assert outcomes["calc.test.ts::calc skipped one"] == TestOutcome.SKIPPED
    else:
        assert outcomes["src/__tests__/calc.test.js::divides"] == TestOutcome.FAILED


def test_js_runner_builds_selection_args(tmp_path: Path) -> None:
    runner = VitestRunner(repo_path=tmp_path, timeout_seconds=10)
    args = runner.selection_args(["calc.test.ts::calc adds", "calc.test.ts::calc divides (x)", "other.test.ts::b"])
    assert "calc.test.ts" in args and "other.test.ts" in args
    pattern = args[args.index("-t") + 1]
    assert pattern.startswith("^(") and "divides \\(x\\)" in pattern
    assert JestRunner(repo_path=tmp_path, timeout_seconds=10).default_command.startswith("npx jest")


def test_js_runner_locates_source(tmp_path: Path) -> None:
    runner = VitestRunner(repo_path=tmp_path, timeout_seconds=10)
    assert runner.locate("src/a.test.ts::suite does thing") == ("src/a.test.ts", "suite does thing")


def test_shell_runner_single_synthetic_test(tmp_path: Path) -> None:
    runner = ShellRunner(repo_path=tmp_path, timeout_seconds=10, command=f"{sys.executable} -c \"print('hi'); import sys; sys.exit(3)\"")
    report = runner.run()
    assert len(report.results) == 1
    assert report.results[0].outcome == TestOutcome.FAILED
    assert report.exit_code == 3 and "hi" in report.stdout_tail
    ok = ShellRunner(repo_path=tmp_path, timeout_seconds=10, command=f"{sys.executable} -c 'pass'").run()
    assert ok.results[0].outcome == TestOutcome.PASSED


def test_make_runner_dispatch(tmp_path: Path) -> None:
    assert isinstance(make_runner("pytest", tmp_path, 5, None), PytestRunner)
    assert isinstance(make_runner("vitest", tmp_path, 5, None), VitestRunner)
    assert isinstance(make_runner("jest", tmp_path, 5, None), JestRunner)
    assert isinstance(make_runner("shell", tmp_path, 5, "make test"), ShellRunner)
    assert "tests/**" in make_runner("pytest", tmp_path, 5, None).test_file_globs
