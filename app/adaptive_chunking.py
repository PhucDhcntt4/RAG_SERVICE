"""Heading-aware semantic chunking with a bounded recursive fallback."""

from __future__ import annotations

import math
import re
from dataclasses import dataclass

from app.chunking import Chunk, chunk_text


SENTENCE_BOUNDARY = re.compile(r"(?<=[.!?])\s+(?=\S)")
RECURSIVE_LEVELS = (
    (re.compile(r"\n\s*\n+"), "\n\n"),
    (re.compile(r"\n+"), "\n"),
    (SENTENCE_BOUNDARY, " "),
    (re.compile(r"(?<=[;:])\s+"), " "),
    (re.compile(r"\s+"), " "),
)


@dataclass(frozen=True)
class AdaptiveChunkingStats:
    method: str
    sections: int
    semantic_units: int
    semantic_sections: int
    average_threshold: float | None
    recursive_splits: int


def percentile(values: list[float], percentage: float) -> float:
    if not values:
        return 0.0
    ordered = sorted(values)
    position = (len(ordered) - 1) * percentage / 100
    lower = math.floor(position)
    upper = math.ceil(position)
    if lower == upper:
        return ordered[lower]
    weight = position - lower
    return ordered[lower] * (1 - weight) + ordered[upper] * weight


def _pack(parts: list[str], separator: str, max_chars: int) -> list[str]:
    result: list[str] = []
    current = ""
    for part in parts:
        part = part.strip()
        if not part:
            continue
        candidate = part if not current else current + separator + part
        if current and len(candidate) > max_chars:
            result.append(current)
            current = part
        else:
            current = candidate
    if current:
        result.append(current)
    return result


def _recursive_split(value: str, max_chars: int, level: int) -> list[str]:
    value = value.strip()
    if not value:
        return []
    if len(value) <= max_chars:
        return [value]
    if level >= len(RECURSIVE_LEVELS):
        return [value[start:start + max_chars] for start in range(0, len(value), max_chars)]

    pattern, separator = RECURSIVE_LEVELS[level]
    raw_parts = [part for part in pattern.split(value) if part.strip()]
    if len(raw_parts) == 1:
        return _recursive_split(value, max_chars, level + 1)

    bounded: list[str] = []
    for part in raw_parts:
        bounded.extend(_recursive_split(part, max_chars, level + 1))
    return _pack(bounded, separator, max_chars)


def recursive_split_text(value: str, max_chars: int) -> list[str]:
    """Split at the safest available boundary and guarantee a hard size cap."""
    if max_chars < 50:
        raise ValueError("max_chars phải từ 50")
    return _recursive_split(value, max_chars, 0)


def semantic_units(value: str, max_chars: int) -> list[str]:
    """Build paragraph/sentence units without allowing an oversized unit."""
    paragraphs = [
        part.strip()
        for part in re.split(r"\n\s*\n+", value.replace("\r\n", "\n").replace("\r", "\n"))
        if part.strip()
    ]
    units: list[str] = []
    for paragraph in paragraphs:
        if len(paragraph) <= max_chars:
            units.append(paragraph)
            continue
        sentences = [part.strip() for part in SENTENCE_BOUNDARY.split(paragraph) if part.strip()]
        source = sentences if len(sentences) > 1 else [paragraph]
        for sentence in source:
            units.extend(recursive_split_text(sentence, max_chars))
    return units


def _cosine_distance(left: list[float], right: list[float]) -> float:
    similarity = sum(a * b for a, b in zip(left, right, strict=True))
    return 1 - max(-1.0, min(1.0, similarity))


