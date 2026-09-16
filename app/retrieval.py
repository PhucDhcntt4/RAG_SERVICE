import re
import unicodedata


SECTION_STOPWORDS = set(
    "chinh sach quy dinh quy trinh thong tin chi tiet tong quan cua la gi va ve cho toi "
    "biet cac nhung nao nhu the nao co duoc ap dung tai trong voi mot hay "
    "giai thich toan bo day du muc khi theo".split()
)
# The source string above contains multi-word phrases such as "chinh sach" and
# "tong quan". Their individual words are not all generic: "sach" and "quan"
# carry strong meaning in headings like "Danh sách Quản lý". Keeping them as
# stopwords prevents the exact section from being expanded.
SECTION_STOPWORDS.difference_update({"sach", "quan"})


def normalized_query(query: str) -> str:
    value = unicodedata.normalize("NFD", query.lower()).replace("đ", "d")
    return " ".join(re.findall(r"[a-z0-9]+", "".join(
        char for char in value if not unicodedata.combining(char))))


def needs_policy_context(query: str) -> bool:
    return bool(re.search(r"\b(?:chinh sach|quy dinh|quy trinh)\b", normalized_query(query)))


def needs_section_context(query: str) -> bool:
    """Explanations, tables and policies need surrounding section evidence."""
    value = normalized_query(query)
    return needs_policy_context(query) or bool(re.search(
        r"\bbang\s+(?:tinh|loi|thuong|phat|quy|gia|dinh|phan|cham|diem)\b"
        r"|\b(?:la gi|gom nhung gi|bao gom|chi tiet|tong quan|cach tinh|cach ap dung)\b"
        r"|\bphuc loi\b",
        value,
    ))


def heading_context_match(rows, query: str) -> bool:
    """Detect a broad section request from strong query-to-heading overlap.

    This complements phrase-based intent detection for short queries such as a
    pasted heading without "la gi". It is intentionally based on each
    document's structure instead of topic-specific keywords.
    """
    terms = set(normalized_query(query).split()) - SECTION_STOPWORDS
    if len(terms) < 3:
        return False
    for row in rows:
        for heading in row.get("section_path") or ():
            heading_terms = set(normalized_query(heading).split()) - SECTION_STOPWORDS
            if not heading_terms:
                continue
            overlap = len(terms & heading_terms)
            if overlap >= 3 and (
                overlap / len(terms) >= 0.5
                or overlap / len(heading_terms) >= 0.6
            ):
                return True
    return False


def section_scope(path, *, include_parent=False):
    """Choose the closest numbered section above letter/bullet subgroups."""
    path = tuple(path or ())
    for index in range(len(path) - 1, -1, -1):
        if re.match(r"^(?:\d+(?:\.\d+)*[.)]?|[IVXLCDM]+[.)])\s+", path[index]):
            # A policy question about 2.3 needs sibling conditions/procedures
            # under 2, not just the small subsection that matched the query.
            number = re.match(r"^(\d+(?:\.\d+)*)", path[index])
            if include_parent and number and "." in number.group(1):
                parent = number.group(1).rsplit(".", 1)[0]
                for ancestor in range(index - 1, -1, -1):
                    if re.match(r"^" + re.escape(parent) + r"[.)]?\s+", path[ancestor]):
                        return path[:ancestor + 1]
            return path[:index + 1]
    return path


