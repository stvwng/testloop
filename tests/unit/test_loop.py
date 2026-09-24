from __future__ import annotations

from pathlib import Path

import pytest

from testloop.config import LoopConfig
from testloop.llm.agent import AgentAttempt
from testloop.loop import EngineeringLoop
from testloop.models import JudgeVerdict, LoopStatus, TestOutcome, TestReport, TestResult, TriageVerdict, Usage, Violation
from testloop.runners.base import TestRunner

T_BUG = "tests/test_calc.py::test_divide"
T_OK = "tests/test_calc.py::test_add"
T_ORDER = "tests/test_calc.py::test_order"


def report(failing: list[str], passing: list[str]) -> TestReport:
    results = [TestResult(test_id=t, outcome=TestOutcome.FAILED, message=f"{t} failed") for t in failing]
    results += [TestResult(test_id=t, outcome=TestOutcome.PASSED) for t in passing]
    return TestReport(results=results, exit_code=1 if failing else 0, stdout_tail="", command="fake")


class FakeRunner(TestRunner):
    test_file_globs = ["tests/**"]
    config_file_globs = ["pytest.ini"]

    def __init__(self, repo_path: Path, reports: list[TestReport]) -> None:
        super().__init__(repo_path, 10, command="fake")
        self.reports = list(reports)
        self.calls: list[list[str] | None] = []

    @property
    def default_command(self) -> str:
        return "fake"

    def run(self, selection=None) -> TestReport:
        self.calls.append(selection)
        if len(self.reports) > 1:
            return self.reports.pop(0)
        return self.reports[0]

    def locate(self, test_id):
        return test_id.split("::")[0], test_id.split("::")[-1]


class FakeTriager:
    def __init__(self, flagged: set[str]) -> None:
        self.flagged = flagged
        self.calls: list[list[str]] = []

    def triage(self, failing):
        self.calls.append([f.test_id for f in failing])
        return [
            TriageVerdict(test_id=f.test_id, usable=f.test_id not in self.flagged, flags=["order_dependent"] if f.test_id in self.flagged else [], explanation="x")
            for f in failing
        ], Usage(input_tokens=10, output_tokens=5)


class FakeJudge:
    def __init__(self, verdicts: list[JudgeVerdict] | None = None) -> None:
        self.verdicts = list(verdicts or [])
        self.calls: list[str] = []

    def review(self, diff, sources):
        self.calls.append(diff)
        if self.verdicts:
            return self.verdicts.pop(0), Usage(input_tokens=10, output_tokens=5)
        return JudgeVerdict(accepted=True, summary="ok"), Usage(input_tokens=10, output_tokens=5)


class FakeAgent:
    def __init__(self, workspace, edits: list[dict[str, str] | None]) -> None:
        self.workspace = workspace
        self.edits = list(edits)
        self.calls: list[tuple[list[str], int]] = []

    def attempt(self, targets, sources, history, iteration):
        self.calls.append(([t.test_id for t in targets], iteration))
        edit = self.edits.pop(0) if self.edits else None
        if edit is None:
            return AgentAttempt(summary="did nothing", usage=Usage(input_tokens=1000, output_tokens=500))
        if "error" in edit:
            return AgentAttempt(error=edit["error"], refused=edit.get("refused") == "1", usage=Usage(input_tokens=10, output_tokens=1))
        for path, content in edit.items():
            self.workspace.write_text(path, content)
        return AgentAttempt(summary=f"edited {list(edit)}", usage=Usage(input_tokens=1000, output_tokens=500), tool_calls=2)


@pytest.fixture
def repo(tmp_path: Path) -> Path:
    (tmp_path / "tests").mkdir()
    (tmp_path / "tests" / "test_calc.py").write_text("def test_divide(): pass\ndef test_add(): pass\ndef test_order(): pass\n")
    (tmp_path / "calc.py").write_text("def divide(a, b): return a // b\n")
    return tmp_path


