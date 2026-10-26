from __future__ import annotations

import logging
import time
from uuid import uuid4

from app.config import Settings
from app.document_asset_repository import DocumentAssetRepository
from app.document_asset_storage import DocumentAssetStore
from app.drive_sync.discovery_service import DriveDiscoveryService
from app.drive_sync.google_client import GoogleDriveClient
from app.drive_sync.import_service import DriveImportService
from app.drive_sync.move_service import DriveMoveService
from app.drive_sync.pipeline_service import DriveSyncPipeline
from app.drive_sync.repository import GoogleDriveSyncRepository
from app.drive_sync.source_service import DriveSourceService
from app.drive_sync.version_service import DriveVersionService
from app.embeddings import GeminiEmbedder
from app.qdrant_repository import QdrantRepository
from app.search_index_state_repository import SearchIndexStateRepository
from app.service import KnowledgeService
from app.taxonomy_repository import TaxonomyRepository


logger = logging.getLogger("rag_service.drive_sync.worker")


STAGE_LABELS = {
    "scan": "scan_new",
    "version_check": "check_version",
    "import": "import_qdrant",
    "move": "move_done_error",
    "finalize": "finalize",
}


def configure_logging(level: str):
    logging.basicConfig(
        level=level,
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
    )
    logger.setLevel(level)

    # HTTP-level logs are extremely verbose and can expose internal URLs.
    # Keep business logs readable even when application LOG_LEVEL=DEBUG.
    for name in (
        "googleapiclient",
        "googleapiclient.discovery",
        "google_auth_httplib2",
        "google.auth",
        "httplib2",
        "httpx",
        "httpcore",
    ):
        logging.getLogger(name).setLevel(logging.WARNING)


def build_pipeline(settings, repository):
    taxonomy = TaxonomyRepository(settings)
    taxonomy.ready()

    assets = DocumentAssetRepository(settings)
    assets.initialize()
    SearchIndexStateRepository(settings).initialize()

    drive = GoogleDriveClient(settings)

    source_service = DriveSourceService(
        drive_client=drive,
        drive_repository=repository,
        taxonomy_repository=taxonomy,
    )
    registered_sources = []
    folder_ids = settings.google_drive_department_folders
    for index, folder_id in enumerate(folder_ids, start=1):
        logger.info(
            "Drive source registration started index=%s/%s",
            index,
            len(folder_ids),
        )
        registration = source_service.register_department(folder_id)
        registered = {
            "source_id": registration["source"]["id"],
            "department_code": registration["source"]["department_code"],
            "folder_id": registration["source"]["folder_id"],
        }
        registered_sources.append(registered)
        logger.info(
            "Drive source registered index=%s/%s source_id=%s department=%s",
            index,
            len(folder_ids),
            registered["source_id"],
            registered["department_code"],
        )

    embedder = GeminiEmbedder(settings)
    qdrant = QdrantRepository(settings)
    knowledge = KnowledgeService(
        settings,
        qdrant,
        embedder,
        asset_repository=assets,
        asset_storage=DocumentAssetStore(),
    )

    pipeline = DriveSyncPipeline(
        drive_repository=repository,
        discovery_service=DriveDiscoveryService(
            drive_client=drive,
            drive_repository=repository,
            taxonomy_repository=taxonomy,
        ),
        version_service=DriveVersionService(
            drive_client=drive,
            drive_repository=repository,
            max_file_bytes=settings.rag_max_upload_bytes,
        ),
        import_service=DriveImportService(
            drive_client=drive,
            drive_repository=repository,
            taxonomy_repository=taxonomy,
            knowledge_service=knowledge,
            max_file_bytes=settings.rag_max_upload_bytes,
        ),
        move_service=DriveMoveService(
            drive_client=drive,
            drive_repository=repository,
        ),
    )
    return pipeline, embedder, qdrant, registered_sources


def run_job(job, repository, pipeline, instance_id: str):
    job_id = int(job["id"])
    source_id = int(job["source_id"])
    source = repository.get_source(source_id)
    if source is None:
        raise RuntimeError("Nguồn Google Drive của job không còn tồn tại")
    if not source["enabled"]:
        raise RuntimeError("Nguồn Google Drive đang bị tạm dừng")

    def progress(stage: str):
        logger.info(
            "Google Drive job stage job_id=%s source_id=%s department=%s stage=%s",
            job_id,
            source_id,
            source["department_code"],
            STAGE_LABELS.get(stage, stage),
        )
        row = repository.heartbeat_job(
            job_id,
            instance_id,
            message=f"stage={stage}",
        )
        repository.heartbeat_worker(instance_id, current_job_id=job_id)
        if row is None:
            raise RuntimeError("Job không còn thuộc Drive worker hiện tại")

    result = pipeline.run(
        source["folder_id"],
        job_id=job_id,
        progress=progress,
    )
    repository.finish_job(job_id, result)

    successful = result["status"] == "completed"
    repository.mark_source_scanned(
        source_id,
        successful=successful,
        error_message="" if successful else f"Pipeline partial: {result['failed']} lỗi",
    )
    return result


