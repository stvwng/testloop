import shutil
from pathlib import Path

import pytest

from testloop.guard import WriteGuard
from testloop.llm.agent import FixAgent, build_tools, format_report
from testloop.models import TestOutcome, TestReport, TestResult
from testloop.runners.pytest_runner import PytestRunner
from testloop.workspace import Workspace
from tests.unit.fakes import FakeAnthropic, scripted

FIXTURES = Path(__file__).resolve().parents[1] / "fixtures"


@pytest.fixture
def repo(tmp_path: Path) -> Path:
    dest = tmp_path / "py_calc"
    shutil.copytree(FIXTURES / "py_calc", dest)
    return dest


def make_parts(repo: Path):
    runner = PytestRunner(repo, 120)
    ws = Workspace(repo, protected_globs=runner.test_file_globs)
    guard = WriteGuard(ws, runner.test_file_globs, runner.config_file_globs, [])
    return runner, ws, guard


def tool(tools, name):
    return next(t for t in tools if t.name == name)


def test_tools_read_list_grep(repo: Path) -> None:
    runner, ws, guard = make_parts(repo)
    tools = build_tools(ws, guard, runner)
    assert {t.name for t in tools} == {"list_files", "read_file", "grep", "edit_file", "write_file", "run_tests"}
    assert "calc.py" in tool(tools, "list_files").call({"glob": "**/*.py"})
    assert "def divide" in tool(tools, "read_file").call({"path": "calc.py"})
    assert "calc.py:" in tool(tools, "grep").call({"pattern": "a // b", "glob": "**/*.py"})


def test_edit_file_requires_unique_match_and_writes(repo: Path) -> None:
    runner, ws, guard = make_parts(repo)
    ws.begin_iteration()
    tools = build_tools(ws, guard, runner)
    edit = tool(tools, "edit_file")
    with pytest.raises(Exception, match="not found"):
        edit.call({"path": "calc.py", "old_string": "nope", "new_string": "x"})
    with pytest.raises(Exception, match="occurs 3 times"):
        edit.call({"path": "calc.py", "old_string": "return", "new_string": "x"})
    out = edit.call({"path": "calc.py", "old_string": "return a // b", "new_string": "return a / b"})
    assert "edited" in out and "return a / b" in ws.read_text("calc.py")
    assert ws.changed_files() == ["calc.py"]


def test_write_tools_are_guarded(repo: Path) -> None:
    runner, ws, guard = make_parts(repo)
    ws.begin_iteration()
    tools = build_tools(ws, guard, runner)
    with pytest.raises(Exception, match="test file"):
        tool(tools, "write_file").call({"path": "tests/test_calc.py", "content": ""})
    with pytest.raises(Exception, match="configures test"):
        tool(tools, "edit_file").call({"path": "pyproject.toml", "old_string": "a", "new_string": "b"})
    with pytest.raises(Exception, match="outside"):
        tool(tools, "write_file").call({"path": "../x.py", "content": ""})
    assert ws.changed_files() == []


def test_run_tests_tool_uses_selection(repo: Path) -> None:
    runner, ws, guard = make_parts(repo)
    tools = build_tools(ws, guard, runner)
    out = tool(tools, "run_tests").call({"test_ids": ["tests/test_calc.py::test_add"]})
    assert "PASSED tests/test_calc.py::test_add" in out and "test_divide" not in out


def test_format_report_includes_failures_and_tail() -> None:
    report = TestReport(
        results=[TestResult(test_id="a", outcome=TestOutcome.FAILED, message="boom", traceback="tb")],
        exit_code=1,
        stdout_tail="raw out",
        command="pytest",
    )
    text = format_report(report)
    assert "FAILED a" in text and "boom" in text and "tb" in text and "1 failed" in text


def test_agent_runs_session_and_reports(repo: Path) -> None:
    runner, ws, guard = make_parts(repo)
    ws.begin_iteration()
    client = FakeAnthropic(
        agent_scripts=[
            scripted(
                [
                    ("read_file", {"path": "calc.py"}),
                    ("edit_file", {"path": "calc.py", "old_string": "return a // b", "new_string": "return a / b"}),
                    ("write_file", {"path": "tests/test_calc.py", "content": "cheat"}),
                ],
                final_text="Replaced floor division with true division.",
            )
        ]
    )
    agent = FixAgent(client=client, model="claude-opus-5", effort="xhigh", workspace=ws, guard=guard, runner=runner, max_tool_calls=60)
    targets = [TestResult(test_id="tests/test_calc.py::test_divide_true_division", outcome=TestOutcome.FAILED, message="assert 3 == 3.5")]
    attempt = agent.attempt(targets, {targets[0].test_id: "def test_divide_true_division(): ..."}, history=[], iteration=1)

    call = client.beta.messages.calls[0]
    assert call["model"] == "claude-opus-5" and call["max_iterations"] == 60
    assert call["output_config"] == {"effort": "xhigh"} and call["fallbacks"] == "default"
    assert "test_divide_true_division" in call["messages"][0]["content"]
    assert attempt.summary.startswith("Replaced") and attempt.tool_calls == 3 and not attempt.refused
    assert attempt.usage.calls == 4
    fake_runner = client.beta.messages.runners[0]
    assert fake_runner.results[2][2] is True  # the test-file write was rejected
    assert "return a / b" in ws.read_text("calc.py")


def test_agent_reports_refusal(repo: Path) -> None:
    runner, ws, guard = make_parts(repo)
    client = FakeAnthropic(agent_scripts=[scripted([], final_text="", stop_reason="refusal")])
    agent = FixAgent(client=client, model="m", effort="high", workspace=ws, guard=guard, runner=runner, max_tool_calls=5, use_fallbacks=False)
    attempt = agent.attempt([], {}, history=[], iteration=1)
    assert attempt.refused and "fallbacks" not in client.beta.messages.calls[0]


def test_agent_prompt_includes_history(repo: Path) -> None:
    from testloop.models import IterationRecord, JudgeVerdict, Violation

    runner, ws, guard = make_parts(repo)
    client = FakeAnthropic(agent_scripts=[scripted([], final_text="ok")])
    agent = FixAgent(client=client, model="m", effort="high", workspace=ws, guard=guard, runner=runner, max_tool_calls=5)
    history = [
        IterationRecord(
            number=1,
            agent_summary="tried X",
            reverted=True,
            judge=JudgeVerdict(accepted=False, violations=[Violation(file="calc.py", kind="special_cased_input", explanation="branched on 7")], summary="cheat"),
        )
    ]
    agent.attempt([], {}, history=history, iteration=2)
    prompt = client.beta.messages.calls[0]["messages"][0]["content"]
    assert "Iteration 1" in prompt and "reverted" in prompt and "branched on 7" in prompt