def build(repo: Path, reports, edits, flagged=frozenset(), verdicts=None, **cfg):
    config = LoopConfig(repo_path=repo, runner="pytest", run_dir=repo / ".testloop" / "run", **cfg)
    runner = FakeRunner(repo, reports)
    loop = EngineeringLoop(config, client=object(), runner=runner)
    loop.triager = FakeTriager(set(flagged))
    loop.judge = FakeJudge(verdicts)
    loop.agent = FakeAgent(loop.workspace, edits)
    return loop


def test_nothing_to_fix_when_baseline_green(repo: Path) -> None:
    loop = build(repo, [report([], [T_OK])], edits=[])
    result = loop.run()
    assert result.status == LoopStatus.NOTHING_TO_FIX and result.exit_code == 0
    assert loop.agent.calls == []


def test_passes_after_one_iteration_and_writes_artifacts(repo: Path) -> None:
    loop = build(repo, [report([T_BUG], [T_OK]), report([], [T_OK, T_BUG])], edits=[{"calc.py": "def divide(a, b): return a / b\n"}])
    result = loop.run()
    assert result.status == LoopStatus.PASSED and result.exit_code == 0
    assert result.targets == [T_BUG] and len(result.iterations) == 1
    assert "+def divide(a, b): return a / b" in result.final_diff
    assert loop.triager.calls == [[T_BUG]]
    run_dir = Path(result.run_dir)
    assert (run_dir / "result.json").exists() and (run_dir / "final.patch").read_text() == result.final_diff
    assert (run_dir / "iteration-1" / "transcript.json").exists()
    assert result.usage.calls >= 3 and result.estimated_cost_usd > 0


def test_flagged_tests_are_excluded_and_reported(repo: Path) -> None:
    loop = build(
        repo,
        [report([T_BUG, T_ORDER], [T_OK]), report([T_ORDER], [T_OK, T_BUG])],
        edits=[{"calc.py": "fixed\n"}],
        flagged={T_ORDER},
    )
    result = loop.run()
    assert result.status == LoopStatus.PASSED
    assert [f.test_id for f in result.flagged] == [T_ORDER]
    assert loop.agent.calls[0][0] == [T_BUG]
    review = (Path(result.run_dir) / "testloop-review.md").read_text()
    assert T_ORDER in review and "order_dependent" in review


def test_all_failing_flagged_means_nothing_to_fix(repo: Path) -> None:
    loop = build(repo, [report([T_ORDER], [T_OK])], edits=[], flagged={T_ORDER})
    result = loop.run()
    assert result.status == LoopStatus.NOTHING_TO_FIX and "flagged" in result.reason
    assert loop.agent.calls == []


def test_quarantined_and_accepted_decisions(repo: Path) -> None:
    (repo / ".testloop.yml").write_text(f'tests:\n  "{T_ORDER}":\n    decision: quarantine\n  "{T_BUG}":\n    decision: accept\n')
    loop = build(repo, [report([T_BUG, T_ORDER], [T_OK]), report([T_ORDER], [T_OK, T_BUG])], edits=[{"calc.py": "fixed\n"}])
    result = loop.run()
    assert result.status == LoopStatus.PASSED
    assert loop.triager.calls == []  # accepted test skips triage, quarantined one never reaches it
    assert loop.agent.calls[0][0] == [T_BUG]


def test_judge_rejection_reverts_and_counts_iteration(repo: Path) -> None:
    rejection = JudgeVerdict(accepted=False, violations=[Violation(file="calc.py", kind="special_cased_input", explanation="branch on 7")], summary="cheat")
    loop = build(
        repo,
        [report([T_BUG], [T_OK]), report([], [T_OK, T_BUG])],
        edits=[{"calc.py": "if a == 7: return 3.5\n"}, {"calc.py": "def divide(a, b): return a / b\n"}],
        verdicts=[rejection, JudgeVerdict(accepted=True, summary="ok")],
    )
    result = loop.run()
    assert result.status == LoopStatus.PASSED and len(result.iterations) == 2
    assert result.iterations[0].reverted and result.iterations[0].judge.summary == "cheat"
    assert "3.5" not in result.final_diff
    assert loop.runner.calls == [None, None]  # baseline + one full-suite run; rejected iteration ran no tests


