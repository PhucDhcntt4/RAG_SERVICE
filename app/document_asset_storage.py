"""Private storage for images extracted from uploaded PDF documents."""
import logging
import re
from pathlib import Path

from app.config import ROOT
from app.storage import StorageError


logger = logging.getLogger("rag_service")
ASSET_NAME = re.compile(r"^p\d{4}-\d{3}-[0-9a-f]{12}\.png$")


class DocumentAssetStore:
    def __init__(self, root=None):
        self.root = Path(root or ROOT / "storage" / "document_assets").resolve()

    def _directory(self, document_id: int) -> Path:
        value = int(document_id)
        if value < 0:
            raise StorageError("Document ID khong hop le")
        path = (self.root / str(value)).resolve()
        if path.parent != self.root:
            raise StorageError("Duong dan asset khong hop le")
        return path

    def save(self, document_id, assets):
        directory = self._directory(document_id)
        directory.mkdir(parents=True, exist_ok=True)
        stored = []
        try:
            for index, asset in enumerate(assets, 1):
                name = (
                    f"p{asset.page_number:04d}-{index:03d}-"
                    f"{asset.checksum[:12].lower()}.png"
                )
                path = directory / name
                # Deterministic names make retries idempotent.
                if not path.exists() or path.stat().st_size != len(asset.content):
                    temporary = directory / (name + ".tmp")
                    temporary.write_bytes(asset.content)
                    temporary.replace(path)
                stored.append({
                    "asset_type": asset.asset_type,
                    "page_number": asset.page_number,
                    "bbox": list(asset.bbox),
                    "width": asset.width,
                    "height": asset.height,
                    "media_type": asset.media_type,
                    "storage_name": name,
                    "checksum": asset.checksum,
                    "section_path": list(asset.section_path),
                })
        except OSError as exc:
            raise StorageError("Khong luu duoc anh trich xuat tu PDF") from exc
        return stored

    def read(self, document_id, storage_name):
        if not ASSET_NAME.fullmatch(storage_name or ""):
            raise StorageError("Ten asset khong hop le")
        try:
            return (self._directory(document_id) / storage_name).read_bytes()
        except FileNotFoundError:
            raise
        except OSError as exc:
            raise StorageError("Khong doc duoc anh tai lieu") from exc

    def prune(self, document_id, keep_names):
        directory = self._directory(document_id)
        if not directory.exists():
            return
        keep = set(keep_names)
        try:
            for path in directory.iterdir():
                if path.is_file() and path.name not in keep:
                    path.unlink(missing_ok=True)
            if not any(directory.iterdir()):
                directory.rmdir()
        except OSError:
            logger.warning("RAG document asset cleanup failed document_id=%s", document_id)

    def remove(self, document_id):
        directory = self._directory(document_id)
        if not directory.exists():
            return True
        try:
            for path in directory.iterdir():
                if path.is_file():
                    path.unlink(missing_ok=True)
            directory.rmdir()
            return True
        except OSError:
            logger.warning("RAG document asset removal failed document_id=%s", document_id)
            return False
