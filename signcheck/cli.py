import csv
import json
import os
import sys
import click
from typing import List, Optional, Dict, Any
from collections import defaultdict

from .models import (
    EnrollmentRecord,
    SigninRecord,
    MatchRule,
    FieldMapping,
    ImportErrorRecord,
)
from .storage import Storage
from .reconcile import reconcile, undo, mark_result, batch_mark
from . import config as config_module


VALID_STATUSES = {"normal", "absent", "non_enrolled", "duplicate"}

STATUS_LABELS = {
    "normal": "正常签到",
    "absent": "缺席",
    "non_enrolled": "非报名人员",
    "duplicate": "重复扫码",
}


def _resolve_filters(
    status, session, mark, keyword, limit, sort_by, sort_order, view, save_view, overwrite, storage
) -> Dict[str, Any]:
    filters: Dict[str, Any] = {}
    if view:
        saved = storage.get_view(view)
        if saved is None:
            click.echo(f"错误：视图「{view}」不存在", err=True)
            storage.close()
            sys.exit(1)
        view_filters = json.loads(saved["filters"])
        filters.update(view_filters)
    cli_overrides: Dict[str, Any] = {}
    if status is not None:
        cli_overrides["status"] = status
    if session is not None:
        cli_overrides["session"] = session
    if mark is not None:
        cli_overrides["mark"] = mark
    if keyword is not None:
        cli_overrides["keyword"] = keyword
    if limit is not None:
        cli_overrides["limit"] = limit
    if sort_by is not None:
        cli_overrides["sort_by"] = sort_by
    if sort_order is not None:
        cli_overrides["sort_order"] = sort_order
    filters.update(cli_overrides)
    if "sort_by" not in filters or filters["sort_by"] is None:
        filters["sort_by"] = config_module.get_config("sort_by")
    if "sort_order" not in filters or filters["sort_order"] is None:
        filters["sort_order"] = config_module.get_config("sort_order")
    if save_view:
        filters_to_save = {k: v for k, v in filters.items() if v is not None}
        saved_json = json.dumps(filters_to_save, ensure_ascii=False)
        ok = storage.save_view(save_view, saved_json, overwrite=overwrite)
        if not ok:
            click.echo(f"错误：视图「{save_view}」已存在，使用 --overwrite 覆盖", err=True)
            storage.close()
            sys.exit(1)
        click.echo(f"[OK] 视图「{save_view}」已保存")
    return filters


def _validate_status(status: Optional[str]) -> Optional[str]:
    if status is None:
        return None
    if status not in VALID_STATUSES:
        click.echo(f"错误：非法状态「{status}」，可选值：{', '.join(sorted(VALID_STATUSES))}", err=True)
        sys.exit(1)
    return status


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

    click.echo("对账完成：")
    for status in ["normal", "absent", "non_enrolled", "duplicate"]:
        cnt = status_counts.get(status, 0)
        label = STATUS_LABELS.get(status, status)
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


def _common_filter_options(f):
    f = click.option("--status", "-s", default=None, help="按状态筛选 (normal/absent/non_enrolled/duplicate)")(f)
    f = click.option("--session", default=None, help="按场次筛选")(f)
    f = click.option("--mark", default=None, help="按人工标记筛选")(f)
    f = click.option("--keyword", "-k", default=None, help="按姓名或手机号关键词筛选")(f)
    f = click.option("--limit", "-n", type=int, default=None, help="限制返回条数")(f)
    f = click.option("--sort-by", type=click.Choice(["id", "status"]), default=None, help="排序字段（默认 id）")(f)
    f = click.option("--sort-order", type=click.Choice(["asc", "desc"]), default=None, help="排序方向（默认 asc）")(f)
    f = click.option("--view", "-v", default=None, help="使用已保存的视图名加载筛选条件")(f)
    f = click.option("--save-view", default=None, help="将当前筛选条件保存为命名视图")(f)
    f = click.option("--overwrite", is_flag=True, default=False, help="覆盖同名视图")(f)
    return f


