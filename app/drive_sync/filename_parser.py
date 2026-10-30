from __future__ import annotations

import re
from dataclasses import asdict, dataclass

from app.drive_sync.departments import (
    DepartmentMappingError,
    normalize_department_code,
    resolve_department,
)


DOCUMENT_KINDS = {
    "QT": "Quy trình",
    "QD": "Quy định",
    "CS": "Chính sách",
    "HD": "Hướng dẫn",
    "BM": "Biểu mẫu",
    "BC": "Báo cáo",
    "KH": "Kế hoạch",
    "TL": "Tài liệu tham khảo",
}

SUPPORTED_EXTENSIONS = {"pdf", "docx", "xlsx", "csv", "txt", "md"}

FILENAME_RE = re.compile(
    r"^(?P<department>[A-ZĐ][A-ZĐ0-9_]*)-"
    r"(?P<kind>[A-Z]{2,10})-"
    r"(?P<sequence>[0-9]{3})_"
    r"(?P<slug>[A-Za-z0-9]+(?:-[A-Za-z0-9]+)*)_"
    r"V(?P<version>[1-9][0-9]*)\."
    r"(?P<extension>[A-Za-z0-9]+)$"
)


class DriveFilenameError(ValueError):
    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code


@dataclass(frozen=True)
class ParsedDriveFilename:
    original_file_name: str
    department_code: str
    department_name: str
    document_kind_code: str
    document_kind_name: str
    sequence_number: int
    document_code: str
    document_slug: str
    document_title: str
    document_version: int
    file_extension: str

    def as_dict(self):
        return asdict(self)


def parse_drive_filename(
    file_name: str,
    *,
    expected_department_code: str | None = None,
) -> ParsedDriveFilename:
    value = (file_name or "").strip()
    if not value or "/" in value or "\\" in value:
        raise DriveFilenameError(
            "INVALID_FILENAME",
            "Tên file không hợp lệ",
        )

    match = FILENAME_RE.fullmatch(value)
    if match is None:
        raise DriveFilenameError(
            "INVALID_FILENAME_FORMAT",
            "Tên file phải theo mẫu "
            "[PHONGBAN]-[LOAI]-[STT]_[TEN-TAI-LIEU]_V[VERSION].[EXT]",
        )

    raw_department = match.group("department")
    try:
        department = resolve_department(raw_department)
    except DepartmentMappingError as exc:
        raise DriveFilenameError("UNKNOWN_DEPARTMENT", str(exc)) from exc

    expected = (
        normalize_department_code(expected_department_code)
        if expected_department_code
        else None
    )
    if expected is not None and department.code != expected:
        raise DriveFilenameError(
            "DEPARTMENT_MISMATCH",
            f"Mã phòng ban trong file là '{department.code}' "
            f"nhưng folder hiện tại là '{expected}'",
        )

    kind_code = match.group("kind")
    kind_name = DOCUMENT_KINDS.get(kind_code)
    if kind_name is None:
        allowed = ", ".join(DOCUMENT_KINDS)
        raise DriveFilenameError(
            "UNSUPPORTED_DOCUMENT_KIND",
            f"Mã loại tài liệu '{kind_code}' không hợp lệ. "
            f"Các mã hợp lệ: {allowed}",
        )

    sequence_number = int(match.group("sequence"))
    if sequence_number == 0:
        raise DriveFilenameError(
            "INVALID_SEQUENCE",
            "STT phải từ 001 đến 999",
        )

    extension = match.group("extension").lower()
    if extension not in SUPPORTED_EXTENSIONS:
        allowed = ", ".join(sorted(SUPPORTED_EXTENSIONS))
        raise DriveFilenameError(
            "UNSUPPORTED_EXTENSION",
            f"Định dạng '.{extension}' chưa được hỗ trợ. "
            f"Các định dạng hợp lệ: {allowed}",
        )

    slug = match.group("slug")
    return ParsedDriveFilename(
        original_file_name=value,
        department_code=department.code,
        department_name=department.name,
        document_kind_code=kind_code,
        document_kind_name=kind_name,
        sequence_number=sequence_number,
        document_code=f"{department.code}-{kind_code}-{sequence_number:03d}",
        document_slug=slug,
        document_title=slug.replace("-", " "),
        document_version=int(match.group("version")),
        file_extension=extension,
    )
