"""Command-line entry point. Exit codes: 0 passed/nothing to fix, 2 max iterations, 3 budget, 4 aborted, 5 flagged (opt-in)."""

from __future__ import annotations

import logging
import re
import sys
from pathlib import Path

import click

from testloop.config import LoopConfig
from testloop.models import LoopResult, LoopStatus
from testloop.report import summary_markdown

EXIT_FLAGGED = 5


def parse_duration(text: str | None) -> int | None:
    if text is None:
        return None
    m = re.fullmatch(r"\s*(\d+)\s*([smh]?)\s*", text)
    if not m:
        raise click.BadParameter(f"expected a duration like 90s, 20m or 1h, got {text!r}")
    value, unit = int(m.group(1)), m.group(2) or "s"
    return value * {"s": 1, "m": 60, "h": 3600}[unit]


def configure_logging(verbose: bool) -> None:
    logging.basicConfig(
        level=logging.DEBUG if verbose else logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
        stream=sys.stderr,
    )
    logging.getLogger("httpx").setLevel(logging.WARNING)
    logging.getLogger("httpx2").setLevel(logging.WARNING)


def common_options(fn):
    options = [
        click.option("--repo", "repo_path", type=click.Path(exists=True, file_okay=False, path_type=Path), default=".", show_default=True, help="Repository to fix."),
        click.option("--runner", type=click.Choice(["pytest", "vitest", "jest", "shell"]), required=True),
        click.option("--test-command", default=None, help="Override the runner's command (required for --runner shell)."),
        click.option("--model", default="claude-opus-5", show_default=True),
        click.option("--effort", "agent_effort", type=click.Choice(["low", "medium", "high", "xhigh", "max"]), default="xhigh", show_default=True, help="Effort for the fixing agent."),
        click.option("--review-effort", type=click.Choice(["low", "medium", "high", "xhigh", "max"]), default="high", show_default=True, help="Effort for triage and the judge."),
        click.option("--protect", "protected_globs", multiple=True, help="Extra globs the agent may not modify (repeatable)."),
        click.option("--baseline-runs", type=click.IntRange(1, 5), default=1, show_default=True, help="Run the baseline N times to detect flaky tests."),
        click.option("--test-timeout", type=int, default=600, show_default=True, help="Seconds allowed per test run."),
        click.option("--no-fallbacks", is_flag=True, help="Disable server-side refusal fallbacks."),
        click.option("--run-dir", type=click.Path(path_type=Path), default=None, help="Where to write transcripts and artifacts (default: <repo>/.testloop/runs/<timestamp>)."),
        click.option("-v", "--verbose", is_flag=True),
    ]
    for opt in reversed(options):
        fn = opt(fn)
    return fn


def build_config(**kw) -> LoopConfig:
    return LoopConfig(
        repo_path=kw["repo_path"],
        runner=kw["runner"],
        test_command=kw["test_command"],
        max_iterations=kw.get("max_iterations", 3),
        model=kw["model"],
        agent_effort=kw["agent_effort"],
        review_effort=kw["review_effort"],
        use_fallbacks=not kw["no_fallbacks"],
        protected_globs=list(kw["protected_globs"]),
        baseline_runs=kw["baseline_runs"],
        test_timeout_seconds=kw["test_timeout"],
        time_budget_seconds=kw.get("time_budget_seconds"),
        max_cost_usd=kw.get("max_cost_usd"),
        max_tool_calls_per_iteration=kw.get("max_tool_calls", 60),
        run_dir=kw["run_dir"],
    )


@click.group()
def main() -> None:
    """testloop: fix code until the tests pass, without touching the tests."""


@main.command()
@common_options
@click.option("--max-iterations", type=click.IntRange(min=1), default=3, show_default=True)
@click.option("--max-tool-calls", type=click.IntRange(min=1), default=60, show_default=True, help="Tool calls allowed per iteration.")
@click.option("--time-budget", default=None, help="Stop starting new iterations after this long (e.g. 20m).")
@click.option("--max-cost-usd", type=float, default=None, help="Stop starting new iterations past this estimated spend.")
@click.option("--output-patch", type=click.Path(path_type=Path), default=None, help="Also copy the final diff here.")
@click.option("--result-json", type=click.Path(path_type=Path), default=None, help="Also copy result.json here.")
@click.option("--fail-on-flagged", is_flag=True, help=f"Exit {EXIT_FLAGGED} when triage flagged tests, even if the loop passed.")
def run(**kw) -> None:
    """Run the fix loop."""
    configure_logging(kw["verbose"])
    from testloop.loop import EngineeringLoop

    config = build_config(time_budget_seconds=parse_duration(kw.pop("time_budget")), **kw)
    result = EngineeringLoop(config).run()
    _copy_outputs(result, kw["output_patch"], kw["result_json"])
    click.echo(summary_markdown(result))
    click.echo(f"artifacts: {result.run_dir}")
    code = result.exit_code
    if kw["fail_on_flagged"] and result.flagged and code == 0:
        code = EXIT_FLAGGED
    sys.exit(code)


@main.command()
@common_options
def triage(**kw) -> None:
    """Only run baseline + triage; report flagged tests and exit without fixing anything."""
    configure_logging(kw["verbose"])
    from testloop.loop import EngineeringLoop

    config = build_config(**kw)
    loop = EngineeringLoop(config)
    loop.config = config.model_copy(update={"max_iterations": 1})
    loop.agent = _NoAgent()
    result = loop.run()
    click.echo(summary_markdown(result))
    click.echo(f"review report: {Path(result.run_dir) / 'testloop-review.md'}")
    sys.exit(EXIT_FLAGGED if result.flagged else 0)


class _NoAgent:
    """Stands in for the fixing agent in triage-only mode so the loop stops after triage."""

    def attempt(self, targets, sources, history, iteration):
        from testloop.llm.agent import AgentAttempt

        return AgentAttempt(summary="triage-only run; no changes attempted")


def _copy_outputs(result: LoopResult, patch: Path | None, result_json: Path | None) -> None:
    run_dir = Path(result.run_dir or ".")
    if patch:
        patch.parent.mkdir(parents=True, exist_ok=True)
        patch.write_text(result.final_diff)
    if result_json:
        result_json.parent.mkdir(parents=True, exist_ok=True)
        result_json.write_bytes((run_dir / "result.json").read_bytes())


@main.command()
@click.option("--repo", "repo_path", type=click.Path(exists=True, file_okay=False, path_type=Path), default=".", show_default=True)
@click.option("--result", "result_json", type=click.Path(exists=True, dir_okay=False, path_type=Path), required=True, help="result.json from a run.")
@click.option("--patch", type=click.Path(exists=True, dir_okay=False, path_type=Path), default=None, help="Apply this patch first (fresh checkout).")
@click.option("--mode", type=click.Choice(["branch", "pull-request"]), default="pull-request", show_default=True)
@click.option("--branch-name", default=None, help="Default: testloop/fix-<short id>.")
@click.option("--base", default=None, help="Base branch for the pull request (default: current branch).")
@click.option("--allow-unfixed", is_flag=True, help="Publish partial progress even if the loop did not pass.")
@click.option("--dry-run", is_flag=True, help="Print the git/gh commands without running them.")
def publish(**kw) -> None:
    """Push the fix to a branch and open a pull request. Never pushes to the source branch."""
    configure_logging(False)
    from testloop.publish import publish_result

    code = publish_result(**kw)
    sys.exit(code)


if __name__ == "__main__":
    main()
