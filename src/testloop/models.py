"""Data models shared across the harness. Pure data, no behaviour that touches disk or network."""

from __future__ import annotations

from datetime import datetime, timezone
from enum import StrEnum
from typing import Literal

from pydantic import BaseModel, Field

# Per-million-token prices used only for the cost budget and the run summary.
# Cache reads are billed at 10% of the input rate.
_PRICES_USD_PER_MTOK: dict[str, tuple[float, float]] = {
    "claude-opus-5": (5.0, 25.0),
    "claude-fable-5-1": (10.0, 50.0),
    "claude-sonnet-5": (2.0, 10.0),
    "claude-haiku-4-5": (1.0, 5.0),
}


class TestOutcome(StrEnum):
    __test__ = False  # keep pytest from collecting these
    PASSED = "passed"
    FAILED = "failed"
    ERROR = "error"
    SKIPPED = "skipped"


class TestCase(BaseModel):
    __test__ = False  # keep pytest from collecting these
    """A test the runner can address individually."""

    test_id: str
    file: str | None = None
    name: str | None = None


class TestResult(BaseModel):
    __test__ = False  # keep pytest from collecting these
    test_id: str
    outcome: TestOutcome
    message: str = ""
    traceback: str = ""
    file: str | None = None
    duration_s: float = 0.0


class TestReport(BaseModel):
    __test__ = False  # keep pytest from collecting these
    results: list[TestResult]
    exit_code: int
    stdout_tail: str
    command: str
    timed_out: bool = False

    @property
    def by_id(self) -> dict[str, TestResult]:
        return {r.test_id: r for r in self.results}

    @property
    def failing_ids(self) -> list[str]:
        return [r.test_id for r in self.results if r.outcome in (TestOutcome.FAILED, TestOutcome.ERROR)]

    @property
    def passing_ids(self) -> list[str]:
        return [r.test_id for r in self.results if r.outcome == TestOutcome.PASSED]


TriageFlag = Literal["order_dependent", "overly_specific_external_output"]


class TriageVerdict(BaseModel):
    test_id: str
    usable: bool
    flags: list[TriageFlag] = Field(default_factory=list)
    explanation: str


class TriageBatch(BaseModel):
    """Structured-output schema for one triage call covering several tests."""

    verdicts: list[TriageVerdict]


class Violation(BaseModel):
    file: str
    line_hint: str = ""
    kind: Literal[
        "special_cased_input",
        "lookup_table_on_test_data",
        "test_environment_detection",
        "test_module_tampering",
        "swallowed_expected_error",
        "other",
    ]
    explanation: str


class JudgeVerdict(BaseModel):
    accepted: bool
    violations: list[Violation] = Field(default_factory=list)
    summary: str


class Usage(BaseModel):
    input_tokens: int = 0
    output_tokens: int = 0
    cache_read_input_tokens: int = 0
    cache_creation_input_tokens: int = 0
    calls: int = 0

    def add(self, other: Usage) -> None:
        self.input_tokens += other.input_tokens
        self.output_tokens += other.output_tokens
        self.cache_read_input_tokens += other.cache_read_input_tokens
        self.cache_creation_input_tokens += other.cache_creation_input_tokens
        self.calls += other.calls or 1

    def estimated_cost_usd(self, model: str) -> float:
        in_price, out_price = _PRICES_USD_PER_MTOK.get(model, _PRICES_USD_PER_MTOK["claude-opus-5"])
        return (
            self.input_tokens * in_price
            + self.cache_creation_input_tokens * in_price * 1.25
            + self.cache_read_input_tokens * in_price * 0.10
            + self.output_tokens * out_price
        ) / 1_000_000


class IterationRecord(BaseModel):
    number: int
    agent_summary: str = ""
    diff: str = ""
    judge: JudgeVerdict | None = None
    reverted: bool = False
    still_failing: list[str] = Field(default_factory=list)
    regressions: list[str] = Field(default_factory=list)
    usage: Usage = Field(default_factory=Usage)
    started_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))
    finished_at: datetime | None = None
    error: str | None = None


class LoopStatus(StrEnum):
    PASSED = "passed"
    MAX_ITERATIONS_REACHED = "max_iterations_reached"
    NOTHING_TO_FIX = "nothing_to_fix"
    BUDGET_EXHAUSTED = "budget_exhausted"
    ABORTED = "aborted"


# Exit codes are stable so pipelines can branch on them.
EXIT_CODES: dict[LoopStatus, int] = {
    LoopStatus.PASSED: 0,
    LoopStatus.NOTHING_TO_FIX: 0,
    LoopStatus.MAX_ITERATIONS_REACHED: 2,
    LoopStatus.BUDGET_EXHAUSTED: 3,
    LoopStatus.ABORTED: 4,
}


class LoopResult(BaseModel):
    status: LoopStatus
    reason: str = ""
    targets: list[str] = Field(default_factory=list)
    flagged: list[TriageVerdict] = Field(default_factory=list)
    flaky: list[str] = Field(default_factory=list)
    iterations: list[IterationRecord] = Field(default_factory=list)
    final_diff: str = ""
    usage: Usage = Field(default_factory=Usage)
    model: str = ""
    run_dir: str | None = None

    @property
    def exit_code(self) -> int:
        return EXIT_CODES[self.status]

    @property
    def estimated_cost_usd(self) -> float:
        return self.usage.estimated_cost_usd(self.model)