def test_max_iterations_reached_keeps_partial_progress(repo: Path) -> None:
    loop = build(
        repo,
        [report([T_BUG], [T_OK])],  # never passes
        edits=[{"calc.py": "v1\n"}, {"calc.py": "v2\n"}, {"calc.py": "v3\n"}],
        max_iterations=3,
    )
    result = loop.run()
    assert result.status == LoopStatus.MAX_ITERATIONS_REACHED and result.exit_code == 2
    assert len(result.iterations) == 3 and "+v3" in result.final_diff
    assert (repo / "calc.py").read_text() == "v3\n"


def test_regression_is_fed_into_next_iteration(repo: Path) -> None:
    loop = build(
        repo,
        [report([T_BUG], [T_OK]), report([T_OK], [T_BUG]), report([], [T_OK, T_BUG])],
        edits=[{"calc.py": "v1\n"}, {"calc.py": "v2\n"}],
    )
    result = loop.run()
    assert result.status == LoopStatus.PASSED
    assert result.iterations[0].regressions == [T_OK]
    assert loop.agent.calls[1][0] == [T_OK]


def test_flaky_baseline_tests_are_excluded(repo: Path) -> None:
    loop = build(
        repo,
        [report([T_BUG, T_ORDER], [T_OK]), report([T_BUG], [T_OK, T_ORDER]), report([], [T_OK, T_BUG, T_ORDER])],
        edits=[{"calc.py": "fixed\n"}],
        baseline_runs=2,
    )
    result = loop.run()
    assert result.status == LoopStatus.PASSED and result.flaky == [T_ORDER]
    assert loop.agent.calls[0][0] == [T_BUG]


def test_cost_budget_stops_loop(repo: Path) -> None:
    loop = build(repo, [report([T_BUG], [T_OK])], edits=[{"calc.py": "v1\n"}, {"calc.py": "v2\n"}], max_cost_usd=0.01)
    result = loop.run()
    assert result.status == LoopStatus.BUDGET_EXHAUSTED and result.exit_code == 3
    assert len(result.iterations) == 1


def test_time_budget_stops_loop(repo: Path) -> None:
    loop = build(repo, [report([T_BUG], [T_OK])], edits=[{"calc.py": "v1\n"}, {"calc.py": "v2\n"}], time_budget_seconds=100)
    ticks = iter([0, 0, 200, 200, 200, 200])
    loop.clock = lambda: next(ticks)
    result = loop.run()
    assert result.status == LoopStatus.BUDGET_EXHAUSTED and len(result.iterations) == 1


def test_refusal_aborts(repo: Path) -> None:
    loop = build(repo, [report([T_BUG], [T_OK])], edits=[{"error": "model refused (cyber)", "refused": "1"}])
    result = loop.run()
    assert result.status == LoopStatus.ABORTED and result.exit_code == 4 and "refused" in result.reason


def test_runner_crash_aborts(repo: Path) -> None:
    crash = TestReport(results=[TestResult(test_id="<suite>", outcome=TestOutcome.ERROR, message="no results")], exit_code=2, stdout_tail="boom", command="fake")
    loop = build(repo, [crash], edits=[])
    result = loop.run()
    assert result.status == LoopStatus.ABORTED and "no results" in result.reason


def test_protected_file_tamper_aborts(repo: Path) -> None:
    class TamperAgent(FakeAgent):
        def attempt(self, targets, sources, history, iteration):
            (repo / "tests" / "test_calc.py").write_text("def test_divide(): pass\n")
            return AgentAttempt(summary="cheated")

    loop = build(repo, [report([T_BUG], [T_OK])], edits=[])
    loop.agent = TamperAgent(loop.workspace, [])
    result = loop.run()
    assert result.status == LoopStatus.ABORTED and "tests/test_calc.py" in result.reason
