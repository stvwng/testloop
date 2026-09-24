from pathlib import Path

from testloop.guard import WriteGuard
from testloop.workspace import Workspace


def make_guard(tmp_path: Path, extra: list[str] | None = None) -> WriteGuard:
    ws = Workspace(tmp_path, protected_globs=[])
    return WriteGuard(
        workspace=ws,
        test_file_globs=["tests/**", "**/test_*.py", "**/*.test.ts", "**/conftest.py"],
        config_file_globs=["pytest.ini", "pyproject.toml", "package.json", "vitest.config.*"],
        extra_protected_globs=extra or [],
    )


def test_allows_ordinary_source(tmp_path: Path) -> None:
    assert make_guard(tmp_path).check("src/calc.py") is None


def test_denies_test_files_and_config(tmp_path: Path) -> None:
    guard = make_guard(tmp_path)
    for path in ["tests/test_calc.py", "src/test_util.py", "app/x.test.ts", "deep/conftest.py", "pyproject.toml", "vitest.config.ts"]:
        denial = guard.check(path)
        assert denial is not None, path
        assert path in denial.message


def test_denies_escapes_and_tool_dirs(tmp_path: Path) -> None:
    guard = make_guard(tmp_path)
    assert guard.check("../evil.py") is not None
    assert guard.check(".git/hooks/pre-commit") is not None
    assert guard.check("node_modules/x/index.js") is not None
    assert guard.check(".venv/lib/x.py") is not None
    assert guard.check(".testloop.yml") is not None


def test_extra_protected_globs(tmp_path: Path) -> None:
    guard = make_guard(tmp_path, extra=["src/fixtures/**"])
    assert guard.check("src/fixtures/golden.json") is not None
    assert guard.check("src/other.json") is None


def test_all_protected_globs_exposed_for_snapshot(tmp_path: Path) -> None:
    guard = make_guard(tmp_path, extra=["src/fixtures/**"])
    assert "src/fixtures/**" in guard.protected_globs and "tests/**" in guard.protected_globs
