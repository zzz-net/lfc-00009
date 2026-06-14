import csv
import io
import json
import logging
import os
import secrets
import sqlite3
import tempfile
import time
import zipfile
from collections import defaultdict
from datetime import datetime, timedelta
from logging.handlers import TimedRotatingFileHandler
from typing import Any, Dict, List, Optional, Tuple, Union

from fastapi import (
    Depends,
    FastAPI,
    File,
    Form,
    Header,
    HTTPException,
    Query,
    Request,
    Response,
    UploadFile,
    status,
)
from fastapi.responses import (
    FileResponse,
    HTMLResponse,
    JSONResponse,
    StreamingResponse,
)
from pydantic import BaseModel, Field

from .constants import STATUS_LABELS, VALID_STATUSES
from .csv_utils import parse_csv_content, apply_field_mapping
from .import_service import (
    import_enrollments,
    import_signins,
    import_rules,
    validate_enroll_rows,
    validate_signin_rows,
    get_enroll_mapping,
    get_signin_mapping,
)
from .export_service import (
    result_to_dict,
    compute_status_counts,
    format_csv_content,
    write_xlsx_file,
    build_html_report,
    build_session_stats_for_report,
    build_session_detail,
)
from .models import (
    EnrollmentRecord,
    FieldMapping,
    ImportErrorRecord,
    MatchRule,
    ReconcileResult,
    SigninRecord,
)
from .reconcile import batch_mark as do_batch_mark
from .reconcile import mark_result as do_mark_result
from .reconcile import reconcile as do_reconcile
from .reconcile import undo as do_undo
from .storage import Storage
from .notifier import notify_after_reconcile
from .models import NotificationRule

# ──────────────────────────────────────────────────────────────────
# Configuration
# ──────────────────────────────────────────────────────────────────

DEFAULT_API_TOKEN = os.environ.get("SIGNCHECK_API_TOKEN") or "signcheck-demo-token-2024"
API_HOST = os.environ.get("SIGNCHECK_API_HOST", "0.0.0.0")
API_PORT = int(os.environ.get("SIGNCHECK_API_PORT", "8000"))

LOG_DIR = os.path.join(os.getcwd(), ".signcheck", "logs")
os.makedirs(LOG_DIR, exist_ok=True)


# ──────────────────────────────────────────────────────────────────
# Request Logger (daily rotation, keep 30 days)
# ──────────────────────────────────────────────────────────────────

def _setup_request_logger() -> logging.Logger:
    logger = logging.getLogger("signcheck_api")
    logger.setLevel(logging.INFO)
    logger.propagate = False

    if logger.handlers:
        return logger

    handler = TimedRotatingFileHandler(
        filename=os.path.join(LOG_DIR, "api_requests.log"),
        when="midnight",
        interval=1,
        backupCount=30,
        encoding="utf-8",
    )
    handler.suffix = "%Y-%m-%d"
    formatter = logging.Formatter("%(message)s")
    handler.setFormatter(formatter)
    logger.addHandler(handler)

    return logger


request_logger = _setup_request_logger()


# ──────────────────────────────────────────────────────────────────
# Pydantic Schemas
# ──────────────────────────────────────────────────────────────────

class ApiResponse(BaseModel):
    code: int = 0
    message: str = "success"
    data: Any = None


class EnrollImportItem(BaseModel):
    name: str
    phone: str
    session: str


class SigninImportItem(BaseModel):
    name: str
    phone: str
    session: str
    scan_time: Optional[str] = None


class MatchRuleItem(BaseModel):
    field: str
    match_type: str = "exact"
    threshold: Optional[float] = None
    priority: int = 0


class FieldMappingItem(BaseModel):
    enroll: Optional[Dict[str, str]] = None
    signin: Optional[Dict[str, str]] = None


class RulesImportRequest(BaseModel):
    match_rules: List[MatchRuleItem]
    field_mapping: Optional[FieldMappingItem] = None


class MarkRequest(BaseModel):
    mark_text: Optional[str] = None
    notes: Optional[str] = None


class BatchMarkItem(BaseModel):
    result_id: int
    mark_text: str
    notes: Optional[str] = None


class BatchMarkRequest(BaseModel):
    items: List[BatchMarkItem]


class NotificationRuleCreate(BaseModel):
    session: str
    channel: str = Field(pattern="^(email|webhook)$")
    target: str
    enabled: int = Field(default=1, ge=0, le=1)
    absent_threshold: int = Field(default=0, ge=0)
    extra_config: Optional[str] = None


class NotificationRuleUpdate(BaseModel):
    session: Optional[str] = None
    channel: Optional[str] = Field(default=None, pattern="^(email|webhook)$")
    target: Optional[str] = None
    enabled: Optional[int] = Field(default=None, ge=0, le=1)
    absent_threshold: Optional[int] = Field(default=None, ge=0)
    extra_config: Optional[str] = None


# ──────────────────────────────────────────────────────────────────
# Storage Dependency
# ──────────────────────────────────────────────────────────────────

_storage_singleton: Optional[Storage] = None


def get_storage() -> Storage:
    """FastAPI dependency: get (or create) the shared Storage instance.

    Storage 构造函数会自动初始化表、做迁移；重启后自动从现有 SQLite 文件加载，
    无需额外「加载」步骤。
    """
    global _storage_singleton
    if _storage_singleton is None:
        _storage_singleton = Storage()
    return _storage_singleton


# ──────────────────────────────────────────────────────────────────
# Token Auth Dependency
# ──────────────────────────────────────────────────────────────────

