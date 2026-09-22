from __future__ import annotations

import argparse
import copy
import json
import logging
import os
from collections import defaultdict
from datetime import datetime
from typing import Callable

from dotenv import dotenv_values

from app.config import ROOT, Settings
from app.product_sync.clip_embedder import OpenClipImageEmbedder
from app.product_sync.grouping import group_shopify_products
from app.product_sync.image_store import ProductImageStore
from app.product_sync.payload_builder import (
    build_catalog_payload,
    inactive_payload,
)
from app.product_sync.qdrant_store import (
    ProductQdrantStore,
    catalog_point_id,
)
from app.product_sync.shopify_client import ShopifyClient, ShopifyConfig

logger = logging.getLogger("rag_service.product_sync.execute")


def clean(value) -> str:
    return str(value or "").strip()


def configure_logging(level):
    logging.basicConfig(
        level=level,
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
    )
    logging.getLogger("httpx").setLevel(logging.WARNING)
    logging.getLogger("httpcore").setLevel(logging.WARNING)


def write_enabled() -> bool:
    values = {**dotenv_values(ROOT / ".env"), **os.environ}
    return str(values.get("PRODUCT_SYNC_WRITE_ENABLED") or "false").strip().lower() in {
        "1",
        "true",
        "yes",
        "on",
    }


def load_groups():
    client = ShopifyClient(ShopifyConfig.load())
    rows = []
    try:
        for index, product in enumerate(client.iter_active_products(), start=1):
            rows.append(product)
            if index % 100 == 0:
                logger.info("Shopify ACTIVE loaded=%s", index)
    finally:
        client.close()

    groups, skipped = group_shopify_products(rows)
    return rows, groups, skipped


def image_identity(checksum: str, color: str) -> tuple[str, str]:
    return clean(checksum), clean(color)


def image_source_kind(point: dict) -> str:
    """Classify ownership of an image point for lifecycle management.

    Explicit source_kind always wins. Legacy points without source_kind are
    treated as Shopify-managed unless they clearly use the recognition source.
    This keeps old Shopify vectors maintainable while protecting vectors
    imported by recognition/marketing clients.
    """
    payload = point.get("payload") or {}

    source_kind = clean(payload.get("source_kind")).casefold()
    if source_kind:
        return source_kind

    source_url = clean(payload.get("source_url")).casefold()
    local_path = clean(payload.get("local_path")).replace("\\", "/").casefold()

    if source_url.startswith("recognition://") or "/recognition/" in f"/{local_path.lstrip('/')}":
        return "recognition"

    # Backward compatibility: old Product RAG image points did not have
    # source_kind, so they remain managed by the Shopify synchronizer.
    return "shopify"


def is_shopify_managed_image(point: dict) -> bool:
    return image_source_kind(point) == "shopify"


def standard_image_payload(
    point_id: int,
    product_code: str,
    product_type: str,
    color: str,
    checksum: str,
) -> dict:
    return {
        "kind": "bot_product_image",
        "source_kind": "shopify",
        "product_image_id": point_id,
        "product_code": product_code,
        "product_type": product_type,
        "color": color,
        "model_name": OpenClipImageEmbedder.MODEL_NAME,
        "pretrained_name": OpenClipImageEmbedder.PRETRAINED_NAME,
        "image_checksum": checksum,
    }


def current_image_index(points: list[dict]):
    """Index only Shopify-managed points.

    External/recognition vectors stay searchable in Qdrant but are never reused
    as Shopify-owned vectors and never become candidates for Shopify cleanup.
    """
    by_identity = defaultdict(list)
    for point in points:
        if not is_shopify_managed_image(point):
            continue

        payload = point.get("payload") or {}
        identity = image_identity(
            payload.get("image_checksum"),
            payload.get("color"),
        )
        if identity[0]:
            by_identity[identity].append(point)
    return dict(by_identity)


