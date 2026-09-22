"""Initialize PostgreSQL metadata used alongside Qdrant."""

import argparse

import psycopg

from app.config import Settings
from app.product_sync.inventory_repository import InventorySyncRepository
from app.product_sync.repository import ProductSyncRepository
from app.taxonomy_repository import TaxonomyRepository


def main():
    parser = argparse.ArgumentParser(
        description=(
            "Tạo bảng phân loại tài liệu, Product Sync và Inventory Sync "
            "trong PostgreSQL"
        )
    )
    parser.add_argument(
        "--confirm",
        action="store_true",
        help="Xác nhận DATABASE_URL đang trỏ đúng PostgreSQL đích",
    )
    args = parser.parse_args()
    if not args.confirm:
        parser.error("Kiểm tra DATABASE_URL rồi thêm --confirm để khởi tạo")

    try:
        settings = Settings.load()
        taxonomy = TaxonomyRepository(settings)
        tree = taxonomy.initialize()
        taxonomy.ready()

        product_sync = ProductSyncRepository(settings)
        product_sync.initialize()

        inventory_sync = InventorySyncRepository(settings)
        inventory_sync.initialize()
    except (psycopg.Error, RuntimeError) as exc:
        raise SystemExit(
            f"Không khởi tạo được PostgreSQL taxonomy ({type(exc).__name__}). "
            "Kiểm tra DATABASE_URL, trạng thái PostgreSQL và quyền CREATE."
        ) from None

    print(
        "PostgreSQL taxonomy initialized: "
        f"{len(tree)} document types, "
        f"{sum(len(doc_type['groups']) for doc_type in tree)} groups."
    )
    print("Qdrant vectors were not changed.")
    print("Product sync and inventory sync metadata initialized.")


if __name__ == "__main__":
    main()