def verify_token(authorization: Optional[str] = Header(default=None)) -> None:
    """Verify Bearer token from Authorization header. Returns 401 on failure."""
    if not authorization:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail={"code": 40101, "message": "缺少 Authorization header"},
        )
    parts = authorization.split()
    if len(parts) == 2 and parts[0].lower() == "bearer":
        token = parts[1]
    else:
        token = authorization
    if not secrets.compare_digest(token, DEFAULT_API_TOKEN):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail={"code": 40102, "message": "Token 无效或错误"},
        )


# ──────────────────────────────────────────────────────────────────
# FastAPI App
# ──────────────────────────────────────────────────────────────────

app = FastAPI(
    title="SignCheck 签到对账 Web API",
    version="1.0.0",
    description="线下培训签到对账工具的 REST API 服务",
)

process_start_time = datetime.now()


# ──────────────────────────────────────────────────────────────────
# Middleware: Request Logging
# ──────────────────────────────────────────────────────────────────

@app.middleware("http")
async def log_requests(request: Request, call_next):
    start_time = time.time()
    try:
        response = await call_next(request)
        status_code = response.status_code
    except HTTPException as exc:
        status_code = exc.status_code
        response = JSONResponse(
            status_code=exc.status_code,
            content={"code": exc.status_code * 100 + 1, "message": str(exc.detail), "data": None},
        )
    except Exception as exc:
        status_code = 500
        response = JSONResponse(
            status_code=500,
            content={"code": 50000, "message": f"服务器内部错误: {exc}", "data": None},
        )
    finally:
        elapsed_ms = int((time.time() - start_time) * 1000)
        timestamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        log_line = (
            f"{timestamp} | {request.method:6s} {request.url.path} | "
            f"status={status_code} | elapsed={elapsed_ms}ms"
        )
        try:
            request_logger.info(log_line)
        except Exception:
            pass

    return response


# ──────────────────────────────────────────────────────────────────
# Helpers
# ──────────────────────────────────────────────────────────────────


# ──────────────────────────────────────────────────────────────────
# /health — 探活接口（无需鉴权）
# ──────────────────────────────────────────────────────────────────

@app.get("/health", summary="探活接口（返回服务状态和数据库连通情况）")
def health_check():
    db_status = "unknown"
    db_info: Dict[str, Any] = {}
    try:
        storage = get_storage()
        cur = storage.conn.execute("SELECT 1")
        cur.fetchone()
        db_status = "connected"
        stats = storage.get_stats()
        db_info = {
            "path": storage.db_path,
            "tables": {
                "enrollments": stats["enrollment_count"],
                "signins": stats["signin_count"],
                "rules": stats["rules_count"],
                "reconcile_results": stats["result_count"],
                "import_errors": stats["error_count"],
                "undo_history": stats["undo_count"],
            },
        }
    except Exception as exc:
        db_status = "error"
        db_info["error"] = str(exc)

    return {
        "code": 0,
        "message": "ok",
        "data": {
            "service": {
                "status": "running",
                "version": "1.0.0",
                "timestamp": datetime.now().isoformat(),
                "uptime_source": process_start_time.isoformat() if "process_start_time" in globals() else None,
            },
            "database": {
                "status": db_status,
                **db_info,
            },
        },
    }


# ──────────────────────────────────────────────────────────────────
# 1. 导入报名
# ──────────────────────────────────────────────────────────────────

@app.post("/api/v1/import/enroll", summary="导入报名 CSV（上传文件）")
def import_enroll_file(
    file: UploadFile = File(..., description="CSV 文件，需包含 姓名/手机号/场次 列"),
    dry_run: bool = Query(default=False, description="只校验不落库"),
    storage: Storage = Depends(get_storage),
    _: None = Depends(verify_token),
):
    try:
        content = file.file.read()
    except Exception as exc:
        raise HTTPException(400, f"读取文件失败: {exc}")
    finally:
        file.file.close()

    rows = parse_csv_content(content)
    filename = os.path.basename(file.filename or "enroll.csv")

    field_mapping = storage.get_field_mapping()
    mapping = field_mapping.enroll or {"name": "姓名", "phone": "手机号", "session": "场次"}
    mapped_rows = apply_field_mapping(rows, mapping)

    valid_records: List[EnrollmentRecord] = []
    errors: List[Dict[str, Any]] = []

    for idx, row in enumerate(mapped_rows):
        source_row = idx + 2
        name = (row.get("name", "") or "").strip()
        phone = (row.get("phone", "") or "").strip()
        session = (row.get("session", "") or "").strip()
        row_errors = []
        if not phone:
            row_errors.append(("missing_phone", f"手机号缺失"))
        if not session:
            row_errors.append(("missing_session", f"场次不能为空"))
        if session and storage.is_session_closed(session):
            row_errors.append(("session_closed", f"场次「{session}」已关闭"))
        if row_errors:
            raw = json.dumps({k: v for k, v in row.items() if v}, ensure_ascii=False)
            for et, em in row_errors:
                errors.append({
                    "row": source_row,
                    "error_type": et,
                    "error_message": f"第 {source_row} 行: {em}",
                    "raw_data": raw,
                })
            continue
        valid_records.append(EnrollmentRecord(
            name=name, phone=phone, session=session,
            source_file=filename, source_row=source_row,
        ))

    if dry_run:
        return ApiResponse(data={
            "valid_count": len(valid_records),
            "error_count": len(errors),
            "errors": errors,
            "mode": "dry_run",
        })

    if valid_records:
        new_ids, existing_ids = storage.add_enrollments(valid_records)
        if new_ids:
            undo_data = json.dumps({"action": "import_enroll", "ids": new_ids})
            storage.add_undo_action("import_enroll", undo_data)
    else:
        new_ids, existing_ids = [], []

    if errors:
        err_records = [ImportErrorRecord(
            source_type="enroll", source_file=filename,
            row_number=e["row"], error_type=e["error_type"],
            error_message=e["error_message"], raw_data=e["raw_data"],
        ) for e in errors]
        storage.add_import_errors(err_records)

    return ApiResponse(data={
        "valid_count": len(valid_records),
        "new_count": len(new_ids),
        "skipped_existing": len(existing_ids),
        "error_count": len(errors),
        "errors": errors,
        "new_ids": new_ids,
    })


