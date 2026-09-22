from __future__ import annotations

import json
import logging
import os
import time
from datetime import datetime
from uuid import uuid4

from dotenv import dotenv_values

from app.config import ROOT, Settings
from app.product_sync.execute_sync import SyncExecutor, load_groups, write_enabled
from app.product_sync.repository import ProductSyncRepository
from app.product_sync.inventory_repository import InventorySyncRepository
from app.product_sync.inventory_sync import InventorySyncExecutor, write_report as write_inventory_report

logger = logging.getLogger("rag_service.product_sync.worker")


def configure_logging(level):
    logging.basicConfig(
        level=level,
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
    )
    for name in (
        "httpx",
        "httpcore",
        "PIL",
        "PIL.TiffImagePlugin",
        "huggingface_hub",
    ):
        logging.getLogger(name).setLevel(logging.WARNING)


def load_poll_seconds() -> int:
    values = {**dotenv_values(ROOT / ".env"), **os.environ}
    raw = str(values.get("PRODUCT_SYNC_POLL_SECONDS") or "5").strip()
    try:
        value = int(raw)
    except ValueError:
        value = 5
    return min(max(value, 2), 60)


def write_job_report(job_id: int, payload: dict):
    output_dir = ROOT / "tmp" / "product_sync_jobs"
    output_dir.mkdir(parents=True, exist_ok=True)
    path = output_dir / f"job_{job_id}_phase2d.json"
    path.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2, default=str),
        encoding="utf-8",
    )
    return path


def run_job(job: dict, settings, jobs: ProductSyncRepository, instance_id: str):
    job_id = int(job["id"])
    apply = not bool(job["dry_run"])

    if apply and not write_enabled():
        jobs.fail(
            job_id,
            "PRODUCT_SYNC_WRITE_ENABLED=false; worker từ chối ghi Qdrant.",
        )
        return

    executor = SyncExecutor(settings, apply=apply)
    failures: list[dict] = []

    stats = {
        "total_products": 0,
        "selected_products": 0,
        "processed_products": 0,
        "skipped_products": 0,
        "failed_products": 0,
        "created_products": 0,
        "updated_products": 0,
        "inactivated_products": 0,
        "new_embeddings": 0,
        "removed_image_points": 0,
    }

    try:
        config = executor.qdrant.validate()
        existing = executor.qdrant.all_catalog()
        all_image_points = executor.qdrant.all_images_by_code()
        shopify_rows, groups, skipped_without_code = load_groups()

        active_codes = set(groups)
        existing_codes = set(existing)

        if job["mode"] == "existing":
            active_target = sorted(active_codes & existing_codes)
            inactive_target = sorted(existing_codes - active_codes)
            skipped_groups = len(active_codes - existing_codes)
        else:
            active_target = sorted(active_codes)
            inactive_target = sorted(existing_codes - active_codes)
            skipped_groups = 0

        combined = [
            ("active", code) for code in active_target
        ] + [
            ("inactive", code) for code in inactive_target
        ]

        stats.update(
            total_products=len(groups),
            selected_products=len(combined),
            skipped_products=skipped_groups + len(skipped_without_code),
        )

        jobs.update_progress(
            job_id,
            **stats,
            message=(
                f"Đã tải Shopify: records={len(shopify_rows)}, groups={len(groups)}. "
                f"Chuẩn bị xử lý {len(combined)} product_code."
            ),
        )

        logger.info(
            "Sync job prepared job_id=%s mode=%s apply=%s active=%s inactive=%s "
            "shopify_records=%s groups=%s",
            job_id,
            job["mode"],
            apply,
            len(active_target),
            len(inactive_target),
            len(shopify_rows),
            len(groups),
        )

        for index, (kind, code) in enumerate(combined, start=1):
            jobs.heartbeat(instance_id, job_id)
            current_points = all_image_points.get(code) or []

            try:
                if kind == "active":
                    existed = code in existing

                    if apply:
                        result = executor.apply_active_product(
                            code,
                            groups[code],
                            existing.get(code),
                            current_points,
                        )
                        if existed:
                            stats["updated_products"] += 1
                        else:
                            stats["created_products"] += 1
                        stats["new_embeddings"] += int(
                            result.get("new_embeddings") or 0
                        )
                        stats["removed_image_points"] += int(
                            result.get("removed_image_points") or 0
                        )
                    else:
                        result = executor.preview_active_product(
                            code,
                            groups[code],
                            existing.get(code),
                            current_points,
                        )
                        if existed:
                            stats["updated_products"] += 1
                        else:
                            stats["created_products"] += 1
                else:
                    if apply:

                        old_payload = existing[code].get("payload") or {}
                        if (
                            str(old_payload.get("status") or "").upper() == "INACTIVE"
                            and not current_points
                        ):
                            result = {
                                "product_code": code,
                                "status": "already_inactive",
                                "removed_image_points": 0,
                                "ai_ready": False,
                            }
                        else:
                            result = executor.apply_inactive_product(
                                code,
                                existing[code],
                                current_points,
                            )
                            stats["removed_image_points"] += int(
                                result.get("removed_image_points") or 0
                            )
                    else:
                        result = {
                            "product_code": code,
                            "catalog_action": "INACTIVATE",
                            "current_image_points": len(current_points),
                            "ai_ready_after": False,
                        }

                    stats["inactivated_products"] += 1

                logger.info(
                    "Sync product completed job_id=%s index=%s/%s code=%s kind=%s result=%s",
                    job_id,
                    index,
                    len(combined),
                    code,
                    kind,
                    result.get("status") or result.get("catalog_action"),
                )

            except Exception as exc:
                stats["failed_products"] += 1
                failures.append(
                    {
                        "product_code": code,
                        "kind": kind,
                        "error_type": type(exc).__name__,
                        "error": str(exc)[:2000],
                    }
                )
                logger.exception(
                    "Sync product failed job_id=%s code=%s error_type=%s",
                    job_id,
                    code,
                    type(exc).__name__,
                )

            finally:
                stats["processed_products"] += 1
                jobs.update_progress(
                    job_id,
                    **stats,
                    message=(
                        f"{stats['processed_products']}/{stats['selected_products']} · "
                        f"mã gần nhất: {code} · lỗi: {stats['failed_products']}"
                    ),
                )

        timestamp = datetime.now().isoformat(timespec="seconds")
        report_path = write_job_report(
            job_id,
            {
                "job_id": job_id,
                "generated_at": timestamp,
                "mode": job["mode"],
                "dry_run": bool(job["dry_run"]),
                "qdrant_before": config,
                "shopify_active_records": len(shopify_rows),
                "grouped_product_codes": len(groups),
                "skipped_without_code": len(skipped_without_code),
                "stats": stats,
                "failures": failures,
            },
        )

        message = (
            f"Hoàn tất. processed={stats['processed_products']}; "
            f"created={stats['created_products']}; updated={stats['updated_products']}; "
            f"inactivated={stats['inactivated_products']}; failed={stats['failed_products']}; "
            f"new_embeddings={stats['new_embeddings']}; "
            f"removed_image_points={stats['removed_image_points']}; report={report_path}"
        )

        jobs.complete(job_id, **stats, message=message)

        logger.info(
            "Sync job completed job_id=%s processed=%s created=%s updated=%s "
            "inactivated=%s failed=%s embeddings=%s removed=%s",
            job_id,
            stats["processed_products"],
            stats["created_products"],
            stats["updated_products"],
            stats["inactivated_products"],
            stats["failed_products"],
            stats["new_embeddings"],
            stats["removed_image_points"],
        )

    finally:
        executor.close()


