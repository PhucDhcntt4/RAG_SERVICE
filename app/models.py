from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field, StringConstraints, model_validator

ShortText = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=500)]
Category = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=100)]
MAX_CHAT_HISTORY_MESSAGE_CHARS = 20000


class InputModel(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)


class SearchRequest(InputModel):
    query: str = Field(min_length=1, max_length=4000)
    categories: list[Category] | None = Field(default=None, max_length=20)
    doc_type_id: int | None = Field(default=None, ge=1)
    group_ids: list[int] | None = Field(default=None, max_length=20)
    top_k: int | None = Field(default=None, ge=1, le=20)

    @model_validator(mode="after")
    def valid_retrieval_scope(self):
        if self.categories is not None and (
            self.doc_type_id is not None or self.group_ids is not None
        ):
            raise ValueError(
                "Chỉ dùng categories hoặc doc_type_id/group_ids cho một yêu cầu"
            )
        if self.group_ids is not None:
            if not self.group_ids:
                raise ValueError("group_ids không được là danh sách rỗng")
            if len(set(self.group_ids)) != len(self.group_ids):
                raise ValueError("group_ids không được trùng nhau")
        return self


class Source(BaseModel):
    source_key: str
    title: str
    category: str
    heading: str | None = None
    chunk_index: int
    similarity: float | None = None
    bm25_score: float | None = None
    rrf_score: float | None = None
    bm25_rank: int | None = None
    vector_rank: int | None = None


class SearchResponse(BaseModel):
    success: bool
    status: Literal["knowledge_found", "knowledge_not_found"]
    content: str
    sources: list[Source]
    elapsed_ms: float


class ChatMessage(InputModel):
    role: Literal["user", "assistant"]
    content: str = Field(min_length=1, max_length=MAX_CHAT_HISTORY_MESSAGE_CHARS)


class ChatRequest(SearchRequest):
    history: list[ChatMessage] = Field(default_factory=list, max_length=12)

    @model_validator(mode="after")
    def bounded_history(self):
        if sum(len(message.content) for message in self.history) > 24000:
            raise ValueError("Lịch sử quá dài; hãy bắt đầu hội thoại mới")
        return self


class ChatSource(Source):
    citation: str


class ChatResponse(BaseModel):
    answer: str
    status: Literal["answered", "insufficient_context"]
    sources: list[ChatSource]
    retrieval_query: str
    context: str
    elapsed_ms: float
    model: str


class DocumentRequest(InputModel):
    source_key: ShortText
    title: ShortText
    category: Category = "customer_care"
    doc_type_id: int | None = Field(default=None, ge=1)
    group_id: int | None = Field(default=None, ge=1)
    text: str = Field(min_length=1, max_length=1000000)


class ActiveRequest(InputModel):
    is_active: bool


class DocTypeCreate(InputModel):
    code: str = Field(min_length=1, max_length=100, pattern=r"^[a-z0-9][a-z0-9_-]*$")
    name: ShortText
    description: str = Field(default="", max_length=2000)
    sort_order: int = Field(default=0, ge=-10000, le=10000)


class DocTypeUpdate(InputModel):
    name: ShortText
    description: str = Field(default="", max_length=2000)
    is_active: bool = True
    sort_order: int = Field(default=0, ge=-10000, le=10000)


class GroupCreate(DocTypeCreate):
    doc_type_id: int = Field(ge=1)


class GroupUpdate(DocTypeUpdate):
    doc_type_id: int = Field(ge=1)


class ClassificationRequest(InputModel):
    group_id: int = Field(ge=1)
