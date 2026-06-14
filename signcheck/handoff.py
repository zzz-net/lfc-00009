import csv
import hashlib
import json
import os
import tempfile
import shutil
import zipfile
from collections import defaultdict
from datetime import datetime
from typing import Dict, List, Optional, Tuple, Any

from .models import HandoffPackage, HandoffExportLog, HandoffAuditLog
from .storage import Storage


STATUS_LABELS = {
    "normal": "正常签到",
    "absent": "缺席",
    "non_enrolled": "非报名人员",
    "duplicate": "重复扫码",
}


def _generate_package_id(session: str) -> str:
    ts = datetime.now().strftime("%Y%m%d%H%M%S")
    short_hash = hashlib.sha256(f"{session}{ts}".encode()).hexdigest()[:8]
    return f"HO-{session}-{ts}-{short_hash}"


def _compute_data_hash(data: str) -> str:
    return hashlib.sha256(data.encode("utf-8")).hexdigest()


def create_handoff(
    storage: Storage,
    session: str,
    operator: str,
) -> Tuple[HandoffPackage, str]:
    if not operator or not operator.strip():
        raise ValueError("操作者不能为空，禁止生成交接包")

    results = storage.query_reconcile_results(session=session)
    if not results:
        raise ValueError(f"场次「{session}」暂无对账结果，无法生成交接包")

    enrollments = [e for e in storage.get_all_enrollments() if e.session == session]
    signins = [s for s in storage.get_all_signins() if s.session == session]

    status_counts: Dict[str, int] = defaultdict(int)
    manual_mark_count = 0
    for r in results:
        status_counts[r.status] += 1
        if r.manual_mark:
            manual_mark_count += 1

    status_summary = json.dumps(
        {STATUS_LABELS.get(k, k): v for k, v in status_counts.items()},
        ensure_ascii=False,
    )

    package_id = _generate_package_id(session)
    generated_at = datetime.now().isoformat()

    package_data = _build_package_json(
        package_id=package_id,
        session=session,
        operator=operator,
        generated_at=generated_at,
        enrollments=enrollments,
        signins=signins,
        results=results,
        status_summary=status_summary,
        manual_mark_count=manual_mark_count,
    )

    data_hash = _compute_data_hash(package_data)

    pkg = HandoffPackage(
        package_id=package_id,
        session=session,
        operator=operator,
        enroll_count=len(enrollments),
        signin_count=len(signins),
        result_count=len(results),
        status_summary=status_summary,
        manual_mark_count=manual_mark_count,
        generated_at=generated_at,
        data_hash=data_hash,
    )

    storage.add_handoff_package(pkg)

    storage.add_handoff_audit_log(HandoffAuditLog(
        operator=operator,
        action="handoff_create",
        target=package_id,
        result="success",
        detail=f"场次={session}, 记录数={len(results)}",
    ))

    return pkg, package_data


def _build_package_json(
    package_id: str,
    session: str,
    operator: str,
    generated_at: str,
    enrollments: list,
    signins: list,
    results: list,
    status_summary: str,
    manual_mark_count: int,
) -> str:
    enroll_data = []
    for e in enrollments:
        enroll_data.append({
            "id": e.id,
            "name": e.name,
            "phone": e.phone,
            "session": e.session,
            "source_file": e.source_file,
            "source_row": e.source_row,
        })

    signin_data = []
    for s in signins:
        signin_data.append({
            "id": s.id,
            "name": s.name,
            "phone": s.phone,
            "session": s.session,
            "scan_time": s.scan_time,
            "source_file": s.source_file,
            "source_row": s.source_row,
        })

    result_data = []
    for r in results:
        result_data.append({
            "id": r.id,
            "enroll_id": r.enroll_id,
            "signin_id": r.signin_id,
            "name": r.name,
            "phone": r.phone,
            "session": r.session,
            "status": r.status,
            "status_label": STATUS_LABELS.get(r.status, r.status),
            "manual_mark": r.manual_mark,
            "notes": r.notes,
        })

    data = {
        "manifest": {
            "package_id": package_id,
            "session": session,
            "operator": operator,
            "generated_at": generated_at,
            "version": "1.0",
        },
        "summary": {
            "enroll_count": len(enroll_data),
            "signin_count": len(signin_data),
            "result_count": len(result_data),
            "manual_mark_count": manual_mark_count,
            "status_summary": json.loads(status_summary),
        },
        "enrollments": enroll_data,
        "signins": signin_data,
        "reconcile_results": result_data,
    }

    return json.dumps(data, ensure_ascii=False, indent=2)