@main.command("list")
@_common_filter_options
def do_list(status, session, mark, keyword, limit, sort_by, sort_order, view, save_view, overwrite):
    """查看对账结果列表（支持筛选、排序、视图）"""
    storage = _get_storage()
    if status is not None:
        status = _validate_status(status)
    filters = _resolve_filters(
        status, session, mark, keyword, limit, sort_by, sort_order, view, save_view, overwrite, storage
    )
    results = storage.query_reconcile_results(
        status=filters.get("status"),
        session=filters.get("session"),
        mark=filters.get("mark"),
        keyword=filters.get("keyword"),
        limit=filters.get("limit"),
        sort_by=filters.get("sort_by", "id"),
        sort_order=filters.get("sort_order", "asc"),
    )

    if not results:
        click.echo("暂无匹配的对账结果")
        storage.close()
        return

    col_widths = {
        "id": max(4, max(len(str(r.id)) for r in results)),
        "name": max(4, max(len(r.name) for r in results)),
        "phone": max(4, max(len(r.phone or "") for r in results)),
        "session": max(4, max(len(r.session) for r in results)),
        "status": max(4, max(len(STATUS_LABELS.get(r.status, r.status)) for r in results)),
        "mark": max(4, max(len(r.manual_mark or "-") for r in results)),
        "notes": max(4, max(len(r.notes or "-") for r in results)),
    }

    header = (
        f"{'ID':<{col_widths['id']}}  "
        f"{'姓名':<{col_widths['name']}}  "
        f"{'手机号':<{col_widths['phone']}}  "
        f"{'场次':<{col_widths['session']}}  "
        f"{'状态':<{col_widths['status']}}  "
        f"{'标记':<{col_widths['mark']}}  "
        f"{'备注':<{col_widths['notes']}}"
    )
    click.echo(header)
    click.echo("-" * len(header))

    for r in results:
        label = STATUS_LABELS.get(r.status, r.status)
        row = (
            f"{str(r.id):<{col_widths['id']}}  "
            f"{r.name:<{col_widths['name']}}  "
            f"{r.phone or '':<{col_widths['phone']}}  "
            f"{r.session:<{col_widths['session']}}  "
            f"{label:<{col_widths['status']}}  "
            f"{r.manual_mark or '-':<{col_widths['mark']}}  "
            f"{r.notes or '-':<{col_widths['notes']}}"
        )
        click.echo(row)

    click.echo(f"\n共 {len(results)} 条结果")
    storage.close()


@main.command("view-list")
def do_view_list():
    """列出所有已保存的视图"""
    storage = _get_storage()
    views = storage.list_views()
    if not views:
        click.echo("暂无已保存的视图")
        storage.close()
        return

    for v in views:
        filters = json.loads(v["filters"])
        desc_parts = []
        if filters.get("status"):
            desc_parts.append(f"状态={filters['status']}")
        if filters.get("session"):
            desc_parts.append(f"场次={filters['session']}")
        if filters.get("mark"):
            desc_parts.append(f"标记={filters['mark']}")
        if filters.get("keyword"):
            desc_parts.append(f"关键词={filters['keyword']}")
        if filters.get("limit"):
            desc_parts.append(f"限制={filters['limit']}")
        if filters.get("sort_by"):
            desc_parts.append(f"排序={filters['sort_by']}")
        if filters.get("sort_order"):
            desc_parts.append(f"方向={filters['sort_order']}")
        desc = ", ".join(desc_parts) if desc_parts else "无筛选条件"
        click.echo(f"  {v['name']}  ({desc})")

    click.echo(f"\n共 {len(views)} 个视图")
    storage.close()


@main.command("view-delete")
@click.argument("name")
def do_view_delete(name: str):
    """删除已保存的视图"""
    storage = _get_storage()
    ok = storage.delete_view(name)
    if not ok:
        click.echo(f"错误：视图「{name}」不存在", err=True)
        storage.close()
        sys.exit(1)
    click.echo(f"[OK] 视图「{name}」已删除")
    storage.close()


@main.command("batch-mark")
@click.argument("csv_file")
def do_batch_mark(csv_file: str):
    """批量导入人工复核标记（CSV 格式：result_id, mark_text, notes）"""
    storage = _get_storage()
    rows, abs_path = _read_csv(csv_file)

    if not rows:
        click.echo("错误：CSV 文件为空", err=True)
        storage.close()
        sys.exit(1)

    headers = set(rows[0].keys())
    required = {"result_id", "mark_text"}
    missing = required - headers
    if missing:
        click.echo(f"错误：CSV 表头缺失必填列：{', '.join(sorted(missing))}", err=True)
        click.echo(f"  当前表头：{', '.join(rows[0].keys())}", err=True)
        click.echo(f"  必须包含：result_id, mark_text（notes 为可选列）", err=True)
        storage.close()
        sys.exit(1)

    prev_states, errors = batch_mark(storage, rows)

    if errors:
        click.echo(f"错误：校验未通过，共 {len(errors)} 个问题，未写入任何数据：", err=True)
        for e in errors:
            click.echo(f"  [ERR] {e['message']}", err=True)
        storage.close()
        sys.exit(1)

    click.echo(f"[OK] 成功导入 {len(prev_states)} 条人工标记")
    storage.close()


