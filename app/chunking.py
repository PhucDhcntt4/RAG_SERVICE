import re
from dataclasses import dataclass


@dataclass(frozen=True)
class Chunk:
    index: int
    heading: str | None
    content: str


def chunk_text(text: str, size: int = 1200, overlap: int = 180) -> list[Chunk]:
    """Keep heading boundaries; windows overlap only within the same section."""
    if size < 200 or not 0 <= overlap < size:
        raise ValueError("Invalid chunk size or overlap")
    sections = []
    heading = None
    lines = []
    for line in text.replace("\r\n", "\n").replace("\r", "\n").splitlines():
        match = re.match(r"^#{1,6}\s+(.+?)\s*$", line.strip())
        if match:
            sections.append((heading, "\n".join(lines).strip()))
            heading, lines = match.group(1)[:500], []
        else:
            lines.append(line)
    sections.append((heading, "\n".join(lines).strip()))
    chunks = []
    for heading, content in sections:
        start = 0
        while start < len(content):
            end = min(start + size, len(content))
            if end < len(content):
                boundary = max(content.rfind("\n", start, end), content.rfind(" ", start, end))
                if boundary > start + max(overlap, size // 2):
                    end = boundary
            value = content[start:end].strip()
            if value:
                chunks.append(Chunk(len(chunks), heading, value))
            if end == len(content):
                break
            start = max(start + 1, end - overlap)
    return chunks