def export_handoff(
    storage: Storage,
    package_id: str,
    output_path: str,
    operator: str,
) -> str:
    if not operator or not operator.strip():
        raise ValueError("操作者不能为空，禁止导出交接包")

    pkg = storage.get_handoff_package(package_id)
    if pkg is None:
        raise ValueError(f"交接包「{package_id}」不存在")

    results = storage.query_reconcile_results(session=pkg.session)
    enrollments = [e for e in storage.get_all_enrollments() if e.session == pkg.session]
    signins = [s for s in storage.get_all_signins() if s.session == pkg.session]

    abs_output = os.path.abspath(output_path)
    os.makedirs(os.path.dirname(abs_output), exist_ok=True)

    tmp_dir = tempfile.mkdtemp(prefix="signcheck_handoff_")
    try:
        package_data = _build_package_json(
            package_id=pkg.package_id,
            session=pkg.session,
            operator=pkg.operator,
            generated_at=pkg.generated_at,
            enrollments=enrollments,
            signins=signins,
            results=results,
            status_summary=pkg.status_summary,
            manual_mark_count=pkg.manual_mark_count,
        )

        manifest_file = os.path.join(tmp_dir, "manifest.json")
        with open(manifest_file, "w", encoding="utf-8") as f:
            f.write(package_data)

        _write_csv_detail(tmp_dir, "enrollments.csv", enrollments, "enroll")
        _write_csv_detail(tmp_dir, "signins.csv", signins, "signin")
        _write_csv_detail(tmp_dir, "reconcile_results.csv", results, "result")

        file_hashes = {}
        for fname in ["manifest.json", "enrollments.csv", "signins.csv", "reconcile_results.csv"]:
            fpath = os.path.join(tmp_dir, fname)
            if os.path.exists(fpath):
                with open(fpath, "rb") as f:
                    file_hashes[fname] = hashlib.sha256(f.read()).hexdigest()

        hash_file = os.path.join(tmp_dir, "checksums.json")
        with open(hash_file, "w", encoding="utf-8") as f:
            json.dump(file_hashes, f, indent=2, ensure_ascii=False)

        with zipfile.ZipFile(abs_output, "w", zipfile.ZIP_DEFLATED) as zf:
            for fname in ["manifest.json", "enrollments.csv", "signins.csv", "reconcile_results.csv", "checksums.json"]:
                fpath = os.path.join(tmp_dir, fname)
                if os.path.exists(fpath):
                    zf.write(fpath, fname)
    finally:
        shutil.rmtree(tmp_dir, ignore_errors=True)

    storage.add_handoff_export_log(HandoffExportLog(
        package_id=package_id,
        export_path=abs_output,
        operator=operator,
    ))

    storage.add_handoff_audit_log(HandoffAuditLog(
        operator=operator,
        action="handoff_export",
        target=package_id,
        result="success",
        detail=f"导出路径={abs_output}",
    ))

    return abs_output


def _write_csv_detail(tmp_dir: str, filename: str, records: list, record_type: str):
    filepath = os.path.join(tmp_dir, filename)
    with open(filepath, "w", encoding="utf-8-sig", newline="") as f:
        writer = csv.writer(f)
        if record_type == "enroll":
            writer.writerow(["ID", "姓名", "手机号", "场次", "来源文件", "来源行号"])
            for e in records:
                writer.writerow([e.id, e.name, e.phone or "", e.session, e.source_file or "", e.source_row or ""])
        elif record_type == "signin":
            writer.writerow(["ID", "姓名", "手机号", "场次", "扫码时间", "来源文件", "来源行号"])
            for s in records:
                writer.writerow([s.id, s.name, s.phone or "", s.session, s.scan_time or "", s.source_file or "", s.source_row or ""])
        elif record_type == "result":
            writer.writerow(["ID", "报名ID", "签到ID", "姓名", "手机号", "场次", "状态", "状态标签", "人工标记", "备注"])
            for r in records:
                writer.writerow([
                    r.id, r.enroll_id or "", r.signin_id or "",
                    r.name, r.phone or "", r.session,
                    r.status, STATUS_LABELS.get(r.status, r.status),
                    r.manual_mark or "", r.notes or "",
                ])


def verify_handoff(zip_path: str) -> Tuple[bool, List[str]]:
    abs_path = os.path.abspath(zip_path)
    if not os.path.exists(abs_path):
        raise ValueError(f"文件不存在：{abs_path}")
    if not zipfile.is_zipfile(abs_path):
        raise ValueError(f"不是有效的 zip 文件：{abs_path}")

    errors: List[str] = []

    tmp_dir = tempfile.mkdtemp(prefix="signcheck_handoff_verify_")
    try:
        with zipfile.ZipFile(abs_path, "r") as zf:
            zf.extractall(tmp_dir)

        checksums_file = os.path.join(tmp_dir, "checksums.json")
        if not os.path.exists(checksums_file):
            errors.append("校验失败：交接包缺少 checksums.json 文件")
            return False, errors

        with open(checksums_file, "r", encoding="utf-8") as f:
            expected_hashes = json.load(f)

        for fname, expected_hash in expected_hashes.items():
            fpath = os.path.join(tmp_dir, fname)
            if not os.path.exists(fpath):
                errors.append(f"校验失败：文件「{fname}」在包中缺失")
                continue
            with open(fpath, "rb") as f:
                actual_hash = hashlib.sha256(f.read()).hexdigest()
            if actual_hash != expected_hash:
                errors.append(f"校验失败：文件「{fname}」被篡改（期望 {expected_hash[:16]}...，实际 {actual_hash[:16]}...）")

        manifest_file = os.path.join(tmp_dir, "manifest.json")
        if not os.path.exists(manifest_file):
            errors.append("校验失败：交接包缺少 manifest.json 文件")
            return False, errors

        with open(manifest_file, "r", encoding="utf-8") as f:
            manifest_data = json.load(f)

        required_fields = ["package_id", "session", "operator", "generated_at"]
        for field in required_fields:
            if field not in manifest_data.get("manifest", {}):
                errors.append(f"校验失败：manifest 缺少必填字段「{field}」")
    finally:
        shutil.rmtree(tmp_dir, ignore_errors=True)

    return len(errors) == 0, errors


