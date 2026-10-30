from typing import Literal

from pydantic import BaseModel, ConfigDict, Field


class ApiModel(BaseModel):
    model_config = ConfigDict(
        extra="forbid",
        str_strip_whitespace=True,
    )


class ProductVariant(ApiModel):
    sku: str = ""
    barcode: str = ""
    external_id: str = ""
    variant_title: str = ""
    color: str = ""
    size: str = ""
    price: float | None = None
    compare_at_price: float | None = None
    inventory_quantity: int | None = None
    available: bool | None = None


class ProductSource(ApiModel):
    citation: str
    product_code: str
    title: str
    product_type: str = ""
    vendor: str = ""
    status: str = ""
    description: str = ""
    material: str = ""
    sole: str = ""
    height: str = ""
    handle: str = ""
    aliases: list[str] = Field(default_factory=list)
    attributes: list[str] = Field(default_factory=list)
    image_url: str | None = None
    image_urls: list[str] = Field(default_factory=list)
    online_store_url: str | None = None
    created_at: str | None = None
    updated_at: str | None = None
    published_at: str | None = None
    new_at: str | None = None
    minimum_price: float | None = None
    maximum_price: float | None = None
    inventory_synced_at: str | None = None
    inventory_status: Literal["in_stock", "out_of_stock", "unknown"] = "unknown"
    total_inventory: int | None = None
    available_variant_count: int = 0
    variants: list[ProductVariant] = Field(default_factory=list)
    image_similarity: float | None = Field(default=None, ge=-1, le=1)
    matched_image_url: str | None = None
    matched_color: str = ""


class ProductResultMeta(ApiModel):
    page: int = Field(ge=1)
    page_size: int = Field(ge=1, le=50)
    shown: int = Field(ge=0)
    total: int | None = Field(default=None, ge=0)
    total_pages: int | None = Field(default=None, ge=0)
    has_more: bool = False
    date_from: str | None = None
    date_to: str | None = None


class ProductImageQuery(ApiModel):
    filename: str
    media_type: str
    size_bytes: int = Field(ge=1)


class ProductImageSearchResponse(ApiModel):
    query_image: ProductImageQuery
    products: list[ProductSource] = Field(default_factory=list)
    result: ProductResultMeta
    low_confidence: bool = False
    best_similarity: float | None = Field(default=None, ge=-1, le=1)

