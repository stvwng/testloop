from pathlib import Path

import pytest

from testloop.decisions import Decisions, load_decisions


def test_missing_file_is_empty(tmp_path: Path) -> None:
    d = load_decisions(tmp_path / ".testloop.yml")
    assert d.accepted == set() and d.quarantined == set()


def test_parses_decisions(tmp_path: Path) -> None:
    (tmp_path / ".testloop.yml").write_text(
        """
tests:
  "tests/test_a.py::test_flaky":
    decision: quarantine
    note: talks to a real clock
  "tests/test_a.py::test_ok":
    decision: accept
"""
    )
    d = load_decisions(tmp_path / ".testloop.yml")
    assert d.quarantined == {"tests/test_a.py::test_flaky"}
    assert d.accepted == {"tests/test_a.py::test_ok"}
    assert d.note("tests/test_a.py::test_flaky") == "talks to a real clock"


def test_rejects_unknown_decision(tmp_path: Path) -> None:
    (tmp_path / ".testloop.yml").write_text('tests:\n  "a::b":\n    decision: maybe\n')
    with pytest.raises(ValueError, match="maybe"):
        load_decisions(tmp_path / ".testloop.yml")


def test_render_template_for_flagged() -> None:
    text = Decisions.template_for(["tests/test_a.py::test_x"], reason="order_dependent")
    assert "tests/test_a.py::test_x" in text and "quarantine" in text
