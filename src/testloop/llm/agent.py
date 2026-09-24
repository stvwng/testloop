"""The fixing agent: one tool-runner session per iteration over a guarded tool set."""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Any

from anthropic import beta_tool
from anthropic.lib.tools import ToolError

from testloop.guard import WriteGuard
from testloop.llm.common import MAX_TOKENS, describe_api_error, truncate, usage_from
from testloop.llm.prompts import AGENT_SYSTEM
from testloop.models import IterationRecord, TestOutcome, TestReport, TestResult, Usage
from testloop.runners.base import TestRunner
from testloop.workspace import PathOutsideRepo, Workspace

log = logging.getLogger(__name__)

FALLBACK_BETA = "server-side-fallback-2026-07-01"
READ_LIMIT = 60000
RESULT_LIMIT = 12000


def format_report(report: TestReport) -> str:
    counts = {o: 0 for o in TestOutcome}
    for r in report.results:
        counts[r.outcome] += 1
    lines = [
        f"{counts[TestOutcome.PASSED]} passed, {counts[TestOutcome.FAILED]} failed, "
        f"{counts[TestOutcome.ERROR]} errored, {counts[TestOutcome.SKIPPED]} skipped (exit code {report.exit_code}"
        + (", TIMED OUT" if report.timed_out else "")
        + ")"
    ]
    for r in report.results:
        lines.append(f"{r.outcome.value.upper()} {r.test_id}")
        if r.outcome in (TestOutcome.FAILED, TestOutcome.ERROR):
            if r.message:
                lines.append("  " + truncate(r.message, 800).replace("\n", "\n  "))
            if r.traceback:
                lines.append("  " + truncate(r.traceback, 2500).replace("\n", "\n  "))
    if report.exit_code != 0 and not any(r.outcome != TestOutcome.PASSED for r in report.results):
        lines.append("Runner output:\n" + truncate(report.stdout_tail, 3000))
    return truncate("\n".join(lines), RESULT_LIMIT)


def build_tools(workspace: Workspace, guard: WriteGuard, runner: TestRunner) -> list[Any]:
    """Tools close over the workspace so the model never sees absolute paths or a shell."""

    def _checked(path: str) -> str:
        denial = guard.check(path)
        if denial is not None:
            raise ToolError(f"write denied ({denial.rule}): {denial.message}")
        return path

    @beta_tool
    def list_files(glob: str = "**/*") -> str:
        """List repository files matching a glob (relative paths, '**' crosses directories).

        Args:
            glob: Pattern such as "src/**/*.py" or "**/*.ts". Defaults to every file.
        """
        files = workspace.list_files(glob)
        if not files:
            return "no files match"
        return truncate("\n".join(files[:2000]), RESULT_LIMIT)

    @beta_tool
    def read_file(path: str) -> str:
        """Read a file from the repository.

        Args:
            path: Repository-relative path.
        """
        try:
            text = workspace.read_text(path)
        except PathOutsideRepo as exc:
            raise ToolError(str(exc)) from exc
        except FileNotFoundError:
            raise ToolError(f"{path} does not exist") from None
        except UnicodeDecodeError:
            raise ToolError(f"{path} is not a text file") from None
        numbered = "\n".join(f"{i:5d}| {line}" for i, line in enumerate(text.splitlines(), start=1))
        return truncate(numbered, READ_LIMIT)

    @beta_tool
    def grep(pattern: str, glob: str = "**/*") -> str:
        """Search file contents with a regular expression.

        Args:
            pattern: Python regular expression.
            glob: Restrict the search to files matching this glob.
        """
        hits = workspace.grep(pattern, glob)
        if not hits:
            return "no matches"
        return truncate("\n".join(f"{f}:{n}: {line}" for f, n, line in hits), RESULT_LIMIT)

    @beta_tool
    def edit_file(path: str, old_string: str, new_string: str) -> str:
        """Replace one exact, unique occurrence of old_string in a file with new_string.

        Args:
            path: Repository-relative path of an existing source file.
            old_string: Exact text to replace; must occur exactly once.
            new_string: Replacement text.
        """
        _checked(path)
        try:
            text = workspace.read_text(path)
        except FileNotFoundError:
            raise ToolError(f"{path} does not exist; use write_file to create files") from None
        count = text.count(old_string)
        if count == 0:
            raise ToolError(f"old_string not found in {path}")
        if count > 1:
            raise ToolError(f"old_string occurs {count} times in {path}; include more context to make it unique")
        workspace.write_text(path, text.replace(old_string, new_string, 1))
        return f"edited {path}"

    @beta_tool
    def write_file(path: str, content: str) -> str:
        """Create or overwrite a source file. Prefer edit_file for changes to existing files.

        Args:
            path: Repository-relative path.
            content: Full file contents.
        """
        _checked(path)
        workspace.write_text(path, content)
        return f"wrote {path} ({len(content)} chars)"

    @beta_tool
    def run_tests(test_ids: list[str] | None = None) -> str:
        """Run tests and return per-test results with failure output.

        Args:
            test_ids: Test ids to run (as given in the task). Omit to run the whole suite.
        """
        report = runner.run(test_ids or None)
        return format_report(report)

    return [list_files, read_file, grep, edit_file, write_file, run_tests]


