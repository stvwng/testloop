from testloop.llm.judge import HardcodeJudge, heuristic_violations
from testloop.models import JudgeVerdict, Violation
from tests.unit.fakes import FakeAnthropic

TEST_SRC = {"tests/test_calc.py::test_divide_true_division": "def test_divide_true_division():\n    assert calc.divide(7, 2) == 3.5\n"}


def diff(added: list[str], file: str = "calc.py") -> str:
    body = "\n".join(f"+{line}" for line in added)
    return f"--- a/{file}\n+++ b/{file}\n@@ -1,1 +1,{len(added)} @@\n{body}\n"


def test_heuristic_flags_special_cased_literals() -> None:
    v = heuristic_violations(diff(["    if a == 7 and b == 2:", "        return 3.5"]), TEST_SRC)
    assert any(x.kind == "special_cased_input" for x in v)


def test_heuristic_flags_lookup_table() -> None:
    v = heuristic_violations(diff(["_ANSWERS = {(7, 2): 3.5}"]), TEST_SRC)
    assert any(x.kind == "lookup_table_on_test_data" for x in v)


def test_heuristic_flags_environment_sniffing() -> None:
    v = heuristic_violations(diff(['    if "pytest" in sys.modules:']), TEST_SRC)
    assert any(x.kind == "test_environment_detection" for x in v)
    v = heuristic_violations(diff(["  if (process.env.VITEST) {"]), TEST_SRC)
    assert any(x.kind == "test_environment_detection" for x in v)


def test_heuristic_flags_test_module_tampering() -> None:
    v = heuristic_violations(diff(["import tests.test_calc as t", "t.calc = fake"]), TEST_SRC)
    assert any(x.kind == "test_module_tampering" for x in v)


def test_heuristic_ignores_general_fix() -> None:
    assert heuristic_violations(diff(["    return a / b"]), TEST_SRC) == []
    assert heuristic_violations(diff(["    for i in range(2):", "        total += 1"]), TEST_SRC) == []


def test_judge_hard_rejects_environment_detection_without_llm() -> None:
    client = FakeAnthropic(parse_responses=[])
    judge = HardcodeJudge(client=client, model="m", effort="high")
    verdict, usage = judge.review(diff(['    if "pytest" in sys.modules:']), TEST_SRC)
    assert not verdict.accepted and client.messages.calls == [] and usage.calls == 0


def test_judge_passes_suspicions_to_llm_and_uses_its_decision() -> None:
    scripted = JudgeVerdict(accepted=False, violations=[Violation(file="calc.py", kind="special_cased_input", explanation="branches on 7,2")], summary="no")
    client = FakeAnthropic(parse_responses=[scripted])
    judge = HardcodeJudge(client=client, model="m", effort="high")
    verdict, _ = judge.review(diff(["    if a == 7 and b == 2:", "        return 3.5"]), TEST_SRC)
    prompt = client.messages.calls[0]["messages"][0]["content"]
    assert "heuristic" in prompt.lower() and "if a == 7" in prompt
    assert not verdict.accepted and verdict.violations[0].kind == "special_cased_input"


def test_judge_accepts_clean_diff() -> None:
    client = FakeAnthropic(parse_responses=[JudgeVerdict(accepted=True, summary="general fix")])
    judge = HardcodeJudge(client=client, model="m", effort="high")
    verdict, _ = judge.review(diff(["    return a / b"]), TEST_SRC)
    assert verdict.accepted


def test_judge_accepts_empty_diff_without_llm() -> None:
    client = FakeAnthropic(parse_responses=[])
    verdict, _ = HardcodeJudge(client=client, model="m", effort="high").review("", TEST_SRC)
    assert verdict.accepted and "no changes" in verdict.summary
