import json
import os
from dataclasses import dataclass
from typing import Dict, List, Optional, Tuple, Any

from .constants import DEFAULT_ENROLL_MAPPING, DEFAULT_SIGNIN_MAPPING
from .csv_utils import apply_field_mapping
from .models import (
    EnrollmentRecord,
    FieldMapping,
    ImportErrorRecord,
    MatchRule,
    SigninRecord,
)


@dataclass
class ImportEnrollResult:
    valid_records: List[EnrollmentRecord]
    errors: List[Dict[str, Any]]
    new_ids: List[int]
    existing_ids: List[int]


@dataclass
class ImportSigninResult:
    valid_records: List[SigninRecord]
    errors: List[Dict[str, Any]]
    new_ids: List[int]
    existing_ids: List[int]


@dataclass
class ImportRulesResult:
    rules: List[MatchRule]
    errors: List[str]
    field_mapping_data: Optional[Dict[str, Any]] = None


def validate_enroll_rows(
    mapped_rows: List[dict],
    source_file: str,
    storage,
) -> Tuple[List[EnrollmentRecord], List[Dict[str, Any]]]:
    valid_records: List[EnrollmentRecord] = []
    errors: List[Dict[str, Any]] = []

    for idx, row in enumerate(mapped_rows):
        source_row = idx + 2
        name = (row.get("name", "") or "").strip()
        phone = (row.get("phone", "") or "").strip()
        session = (row.get("session", "") or "").strip()

        row_errors = []
        if not phone:
            row_errors.append(("missing_phone", f"第 {source_row} 行：手机号缺失"))
        if not session:
            row_errors.append(("missing_session", f"第 {source_row} 行：场次不能为空"))
        if session and storage.is_session_closed(session):
            row_errors.append(("session_closed", f"第 {source_row} 行：场次「{session}」已关闭，无法导入"))

        if row_errors:
            raw = json.dumps({k: v for k, v in row.items() if v}, ensure_ascii=False)
            for error_type, error_msg in row_errors:
                errors.append({
                    "row": source_row,
                    "error_type": error_type,
                    "error_message": error_msg,
                    "raw_data": raw,
                })
            continue

        valid_records.append(EnrollmentRecord(
            name=name, phone=phone, session=session,
            source_file=os.path.basename(source_file), source_row=source_row,
        ))

    return valid_records, errors


def validate_signin_rows(
    mapped_rows: List[dict],
    source_file: str,
    storage,
) -> Tuple[List[SigninRecord], List[Dict[str, Any]]]:
    valid_records: List[SigninRecord] = []
    errors: List[Dict[str, Any]] = []
    existing_sessions = storage.get_enrollment_sessions()

    for idx, row in enumerate(mapped_rows):
        source_row = idx + 2
        name = (row.get("name", "") or "").strip()
        phone = (row.get("phone", "") or "").strip()
        session = (row.get("session", "") or "").strip()
        scan_time = (row.get("scan_time", "") or "").strip()

        row_errors = []
        if not phone:
            row_errors.append(("missing_phone", f"第 {source_row} 行：手机号缺失"))
        if existing_sessions and session and session not in existing_sessions:
            row_errors.append(("invalid_session", f"第 {source_row} 行：场次「{session}」不存在"))
        if session and storage.is_session_closed(session):
            row_errors.append(("session_closed", f"第 {source_row} 行：场次「{session}」已关闭，无法导入"))

        if row_errors:
            raw = json.dumps({k: v for k, v in row.items() if v}, ensure_ascii=False)
            for error_type, error_msg in row_errors:
                errors.append({
                    "row": source_row,
                    "error_type": error_type,
                    "error_message": error_msg,
                    "raw_data": raw,
                })
            continue

        valid_records.append(SigninRecord(
            name=name, phone=phone, session=session, scan_time=scan_time,
            source_file=os.path.basename(source_file), source_row=source_row,
        ))

    return valid_records, errors


def import_enrollments(
    storage,
    mapped_rows: List[dict],
    source_file: str,
    dry_run: bool = False,
) -> ImportEnrollResult:
    valid_records, errors = validate_enroll_rows(mapped_rows, source_file, storage)

    if dry_run:
        return ImportEnrollResult(
            valid_records=valid_records,
            errors=errors,
            new_ids=[],
            existing_ids=[],
        )

    new_ids: List[int] = []
    existing_ids: List[int] = []

    if valid_records:
        new_ids, existing_ids = storage.add_enrollments(valid_records)
        if new_ids:
            undo_data = json.dumps({"action": "import_enroll", "ids": new_ids})
            storage.add_undo_action("import_enroll", undo_data)

    if errors:
        err_records = [ImportErrorRecord(
            source_type="enroll",
            source_file=os.path.basename(source_file),
            row_number=e["row"],
            error_type=e["error_type"],
            error_message=e["error_message"],
            raw_data=e.get("raw_data"),
        ) for e in errors]
        storage.add_import_errors(err_records)

    return ImportEnrollResult(
        valid_records=valid_records,
        errors=errors,
        new_ids=new_ids,
        existing_ids=existing_ids,
    )


