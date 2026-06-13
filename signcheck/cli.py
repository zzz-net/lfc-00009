import csv
import json
import os
import sys
import click
from typing import List, Optional

from .models import (
    EnrollmentRecord,
    SigninRecord,
    MatchRule,
    FieldMapping,
    ImportErrorRecord,
)
from .storage import Storage
from .reconcile import reconcile, undo, mark_result


def _get_storage() -> Storage:
    return Storage()


def _read_csv(file_path: str) -> tuple:
    abs_path = os.path.abspath(file_path)
    if not os.path.exists(abs_path):
        click.echo(f"错误：文件不存在 {abs_path}", err=True)
        sys.exit(1)
    with open(abs_path, "r", encoding="utf-8-sig") as f:
        reader = csv.DictReader(f)
        rows = list(reader)
    return rows, abs_path


def _apply_field_mapping(rows: list, mapping: dict) -> list:
    if not mapping:
        return rows
    mapped = []
    for row in rows:
        new_row = {}
        for field_name, csv_col in mapping.items():
            new_row[field_name] = row.get(csv_col, "").strip()
        mapped.append(new_row)
    return mapped


@click.group()
@click.version_option(version="1.0.0")
def main():
    """线下培训签到对账 CLI"""
    pass


@main.command("import-enroll")
@click.argument("csv_file")
def import_enroll(csv_file: str):
    """导入报名 CSV 文件"""
    storage = _get_storage()
    rows, abs_path = _read_csv(csv_file)
    field_mapping = storage.get_field_mapping()
    mapping = field_mapping.enroll if field_mapping.enroll else {
        "name": "姓名",
        "phone": "手机号",
        "session": "场次",
    }

    mapped_rows = _apply_field_mapping(rows, mapping)

    valid_records: List[EnrollmentRecord] = []
    errors: List[ImportErrorRecord] = []
    existing_sessions = storage.get_enrollment_sessions()

    for idx, row in enumerate(mapped_rows):
        source_row = idx + 2
        name = row.get("name", "").strip()
        phone = row.get("phone", "").strip() if row.get("phone") else ""
        session = row.get("session", "").strip()

        row_errors = []
        if not phone:
            row_errors.append(("missing_phone", f"第 {source_row} 行：手机号缺失"))
        if not session:
            row_errors.append(("missing_session", f"第 {source_row} 行：场次不能为空"))

        if row_errors:
            for error_type, error_msg in row_errors:
                raw = json.dumps({k: v for k, v in row.items() if v}, ensure_ascii=False)
                errors.append(ImportErrorRecord(
                    source_type="enroll",
                    source_file=os.path.basename(abs_path),
                    row_number=source_row,
                    error_type=error_type,
                    error_message=error_msg,
                    raw_data=raw,
                ))
            continue

        valid_records.append(EnrollmentRecord(
            name=name,
            phone=phone,
            session=session,
            source_file=os.path.basename(abs_path),
            source_row=source_row,
        ))

    if valid_records:
        new_ids, existing_ids = storage.add_enrollments(valid_records)
        if new_ids:
            undo_data = json.dumps({"action": "import_enroll", "ids": new_ids})
            storage.add_undo_action("import_enroll", undo_data)
    else:
        new_ids, existing_ids = [], []

    if errors:
        storage.add_import_errors(errors)

    click.echo(f"[OK] 新增 {len(new_ids)} 条报名记录，跳过 {len(existing_ids)} 条已存在记录")
    if errors:
        for e in errors:
            click.echo(f"[ERR] {e.error_message}")
        click.echo(f"共 {len(errors)} 条错误，已跳过")

    storage.close()