@main.command("export")
@click.option("--output", "-o", default=None, help="输出文件路径（默认从配置读取）")
@click.option("--status", "-s", default=None, help="按状态筛选")
@click.option("--session", default=None, help="按场次筛选")
@click.option("--mark", default=None, help="按人工标记筛选")
@click.option("--keyword", "-k", default=None, help="按姓名或手机号关键词筛选")
@click.option("--limit", "-n", type=int, default=None, help="限制导出条数")
@click.option("--sort-by", type=click.Choice(["id", "status"]), default=None, help="排序字段")
@click.option("--sort-order", type=click.Choice(["asc", "desc"]), default=None, help="排序方向")
@click.option("--view", "-v", default=None, help="使用已保存的视图名加载筛选条件")
def do_export(output, status, session, mark, keyword, limit, sort_by, sort_order, view):
    """导出对账结果为 CSV（支持筛选）"""
    storage = _get_storage()
    if status is not None:
        status = _validate_status(status)
    filters = _resolve_filters(
        status, session, mark, keyword, limit, sort_by, sort_order, view, None, False, storage
    )
    has_filter = any(filters.get(k) for k in ("status", "session", "mark", "keyword"))
    if has_filter:
        results = storage.query_reconcile_results(
            status=filters.get("status"),
            session=filters.get("session"),
            mark=filters.get("mark"),
            keyword=filters.get("keyword"),
            limit=filters.get("limit"),
            sort_by=filters.get("sort_by", "id"),
            sort_order=filters.get("sort_order", "asc"),
        )
    else:
        results = storage.get_all_reconcile_results()

    if not results:
        click.echo("错误：暂无对账结果，请先执行 reconcile", err=True)
        storage.close()
        sys.exit(1)

    if output is None:
        output = config_module.get_config("export_path")

    abs_output = os.path.abspath(output)
    with open(abs_output, "w", encoding="utf-8-sig", newline="") as f:
        writer = csv.writer(f)
        writer.writerow(["ID", "姓名", "手机号", "场次", "状态", "标记", "备注"])
        for r in results:
            label = STATUS_LABELS.get(r.status, r.status)
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
    for st in ["normal", "absent", "non_enrolled", "duplicate"]:
        cnt = status_counts.get(st, 0)
        label = STATUS_LABELS.get(st, st)
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

    if stats["status_counts"]:
        for status in ["normal", "absent", "non_enrolled", "duplicate"]:
            cnt = stats["status_counts"].get(status, 0)
            if cnt:
                label = STATUS_LABELS.get(status, status)
                click.echo(f"  - {label}: {cnt}")

    click.echo(f"撤销历史: {stats['undo_count']} 步")
    click.echo(f"导入错误: {stats['error_count']} 条")

    if stats['error_count'] > 0:
        errors = storage.get_all_import_errors()
        migration_errors = [e for e in errors if e.source_type.startswith("migration_")]
        if migration_errors:
            enroll_dropped = sum(1 for e in migration_errors if e.source_type == "migration_enrollments")
            signin_dropped = sum(1 for e in migration_errors if e.source_type == "migration_signins")
            if enroll_dropped:
                click.echo(f"  （迁移时去重丢弃报名重复记录 {enroll_dropped} 条，详见 errors 命令）")
            if signin_dropped:
                click.echo(f"  （迁移时去重丢弃签到重复记录 {signin_dropped} 条，详见 errors 命令）")

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


def _calc_percentage(count: int, total: int) -> str:
    if total == 0:
        return "0.00%"
    return f"{count / total * 100:.2f}%"


def _build_stats_rows(results: List[Dict[str, Any]], sessions: List[str]) -> List[Dict[str, Any]]:
    session_status_counts: Dict[str, Dict[str, int]] = defaultdict(lambda: defaultdict(int))
    for r in results:
        session_status_counts[r["session"]][r["status"]] = r["count"]

    rows = []
    for session in sessions:
        counts = session_status_counts.get(session, {})
        total = sum(counts.values())
        row = {
            "session": session,
            "total": total,
        }
        for status in ["normal", "absent", "non_enrolled", "duplicate"]:
            cnt = counts.get(status, 0)
            row[status] = cnt
            row[f"{status}_pct"] = _calc_percentage(cnt, total)
        rows.append(row)
    return rows


