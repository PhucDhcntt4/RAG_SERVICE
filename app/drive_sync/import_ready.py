from __future__ import annotations

import argparse
import json

from app.config import Settings
from app.document_asset_repository import DocumentAssetRepository
from app.document_asset_storage import DocumentAssetStore
from app.drive_sync.google_client import GoogleDriveClient
from app.drive_sync.import_service import DriveImportService
from app.drive_sync.repository import GoogleDriveSyncRepository
from app.embeddings import GeminiEmbedder
from app.qdrant_repository import QdrantRepository
from app.service import KnowledgeService
from app.taxonomy_repository import TaxonomyRepository


def main():
    parser = argparse.ArgumentParser(
        description="Import các tài liệu Google Drive đã sẵn sàng vào RAG"
    )
    parser.add_argument(
        "--folder-id",
        required=True,
        help="Google Drive folder ID của phòng ban đã đăng ký",
    )
    args = parser.parse_args()

    settings = Settings.load()
    drive_repository = GoogleDriveSyncRepository(settings)
    drive_repository.ready()
    source = drive_repository.get_source_by_folder_id(args.folder_id)
    if source is None:
        raise SystemExit("Folder phòng ban chưa được đăng ký")

    taxonomy = TaxonomyRepository(settings)
    taxonomy.ready()
    asset_repository = DocumentAssetRepository(settings)
    asset_repository.initialize()
    embedder = GeminiEmbedder(settings)
    qdrant = QdrantRepository(settings)
    try:
        knowledge = KnowledgeService(
            settings,
            qdrant,
            embedder,
            asset_repository=asset_repository,
            asset_storage=DocumentAssetStore(),
        )
        service = DriveImportService(
            drive_client=GoogleDriveClient(settings),
            drive_repository=drive_repository,
            taxonomy_repository=taxonomy,
            knowledge_service=knowledge,
            max_file_bytes=settings.rag_max_upload_bytes,
        )
        result = service.import_source(source["id"])
    finally:
        embedder.close()
        qdrant.close()

    print("Import Google Drive vào RAG hoàn tất.")
    print(json.dumps(result, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
