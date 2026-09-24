import json
import subprocess
from pathlib import Path

from testloop.publish import COMMIT_MARKER, pr_body, publish_result


def git(repo: Path, *args: str) -> str:
    return subprocess.run(["git", *args], cwd=repo, capture_output=True, text=True, check=True).stdout.strip()


def make_repo(tmp_path: Path) -> Path:
    repo = tmp_path / "repo"
    repo.mkdir()
    git(repo, "init", "-q", "-b", "main")
    git(repo, "config", "user.email", "t@example.com")
    git(repo, "config", "user.name", "t")
    (repo / "calc.py").write_text("return a // b\n")
    git(repo, "add", "-A")
    git(repo, "commit", "-q", "-m", "init")
    return repo


def write_result(path: Path, status: str = "passed") -> Path:
    path.write_text(json.dumps({"status": status, "targets": ["t::a"], "iterations": [{"number": 1, "agent_summary": "fixed division"}], "model": "m", "estimated_cost_usd": 1.5, "run_dir": "/x/runs/20260924T1200Z", "flagged": [{"test_id": "t::b", "flags": ["order_dependent"], "explanation": "e"}]}))
    return path


def test_pr_body_lists_targets_flags_and_summary() -> None:
    body = pr_body(json.loads(write_result(Path("/tmp/testloop-body.json")).read_text()))
    assert "`t::a`" in body and "t::b" in body and "fixed division" in body and "Generated with" in body


def test_refuses_unfixed_without_flag(tmp_path: Path) -> None:
    repo = make_repo(tmp_path)
    assert publish_result(repo, write_result(tmp_path / "r.json", "max_iterations_reached"), None, "branch", None, None, False, True) == 2


def test_self_trigger_guard(tmp_path: Path) -> None:
    repo = make_repo(tmp_path)
    (repo / "calc.py").write_text("x\n")
    git(repo, "commit", "-qam", f"{COMMIT_MARKER} earlier fix")
    assert publish_result(repo, write_result(tmp_path / "r.json"), None, "branch", None, None, False, False) == 3


def test_branch_mode_commits_on_new_branch_without_touching_main(tmp_path: Path) -> None:
    repo = make_repo(tmp_path)
    remote = tmp_path / "remote.git"
    subprocess.run(["git", "init", "-q", "--bare", str(remote)], check=True)
    git(repo, "remote", "add", "origin", str(remote))
    git(repo, "push", "-q", "-u", "origin", "main")
    (repo / "calc.py").write_text("return a / b\n")  # the loop's change is in the working tree
    code = publish_result(repo, write_result(tmp_path / "r.json"), None, "branch", None, None, False, False)
    assert code == 0
    assert git(repo, "rev-parse", "--abbrev-ref", "HEAD") == "testloop/fix-20260924t1200z"
    assert COMMIT_MARKER in git(repo, "log", "-1", "--pretty=%B") and "Testloop-Run: 20260924T1200Z" in git(repo, "log", "-1", "--pretty=%B")
    assert git(repo, "show", "main:calc.py") == "return a // b"
    assert "testloop/fix-20260924t1200z" in subprocess.run(["git", "branch"], cwd=remote, capture_output=True, text=True).stdout


def test_patch_mode_applies_patch_first(tmp_path: Path) -> None:
    repo = make_repo(tmp_path)
    patch = tmp_path / "fix.patch"
    patch.write_text("--- a/calc.py\n+++ b/calc.py\n@@ -1 +1 @@\n-return a // b\n+return a / b\n")
    code = publish_result(repo, write_result(tmp_path / "r.json"), patch, "branch", "custom/branch", None, False, True)
    assert code == 0  # dry run: commands are only logged