def run_inventory_job(
    settings,
    inventory: InventorySyncRepository,
    jobs: ProductSyncRepository,
    instance_id: str,
    trigger_type: str,
):
    if not write_enabled():
        return

    logger.info("Inventory sync started trigger=%s", trigger_type)
    executor = InventorySyncExecutor(settings, apply=True)
    try:
        stats = executor.run(progress=lambda: jobs.heartbeat(instance_id, None))
        report_path = write_inventory_report(stats, apply=True)
        inventory.complete(stats)
        logger.info(
            "Inventory sync completed trigger=%s checked_products=%s updated_products=%s "
            "checked_variants=%s changed_variants=%s missing_products=%s missing_variants=%s report=%s",
            trigger_type,
            stats["checked_products"],
            stats["updated_products"],
            stats["checked_variants"],
            stats["changed_variants"],
            stats["missing_products"],
            stats["missing_variants"],
            report_path,
        )
    except Exception as exc:
        inventory.fail(f"{type(exc).__name__}: {str(exc)[:1500]}")
        logger.exception("Inventory sync failed error_type=%s", type(exc).__name__)
    finally:
        executor.close()
        jobs.heartbeat(instance_id, None)


def main():
    settings = Settings.load()
    configure_logging(settings.log_level)

    jobs = ProductSyncRepository(settings)
    jobs.initialize()

    inventory = InventorySyncRepository(settings)
    inventory.initialize()
    inventory.recover_interrupted()

    lock_connection = jobs.acquire_worker_lock()
    if lock_connection is None:
        logger.error(
            "Product Sync Worker không khởi động: đã có worker khác giữ singleton lock."
        )
        raise SystemExit(2)

    instance_id = uuid4().hex
    poll_seconds = load_poll_seconds()

    recovered = jobs.recover_interrupted_jobs()
    jobs.heartbeat(instance_id, None)

    logger.info(
        "Product Sync Worker started instance=%s poll_seconds=%s write_enabled=%s recovered_jobs=%s",
        instance_id,
        poll_seconds,
        write_enabled(),
        recovered,
    )

    try:
        while True:
            try:
                jobs.heartbeat(instance_id, None)

                if write_enabled():
                    jobs.maybe_enqueue_scheduled(dry_run=False)

                job = jobs.claim_next_job()
                if job is None:
                    if write_enabled():
                        inventory_claim = inventory.claim_due()
                        if inventory_claim is not None:
                            run_inventory_job(
                                settings,
                                inventory,
                                jobs,
                                instance_id,
                                inventory_claim["trigger_type"],
                            )
                            continue
                    time.sleep(poll_seconds)
                    continue

                jobs.heartbeat(instance_id, int(job["id"]))

                logger.info(
                    "Product sync job claimed job_id=%s trigger=%s mode=%s dry_run=%s",
                    job["id"],
                    job["trigger_type"],
                    job["mode"],
                    job["dry_run"],
                )

                try:
                    run_job(job, settings, jobs, instance_id)
                except Exception as exc:
                    logger.exception(
                        "Product sync job failed job_id=%s error_type=%s",
                        job["id"],
                        type(exc).__name__,
                    )
                    jobs.fail(
                        int(job["id"]),
                        f"{type(exc).__name__}: {str(exc)[:1500]}",
                    )
                finally:
                    jobs.heartbeat(instance_id, None)

            except KeyboardInterrupt:
                logger.info("Product Sync Worker stopped")
                return
            except Exception as exc:
                logger.exception(
                    "Product Sync Worker loop error error_type=%s",
                    type(exc).__name__,
                )
                time.sleep(poll_seconds)
    finally:
        jobs.release_worker_lock(lock_connection)


if __name__ == "__main__":
    main()
