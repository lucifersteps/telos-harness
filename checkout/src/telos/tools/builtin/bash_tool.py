"""Config-scoped Bash tool."""

from __future__ import annotations

import asyncio
import os
import signal
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from telos.agent.tools.types import ExecutableTool, ToolResult, create_executable_tool
from telos.memory_space.paths import is_memory_space_uri
from telos.tools.builtin._options import resolve_env_ref
from telos.tools.builtin._paths import (
    configured_allowed_roots,
    configured_deny_patterns,
    is_denied_by_patterns,
    resolve_configured_path,
)

DEFAULT_TIMEOUT_SECONDS = 60
MAX_TIMEOUT_SECONDS = 300
DEFAULT_MAX_OUTPUT_BYTES = 100 * 1024
SIGTERM_GRACE_SECONDS = 5
READ_CHUNK_BYTES = 8192


@dataclass(frozen=True)
class BashAccessPolicy:
    allowed_roots: list[Path]
    default_cwd: Path
    deny_patterns: tuple[str, ...]
    memory_space_root: Path | None = None
    default_timeout_seconds: int = DEFAULT_TIMEOUT_SECONDS
    max_timeout_seconds: int = MAX_TIMEOUT_SECONDS
    max_output_bytes: int = DEFAULT_MAX_OUTPUT_BYTES
    shell_path: str = "/bin/bash"


@dataclass(frozen=True)
class _CapturedOutput:
    data: bytes
    truncated_bytes: int = 0


def create_bash_tool(
    *,
    repo_root: Path | None = None,
    data_dir: Path | None = None,
    memory_space_dir: Path | None = None,
    options: dict[str, Any] | None = None,
) -> ExecutableTool:
    policy = _bash_access_policy(
        repo_root=repo_root,
        data_dir=data_dir,
        memory_space_dir=memory_space_dir,
        options=options or {},
    )
    return create_executable_tool(
        name="Bash",
        description=(
            "在配置好的本地工作目录中执行 Bash 命令。仅在需要 shell 语义时使用，例如"
            "管道、环境变量、进程、git、包管理器、构建/测试命令或多步骤命令。每次调用"
            "都会在新的非交互 shell 中运行；stdout 和 stderr 会合并返回。不支持后台执行，"
            "会自动注入 MEMORY_SPACE 环境变量指向当前 memory space；不支持 memory_space:// URI。"
        ),
        parameters={
            "type": "object",
            "properties": {
                "command": {
                    "type": "string",
                    "minLength": 1,
                    "description": "要执行的 Bash 命令。不能包含 memory_space:// URI。",
                },
                "cwd": {
                    "type": "string",
                    "description": (
                        "工作目录。相对路径会基于配置的默认 cwd 解析，并且必须位于允许的 "
                        "Bash 根目录内。未配置 cwd 时默认使用第一个允许根目录。"
                        "不支持 memory_space:// URI。"
                    ),
                },
                "timeout": {
                    "type": "integer",
                    "minimum": 1,
                    "maximum": policy.max_timeout_seconds,
                    "description": (
                        "超时时间，单位为秒。"
                        f"默认 {policy.default_timeout_seconds} 秒，"
                        f"最大 {policy.max_timeout_seconds} 秒。"
                    ),
                },
            },
            "required": ["command"],
            "additionalProperties": False,
        },
        handler=lambda args: run_bash_command(
            policy,
            command=str(args.get("command") or ""),
            cwd=_optional_str(args.get("cwd")),
            timeout_seconds=args.get("timeout"),
        ),
        need_confirm=True,
        approval_preview=lambda args: _approval_preview(policy, args),
    )


async def run_bash_command(
    policy: BashAccessPolicy,
    *,
    command: str,
    cwd: str | None = None,
    timeout_seconds: object = None,
) -> ToolResult:
    command = str(command or "")
    if not command:
        return ToolResult(output="Command cannot be empty.", is_error=True)
    if _contains_memory_space_uri(command):
        return ToolResult(
            output=(
                "Bash does not support memory_space:// URIs. "
                "Use Read, Write, or Edit for memory space paths."
            ),
            is_error=True,
        )
    try:
        effective_cwd = _resolve_bash_cwd(policy, cwd)
        timeout = _normalize_timeout(policy, timeout_seconds)
    except ValueError as error:
        return ToolResult(output=str(error), is_error=True)

    if not effective_cwd.exists():
        return ToolResult(
            output=f"Working directory does not exist: {effective_cwd}",
            is_error=True,
        )
    if not effective_cwd.is_dir():
        return ToolResult(
            output=f"Working directory is not a directory: {effective_cwd}",
            is_error=True,
        )

    env = _noninteractive_env(
        policy.shell_path,
        memory_space_root=policy.memory_space_root,
    )
    try:
        process = await asyncio.create_subprocess_exec(
            policy.shell_path,
            "-c",
            command,
            cwd=str(effective_cwd),
            env=env,
            stdin=asyncio.subprocess.DEVNULL,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.STDOUT,
            start_new_session=True,
        )
    except OSError as error:
        return ToolResult(output=f"Failed to start Bash: {error}", is_error=True)

    assert process.stdout is not None
    reader = asyncio.create_task(
        _capture_output(process.stdout, max_bytes=policy.max_output_bytes)
    )
    timed_out = False
    try:
        exit_code = await asyncio.wait_for(process.wait(), timeout=timeout)
    except TimeoutError:
        timed_out = True
        await _terminate_process(process)
        exit_code = process.returncode
    finally:
        captured = await reader

    output = _format_output(captured, max_bytes=policy.max_output_bytes)
    if timed_out:
        return ToolResult(
            output=_append_status(output, f"Command killed by timeout ({timeout}s)"),
            is_error=True,
        )
    if exit_code is None:
        return ToolResult(
            output=_append_status(output, "Command ended without an exit code."),
            is_error=True,
        )
    if exit_code != 0:
        if not output:
            output = f"Process exited with code {exit_code}"
        return ToolResult(
            output=_append_status(output, f"Command failed with exit code: {exit_code}"),
            is_error=True,
        )
    return ToolResult(output=output or "Command executed successfully.")


