"""Conservative heading detection for plain text; no model or network needed."""

import re
from dataclasses import dataclass


MARKDOWN = re.compile(r"^(#{1,6})\s+(.+?)\s*$")
ROMAN = re.compile(r"^([IVXLCDM]+)[.)]\s+(.+)$")
DECIMAL = re.compile(r"^(\d+(?:\.\d+)+)[.)]?\s+(.+)$")
LETTER = re.compile(r"^([a-zđ])[.)]\s+(.+)$")
WARNING = re.compile(r"^(?:LƯU Ý|CHÚ Ý|CẢNH BÁO|KHÔNG ĐƯỢC|NGHIÊM CẤM)\b")


@dataclass(frozen=True)
class TextLine:
    text: str
    level: int | None = None
    reason: str = "body"


def _short_label(text: str) -> bool:
    """Reject sentences, inline label/body pairs and numeric address entries."""
    return bool(
        text and text[0].isalpha()
        and len(text) <= 120 and len(text.split()) <= 16
        and not text.endswith((".", "!", "?", ";"))
        and ":" not in text.rstrip(":")
    )


def detect_text_headings(text: str, *, natural: bool = True) -> list[TextLine]:
    lines = text.replace("\r\n", "\n").replace("\r", "\n").splitlines()
    stripped = [line.strip() for line in lines]
    # Explicit Markdown defines the hierarchy for the whole document. This also
    # keeps numbered lists inside existing Markdown sections from becoming headings.
    markdown_mode = any(MARKDOWN.match(line) for line in stripped)
    if markdown_mode or not natural:
        result = []
        for original, value in zip(lines, stripped):
            match = MARKDOWN.match(value)
            result.append(
                TextLine(match[2][:500], len(match[1]), "markdown")
                if match else TextLine(original)
            )
        return result

    # Precompute the next nonempty line once; do not scan the rest of the file
    # for every candidate in a large document.
    following = [""] * len(lines)
    next_text = ""
    for index in range(len(lines) - 1, -1, -1):
        following[index] = next_text
        if stripped[index]:
            next_text = stripped[index]

    uppercase = set()
    for index, value in enumerate(stripped):
        if (
            _short_label(value) and value.isupper()
            and sum(char.isalpha() for char in value) >= 4
            and not WARNING.match(value)
            and not ROMAN.match(value)
            and (index == 0 or not stripped[index - 1])
            and following[index]
            and (index + 1 == len(lines) or not stripped[index + 1])
        ):
            uppercase.add(index)

    # Isolated uppercase text needs supporting structure: another peer heading,
    # or a numbered outline immediately below it. A lone warning stays body text.
    if len(uppercase) < 2:
        uppercase = {
            index for index in uppercase
            if ROMAN.match(following[index]) or DECIMAL.match(following[index])
        }

    result = []
    uppercase_parent = False
    roman_parent = False
    numbered_level = None
    for index, (original, value) in enumerate(zip(lines, stripped)):
        level, reason = None, "body"
        roman = ROMAN.match(value)
        decimal = DECIMAL.match(value)
        letter = LETTER.match(value)

        if index in uppercase:
            level, reason = 1, "uppercase_section"
            uppercase_parent = True
            roman_parent = False
            numbered_level = None
        elif roman and _short_label(roman[2]):
            level = 2 if uppercase_parent else 1
            reason = "roman_number"
            roman_parent = True
            numbered_level = level
        elif decimal and _short_label(decimal[2]):
            # 1.1 is below I when present; without I it starts the outline.
            level = decimal[1].count(".") + int(uppercase_parent) + int(roman_parent)
            reason = "section_number"
            numbered_level = level
        elif letter and numbered_level is not None and _short_label(letter[2]):
            level, reason = numbered_level + 1, "letter_section"

        result.append(TextLine(value, level, reason) if level else TextLine(original))

    return result
