"""Artifacts a pipeline can consume: review report, result JSON, patch, CI annotations."""

from __future__ import annotations

import json
import os
from pathlib import Path

from testloop.decisions import Decisions
from testloop.models import LoopResult, TriageVerdict

REVIEW_MD = "testloop-review.md"
REVIEW_JSON = "testloop-review.json"
RESULT_JSON = "result.json"
PATCH = "final.patch"


def write_review_report(run_dir: Path, flagged: list[TriageVerdict], flaky: list[str], quarantined: list[str], decisions_file: str) -> Path:
    lines = ["# testloop: tests needing human review", ""]
    if not flagged and not flaky:
        lines.append("No tests were flagged.")
    if flagged:
        lines += ["## Flagged by triage (excluded from the fix loop)", ""]
        for v in flagged:
            lines += [f"### `{v.test_id}`", "", f"- flags: {', '.join(v.flags) or 'none'}", f"- reason: {v.explanation}", ""]
        lines += [
            f"To stop these from being re-flagged, record a decision in `{decisions_file}`:",
            "",
            "```yaml",
            Decisions.template_for([v.test_id for v in flagged], reason=", ".join(sorted({f for v in flagged for f in v.flags})) or "review").rstrip(),
            "```",
            "",
        ]
    if flaky:
        lines += ["## Flaky at baseline (failed in some baseline runs but not all)", ""]
        lines += [f"- `{t}`" for t in flaky] + [""]
    if quarantined:
        lines += ["## Quarantined by an earlier decision (skipped)", ""]
        lines += [f"- `{t}`" for t in quarantined] + [""]
    run_dir.mkdir(parents=True, exist_ok=True)
    md_path = run_dir / REVIEW_MD
    md_path.write_text("\n".join(lines))
    (run_dir / REVIEW_JSON).write_text(
        json.dumps({"flagged": [v.model_dump() for v in flagged], "flaky": flaky, "quarantined": quarantined}, indent=2)
    )
    return md_path


def write_result(run_dir: Path, result: LoopResult) -> None:
    run_dir.mkdir(parents=True, exist_ok=True)
    payload = result.model_dump(mode="json")
    payload["exit_code"] = result.exit_code
    payload["estimated_cost_usd"] = round(result.estimated_cost_usd, 4)
    (run_dir / RESULT_JSON).write_text(json.dumps(payload, indent=2))
    (run_dir / PATCH).write_text(result.final_diff)


def summary_markdown(result: LoopResult) -> str:
    lines = [
        f"## testloop: {result.status.value}",
        "",
        f"- reason: {result.reason or 'n/a'}",
        f"- targets: {len(result.targets)}",
        f"- iterations: {len(result.iterations)}",
        f"- flagged for review: {len(result.flagged)}",
        f"- flaky at baseline: {len(result.flaky)}",
        f"- model: {result.model}",
        f"- tokens in/out: {result.usage.input_tokens}/{result.usage.output_tokens} (cache read {result.usage.cache_read_input_tokens})",
        f"- estimated cost: ${result.estimated_cost_usd:.2f}",
        "",
    ]
    for rec in result.iterations:
        state = "reverted" if rec.reverted else ("error" if rec.error else "kept")
        lines.append(f"- iteration {rec.number}: {state}; still failing {len(rec.still_failing)}, regressions {len(rec.regressions)}")
    if result.flagged:
        lines += ["", "### Flagged tests", ""] + [f"- `{v.test_id}`: {', '.join(v.flags)} — {v.explanation}" for v in result.flagged]
    return "\n".join(lines) + "\n"


def emit_ci_output(result: LoopResult) -> None:
    """GitHub Actions integration: step summary and annotations. Silent elsewhere."""
    summary_path = os.environ.get("GITHUB_STEP_SUMMARY")
    if summary_path:
        with open(summary_path, "a", encoding="utf-8") as fh:
            fh.write(summary_markdown(result))
    if os.environ.get("GITHUB_ACTIONS"):
        for v in result.flagged:
            file = v.test_id.split("::")[0]
            print(f"::warning file={file},title=testloop flagged {','.join(v.flags)}::{v.test_id}: {v.explanation}")
        for t in result.flaky:
            print(f"::warning file={t.split('::')[0]},title=testloop flaky::{t} failed in some baseline runs but not all")
        if result.status.value in ("aborted", "budget_exhausted"):
            print(f"::error title=testloop {result.status.value}::{result.reason}")
