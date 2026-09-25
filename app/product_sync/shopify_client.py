from __future__ import annotations

import logging
import os
import re
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Iterator

import httpx
from dotenv import dotenv_values

logger = logging.getLogger("rag_service.product_sync.shopify")

ROOT = Path(__file__).resolve().parents[2]
SHOP_RE = re.compile(r"^[a-z0-9][a-z0-9-]*\.myshopify\.com$", re.IGNORECASE)
VERSION_RE = re.compile(r"^\d{4}-\d{2}$")


class ShopifyError(RuntimeError):
    pass


@dataclass(frozen=True)
class ShopifyConfig:
    shop: str
    token: str
    api_version: str = "2026-01"

    @classmethod
    def load(cls) -> "ShopifyConfig":
        values = {**dotenv_values(ROOT / ".env"), **os.environ}

        shop = str(values.get("SHOP") or "").strip().lower()
        token = str(values.get("SHOPIFY_TOKEN") or "").strip()
        version = str(values.get("SHOPIFY_API_VERSION") or "2026-01").strip()

        if not SHOP_RE.fullmatch(shop):
            raise ShopifyError("SHOP phải có dạng ten-shop.myshopify.com")
        if not token:
            raise ShopifyError("Thiếu SHOPIFY_TOKEN")
        if not VERSION_RE.fullmatch(version):
            raise ShopifyError("SHOPIFY_API_VERSION phải có dạng YYYY-MM")

        return cls(shop=shop, token=token, api_version=version)


PRODUCTS_QUERY = """
query ProductSyncPage($first: Int!, $after: String, $query: String!) {
  products(first: $first, after: $after, query: $query, sortKey: UPDATED_AT) {
    nodes {
      id
      legacyResourceId
      title
      handle
      vendor
      productType
      description
      status
      updatedAt
      onlineStoreUrl

      variants(first: 100) {
        nodes {
          id
          legacyResourceId
          sku
          barcode
          title
          price
          compareAtPrice
          inventoryQuantity
          availableForSale
          selectedOptions {
            name
            value
          }
        }
      }

      media(first: 100) {
        nodes {
          ... on MediaImage {
            id
            image {
              id
              url
              altText
              width
              height
            }
          }
        }
      }
    }

    pageInfo {
      hasNextPage
      endCursor
    }
  }
}
"""


PRODUCT_KEYS_QUERY = """
query ProductDeltaPage($first: Int!, $after: String, $query: String!) {
  products(first: $first, after: $after, query: $query, sortKey: UPDATED_AT) {
    nodes {
      id
      legacyResourceId
      status
      updatedAt

      variants(first: 100) {
        nodes {
          sku
        }
      }
    }

    pageInfo {
      hasNextPage
      endCursor
    }
  }
}
"""


INVENTORY_PRODUCTS_QUERY = """
query InventorySyncPage($first: Int!, $after: String, $query: String!) {
  products(first: $first, after: $after, query: $query, sortKey: UPDATED_AT) {
    nodes {
      id
      legacyResourceId
      status

      variants(first: 100) {
        nodes {
          id
          legacyResourceId
          sku
          inventoryQuantity
          availableForSale
          selectedOptions {
            name
            value
          }
        }
      }
    }

    pageInfo {
      hasNextPage
      endCursor
    }
  }
}
"""


def escape_shopify_search(value: str) -> str:
    return (
        str(value or "")
        .replace("\\", "\\\\")
        .replace('"', '\\"')
    )


def variant_skus_from_shopify(product: dict) -> list[str]:
    variants = ((product.get("variants") or {}).get("nodes") or [])
    result: list[str] = []
    seen: set[str] = set()

    for variant in variants:
        sku = str(variant.get("sku") or "").strip()
        if sku and sku not in seen:
            seen.add(sku)
            result.append(sku)

    return result


def product_code_from_shopify(product: dict) -> str | None:
    """Phase 1 current rule: first non-empty variant SKU is product_code.

    Phase 1.5 audits this rule before Phase 2 is allowed to write Qdrant.
    """
    skus = variant_skus_from_shopify(product)
    return skus[0] if skus else None


def aggregate_shopify_status(products: list[dict]) -> str:
    """Return the effective Shopify status for one grouped product code."""
    statuses = {
        str(product.get("status") or "").strip().upper()
        for product in products
        if str(product.get("status") or "").strip()
    }

    # A product code remains searchable while at least one Shopify source is ACTIVE.
    for status in ("ACTIVE", "UNLISTED", "DRAFT", "ARCHIVED"):
        if status in statuses:
            return status
    return "INACTIVE"


