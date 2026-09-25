from __future__ import annotations

import argparse
import json
import os
import re
import sys
import time
from pathlib import Path
from typing import Any

import httpx
from dotenv import dotenv_values


SHOP_RE = re.compile(r"^[a-z0-9][a-z0-9-]*\.myshopify\.com$", re.IGNORECASE)
VERSION_RE = re.compile(r"^\d{4}-\d{2}$")


class ShopifyError(RuntimeError):
    pass


def find_project_root() -> Path:
    """Tìm thư mục chứa .env từ file hiện tại đi ngược lên trên."""
    current = Path(__file__).resolve().parent

    for candidate in (current, *current.parents):
        if (candidate / ".env").exists():
            return candidate

    return current


ROOT = find_project_root()


PRODUCT_BY_SKU_QUERY = """
query ProductBySku($first: Int!, $query: String!) {
  products(first: $first, query: $query) {
    nodes {
      id
      legacyResourceId

      title
      handle
      description
      descriptionHtml
      vendor
      productType
      status
      tags

      # 3 DATE FIELDS
      createdAt
      updatedAt
      publishedAt

      onlineStoreUrl

      seo {
        title
        description
      }

      options {
        id
        name
        position
        values
      }

      variants(first: 100) {
        nodes {
          id
          legacyResourceId

          title
          displayName

          sku
          barcode

          price
          compareAtPrice

          inventoryQuantity
          availableForSale
          taxable

          createdAt
          updatedAt

          selectedOptions {
            name
            value
          }
        }

        pageInfo {
          hasNextPage
          endCursor
        }
      }

      media(first: 100) {
        nodes {
          id
          mediaContentType
          alt

          preview {
            image {
              id
              url
              altText
              width
              height
            }
          }

          ... on MediaImage {
            image {
              id
              url
              altText
              width
              height
            }
          }
        }

        pageInfo {
          hasNextPage
          endCursor
        }
      }

      metafields(first: 100) {
        nodes {
          id
          namespace
          key
          type
          value
          createdAt
          updatedAt
        }

        pageInfo {
          hasNextPage
          endCursor
        }
      }
    }
  }
}
"""


def load_config() -> tuple[str, str, str]:
    values = {**dotenv_values(ROOT / ".env"), **os.environ}

    shop = str(values.get("SHOP") or "").strip().lower()
    token = str(values.get("SHOPIFY_TOKEN") or "").strip()
    api_version = str(
        values.get("SHOPIFY_API_VERSION") or "2026-01"
    ).strip()

    if not SHOP_RE.fullmatch(shop):
        raise ShopifyError(
            "SHOP phải có dạng ten-shop.myshopify.com trong .env"
        )

    if not token:
        raise ShopifyError("Thiếu SHOPIFY_TOKEN trong .env")

    if not VERSION_RE.fullmatch(api_version):
        raise ShopifyError(
            "SHOPIFY_API_VERSION phải có dạng YYYY-MM"
        )

    return shop, token, api_version


def make_sku_query(sku: str) -> str:
    sku = sku.strip()

    if not sku:
        raise ShopifyError("SKU không được để trống")

    # Escape ký tự đặc biệt cơ bản cho Shopify search syntax.
    escaped = (
        sku.replace("\\", "\\\\")
        .replace('"', '\\"')
    )

    return f'sku:"{escaped}"'


