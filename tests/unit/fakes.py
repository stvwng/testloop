"""A scripted stand-in for anthropic.Anthropic covering the two surfaces the harness uses."""

from __future__ import annotations

from collections import deque
from types import SimpleNamespace
from typing import Any, Callable

from anthropic.lib.tools import ToolError


def usage(inp: int = 100, out: int = 50) -> SimpleNamespace:
    return SimpleNamespace(input_tokens=inp, output_tokens=out, cache_read_input_tokens=0, cache_creation_input_tokens=0)


class FakeMessages:
    def __init__(self, responses: list[Any]) -> None:
        self.responses: deque[Any] = deque(responses)
        self.calls: list[dict[str, Any]] = []

    def parse(self, **kwargs: Any) -> SimpleNamespace:
        self.calls.append(kwargs)
        if not self.responses:
            raise AssertionError("FakeMessages.parse called more times than scripted")
        nxt = self.responses.popleft()
        parsed = nxt(kwargs) if callable(nxt) else nxt
        return SimpleNamespace(parsed_output=parsed, usage=usage(), stop_reason="end_turn", stop_details=None)


ToolStep = tuple[str, dict[str, Any]]  # (tool name, input)


class FakeToolRunner:
    """Executes a script of tool calls against the real tool objects, then ends the turn."""

    def __init__(self, kwargs: dict[str, Any], script: list[ToolStep], final_text: str, stop_reason: str = "end_turn") -> None:
        self.kwargs = kwargs
        self.script = list(script)
        self.final_text = final_text
        self.stop_reason = stop_reason
        self.tools = {t.name: t for t in kwargs["tools"]}
        self.results: list[tuple[str, Any, bool]] = []

    def __iter__(self):
        limit = self.kwargs.get("max_iterations") or len(self.script) + 1
        for n, (name, tool_input) in enumerate(self.script):
            if n + 1 > limit:
                return
            block = SimpleNamespace(type="tool_use", name=name, input=tool_input, id=f"toolu_{n}")
            yield SimpleNamespace(content=[block], usage=usage(), stop_reason="tool_use", stop_details=None)
            try:
                out = self.tools[name].call(tool_input)
                self.results.append((name, out, False))
            except ToolError as exc:
                self.results.append((name, exc.content, True))
            except Exception as exc:  # the real runner reports repr(exc) with is_error
                self.results.append((name, repr(exc), True))
        yield SimpleNamespace(
            content=[SimpleNamespace(type="text", text=self.final_text)],
            usage=usage(),
            stop_reason=self.stop_reason,
            stop_details=SimpleNamespace(category="cyber", explanation="x") if self.stop_reason == "refusal" else None,
        )


class FakeBetaMessages:
    def __init__(self, scripts: list[Callable[[dict[str, Any]], FakeToolRunner]]) -> None:
        self.scripts: deque[Callable[[dict[str, Any]], FakeToolRunner]] = deque(scripts)
        self.calls: list[dict[str, Any]] = []
        self.runners: list[FakeToolRunner] = []

    def tool_runner(self, **kwargs: Any) -> FakeToolRunner:
        self.calls.append(kwargs)
        if not self.scripts:
            raise AssertionError("tool_runner called more times than scripted")
        runner = self.scripts.popleft()(kwargs)
        self.runners.append(runner)
        return runner


class FakeAnthropic:
    def __init__(self, parse_responses: list[Any] | None = None, agent_scripts: list[Any] | None = None) -> None:
        self.messages = FakeMessages(parse_responses or [])
        self.beta = SimpleNamespace(messages=FakeBetaMessages(agent_scripts or []))


def scripted(script: list[ToolStep], final_text: str = "done", stop_reason: str = "end_turn") -> Callable[[dict[str, Any]], FakeToolRunner]:
    return lambda kwargs: FakeToolRunner(kwargs, script, final_text, stop_reason)
