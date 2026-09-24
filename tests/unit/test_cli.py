import json
from pathlib import Path

import pytest
from click.testing import CliRunner

from testloop.cli import main, parse_duration
from testloop.models import LoopResult, LoopStatus, TriageVerdict


def test_parse_duration() -> None:
    assert parse_duration("90s") == 90 and parse_duration("20m") == 1200 and parse_duration("1h") == 3600 and parse_duration("45") == 45
    assert parse_duration(None) is None
    with pytest.raises(Exception):
        parse_duration("soon")


def test_run_command_wires_config_and_exit_code(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    captured = {}

    class FakeLoop:
        def __init__(self, config):
            captured["config"] = config
            self.run_dir = tmp_path / "run"

        def run(self):
            self.run_dir.mkdir()
            result = LoopResult(status=LoopStatus.MAX_ITERATIONS_REACHED, run_dir=str(self.run_dir), final_diff="+x", model="m",
                                flagged=[TriageVerdict(test_id="a::b", usable=False, flags=["order_dependent"], explanation="e")])
            (self.run_dir / "result.json").write_text(json.dumps({"status": "max_iterations_reached"}))
            return result

    import testloop.loop

    monkeypatch.setattr(testloop.loop, "EngineeringLoop", FakeLoop)
    out = CliRunner().invoke(
        main,
        ["run", "--repo", str(tmp_path), "--runner", "pytest", "--max-iterations", "5", "--time-budget", "20m", "--max-cost-usd", "7",
         "--protect", "src/fixtures/**", "--output-patch", str(tmp_path / "out" / "fix.patch"), "--result-json", str(tmp_path / "out" / "r.json")],
    )
    assert out.exit_code == 2, out.output
    cfg = captured["config"]
    assert cfg.max_iterations == 5 and cfg.time_budget_seconds == 1200 and cfg.max_cost_usd == 7 and cfg.protected_globs == ["src/fixtures/**"]
    assert (tmp_path / "out" / "fix.patch").read_text() == "+x"
    assert json.loads((tmp_path / "out" / "r.json").read_text())["status"] == "max_iterations_reached"
    assert "max_iterations_reached" in out.output and "a::b" in out.output


def test_fail_on_flagged(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    class FakeLoop:
        def __init__(self, config):
            self.run_dir = tmp_path / "run"

        def run(self):
            self.run_dir.mkdir(exist_ok=True)
            (self.run_dir / "result.json").write_text("{}")
            return LoopResult(status=LoopStatus.PASSED, run_dir=str(self.run_dir), flagged=[TriageVerdict(test_id="a::b", usable=False, explanation="e")])

    import testloop.loop

    monkeypatch.setattr(testloop.loop, "EngineeringLoop", FakeLoop)
    assert CliRunner().invoke(main, ["run", "--repo", str(tmp_path), "--runner", "pytest"]).exit_code == 0
    assert CliRunner().invoke(main, ["run", "--repo", str(tmp_path), "--runner", "pytest", "--fail-on-flagged"]).exit_code == 5


def test_shell_runner_requires_command(tmp_path: Path) -> None:
    out = CliRunner().invoke(main, ["run", "--repo", str(tmp_path), "--runner", "shell"])
    assert out.exit_code != 0 and "test_command" in str(out.exception or out.output)
