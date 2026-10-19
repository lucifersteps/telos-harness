"""Lightweight visual difference metrics for timeline screenshots."""

from __future__ import annotations

from dataclasses import dataclass, replace
from pathlib import Path
from typing import Any


DEFAULT_LONG_EDGE = 384
DEFAULT_PIXEL_THRESHOLD = 12
DEFAULT_BLOCK_SIZE = 16
DEFAULT_BLOCK_MAE_THRESHOLD = 0.02
DEFAULT_BLOCK_CHANGED_RATIO_THRESHOLD = 0.08
DEFAULT_SIGNIFICANT_MAE = 0.020
DEFAULT_SIGNIFICANT_CHANGED_RATIO = 0.13
DEFAULT_SIGNIFICANT_BLOCK_RATIO = 0.30
DEFAULT_SCROLL_BAND_COUNT = 6
DEFAULT_SCROLL_SEARCH_HEIGHT_RATIO = 0.32
DEFAULT_SCROLL_STEP_PX = 4
DEFAULT_SCROLL_MIN_OFFSET_PX = 4
DEFAULT_SCROLL_MIN_CHANGED_RATIO = 0.02
DEFAULT_SCROLL_MIN_MAE = 0.01
DEFAULT_SCROLL_ALIGNED_MAE = 0.014
DEFAULT_SCROLL_ALIGNED_CHANGED_RATIO = 0.04
DEFAULT_SCROLL_MIN_EXPLAINED_RATIO = 0.55
DEFAULT_SCROLL_MIN_OVERLAP_RATIO = 0.72
DEFAULT_REGION_SCROLL_MIN_OVERLAP_RATIO = 0.60
DEFAULT_REGION_SCROLL_ALIGNED_MAE = 0.016
DEFAULT_REGION_SCROLL_ALIGNED_CHANGED_RATIO = 0.08


@dataclass(frozen=True)
class VisualDiffResult:
    mae: float = 0.0
    changed_ratio: float = 0.0
    changed_block_ratio: float = 0.0
    scroll_like: bool = False
    scroll_offset_y: int = 0
    scroll_aligned_mae: float = 0.0
    scroll_aligned_changed_ratio: float = 0.0
    scroll_overlap_ratio: float = 0.0
    scroll_explained_ratio: float = 0.0
    width: int = 0
    height: int = 0
    block_size: int = DEFAULT_BLOCK_SIZE
    significant: bool = False
    error: str = ""

    @property
    def available(self) -> bool:
        return not self.error and self.width > 0 and self.height > 0

    def to_payload(self) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "mae": round(self.mae, 6),
            "changed_ratio": round(self.changed_ratio, 6),
            "changed_block_ratio": round(self.changed_block_ratio, 6),
            "scroll_like": self.scroll_like,
            "scroll_offset_y": self.scroll_offset_y,
            "scroll_aligned_mae": round(self.scroll_aligned_mae, 6),
            "scroll_aligned_changed_ratio": round(self.scroll_aligned_changed_ratio, 6),
            "scroll_overlap_ratio": round(self.scroll_overlap_ratio, 6),
            "scroll_explained_ratio": round(self.scroll_explained_ratio, 6),
            "width": self.width,
            "height": self.height,
            "block_size": self.block_size,
            "significant": self.significant,
        }
        if self.error:
            payload["error"] = self.error
        return payload


