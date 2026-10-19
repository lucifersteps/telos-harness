"""Helpers for model-authored files under memory_space://memory."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path


MEMORY_ENTRY_FILENAME = "memory.md"


@dataclass(frozen=True)
class MemoryFileDescription:
    description: str


def expected_memory_file_uri(path: Path, *, memory_dir: Path) -> str:
    relative = _lexical_relative_to(path, memory_dir)
    return f"memory_space://memory/{relative.as_posix()}"


def is_memory_entry_file(path: Path, *, memory_dir: Path) -> bool:
    try:
        relative = _lexical_relative_to(path, memory_dir)
    except ValueError:
        return False
    return relative.as_posix() == MEMORY_ENTRY_FILENAME


def validate_memory_file_content(
    content: str,
    *,
    path: Path,
    memory_dir: Path,
) -> str:
    if is_memory_entry_file(path, memory_dir=memory_dir):
        return ""
    expected_uri = expected_memory_file_uri(path, memory_dir=memory_dir)
    parsed, error = parse_memory_file_description(content)
    if error:
        return (
            f"{expected_uri} must start with a YAML header containing "
            f"description: {error}"
        )
    return ""


def render_memory_file_descriptions(memory_space_dir: Path | None) -> str:
    if memory_space_dir is None:
        return "(memory space is not configured)"
    memory_dir = Path(memory_space_dir) / "memory"
    if not memory_dir.exists():
        return "(empty)"
    files = [
        path
        for path in sorted(
            memory_dir.rglob("*"),
            key=lambda item: item.relative_to(memory_dir).as_posix(),
        )
        if path.is_file() and not is_memory_entry_file(path, memory_dir=memory_dir)
    ]
    if not files:
        return "(empty)"
    return "\n".join(
        _render_memory_file_description(path, memory_dir=memory_dir)
        for path in files
    )


def parse_memory_file_description(
    content: str,
) -> tuple[MemoryFileDescription | None, str]:
    header, error = _front_matter(content)
    if error:
        return None, error
    values, error = _parse_header_values(header)
    if error:
        return None, error
    description = _clean_scalar(values.get("description", ""))
    if not description:
        return None, "missing description"
    return MemoryFileDescription(description=description), ""


def _render_memory_file_description(path: Path, *, memory_dir: Path) -> str:
    uri = expected_memory_file_uri(path, memory_dir=memory_dir)
    try:
        content = path.read_text(encoding="utf-8")
    except UnicodeDecodeError:
        description = "(invalid memory file header: file is not UTF-8 text)"
    except OSError as error:
        description = f"(invalid memory file header: {error})"
    else:
        parsed, error = parse_memory_file_description(content)
        if parsed is None:
            description = f"(invalid memory file header: {error})"
        else:
            description = parsed.description
    return f"- path: {uri}\n  description: {description}"


def _front_matter(content: str) -> tuple[list[str], str]:
    text = str(content or "").replace("\r\n", "\n").replace("\r", "\n")
    lines = text.split("\n")
    if not lines or lines[0].strip() != "---":
        return [], "missing opening ---"
    header: list[str] = []
    for line in lines[1:]:
        if line.strip() == "---":
            return header, ""
        header.append(line)
    return [], "missing closing ---"


def _parse_header_values(lines: list[str]) -> tuple[dict[str, str], str]:
    values: dict[str, str] = {}
    index = 0
    while index < len(lines):
        line = lines[index]
        stripped = line.strip()
        if not stripped or stripped.startswith("#"):
            index += 1
            continue
        if line.startswith((" ", "\t")):
            return {}, f"unexpected indented line: {stripped}"
        if ":" not in line:
            return {}, f"invalid YAML line: {stripped}"
        key, raw_value = line.split(":", 1)
        key = key.strip()
        raw_value = raw_value.strip()
        if key != "description":
            index += 1
            continue
        if raw_value in {">", "|"}:
            value_lines: list[str] = []
            index += 1
            while index < len(lines):
                nested = lines[index]
                if nested.strip() and not nested.startswith((" ", "\t")):
                    break
                value_lines.append(nested.strip())
                index += 1
            values[key] = " ".join(part for part in value_lines if part).strip()
            continue
        values[key] = _unquote(raw_value)
        index += 1
    return values, ""


def _clean_scalar(value: str) -> str:
    return " ".join(str(value or "").split()).strip()


def _unquote(value: str) -> str:
    text = str(value or "").strip()
    if len(text) >= 2 and text[0] == text[-1] and text[0] in {"'", '"'}:
        return text[1:-1]
    return text


def _lexical_relative_to(path: Path, root: Path) -> Path:
    base = Path(root).expanduser().absolute()
    candidate = Path(path).expanduser()
    if not candidate.is_absolute():
        candidate = base / candidate
    return candidate.relative_to(base)
