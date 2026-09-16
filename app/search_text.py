from collections.abc import Sequence


def build_search_text(
    title: str,
    heading: str | None,
    content: str,
    section_path: Sequence[str] | None = None,
) -> str:
    """Use the same heading context for document embeddings and BM25."""
    path_text = " > ".join(section_path or ()) or heading or ""
    return f"{title}\n{path_text}\n{content}"
