from __future__ import annotations

import unicodedata
from dataclasses import dataclass


@dataclass(frozen=True)
class Department:
    code: str
    name: str


DEPARTMENTS = {
    "BGD": Department(code="BGD", name="Ban Giám đốc"),
    "HR": Department(code="HR", name="Nhân sự"),
    "ACCOUNTING": Department(code="ACCOUNTING", name="Kế toán"),
    "SALES": Department(code="SALES", name="Kinh doanh"),
    "MARKETING": Department(code="MARKETING", name="Marketing"),
    "CSKH": Department(code="CSKH", name="Chăm sóc khách hàng"),
    "IT": Department(code="IT", name="Công nghệ thông tin"),
}


class DepartmentMappingError(ValueError):
    pass


def normalize_department_code(value: str) -> str:
    text = (value or "").strip().replace("Đ", "D").replace("đ", "d")
    text = unicodedata.normalize("NFD", text)
    text = "".join(
        character
        for character in text
        if unicodedata.category(character) != "Mn"
    )
    return text.upper()


def resolve_department(folder_name: str) -> Department:
    code = normalize_department_code(folder_name)
    department = DEPARTMENTS.get(code)
    if department is None:
        allowed = ", ".join(DEPARTMENTS)
        raise DepartmentMappingError(
            f"Folder phòng ban '{folder_name}' không được hỗ trợ. "
            f"Các mã hợp lệ: {allowed}"
        )
    return department


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
