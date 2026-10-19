"""Tool protocol types."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Awaitable, Callable


class ToolInputError(ValueError):
    """Resolver explicitly reports a failure before any side effect begins."""

    def __init__(self, message: str, *, code: str = "invalid_arguments"):
        super().__init__(message)
        self.code = code


@dataclass(frozen=True)
class ToolResult:
    output: object
    is_error: bool = False
    stop_turn: bool = False
    error: str = ""
    retryable: bool = False


@dataclass(frozen=True)
class ToolInvocationContext:
    session_id: str = ""
    turn_id: str = ""
    tool_call_id: str = ""
    tool_name: str = ""


ToolHandler = Callable[[dict[str, Any]], Awaitable[object] | object]
ToolContextHandler = Callable[
    [dict[str, Any], ToolInvocationContext],
    Awaitable[object] | object,
]
ToolExecutionHandler = Callable[[], Awaitable[object] | object]
ToolApprovalPreview = Callable[[], object]
ToolResolver = Callable[
    [dict[str, Any], ToolInvocationContext],
    Awaitable["ToolExecution"] | "ToolExecution",
]


@dataclass(frozen=True)
class ToolExecution:
    """Resolved operation and approval presentation.

    A structured approval preview may provide a human-readable ``summary``.
    Adapters can display it while the full preview and args remain in the audit.
    """

    execute: ToolExecutionHandler
    need_confirm: bool = False
    approval_preview: ToolApprovalPreview | object | None = None
    description: str = ""
    display: dict[str, Any] | None = None

    async def run(self) -> object:
        result = self.execute()
        if hasattr(result, "__await__"):
            return await result  # type: ignore[no-any-return]
        return result

    def approval_preview_value(self) -> object | None:
        if callable(self.approval_preview):
            return self.approval_preview()
        return self.approval_preview


@dataclass(frozen=True)
class ExecutableTool:
    name: str
    description: str
    parameters: dict[str, Any]
    resolver: ToolResolver

    async def resolve_execution(
        self,
        args: dict[str, Any],
        *,
        context: ToolInvocationContext | None = None,
    ) -> ToolExecution:
        result = self.resolver(args, context or ToolInvocationContext())
        if hasattr(result, "__await__"):
            result = await result
        if not isinstance(result, ToolExecution):
            raise TypeError(f"Tool resolver for {self.name!r} did not return ToolExecution")
        return result


def create_executable_tool(
    *,
    name: str,
    description: str,
    parameters: dict[str, Any],
    handler: ToolHandler | None = None,
    context_handler: ToolContextHandler | None = None,
    need_confirm: bool = False,
    approval_preview: Callable[[dict[str, Any]], object] | object | None = None,
    execution_description: str = "",
    display: dict[str, Any] | None = None,
) -> ExecutableTool:
    if (handler is None) == (context_handler is None):
        raise ValueError("create_executable_tool requires exactly one handler")

    def resolver(args: dict[str, Any], context: ToolInvocationContext) -> ToolExecution:
        preview: ToolApprovalPreview | object | None
        if callable(approval_preview):
            preview = lambda: approval_preview(args)
        else:
            preview = approval_preview
        execute = (
            (lambda: context_handler(args, context))
            if context_handler is not None
            else (lambda: handler(args))
        )
        return ToolExecution(
            execute=execute,
            need_confirm=need_confirm,
            approval_preview=preview,
            description=execution_description,
            display=display,
        )

    return ExecutableTool(
        name=name,
        description=description,
        parameters=parameters,
        resolver=resolver,
    )