def log_result_issues(job, result):
    sections = (
        ("scan_new", result["scan"].get("items", [])),
        ("check_version", result["version_check"].get("items", [])),
        ("import_qdrant", result["import"].get("items", [])),
        ("move_done_error", result["move"].get("items", [])),
    )
    for stage, items in sections:
        for item in items:
            error_code = item.get("error_code") or item.get("error_type")
            if item.get("status") != "error" and not error_code:
                continue
            logger.warning(
                "Google Drive file issue job_id=%s source_id=%s stage=%s "
                "file=%s document_code=%s error_code=%s error=%s",
                job["id"],
                job["source_id"],
                stage,
                item.get("file_name") or "-",
                item.get("document_code") or "-",
                error_code or "UNKNOWN",
                item.get("error_message") or "-",
            )


def main():
    settings = Settings.load()
    configure_logging(settings.log_level)

    if not settings.google_drive_sync_enabled:
        raise SystemExit(
            "Google Drive Sync đang tắt. Hãy đặt GOOGLE_DRIVE_SYNC_ENABLED=true."
        )

    repository = GoogleDriveSyncRepository(settings)
    repository.initialize()
    repository.ready()

    lock_connection = repository.acquire_worker_lock()
    if lock_connection is None:
        logger.error("Drive worker không khởi động: worker khác đang chạy.")
        raise SystemExit(2)

    instance_id = uuid4().hex
    # Chu kỳ đồng bộ nằm trong PostgreSQL và tính theo phút. Vòng lặp worker
    # chỉ thức dậy để kiểm tra queue, tối đa mỗi 30 giây.
    poll_seconds = min(30, max(5, settings.google_drive_poll_seconds))
    embedder = None
    qdrant = None

    try:
        recovered = repository.recover_interrupted_jobs()
        pipeline, embedder, qdrant, registered_sources = build_pipeline(
            settings, repository
        )
        logger.info(
            "Google Drive Worker started instance=%s poll_seconds=%s "
            "registered_departments=%s recovered=%s",
            instance_id,
            poll_seconds,
            [
                f"{item['department_code']}#{item['source_id']}"
                for item in registered_sources
            ],
            recovered,
        )
        repository.heartbeat_worker(instance_id)

        while True:
            try:
                repository.heartbeat_worker(instance_id)
                queued = repository.enqueue_due_sources()
                if queued:
                    logger.info(
                        "Google Drive scheduled jobs enqueued ids=%s",
                        [row["id"] for row in queued],
                    )

                job = repository.claim_next_job(instance_id)
                if job is None:
                    time.sleep(poll_seconds)
                    continue

                logger.info(
                    "Google Drive job claimed job_id=%s source_id=%s trigger=%s attempt=%s",
                    job["id"],
                    job["source_id"],
                    job["trigger_type"],
                    job["attempt_count"],
                )
                repository.heartbeat_worker(
                    instance_id,
                    current_job_id=int(job["id"]),
                )
                try:
                    result = run_job(job, repository, pipeline, instance_id)
                    log_result_issues(job, result)
                    scan = result["scan"]
                    version = result["version_check"]
                    imported = result["import"]
                    moved = result["move"]
                    logger.info(
                        "Google Drive job finished job_id=%s source_id=%s "
                        "status=%s discovered=%s checked=%s ready=%s "
                        "imported=%s skipped=%s moved=%s failed=%s",
                        job["id"],
                        job["source_id"],
                        result["status"],
                        scan["discovered"],
                        version["checked"],
                        version["ready"],
                        imported["completed"],
                        version["skipped"],
                        moved["moved"],
                        result["failed"],
                    )
                except Exception as exc:
                    logger.exception(
                        "Google Drive job failed job_id=%s source_id=%s "
                        "error_type=%s error=%s",
                        job["id"],
                        job["source_id"],
                        type(exc).__name__,
                        str(exc),
                    )
                    repository.fail_job(
                        int(job["id"]),
                        f"{type(exc).__name__}: {str(exc)[:3500]}",
                    )
                    repository.mark_source_scanned(
                        int(job["source_id"]),
                        successful=False,
                        error_message=f"{type(exc).__name__}: {str(exc)[:3500]}",
                    )
                finally:
                    repository.heartbeat_worker(instance_id)
            except KeyboardInterrupt:
                logger.info("Google Drive Worker stopped")
                return
            except Exception as exc:
                logger.exception(
                    "Google Drive Worker loop error error_type=%s error=%s",
                    type(exc).__name__,
                    str(exc),
                )
                time.sleep(poll_seconds)
    finally:
        try:
            repository.stop_worker(instance_id)
        except Exception:
            logger.warning("Could not persist Drive worker stopped state")
        if embedder is not None:
            embedder.close()
        if qdrant is not None:
            qdrant.close()
        repository.release_worker_lock(lock_connection)


if __name__ == "__main__":
    main()
