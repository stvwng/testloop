"""Tiny calculator used as a harness fixture. Contains one deliberate bug."""


def add(a: float, b: float) -> float:
    return a + b


def divide(a: float, b: float) -> float:
    if b == 0:
        raise ZeroDivisionError("cannot divide by zero")
    # BUG: integer division truncates; tests expect true division.
    return a // b


def slugify(text: str) -> str:
    return "-".join(text.lower().split())
