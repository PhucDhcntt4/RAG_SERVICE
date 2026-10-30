import os
from pathlib import Path
from typing import Literal

from dotenv import dotenv_values # type: ignore
from pydantic import BaseModel, ConfigDict, Field, SecretStr, model_validator # type: ignore

ROOT = Path(__file__).resolve().parent.parent


class Settings(BaseModel):
    model_config = ConfigDict(extra="ignore", hide_input_in_errors=True)

    search_api_key: SecretStr
    admin_api_key: SecretStr
    gemini_api_key: SecretStr
    database_url: SecretStr | None = None
    rag_embedding_model: Literal["gemini-embedding-001"] = "gemini-embedding-001"
    qdrant_url: str = Field(default="http://127.0.0.1:6333", min_length=1, max_length=500)
    qdrant_api_key: SecretStr | None = None
    qdrant_collection: str = Field(
        default="rag_service_compare_v1",
        pattern=r"^[A-Za-z0-9._-]{1,255}$",
    )
    qdrant_timeout_seconds: int = Field(default=15, ge=1, le=120)
    rag_top_k: int = Field(default=5, ge=1, le=20)
    rag_hybrid_enabled: bool = True
    rag_hybrid_candidates: int = Field(default=20, ge=1, le=100)
    rag_rrf_k: int = Field(default=60, ge=1, le=1000)
    rag_overview_max_documents: int = Field(default=3, ge=1, le=5)
    rag_overview_max_chunks: int = Field(default=100, ge=1, le=300)
    rag_min_similarity: float = Field(default=0.45, ge=0, le=1)
    rag_max_context_chars: int = Field(default=6000, ge=500, le=30000)
    rag_chunking_strategy: Literal["adaptive", "fixed"] = "adaptive"
    rag_chunk_size: int = Field(default=1200, ge=200, le=4000)
    rag_chunk_overlap: int = Field(default=180, ge=0)
    rag_semantic_min_chars: int = Field(default=300, ge=50, le=3999)
    rag_semantic_breakpoint_percentile: float = Field(default=80, gt=0, lt=100)
    rag_max_document_chars: int = Field(default=200000, ge=100, le=1000000)
    rag_max_chunks: int = Field(default=300, ge=1, le=1000)
    rag_max_upload_bytes: int = Field(default=10485760, ge=1024, le=20971520)
    rag_max_concurrent_requests: int = Field(default=2, ge=1, le=16)
    rag_embedding_timeout_seconds: int = Field(default=30, ge=1, le=120)
    rag_embedding_batch_size: int = Field(default=32, ge=1, le=32)
    rag_embedding_batch_interval_seconds: float = Field(default=0, ge=0, le=60)
    rag_embedding_max_retries: int = Field(default=0, ge=0, le=3)
    rag_chat_model: str = Field(default="gemini-3.1-flash-lite", min_length=1, max_length=100, pattern=r"^[a-zA-Z0-9._-]+$")
    rag_chat_timeout_seconds: int = Field(default=60, ge=5, le=120)
    rag_chat_max_statements: int = Field(default=40, ge=1, le=100)
    rag_chat_max_answer_chars: int = Field(default=12000, ge=1000, le=20000)
    rag_local_admin_enabled: bool = True
    log_level: Literal["DEBUG", "INFO", "WARNING", "ERROR"] = "INFO"
    product_catalog_collection: str = Field(
        default="bot_product_catalog_v1",
        pattern=r"^[A-Za-z0-9._-]{1,255}$",
    )

    product_image_collection: str = Field(
        default="bot_product_images_clip_v2",
        pattern=r"^[A-Za-z0-9._-]{1,255}$",
    )
    product_image_search_enabled: bool = False
    product_clip_model: str = "ViT-B-32"
    product_clip_pretrained: str = "laion2b_s34b_b79k"
    product_clip_device: str = "auto"
    product_image_max_bytes: int = Field(
        default=8 * 1024 * 1024,
        ge=1024,
        le=20 * 1024 * 1024,
    )
    product_image_result_limit: int = Field(default=8, ge=1, le=20)
    product_image_candidate_limit: int = Field(default=50, ge=1, le=200)
    product_image_min_score: float = Field(default=0.25, ge=0, le=1)
    product_vision_enabled: bool = False
    product_vision_model: str | None = None
    product_vision_timeout_seconds: float = Field(default=45, gt=0, le=180)
    product_vision_verify_top_k: int = Field(default=5, ge=1, le=20)
    product_vision_original_candidate_slots: int = Field(default=2, ge=1, le=10)
    product_visual_verification_enabled: bool = False
    product_reference_image_timeout_seconds: float = Field(
        default=15,
        gt=0,
        le=60,
    )
    product_reference_image_max_bytes: int = Field(
        default=6 * 1024 * 1024,
        ge=1024,
        le=20 * 1024 * 1024,
    )
    product_image_fast_accept_score: float = Field(default=0.88, ge=0, le=1)
    product_image_fast_accept_margin: float = Field(default=0.08, ge=0, le=1)
    product_image_vector_weight: float = Field(default=0.55, ge=0, le=1)
    product_image_visual_weight: float = Field(default=0.45, ge=0, le=1)
    product_image_verify_min_score: float = Field(default=0.65, ge=0, le=1)
    product_image_visual_min_score: float = Field(default=0.50, ge=0, le=1)
    product_image_high_confidence: float = Field(default=0.82, ge=0, le=1)
    product_image_medium_confidence: float = Field(default=0.65, ge=0, le=1)

    google_drive_credentials_file: str = "API/service.json"
    google_drive_sync_enabled: bool = False
    google_drive_department_folder_ids: str = ""
    google_drive_poll_seconds: int = Field(
        default=60,
        ge=10,
        le=86400,
    )


    @model_validator(mode="after")
    def validate_settings(self):
        for name in ("gemini_api_key",):
            if not getattr(self, name).get_secret_value().strip():
                raise ValueError(f"Thiếu {name.upper()}")
        keys = [self.search_api_key.get_secret_value(), self.admin_api_key.get_secret_value()]
        if any(len(key) < 32 or not key.isascii() or any(c.isspace() for c in key) for key in keys):
            raise ValueError("API key phải có ít nhất 32 ký tự ASCII, không có khoảng trắng")
        if keys[0] == keys[1]:
            raise ValueError("SEARCH_API_KEY và ADMIN_API_KEY phải khác nhau")
        if self.rag_chunk_overlap >= self.rag_chunk_size:
            raise ValueError("RAG_CHUNK_OVERLAP phải nhỏ hơn RAG_CHUNK_SIZE")
        if (
            self.google_drive_sync_enabled
            and not self.google_drive_credentials_path.is_file()
        ):
            raise ValueError(
                "GOOGLE_DRIVE_CREDENTIALS_FILE không tồn tại hoặc không phải file"
            )
        return self

    @classmethod
    def load(cls):
        # Explicit project path: never load a parent project's .env accidentally.
        values = {**dotenv_values(ROOT / ".env"), **os.environ}
        return cls.model_validate({key.lower(): value for key, value in values.items()})


    @property
    def google_drive_credentials_path(self) -> Path:
        value = Path(self.google_drive_credentials_file)

        if value.is_absolute():
            return value

        return ROOT / value

    @property
    def google_drive_department_folders(self) -> tuple[str, ...]:
        """Parse the comma-separated department folder IDs from .env."""
        values = (
            item.strip()
            for item in self.google_drive_department_folder_ids.split(",")
        )
        return tuple(dict.fromkeys(item for item in values if item))
