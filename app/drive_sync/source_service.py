from __future__ import annotations

from app.drive_sync.departments import (
    build_group_code,
    build_group_name,
    resolve_department,
)
from app.taxonomy_repository import TaxonomyConflict


class DriveSourceError(RuntimeError):
    pass


class DriveSourceService:
    def __init__(
        self,
        *,
        drive_client,
        drive_repository,
        taxonomy_repository,
    ):
        self.drive = drive_client
        self.repository = drive_repository
        self.taxonomy = taxonomy_repository

    def _get_internal_doc_type(self):
        doc_type = next(
            (
                item
                for item in self.taxonomy.list_doc_types()
                if item["code"] == "internal"
            ),
            None,
        )
        if doc_type is None:
            raise DriveSourceError(
                "Không tìm thấy loại tài liệu có mã 'internal'"
            )
        if not doc_type["is_active"]:
            raise DriveSourceError("Loại 'Tài liệu nội bộ' đang bị tạm dừng")
        return doc_type

    def _ensure_group(
        self,
        *,
        doc_type_id: int,
        group_code: str,
        group_name: str,
    ):
        current = self.taxonomy.get_group_by_code(group_code)
        if current is not None:
            return self._validate_group(current, doc_type_id, group_code)

        try:
            return self.taxonomy.create_group(
                doc_type_id=doc_type_id,
                code=group_code,
                name=group_name,
                description="Nhóm được tạo tự động từ Google Drive",
            )
        except TaxonomyConflict:
            # A second process may have created the same group concurrently.
            current = self.taxonomy.get_group_by_code(group_code)
            if current is None:
                raise
            return self._validate_group(current, doc_type_id, group_code)

    @staticmethod
    def _validate_group(group, doc_type_id: int, group_code: str):
        if group["doc_type_id"] != doc_type_id:
            raise DriveSourceError(
                f"Nhóm '{group_code}' đã tồn tại "
                "nhưng thuộc loại tài liệu khác"
            )
        if not group["is_active"]:
            raise DriveSourceError(
                f"Nhóm '{group_code}' đang bị tạm dừng; "
                "hãy bật lại nhóm trước khi đăng ký nguồn Drive"
            )
        return group

    def register_department(self, folder_id: str):
        structure = self.drive.inspect_department_folder(folder_id)
        folder_name = structure["department"]["name"]
        department = resolve_department(folder_name)
        doc_type = self._get_internal_doc_type()

        groups = {}
        for scope in ("public", "private"):
            groups[scope] = self._ensure_group(
                doc_type_id=doc_type["id"],
                group_code=build_group_code(department, scope),
                group_name=build_group_name(department, scope),
            )

        source = self.repository.upsert_source(
            folder_id=structure["department"]["id"],
            folder_name=folder_name,
            department_code=department.code,
            department_name=department.name,
            doc_type_id=doc_type["id"],
        )
        return {
            "source": source,
            "groups": groups,
            "structure": structure,
        }