def import_handoff(
    storage: Storage,
    zip_path: str,
    operator: str,
    overwrite: bool = False,
) -> Dict[str, Any]:
    if not operator or not operator.strip():
        raise ValueError("操作者不能为空，禁止导入交接包")

    abs_path = os.path.abspath(zip_path)
    if not os.path.exists(abs_path):
        raise ValueError(f"文件不存在：{abs_path}")

    valid, verify_errors = verify_handoff(abs_path)
    if not valid:
        storage.add_handoff_audit_log(HandoffAuditLog(
            operator=operator,
            action="handoff_import",
            target=abs_path,
            result="failed",
            detail="校验失败：" + "; ".join(verify_errors),
        ))
        raise ValueError("交接包校验失败：" + "; ".join(verify_errors))

    tmp_dir = tempfile.mkdtemp(prefix="signcheck_handoff_import_")
    try:
        with zipfile.ZipFile(abs_path, "r") as zf:
            zf.extractall(tmp_dir)

        with open(os.path.join(tmp_dir, "manifest.json"), "r", encoding="utf-8") as f:
            manifest_data = json.load(f)

        manifest = manifest_data.get("manifest", {})
        package_id = manifest.get("package_id", "")
        session = manifest.get("session", "")

        if not package_id or not session:
            raise ValueError("交接包 manifest 缺少 package_id 或 session")

        existing = storage.get_handoff_package(package_id)
        session_conflict = storage.get_handoff_packages_by_session(session)

        if existing is not None:
            if not overwrite:
                storage.add_handoff_audit_log(HandoffAuditLog(
                    operator=operator,
                    action="handoff_import",
                    target=package_id,
                    result="rejected",
                    detail=f"包编号冲突：{package_id} 已存在，使用 --overwrite 允许覆盖",
                ))
                raise ValueError(f"包编号冲突：「{package_id}」已存在，使用 --overwrite 允许覆盖")

            storage.delete_handoff_package(package_id)
            storage.add_handoff_audit_log(HandoffAuditLog(
                operator=operator,
                action="handoff_import_overwrite",
                target=package_id,
                result="success",
                detail=f"覆盖已存在的包编号：{package_id}",
            ))

        if not existing and session_conflict:
            existing_session_pkg = session_conflict[0]
            if not overwrite:
                storage.add_handoff_audit_log(HandoffAuditLog(
                    operator=operator,
                    action="handoff_import",
                    target=package_id,
                    result="rejected",
                    detail=f"场次名冲突：场次「{session}」已有交接包 {existing_session_pkg.package_id}，使用 --overwrite 允许覆盖",
                ))
                raise ValueError(
                    f"场次名冲突：场次「{session}」已有交接包 {existing_session_pkg.package_id}，使用 --overwrite 允许覆盖"
                )

            storage.delete_handoff_package(existing_session_pkg.package_id)
            storage.add_handoff_audit_log(HandoffAuditLog(
                operator=operator,
                action="handoff_import_overwrite",
                target=existing_session_pkg.package_id,
                result="success",
                detail=f"覆盖场次「{session}」已有交接包",
            ))

        summary = manifest_data.get("summary", {})
        pkg = HandoffPackage(
            package_id=package_id,
            session=session,
            operator=manifest.get("operator", operator),
            enroll_count=summary.get("enroll_count", 0),
            signin_count=summary.get("signin_count", 0),
            result_count=summary.get("result_count", 0),
            status_summary=json.dumps(summary.get("status_summary", {}), ensure_ascii=False),
            manual_mark_count=summary.get("manual_mark_count", 0),
            generated_at=manifest.get("generated_at", ""),
            data_hash=_compute_data_hash(json.dumps(manifest_data, ensure_ascii=False)),
        )

        storage.add_handoff_package(pkg)

        storage.add_handoff_audit_log(HandoffAuditLog(
            operator=operator,
            action="handoff_import",
            target=package_id,
            result="success",
            detail=f"场次={session}, 记录数={summary.get('result_count', 0)}",
        ))

        return {
            "package_id": package_id,
            "session": session,
            "enroll_count": summary.get("enroll_count", 0),
            "signin_count": summary.get("signin_count", 0),
            "result_count": summary.get("result_count", 0),
            "manual_mark_count": summary.get("manual_mark_count", 0),
            "overwritten": existing is not None or bool(session_conflict),
        }
    finally:
        shutil.rmtree(tmp_dir, ignore_errors=True)