async def _capture_output(
    stream: asyncio.StreamReader,
    *,
    max_bytes: int,
) -> _CapturedOutput:
    buffer = bytearray()
    truncated = 0
    while True:
        chunk = await stream.read(READ_CHUNK_BYTES)
        if not chunk:
            break
        remaining = max(0, max_bytes - len(buffer))
        if remaining:
            buffer.extend(chunk[:remaining])
        if len(chunk) > remaining:
            truncated += len(chunk) - remaining
    return _CapturedOutput(bytes(buffer), truncated_bytes=truncated)


async def _terminate_process(process: asyncio.subprocess.Process) -> None:
    if process.returncode is not None:
        return
    _signal_process(process, signal.SIGTERM)
    try:
        await asyncio.wait_for(process.wait(), timeout=SIGTERM_GRACE_SECONDS)
        return
    except TimeoutError:
        pass
    if process.returncode is None:
        _signal_process(process, signal.SIGKILL)
        await process.wait()


def _signal_process(process: asyncio.subprocess.Process, sig: signal.Signals) -> None:
    try:
        os.killpg(process.pid, sig)
    except ProcessLookupError:
        return
    except PermissionError:
        return
    except OSError:
        try:
            process.send_signal(sig)
        except ProcessLookupError:
            return


def _noninteractive_env(
    shell_path: str,
    *,
    memory_space_root: Path | None = None,
) -> dict[str, str]:
    env = dict(os.environ)
    env.update(
        {
            "NO_COLOR": "1",
            "TERM": "dumb",
            "GIT_TERMINAL_PROMPT": os.environ.get("GIT_TERMINAL_PROMPT") or "0",
            "SHELL": shell_path,
        }
    )
    if memory_space_root is not None:
        env["MEMORY_SPACE"] = str(memory_space_root)
    return env


def _format_output(captured: _CapturedOutput, *, max_bytes: int) -> str:
    output = captured.data.decode("utf-8", errors="replace")
    if captured.truncated_bytes <= 0:
        return output
    marker = (
        f"<system>Output truncated after {max_bytes} bytes; "
        f"{captured.truncated_bytes} bytes omitted.</system>"
    )
    return _append_status(output, marker)


def _append_status(output: str, line: str) -> str:
    if not output:
        return line
    separator = "" if output.endswith("\n") else "\n"
    return f"{output}{separator}{line}"


def _approval_preview(policy: BashAccessPolicy, args: dict[str, Any]) -> dict[str, object]:
    cwd = _resolve_bash_cwd(policy, _optional_str(args.get("cwd")))
    timeout = _normalize_timeout(policy, args.get("timeout"))
    return {
        "command": str(args.get("command") or ""),
        "cwd": str(cwd),
        "timeout_seconds": timeout,
        "shell": policy.shell_path,
    }


def _resolve_bash_cwd(policy: BashAccessPolicy, cwd: str | None) -> Path:
    raw_path = str(cwd or "").strip()
    if raw_path and is_memory_space_uri(raw_path):
        raise ValueError(
            "Bash does not support memory_space:// URIs. "
            "Use Read, Write, or Edit for memory space paths."
        )
    if raw_path:
        resolved_ref = resolve_env_ref(raw_path)
        if is_memory_space_uri(resolved_ref):
            raise ValueError(
                "Bash does not support memory_space:// URIs. "
                "Use Read, Write, or Edit for memory space paths."
            )
        candidate = Path(resolved_ref).expanduser()
        if not candidate.is_absolute():
            candidate = policy.default_cwd / candidate
    else:
        candidate = policy.default_cwd
    resolved = candidate.resolve()
    for root in policy.allowed_roots:
        try:
            relative = resolved.relative_to(root)
        except ValueError:
            continue
        _ensure_cwd_allowed(relative, policy)
        return resolved
    raise ValueError("Working directory must stay inside an allowed Bash root")