class ShopifyClient:
    def __init__(self, config: ShopifyConfig):
        self.config = config
        self.endpoint = (
            f"https://{config.shop}/admin/api/"
            f"{config.api_version}/graphql.json"
        )
        self.client = httpx.Client(
            timeout=httpx.Timeout(30.0, connect=10.0),
            headers={
                "Content-Type": "application/json",
                "X-Shopify-Access-Token": config.token,
            },
        )

    def close(self):
        self.client.close()

    def _graphql(self, query: str, variables: dict) -> dict:
        last_error: Exception | None = None

        for attempt in range(4):
            try:
                response = self.client.post(
                    self.endpoint,
                    json={"query": query, "variables": variables},
                )

                if response.status_code in (401, 403):
                    raise ShopifyError(
                        "Shopify từ chối xác thực. Kiểm tra SHOPIFY_TOKEN và quyền app."
                    )

                if response.status_code == 429 or response.status_code >= 500:
                    if attempt < 3:
                        time.sleep(2 ** attempt)
                        continue

                response.raise_for_status()
                payload = response.json()

                errors = payload.get("errors") or []
                if errors:
                    throttled = any(
                        (item.get("extensions") or {}).get("code") == "THROTTLED"
                        for item in errors
                    )
                    if throttled and attempt < 3:
                        time.sleep(2 ** attempt)
                        continue

                    message = "; ".join(
                        str(item.get("message") or "GraphQL error")
                        for item in errors[:3]
                    )
                    raise ShopifyError(f"Shopify GraphQL lỗi: {message}")

                data = payload.get("data")
                if not isinstance(data, dict):
                    raise ShopifyError("Shopify trả về payload không hợp lệ")

                return data

            except ShopifyError:
                raise
            except (httpx.HTTPError, ValueError) as exc:
                last_error = exc
                if attempt < 3:
                    time.sleep(2 ** attempt)
                    continue

        raise ShopifyError("Không thể gọi Shopify Admin API") from last_error

    def _iter_products(
        self,
        query: str,
        *,
        graphql_query: str = PRODUCTS_QUERY,
        page_size: int = 50,
    ) -> Iterator[dict]:
        after = None

        while True:
            data = self._graphql(
                graphql_query,
                {
                    "first": page_size,
                    "after": after,
                    "query": query,
                },
            )

            connection = data.get("products") or {}
            nodes = connection.get("nodes") or []

            for product in nodes:
                yield product

            page_info = connection.get("pageInfo") or {}
            if not page_info.get("hasNextPage"):
                break

            after = page_info.get("endCursor")
            if not after:
                raise ShopifyError(
                    "Shopify báo còn trang nhưng không trả endCursor"
                )

    def iter_active_products(self, page_size: int = 50) -> Iterator[dict]:
        yield from self._iter_products(
            "status:active",
            graphql_query=PRODUCTS_QUERY,
            page_size=page_size,
        )

    def iter_active_inventory_products(
        self,
        page_size: int = 100,
    ) -> Iterator[dict]:
        """Load the minimal ACTIVE product fields needed by Inventory Sync."""
        yield from self._iter_products(
            "status:active",
            graphql_query=INVENTORY_PRODUCTS_QUERY,
            page_size=min(max(int(page_size), 1), 100),
        )

    def iter_all_product_keys(self, page_size: int = 100) -> Iterator[dict]:
        """Load only ID/status/updatedAt/SKU for Delta Sync bootstrap."""
        yield from self._iter_products(
            "",
            graphql_query=PRODUCT_KEYS_QUERY,
            page_size=page_size,
        )

    def iter_changed_products(
        self,
        updated_after: str,
        updated_before: str,
        page_size: int = 100,
    ) -> Iterator[dict]:
        """Load products changed in a time window, regardless of status."""
        query = (
            f"updated_at:>'{updated_after}' "
            f"updated_at:<='{updated_before}'"
        )
        yield from self._iter_products(
            query,
            graphql_query=PRODUCT_KEYS_QUERY,
            page_size=page_size,
        )

    def iter_products_by_sku(
        self,
        sku: str,
        page_size: int = 50,
    ) -> Iterator[dict]:
        """Load full Shopify products for one SKU, regardless of status."""
        escaped = escape_shopify_search(sku)
        yield from self._iter_products(
            f'sku:"{escaped}"',
            graphql_query=PRODUCTS_QUERY,
            page_size=page_size,
        )


def exact_products_for_code(
    client: ShopifyClient,
    product_code: str,
) -> list[dict]:
    """Return all Shopify sources whose first non-empty SKU is product_code."""
    target = str(product_code or "").strip().casefold()
    if not target:
        return []

    result = []
    for product in client.iter_products_by_sku(product_code):
        current = product_code_from_shopify(product)
        if current and current.casefold() == target:
            result.append(product)
    return result
