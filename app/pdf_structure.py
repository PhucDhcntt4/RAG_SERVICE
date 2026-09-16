import re
from collections import Counter
from dataclasses import dataclass, replace

from app.pdf_extract import PdfExtraction, PdfLine


PREFIX_ONLY = re.compile(r"(?:[IVXLCDM]+[.)]|\d+(?:\.\d+)*[.)]|[a-zđ][.)])")
ROMAN = re.compile(r"^[IVXLCDM]+[.)]\s+\S")
PART = re.compile(r"^PHẦN\s+[IVXLCDM]+(?:[.:])?\s+\S", re.IGNORECASE)
# Require a trailing dot/parenthesis. This avoids turning emphasized prose such
# as "10 tây đến 12 tây hằng tháng" into a numbered section heading.
NUMBERED = re.compile(r"^(\d+(?:\.\d+)*)(?:[.)])\s+\S")
LETTER = re.compile(r"^[a-zđ][.)]\s+\S")
BULLET = re.compile(r"^[-–—•●▪◦\uf0b7]")


@dataclass(frozen=True)
class DetectedLine:
    line: PdfLine
    level: int | None
    reason: str


def font_counts(lines):
    counts = Counter()
    for line in lines:
        for span in line.spans:
            weight = sum(not char.isspace() for char in span.text)
            if weight:
                counts[round(span.font_size, 1)] += weight
    return counts


def is_bold(line):
    total = bold = 0
    for span in line.spans:
        weight = sum(not char.isspace() for char in span.text)
        total += weight
        if span.bold:
            bold += weight
    return total > 0 and bold / total >= 0.6


def split_inline_heading(line: PdfLine) -> tuple[PdfLine, PdfLine | None]:
    """Separate a numbered bold label from regular text at a span boundary."""
    for index, span in enumerate(line.spans):
        if span.text.strip() and not span.bold:
            break
    else:
        return line, None

    heading_spans = line.spans[:index]
    body_spans = line.spans[index:]
    heading_text = "".join(span.text for span in heading_spans).strip()
    if not (
        heading_text.endswith(":")
        and (ROMAN.match(heading_text) or NUMBERED.match(heading_text)
             or LETTER.match(heading_text))
    ):
        return line, None

    def part(spans):
        return replace(
            line,
            text="".join(span.text for span in spans).strip(),
            spans=spans,
            bbox=(min(s.bbox[0] for s in spans), min(s.bbox[1] for s in spans),
                  max(s.bbox[2] for s in spans), max(s.bbox[3] for s in spans)),
        )

    return part(heading_spans), part(body_spans)


def prepare_lines(lines, body_size):
    kept = []
    for line in lines:
        sizes = font_counts([line])
        line_size = sizes.most_common(1)[0][0] if sizes else body_size
        is_page_number = (
            line.text.strip() == str(line.page)
            and line.bbox[1] >= line.page_height * 0.90
            and line_size <= body_size
        )
        if not is_page_number:
            kept.append(line)

    merged = []
    index = 0

    while index < len(kept):
        left = kept[index]

        if PREFIX_ONLY.fullmatch(left.text.strip()) and index + 1 < len(kept):
            right = kept[index + 1]
            left_y = (left.bbox[1] + left.bbox[3]) / 2
            right_y = (right.bbox[1] + right.bbox[3]) / 2
            gap = right.bbox[0] - left.bbox[2]

            if (
                left.page == right.page
                and abs(left_y - right_y) <= 2.5
                and 0 <= gap <= 3 * body_size
                and is_bold(left)
                and is_bold(right)
            ):
                left = replace(
                    left,
                    text=left.text.strip() + " " + right.text.strip(),
                    spans=left.spans + right.spans,
                    bbox=(
                        min(left.bbox[0], right.bbox[0]),
                        min(left.bbox[1], right.bbox[1]),
                        max(left.bbox[2], right.bbox[2]),
                        max(left.bbox[3], right.bbox[3]),
                    ),
                )
                index += 1

        merged.append(left)
        index += 1

    return merged


def detect_headings(extraction: PdfExtraction) -> list[DetectedLine]:
    counts = font_counts(extraction.lines)
    if not counts:
        return []

    body_size = counts.most_common(1)[0][0]
    lines = prepare_lines(extraction.lines, body_size)
    # Handbooks often repeat part names in a table of contents using a smaller
    # font. Only the largest bold occurrence establishes the structural scope.
    part_sizes = {}
    for candidate in lines:
        candidate_text = candidate.text.strip()
        candidate_sizes = font_counts([candidate])
        candidate_size = candidate_sizes.most_common(1)[0][0] if candidate_sizes else body_size
        if PART.match(candidate_text) and is_bold(candidate):
            key = candidate_text.casefold()
            part_sizes[key] = max(part_sizes.get(key, 0), candidate_size)
    result = []
    title_seen = False
    numbered_level = None

    for original_line in lines:
        line, inline_body = split_inline_heading(original_line)
        text = line.text.strip()
        sizes = font_counts([line])
        size = sizes.most_common(1)[0][0] if sizes else body_size
        level, reason = None, "body"

        if (is_bold(line) and len(text) <= 180 and text.startswith("❖")
                and text.endswith(":")):
            level, reason = (numbered_level or 2) + 2, "scope_label"
        elif is_bold(line) and len(text) <= 180 and not BULLET.match(text):
            number = NUMBERED.match(text)

            if not title_seen and line.page == 1 and size >= body_size * 1.3:
                level, reason = 1, "document_title"
                title_seen = True
            elif (PART.match(text)
                  and size >= part_sizes.get(text.casefold(), size) - 0.1):
                # A named document part is a new top-level retrieval boundary.
                # The document title is already carried separately in metadata.
                level, reason = 1, "document_part"
                numbered_level = None
            elif ROMAN.match(text):
                level, reason = 2, "roman_number"
                numbered_level = level
            elif number:
                level = number.group(1).count(".") + 2
                reason = "section_number"
                numbered_level = level
            elif LETTER.match(text) and numbered_level is not None:
                level, reason = numbered_level + 1, "letter_section"
            elif (numbered_level is None and text.endswith(":")
                  and len(text) <= 100 and not LETTER.match(text)):
                level, reason = 2, "intro_label"

        if level is None:
            result.append(DetectedLine(line=original_line, level=None, reason="body"))
        else:
            result.append(DetectedLine(line=line, level=level, reason=reason))
            if inline_body is not None:
                result.append(DetectedLine(line=inline_body, level=None, reason="body"))

    return result