def _ensure_cwd_allowed(relative_path: Path, policy: BashAccessPolicy) -> None:
    if is_denied_by_patterns(relative_path, policy.deny_patterns):
        raise ValueError(f"Cannot use denied Bash working directory: {relative_path}")


def _bash_access_policy(
    *,
    repo_root: Path | None,
    data_dir: Path | None,
    memory_space_dir: Path | None,
    options: dict[str, Any],
) -> BashAccessPolicy:
    roots = configured_allowed_roots(
        options,
        repo_root=repo_root,
        data_dir=data_dir,
        option_label="Bash allowed path",
    ) or _default_allowed_roots(repo_root=repo_root, data_dir=data_dir)
    default_cwd = _configured_default_cwd(
        options,
        roots=roots,
        repo_root=repo_root,
        data_dir=data_dir,
    )
    policy = BashAccessPolicy(
        allowed_roots=roots,
        default_cwd=default_cwd,
        deny_patterns=tuple(configured_deny_patterns(options)),
        memory_space_root=memory_space_dir.expanduser().resolve()
        if memory_space_dir is not None
        else None,
        default_timeout_seconds=_option_int(
            options,
            ("timeout_default", "default_timeout", "default_timeout_seconds"),
            default=DEFAULT_TIMEOUT_SECONDS,
            minimum=1,
            maximum=MAX_TIMEOUT_SECONDS,
        ),
        max_timeout_seconds=_option_int(
            options,
            ("timeout_max", "max_timeout", "max_timeout_seconds"),
            default=MAX_TIMEOUT_SECONDS,
            minimum=1,
            maximum=MAX_TIMEOUT_SECONDS,
        ),
        max_output_bytes=_option_int(
            options,
            ("max_output_bytes", "output_max_bytes"),
            default=DEFAULT_MAX_OUTPUT_BYTES,
            minimum=1024,
            maximum=10 * 1024 * 1024,
        ),
        shell_path=_configured_shell_path(options),
    )
    if policy.default_timeout_seconds > policy.max_timeout_seconds:
        raise ValueError("Bash default timeout cannot exceed max timeout")
    _resolve_bash_cwd(policy, None)
    return policy


def _default_allowed_roots(
    *,
    repo_root: Path | None,
    data_dir: Path | None,
) -> list[Path]:
    roots: list[Path] = []
    for root in (repo_root, data_dir):
        if root is None:
            continue
        resolved = root.expanduser().resolve()
        if resolved not in roots:
            roots.append(resolved)
    if not roots:
        raise ValueError("Bash requires repo_root or data_dir")
    return roots


def _configured_default_cwd(
    options: dict[str, Any],
    *,
    roots: list[Path],
    repo_root: Path | None,
    data_dir: Path | None,
) -> Path:
    value = str(options.get("cwd") or "")
    if not value.strip():
        return roots[0]
    return resolve_configured_path(
        value,
        repo_root=repo_root,
        data_dir=data_dir,
        default_base=roots[0],
        option_label="Bash cwd",
    )


def _configured_shell_path(options: dict[str, Any]) -> str:
    configured = str(options.get("shell") or "").strip()
    if configured:
        return configured
    if Path("/bin/bash").exists():
        return "/bin/bash"
    return "bash"


def _normalize_timeout(policy: BashAccessPolicy, value: object) -> int:
    if value is None or value == "":
        return policy.default_timeout_seconds
    if isinstance(value, bool):
        raise ValueError("timeout must be an integer number of seconds")
    try:
        timeout = int(value)
    except (TypeError, ValueError) as error:
        raise ValueError("timeout must be an integer number of seconds") from error
    if timeout <= 0:
        raise ValueError("timeout must be positive")
    if timeout > policy.max_timeout_seconds:
        raise ValueError(f"timeout must be <= {policy.max_timeout_seconds}s")
    return timeout


def _option_int(
    options: dict[str, Any],
    keys: tuple[str, ...],
    *,
    default: int,
    minimum: int,
    maximum: int,
) -> int:
    value: object = None
    for key in keys:
        if options.get(key) is not None:
            value = options.get(key)
            break
    if value is None:
        return default
    if isinstance(value, bool):
        raise ValueError(f"{keys[0]} must be an integer")
    try:
        parsed = int(value)
    except (TypeError, ValueError) as error:
        raise ValueError(f"{keys[0]} must be an integer") from error
    if parsed < minimum:
        raise ValueError(f"{keys[0]} must be >= {minimum}")
    if parsed > maximum:
        raise ValueError(f"{keys[0]} must be <= {maximum}")
    return parsed


def _optional_str(value: object) -> str | None:
    if value is None:
        return None
    text = str(value).strip()
    return text or None


def _contains_memory_space_uri(value: str) -> bool:
    return "memory_space://" in str(value or "")