@app.post("/api/v1/import/enroll/json", summary="导入报名（JSON 数组）")
def import_enroll_json(
    items: List[EnrollImportItem],
    dry_run: bool = Query(default=False),
    source_file: str = Query(default="api_json_import"),
    storage: Storage = Depends(get_storage),
    _: None = Depends(verify_token),
):
    valid_records: List[EnrollmentRecord] = []
    errors: List[Dict[str, Any]] = []
    for idx, item in enumerate(items):
        source_row = idx + 1
        name = (item.name or "").strip()
        phone = (item.phone or "").strip()
        session = (item.session or "").strip()
        row_errors = []
        if not phone:
            row_errors.append(("missing_phone", "手机号缺失"))
        if not session:
            row_errors.append(("missing_session", "场次不能为空"))
        if session and storage.is_session_closed(session):
            row_errors.append(("session_closed", f"场次「{session}」已关闭"))
        if row_errors:
            for et, em in row_errors:
                errors.append({"row": source_row, "error_type": et, "error_message": em})
            continue
        valid_records.append(EnrollmentRecord(
            name=name, phone=phone, session=session,
            source_file=source_file, source_row=source_row,
        ))

    if dry_run:
        return ApiResponse(data={"valid_count": len(valid_records), "error_count": len(errors), "errors": errors})

    new_ids, existing_ids = storage.add_enrollments(valid_records) if valid_records else ([], [])
    if new_ids:
        storage.add_undo_action("import_enroll", json.dumps({"ids": new_ids}))

    return ApiResponse(data={
        "new_count": len(new_ids),
        "skipped_existing": len(existing_ids),
        "error_count": len(errors),
        "errors": errors,
    })


# ──────────────────────────────────────────────────────────────────
# 2. 导入签到
# ──────────────────────────────────────────────────────────────────

@app.post("/api/v1/import/signin", summary="导入签到 CSV（上传文件）")
def import_signin_file(
    file: UploadFile = File(..., description="CSV 文件，需包含 姓名/手机号/场次/扫码时间 列"),
    dry_run: bool = Query(default=False),
    storage: Storage = Depends(get_storage),
    _: None = Depends(verify_token),
):
    try:
        content = file.file.read()
    except Exception as exc:
        raise HTTPException(400, f"读取文件失败: {exc}")
    finally:
        file.file.close()

    rows = parse_csv_content(content)
    filename = os.path.basename(file.filename or "signin.csv")

    field_mapping = storage.get_field_mapping()
    mapping = field_mapping.signin or {"name": "姓名", "phone": "手机号", "session": "场次", "scan_time": "扫码时间"}
    mapped_rows = apply_field_mapping(rows, mapping)
    existing_sessions = storage.get_enrollment_sessions()

    valid_records: List[SigninRecord] = []
    errors: List[Dict[str, Any]] = []

    for idx, row in enumerate(mapped_rows):
        source_row = idx + 2
        name = (row.get("name", "") or "").strip()
        phone = (row.get("phone", "") or "").strip()
        session = (row.get("session", "") or "").strip()
        scan_time = (row.get("scan_time", "") or "").strip()
        row_errors = []
        if not phone:
            row_errors.append(("missing_phone", "手机号缺失"))
        if existing_sessions and session and session not in existing_sessions:
            row_errors.append(("invalid_session", f"场次「{session}」不存在"))
        if session and storage.is_session_closed(session):
            row_errors.append(("session_closed", f"场次「{session}」已关闭"))
        if row_errors:
            raw = json.dumps({k: v for k, v in row.items() if v}, ensure_ascii=False)
            for et, em in row_errors:
                errors.append({
                    "row": source_row, "error_type": et,
                    "error_message": f"第 {source_row} 行: {em}", "raw_data": raw,
                })
            continue
        valid_records.append(SigninRecord(
            name=name, phone=phone, session=session, scan_time=scan_time,
            source_file=filename, source_row=source_row,
        ))

    if dry_run:
        return ApiResponse(data={
            "valid_count": len(valid_records),
            "error_count": len(errors),
            "errors": errors,
        })

    new_ids, existing_ids = storage.add_signins(valid_records) if valid_records else ([], [])
    if new_ids:
        storage.add_undo_action("import_signin", json.dumps({"ids": new_ids}))

    if errors:
        err_records = [ImportErrorRecord(
            source_type="signin", source_file=filename,
            row_number=e["row"], error_type=e["error_type"],
            error_message=e["error_message"], raw_data=e.get("raw_data"),
        ) for e in errors]
        storage.add_import_errors(err_records)

    return ApiResponse(data={
        "valid_count": len(valid_records),
        "new_count": len(new_ids),
        "skipped_existing": len(existing_ids),
        "error_count": len(errors),
        "errors": errors,
    })