@main.command("import-signin")
@click.argument("csv_file")
def import_signin(csv_file: str):
    """导入扫码签到 CSV 文件"""
    storage = _get_storage()
    rows, abs_path = _read_csv(csv_file)
    field_mapping = storage.get_field_mapping()
    mapping = field_mapping.signin if field_mapping.signin else {
        "name": "姓名",
        "phone": "手机号",
        "session": "场次",
        "scan_time": "扫码时间",
    }

    mapped_rows = _apply_field_mapping(rows, mapping)

    valid_records: List[SigninRecord] = []
    errors: List[ImportErrorRecord] = []
    existing_sessions = storage.get_enrollment_sessions()

    for idx, row in enumerate(mapped_rows):
        source_row = idx + 2
        name = row.get("name", "").strip()
        phone = row.get("phone", "").strip() if row.get("phone") else ""
        session = row.get("session", "").strip()
        scan_time = row.get("scan_time", "").strip() if row.get("scan_time") else ""

        row_errors = []
        if not phone:
            row_errors.append(("missing_phone", f"第 {source_row} 行：手机号缺失"))
        if existing_sessions and session and session not in existing_sessions:
            row_errors.append(("invalid_session", f"第 {source_row} 行：场次「{session}」不存在"))

        if row_errors:
            for error_type, error_msg in row_errors:
                raw = json.dumps({k: v for k, v in row.items() if v}, ensure_ascii=False)
                errors.append(ImportErrorRecord(
                    source_type="signin",
                    source_file=os.path.basename(abs_path),
                    row_number=source_row,
                    error_type=error_type,
                    error_message=error_msg,
                    raw_data=raw,
                ))
            continue

        valid_records.append(SigninRecord(
            name=name,
            phone=phone,
            session=session,
            scan_time=scan_time,
            source_file=os.path.basename(abs_path),
            source_row=source_row,
        ))

    if valid_records:
        new_ids, existing_ids = storage.add_signins(valid_records)
        if new_ids:
            undo_data = json.dumps({"action": "import_signin", "ids": new_ids})
            storage.add_undo_action("import_signin", undo_data)
    else:
        new_ids, existing_ids = [], []

    if errors:
        storage.add_import_errors(errors)

    click.echo(f"[OK] 新增 {len(new_ids)} 条签到记录，跳过 {len(existing_ids)} 条已存在记录")
    if errors:
        for e in errors:
            click.echo(f"[ERR] {e.error_message}")
        click.echo(f"共 {len(errors)} 条错误，已跳过")

    storage.close()


@main.command("import-rules")
@click.argument("json_file")
def import_rules(json_file: str):
    """导入匹配规则 JSON 文件"""
    storage = _get_storage()
    abs_path = os.path.abspath(json_file)
    if not os.path.exists(abs_path):
        click.echo(f"错误：文件不存在 {abs_path}", err=True)
        sys.exit(1)

    with open(abs_path, "r", encoding="utf-8-sig") as f:
        data = json.load(f)

    prev_rules = storage.get_all_rules()
    prev_mapping = storage.get_field_mapping()

    storage.clear_rules()

    match_rules_data = data.get("match_rules", [])
    rules: List[MatchRule] = []
    for r in match_rules_data:
        rules.append(MatchRule(
            field_name=r["field"],
            match_type=r.get("match_type", "exact"),
            threshold=r.get("threshold"),
            priority=r.get("priority", 0),
        ))

    if rules:
        storage.add_rules(rules)

    field_mapping_data = data.get("field_mapping", None)
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

    click.echo(f"[OK] 成功导入 {len(rules)} 条匹配规则")
    if field_mapping_data:
        click.echo(f"[OK] 成功导入字段映射配置")

    storage.close()


@main.command("reconcile")
def do_reconcile():
    """执行对账"""
    storage = _get_storage()
    enrollments = storage.get_all_enrollments()
    signins = storage.get_all_signins()

    if not enrollments:
        click.echo("错误：尚未导入报名数据，请先执行 import-enroll", err=True)
        storage.close()
        sys.exit(1)
    if not signins:
        click.echo("错误：尚未导入签到数据，请先执行 import-signin", err=True)
        storage.close()
        sys.exit(1)

    results = reconcile(storage)

    status_counts = {}
    for r in results:
        status_counts[r.status] = status_counts.get(r.status, 0) + 1

    status_labels = {
        "normal": "正常签到",
        "absent": "缺席",
        "non_enrolled": "非报名人员",
        "duplicate": "重复扫码",
    }

    click.echo("对账完成：")
    for status in ["normal", "absent", "non_enrolled", "duplicate"]:
        cnt = status_counts.get(status, 0)
        label = status_labels.get(status, status)
        click.echo(f"  {label}: {cnt}")

    storage.close()


