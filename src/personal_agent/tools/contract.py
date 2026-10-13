"""One tool contract (ADR-0156 D6).

Every tool result reaches the model in one shape:

* success — ``{"ok": true, ...payload}``
* failure — ``{"ok": false, "error": {"code", "message", "retryable", "next_action"}}``

A tool builds a failure only by raising its own :class:`ToolError` subclass, typed with the
tool's ``Literal`` code set, so mypy refuses an undeclared code at the call site::

    FetchCode = Literal["invalid_url", "upstream_timeout"]

    class FetchError(ToolError[FetchCode]):
        ...

    raise FetchError("invalid_url", "...", retryable=False, next_action="...")

The tool's :class:`~personal_agent.tools.types.ToolDefinition` declares the same set with
``error_codes=error_codes(FetchError)``, and the execution layer refuses any other code at run
time. Failures that the layer builds, not a tool (unknown tool, a permission refusal, a
timeout), use :data:`DispatchCode`.

FRE-1575.
"""

from __future__ import annotations

import json
from collections.abc import Mapping
from typing import Generic, Literal, TypeVar, get_args

from personal_agent.llm_client.token_counter import estimate_tokens
from personal_agent.tools.types import (
    ERROR_MESSAGE_LIMIT,
    RESULT_TOKEN_CEILING,
    ToolErrorDetail,
)

CodeT = TypeVar("CodeT", bound=str)

_TRUNCATED_MARK = " [truncated]"


def bound_message(text: str) -> str:
    """Cut a model-facing error message to the 2,000-character bound (ADR-0156 D6.2).

    Args:
        text: The full message.

    Returns:
        The message unchanged, or cut with a ``[truncated]`` mark so the cut is visible.
    """
    if len(text) <= ERROR_MESSAGE_LIMIT:
        return text
    return text[: ERROR_MESSAGE_LIMIT - len(_TRUNCATED_MARK)] + _TRUNCATED_MARK


class ToolExecutionError(Exception):
    """Base class of every tool failure. Raise a typed :class:`ToolError` subclass instead."""


class ToolError(ToolExecutionError, Generic[CodeT]):
    """A tool failure with a code from the tool's closed code set (ADR-0156 D6.1).

    Subclass it once per tool with the tool's ``Literal`` code type; never raise it bare.
    """

    def __init__(self, code: CodeT, message: str, *, retryable: bool, next_action: str) -> None:
        """Build the failure.

        Args:
            code: Stable code from the tool's ``Literal`` set.
            message: What failed and why. Bounded at 2,000 characters for the model.
            retryable: Whether repeating the same call can succeed.
            next_action: The next call that can work, or that none exists.
        """
        super().__init__(message)
        self.code: str = code
        self.message = message
        self.retryable = retryable
        self.next_action = next_action

    @property
    def detail(self) -> ToolErrorDetail:
        """The model-facing failure."""
        return ToolErrorDetail(
            code=self.code,
            message=bound_message(self.message),
            retryable=self.retryable,
            next_action=bound_message(self.next_action),
        )


class TerminalToolError(ToolError[CodeT]):
    """A tool failure the model cannot recover from — short-circuit the turn (FRE-402).

    The orchestrator surfaces a classified error without a recovery LLM round trip. ``reason``
    and ``next_step`` are user-facing strings carried into the ``ClassifiedError`` in the PWA.
    """

    def __init__(self, code: CodeT, message: str, *, reason: str, next_step: str) -> None:
        """Build the terminal failure.

        Args:
            code: Stable code from the tool's ``Literal`` set.
            message: Internal error message (logs and ``ToolResult.error``).
            reason: User-facing explanation of what happened (no internals).
            next_step: Concrete guidance for what the user can do next.
        """
        super().__init__(code, message, retryable=False, next_action=next_step)
        self.reason = reason
        self.next_step = next_step


def error_codes(cls: type[ToolError[CodeT]]) -> frozenset[str]:
    """Read the closed code set from a tool's error class.

    Args:
        cls: A ``ToolError[SomeLiteral]`` subclass.

    Returns:
        Every value of the ``Literal`` that parametrises the class.

    Raises:
        TypeError: The class is not parametrised with a ``Literal`` of strings.
    """
    for klass in cls.__mro__:
        for base in getattr(klass, "__orig_bases__", ()):
            args = get_args(base)
            if args:
                codes = get_args(args[0])
                if codes and all(isinstance(code, str) for code in codes):
                    return frozenset(codes)
    raise TypeError(f"{cls.__name__} is not parametrised with a Literal of string codes")