@app.post("/api/v1/import/signin/json", summary="导入签到（JSON 数组）")
def import_signin_json(
    items: List[SigninImportItem],
    dry_run: bool = Query(default=False),
    source_file: str = Query(default="api_json_import"),
    storage: Storage = Depends(get_storage),
    _: None = Depends(verify_token),
):
    existing_sessions = storage.get_enrollment_sessions()
    valid_records: List[SigninRecord] = []
    errors: List[Dict[str, Any]] = []
    for idx, item in enumerate(items):
        source_row = idx + 1
        name = (item.name or "").strip()
        phone = (item.phone or "").strip()
        session = (item.session or "").strip()
        scan_time = (item.scan_time or "").strip() if item.scan_time else ""
        row_errors = []
        if not phone:
            row_errors.append(("missing_phone", "手机号缺失"))
        if existing_sessions and session and session not in existing_sessions:
            row_errors.append(("invalid_session", f"场次「{session}」不存在"))
        if session and storage.is_session_closed(session):
            row_errors.append(("session_closed", f"场次「{session}」已关闭"))
        if row_errors:
            errors.append({"row": source_row, "error_type": row_errors[0][0], "error_message": row_errors[0][1]})
            continue
        valid_records.append(SigninRecord(
            name=name, phone=phone, session=session, scan_time=scan_time,
            source_file=source_file, source_row=source_row,
        ))

    if dry_run:
        return ApiResponse(data={"valid_count": len(valid_records), "errors": errors})

    new_ids, existing_ids = storage.add_signins(valid_records) if valid_records else ([], [])
    if new_ids:
        storage.add_undo_action("import_signin", json.dumps({"ids": new_ids}))

    return ApiResponse(data={
        "new_count": len(new_ids), "skipped_existing": len(existing_ids),
        "error_count": len(errors), "errors": errors,
    })


# ──────────────────────────────────────────────────────────────────
# 3. 导入匹配规则
# ──────────────────────────────────────────────────────────────────

@app.post("/api/v1/import/rules", summary="导入匹配规则与字段映射")
def import_rules(
    req: RulesImportRequest,
    dry_run: bool = Query(default=False),
    storage: Storage = Depends(get_storage),
    _: None = Depends(verify_token),
):
    match_rules_data = req.match_rules
    rules: List[MatchRule] = []
    errors: List[str] = []

    for idx, r in enumerate(match_rules_data):
        rule_idx = idx + 1
        if not r.field:
            errors.append(f"第 {rule_idx} 条规则: 缺少 field 字段")
            continue
        if r.match_type not in ("exact", "fuzzy", "contains"):
            errors.append(f"第 {rule_idx} 条规则: match_type「{r.match_type}」不合法")
            continue
        if r.match_type == "fuzzy" and r.threshold is None:
            errors.append(f"第 {rule_idx} 条规则: fuzzy 匹配必须指定 threshold")
            continue
        rules.append(MatchRule(
            field_name=r.field, match_type=r.match_type,
            threshold=r.threshold, priority=r.priority,
        ))

    field_mapping_data = req.field_mapping
    if field_mapping_data:
        if not isinstance(field_mapping_data.enroll, (type(None), dict)):
            errors.append("field_mapping.enroll 必须是对象")
        if not isinstance(field_mapping_data.signin, (type(None), dict)):
            errors.append("field_mapping.signin 必须是对象")

    if dry_run:
        return ApiResponse(data={
            "valid_rules": len(rules),
            "error_count": len(errors),
            "errors": errors,
            "has_field_mapping": field_mapping_data is not None,
        })

    if errors:
        raise HTTPException(400, {"message": "校验未通过", "errors": errors})

    prev_rules = storage.get_all_rules()
    prev_mapping = storage.get_field_mapping()
    storage.clear_rules()
    if rules:
        storage.add_rules(rules)
    if field_mapping_data:
        fm = FieldMapping(
            enroll=field_mapping_data.enroll or {},
            signin=field_mapping_data.signin or {},
        )
        storage.save_field_mapping(fm)

    storage.add_undo_action("import_rules", json.dumps({
        "action": "import_rules",
        "prev_rules": [{"field_name": r.field_name, "match_type": r.match_type,
                        "threshold": r.threshold, "priority": r.priority} for r in prev_rules],
        "prev_mapping": {"enroll": prev_mapping.enroll, "signin": prev_mapping.signin},
    }, ensure_ascii=False))

    return ApiResponse(data={
        "imported_rules": len(rules),
        "field_mapping_updated": field_mapping_data is not None,
    })


# ──────────────────────────────────────────────────────────────────
# 4. 执行对账（支持按场次过滤）
# ──────────────────────────────────────────────────────────────────

