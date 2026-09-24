from testloop.models import TestOutcome, TestReport, TestResult, Usage


def make_report(results: list[TestResult]) -> TestReport:
    return TestReport(results=results, exit_code=1, stdout_tail="", command="pytest")


def test_report_partitions_by_outcome() -> None:
    report = make_report(
        [
            TestResult(test_id="a", outcome=TestOutcome.PASSED),
            TestResult(test_id="b", outcome=TestOutcome.FAILED, message="boom"),
            TestResult(test_id="c", outcome=TestOutcome.ERROR, message="err"),
            TestResult(test_id="d", outcome=TestOutcome.SKIPPED),
        ]
    )
    assert report.failing_ids == ["b", "c"]
    assert report.passing_ids == ["a"]
    assert report.by_id["b"].message == "boom"


def test_usage_accumulates_and_estimates_cost() -> None:
    total = Usage()
    total.add(Usage(input_tokens=1_000_000, output_tokens=0))
    total.add(Usage(input_tokens=0, output_tokens=1_000_000, cache_read_input_tokens=1_000_000))
    assert total.input_tokens == 1_000_000
    # opus 5: $5 in, $25 out, cache reads at 10% of input
    assert total.estimated_cost_usd(model="claude-opus-5") == 5.0 + 25.0 + 0.5