def import_signins(
    storage,
    mapped_rows: List[dict],
    source_file: str,
    dry_run: bool = False,
) -> ImportSigninResult:
    valid_records, errors = validate_signin_rows(mapped_rows, source_file, storage)

    if dry_run:
        return ImportSigninResult(
            valid_records=valid_records,
            errors=errors,
            new_ids=[],
            existing_ids=[],
        )

    new_ids: List[int] = []
    existing_ids: List[int] = []

    if valid_records:
        new_ids, existing_ids = storage.add_signins(valid_records)
        if new_ids:
            undo_data = json.dumps({"action": "import_signin", "ids": new_ids})
            storage.add_undo_action("import_signin", undo_data)

    if errors:
        err_records = [ImportErrorRecord(
            source_type="signin",
            source_file=os.path.basename(source_file),
            row_number=e["row"],
            error_type=e["error_type"],
            error_message=e["error_message"],
            raw_data=e.get("raw_data"),
        ) for e in errors]
        storage.add_import_errors(err_records)

    return ImportSigninResult(
        valid_records=valid_records,
        errors=errors,
        new_ids=new_ids,
        existing_ids=existing_ids,
    )


def validate_rules_data(
    match_rules_data: List[dict],
    field_mapping_data: Optional[dict] = None,
    allow_contains: bool = True,
) -> ImportRulesResult:
    rules: List[MatchRule] = []
    errors: List[str] = []

    valid_match_types = ("exact", "fuzzy", "contains") if allow_contains else ("exact", "fuzzy")

    for idx, r in enumerate(match_rules_data):
        rule_idx = idx + 1
        field_name = r.get("field", r.get("field_name", ""))
        if not field_name:
            errors.append(f"第 {rule_idx} 条规则：缺少 field 字段")
            continue
        match_type = r.get("match_type", "exact")
        if match_type not in valid_match_types:
            errors.append(f"第 {rule_idx} 条规则：match_type「{match_type}」不合法，可选 {'/'.join(valid_match_types)}")
            continue
        threshold = r.get("threshold")
        if match_type == "fuzzy" and threshold is None:
            errors.append(f"第 {rule_idx} 条规则：fuzzy 匹配必须指定 threshold")
            continue
        rules.append(MatchRule(
            field_name=field_name,
            match_type=match_type,
            threshold=threshold,
            priority=r.get("priority", 0),
        ))

    if field_mapping_data:
        if not isinstance(field_mapping_data, dict):
            errors.append("field_mapping 必须是对象")
        else:
            enroll = field_mapping_data.get("enroll")
            signin = field_mapping_data.get("signin")
            if enroll is not None and not isinstance(enroll, dict):
                errors.append("field_mapping.enroll 必须是对象")
            if signin is not None and not isinstance(signin, dict):
                errors.append("field_mapping.signin 必须是对象")

    return ImportRulesResult(
        rules=rules,
        errors=errors,
        field_mapping_data=field_mapping_data if not errors else None,
    )


def import_rules(
    storage,
    match_rules_data: List[dict],
    field_mapping_data: Optional[dict] = None,
    dry_run: bool = False,
    allow_contains: bool = True,
) -> ImportRulesResult:
    result = validate_rules_data(match_rules_data, field_mapping_data, allow_contains=allow_contains)

    if dry_run:
        return result

    if result.errors:
        return result

    prev_rules = storage.get_all_rules()
    prev_mapping = storage.get_field_mapping()

    storage.clear_rules()

    if result.rules:
        storage.add_rules(result.rules)

    if field_mapping_data:
        fm = FieldMapping(
            enroll=field_mapping_data.get("enroll", {}),
            signin=field_mapping_data.get("signin", {}),
        )
        storage.save_field_mapping(fm)

    undo_data = json.dumps({
        "action": "import_rules",
        "prev_rules": [{"field_name": r.field_name, "match_type": r.match_type, "threshold": r.threshold, "priority": r.priority} for r in prev_rules],
        "prev_mapping": {"enroll": prev_mapping.enroll, "signin": prev_mapping.signin} if prev_mapping.enroll or prev_mapping.signin else None,
    }, ensure_ascii=False)
    storage.add_undo_action("import_rules", undo_data)

    return result


def get_enroll_mapping(storage) -> dict:
    field_mapping = storage.get_field_mapping()
    return field_mapping.enroll if field_mapping.enroll else dict(DEFAULT_ENROLL_MAPPING)


def get_signin_mapping(storage) -> dict:
    field_mapping = storage.get_field_mapping()
    return field_mapping.signin if field_mapping.signin else dict(DEFAULT_SIGNIN_MAPPING)