@app.post("/api/v1/reconcile", summary="执行对账（支持按场次过滤）")
def reconcile_endpoint(
    sessions: Optional[List[str]] = Query(default=None, description="指定场次，不指定则全部开放场次"),
    storage: Storage = Depends(get_storage),
    _: None = Depends(verify_token),
):
    enrollments = storage.get_all_enrollments()
    signins = storage.get_all_signins()
    if not enrollments:
        raise HTTPException(400, "尚未导入报名数据")
    if not signins:
        raise HTTPException(400, "尚未导入签到数据")

    target_sessions = set(sessions) if sessions else None
    if target_sessions:
        existing_enroll_sessions = {e.session for e in enrollments}
        existing_signin_sessions = {s.session for s in signins}
        all_existing = existing_enroll_sessions | existing_signin_sessions
        invalid = target_sessions - all_existing
        if invalid:
            raise HTTPException(400, f"场次不存在: {', '.join(sorted(invalid))}")

    filtered_enroll = [e for e in enrollments if target_sessions is None or e.session in target_sessions]
    filtered_signin = [s for s in signins if target_sessions is None or s.session in target_sessions]

    rules = storage.get_all_rules()
    if not rules:
        from .reconcile import _default_rules
        rules = _default_rules()

    closed_sessions = {s.name for s in storage.get_all_sessions() if s.status == "closed"}
    enroll_by_session: Dict[str, List[EnrollmentRecord]] = defaultdict(list)
    for e in filtered_enroll:
        if e.session not in closed_sessions:
            enroll_by_session[e.session].append(e)
    signin_by_session: Dict[str, List[SigninRecord]] = defaultdict(list)
    for s in filtered_signin:
        if s.session not in closed_sessions:
            signin_by_session[s.session].append(s)

    from .matcher import build_enrollment_lookup, find_match

    all_sessions_set = set(enroll_by_session.keys()) | set(signin_by_session.keys())
    results: List[ReconcileResult] = []
    skipped_closed = sorted(closed_sessions & (target_sessions or set()))

    for session in sorted(all_sessions_set):
        session_enrolls = enroll_by_session.get(session, [])
        session_signins = signin_by_session.get(session, [])
        lookup = build_enrollment_lookup(session_enrolls, rules)
        enroll_to_signins: Dict[int, List[SigninRecord]] = defaultdict(list)
        matched_signin_ids: set = set()

        for signin in session_signins:
            enroll = find_match(signin, lookup, rules, set(enroll_to_signins.keys()))
            if enroll is not None:
                enroll_to_signins[enroll.id].append(signin)
                matched_signin_ids.add(signin.id)
            else:
                results.append(ReconcileResult(
                    signin_id=signin.id, name=signin.name, phone=signin.phone,
                    session=session, status="non_enrolled",
                ))
        for enroll in session_enrolls:
            if enroll.id in enroll_to_signins:
                slist = enroll_to_signins[enroll.id]
                results.append(ReconcileResult(
                    enroll_id=enroll.id, signin_id=slist[0].id,
                    name=enroll.name, phone=enroll.phone, session=session, status="normal",
                ))
                for signin in slist[1:]:
                    results.append(ReconcileResult(
                        enroll_id=enroll.id, signin_id=signin.id,
                        name=signin.name, phone=signin.phone, session=session, status="duplicate",
                    ))
            else:
                results.append(ReconcileResult(
                    enroll_id=enroll.id, name=enroll.name, phone=enroll.phone,
                    session=session, status="absent",
                ))

    prev_results = storage.get_all_reconcile_results()
    if target_sessions:
        storage.conn.execute(
            "DELETE FROM reconcile_results WHERE session IN (" +
            ",".join("?" * len(target_sessions)) + ")",
            tuple(target_sessions),
        )
        storage.conn.commit()
    else:
        storage.clear_reconcile_results()
    storage.add_reconcile_results(results)

    prev_serialized = [
        {"id": r.id, "enroll_id": r.enroll_id, "signin_id": r.signin_id,
         "name": r.name, "phone": r.phone, "session": r.session,
         "status": r.status, "manual_mark": r.manual_mark, "notes": r.notes}
        for r in prev_results
    ]
    storage.add_undo_action("reconcile", json.dumps({
        "action": "reconcile", "prev_results": prev_serialized,
    }, ensure_ascii=False))

    status_counts: Dict[str, int] = defaultdict(int)
    for r in results:
        status_counts[r.status] += 1

    session_breakdown: Dict[str, Dict[str, int]] = defaultdict(lambda: defaultdict(int))
    for r in results:
        session_breakdown[r.session][r.status] += 1

    return ApiResponse(data={
        "total": len(results),
        "sessions_processed": len(all_sessions_set),
        "target_sessions": sorted(target_sessions) if target_sessions else "all",
        "skipped_closed_sessions": skipped_closed,
        "status_summary": {
            s: {"count": status_counts.get(s, 0), "label": STATUS_LABELS[s]}
            for s in ["normal", "absent", "non_enrolled", "duplicate"]
        },
        "session_breakdown": {
            sess: {
                st: {"count": cnt, "label": STATUS_LABELS[st]}
                for st, cnt in breakdown.items()
            } for sess, breakdown in session_breakdown.items()
        },
        "notifications": notify_after_reconcile(
            sorted(all_sessions_set), results, storage,
        ),
    })


# ──────────────────────────────────────────────────────────────────
# 5. 查看对账结果
# ──────────────────────────────────────────────────────────────────

@app.get("/api/v1/results", summary="查询对账结果列表（支持筛选、排序、分页）")
def list_results(
    status: Optional[str] = Query(default=None, description="normal/absent/non_enrolled/duplicate"),
    session: Optional[str] = Query(default=None),
    mark: Optional[str] = Query(default=None),
    keyword: Optional[str] = Query(default=None, description="姓名/手机号关键词"),
    limit: Optional[int] = Query(default=None, ge=1),
    offset: int = Query(default=0, ge=0),
    sort_by: str = Query(default="id", pattern="^(id|status|session|name)$"),
    sort_order: str = Query(default="asc", pattern="^(asc|desc)$"),
    storage: Storage = Depends(get_storage),
    _: None = Depends(verify_token),
):
    if status and status not in VALID_STATUSES:
        raise HTTPException(400, f"非法 status: {status}")

    sql = "SELECT * FROM reconcile_results"
    conditions = []
    params = []
    if status:
        conditions.append("status = ?")
        params.append(status)
    if session:
        conditions.append("session = ?")
        params.append(session)
    if mark:
        conditions.append("manual_mark = ?")
        params.append(mark)
    if keyword:
        conditions.append("(name LIKE ? OR phone LIKE ?)")
        params.extend([f"%{keyword}%", f"%{keyword}%"])
    if conditions:
        sql += " WHERE " + " AND ".join(conditions)

    valid_cols = {"id": "id", "status": "status", "session": "session", "name": "name"}
    order_col = valid_cols.get(sort_by, "id")
    order_dir = "DESC" if sort_order.lower() == "desc" else "ASC"
    sql += f" ORDER BY {order_col} {order_dir}"

    count_sql = "SELECT COUNT(*) as c FROM reconcile_results"
    if conditions:
        count_sql += " WHERE " + " AND ".join(conditions)
    total = storage.conn.execute(count_sql, params).fetchone()["c"]

    if limit is not None:
        sql += " LIMIT ? OFFSET ?"
        params.extend([limit, offset])

    rows = storage.conn.execute(sql, params).fetchall()
    results = [ReconcileResult(**dict(r)) for r in rows]

    return ApiResponse(data={
        "total": total,
        "count": len(results),
        "offset": offset,
        "limit": limit,
        "records": [result_to_dict(r) for r in results],
    })