def _semantic_section(
    chunk: Chunk,
    embedder,
    *,
    max_chars: int,
    min_chars: int,
    breakpoint_percentile: float,
) -> tuple[list[str], float | None, int, int]:
    units = semantic_units(chunk.content, max_chars)
    if not units:
        return [], None, 0, 0
    if len(units) == 1:
        return units, None, 1, int(len(chunk.content) > max_chars)

    windows = [
        "\n\n".join(units[max(0, index - 1):min(len(units), index + 2)])
        for index in range(len(units))
    ]
    vectors = embedder.embed(windows)
    distances = [
        _cosine_distance(left, right)
        for left, right in zip(vectors, vectors[1:])
    ]
    threshold = percentile(distances, breakpoint_percentile)
    groups: list[str] = []
    current: list[str] = []
    current_size = 0

    def flush() -> None:
        nonlocal current, current_size
        if current:
            groups.append("\n\n".join(current))
        current = []
        current_size = 0

    for index, unit in enumerate(units):
        projected = current_size + len(unit) + (2 if current else 0)
        semantic_break = (
            bool(current)
            and index > 0
            and threshold > 0
            and distances[index - 1] >= threshold
            and current_size >= min_chars
        )
        if current and (projected > max_chars or semantic_break):
            flush()
        current.append(unit)
        current_size += len(unit) + (2 if current_size else 0)
    flush()

    # Avoid a tiny tail when it fits safely in the preceding semantic chunk.
    if len(groups) > 1 and len(groups[-1]) < min_chars:
        candidate = groups[-2] + "\n\n" + groups[-1]
        if len(candidate) <= max_chars:
            groups[-2:] = [candidate]
    return groups, threshold, len(units), sum(len(unit) >= max_chars for unit in units)


def adaptive_chunk_text(
    text: str,
    embedder,
    *,
    max_chars: int = 1200,
    min_chars: int = 300,
    breakpoint_percentile: float = 80,
    natural_headings: bool = True,
    base_chunks: list[Chunk] | None = None,
) -> tuple[list[Chunk], AdaptiveChunkingStats]:
    """Apply heading boundaries, semantic boundaries, then recursive fallback."""
    if max_chars < 200:
        raise ValueError("max_chars phải từ 200")
    if not 50 <= min_chars < max_chars:
        raise ValueError("min_chars phải từ 50 và nhỏ hơn max_chars")
    if not 0 < breakpoint_percentile < 100:
        raise ValueError("breakpoint_percentile phải trong khoảng 0-100")

    if base_chunks is None:
        # A very large size extracts logical sections without pre-cutting them.
        bases = chunk_text(
            text,
            max(200, len(text) + 1),
            0,
            natural_headings=natural_headings,
        )
    else:
        bases = list(base_chunks)

    result: list[Chunk] = []
    thresholds: list[float] = []
    total_units = 0
    semantic_sections = 0
    recursive_splits = 0
    for base in bases:
        # PDF table chunks are already row-aware; semantic splitting could detach
        # a value from its row conditions. Preserve them exactly.
        if base.content.startswith("Bảng, trang PDF "):
            values = [base.content]
            threshold = None
            units = 1
            fallback_count = 0
        elif len(base.content) <= max_chars:
            values = [base.content.strip()]
            threshold = None
            units = 1
            fallback_count = 0
        else:
            values, threshold, units, fallback_count = _semantic_section(
                base,
                embedder,
                max_chars=max_chars,
                min_chars=min_chars,
                breakpoint_percentile=breakpoint_percentile,
            )
            semantic_sections += int(units > 1)
        if threshold is not None:
            thresholds.append(threshold)
        total_units += units
        recursive_splits += fallback_count
        for value in values:
            if value.strip():
                result.append(
                    Chunk(
                        index=len(result),
                        heading=base.heading,
                        content=value.strip(),
                        section_path=base.section_path,
                    )
                )

    average = sum(thresholds) / len(thresholds) if thresholds else None
    return result, AdaptiveChunkingStats(
        method="heading_semantic_recursive",
        sections=len(bases),
        semantic_units=total_units,
        semantic_sections=semantic_sections,
        average_threshold=average,
        recursive_splits=recursive_splits,
    )