DispatchCode = Literal[
    "unknown_tool",
    "missing_parameter",
    "invalid_arguments",
    "invalid_json_arguments",
    "mode_refused",
    "path_refused",
    "permission_denied",
    "permission_rules_unavailable",
    "approval_unavailable",
    "denied_by_user",
    "approval_timed_out",
    "not_available_to_worker",
    "tool_not_revealed",
    "loop_blocked",
    "timeout",
    "tool_exception",
    "undeclared_error_code",
    "contract_violation",
]
"""Failure codes that the execution layer and dispatch build, not a tool."""


def dispatch_error(
    code: DispatchCode, message: str, *, retryable: bool, next_action: str
) -> ToolErrorDetail:
    """Build a failure that the layer or dispatch reports, not a tool.

    Args:
        code: A :data:`DispatchCode`.
        message: What failed. Bounded at 2,000 characters.
        retryable: Whether repeating the same call can succeed.
        next_action: The next call that can work, or that none exists.

    Returns:
        The model-facing failure.
    """
    return ToolErrorDetail(
        code=code,
        message=bound_message(message),
        retryable=retryable,
        next_action=bound_message(next_action),
    )


def legacy_failure_marker(output: object) -> str | None:
    """Report why a success payload looks like a pre-contract error dict.

    A tool that returns ``{"success": False, "error": ...}`` instead of raising its error
    class breaks the contract: the layer would log a failure as a success (ADR-0156 D6.3).

    Args:
        output: A tool's returned payload.

    Returns:
        The reason the payload is not a valid success, or ``None`` when it is.
    """
    if not isinstance(output, Mapping):
        return None
    if "ok" in output:
        return "the payload sets 'ok', which only the contract renderer sets"
    if output.get("success") is False:
        return "the payload has success: false"
    if output.get("status") == "error":
        return "the payload has status: 'error'"
    if output.get("error"):
        return "the payload has a top-level 'error'"
    return None


def failure_payload(detail: ToolErrorDetail) -> dict[str, object]:
    """Return the failure shape for one error.

    Args:
        detail: The failure.

    Returns:
        ``{"ok": False, "error": {...}}``.
    """
    return {"ok": False, "error": detail.model_dump()}


def render_content(
    *,
    output: str | Mapping[str, object] | None,
    error_detail: ToolErrorDetail | None,
    max_result_tokens: int = RESULT_TOKEN_CEILING,
    truncation_next_action: str = "Narrow the request and call again.",
) -> str:
    """Build the tool message content the model reads (ADR-0156 D6.1, D6.4).

    Args:
        output: The tool's success payload. Ignored when ``error_detail`` is set.
        error_detail: The failure, or ``None`` for a success.
        max_result_tokens: The tool's result bound. Never above 25,000.
        truncation_next_action: Told to the model when the result is truncated.

    Returns:
        JSON text in the contract shape, within the token bound.
    """
    if error_detail is not None:
        return json.dumps(failure_payload(error_detail))

    payload: dict[str, object] = {"ok": True}
    if isinstance(output, Mapping):
        payload.update(output)
    elif output is not None:
        payload["result"] = output
    content = json.dumps(payload, default=str)
    return _bound_content(
        content, min(max_result_tokens, RESULT_TOKEN_CEILING), truncation_next_action
    )


def result_tokens(text: str) -> int:
    """Count a result's tokens conservatively.

    ``estimate_tokens`` uses ``cl100k_base``, an approximation for the models that read the
    result. Dense JSON can need more tokens than that count, so the count is never below one
    token per 3 characters.

    Args:
        text: The result text.

    Returns:
        The larger of the ``cl100k_base`` count and ``len(text) / 3``, rounded up.
    """
    return max(estimate_tokens(text), -(-len(text) // 3))


def _bound_content(content: str, bound: int, next_action: str) -> str:
    """Truncate a success result over its token bound, naming the next call.

    Args:
        content: The full success JSON.
        bound: The token bound.
        next_action: The next call that gets the rest, or a narrower result.

    Returns:
        ``content`` when it fits, else a valid-JSON truncation record within the bound.
    """
    tokens = result_tokens(content)
    if tokens <= bound:
        return content
    chars = int(len(content) * bound / tokens)
    while True:
        truncated = json.dumps(
            {
                "ok": True,
                "truncated": True,
                "result_tokens": tokens,
                "max_result_tokens": bound,
                "next_action": next_action,
                "partial": content[:chars],
            }
        )
        if result_tokens(truncated) <= bound or chars == 0:
            return truncated
        chars = int(chars * 0.85)
