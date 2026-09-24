"""The engineering loop: baseline -> triage -> (agent -> guard -> judge -> tests) x N."""

from __future__ import annotations

import json
import logging
import time
from datetime import datetime, timezone
from pathlib import Path

import anthropic

from testloop.config import LoopConfig
from testloop.decisions import load_decisions
from testloop.guard import WriteGuard
from testloop.llm.agent import FixAgent
from testloop.llm.judge import HardcodeJudge
from testloop.llm.triage import TestTriager, gather_test_context
from testloop.models import IterationRecord, LoopResult, LoopStatus, TestReport, TestResult, TriageVerdict, Usage
from testloop.report import emit_ci_output, write_result, write_review_report
from testloop.runners import make_runner
from testloop.runners.base import TestRunner
from testloop.workspace import Workspace

log = logging.getLogger(__name__)


class EngineeringLoop:
    def __init__(self, config: LoopConfig, client=None, runner: TestRunner | None = None) -> None:
        self.config = config
        self.client = client if client is not None else anthropic.Anthropic()
        self.runner = runner or make_runner(config.runner, config.repo_path, config.test_timeout_seconds, config.test_command)
        self.guard = WriteGuard(
            workspace=Workspace(config.repo_path, protected_globs=[]),
            test_file_globs=self.runner.test_file_globs,
            config_file_globs=self.runner.config_file_globs,
            extra_protected_globs=config.protected_globs,
        )
        self.workspace = Workspace(config.repo_path, protected_globs=self.guard.protected_globs)
        self.guard.workspace = self.workspace
        self.triager = TestTriager(self.client, config.model, config.review_effort, config.repo_path, self.runner)
        self.judge = HardcodeJudge(self.client, config.model, config.review_effort)
        self.agent = FixAgent(
            self.client, config.model, config.agent_effort, self.workspace, self.guard, self.runner,
            max_tool_calls=config.max_tool_calls_per_iteration, use_fallbacks=config.use_fallbacks,
        )
        self.clock = time.monotonic
        self.run_dir = config.run_dir or config.repo_path / ".testloop" / "runs" / datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
        self.result = LoopResult(status=LoopStatus.ABORTED, model=config.model, run_dir=str(self.run_dir))

    # ---- public --------------------------------------------------------------------------

    def run(self) -> LoopResult:
        self.run_dir.mkdir(parents=True, exist_ok=True)
        started = self.clock()
        try:
            self._run(started)
        except Exception as exc:  # noqa: BLE001 - a crash must still produce artifacts and an exit code
            log.exception("loop crashed")
            self._finish(LoopStatus.ABORTED, f"unexpected error: {exc!r}")
        self.result.final_diff = self.workspace.run_diff()
        write_result(self.run_dir, self.result)
        emit_ci_output(self.result)
        log.info("testloop finished: %s (%s); cost ~$%.2f", self.result.status.value, self.result.reason, self.result.estimated_cost_usd)
        return self.result

    # ---- phases --------------------------------------------------------------------------

    def _run(self, started: float) -> None:
        cfg = self.config
        decisions = load_decisions(cfg.repo_path / cfg.decisions_file)
        self.workspace.snapshot_protected()

        baseline, flaky = self._baseline()
        if baseline is None:
            return
        failing = [baseline.by_id[t] for t in baseline.failing_ids if t not in flaky]
        self.result.flaky = sorted(flaky)
        if not failing:
            write_review_report(self.run_dir, [], self.result.flaky, [], cfg.decisions_file)
            self._finish(LoopStatus.NOTHING_TO_FIX, "baseline test suite already passes")
            return

        quarantined = [t.test_id for t in failing if t.test_id in decisions.quarantined]
        candidates = [t for t in failing if t.test_id not in decisions.quarantined]
        to_triage = [t for t in candidates if t.test_id not in decisions.accepted]
        flagged: list[TriageVerdict] = []
        if to_triage:
            verdicts, usage = self.triager.triage(to_triage)
            self.result.usage.add(usage)
            flagged = [v for v in verdicts if not v.usable]
        self.result.flagged = flagged
        flagged_ids = {v.test_id for v in flagged}
        excluded = flagged_ids | set(quarantined) | set(flaky)
        write_review_report(self.run_dir, flagged, self.result.flaky, quarantined, cfg.decisions_file)

        targets = [t for t in candidates if t.test_id not in flagged_ids]
        self.result.targets = [t.test_id for t in targets]
        if not targets:
            self._finish(LoopStatus.NOTHING_TO_FIX, f"all {len(failing)} failing test(s) were flagged, quarantined or flaky; nothing for the agent to fix")
            return

        baseline_passing = set(baseline.passing_ids)
        history: list[IterationRecord] = []
        for number in range(1, cfg.max_iterations + 1):
            over = self._budget_exceeded(started)
            if over:
                self._finish(LoopStatus.BUDGET_EXHAUSTED, over)
                return
            record = self._iteration(number, targets, history, baseline_passing, excluded)
            history.append(record)
            self.result.iterations.append(record)
            self._save_iteration(record)
            if record.error and not record.reverted:
                self._finish(LoopStatus.ABORTED, record.error)
                return
            if record.error is None and not record.reverted and not record.still_failing and not record.regressions:
                self._finish(LoopStatus.PASSED, f"all target tests pass after {number} iteration(s)")
                return
            if not record.reverted:
                targets = [self._last_report.by_id[t] for t in record.still_failing + record.regressions]
        self._finish(LoopStatus.MAX_ITERATIONS_REACHED, f"{cfg.max_iterations} iteration(s) used; still failing: {history[-1].still_failing or history[-1].regressions or 'last change rejected'}")

    def _baseline(self) -> tuple[TestReport | None, set[str]]:
        reports: list[TestReport] = []
        for n in range(self.config.baseline_runs):
            log.info("baseline run %d/%d", n + 1, self.config.baseline_runs)
            report = self.runner.run()
            if self._runner_broken(report):
                self._finish(LoopStatus.ABORTED, f"test runner produced no results: {report.results[0].message if report.results else 'empty report'}\n{report.stdout_tail[-2000:]}")
                return None, set()
            reports.append(report)
        failing_sets = [set(r.failing_ids) for r in reports]
        always = set.intersection(*failing_sets)
        ever = set.union(*failing_sets)
        flaky = ever - always
        if flaky:
            log.warning("flaky at baseline (excluded): %s", sorted(flaky))
        self._last_report = reports[-1]
        return reports[-1], flaky

    @staticmethod
    def _runner_broken(report: TestReport) -> bool:
        return len(report.results) == 1 and report.results[0].test_id == "<suite>" and report.results[0].outcome.value == "error"

    def _iteration(self, number: int, targets: list[TestResult], history: list[IterationRecord], baseline_passing: set[str], excluded: set[str]) -> IterationRecord:
        record = IterationRecord(number=number)
        log.info("iteration %d: %d target(s): %s", number, len(targets), [t.test_id for t in targets])
        sources = self._test_sources(targets)
        self.workspace.begin_iteration()

        attempt = self.agent.attempt(targets, sources, history, number)
        record.usage.add(attempt.usage)
        self.result.usage.add(attempt.usage)
        record.agent_summary = attempt.summary
        self._transcript = attempt.transcript
        if attempt.error:
            self.workspace.revert_iteration()
            record.error = attempt.error
            record.reverted = False  # error aborts the run; nothing to build on
            return self._done(record)

        tampered = self.workspace.verify_protected_unchanged()
        if tampered:
            self.workspace.revert_iteration()
            record.error = f"protected files were modified by the agent: {tampered}"
            return self._done(record)

        record.diff = self.workspace.iteration_diff()
        verdict, usage = self.judge.review(record.diff, sources)
        record.usage.add(usage)
        self.result.usage.add(usage)
        record.judge = verdict
        if not verdict.accepted:
            self.workspace.revert_iteration()
            record.reverted = True
            return self._done(record)

        report = self.runner.run()
        self._last_report = report
        failing_now = set(report.failing_ids)
        record.still_failing = [t.test_id for t in targets if t.test_id in failing_now]
        record.regressions = sorted(t for t in baseline_passing if t in failing_now and t not in excluded)
        return self._done(record)

    # ---- helpers -------------------------------------------------------------------------

    def _test_sources(self, targets: list[TestResult]) -> dict[str, str]:
        out: dict[str, str] = {}
        for t in targets:
            snippet, whole, _ = gather_test_context(self.config.repo_path, self.runner, t)
            out[t.test_id] = snippet or whole[:4000]
        return out

    def _budget_exceeded(self, started: float) -> str | None:
        cfg = self.config
        elapsed = self.clock() - started
        if cfg.time_budget_seconds is not None and elapsed >= cfg.time_budget_seconds:
            return f"time budget of {cfg.time_budget_seconds}s exhausted after {elapsed:.0f}s"
        cost = self.result.estimated_cost_usd
        if cfg.max_cost_usd is not None and cost >= cfg.max_cost_usd:
            return f"cost budget of ${cfg.max_cost_usd:.2f} exhausted (estimated ${cost:.2f})"
        return None

    def _done(self, record: IterationRecord) -> IterationRecord:
        record.finished_at = datetime.now(timezone.utc)
        return record

    def _save_iteration(self, record: IterationRecord) -> None:
        d = self.run_dir / f"iteration-{record.number}"
        d.mkdir(parents=True, exist_ok=True)
        (d / "transcript.json").write_text(json.dumps(getattr(self, "_transcript", []), indent=2, default=str))
        (d / "change.patch").write_text(record.diff)
        (d / "record.json").write_text(record.model_dump_json(indent=2))

    def _finish(self, status: LoopStatus, reason: str) -> None:
        self.result.status = status
        self.result.reason = reason