def compare_screenshots(
    previous_path: str | Path,
    current_path: str | Path,
    *,
    long_edge: int = DEFAULT_LONG_EDGE,
    pixel_threshold: int = DEFAULT_PIXEL_THRESHOLD,
    block_size: int = DEFAULT_BLOCK_SIZE,
) -> VisualDiffResult:
    try:
        from PIL import Image, ImageChops  # type: ignore
    except Exception as error:  # pragma: no cover - depends on runtime packaging.
        return VisualDiffResult(error=f"pillow_unavailable: {error}")

    previous = Path(previous_path)
    current = Path(current_path)
    if not previous.exists() or not current.exists():
        return VisualDiffResult(error="image_missing")

    try:
        with Image.open(previous) as previous_image, Image.open(current) as current_image:
            target_size = _target_size(previous_image.size, long_edge=long_edge)
            if target_size[0] <= 0 or target_size[1] <= 0:
                return VisualDiffResult(error="invalid_image_size")
            normalized_previous = _normalize_image(previous_image, target_size)
            normalized_current = _normalize_image(current_image, target_size)
            diff = ImageChops.difference(normalized_previous, normalized_current)
            if max(diff.getbbox() or (0, 0, 0, 0)) == 0:
                return VisualDiffResult(
                    width=target_size[0],
                    height=target_size[1],
                    block_size=block_size,
                    significant=False,
                )
            data = diff.tobytes()
    except Exception as error:
        return VisualDiffResult(error=f"image_compare_failed: {error}")

    result = _metrics_from_diff_bytes(
        data,
        width=target_size[0],
        height=target_size[1],
        pixel_threshold=max(0, int(pixel_threshold)),
        block_size=max(1, int(block_size)),
    )
    if not result.significant:
        return result
    scroll = _detect_scroll_like(
        normalized_previous,
        normalized_current,
        diff,
        width=target_size[0],
        height=target_size[1],
        pixel_threshold=max(0, int(pixel_threshold)),
    )
    if not scroll.scroll_like:
        return result
    return replace(
        result,
        significant=False,
        scroll_like=True,
        scroll_offset_y=scroll.offset_y,
        scroll_aligned_mae=scroll.aligned_mae,
        scroll_aligned_changed_ratio=scroll.aligned_changed_ratio,
        scroll_overlap_ratio=scroll.overlap_ratio,
        scroll_explained_ratio=scroll.explained_ratio,
    )


def _normalize_image(image: Any, target_size: tuple[int, int]) -> Any:
    from PIL import Image, ImageFilter  # type: ignore

    normalized = image.convert("L").resize(target_size, Image.Resampling.BILINEAR)
    return normalized.filter(ImageFilter.BoxBlur(1))


def _target_size(size: tuple[int, int], *, long_edge: int) -> tuple[int, int]:
    width, height = int(size[0] or 0), int(size[1] or 0)
    if width <= 0 or height <= 0:
        return 0, 0
    edge = max(1, int(long_edge or DEFAULT_LONG_EDGE))
    scale = min(1.0, edge / max(width, height))
    return max(1, round(width * scale)), max(1, round(height * scale))


