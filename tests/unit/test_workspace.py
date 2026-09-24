import os
from pathlib import Path

import pytest

from testloop.workspace import PathOutsideRepo, Workspace, glob_to_regex


def make_ws(tmp_path: Path) -> Workspace:
    (tmp_path / "src").mkdir()
    (tmp_path / "tests").mkdir()
    (tmp_path / "src" / "calc.py").write_text("x = 1\n")
    (tmp_path / "tests" / "test_calc.py").write_text("def test(): pass\n")
    return Workspace(tmp_path, protected_globs=["tests/**", "**/conftest.py"])


@pytest.mark.parametrize(
    "pattern,path,expected",
    [
        ("tests/**", "tests/test_a.py", True),
        ("tests/**", "tests/sub/test_a.py", True),
        ("tests/**", "src/tests_helper.py", False),
        ("**/test_*.py", "test_a.py", True),
        ("**/test_*.py", "a/b/test_a.py", True),
        ("**/test_*.py", "a/b/my_test_a.py", False),
        ("**/*.test.ts", "src/x.test.ts", True),
        ("vitest.config.*", "vitest.config.mts", True),
        ("vitest.config.*", "sub/vitest.config.mts", False),
        ("**/__tests__/**", "src/__tests__/a.js", True),
    ],
)
def test_glob_matching(pattern: str, path: str, expected: bool) -> None:
    assert bool(glob_to_regex(pattern).match(path)) is expected


def test_resolve_rejects_escape(tmp_path: Path) -> None:
    ws = make_ws(tmp_path)
    with pytest.raises(PathOutsideRepo):
        ws.resolve("../outside.py")
    with pytest.raises(PathOutsideRepo):
        ws.resolve("/etc/passwd")
    assert ws.resolve("src/calc.py") == (tmp_path / "src" / "calc.py").resolve()


def test_resolve_rejects_symlink_escape(tmp_path: Path) -> None:
    ws = make_ws(tmp_path)
    outside = tmp_path.parent / "outside_target.py"
    outside.write_text("")
    os.symlink(outside, tmp_path / "src" / "link.py")
    with pytest.raises(PathOutsideRepo):
        ws.resolve("src/link.py")


def test_protected_snapshot_detects_modification_addition_and_deletion(tmp_path: Path) -> None:
    ws = make_ws(tmp_path)
    ws.snapshot_protected()
    assert ws.verify_protected_unchanged() == []
    (tmp_path / "tests" / "test_calc.py").write_text("def test(): assert False\n")
    assert ws.verify_protected_unchanged() == ["tests/test_calc.py"]
    (tmp_path / "tests" / "test_new.py").write_text("")
    (tmp_path / "src" / "conftest.py").write_text("")
    changed = ws.verify_protected_unchanged()
    assert set(changed) == {"tests/test_calc.py", "tests/test_new.py", "src/conftest.py"}
    (tmp_path / "tests" / "test_calc.py").unlink()
    assert "tests/test_calc.py" in ws.verify_protected_unchanged()


def test_tracked_writes_revert_and_diff(tmp_path: Path) -> None:
    ws = make_ws(tmp_path)
    ws.begin_iteration()
    ws.write_text("src/calc.py", "x = 2\n")
    ws.write_text("src/new.py", "print('new')\n")
    assert ws.read_text("src/calc.py") == "x = 2\n"
    diff = ws.iteration_diff()
    assert "-x = 1" in diff and "+x = 2" in diff and "src/new.py" in diff
    ws.revert_iteration()
    assert ws.read_text("src/calc.py") == "x = 1\n"
    assert not (tmp_path / "src" / "new.py").exists()
    assert ws.iteration_diff() == ""


def test_run_diff_spans_iterations(tmp_path: Path) -> None:
    ws = make_ws(tmp_path)
    ws.begin_iteration()
    ws.write_text("src/calc.py", "x = 2\n")
    ws.begin_iteration()
    ws.write_text("src/calc.py", "x = 3\n")
    assert "-x = 2" in ws.iteration_diff()
    run_diff = ws.run_diff()
    assert "-x = 1" in run_diff and "+x = 3" in run_diff and "x = 2" not in run_diff
    assert ws.changed_files() == ["src/calc.py"]


def test_list_and_grep_skip_ignored_dirs(tmp_path: Path) -> None:
    ws = make_ws(tmp_path)
    (tmp_path / "node_modules").mkdir()
    (tmp_path / "node_modules" / "junk.py").write_text("needle\n")
    (tmp_path / "src" / "calc.py").write_text("needle here\n")
    assert ws.list_files("**/*.py") == ["src/calc.py", "tests/test_calc.py"]
    hits = ws.grep("needle", "**/*.py")
    assert hits == [("src/calc.py", 1, "needle here")]