@dataclass
class AgentAttempt:
    summary: str = ""
    usage: Usage = field(default_factory=Usage)
    stop_reason: str = ""
    tool_calls: int = 0
    refused: bool = False
    error: str | None = None
    transcript: list[dict[str, Any]] = field(default_factory=list)


class FixAgent:
    def __init__(
        self,
        client,
        model: str,
        effort: str,
        workspace: Workspace,
        guard: WriteGuard,
        runner: TestRunner,
        max_tool_calls: int,
        use_fallbacks: bool = True,
    ) -> None:
        self.client = client
        self.model = model
        self.effort = effort
        self.workspace = workspace
        self.guard = guard
        self.runner = runner
        self.max_tool_calls = max_tool_calls
        self.use_fallbacks = use_fallbacks

    def attempt(
        self,
        targets: list[TestResult],
        test_sources: dict[str, str],
        history: list[IterationRecord],
        iteration: int,
    ) -> AgentAttempt:
        tools = build_tools(self.workspace, self.guard, self.runner)
        prompt = self._build_prompt(targets, test_sources, history, iteration)
        kwargs: dict[str, Any] = dict(
            model=self.model,
            max_tokens=MAX_TOKENS,
            system=[{"type": "text", "text": AGENT_SYSTEM, "cache_control": {"type": "ephemeral"}}],
            thinking={"type": "adaptive"},
            output_config={"effort": self.effort},
            tools=tools,
            messages=[{"role": "user", "content": prompt}],
            max_iterations=self.max_tool_calls,
        )
        if self.use_fallbacks:
            kwargs["betas"] = [FALLBACK_BETA]
            kwargs["fallbacks"] = "default"

        attempt = AgentAttempt()
        log.info("agent: iteration %d, %d target test(s), prompt %d chars", iteration, len(targets), len(prompt))
        try:
            runner = self.client.beta.messages.tool_runner(**kwargs)
            last_text = ""
            for message in runner:
                attempt.usage.add(usage_from(message))
                attempt.stop_reason = getattr(message, "stop_reason", "") or ""
                entry: dict[str, Any] = {"stop_reason": attempt.stop_reason, "blocks": []}
                for block in message.content:
                    btype = getattr(block, "type", "")
                    if btype == "tool_use":
                        attempt.tool_calls += 1
                        entry["blocks"].append({"type": "tool_use", "name": block.name, "input": block.input})
                        log.debug("[DEBUG] agent tool_use name=%s input=%s", block.name, truncate(repr(block.input), 300))
                    elif btype == "text":
                        last_text = block.text
                        entry["blocks"].append({"type": "text", "text": block.text})
                attempt.transcript.append(entry)
                if attempt.stop_reason == "refusal":
                    attempt.refused = True
                    details = getattr(message, "stop_details", None)
                    attempt.error = f"model refused ({getattr(details, 'category', None)}): {getattr(details, 'explanation', '')}"
                    log.error("agent: %s", attempt.error)
                    break
            attempt.summary = last_text.strip()
        except Exception as exc:  # noqa: BLE001
            err = describe_api_error("agent", exc)
            attempt.error = str(err)
        return attempt

    def _build_prompt(self, targets: list[TestResult], test_sources: dict[str, str], history: list[IterationRecord], iteration: int) -> str:
        parts = [f"Iteration {iteration}. Make these {len(targets)} failing test(s) pass by fixing the implementation.\n"]
        for t in targets:
            parts.append(f"<failing_test id=\"{t.test_id}\">")
            if t.message:
                parts.append(f"<message>\n{truncate(t.message, 1500)}\n</message>")
            if t.traceback:
                parts.append(f"<traceback>\n{truncate(t.traceback, 4000)}\n</traceback>")
            src = test_sources.get(t.test_id)
            if src:
                parts.append(f"<source>\n{src}</source>")
            parts.append("</failing_test>\n")
        if history:
            parts.append("<previous_iterations>")
            for rec in history:
                parts.append(f"Iteration {rec.number}: {rec.agent_summary or '(no summary)'}")
                if rec.reverted and rec.judge:
                    parts.append(f"  Outcome: change REJECTED and reverted — {rec.judge.summary}")
                    for v in rec.judge.violations:
                        parts.append(f"  - {v.kind} in {v.file}: {v.explanation}")
                elif rec.error:
                    parts.append(f"  Outcome: error — {rec.error}")
                else:
                    if rec.still_failing:
                        parts.append(f"  Outcome: change kept; still failing: {', '.join(rec.still_failing)}")
                    if rec.regressions:
                        parts.append(f"  Regressions introduced: {', '.join(rec.regressions)}")
            parts.append("</previous_iterations>\n")
            parts.append("Changes from kept iterations are already applied in the workspace; build on them.")
        parts.append("Use run_tests with the ids above while iterating. Report when done.")
        return "\n".join(parts)
