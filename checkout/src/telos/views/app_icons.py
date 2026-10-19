"""macOS app icon lookup for the web timeline."""

from __future__ import annotations

import plistlib
from xml.parsers.expat import ExpatError
import re
import subprocess
from pathlib import Path

_SAFE_ID_RE = re.compile(r"[^0-9A-Za-z_.-]+")
_COMMON_APP_DIRS = (
    Path("/Applications"),
    Path.home() / "Applications",
    Path("/System/Applications"),
    Path("/System/Library/CoreServices"),
)


def app_icon_for(
    *,
    cache_dir: Path,
    bundle_id: str = "",
    app_name: str = "",
) -> Path | None:
    bundle = _clean_identifier(bundle_id)
    name = _clean_app_name(app_name)
    if not bundle and not name:
        return None

    cache_key = _cache_key(bundle=bundle, name=name)
    output = Path(cache_dir).expanduser().resolve() / f"{cache_key}.png"
    if output.is_file():
        return output

    app_path = _find_app(bundle_id=bundle, app_name=name)
    if app_path is None:
        return None
    source = _find_icon_source(app_path)
    if source is None:
        return None

    output.parent.mkdir(parents=True, exist_ok=True)
    tmp = output.with_suffix(".tmp.png")
    if _convert_icon_to_png(source, tmp):
        tmp.replace(output)
        return output
    try:
        tmp.unlink()
    except FileNotFoundError:
        pass
    return None


def _clean_identifier(value: str) -> str:
    text = str(value or "").strip()
    if len(text) > 200:
        text = text[:200]
    return text if re.fullmatch(r"[0-9A-Za-z_.-]+", text) else ""


def _clean_app_name(value: str) -> str:
    text = str(value or "").strip()
    text = text[:-4] if text.endswith(".app") else text
    text = re.sub(r'["\r\n/:\\]+', "", text)
    return text[:120]


def _cache_key(*, bundle: str, name: str) -> str:
    raw = bundle or name or "unknown"
    clean = _SAFE_ID_RE.sub("_", raw).strip("._-")
    return clean or "unknown"


def _find_app(*, bundle_id: str, app_name: str) -> Path | None:
    if bundle_id:
        found = _find_app_by_bundle_id(bundle_id)
        if found is not None:
            return found
    if app_name:
        found = _find_app_by_name(app_name)
        if found is not None:
            return found
    return None


def _mdfind_first_app(query: str) -> Path | None:
    try:
        result = subprocess.run(
            ["mdfind", query],
            text=True,
            capture_output=True,
            timeout=2,
            check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    if result.returncode != 0:
        return None
    for line in result.stdout.splitlines():
        path = Path(line).expanduser()
        if _is_app(path):
            return path
    return None


def _find_app_by_bundle_id(bundle_id: str) -> Path | None:
    return _mdfind_first_app(f'kMDItemCFBundleIdentifier == "{bundle_id}"')


def _find_app_by_name(app_name: str) -> Path | None:
    for root in _COMMON_APP_DIRS:
        candidate = root / f"{app_name}.app"
        if _is_app(candidate):
            return candidate
    return _mdfind_first_app(
        f'kMDItemContentType == "com.apple.application-bundle" '
        f'&& kMDItemDisplayName == "{app_name}"'
    )


def _is_app(path: Path) -> bool:
    return path.suffix == ".app" and path.is_dir() and (path / "Contents" / "Info.plist").is_file()


def _find_icon_source(app_path: Path) -> Path | None:
    resources = app_path / "Contents" / "Resources"
    try:
        info = plistlib.loads((app_path / "Contents" / "Info.plist").read_bytes())
    except (OSError, ValueError, TypeError, ExpatError):
        info = {}
    if not isinstance(info, dict):
        info = {}

    raw_names = [
        str(info.get("CFBundleIconFile") or "").strip(),
        str(info.get("CFBundleIconName") or "").strip(),
    ]
    candidates: list[Path] = []
    for raw in raw_names:
        if not raw:
            continue
        path = resources / raw
        candidates.append(path)
        if not path.suffix:
            candidates.append(resources / f"{raw}.icns")
            candidates.append(resources / f"{raw}.png")
    candidates.extend(sorted(resources.glob("*.icns")))
    candidates.extend(sorted(resources.glob("*.png")))

    for candidate in candidates:
        if candidate.is_file() and candidate.suffix.lower() in {".icns", ".png"}:
            return candidate
    return None


def _convert_icon_to_png(source: Path, output: Path) -> bool:
    if source.suffix.lower() == ".png":
        try:
            output.write_bytes(source.read_bytes())
            return output.is_file() and output.stat().st_size > 0
        except OSError:
            return False
    try:
        result = subprocess.run(
            ["sips", "-s", "format", "png", str(source), "--out", str(output)],
            text=True,
            capture_output=True,
            timeout=5,
            check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return False
    return result.returncode == 0 and output.is_file() and output.stat().st_size > 0
