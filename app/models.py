from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field, StringConstraints

ShortText = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=500)]
Category = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=100)]


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


class DocumentRequest(InputModel):
    source_key: ShortText
    title: ShortText
    category: Category = "customer_care"
    text: str = Field(min_length=1, max_length=1000000)


class ActiveRequest(InputModel):
    is_active: bool
