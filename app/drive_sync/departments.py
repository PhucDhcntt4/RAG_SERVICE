from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass


@dataclass(frozen=True)
class Department:
    code: str
    name: str


class DepartmentMappingError(ValueError):
    pass


DEPARTMENT_CODE_RE = re.compile(r"^[A-Z][A-Z0-9_]{0,49}$")
DEPARTMENT_SEPARATOR_RE = re.compile(r"\s*[-–—]\s*", re.UNICODE)


def normalize_department_code(value: str) -> str:
    text = (value or "").strip()
    text = text.replace("Đ", "D").replace("đ", "d")
    text = unicodedata.normalize("NFD", text)
    text = "".join(
        character
        for character in text
        if unicodedata.category(character) != "Mn"
    )
    text = text.upper()
    text = re.sub(r"\s+", "_", text)
    text = re.sub(r"[^A-Z0-9_]", "", text)
    return text


def resolve_department(folder_name: str) -> Department:
    value = (folder_name or "").strip()
    if not value:
        raise DepartmentMappingError(
            "Tên folder phòng ban không được để trống"
        )

    # Hỗ trợ tên chỉ có mã (HR) hoặc mã kèm tên hiển thị
    # (HR-Nhân sự, HR–Nhân sự, HR—Nhân sự).
    parts = DEPARTMENT_SEPARATOR_RE.split(value, maxsplit=1)
    raw_code = parts[0].strip()
    code = normalize_department_code(raw_code)

    if not DEPARTMENT_CODE_RE.fullmatch(code):
        raise DepartmentMappingError(
            f"Không thể tạo mã phòng ban hợp lệ từ folder '{folder_name}'. "
            "Mã phải bắt đầu bằng chữ và chỉ chứa chữ, số hoặc dấu gạch dưới."
        )

    department_name = (
        parts[1].strip()
        if len(parts) > 1 and parts[1].strip()
        else raw_code
    )
    return Department(code=code, name=department_name)


def normalized_scope(scope: str) -> str:
    value = (scope or "").strip().lower()
    if value not in {"public", "private"}:
        raise DepartmentMappingError(f"Phạm vi không hợp lệ: {scope}")
    return value


def build_group_code(department: Department, scope: str) -> str:
    return f"{department.code.lower()}_{normalized_scope(scope)}"


def build_group_name(department: Department, scope: str) -> str:
    value = normalized_scope(scope)
    suffix = "Public" if value == "public" else "Private"
    return f"{department.name} {suffix}"