def select_section_scopes(rows, query):
    broad = needs_section_context(query) or heading_context_match(rows, query)
    terms = set(normalized_query(query).split()) - SECTION_STOPWORDS
    scopes = []
    for row in rows:
        path = tuple(row.get('section_path') or ())
        if row.get('document_id') is None or not path:
            continue

        ancestor_scores = [
            len(terms & set(normalized_query(heading).split()))
            for heading in path
        ]
        best = max(ancestor_scores, default=0)
        if broad and best >= 2:

            index = ancestor_scores.index(best)
            scope = path[:index + 1]
        else:
            scope = section_scope(path, include_parent=broad)
        candidate = (row['document_id'], scope)
        if candidate not in scopes:
            scopes.append(candidate)
    # A weak broad ancestor (for example "PHẦN I ... CÔNG TY") must not
    # swallow a stronger child scope ("1.2. Tầm nhìn – Sứ mệnh – Giá trị cốt lõi").
    # Keeping both would expand the whole part and overview condensation would
    # retain only the first chunk of that child section.
    scopes = [
        (document_id, path)
        for document_id, path in scopes
        if not any(
            other_document_id == document_id
            and len(other_path) > len(path)
            and other_path[:len(path)] == path
            for other_document_id, other_path in scopes
        )
    ]
    scores = [len(terms & set(normalized_query(path[-1]).split())) for _, path in scopes]
    if scores and max(scores) >= 2:
        strongest = max(scores)
        return [scope for scope, score in zip(scopes, scores) if score == strongest]
    return scopes


def is_broad_part_scope(path) -> bool:
    """Whether a scope represents a whole named document part."""
    return bool(path and re.match(r"^phan\s+[ivxlcdm]+\b", normalized_query(path[-1])))


def condense_part_overview(rows, scopes):
    """Keep one representative chunk per direct child of a broad document part."""
    part_scopes = {(document_id, tuple(path)) for document_id, path in scopes
                   if is_broad_part_scope(path)}
    if not part_scopes:
        return rows
    selected = []
    seen = set()
    for row in rows:
        path = tuple(row.get('section_path') or ())
        matching = next((scope for document_id, scope in part_scopes
                         if document_id == row.get('document_id')
                         and path[:len(scope)] == scope), None)
        if matching is None:
            key = (row.get('document_id'), path, row.get('chunk_id'))
        else:
            child = path[len(matching)] if len(path) > len(matching) else None
            key = (row.get('document_id'), matching, child)
        if key not in seen:
            seen.add(key)
            selected.append(row)
    return selected


def needs_document_context(query: str) -> bool:
    """Broad questions need surrounding document evidence, across all topics."""
    value = unicodedata.normalize("NFD", query.lower()).replace("đ", "d")
    value = "".join(char for char in value if not unicodedata.combining(char))
    value = " ".join(re.findall(r"[a-z0-9]+", value))
    if any(re.search(r"\b" + phrase + r"\b", value) for phrase in (
        "liet ke", "danh sach", "tat ca", "toan bo", "tong cong", "tong so", "day du",
    )):
        return True
    return bool(re.search(r"\bco\b.+\bo dau\b|\b(?:cac|nhung)\b.+\b(?:o dau|nao)\b", value))


def reciprocal_rank_fusion(
    bm25_rows,
    vector_rows,
    *,
    k=60,
    limit=20,
):
    if not isinstance(k, int) or k < 1:
        raise ValueError("k must be a positive integer")

    if not isinstance(limit, int) or limit < 1:
        raise ValueError("limit must be a positive integer")

    candidates = {}

    branches = (
        ("bm25", bm25_rows),
        ("vector", vector_rows),
    )

    for branch_name, rows in branches:
        seen = set()

        for rank, row in enumerate(rows, start=1):
            chunk_id = row["chunk_id"]

            # Một chunk chỉ được đóng góp một lần trong mỗi nhánh.
            if chunk_id in seen:
                raise ValueError(
                    f"Duplicate chunk_id in {branch_name}: {chunk_id}"
                )

            seen.add(chunk_id)

            if chunk_id not in candidates:
                candidates[chunk_id] = {
                    **row,
                    "bm25_rank": None,
                    "vector_rank": None,
                    "bm25_score": None,
                    "similarity": None,
                    "rrf_score": 0.0,
                }

            candidate = candidates[chunk_id]

            candidate[f"{branch_name}_rank"] = rank
            candidate["rrf_score"] += 1.0 / (k + rank)

            if branch_name == "bm25":
                candidate["bm25_score"] = row["bm25_score"]
            else:
                candidate["similarity"] = row["similarity"]

    ranked = sorted(
        candidates.values(),
        key=lambda row: (-row["rrf_score"], row["chunk_id"]),
    )

    return ranked[:limit]
