"""Initialize user-editable Telos config for desktop/web entrypoints."""

from __future__ import annotations

import shutil
import sys
import tempfile
from pathlib import Path

from telos.storage.paths import find_repo_root


def default_user_config_dir() -> Path:
    return (Path.home() / ".telos" / "configs").expanduser().resolve()


def ensure_user_config(config_dir: str | Path | None = None) -> Path:
    """Create a first-run user config under ``~/.telos/configs`` if needed."""

    target = Path(config_dir).expanduser().resolve() if config_dir else default_user_config_dir()
    main_config = target / "telos.yaml"
    if main_config.exists():
        return target

    target.mkdir(parents=True, exist_ok=True)
    source_root = _find_default_source_root()
    if source_root is None:
        _write_minimal_config(target)
        return target

    _copy_dir_if_exists(source_root / "profiles", target / "profiles")
    _copy_dir_if_exists(source_root / "skills", target / "skills")
    (target / "agents").mkdir(parents=True, exist_ok=True)
    (target / "skills").mkdir(parents=True, exist_ok=True)

    _write_text_atomic(target / "telos.yaml", _render_root_config(target))

    source_agent = source_root / "configs" / "agents" / "main.yaml"
    if source_agent.exists():
        agent_text = _render_agent_config(source_agent.read_text(encoding="utf-8"), target)
    else:
        agent_text = _minimal_agent_config(target)
    _write_text_atomic(target / "agents" / "main.yaml", agent_text)
    return target


def _find_default_source_root() -> Path | None:
    candidates: list[Path] = []
    meipass = getattr(sys, "_MEIPASS", "")
    if meipass:
        candidates.append(Path(str(meipass)))
    candidates.append(find_repo_root())

    for candidate in candidates:
        root = candidate.expanduser().resolve()
        if (root / "configs" / "telos.yaml").exists():
            return root
    return None


def _render_root_config(target: Path) -> str:
    return "\n".join(
        [
            "enabled_agents:",
            "  - main",
            "",
            "agents:",
            f"  main: {target / 'agents' / 'main.yaml'}",
            "",
        ]
    )


def _indent_of(line: str) -> str:
    return line[: len(line) - len(line.lstrip(" "))]


def _render_agent_config(text: str, target: Path) -> str:
    lines: list[str] = []
    for line in text.splitlines():
        stripped = line.strip()
        if stripped.startswith("prompt_template:") and "system_prompt.md" in stripped:
            lines.append(
                f"{_indent_of(line)}prompt_template: "
                f"{target / 'profiles' / 'system_prompt.md'}"
            )
        elif stripped.startswith("cwd:") and stripped != "cwd:":
            lines.append(f"{_indent_of(line)}cwd: {target.parent}")
        elif stripped.startswith("- ") and stripped.removeprefix("- ").rstrip("/").endswith(
            "skills"
        ):
            lines.append(f"{_indent_of(line)}- {target / 'skills'}")
        else:
            lines.append(line)
    return "\n".join(lines).rstrip() + "\n"


def _copy_dir_if_exists(source: Path, target: Path) -> None:
    if not source.exists():
        return
    if target.exists():
        return
    shutil.copytree(source, target, ignore=shutil.ignore_patterns("__pycache__", ".DS_Store"))


def _write_minimal_config(target: Path) -> None:
    (target / "agents").mkdir(parents=True, exist_ok=True)
    (target / "profiles").mkdir(parents=True, exist_ok=True)
    (target / "skills").mkdir(parents=True, exist_ok=True)
    _write_text_atomic(
        target / "profiles" / "system_prompt.md",
        "你是 Telos，一个简洁、高效的个人生活助理。\n",
    )
    _write_text_atomic(target / "telos.yaml", _render_root_config(target))
    _write_text_atomic(target / "agents" / "main.yaml", _minimal_agent_config(target))


def _minimal_agent_config(target: Path) -> str:
    return "\n".join(
        [
            "name: telos",
            "description: 默认 Telos 主代理",
            "memory_space: defaults",
            "",
            "adapters:",
            "  enabled:",
            "    - web",
            "",
            "agent_loop:",
            "  max_steps: 80",
            "",
            "system_prompt:",
            "  enabled: true",
            f"  prompt_template: {target / 'profiles' / 'system_prompt.md'}",
            "",
            "providers:",
            "  default: fake",
            "  fake:",
            "    type: fake",
            "    model: fake-echo",
            "",
            "tools:",
            "  policy:",
            "    mode: custom",
            "  entries: {}",
            "",
            "skills:",
            "  enabled: false",
            "  roots:",
            f"    - {target / 'skills'}",
            "",
            "mcp:",
            "  enabled: false",
            "  servers:",
            "",
        ]
    )


def _write_text_atomic(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile("w", encoding="utf-8", dir=str(path.parent), delete=False) as tmp:
        tmp.write(text)
        tmp_path = Path(tmp.name)
    tmp_path.replace(path)
