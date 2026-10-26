from __future__ import annotations

import argparse
import json

import psycopg

from app.config import Settings
from app.document_asset_repository import DocumentAssetRepository
from app.document_asset_storage import DocumentAssetStore
from app.drive_sync.discovery_service import DriveDiscoveryService
from app.drive_sync.google_client import GoogleDriveClient, GoogleDriveError
from app.drive_sync.import_service import DriveImportService
from app.drive_sync.move_service import DriveMoveService
from app.drive_sync.pipeline_service import (
    DrivePipelineError,
    DriveSyncPipeline,
)
from app.drive_sync.repository import GoogleDriveSyncRepository
from app.drive_sync.version_service import DriveVersionService
from app.embeddings import GeminiEmbedder
from app.qdrant_repository import QdrantRepository
from app.service import KnowledgeService
from app.taxonomy_repository import TaxonomyRepository


def main():
    parser = argparse.ArgumentParser(
        description="Chạy toàn bộ quy trình đồng bộ Google Drive một lần"
    )
    parser.add_argument(
        "--folder-id",
        required=True,
        help="Google Drive folder ID của phòng ban",
    )
    args = parser.parse_args()

    embedder = None
    qdrant = None

    try:
        settings = Settings.load()

        drive_repository = GoogleDriveSyncRepository(settings)
        drive_repository.ready()

        taxonomy = TaxonomyRepository(settings)
        taxonomy.ready()

        asset_repository = DocumentAssetRepository(settings)
        asset_repository.initialize()

        drive_client = GoogleDriveClient(settings)

        embedder = GeminiEmbedder(settings)
        qdrant = QdrantRepository(settings)

        knowledge = KnowledgeService(
            settings,
            qdrant,
            embedder,
            asset_repository=asset_repository,
            asset_storage=DocumentAssetStore(),
        )

        discovery_service = DriveDiscoveryService(
            drive_client=drive_client,
            drive_repository=drive_repository,
            taxonomy_repository=taxonomy,
        )

        version_service = DriveVersionService(
            drive_client=drive_client,
            drive_repository=drive_repository,
            max_file_bytes=settings.rag_max_upload_bytes,
        )

        import_service = DriveImportService(
            drive_client=drive_client,
            drive_repository=drive_repository,
            taxonomy_repository=taxonomy,
            knowledge_service=knowledge,
            max_file_bytes=settings.rag_max_upload_bytes,
        )

        move_service = DriveMoveService(
            drive_client=drive_client,
            drive_repository=drive_repository,
        )

        pipeline = DriveSyncPipeline(
            drive_repository=drive_repository,
            discovery_service=discovery_service,
            version_service=version_service,
            import_service=import_service,
            move_service=move_service,
        )

        result = pipeline.run(args.folder_id)

    except (
        DrivePipelineError,
        GoogleDriveError,
        psycopg.Error,
        RuntimeError,
    ) as exc:
        raise SystemExit(
            f"Đồng bộ Google Drive thất bại: {exc}"
        ) from None
    finally:
        if embedder is not None:
            embedder.close()

        if qdrant is not None:
            qdrant.close()

    print("Đồng bộ Google Drive hoàn tất.")
    print(
        json.dumps(
            result,
            ensure_ascii=False,
            indent=2,
            default=str,
        )
    )

    if result["status"] != "completed":
        raise SystemExit(1)


if __name__ == "__main__":
    main()