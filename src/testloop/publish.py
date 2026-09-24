"""Opt-in delivery step for CI: commit the fix on a new branch and open a pull request.

Deliberately separate from the loop so a pipeline can inspect result.json before anything
leaves the runner. Never pushes to the branch it started on, and refuses to run on a commit
it authored itself, which stops the loop from re-triggering on its own push.
"""

from __future__ import annotations

import json
import logging
import shlex
import subprocess
from pathlib import Path

log = logging.getLogger(__name__)

COMMIT_MARKER = "[testloop]"
TRAILER = "Testloop-Run"


class PublishError(RuntimeError):
    pass


def _git(repo: Path, *args: str, dry_run: bool = False, check: bool = True) -> str:
    argv = ["git", *args]
    log.info("$ %s", shlex.join(argv))
    if dry_run:
        return ""
    proc = subprocess.run(argv, cwd=repo, capture_output=True, text=True, check=False)
    if check and proc.returncode != 0:
        raise PublishError(f"{shlex.join(argv)} failed: {proc.stderr.strip() or proc.stdout.strip()}")
    return proc.stdout.strip()


def pr_body(result: dict) -> str:
    lines = [
        f"Automated fix by testloop (`{result.get('status')}`).",
        "",
        f"- targets: {', '.join(f'`{t}`' for t in result.get('targets', [])) or 'none'}",
        f"- iterations: {len(result.get('iterations', []))}",
        f"- model: {result.get('model')}",
        f"- estimated cost: ${result.get('estimated_cost_usd', 0):.2f}",
    ]
    flagged = result.get("flagged") or []
    if flagged:
        lines += ["", "### Tests flagged for human review (not touched by the agent)", ""]
        lines += [f"- `{v['test_id']}`: {', '.join(v.get('flags', []))} — {v.get('explanation', '')}" for v in flagged]
    for rec in result.get("iterations", []):
        if rec.get("agent_summary"):
            lines += ["", f"### Iteration {rec['number']}", "", rec["agent_summary"]]
    lines += ["", "🤖 Generated with [Claude Code](https://claude.com/claude-code)"]
    return "\n".join(lines)


def publish_result(
    repo_path: Path,
    result_json: Path,
    patch: Path | None,
    mode: str,
    branch_name: str | None,
    base: str | None,
    allow_unfixed: bool,
    dry_run: bool,
) -> int:
    result = json.loads(result_json.read_text())
    status = result.get("status")
    if status != "passed" and not allow_unfixed:
        log.error("loop status is %r; pass --allow-unfixed to publish partial progress", status)
        return 2
    if status in ("nothing_to_fix", "aborted"):
        log.info("nothing to publish for status %r", status)
        return 0

    head_msg = _git(repo_path, "log", "-1", "--pretty=%B", dry_run=dry_run, check=False)
    if COMMIT_MARKER in head_msg:
        log.error("HEAD was authored by testloop; refusing to publish on top of it (self-trigger guard)")
        return 3

    current = base or _git(repo_path, "rev-parse", "--abbrev-ref", "HEAD", dry_run=dry_run) or "main"
    run_id = Path(result.get("run_dir") or "run").name
    branch = branch_name or f"testloop/fix-{run_id.lower()}"

    if patch:
        _git(repo_path, "apply", "--index", str(patch), dry_run=dry_run)
    _git(repo_path, "checkout", "-b", branch, dry_run=dry_run)
    _git(repo_path, "add", "-A", dry_run=dry_run)
    if not dry_run and not _git(repo_path, "diff", "--cached", "--name-only"):
        log.error("no changes to commit")
        return 2
    targets = result.get("targets", [])
    title = f"{COMMIT_MARKER} fix {len(targets)} failing test(s)"
    message = f"{title}\n\n{pr_body(result)}\n\n{TRAILER}: {run_id}\n"
    _git(repo_path, "-c", "user.name=testloop", "-c", "user.email=testloop@users.noreply.github.com", "commit", "-q", "-m", message, dry_run=dry_run)
    _git(repo_path, "push", "-u", "origin", branch, dry_run=dry_run)
    log.info("pushed %s", branch)
    if mode == "pull-request":
        argv = ["gh", "pr", "create", "--base", current, "--head", branch, "--title", title, "--body", pr_body(result)]
        log.info("$ %s", shlex.join(argv))
        if not dry_run:
            proc = subprocess.run(argv, cwd=repo_path, capture_output=True, text=True, check=False)
            if proc.returncode != 0:
                raise PublishError(f"gh pr create failed: {proc.stderr.strip()}")
            log.info("opened %s", proc.stdout.strip())
    return 0
