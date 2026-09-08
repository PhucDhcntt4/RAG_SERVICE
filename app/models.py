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
    top_k: int | None = Field(default=None, ge=1, le=20)


class Source(BaseModel):
    source_key: str
    title: str
    category: str
    heading: str | None = None
    chunk_index: int
    similarity: float


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
    text: str = Field(min_length=1, max_length=1000000)


class ActiveRequest(InputModel):
    is_active: bool
