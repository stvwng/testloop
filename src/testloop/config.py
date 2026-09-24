"""Run configuration. Validated once at the boundary so the rest of the code can trust it."""

from __future__ import annotations

from pathlib import Path
from typing import Literal

from pydantic import BaseModel, Field, field_validator, model_validator

RunnerKind = Literal["pytest", "vitest", "jest", "shell"]
Effort = Literal["low", "medium", "high", "xhigh", "max"]


class LoopConfig(BaseModel):
    repo_path: Path
    runner: RunnerKind
    test_command: str | None = None
    max_iterations: int = Field(default=3, ge=1)

    model: str = "claude-opus-5"
    agent_effort: Effort = "xhigh"
    review_effort: Effort = "high"
    use_fallbacks: bool = True

    protected_globs: list[str] = Field(default_factory=list)
    triage_scope: Literal["failing", "all"] = "failing"
    decisions_file: str = ".testloop.yml"

    # Baseline the suite more than once to detect flaky tests before the loop starts.
    baseline_runs: int = Field(default=1, ge=1, le=5)

    # Bounds so a CI job cannot run away.
    max_tool_calls_per_iteration: int = Field(default=60, ge=1)
    test_timeout_seconds: int = Field(default=600, ge=1)
    time_budget_seconds: int | None = Field(default=None, gt=0)
    max_cost_usd: float | None = Field(default=None, gt=0)

    run_dir: Path | None = None

    @field_validator("repo_path")
    @classmethod
    def _must_exist(cls, value: Path) -> Path:
        resolved = value.expanduser().resolve()
        if not resolved.is_dir():
            raise ValueError(f"repo_path does not exist or is not a directory: {value}")
        return resolved

    @model_validator(mode="after")
    def _shell_needs_command(self) -> LoopConfig:
        if self.runner == "shell" and not self.test_command:
            raise ValueError("runner 'shell' requires test_command")
        return self