@main.command("mark")
@click.argument("result_id", type=int)
@click.option("--mark-text", "-m", default=None, help="标记内容")
@click.option("--notes", "-n", default=None, help="备注")
def do_mark(result_id: int, mark_text: Optional[str], notes: Optional[str]):
    """人工标记对账结果"""
    storage = _get_storage()

    if mark_text is None and notes is None:
        click.echo("错误：请至少提供 --mark-text 或 --notes", err=True)
        storage.close()
        sys.exit(1)

    msg = mark_result(storage, result_id, mark_text, notes)
    if msg is None:
        click.echo(f"错误：未找到结果 #{result_id}", err=True)
        storage.close()
        sys.exit(1)

    click.echo(f"[OK] {msg}")
    storage.close()


@main.command("undo")
def do_undo():
    """撤销上一步操作"""
    storage = _get_storage()
    msg = undo(storage)
    if msg is None:
        click.echo("没有可撤销的操作")
    else:
        click.echo(f"[OK] 已撤销上一步操作：{msg}")
    storage.close()


@main.command("export")
@click.option("--output", "-o", default="reconcile_result.csv", help="输出文件路径")
def do_export(output: str):
    """导出对账结果为 CSV"""
    storage = _get_storage()
    results = storage.get_all_reconcile_results()

    if not results:
        click.echo("错误：暂无对账结果，请先执行 reconcile", err=True)
        storage.close()
        sys.exit(1)

    status_labels = {
        "normal": "正常签到",
        "absent": "缺席",
        "non_enrolled": "非报名人员",
        "duplicate": "重复扫码",
    }

    abs_output = os.path.abspath(output)
    with open(abs_output, "w", encoding="utf-8-sig", newline="") as f:
        writer = csv.writer(f)
        writer.writerow(["ID", "姓名", "手机号", "场次", "状态", "标记", "备注"])
        for r in results:
            label = status_labels.get(r.status, r.status)
            writer.writerow([
                r.id,
                r.name,
                r.phone or "",
                r.session,
                label,
                r.manual_mark or "",
                r.notes or "",
            ])

    status_counts = {}
    for r in results:
        status_counts[r.status] = status_counts.get(r.status, 0) + 1

    click.echo(f"[OK] 已导出 {len(results)} 条对账结果到 {abs_output}")
    click.echo("汇总：")
    for status in ["normal", "absent", "non_enrolled", "duplicate"]:
        cnt = status_counts.get(status, 0)
        label = status_labels.get(status, status)
        click.echo(f"  {label}: {cnt}")

    storage.close()


@main.command("status")
def do_status():
    """查看当前状态"""
    storage = _get_storage()
    stats = storage.get_stats()

    click.echo(f"报名记录: {stats['enrollment_count']}")
    click.echo(f"签到记录: {stats['signin_count']}")
    click.echo(f"匹配规则: {stats['rules_count']} 条")
    click.echo(f"对账结果: {stats['result_count']} 条")

    status_labels = {
        "normal": "正常签到",
        "absent": "缺席",
        "non_enrolled": "非报名人员",
        "duplicate": "重复扫码",
    }

    if stats["status_counts"]:
        for status in ["normal", "absent", "non_enrolled", "duplicate"]:
            cnt = stats["status_counts"].get(status, 0)
            if cnt:
                label = status_labels.get(status, status)
                click.echo(f"  - {label}: {cnt}")

    click.echo(f"撤销历史: {stats['undo_count']} 步")
    click.echo(f"导入错误: {stats['error_count']} 条")

    storage.close()


@main.command("errors")
def show_errors():
    """查看导入错误"""
    storage = _get_storage()
    errors = storage.get_all_import_errors()

    if not errors:
        click.echo("暂无导入错误记录")
        storage.close()
        return

    for e in errors:
        click.echo(f"[{e.source_type}] {e.error_message}")
        if e.raw_data:
            click.echo(f"  原始数据: {e.raw_data}")

    click.echo(f"\n共 {len(errors)} 条错误")
    storage.close()


@main.command("reset")
@click.confirmation_option(prompt="确定要清空所有数据吗？")
def do_reset():
    """清空所有数据（需确认）"""
    db_dir = os.path.join(os.getcwd(), ".signcheck")
    db_path = os.path.join(db_dir, "signcheck.db")
    if os.path.exists(db_path):
        os.remove(db_path)
        click.echo("[OK] 已清空所有数据")
    else:
        click.echo("当前无数据")


if __name__ == "__main__":
    main()
