from pathlib import Path

import pytest
from pydantic import ValidationError

from testloop.config import LoopConfig


def test_defaults(tmp_path: Path) -> None:
    cfg = LoopConfig(repo_path=tmp_path, runner="pytest")
    assert cfg.max_iterations == 3
    assert cfg.model == "claude-opus-5"
    assert cfg.agent_effort == "xhigh"
    assert cfg.review_effort == "high"
    assert cfg.baseline_runs == 1
    assert cfg.max_tool_calls_per_iteration == 60


def test_max_iterations_must_be_positive(tmp_path: Path) -> None:
    with pytest.raises(ValidationError):
        LoopConfig(repo_path=tmp_path, runner="pytest", max_iterations=0)


def test_shell_runner_requires_command(tmp_path: Path) -> None:
    with pytest.raises(ValidationError, match="test_command"):
        LoopConfig(repo_path=tmp_path, runner="shell")


def test_repo_path_must_exist(tmp_path: Path) -> None:
    with pytest.raises(ValidationError, match="does not exist"):
        LoopConfig(repo_path=tmp_path / "missing", runner="pytest")


def test_budgets_are_optional_and_positive(tmp_path: Path) -> None:
    cfg = LoopConfig(repo_path=tmp_path, runner="pytest", time_budget_seconds=600, max_cost_usd=5.0)
    assert cfg.time_budget_seconds == 600
    with pytest.raises(ValidationError):
        LoopConfig(repo_path=tmp_path, runner="pytest", max_cost_usd=-1)
