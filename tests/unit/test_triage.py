import shutil
from pathlib import Path

from testloop.llm.triage import TestTriager
from testloop.models import TestOutcome, TestResult, TriageBatch, TriageVerdict
from testloop.runners.pytest_runner import PytestRunner
from tests.unit.fakes import FakeAnthropic

FIXTURES = Path(__file__).resolve().parents[1] / "fixtures"


def test_triage_builds_prompt_with_source_and_returns_verdicts(tmp_path: Path) -> None:
    repo = tmp_path / "py_calc"
    shutil.copytree(FIXTURES / "py_calc", repo)
    failing = [
        TestResult(test_id="tests/test_calc.py::test_divide_true_division", outcome=TestOutcome.FAILED, message="assert 3 == 3.5"),
        TestResult(test_id="tests/test_calc.py::test_order_dependent_on_previous_test", outcome=TestOutcome.FAILED, message="assert [] == ['hello-world']"),
    ]
    scripted = TriageBatch(
        verdicts=[
            TriageVerdict(test_id=failing[0].test_id, usable=True, flags=[], explanation="pure function"),
            TriageVerdict(test_id=failing[1].test_id, usable=False, flags=["order_dependent"], explanation="reads _seen"),
        ]
    )
    client = FakeAnthropic(parse_responses=[scripted])
    triager = TestTriager(client=client, model="claude-opus-5", effort="high", repo_path=repo, runner=PytestRunner(repo, 60))
    verdicts, usage = triager.triage(failing)

    call = client.messages.calls[0]
    assert call["output_format"] is TriageBatch
    assert call["model"] == "claude-opus-5" and call["output_config"] == {"effort": "high"}
    prompt = call["messages"][0]["content"]
    assert "def test_order_dependent_on_previous_test" in prompt and "assert 3 == 3.5" in prompt
    assert [v.usable for v in verdicts] == [True, False]
    assert usage.input_tokens == 100 and usage.calls == 1


def test_triage_fills_in_missing_verdicts_as_usable(tmp_path: Path) -> None:
    client = FakeAnthropic(parse_responses=[TriageBatch(verdicts=[])])
    triager = TestTriager(client=client, model="m", effort="high", repo_path=tmp_path, runner=PytestRunner(tmp_path, 60))
    verdicts, _ = triager.triage([TestResult(test_id="a::b", outcome=TestOutcome.FAILED)])
    assert verdicts[0].usable and "no verdict" in verdicts[0].explanation


def test_triage_batches_large_inputs(tmp_path: Path) -> None:
    results = [TestResult(test_id=f"t::{i}", outcome=TestOutcome.FAILED) for i in range(25)]
    responses = [lambda kw: TriageBatch(verdicts=[]) for _ in range(3)]
    client = FakeAnthropic(parse_responses=responses)
    triager = TestTriager(client=client, model="m", effort="high", repo_path=tmp_path, runner=PytestRunner(tmp_path, 60), batch_size=10)
    verdicts, usage = triager.triage(results)
    assert len(verdicts) == 25 and usage.calls == 3
