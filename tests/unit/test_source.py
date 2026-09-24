from testloop.llm.source import extract_test_source

PY = '''import pytest

@pytest.mark.slow
def test_a():
    assert 1 == 1


class TestX:
    def test_b(self):
        x = 2
        assert x == 2

    def test_c(self):
        pass


def test_d():
    pass
'''

TS = '''describe("calc", () => {
  it("adds", () => {
    expect(add(2, 3)).toBe(5);
  });

  it("divides with true division", () => {
    expect(divide(7, 2)).toBe(3.5);
  });
});
'''


def test_python_function_with_decorator() -> None:
    src = extract_test_source(PY, "test_a", "py")
    assert src.startswith("@pytest.mark.slow\ndef test_a():") and "class TestX" not in src


def test_python_method_stops_at_sibling() -> None:
    src = extract_test_source(PY, "test_b", "py")
    assert "def test_b" in src and "assert x == 2" in src and "test_c" not in src


def test_js_it_block_by_full_name_suffix() -> None:
    src = extract_test_source(TS, "calc divides with true division", "ts")
    assert src.startswith('it("divides with true division"') and "toBe(3.5)" in src and "adds" not in src


def test_missing_returns_empty() -> None:
    assert extract_test_source(PY, "nope", "py") == ""