def _print_stats_table(rows: List[Dict[str, Any]], show_global: bool = True):
    all_statuses = ["normal", "absent", "non_enrolled", "duplicate"]

    if show_global and len(rows) > 1:
        global_row = {
            "session": "合计",
            "total": sum(r["total"] for r in rows),
        }
        for status in all_statuses:
            global_row[status] = sum(r[status] for r in rows)
        total_all = global_row["total"]
        for status in all_statuses:
            global_row[f"{status}_pct"] = _calc_percentage(global_row[status], total_all)
        display_rows = rows + [global_row]
    else:
        display_rows = rows

    col_widths = {
        "session": max(6, max(len(str(r["session"])) for r in display_rows)),
        "total": max(6, max(len(str(r["total"])) for r in display_rows)),
    }
    for status in all_statuses:
        label = STATUS_LABELS[status]
        max_cnt_len = max(len(str(r[status])) for r in display_rows)
        max_pct_len = max(len(r[f"{status}_pct"]) for r in display_rows)
        col_widths[status] = max(len(label), max_cnt_len + max_pct_len + 3)

    header = f"{'场次':<{col_widths['session']}}  {'总计':<{col_widths['total']}}"
    for status in all_statuses:
        label = STATUS_LABELS[status]
        header += f"  {label:<{col_widths[status]}}"
    click.echo(header)
    click.echo("-" * len(header))

    for r in display_rows:
        line = f"{str(r['session']):<{col_widths['session']}}  {str(r['total']):<{col_widths['total']}}"
        for status in all_statuses:
            cnt = r[status]
            pct = r[f"{status}_pct"]
            cell = f"{cnt} ({pct})"
            line += f"  {cell:<{col_widths[status]}}"
        click.echo(line)


@main.command("stats")
@click.option("--session", default=None, help="按指定场次查看统计")
@click.option("--export", "-o", default=None, help="导出统计结果为 CSV")
def do_stats(session: Optional[str], export: Optional[str]):
    """查看对账结果统计汇总"""
    storage = _get_storage()

    all_results = storage.count_reconcile_results_by_session_and_status()
    all_sessions = storage.get_reconcile_sessions()

    if not all_results:
        click.echo("暂无对账结果，请先执行 reconcile")
        storage.close()
        return

    if session is not None:
        if session not in all_sessions:
            click.echo(f"错误：场次「{session}」不存在，可用场次：{', '.join(all_sessions)}", err=True)
            storage.close()
            sys.exit(1)
        results = [r for r in all_results if r["session"] == session]
        sessions = [session]
    else:
        results = all_results
        sessions = all_sessions

    stats_rows = _build_stats_rows(results, sessions)

    if export is None:
        _print_stats_table(stats_rows, show_global=(session is None and len(sessions) > 1))
        total_all = sum(r["total"] for r in stats_rows)
        click.echo(f"\n共 {len(stats_rows)} 场，{total_all} 条记录")
    else:
        abs_output = os.path.abspath(export)
        with open(abs_output, "w", encoding="utf-8-sig", newline="") as f:
            writer = csv.writer(f)
            writer.writerow(["场次", "总计", "正常签到", "正常签到占比", "缺席", "缺席占比", "非报名人员", "非报名人员占比", "重复扫码", "重复扫码占比"])
            for r in stats_rows:
                writer.writerow([
                    r["session"],
                    r["total"],
                    r["normal"],
                    r["normal_pct"],
                    r["absent"],
                    r["absent_pct"],
                    r["non_enrolled"],
                    r["non_enrolled_pct"],
                    r["duplicate"],
                    r["duplicate_pct"],
                ])
            if session is None and len(stats_rows) > 1:
                total_all = sum(r["total"] for r in stats_rows)
                writer.writerow([
                    "合计",
                    total_all,
                    sum(r["normal"] for r in stats_rows),
                    _calc_percentage(sum(r["normal"] for r in stats_rows), total_all),
                    sum(r["absent"] for r in stats_rows),
                    _calc_percentage(sum(r["absent"] for r in stats_rows), total_all),
                    sum(r["non_enrolled"] for r in stats_rows),
                    _calc_percentage(sum(r["non_enrolled"] for r in stats_rows), total_all),
                    sum(r["duplicate"] for r in stats_rows),
                    _calc_percentage(sum(r["duplicate"] for r in stats_rows), total_all),
                ])
        click.echo(f"[OK] 已导出 {len(stats_rows)} 场统计到 {abs_output}")

    storage.close()


@main.group("config")
def config_group():
    """管理 CLI 配置偏好"""
    pass


@config_group.command("show")
def config_show():
    """显示当前配置"""
    cfg = config_module.load_config()
    click.echo("当前配置：")
    for key in sorted(cfg.keys()):
        click.echo(f"  {key}: {cfg[key]}")


@config_group.command("set")
@click.argument("key")
@click.argument("value")
def config_set(key: str, value: str):
    """设置单项配置"""
    try:
        cfg = config_module.set_config(key, value)
        click.echo(f"[OK] 已设置 {key} = {cfg[key]}")
    except ValueError as e:
        click.echo(f"错误：{e}", err=True)
        sys.exit(1)


@config_group.command("reset")
@click.confirmation_option(prompt="确定要恢复出厂默认配置吗？")
def config_reset():
    """恢复出厂默认配置"""
    cfg = config_module.reset_config()
    click.echo("[OK] 已恢复默认配置")
    click.echo("当前配置：")
    for key in sorted(cfg.keys()):
        click.echo(f"  {key}: {cfg[key]}")


if __name__ == "__main__":
    main()
