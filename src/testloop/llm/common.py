"""Helpers shared by the three model-facing components."""

from __future__ import annotations

import logging
from typing import Any

import anthropic

from testloop.models import Usage

log = logging.getLogger(__name__)

MAX_TOKENS = 16000


def usage_from(response: Any) -> Usage:
    u = getattr(response, "usage", None)
    if u is None:
        return Usage(calls=1)
    return Usage(
        input_tokens=getattr(u, "input_tokens", 0) or 0,
        output_tokens=getattr(u, "output_tokens", 0) or 0,
        cache_read_input_tokens=getattr(u, "cache_read_input_tokens", 0) or 0,
        cache_creation_input_tokens=getattr(u, "cache_creation_input_tokens", 0) or 0,
        calls=1,
    )


class LLMCallError(RuntimeError):
    """Wraps SDK errors with the component name so run logs say which step failed."""


def describe_api_error(component: str, exc: Exception) -> LLMCallError:
    # Most-specific first so retryable and non-retryable failures read differently in logs.
    if isinstance(exc, anthropic.RateLimitError):
        msg = f"{component}: rate limited by the API ({exc.message})"
    elif isinstance(exc, anthropic.AuthenticationError):
        msg = f"{component}: authentication failed; set ANTHROPIC_API_KEY or run `ant auth login`"
    elif isinstance(exc, anthropic.BadRequestError):
        msg = f"{component}: bad request ({exc.message})"
    elif isinstance(exc, anthropic.APIStatusError):
        msg = f"{component}: API error {exc.status_code} ({exc.message})"
    elif isinstance(exc, anthropic.APIConnectionError):
        msg = f"{component}: could not reach the API ({exc})"
    else:
        msg = f"{component}: {exc!r}"
    log.error(msg, exc_info=exc)
    return LLMCallError(msg)


def truncate(text: str, limit: int, marker: str = "\n... [truncated by testloop] ...\n") -> str:
    if len(text) <= limit:
        return text
    head = limit * 2 // 3
    return text[:head] + marker + text[-(limit - head) :]