class ShopifyClient:
    def __init__(self) -> None:
        shop, token, api_version = load_config()

        self.endpoint = (
            f"https://{shop}/admin/api/"
            f"{api_version}/graphql.json"
        )

        self.client = httpx.Client(
            timeout=httpx.Timeout(30.0, connect=10.0),
            headers={
                "Content-Type": "application/json",
                "X-Shopify-Access-Token": token,
            },
        )

    def close(self) -> None:
        self.client.close()

    def _graphql(
        self,
        query: str,
        variables: dict[str, Any],
    ) -> dict[str, Any]:

        last_error: Exception | None = None

        for attempt in range(4):
            try:
                response = self.client.post(
                    self.endpoint,
                    json={
                        "query": query,
                        "variables": variables,
                    },
                )

                if response.status_code in (401, 403):
                    raise ShopifyError(
                        "Shopify từ chối xác thực. "
                        "Kiểm tra SHOPIFY_TOKEN và quyền read_products."
                    )

                if (
                    response.status_code == 429
                    or response.status_code >= 500
                ):
                    if attempt < 3:
                        time.sleep(2 ** attempt)
                        continue

                response.raise_for_status()
                payload = response.json()

                errors = payload.get("errors") or []

                if errors:
                    throttled = any(
                        (item.get("extensions") or {}).get("code")
                        == "THROTTLED"
                        for item in errors
                    )

                    if throttled and attempt < 3:
                        time.sleep(2 ** attempt)
                        continue

                    message = "; ".join(
                        str(
                            item.get("message")
                            or "GraphQL error"
                        )
                        for item in errors[:5]
                    )

                    raise ShopifyError(
                        f"Shopify GraphQL lỗi: {message}"
                    )

                data = payload.get("data")

                if not isinstance(data, dict):
                    raise ShopifyError(
                        "Shopify trả về payload không hợp lệ"
                    )

                return data

            except ShopifyError:
                raise

            except (httpx.HTTPError, ValueError) as exc:
                last_error = exc

                if attempt < 3:
                    time.sleep(2 ** attempt)
                    continue

        raise ShopifyError(
            "Không thể gọi Shopify Admin API"
        ) from last_error

    def find_products_by_sku(
        self,
        sku: str,
        first: int = 20,
    ) -> list[dict[str, Any]]:
        """
        Tìm product theo SKU.

        Shopify search dùng filter sku:...
        Sau khi nhận kết quả, code kiểm tra lại SKU chính xác
        trên variants để tránh lấy nhầm kết quả.
        """
        target_sku = sku.strip()

        if not target_sku:
            raise ShopifyError("SKU không được để trống")

        data = self._graphql(
            PRODUCT_BY_SKU_QUERY,
            {
                "first": min(max(first, 1), 100),
                "query": make_sku_query(target_sku),
            },
        )

        products = (
            (data.get("products") or {}).get("nodes")
            or []
        )

        exact_products: list[dict[str, Any]] = []

        for product in products:
            variants = (
                (product.get("variants") or {}).get("nodes")
                or []
            )

            matched_variants = [
                variant
                for variant in variants
                if str(
                    variant.get("sku") or ""
                ).strip().casefold()
                == target_sku.casefold()
            ]

            if matched_variants:
                # Gắn thêm variant khớp SKU để tiện sử dụng.
                product["_matchedVariants"] = matched_variants
                exact_products.append(product)

        return exact_products

    def find_product_by_sku(
        self,
        sku: str,
    ) -> dict[str, Any] | None:
        """
        Trả product đầu tiên có SKU khớp chính xác.
        """
        products = self.find_products_by_sku(sku)

        if not products:
            return None

        return products[0]