def finalize_catalog_payload(payload: dict, images: list[dict], ai_ready: bool) -> dict:
    result = copy.deepcopy(payload)
    result["images"] = images

    detail = dict(result.get("detail") or {})
    detail["images"] = copy.deepcopy(images)
    result["detail"] = detail

    summary = dict(result.get("summary") or {})
    summary["image_count"] = len(images)
    summary["local_image_count"] = sum(
        1 for image in images if clean(image.get("local_path"))
    )
    summary["embedding_count"] = sum(
        1 for image in images if bool(image.get("embedded"))
    )
    summary["ai_ready"] = bool(ai_ready)
    result["summary"] = summary

    return result


class SyncExecutor:
    def __init__(self, settings, *, apply: bool):
        self.settings = settings
        self.apply = apply
        self.qdrant = ProductQdrantStore(settings)
        self.image_store = None
        self.clip = None
        self.vector_cache: dict[str, list[float]] = {}

    def close(self):
        if self.image_store is not None:
            self.image_store.close()
        self.qdrant.close()

    def _get_image_store(self):
        if self.image_store is None:
            self.image_store = ProductImageStore()
        return self.image_store

    def _get_clip(self):
        if self.clip is None:
            self.clip = OpenClipImageEmbedder()
            logger.info(
                "OpenCLIP loaded model=%s pretrained=%s device=%s",
                self.clip.MODEL_NAME,
                self.clip.PRETRAINED_NAME,
                self.clip.device,
            )
        return self.clip

    def _bytes_for_image(self, image: dict, product_code: str):
        store = self._get_image_store()

        existing = store.existing_bytes(clean(image.get("local_path")))
        if existing is not None:
            import hashlib
            checksum = hashlib.sha256(existing).hexdigest()
            return existing, checksum, clean(image.get("local_path")), clean(image.get("mime_type"))

        downloaded = store.download(
            clean(image.get("source_url")),
            product_code,
        )
        return (
            downloaded.data,
            downloaded.checksum,
            downloaded.relative_path,
            downloaded.mime_type,
        )

    def _ensure_vector(
        self,
        image: dict,
        product_code: str,
        product_type: str,
        current_index: dict,
    ) -> tuple[dict, int | None, bool]:
        """Return finalized image, kept/created point id, whether a vector was created."""
        final = dict(image)
        checksum = clean(final.get("checksum"))
        color = clean(final.get("color"))

        # If catalog already knows checksum and Qdrant has exactly that image/color,
        # no download or CLIP work is needed.
        if checksum:
            matches = current_index.get(image_identity(checksum, color)) or []
            if matches:
                keep = matches[0]
                point_id = keep["point_id"]
                final["embedded"] = True

                if self.apply:
                    self.qdrant.set_image_payload(
                        point_id,
                        standard_image_payload(
                            point_id,
                            product_code,
                            product_type,
                            color,
                            checksum,
                        ),
                    )

                return final, point_id, False

        data, actual_checksum, relative_path, mime_type = self._bytes_for_image(
            final,
            product_code,
        )
        final["checksum"] = actual_checksum
        final["local_path"] = relative_path
        final["mime_type"] = mime_type or final.get("mime_type") or "image/jpeg"

        identity = image_identity(actual_checksum, color)
        matches = current_index.get(identity) or []
        if matches:
            keep = matches[0]
            point_id = keep["point_id"]
            final["embedded"] = True

            if self.apply:
                self.qdrant.set_image_payload(
                    point_id,
                    standard_image_payload(
                        point_id,
                        product_code,
                        product_type,
                        color,
                        actual_checksum,
                    ),
                )

            return final, point_id, False

        vector = self.vector_cache.get(actual_checksum)
        if vector is None:
            vector = self._get_clip().embed_bytes(data)
            self.vector_cache[actual_checksum] = vector

        point_id = self.qdrant.allocate_image_point_id(
            product_code,
            actual_checksum,
            color,
        )

        if self.apply:
            self.qdrant.upsert_image(
                point_id,
                vector,
                standard_image_payload(
                    point_id,
                    product_code,
                    product_type,
                    color,
                    actual_checksum,
                ),
            )

        final["embedded"] = True
        return final, point_id, True

    def preview_active_product(
        self,
        code: str,
        group,
        existing_row: dict | None,
        current_points: list[dict],
    ) -> dict:
        old_payload = (existing_row or {}).get("payload")
        payload, diff = build_catalog_payload(group, old_payload)

        managed_points = [
            point for point in current_points
            if is_shopify_managed_image(point)
        ]
        protected_points = [
            point for point in current_points
            if not is_shopify_managed_image(point)
        ]

        current_index = current_image_index(managed_points)
        known_target = [
            image_identity(image.get("checksum"), image.get("color"))
            for image in payload.get("images") or []
            if clean(image.get("checksum"))
        ]
        unresolved = sum(
            1
            for image in payload.get("images") or []
            if not clean(image.get("checksum"))
        )

        current_identities = set(current_index)
        known_target_set = set(known_target)

        missing_known = sorted(
            known_target_set - current_identities
        )

        exact_reconcile = unresolved == 0
        stale = []
        duplicate = []

        if exact_reconcile:
            for identity, points in current_index.items():
                if identity not in known_target_set:
                    stale.extend(point["point_id"] for point in points)
                elif len(points) > 1:
                    duplicate.extend(
                        point["point_id"] for point in points[1:]
                    )

        return {
            "product_code": code,
            "catalog_action": "CREATE" if existing_row is None else "UPDATE",
            "source_products": group.source_product_count,
            "target_images": len(payload.get("images") or []),
            "current_image_points": len(current_points),
            "shopify_managed_image_points": len(managed_points),
            "protected_image_points": len(protected_points),
            "protected_image_point_ids": [
                point["point_id"] for point in protected_points
            ],
            "known_target_images": len(known_target),
            "unresolved_target_images": unresolved,
            "known_target_missing_vectors": len(missing_known),
            "exact_reconcile_available": exact_reconcile,
            "stale_image_points": stale,
            "duplicate_image_points": duplicate,
            "colors": (payload.get("public_info") or {}).get("colors") or [],
            "phase2b_added_urls": diff.get("added_count", 0),
            "phase2b_removed_urls": diff.get("removed_count", 0),
        }

    def apply_active_product(
        self,
        code: str,
        group,
        existing_row: dict | None,
        current_points: list[dict],
    ) -> dict:
        old_payload = (existing_row or {}).get("payload")
        payload, _ = build_catalog_payload(group, old_payload)

        point_id = (
            existing_row["point_id"]
            if existing_row is not None
            else catalog_point_id(code)
        )

        # Stage catalog first as not AI-ready. If anything fails later,
        # retrieval does not use a half-synced active product.
        staged = copy.deepcopy(payload)
        staged_summary = dict(staged.get("summary") or {})
        staged_summary["ai_ready"] = False
        staged["summary"] = staged_summary

        self.qdrant.upsert_catalog(point_id, staged)

        managed_points = [
            point for point in current_points
            if is_shopify_managed_image(point)
        ]
        protected_points = [
            point for point in current_points
            if not is_shopify_managed_image(point)
        ]

        current_index = current_image_index(managed_points)
        kept_point_ids = set()
        final_images = []
        embedded_new = 0

        for image in payload.get("images") or []:
            final_image, kept_id, was_created = self._ensure_vector(
                image,
                code,
                clean(payload.get("product_type")),
                current_index,
            )
            final_images.append(final_image)
            if kept_id is not None:
                kept_point_ids.add(kept_id)
            if was_created:
                embedded_new += 1

        # Delete stale or duplicate vectors only AFTER every target vector exists.
        stale_ids = [
            point["point_id"]
            for point in managed_points
            if point["point_id"] not in kept_point_ids
        ]
        self.qdrant.delete_image_points(stale_ids)

        ai_ready = bool(final_images) and all(
            bool(image.get("embedded"))
            for image in final_images
        )
        final_payload = finalize_catalog_payload(
            payload,
            final_images,
            ai_ready,
        )
        self.qdrant.upsert_catalog(point_id, final_payload)

        return {
            "product_code": code,
            "status": "completed",
            "catalog_point_id": point_id,
            "images": len(final_images),
            "new_embeddings": embedded_new,
            "removed_image_points": len(stale_ids),
            "protected_image_points": len(protected_points),
            "ai_ready": ai_ready,
        }

    def apply_inactive_product(
        self,
        code: str,
        existing_row: dict,
        current_points: list[dict],
    ) -> dict:
        payload = inactive_payload(existing_row["payload"])

        images = []
        for image in payload.get("images") or []:
            item = dict(image)
            item["embedded"] = False
            images.append(item)

        payload = finalize_catalog_payload(payload, images, False)
        payload["status"] = "INACTIVE"
        payload["summary"]["status"] = "INACTIVE"
        payload["public_info"]["status"] = "INACTIVE"
        payload["detail"]["product"]["status"] = "INACTIVE"

        # Catalog becomes non-retrievable before Shopify-managed image vectors
        # are removed. External/recognition vectors are owned by another client
        # and must never be deleted by the Shopify synchronizer.
        self.qdrant.upsert_catalog(existing_row["point_id"], payload)

        managed_points = [
            point for point in current_points
            if is_shopify_managed_image(point)
        ]
        protected_points = [
            point for point in current_points
            if not is_shopify_managed_image(point)
        ]
        managed_ids = [point["point_id"] for point in managed_points]
        self.qdrant.delete_image_points(managed_ids)

        return {
            "product_code": code,
            "status": "inactivated",
            "removed_image_points": len(managed_ids),
            "protected_image_points": len(protected_points),
            "ai_ready": False,
        }


