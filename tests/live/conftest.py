import os
import shutil
import subprocess
from pathlib import Path

import pytest

FIXTURES = Path(__file__).resolve().parents[1] / "fixtures"


def _has_credentials() -> bool:
    if os.environ.get("ANTHROPIC_API_KEY") or os.environ.get("ANTHROPIC_AUTH_TOKEN"):
        return True
    try:
        return subprocess.run(["ant", "auth", "status"], capture_output=True, text=True, timeout=10).returncode == 0
    except (FileNotFoundError, subprocess.TimeoutExpired):
        return False


@pytest.fixture(scope="session", autouse=True)
def _require_credentials() -> None:
    if not _has_credentials():
        pytest.skip("live tests need ANTHROPIC_API_KEY or an `ant auth login` profile")


@pytest.fixture
def py_calc(tmp_path: Path) -> Path:
    dest = tmp_path / "py_calc"
    shutil.copytree(FIXTURES / "py_calc", dest)
    return dest


@pytest.fixture
def ts_calc(tmp_path: Path) -> Path:
    if shutil.which("npx") is None:
        pytest.skip("node/npx not available")
    dest = tmp_path / "ts_calc"
    shutil.copytree(FIXTURES / "ts_calc", dest)
    subprocess.run(["npm", "install", "--silent", "--no-audit", "--no-fund"], cwd=dest, check=True, timeout=300)
    return dest
