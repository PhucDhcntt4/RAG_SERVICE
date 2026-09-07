"""Private local files with readable, sanitized names and exclusive creation."""
import logging
import os
import re
from dataclasses import dataclass
from pathlib import Path, PureWindowsPath

from app.config import ROOT

logger = logging.getLogger("rag_service")
MIME_TYPES = {".txt": "text/plain", ".md": "text/markdown", ".pdf": "application/pdf"}


class StorageError(RuntimeError):
    pass


@dataclass(frozen=True)
class StoredFile:
    key: str
    name: str
    mime_type: str
    size: int
    origin: str


class LocalFileStore:
    def __init__(self, root=None, legacy_root=None):
        self.root = Path(root or ROOT / "knowlegde").resolve()
        self.legacy_root = Path(legacy_root or (self.root if root else ROOT / "storage" / "documents")).resolve()

    def path(self, key):
        if not isinstance(key, str):
            raise StorageError("Mã file lưu trữ không hợp lệ")
        if re.fullmatch(r"[0-9a-f]{32}\.(txt|md|pdf)", key):
            # Existing DB records still reference files in storage/documents.
            root, name = self.legacy_root, key
        elif key.startswith("named/"):
            root, name = self.root, key.removeprefix("named/")
            if (not name or PureWindowsPath(name).name != name or name in {".", ".."}
                    or re.search(r'[\x00-\x1f<>:"/\\|?*]', name)
                    or name != name.strip(" .") or Path(name).suffix.lower() not in MIME_TYPES):
                raise StorageError("Tên file lưu trữ không hợp lệ")
        else:
            raise StorageError("Mã file lưu trữ không hợp lệ")
        path = (root / name).resolve()
        if path.parent != root:
            raise StorageError("Đường dẫn file lưu trữ không hợp lệ")
        return path

    def save(self, data, filename, *, origin="upload"):
        name = PureWindowsPath(filename).name
        name = re.sub(r'[\x00-\x1f<>:"/\\|?*]', "_", name).strip(" .")
        suffix = Path(name).suffix
        if suffix.lower() not in MIME_TYPES:
            raise StorageError("Chỉ lưu file TXT, MD hoặc PDF")
        stem = name[:-len(suffix)]
        if re.match(r"^(CON|PRN|AUX|NUL|COM[1-9]|LPT[1-9])(?:\.|$)", stem, re.I):
            stem = "_" + stem
        # Leave room for a collision suffix and Windows' component/path limits.
        stem = stem.encode("utf-16-le")[:320].decode("utf-16-le", errors="ignore")
        name = stem + suffix
        path = None
        created = False
        try:
            self.root.mkdir(parents=True, exist_ok=True)
            for number in range(1, 10001):
                candidate = name if number == 1 else f"{stem} ({number}){suffix}"
                key = "named/" + candidate
                path = self.path(key)
                try:
                    output = path.open("xb")
                    created = True
                    break
                except FileExistsError:
                    continue
            else:
                raise StorageError("Có quá nhiều file trùng tên; vui lòng đổi tên file")
            with output:
                output.write(data)
                output.flush()
                os.fsync(output.fileno())
        except OSError as exc:
            if created and path is not None:
                try:
                    path.unlink(missing_ok=True)
                except OSError:
                    logger.warning("RAG incomplete file cleanup failed")
            raise StorageError("Không lưu được file trên máy chủ; kiểm tra dung lượng và quyền thư mục knowlegde") from exc
        return StoredFile(key, name, MIME_TYPES[suffix.lower()], len(data), origin)

    def read(self, key):
        try:
            return self.path(key).read_bytes()
        except FileNotFoundError:
            raise
        except OSError as exc:
            raise StorageError("Không đọc được file trên máy chủ") from exc

    def remove(self, key):
        if not key:
            return True
        try:
            self.path(key).unlink(missing_ok=True)
            return True
        except (OSError, StorageError):
            logger.warning("RAG local file cleanup failed; manual cleanup required")
            return False
