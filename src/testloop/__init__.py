"""testloop: an LLM engineering loop that fixes source code until a test suite passes."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from testloop.config import LoopConfig
from testloop.models import LoopResult, LoopStatus

if TYPE_CHECKING:
    from testloop.loop import EngineeringLoop

__all__ = ["EngineeringLoop", "LoopConfig", "LoopResult", "LoopStatus"]


def __getattr__(name: str) -> Any:
    # Lazy import keeps `import testloop` cheap and avoids an import cycle with loop.py.
    if name == "EngineeringLoop":
        from testloop.loop import EngineeringLoop

        return EngineeringLoop
    raise AttributeError(name)