def parse_args():
    parser = argparse.ArgumentParser(
        description="Phase 2C staged Product RAG sync writer"
    )
    parser.add_argument(
        "--mode",
        choices=["existing", "all_active"],
        default="all_active",
    )
    parser.add_argument(
        "--product-code",
        default=None,
        help="Chỉ kiểm tra/ghi một product_code, ví dụ JC53.",
    )
    parser.add_argument(
        "--max-products",
        type=int,
        default=None,
        help="Giới hạn số product_code để chạy thử.",
    )
    parser.add_argument(
        "--apply",
        action="store_true",
        help="Cho phép ghi Qdrant. Còn cần PRODUCT_SYNC_WRITE_ENABLED=true.",
    )
    return parser.parse_args()


def main():
    args = parse_args()
    settings = Settings.load()
    configure_logging(settings.log_level)

    if args.apply and not write_enabled():
        raise SystemExit(
            "TỪ CHỐI GHI: hãy set PRODUCT_SYNC_WRITE_ENABLED=true "
            "rồi chạy lại với --apply."
        )

    executor = SyncExecutor(settings, apply=args.apply)

    try:
        config = executor.qdrant.validate()
        logger.info(
            "Qdrant validated catalog=%s(size=1) images=%s(size=512)",
            config["catalog_points"],
            config["image_points"],
        )

        existing = executor.qdrant.all_catalog()
        all_image_points = executor.qdrant.all_images_by_code()

        shopify_rows, groups, skipped = load_groups()

        active_codes = set(groups)
        existing_codes = set(existing)

        if args.mode == "existing":
            active_target = sorted(active_codes & existing_codes)
            inactive_target = sorted(existing_codes - active_codes)
        else:
            active_target = sorted(active_codes)
            inactive_target = sorted(existing_codes - active_codes)

        if args.product_code:
            requested = args.product_code.strip()
            active_target = [requested] if requested in active_codes else []
            inactive_target = (
                [requested]
                if requested in existing_codes and requested not in active_codes
                else []
            )

            if not active_target and not inactive_target:
                raise SystemExit(
                    f"Không tìm thấy product_code={requested} trong Shopify ACTIVE "
                    "hoặc Qdrant hiện tại."
                )

        combined = [
            ("active", code) for code in active_target
        ] + [
            ("inactive", code) for code in inactive_target
        ]

        if args.max_products is not None:
            combined = combined[: max(0, args.max_products)]

        report_rows = []
        failed = []

        print()
        print("=" * 78)
        print("PRODUCT SYNC - PHASE 2C STAGED WRITER")
        print("=" * 78)
        print(f"MODE                         : {args.mode}")
        print(f"WRITE ENABLED                : {str(args.apply).upper()}")
        print(f"Shopify ACTIVE records       : {len(shopify_rows)}")
        print(f"Grouped product_code         : {len(groups)}")
        print(f"Skipped without product_code : {len(skipped)}")
        print(f"Qdrant catalog points        : {config['catalog_points']}")
        print(f"Qdrant image points          : {config['image_points']}")
        print(f"Selected active codes        : {sum(1 for t, _ in combined if t == 'active')}")
        print(f"Selected inactive codes      : {sum(1 for t, _ in combined if t == 'inactive')}")
        print("-" * 78)

        for kind, code in combined:
            try:
                current_points = all_image_points.get(code) or []

                if kind == "active":
                    if args.apply:
                        result = executor.apply_active_product(
                            code,
                            groups[code],
                            existing.get(code),
                            current_points,
                        )
                    else:
                        result = executor.preview_active_product(
                            code,
                            groups[code],
                            existing.get(code),
                            current_points,
                        )
                else:
                    if args.apply:
                        result = executor.apply_inactive_product(
                            code,
                            existing[code],
                            current_points,
                        )
                    else:
                        result = {
                            "product_code": code,
                            "catalog_action": "INACTIVATE",
                            "current_image_points": len(current_points),
                            "ai_ready_after": False,
                        }

                report_rows.append(result)

                if args.product_code or args.max_products:
                    print(json.dumps(result, ensure_ascii=False, indent=2))

            except Exception as exc:
                failed.append(
                    {
                        "product_code": code,
                        "error_type": type(exc).__name__,
                        "error": str(exc),
                    }
                )
                logger.exception(
                    "Product sync failed product_code=%s error_type=%s",
                    code,
                    type(exc).__name__,
                )

        timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        output_dir = ROOT / "tmp" / "product_sync_execute" / timestamp
        output_dir.mkdir(parents=True, exist_ok=True)

        report = {
            "generated_at": datetime.now().isoformat(timespec="seconds"),
            "write_enabled": args.apply,
            "mode": args.mode,
            "product_code": args.product_code,
            "results": report_rows,
            "failed": failed,
        }
        (output_dir / "phase2c_report.json").write_text(
            json.dumps(report, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )

        if not args.apply:
            exact = [
                row
                for row in report_rows
                if row.get("exact_reconcile_available")
            ]
            known_stale = sum(
                len(row.get("stale_image_points") or [])
                + len(row.get("duplicate_image_points") or [])
                for row in exact
            )
            unresolved = sum(
                int(row.get("unresolved_target_images") or 0)
                for row in report_rows
            )

            print(f"Known stale/duplicate vectors : {known_stale}")
            print(f"Images needing checksum later : {unresolved}")
        else:
            completed = sum(
                1 for row in report_rows
                if row.get("status") in {"completed", "inactivated"}
            )
            print(f"Completed product codes        : {completed}")
            print(f"Failed product codes           : {len(failed)}")

        print(f"Report: {output_dir}")
        print("=" * 78)

        if not args.apply:
            print("DRY-RUN: KHÔNG ghi Qdrant, KHÔNG download/embed ảnh mới.")
        else:
            print("APPLY: Qdrant đã được thay đổi cho các product hoàn tất.")

        print()

        if failed:
            raise SystemExit(2)

    finally:
        executor.close()


if __name__ == "__main__":
    main()