@app.get("/api/v1/results/{result_id}", summary="查看单条对账结果详情")
def get_result(
    result_id: int,
    storage: Storage = Depends(get_storage),
    _: None = Depends(verify_token),
):
    r = storage.get_reconcile_result_by_id(result_id)
    if r is None:
        raise HTTPException(404, f"结果 #{result_id} 不存在")
    data = result_to_dict(r)
    if r.enroll_id:
        er = storage.conn.execute("SELECT * FROM enrollments WHERE id=?", (r.enroll_id,)).fetchone()
        if er:
            data["enrollment"] = dict(er)
    if r.signin_id:
        sr = storage.conn.execute("SELECT * FROM signins WHERE id=?", (r.signin_id,)).fetchone()
        if sr:
            data["signin"] = dict(sr)
    return ApiResponse(data=data)


@app.patch("/api/v1/results/{result_id}/mark", summary="人工标记对账结果")
def mark_result(
    result_id: int,
    req: MarkRequest,
    storage: Storage = Depends(get_storage),
    _: None = Depends(verify_token),
):
    if req.mark_text is None and req.notes is None:
        raise HTTPException(400, "mark_text 或 notes 至少提供一项")
    msg = do_mark_result(storage, result_id, req.mark_text, req.notes)
    if msg is None:
        raise HTTPException(404, f"结果 #{result_id} 不存在")
    return ApiResponse(data={"result_id": result_id, "message": msg})


@app.post("/api/v1/results/batch-mark", summary="批量人工标记")
def batch_mark_endpoint(
    req: BatchMarkRequest,
    storage: Storage = Depends(get_storage),
    _: None = Depends(verify_token),
):
    rows = [
        {"result_id": str(i.result_id), "mark_text": i.mark_text,
         "notes": i.notes or ""}
        for i in req.items
    ]
    prev_states, errors = do_batch_mark(storage, rows)
    if errors:
        raise HTTPException(400, {"message": f"{len(errors)} 条错误", "errors": errors})
    return ApiResponse(data={"updated": len(prev_states)})


# ──────────────────────────────────────────────────────────────────
# 6. 统计汇总（场次明细 + 全局汇总）
# ──────────────────────────────────────────────────────────────────

@app.get("/api/v1/stats", summary="统计汇总：全局汇总 + 各场次明细")
def stats_endpoint(
    storage: Storage = Depends(get_storage),
    _: None = Depends(verify_token),
):
    stats = storage.get_stats()
    all_results = storage.get_all_reconcile_results()
    all_sessions = storage.get_reconcile_sessions()

    global_counts: Dict[str, int] = defaultdict(int)
    for r in all_results:
        global_counts[r.status] += 1
    total = len(all_results)
    pct = lambda c: f"{c / total * 100:.2f}%" if total > 0 else "0.00%"

    global_summary = {
        "total": total,
        "status_breakdown": {
            s: {
                "count": global_counts.get(s, 0),
                "label": STATUS_LABELS[s],
                "percentage": pct(global_counts.get(s, 0)),
            }
            for s in ["normal", "absent", "non_enrolled", "duplicate"]
        },
    }

    sessions_detail = [_session_detail(storage, s, all_results) for s in all_sessions]
    sessions_detail.sort(key=lambda x: x["total"], reverse=True)

    attendance_rate = pct(global_counts.get("normal", 0)) if total > 0 else "0.00%"
    absent_rate = pct(global_counts.get("absent", 0)) if total > 0 else "0.00%"

    return ApiResponse(data={
        "global": {
            **global_summary,
            "attendance_rate": attendance_rate,
            "absent_rate": absent_rate,
        },
        "sessions": sessions_detail,
        "sessions_count": len(sessions_detail),
        "datasets": {
            "enrollment_count": stats["enrollment_count"],
            "signin_count": stats["signin_count"],
            "rules_count": stats["rules_count"],
            "import_errors": stats["error_count"],
            "undo_count": stats["undo_count"],
        },
    })


# ──────────────────────────────────────────────────────────────────
# 7. 导出报告（CSV / JSON / XLSX / HTML）
# ──────────────────────────────────────────────────────────────────

@app.get("/api/v1/export", summary="导出对账报告（支持 csv/json/xlsx/html）")
def export_endpoint(
    fmt: str = Query(default="json", pattern="^(csv|json|xlsx|html)$"),
    status: Optional[str] = Query(default=None),
    session: Optional[str] = Query(default=None),
    mark: Optional[str] = Query(default=None),
    keyword: Optional[str] = Query(default=None),
    storage: Storage = Depends(get_storage),
    _: None = Depends(verify_token),
):
    if status and status not in VALID_STATUSES:
        raise HTTPException(400, f"非法 status: {status}")

    has_filter = any([status, session, mark, keyword])
    if has_filter:
        results = storage.query_reconcile_results(
            status=status, session=session, mark=mark, keyword=keyword,
        )
    else:
        results = storage.get_all_reconcile_results()

    if not results:
        raise HTTPException(400, "暂无对账结果可导出")

    status_counts = compute_status_counts(results)

    if fmt == "json":
        data = build_json_data(results, status_counts)
        return ApiResponse(data=data)

    if fmt == "csv":
        csv_content = format_csv_content(results)
        content = csv_content.encode("utf-8-sig")
        return Response(
            content=content,
            media_type="text/csv; charset=utf-8-sig",
            headers={"Content-Disposition": f"attachment; filename=reconcile_{datetime.now().strftime('%Y%m%d_%H%M%S')}.csv"},
        )

    if fmt == "xlsx":
        try:
            headers, rows = build_results_xlsx_rows(results)
        except Exception:
            raise HTTPException(500, "openpyxl 未安装")
        tmp_dir = tempfile.mkdtemp(prefix="signcheck_xlsx_")
        tmp_path = os.path.join(tmp_dir, f"reconcile_{datetime.now().strftime('%Y%m%d_%H%M%S')}.xlsx")
        try:
            write_xlsx_file(tmp_path, headers, rows, sheet_name="对账结果")
        except RuntimeError:
            raise HTTPException(500, "openpyxl 未安装")
        return FileResponse(
            path=tmp_path,
            media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
            filename=os.path.basename(tmp_path),
        )

    if fmt == "html":
        target_sessions = sorted({r.session for r in results})
        global_stats, session_stats, results_by_session = build_session_stats_for_report(
            results, target_sessions
        )
        html = build_html_report(
            title="签到报告 - API 导出",
            global_stats=global_stats,
            session_stats=session_stats,
            results_by_session=results_by_session,
            generated_at=datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        )
        return HTMLResponse(content=html)

    raise HTTPException(400, f"不支持的格式: {fmt}")


