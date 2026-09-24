import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import pytest

import calc

_seen: list[str] = []


def test_add() -> None:
    assert calc.add(2, 3) == 5


def test_divide_true_division() -> None:
    assert calc.divide(7, 2) == 3.5


def test_divide_by_zero() -> None:
    with pytest.raises(ZeroDivisionError):
        calc.divide(1, 0)


def test_slugify_records_state() -> None:
    _seen.append(calc.slugify("Hello World"))
    assert _seen[-1] == "hello-world"


def test_order_dependent_on_previous_test() -> None:
    # Relies on test_slugify_records_state having run first.
    assert _seen == ["hello-world"]