def _metrics_from_diff_bytes(
    data: bytes,
    *,
    width: int,
    height: int,
    pixel_threshold: int,
    block_size: int,
) -> VisualDiffResult:
    total_pixels = max(1, width * height)
    block_cols = (width + block_size - 1) // block_size
    block_rows = (height + block_size - 1) // block_size
    block_count = max(1, block_cols * block_rows)
    block_sums = [0] * block_count
    block_changed_pixels = [0] * block_count
    block_pixels = [0] * block_count

    total_diff = 0
    changed_pixels = 0
    for index, value in enumerate(data):
        total_diff += value
        if value > pixel_threshold:
            changed_pixels += 1
        x = index % width
        y = index // width
        block_index = (y // block_size) * block_cols + (x // block_size)
        block_sums[block_index] += value
        block_pixels[block_index] += 1
        if value > pixel_threshold:
            block_changed_pixels[block_index] += 1

    changed_blocks = 0
    for block_index in range(block_count):
        pixels = max(1, block_pixels[block_index])
        block_mae = block_sums[block_index] / (pixels * 255.0)
        block_changed_ratio = block_changed_pixels[block_index] / pixels
        if (
            block_mae >= DEFAULT_BLOCK_MAE_THRESHOLD
            or block_changed_ratio >= DEFAULT_BLOCK_CHANGED_RATIO_THRESHOLD
        ):
            changed_blocks += 1

    mae = total_diff / (total_pixels * 255.0)
    changed_ratio = changed_pixels / total_pixels
    changed_block_ratio = changed_blocks / block_count
    significant = (
        mae >= DEFAULT_SIGNIFICANT_MAE
        or changed_ratio >= DEFAULT_SIGNIFICANT_CHANGED_RATIO
        or changed_block_ratio >= DEFAULT_SIGNIFICANT_BLOCK_RATIO
    )
    return VisualDiffResult(
        mae=mae,
        changed_ratio=changed_ratio,
        changed_block_ratio=changed_block_ratio,
        width=width,
        height=height,
        block_size=block_size,
        significant=significant,
    )


@dataclass(frozen=True)
class _BandRawMetrics:
    changed_pixels: int
    pixel_count: int
    mae: float
    changed_ratio: float


@dataclass(frozen=True)
class _ShiftMetrics:
    offset_y: int = 0
    mae: float = 0.0
    changed_ratio: float = 0.0
    overlap_ratio: float = 0.0


@dataclass(frozen=True)
class _ScrollMetrics:
    scroll_like: bool = False
    offset_y: int = 0
    aligned_mae: float = 0.0
    aligned_changed_ratio: float = 0.0
    overlap_ratio: float = 0.0
    explained_ratio: float = 0.0


@dataclass(frozen=True)
class _ScrollRegion:
    start_x: int
    end_x: int
    start_y: int
    end_y: int


def _detect_scroll_like(
    previous_image: Any,
    current_image: Any,
    diff_image: Any,
    *,
    width: int,
    height: int,
    pixel_threshold: int,
) -> _ScrollMetrics:
    if width <= 0 or height <= 0:
        return _ScrollMetrics()
    search_limit = _scroll_search_limit(height)
    band_count = max(1, min(DEFAULT_SCROLL_BAND_COUNT, width))
    total_changed_weight = 0
    explained_weight = 0
    weighted_mae = 0.0
    weighted_changed_ratio = 0.0
    weighted_overlap = 0.0
    strongest_weight = -1
    strongest_offset = 0

    for band_index in range(band_count):
        start_x = round(width * band_index / band_count)
        end_x = round(width * (band_index + 1) / band_count)
        if end_x <= start_x:
            continue
        raw = _band_raw_metrics(
            start_x=start_x,
            end_x=end_x,
            height=height,
            diff_image=diff_image,
            pixel_threshold=pixel_threshold,
        )
        if (
            raw.changed_ratio < DEFAULT_SCROLL_MIN_CHANGED_RATIO
            and raw.mae < DEFAULT_SCROLL_MIN_MAE
        ):
            continue
        changed_weight = max(raw.changed_pixels, round(raw.mae * raw.pixel_count))
        if changed_weight <= 0:
            continue
        total_changed_weight += changed_weight
        shifted = _best_vertical_shift(
            previous_image,
            current_image,
            region=_ScrollRegion(start_x=start_x, end_x=end_x, start_y=0, end_y=height),
            pixel_threshold=pixel_threshold,
            search_limit=search_limit,
            min_overlap_ratio=DEFAULT_SCROLL_MIN_OVERLAP_RATIO,
        )
        if not _is_scroll_shift(shifted):
            continue
        explained_weight += changed_weight
        weighted_mae += shifted.mae * changed_weight
        weighted_changed_ratio += shifted.changed_ratio * changed_weight
        weighted_overlap += shifted.overlap_ratio * changed_weight
        if changed_weight > strongest_weight:
            strongest_weight = changed_weight
            strongest_offset = shifted.offset_y

    if total_changed_weight <= 0 or explained_weight <= 0:
        return _detect_regional_scroll_like(
            previous_image,
            current_image,
            diff_image,
            width=width,
            height=height,
            pixel_threshold=pixel_threshold,
            search_limit=search_limit,
        )
    explained_ratio = explained_weight / total_changed_weight
    if explained_ratio < DEFAULT_SCROLL_MIN_EXPLAINED_RATIO:
        regional = _detect_regional_scroll_like(
            previous_image,
            current_image,
            diff_image,
            width=width,
            height=height,
            pixel_threshold=pixel_threshold,
            search_limit=search_limit,
        )
        if regional.scroll_like:
            return regional
        return _ScrollMetrics(explained_ratio=explained_ratio)
    return _ScrollMetrics(
        scroll_like=True,
        offset_y=strongest_offset,
        aligned_mae=weighted_mae / explained_weight,
        aligned_changed_ratio=weighted_changed_ratio / explained_weight,
        overlap_ratio=weighted_overlap / explained_weight,
        explained_ratio=explained_ratio,
    )


def _detect_regional_scroll_like(
    previous_image: Any,
    current_image: Any,
    diff_image: Any,
    *,
    width: int,
    height: int,
    pixel_threshold: int,
    search_limit: int,
) -> _ScrollMetrics:
    total_changed = _diff_image_totals(diff_image, pixel_threshold=pixel_threshold)[1]
    if total_changed <= 0:
        return _ScrollMetrics()
    best: tuple[tuple[float, float, float], _ScrollMetrics] | None = None
    for region in _scroll_candidate_regions(width=width, height=height):
        raw = _region_raw_metrics(
            region=region,
            diff_image=diff_image,
            pixel_threshold=pixel_threshold,
        )
        if (
            raw.changed_ratio < DEFAULT_SCROLL_MIN_CHANGED_RATIO
            and raw.mae < DEFAULT_SCROLL_MIN_MAE
        ):
            continue
        shifted = _best_vertical_shift(
            previous_image,
            current_image,
            region=region,
            pixel_threshold=pixel_threshold,
            search_limit=search_limit,
            min_overlap_ratio=DEFAULT_REGION_SCROLL_MIN_OVERLAP_RATIO,
        )
        if not _is_scroll_shift(
            shifted,
            aligned_mae=DEFAULT_REGION_SCROLL_ALIGNED_MAE,
            aligned_changed_ratio=DEFAULT_REGION_SCROLL_ALIGNED_CHANGED_RATIO,
            min_overlap_ratio=DEFAULT_REGION_SCROLL_MIN_OVERLAP_RATIO,
        ):
            continue
        explained_ratio = min(1.0, raw.changed_pixels / total_changed)
        metrics = _ScrollMetrics(
            scroll_like=True,
            offset_y=shifted.offset_y,
            aligned_mae=shifted.mae,
            aligned_changed_ratio=shifted.changed_ratio,
            overlap_ratio=shifted.overlap_ratio,
            explained_ratio=explained_ratio,
        )
        score = (shifted.mae, shifted.changed_ratio, -explained_ratio)
        if best is None or score < best[0]:
            best = (score, metrics)
    return best[1] if best is not None else _ScrollMetrics()


def _scroll_candidate_regions(*, width: int, height: int) -> list[_ScrollRegion]:
    regions: list[_ScrollRegion] = []
    x_ranges = (
        (0.0, 1.0),
        (0.20, 1.0),
        (0.30, 1.0),
        (0.40, 1.0),
        (0.50, 1.0),
        (0.0, 0.80),
        (0.0, 0.70),
        (0.15, 0.85),
    )
    y_ranges = (
        (0.0, 1.0),
        (0.08, 0.94),
        (0.12, 0.88),
        (0.15, 0.84),
    )
    seen: set[tuple[int, int, int, int]] = set()
    for x_start_ratio, x_end_ratio in x_ranges:
        start_x = round(width * x_start_ratio)
        end_x = round(width * x_end_ratio)
        if end_x - start_x < max(24, round(width * 0.18)):
            continue
        for y_start_ratio, y_end_ratio in y_ranges:
            start_y = round(height * y_start_ratio)
            end_y = round(height * y_end_ratio)
            if end_y - start_y < max(48, round(height * 0.40)):
                continue
            key = (start_x, end_x, start_y, end_y)
            if key in seen:
                continue
            seen.add(key)
            regions.append(
                _ScrollRegion(
                    start_x=start_x,
                    end_x=end_x,
                    start_y=start_y,
                    end_y=end_y,
                )
            )
    return regions


def _scroll_search_limit(height: int) -> int:
    return min(
        max(0, height - 1),
        max(
            DEFAULT_SCROLL_MIN_OFFSET_PX,
            round(max(1, height) * DEFAULT_SCROLL_SEARCH_HEIGHT_RATIO),
        ),
    )


def _band_raw_metrics(
    *,
    start_x: int,
    end_x: int,
    height: int,
    diff_image: Any,
    pixel_threshold: int,
) -> _BandRawMetrics:
    band_diff = diff_image.crop((start_x, 0, end_x, height))
    pixel_count = max(1, (end_x - start_x) * height)
    total, changed = _diff_image_totals(band_diff, pixel_threshold=pixel_threshold)
    return _BandRawMetrics(
        changed_pixels=changed,
        pixel_count=pixel_count,
        mae=total / (pixel_count * 255.0),
        changed_ratio=changed / pixel_count,
    )


def _region_raw_metrics(
    *,
    region: _ScrollRegion,
    diff_image: Any,
    pixel_threshold: int,
) -> _BandRawMetrics:
    region_diff = diff_image.crop((region.start_x, region.start_y, region.end_x, region.end_y))
    pixel_count = max(1, (region.end_x - region.start_x) * (region.end_y - region.start_y))
    total, changed = _diff_image_totals(region_diff, pixel_threshold=pixel_threshold)
    return _BandRawMetrics(
        changed_pixels=changed,
        pixel_count=pixel_count,
        mae=total / (pixel_count * 255.0),
        changed_ratio=changed / pixel_count,
    )


def _best_vertical_shift(
    previous_image: Any,
    current_image: Any,
    *,
    region: _ScrollRegion,
    pixel_threshold: int,
    search_limit: int,
    min_overlap_ratio: float,
) -> _ShiftMetrics:
    from PIL import ImageChops  # type: ignore

    height = max(0, region.end_y - region.start_y)
    width = max(0, region.end_x - region.start_x)
    limit = min(search_limit, max(0, height - 1))
    if limit < DEFAULT_SCROLL_MIN_OFFSET_PX:
        return _ShiftMetrics()
    best = _ShiftMetrics()
    best_score: tuple[float, float, int] | None = None
    for offset_y in range(-limit, limit + 1, DEFAULT_SCROLL_STEP_PX):
        overlap = height - abs(offset_y)
        overlap_ratio = overlap / height
        if overlap <= 0 or overlap_ratio < min_overlap_ratio:
            continue
        current_y = region.start_y + max(0, offset_y)
        previous_y = region.start_y + max(0, -offset_y)
        samples = max(1, overlap * width)
        previous_crop = previous_image.crop(
            (region.start_x, previous_y, region.end_x, previous_y + overlap)
        )
        current_crop = current_image.crop(
            (region.start_x, current_y, region.end_x, current_y + overlap)
        )
        diff = ImageChops.difference(previous_crop, current_crop)
        diff_sum, changed = _diff_image_totals(diff, pixel_threshold=pixel_threshold)
        mae = diff_sum / (samples * 255.0)
        changed_ratio = changed / samples
        score = (mae, changed_ratio, abs(offset_y))
        if best_score is None or score < best_score:
            best_score = score
            best = _ShiftMetrics(
                offset_y=offset_y,
                mae=mae,
                changed_ratio=changed_ratio,
                overlap_ratio=overlap_ratio,
            )
    return best


def _is_scroll_shift(
    shifted: _ShiftMetrics,
    *,
    aligned_mae: float = DEFAULT_SCROLL_ALIGNED_MAE,
    aligned_changed_ratio: float = DEFAULT_SCROLL_ALIGNED_CHANGED_RATIO,
    min_overlap_ratio: float = DEFAULT_SCROLL_MIN_OVERLAP_RATIO,
) -> bool:
    return (
        abs(shifted.offset_y) >= DEFAULT_SCROLL_MIN_OFFSET_PX
        and shifted.mae <= aligned_mae
        and shifted.changed_ratio <= aligned_changed_ratio
        and shifted.overlap_ratio >= min_overlap_ratio
    )


def _diff_image_totals(diff_image: Any, *, pixel_threshold: int) -> tuple[int, int]:
    from PIL import ImageStat  # type: ignore

    total = int(ImageStat.Stat(diff_image).sum[0])
    histogram = diff_image.histogram()
    changed = sum(histogram[pixel_threshold + 1 :])
    return total, changed
