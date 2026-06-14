from dataclasses import dataclass, field
from typing import Optional, List


@dataclass
class EnrollmentRecord:
    id: Optional[int] = None
    name: str = ""
    phone: Optional[str] = None
    session: str = ""
    source_file: Optional[str] = None
    source_row: Optional[int] = None
    imported_at: Optional[str] = None


@dataclass
class SigninRecord:
    id: Optional[int] = None
    name: str = ""
    phone: Optional[str] = None
    session: str = ""
    scan_time: Optional[str] = None
    source_file: Optional[str] = None
    source_row: Optional[int] = None
    imported_at: Optional[str] = None


@dataclass
class MatchRule:
    id: Optional[int] = None
    field_name: str = ""
    match_type: str = "exact"
    threshold: Optional[float] = None
    priority: int = 0
    imported_at: Optional[str] = None


@dataclass
class ReconcileResult:
    id: Optional[int] = None
    enroll_id: Optional[int] = None
    signin_id: Optional[int] = None
    name: str = ""
    phone: Optional[str] = None
    session: str = ""
    status: str = ""
    manual_mark: Optional[str] = None
    notes: Optional[str] = None
    created_at: Optional[str] = None


@dataclass
class ImportErrorRecord:
    id: Optional[int] = None
    source_type: str = ""
    source_file: Optional[str] = None
    row_number: int = 0
    error_type: str = ""
    error_message: str = ""
    raw_data: Optional[str] = None


@dataclass
class UndoAction:
    id: Optional[int] = None
    action_type: str = ""
    action_data: str = ""
    created_at: Optional[str] = None


@dataclass
class FieldMapping:
    enroll: dict = field(default_factory=dict)
    signin: dict = field(default_factory=dict)


@dataclass
class SessionRecord:
    id: Optional[int] = None
    name: str = ""
    status: str = "open"
    start_time: Optional[str] = None
    end_time: Optional[str] = None
    description: Optional[str] = None
    created_at: Optional[str] = None
    closed_at: Optional[str] = None


@dataclass
class NotificationRule:
    id: Optional[int] = None
    session: str = ""
    channel: str = ""
    target: str = ""
    enabled: int = 1
    absent_threshold: int = 0
    extra_config: Optional[str] = None
    created_at: Optional[str] = None
    updated_at: Optional[str] = None


@dataclass
class NotificationLog:
    id: Optional[int] = None
    channel: str = ""
    target: str = ""
    session: str = ""
    status: str = ""
    message: Optional[str] = None
    retries: int = 0
    created_at: Optional[str] = None


@dataclass
class HandoffPackage:
    id: Optional[int] = None
    package_id: str = ""
    session: str = ""
    operator: str = ""
    enroll_count: int = 0
    signin_count: int = 0
    result_count: int = 0
    status_summary: str = ""
    manual_mark_count: int = 0
    generated_at: Optional[str] = None
    data_hash: Optional[str] = None
    created_at: Optional[str] = None


@dataclass
class HandoffExportLog:
    id: Optional[int] = None
    package_id: str = ""
    export_path: str = ""
    operator: str = ""
    exported_at: Optional[str] = None


@dataclass
class HandoffAuditLog:
    id: Optional[int] = None
    operator: str = ""
    action: str = ""
    target: str = ""
    result: str = ""
    detail: Optional[str] = None
    created_at: Optional[str] = None