# ──────────────────────────────────────────────────────────────────
# 8. 状态 / 撤销 / 错误记录
# ──────────────────────────────────────────────────────────────────

@app.get("/api/v1/status", summary="查看整体数据状态")
def status_endpoint(
    storage: Storage = Depends(get_storage),
    _: None = Depends(verify_token),
):
    stats = storage.get_stats()
    sessions = storage.get_all_sessions()
    enroll_sessions = sorted(storage.get_enrollment_sessions())
    return ApiResponse(data={
        "counts": {
            "enrollments": stats["enrollment_count"],
            "signins": stats["signin_count"],
            "rules": stats["rules_count"],
            "reconcile_results": stats["result_count"],
            "import_errors": stats["error_count"],
            "undo_history": stats["undo_count"],
        },
        "result_status_summary": {
            s: {"count": stats["status_counts"].get(s, 0), "label": STATUS_LABELS[s]}
            for s in ["normal", "absent", "non_enrolled", "duplicate"]
        },
        "sessions": [
            {"name": s.name, "status": s.status, "created_at": s.created_at,
             "closed_at": s.closed_at, "description": s.description}
            for s in sessions
        ],
        "enrollment_sessions": enroll_sessions,
    })


@app.post("/api/v1/undo", summary="撤销上一步操作")
def undo_endpoint(
    storage: Storage = Depends(get_storage),
    _: None = Depends(verify_token),
):
    msg = do_undo(storage)
    if msg is None:
        return ApiResponse(code=1, message="无可撤销操作", data=None)
    return ApiResponse(data={"message": msg})


@app.get("/api/v1/errors", summary="查看导入错误记录")
def errors_endpoint(
    source_type: Optional[str] = Query(default=None, description="enroll/signin/migration_*"),
    limit: int = Query(default=100, ge=1, le=1000),
    storage: Storage = Depends(get_storage),
    _: None = Depends(verify_token),
):
    errors = storage.get_all_import_errors()
    if source_type:
        errors = [e for e in errors if e.source_type == source_type]
    errors = errors[:limit]
    return ApiResponse(data={
        "count": len(errors),
        "records": [
            {
                "id": e.id, "source_type": e.source_type, "source_file": e.source_file,
                "row_number": e.row_number, "error_type": e.error_type,
                "error_message": e.error_message, "raw_data": e.raw_data,
            }
            for e in errors
        ],
    })


@app.get("/api/v1/sessions", summary="场次管理：列出所有场次")
def list_sessions(
    storage: Storage = Depends(get_storage),
    _: None = Depends(verify_token),
):
    sessions = storage.get_all_sessions()
    enroll_sessions = storage.get_enrollment_sessions()
    reconcile_sessions = set(storage.get_reconcile_sessions())
    all_session_names = sorted(enroll_sessions | reconcile_sessions | {s.name for s in sessions})

    records = []
    for name in all_session_names:
        managed = storage.get_session(name)
        enroll_cnt = storage.conn.execute(
            "SELECT COUNT(*) as c FROM enrollments WHERE session=?", (name,)
        ).fetchone()["c"]
        signin_cnt = storage.conn.execute(
            "SELECT COUNT(*) as c FROM signins WHERE session=?", (name,)
        ).fetchone()["c"]
        result_cnt = storage.conn.execute(
            "SELECT COUNT(*) as c FROM reconcile_results WHERE session=?", (name,)
        ).fetchone()["c"]
        records.append({
            "name": name,
            "status": managed.status if managed else "unmanaged",
            "created_at": managed.created_at if managed else None,
            "closed_at": managed.closed_at if managed else None,
            "description": managed.description if managed else None,
            "record_counts": {
                "enrollments": enroll_cnt,
                "signins": signin_cnt,
                "reconcile_results": result_cnt,
            },
        })
    return ApiResponse(data={"count": len(records), "sessions": records})


@app.post("/api/v1/sessions/{name}/close", summary="关闭场次（关闭后不再参与对账）")
def close_session(
    name: str,
    storage: Storage = Depends(get_storage),
    _: None = Depends(verify_token),
):
    storage.create_session(name)
    ok = storage.close_session(name)
    if not ok:
        return ApiResponse(code=1, message=f"场次「{name}」未处于开放状态或不存在")
    return ApiResponse(data={"session": name, "status": "closed"})


# ──────────────────────────────────────────────────────────────────
# Token Info
# ──────────────────────────────────────────────────────────────────

