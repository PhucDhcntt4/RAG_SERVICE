import math
import re
import unicodedata
from collections import defaultdict, Counter

from app.search_text import build_search_text

TOKEN_ALIASES = {
    "hcm": ("hồ", "chí", "minh"),
    "tphcm": ("hồ", "chí", "minh"),
}


def tokenize(text: str) -> list[str]:
    text = unicodedata.normalize("NFKC", text).lower()
    tokens = re.findall(r"[\w_]+", text)

    normalized = []

    for token in tokens:
        replacement = TOKEN_ALIASES.get(token)

        if replacement is None:
            normalized.append(token)
        else:
            normalized.extend(replacement)

    return normalized

class BM25Index:
    def __init__(self, chunks, *, k1=1.5, b=0.75):
        if not math.isfinite(k1) or k1 <= 0:
            raise ValueError("k1 must be a positive and finite")
        if not math.isfinite(b) or not 0 <= b <= 1:
            raise ValueError("b must be a value between 0 and 1")

        self.k1 = k1
        self.b = b

        self.chunks = {}
        self.lengths = {}
        self.postings = defaultdict(dict)

        for chunk in chunks:
            chunk_id = chunk["chunk_id"]

            if chunk_id in self.chunks:
                raise ValueError(f"Duplicate chunk_id: {chunk_id}")

            self.chunks[chunk_id] = dict(chunk)

            text = build_search_text(
                title=chunk.get("title") or "",
                heading=chunk.get("heading"),
                content=chunk["content"],
                section_path=chunk.get("section_path"),
            )

            tokens = tokenize(text)
            self.lengths[chunk_id] = len(tokens)

            for term, frequency in Counter(tokens).items():
                self.postings[term][chunk_id] = frequency

        self.count = len(self.chunks)
        self.avg_length = (
            sum(self.lengths.values()) / self.count if self.count else 0
        )

    def search(self, query, *, categories=None, limit=10):
        if limit < 1:
            raise ValueError("limit must be a positive")

        if not self.count or not self.avg_length:
            return []

        allowed_categories = set(categories) if categories else None
        scores = defaultdict(float)

        for term in sorted(set(tokenize(query))):
            matches = self.postings.get(term)

            if not matches:
                continue

            document_frequency = len(matches)

            idf = math.log1p(
                (self.count - document_frequency + 0.5) /
                (document_frequency + 0.5)
            )

            for chunk_id, frenquency in matches.items():
                chunk = self.chunks[chunk_id]

                if (
                    allowed_categories is not None and
                    chunk.get("category") not in allowed_categories
                ):
                    continue

                legth_ratio =(
                    self.lengths[chunk_id] / self.avg_length
                )

                denominator = frenquency + self.k1 * (1 - self.b + self.b * legth_ratio)

                scores[chunk_id] += idf * frenquency * (self.k1 + 1) / denominator

        ranked = sorted(
                scores.items(),
                key=lambda item: (-item[1], item[0])
            )

        return [
                {
                    **self.chunks[chunk_id],
                    "bm25_score": score,
                }
                for chunk_id, score in ranked[:limit]
            ]