def print_product_summary(
    product: dict[str, Any],
    searched_sku: str,
) -> None:

    print("=" * 75)
    print("SHOPIFY PRODUCT BY SKU")
    print("=" * 75)

    print(f"Search SKU     : {searched_sku}")
    print(f"Product ID     : {product.get('id')}")
    print(
        f"Legacy ID      : "
        f"{product.get('legacyResourceId')}"
    )
    print(f"Title          : {product.get('title')}")
    print(f"Handle         : {product.get('handle')}")
    print(f"Status         : {product.get('status')}")
    print(f"Vendor         : {product.get('vendor')}")
    print(
        f"Product Type   : "
        f"{product.get('productType')}"
    )

    print()
    print("PRODUCT DATE")
    print("-" * 75)
    print(
        f"createdAt      : "
        f"{product.get('createdAt')}"
    )
    print(
        f"updatedAt      : "
        f"{product.get('updatedAt')}"
    )
    print(
        f"publishedAt    : "
        f"{product.get('publishedAt')}"
    )

    print()
    print("MATCHED VARIANT")
    print("-" * 75)

    matched = product.get("_matchedVariants") or []

    for index, variant in enumerate(
        matched,
        start=1,
    ):
        print(f"Variant #{index}")
        print(f"  ID           : {variant.get('id')}")
        print(
            f"  Legacy ID    : "
            f"{variant.get('legacyResourceId')}"
        )
        print(f"  Title        : {variant.get('title')}")
        print(f"  SKU          : {variant.get('sku')}")
        print(
            f"  Barcode      : "
            f"{variant.get('barcode')}"
        )
        print(f"  Price        : {variant.get('price')}")
        print(
            f"  Compare price: "
            f"{variant.get('compareAtPrice')}"
        )
        print(
            f"  Inventory    : "
            f"{variant.get('inventoryQuantity')}"
        )
        print(
            f"  Available    : "
            f"{variant.get('availableForSale')}"
        )
        print(
            f"  Created      : "
            f"{variant.get('createdAt')}"
        )
        print(
            f"  Updated      : "
            f"{variant.get('updatedAt')}"
        )

        options = variant.get("selectedOptions") or []

        if options:
            print("  Options      :")
            for option in options:
                print(
                    f"    - "
                    f"{option.get('name')}: "
                    f"{option.get('value')}"
                )

    print()
    print("ALL VARIANTS")
    print("-" * 75)

    variants = (
        (product.get("variants") or {}).get("nodes")
        or []
    )

    for index, variant in enumerate(
        variants,
        start=1,
    ):
        print(
            f"{index}. "
            f"SKU={variant.get('sku') or '-'} | "
            f"Price={variant.get('price')} | "
            f"Inventory="
            f"{variant.get('inventoryQuantity')} | "
            f"Available="
            f"{variant.get('availableForSale')}"
        )


def main() -> int:
    parser = argparse.ArgumentParser(
        description=(
            "Tìm sản phẩm Shopify theo SKU và lấy "
            "createdAt, updatedAt, publishedAt."
        )
    )

    parser.add_argument(
        "sku",
        nargs="?",
        help="SKU cần tìm, ví dụ ABC-001",
    )

    parser.add_argument(
        "--json",
        action="store_true",
        help="In toàn bộ kết quả dưới dạng JSON.",
    )

    parser.add_argument(
        "--output",
        help=(
            "Lưu JSON ra file, "
            "ví dụ product.json"
        ),
    )

    args = parser.parse_args()

    sku = str(args.sku or "").strip()

    if not sku:
        sku = input("Nhập SKU cần tìm: ").strip()

    if not sku:
        print("SKU không được để trống.", file=sys.stderr)
        return 1

    client = ShopifyClient()

    try:
        products = client.find_products_by_sku(sku)

        if not products:
            print(
                f"Không tìm thấy sản phẩm có SKU: {sku}",
                file=sys.stderr,
            )
            return 1

        if len(products) > 1:
            print(
                f"Cảnh báo: tìm thấy {len(products)} "
                f"sản phẩm có SKU '{sku}'."
            )
            print(
                "Có thể SKU đang bị trùng trong Shopify."
            )
            print()

        # Mặc định hiển thị sản phẩm đầu tiên.
        product = products[0]

        if args.json:
            print(
                json.dumps(
                    product,
                    ensure_ascii=False,
                    indent=2,
                )
            )
        else:
            print_product_summary(product, sku)

        if args.output:
            output_path = Path(args.output).resolve()

            output_path.write_text(
                json.dumps(
                    product,
                    ensure_ascii=False,
                    indent=2,
                ),
                encoding="utf-8",
            )

            print()
            print(f"Đã lưu JSON: {output_path}")

        return 0

    except ShopifyError as exc:
        print(f"Lỗi: {exc}", file=sys.stderr)
        return 2

    finally:
        client.close()


if __name__ == "__main__":
    raise SystemExit(main())