@app.get("/api/v1/token/info", summary="查询当前 token 配置（仅开发用）")
def token_info(_: None = Depends(verify_token)):
    return ApiResponse(data={
        "token_env_var": "SIGNCHECK_API_TOKEN",
        "header": "Authorization: Bearer <token>",
        "token_length": len(DEFAULT_API_TOKEN),
        "note": "生产环境请通过 SIGNCHECK_API_TOKEN 环境变量设置强随机 token",
    })


# ──────────────────────────────────────────────────────────────────
# 9. 通知规则管理
# ──────────────────────────────────────────────────────────────────

@app.get("/api/v1/notify/rules", summary="列出所有通知规则")
def list_notify_rules(
    storage: Storage = Depends(get_storage),
    _: None = Depends(verify_token),
):
    rules = storage.get_all_notification_rules()
    return ApiResponse(data={
        "count": len(rules),
        "rules": [
            {
                "id": r.id,
                "session": r.session,
                "channel": r.channel,
                "target": r.target,
                "enabled": bool(r.enabled),
                "absent_threshold": r.absent_threshold,
                "extra_config": r.extra_config,
                "created_at": r.created_at,
                "updated_at": r.updated_at,
            }
            for r in rules
        ],
    })


@app.post("/api/v1/notify/rules", summary="创建通知规则")
def create_notify_rule(
    req: NotificationRuleCreate,
    storage: Storage = Depends(get_storage),
    _: None = Depends(verify_token),
):
    rule = NotificationRule(
        session=req.session,
        channel=req.channel,
        target=req.target,
        enabled=req.enabled,
        absent_threshold=req.absent_threshold,
        extra_config=req.extra_config,
    )
    rule_id = storage.add_notification_rule(rule)
    created = storage.get_notification_rule(rule_id)
    return ApiResponse(data={
        "id": rule_id,
        "rule": {
            "id": created.id,
            "session": created.session,
            "channel": created.channel,
            "target": created.target,
            "enabled": bool(created.enabled),
            "absent_threshold": created.absent_threshold,
            "extra_config": created.extra_config,
            "created_at": created.created_at,
            "updated_at": created.updated_at,
        },
    })


@app.put("/api/v1/notify/rules/{rule_id}", summary="更新通知规则")
def update_notify_rule(
    rule_id: int,
    req: NotificationRuleUpdate,
    storage: Storage = Depends(get_storage),
    _: None = Depends(verify_token),
):
    existing = storage.get_notification_rule(rule_id)
    if existing is None:
        raise HTTPException(404, f"通知规则 #{rule_id} 不存在")
    updates = {k: v for k, v in req.model_dump().items() if v is not None}
    if not updates:
        raise HTTPException(400, "至少提供一项更新字段")
    ok = storage.update_notification_rule(rule_id, **updates)
    if not ok:
        raise HTTPException(500, "更新失败")
    updated = storage.get_notification_rule(rule_id)
    return ApiResponse(data={
        "id": rule_id,
        "rule": {
            "id": updated.id,
            "session": updated.session,
            "channel": updated.channel,
            "target": updated.target,
            "enabled": bool(updated.enabled),
            "absent_threshold": updated.absent_threshold,
            "extra_config": updated.extra_config,
            "created_at": updated.created_at,
            "updated_at": updated.updated_at,
        },
    })


@app.delete("/api/v1/notify/rules/{rule_id}", summary="删除通知规则")
def delete_notify_rule(
    rule_id: int,
    storage: Storage = Depends(get_storage),
    _: None = Depends(verify_token),
):
    existing = storage.get_notification_rule(rule_id)
    if existing is None:
        raise HTTPException(404, f"通知规则 #{rule_id} 不存在")
    storage.delete_notification_rule(rule_id)
    return ApiResponse(data={"deleted": rule_id})


# ──────────────────────────────────────────────────────────────────
# 10. 通知日志查询
# ──────────────────────────────────────────────────────────────────

@app.get("/api/v1/notify/logs", summary="查询通知发送日志")
def list_notify_logs(
    session: Optional[str] = Query(default=None),
    limit: int = Query(default=100, ge=1, le=1000),
    offset: int = Query(default=0, ge=0),
    storage: Storage = Depends(get_storage),
    _: None = Depends(verify_token),
):
    if session:
        logs = storage.get_notification_logs_by_session(session, limit=limit)
    else:
        logs = storage.get_notification_logs(limit=limit, offset=offset)
    return ApiResponse(data={
        "count": len(logs),
        "logs": [
            {
                "id": l.id,
                "channel": l.channel,
                "target": l.target,
                "session": l.session,
                "status": l.status,
                "message": l.message,
                "retries": l.retries,
                "created_at": l.created_at,
            }
            for l in logs
        ],
    })


# ──────────────────────────────────────────────────────────────────
# Start Server
# ──────────────────────────────────────────────────────────────────

def run():
    """入口：启动 uvicorn 服务器。"""
    import uvicorn
    print(f"=" * 60)
    print(f"  SignCheck Web API 启动中...")
    print(f"  服务地址: http://{API_HOST}:{API_PORT}")
    print(f"  API 文档: http://{API_HOST}:{API_PORT}/docs")
    print(f"  ReDoc:    http://{API_HOST}:{API_PORT}/redoc")
    print(f"  探活:     http://{API_HOST}:{API_PORT}/health")
    print(f"  Token:    {DEFAULT_API_TOKEN[:4]}{'*' * max(0, len(DEFAULT_API_TOKEN) - 8)}"
          f" (通过 SIGNCHECK_API_TOKEN 环境变量修改)")
    print(f"  日志目录: {LOG_DIR}")
    print(f"=" * 60)
    uvicorn.run(app, host=API_HOST, port=API_PORT, log_level="info")


if __name__ == "__main__":
    run()
