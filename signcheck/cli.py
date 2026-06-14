import csv
import json
import os
import sys
import zipfile
import tempfile
import shutil
import click
from datetime import datetime
from typing import List, Optional, Dict, Any
from collections import defaultdict

from .models import (
    EnrollmentRecord,
    SigninRecord,
)
from .storage import Storage
from .reconcile import reconcile, undo, mark_result, batch_mark
from . import config as config_module
from .constants import STATUS_LABELS, VALID_STATUSES
from .csv_utils import read_csv_file
from .import_service import (
    import_enrollments,
    import_signins,
    import_rules,
    get_enroll_mapping,
    get_signin_mapping,
)
from .export_service import (
    compute_status_counts,
    format_csv_content,
    write_csv_file,
    build_json_data,
    write_json_file,
    write_xlsx_file,
    build_results_xlsx_rows,
    build_html_report,
    build_session_stats_for_report,
    calc_percentage,
    build_stats_rows,
)
from .handoff_service import (
    create_handoff,
    export_handoff,
    import_handoff,
    verify_handoff,
)


def _validate_status(status: Optional[str]) -> Optional[str]:
    if status is None:
        return None
    if status not in VALID_STATUSES:
        click.echo(f"错误：非法状态「{status}」，可选值：{', '.join(sorted(VALID_STATUSES))}", err=True)
        sys.exit(1)
    return status


def _get_storage() -> Storage:
    return Storage()


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


@click.group()
@click.version_option(version="1.0.0")
def main():
    """线下培训签到对账 CLI"""
    pass


@main.command("import-enroll")
@click.argument("csv_file")
@click.option("--dry-run", is_flag=True, default=False, help="只校验不落库，预览导入结果")
def import_enroll(csv_file: str, dry_run: bool):
    """导入报名 CSV 文件"""
    storage = _get_storage()
    rows, abs_path = read_csv_file(csv_file)
    mapping = get_enroll_mapping(storage)
    from .csv_utils import apply_field_mapping
    mapped_rows = apply_field_mapping(rows, mapping)

    result = import_enrollments(storage, mapped_rows, abs_path, dry_run=dry_run)

    if dry_run:
        click.echo(f"[DRY-RUN] 校验完成，将导入 {len(result.valid_records)} 条报名记录")
        skipped = len(result.errors)
        if skipped:
            click.echo(f"[DRY-RUN] 跳过 {skipped} 条问题记录：")
            for e in result.errors:
                click.echo(f"  [ERR] {e['error_message']}")
        else:
            click.echo("[DRY-RUN] 所有记录校验通过，无错误")
        storage.close()
        return

    click.echo(f"[OK] 新增 {len(result.new_ids)} 条报名记录，跳过 {len(result.existing_ids)} 条已存在记录")
    if result.errors:
        for e in result.errors:
            click.echo(f"[ERR] {e['error_message']}")
        click.echo(f"共 {len(result.errors)} 条错误，已跳过")

    storage.close()


@main.command("import-signin")
@click.argument("csv_file")
@click.option("--dry-run", is_flag=True, default=False, help="只校验不落库，预览导入结果")
def import_signin(csv_file: str, dry_run: bool):
    """导入扫码签到 CSV 文件"""
    storage = _get_storage()
    rows, abs_path = read_csv_file(csv_file)
    mapping = get_signin_mapping(storage)
    from .csv_utils import apply_field_mapping
    mapped_rows = apply_field_mapping(rows, mapping)

    result = import_signins(storage, mapped_rows, abs_path, dry_run=dry_run)

    if dry_run:
        click.echo(f"[DRY-RUN] 校验完成，将导入 {len(result.valid_records)} 条签到记录")
        skipped = len(result.errors)
        if skipped:
            click.echo(f"[DRY-RUN] 跳过 {skipped} 条问题记录：")
            for e in result.errors:
                click.echo(f"  [ERR] {e['error_message']}")
        else:
            click.echo("[DRY-RUN] 所有记录校验通过，无错误")
        storage.close()
        return

    click.echo(f"[OK] 新增 {len(result.new_ids)} 条签到记录，跳过 {len(result.existing_ids)} 条已存在记录")
    if result.errors:
        for e in result.errors:
            click.echo(f"[ERR] {e['error_message']}")
        click.echo(f"共 {len(result.errors)} 条错误，已跳过")

    storage.close()


@main.command("import-rules")
@click.argument("json_file")
@click.option("--dry-run", is_flag=True, default=False, help="只校验不落库，预览导入结果")
def import_rules_cmd(json_file: str, dry_run: bool):
    """导入匹配规则 JSON 文件"""
    storage = _get_storage()
    abs_path = os.path.abspath(json_file)
    if not os.path.exists(abs_path):
        click.echo(f"错误：文件不存在 {abs_path}", err=True)
        sys.exit(1)

    with open(abs_path, "r", encoding="utf-8-sig") as f:
        data = json.load(f)

    match_rules_data = data.get("match_rules", [])
    field_mapping_data = data.get("field_mapping", None)

    result = import_rules(
        storage, match_rules_data, field_mapping_data, dry_run=dry_run, allow_contains=False
    )

    if dry_run:
        click.echo(f"[DRY-RUN] 校验完成，将导入 {len(result.rules)} 条匹配规则")
        if field_mapping_data:
            click.echo("[DRY-RUN] 将更新字段映射配置")
        if result.errors:
            click.echo(f"[DRY-RUN] 发现 {len(result.errors)} 个错误：")
            for e in result.errors:
                click.echo(f"  [ERR] {e}")
        else:
            click.echo("[DRY-RUN] 所有规则校验通过，无错误")
        storage.close()
        return

    if result.errors:
        click.echo(f"错误：校验未通过，共 {len(result.errors)} 个问题：", err=True)
        for e in result.errors:
            click.echo(f"  [ERR] {e}", err=True)
        storage.close()
        sys.exit(1)

    click.echo(f"[OK] 成功导入 {len(result.rules)} 条匹配规则")
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

    results, skipped_sessions = reconcile(storage)

    if skipped_sessions:
        click.echo(f"[提示] 已跳过 {len(skipped_sessions)} 个已关闭场次：{', '.join(skipped_sessions)}")

    status_counts = compute_status_counts(results)

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
    rows, abs_path = read_csv_file(csv_file)

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
@click.option("--format", "fmt", type=click.Choice(["csv", "json", "xlsx"]), default="csv", help="导出格式：csv/json/xlsx")
def do_export(output, status, session, mark, keyword, limit, sort_by, sort_order, view, fmt):
    """导出对账结果（支持 CSV/JSON/XLSX 格式，可筛选）"""
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
        default_ext = {"csv": ".csv", "json": ".json", "xlsx": ".xlsx"}[fmt]
        base_output = config_module.get_config("export_path")
        base, ext = os.path.splitext(base_output)
        output = base + default_ext

    abs_output = os.path.abspath(output)
    status_counts = compute_status_counts(results)

    if fmt == "csv":
        write_csv_file(abs_output, results)
    elif fmt == "json":
        json_data = build_json_data(results, status_counts)
        write_json_file(abs_output, json_data)
    elif fmt == "xlsx":
        headers, rows = build_results_xlsx_rows(results)
        write_xlsx_file(abs_output, headers, rows, sheet_name="对账结果")

    click.echo(f"[OK] 已导出 {len(results)} 条对账结果到 {abs_output}（格式：{fmt}）")
    click.echo("汇总：")
    for st in ["normal", "absent", "non_enrolled", "duplicate"]:
        cnt = status_counts.get(st, 0)
        label = STATUS_LABELS.get(st, st)
        click.echo(f"  {label}: {cnt}")

    storage.close()


@main.command("report")
@click.option("--output", "-o", default="signin_report.html", help="输出文件路径（默认 signin_report.html）")
@click.option("--session", default=None, help="按指定场次生成报告，不指定则生成全局报告")
@click.option("--format", "fmt", type=click.Choice(["html"]), default="html", help="报告格式（目前支持 html）")
def do_report(output: str, session: Optional[str], fmt: str):
    """生成签到报告（HTML 格式，含汇总统计与详细名单）"""
    storage = _get_storage()

    all_results = storage.get_all_reconcile_results()
    if not all_results:
        click.echo("错误：暂无对账结果，请先执行 reconcile", err=True)
        storage.close()
        sys.exit(1)

    all_sessions = storage.get_reconcile_sessions()

    if session is not None:
        if session not in all_sessions:
            click.echo(f"错误：场次「{session}」不存在，可用场次：{', '.join(all_sessions)}", err=True)
            storage.close()
            sys.exit(1)
        target_sessions = [session]
        title = f"签到报告 - {session}"
    else:
        target_sessions = all_sessions
        title = "签到报告 - 全局汇总"

    results = [r for r in all_results if r.session in target_sessions]

    global_stats, session_stats, results_by_session = build_session_stats_for_report(
        results, target_sessions
    )

    generated_at = datetime.now().strftime("%Y-%m-%d %H:%M:%S")

    if fmt == "html":
        html = build_html_report(
            title=title,
            global_stats=global_stats,
            session_stats=session_stats,
            results_by_session=results_by_session,
            generated_at=generated_at,
        )
        abs_output = os.path.abspath(output)
        os.makedirs(os.path.dirname(abs_output), exist_ok=True)
        with open(abs_output, "w", encoding="utf-8") as f:
            f.write(html)

    click.echo(f"[OK] 报告已生成：{abs_output}")
    click.echo(f"  场次：{len(target_sessions)} 个　记录：{len(results)} 条")
    for st in ["normal", "absent", "non_enrolled", "duplicate"]:
        cnt = global_stats.get(st, 0)
        label = STATUS_LABELS.get(st, st)
        click.echo(f"  {label}: {cnt}")
    click.echo(f"  浏览器打开即可查看")

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


@main.command("backup")
@click.option("--output-dir", "-d", default=".", help="备份文件输出目录（默认当前目录）")
def do_backup(output_dir: str):
    """备份全部数据为时间戳命名的 .zip 快照"""
    storage = _get_storage()
    snapshot = storage.export_all()
    storage.close()

    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    backup_name = f"signcheck_backup_{timestamp}.zip"
    abs_output_dir = os.path.abspath(output_dir)
    os.makedirs(abs_output_dir, exist_ok=True)
    abs_output = os.path.join(abs_output_dir, backup_name)

    tmp_dir = tempfile.mkdtemp(prefix="signcheck_backup_")
    try:
        meta_file = os.path.join(tmp_dir, "metadata.json")
        meta = snapshot.get("_meta", {})
        meta["filename"] = backup_name
        write_json_file(meta_file, meta)

        for table_name in Storage.BACKUP_TABLES:
            rows = snapshot.get(table_name, [])
            if rows:
                table_file = os.path.join(tmp_dir, f"{table_name}.json")
                write_json_file(table_file, rows)

        with zipfile.ZipFile(abs_output, "w", zipfile.ZIP_DEFLATED) as zf:
            zf.write(meta_file, "metadata.json")
            for table_name in Storage.BACKUP_TABLES:
                table_file = os.path.join(tmp_dir, f"{table_name}.json")
                if os.path.exists(table_file):
                    zf.write(table_file, f"{table_name}.json")
    finally:
        shutil.rmtree(tmp_dir, ignore_errors=True)

    click.echo(f"[OK] 备份已生成：{abs_output}")
    summary = snapshot.get("_meta", {})
    for table in Storage.BACKUP_TABLES:
        cnt = len(snapshot.get(table, []))
        if cnt > 0:
            click.echo(f"  {table}: {cnt} 条")
    click.echo(f"  导出时间：{summary.get('exported_at', 'N/A')}")


@main.command("restore")
@click.argument("backup_file")
@click.option("--force", is_flag=True, default=False, help="跳过冲突确认，强制覆盖还原")
def do_restore(backup_file: str, force: bool):
    """从备份 .zip 还原数据，还原前检查冲突"""
    abs_backup = os.path.abspath(backup_file)
    if not os.path.exists(abs_backup):
        click.echo(f"错误：备份文件不存在 {abs_backup}", err=True)
        sys.exit(1)
    if not zipfile.is_zipfile(abs_backup):
        click.echo(f"错误：{abs_backup} 不是有效的 zip 文件", err=True)
        sys.exit(1)

    tmp_dir = tempfile.mkdtemp(prefix="signcheck_restore_")
    try:
        with zipfile.ZipFile(abs_backup, "r") as zf:
            zf.extractall(tmp_dir)

        meta_file = os.path.join(tmp_dir, "metadata.json")
        if not os.path.exists(meta_file):
            click.echo("错误：备份文件缺少 metadata.json", err=True)
            sys.exit(1)
        with open(meta_file, "r", encoding="utf-8") as f:
            meta = json.load(f)

        snapshot: Dict[str, Any] = {"_meta": meta}
        for table_name in Storage.BACKUP_TABLES:
            table_file = os.path.join(tmp_dir, f"{table_name}.json")
            if os.path.exists(table_file):
                with open(table_file, "r", encoding="utf-8") as f:
                    snapshot[table_name] = json.load(f)
            else:
                snapshot[table_name] = []
    finally:
        shutil.rmtree(tmp_dir, ignore_errors=True)

    storage = _get_storage()
    try:
        current = storage.get_conflict_summary()
        backup_sessions = set()
        for s in snapshot.get("sessions", []):
            if s.get("name"):
                backup_sessions.add(s["name"])
        for e in snapshot.get("enrollments", []):
            if e.get("session"):
                backup_sessions.add(e["session"])
        for s in snapshot.get("signins", []):
            if s.get("session"):
                backup_sessions.add(s["session"])
        for r in snapshot.get("reconcile_results", []):
            if r.get("session"):
                backup_sessions.add(r["session"])

        current_sessions = set(current.get("sessions", []))
        session_conflicts = sorted(backup_sessions & current_sessions)

        table_conflicts = []
        for table in Storage.BACKUP_TABLES:
            cur_cnt = current.get("table_counts", {}).get(table, 0)
            bak_cnt = len(snapshot.get(table, []))
            if cur_cnt > 0 and bak_cnt > 0:
                table_conflicts.append((table, cur_cnt, bak_cnt))

        has_conflict = bool(session_conflicts or table_conflicts)

        if has_conflict and not force:
            click.echo("检测到冲突，请确认是否继续还原：")
            click.echo()
            if session_conflicts:
                click.echo(f"场次名冲突（{len(session_conflicts)} 个）：")
                for s in session_conflicts:
                    click.echo(f"  - {s}")
                click.echo()
            if table_conflicts:
                click.echo("现有数据与备份数据重叠的表：")
                for table, cur_cnt, bak_cnt in table_conflicts:
                    click.echo(f"  - {table}: 当前 {cur_cnt} 条 / 备份 {bak_cnt} 条")
                click.echo()
            if not click.confirm("确认用备份数据覆盖当前全部数据？此操作不可撤销"):
                click.echo("已取消还原")
                storage.close()
                return

        storage.import_snapshot(snapshot, overwrite=True)

        click.echo(f"[OK] 已从备份还原：{abs_backup}")
        for table in Storage.BACKUP_TABLES:
            cnt = len(snapshot.get(table, []))
            if cnt > 0:
                click.echo(f"  {table}: 还原 {cnt} 条")
    finally:
        storage.close()


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
            global_row[f"{status}_pct"] = calc_percentage(global_row[status], total_all)
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
@click.option("--export", "-o", default=None, help="导出统计结果到文件")
@click.option("--format", "fmt", type=click.Choice(["csv", "json", "xlsx"]), default="csv", help="导出格式：csv/json/xlsx")
def do_stats(session: Optional[str], export: Optional[str], fmt: str):
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

    stats_rows = build_stats_rows(results, sessions)

    if export is None:
        _print_stats_table(stats_rows, show_global=(session is None and len(sessions) > 1))
        total_all = sum(r["total"] for r in stats_rows)
        click.echo(f"\n共 {len(stats_rows)} 场，{total_all} 条记录")
    else:
        abs_output = os.path.abspath(export)
        headers = ["场次", "总计", "正常签到", "正常签到占比", "缺席", "缺席占比", "非报名人员", "非报名人员占比", "重复扫码", "重复扫码占比"]
        display_rows = []
        for r in stats_rows:
            display_rows.append([
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
            display_rows.append([
                "合计",
                total_all,
                sum(r["normal"] for r in stats_rows),
                calc_percentage(sum(r["normal"] for r in stats_rows), total_all),
                sum(r["absent"] for r in stats_rows),
                calc_percentage(sum(r["absent"] for r in stats_rows), total_all),
                sum(r["non_enrolled"] for r in stats_rows),
                calc_percentage(sum(r["non_enrolled"] for r in stats_rows), total_all),
                sum(r["duplicate"] for r in stats_rows),
                calc_percentage(sum(r["duplicate"] for r in stats_rows), total_all),
            ])

        if fmt == "csv":
            os.makedirs(os.path.dirname(abs_output), exist_ok=True)
            with open(abs_output, "w", encoding="utf-8-sig", newline="") as f:
                writer = csv.writer(f)
                writer.writerow(headers)
                for row in display_rows:
                    writer.writerow(row)
        elif fmt == "json":
            session_stats = []
            for r in stats_rows:
                session_stats.append({
                    "session": r["session"],
                    "total": r["total"],
                    "status": {
                        STATUS_LABELS[s]: {
                            "count": r[s],
                            "percentage": r[f"{s}_pct"],
                        } for s in ["normal", "absent", "non_enrolled", "duplicate"]
                    },
                })
            json_data = {
                "meta": {
                    "generated_at": datetime.now().isoformat(),
                    "sessions_count": len(stats_rows),
                    "total_records": sum(r["total"] for r in stats_rows),
                },
                "sessions": session_stats,
            }
            if session is None and len(stats_rows) > 1:
                total_all = sum(r["total"] for r in stats_rows)
                json_data["global"] = {
                    "total": total_all,
                    "status": {
                        STATUS_LABELS[s]: {
                            "count": sum(r[s] for r in stats_rows),
                            "percentage": calc_percentage(sum(r[s] for r in stats_rows), total_all),
                        } for s in ["normal", "absent", "non_enrolled", "duplicate"]
                    },
                }
            write_json_file(abs_output, json_data)
        elif fmt == "xlsx":
            write_xlsx_file(abs_output, headers, display_rows, sheet_name="场次统计")

        click.echo(f"[OK] 已导出 {len(stats_rows)} 场统计到 {abs_output}（格式：{fmt}）")

    storage.close()


@main.group("session")
def session_group():
    """管理场次生命周期"""
    pass


@session_group.command("create")
@click.argument("name")
@click.option("--start", "start_time", default=None, help="场次开始时间")
@click.option("--end", "end_time", default=None, help="场次结束时间")
@click.option("--desc", "description", default=None, help="场次描述")
def session_create(name: str, start_time: Optional[str], end_time: Optional[str], description: Optional[str]):
    """创建新场次"""
    storage = _get_storage()
    session_id = storage.create_session(name, start_time, end_time, description)
    if session_id is None:
        click.echo(f"错误：场次「{name}」已存在", err=True)
        storage.close()
        sys.exit(1)
    click.echo(f"[OK] 已创建场次「{name}」（ID: {session_id}）")
    storage.close()


@session_group.command("close")
@click.argument("name")
def session_close(name: str):
    """关闭场次，拒绝新导入和对账操作"""
    storage = _get_storage()
    if storage.get_session(name) is None:
        click.echo(f"错误：场次「{name}」不存在", err=True)
        storage.close()
        sys.exit(1)
    ok = storage.close_session(name)
    if not ok:
        click.echo(f"错误：场次「{name}」已处于关闭状态", err=True)
        storage.close()
        sys.exit(1)
    click.echo(f"[OK] 场次「{name}」已关闭")
    storage.close()


@session_group.command("list")
def session_list():
    """列出所有场次及状态"""
    storage = _get_storage()
    sessions = storage.get_all_sessions()
    if not sessions:
        click.echo("暂无场次，请使用 session create 创建")
        storage.close()
        return

    col_widths = {
        "name": max(6, max(len(s.name) for s in sessions)),
        "status": max(8, max(len("已关闭" if s.status == "closed" else "进行中") for s in sessions)),
        "start": max(8, max(len(s.start_time or "-") for s in sessions)),
        "end": max(8, max(len(s.end_time or "-") for s in sessions)),
        "created": max(8, max(len(s.created_at or "-") for s in sessions)),
    }

    header = (
        f"{'场次名':<{col_widths['name']}}  "
        f"{'状态':<{col_widths['status']}}  "
        f"{'开始时间':<{col_widths['start']}}  "
        f"{'结束时间':<{col_widths['end']}}  "
        f"{'创建时间':<{col_widths['created']}}"
    )
    click.echo(header)
    click.echo("-" * len(header))

    for s in sessions:
        status_label = "已关闭" if s.status == "closed" else "进行中"
        row = (
            f"{s.name:<{col_widths['name']}}  "
            f"{status_label:<{col_widths['status']}}  "
            f"{s.start_time or '-':<{col_widths['start']}}  "
            f"{s.end_time or '-':<{col_widths['end']}}  "
            f"{s.created_at or '-':<{col_widths['created']}}"
        )
        click.echo(row)

    click.echo(f"\n共 {len(sessions)} 个场次")
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


@main.group("handoff")
def handoff_group():
    """场次交接包管理"""
    pass


@handoff_group.command("create")
@click.argument("session")
@click.option("--operator", "-o", required=True, help="操作者（必填）")
def handoff_create(session: str, operator: str):
    """从已对账场次生成交接包"""
    if not operator or not operator.strip():
        click.echo("错误：操作者不能为空，禁止生成交接包", err=True)
        sys.exit(1)

    storage = _get_storage()
    try:
        pkg, _ = create_handoff(storage, session, operator)
        click.echo(f"[OK] 交接包已生成")
        click.echo(f"  包编号: {pkg.package_id}")
        click.echo(f"  场次: {pkg.session}")
        click.echo(f"  操作者: {pkg.operator}")
        click.echo(f"  报名数: {pkg.enroll_count}")
        click.echo(f"  签到数: {pkg.signin_count}")
        click.echo(f"  对账结果数: {pkg.result_count}")
        click.echo(f"  人工标记数: {pkg.manual_mark_count}")
        click.echo(f"  生成时间: {pkg.generated_at}")
        summary = json.loads(pkg.status_summary)
        for label, count in summary.items():
            click.echo(f"  {label}: {count}")
    except ValueError as e:
        click.echo(f"错误：{e}", err=True)
        storage.close()
        sys.exit(1)
    storage.close()


@handoff_group.command("list")
@click.option("--session", default=None, help="按场次筛选")
def handoff_list(session: Optional[str]):
    """列出所有交接包"""
    storage = _get_storage()
    if session:
        packages = storage.get_handoff_packages_by_session(session)
    else:
        packages = storage.get_all_handoff_packages()

    if not packages:
        click.echo("暂无交接包")
        storage.close()
        return

    col_widths = {
        "package_id": max(8, max(len(p.package_id) for p in packages)),
        "session": max(4, max(len(p.session) for p in packages)),
        "operator": max(4, max(len(p.operator) for p in packages)),
        "results": max(4, max(len(str(p.result_count)) for p in packages)),
        "generated": max(8, max(len(p.generated_at or "-") for p in packages)),
    }

    header = (
        f"{'包编号':<{col_widths['package_id']}}  "
        f"{'场次':<{col_widths['session']}}  "
        f"{'操作者':<{col_widths['operator']}}  "
        f"{'结果数':<{col_widths['results']}}  "
        f"{'生成时间':<{col_widths['generated']}}"
    )
    click.echo(header)
    click.echo("-" * len(header))

    for p in packages:
        row = (
            f"{p.package_id:<{col_widths['package_id']}}  "
            f"{p.session:<{col_widths['session']}}  "
            f"{p.operator:<{col_widths['operator']}}  "
            f"{str(p.result_count):<{col_widths['results']}}  "
            f"{p.generated_at or '-':<{col_widths['generated']}}"
        )
        click.echo(row)

    click.echo(f"\n共 {len(packages)} 个交接包")
    storage.close()


@handoff_group.command("export")
@click.argument("package_id")
@click.option("--output", "-o", required=True, help="输出 zip 文件路径")
@click.option("--operator", "-p", required=True, help="操作者（必填）")
def handoff_export(package_id: str, output: str, operator: str):
    """导出交接包为 zip 文件（含 JSON 清单和 CSV 明细）"""
    if not operator or not operator.strip():
        click.echo("错误：操作者不能为空，禁止导出交接包", err=True)
        sys.exit(1)

    storage = _get_storage()
    try:
        abs_output = export_handoff(storage, package_id, output, operator)
        click.echo(f"[OK] 交接包已导出到 {abs_output}")
        click.echo(f"  包编号: {package_id}")
        click.echo(f"  包含文件: manifest.json, enrollments.csv, signins.csv, reconcile_results.csv, checksums.json")
    except ValueError as e:
        click.echo(f"错误：{e}", err=True)
        storage.close()
        sys.exit(1)
    storage.close()


@handoff_group.command("import")
@click.argument("zip_file")
@click.option("--operator", "-o", required=True, help="操作者（必填）")
@click.option("--overwrite", is_flag=True, default=False, help="允许覆盖已有交接包")
def handoff_import(zip_file: str, operator: str, overwrite: bool):
    """导入交接包 zip 文件"""
    if not operator or not operator.strip():
        click.echo("错误：操作者不能为空，禁止导入交接包", err=True)
        sys.exit(1)

    storage = _get_storage()
    try:
        result = import_handoff(storage, zip_file, operator, overwrite=overwrite)
        click.echo(f"[OK] 交接包已导入")
        click.echo(f"  包编号: {result['package_id']}")
        click.echo(f"  场次: {result['session']}")
        click.echo(f"  报名数: {result['enroll_count']}")
        click.echo(f"  签到数: {result['signin_count']}")
        click.echo(f"  对账结果数: {result['result_count']}")
        click.echo(f"  人工标记数: {result['manual_mark_count']}")
        if result['overwritten']:
            click.echo(f"  覆盖模式: 是（已写入审计日志）")
    except ValueError as e:
        click.echo(f"错误：{e}", err=True)
        storage.close()
        sys.exit(1)
    storage.close()


@handoff_group.command("verify")
@click.argument("zip_file")
def handoff_verify(zip_file: str):
    """校验交接包文件完整性（hash 校验）"""
    storage = _get_storage()
    try:
        valid, errors = verify_handoff(zip_file)
        if valid:
            click.echo("[OK] 交接包校验通过，所有文件 hash 一致")
        else:
            click.echo("校验失败，发现以下问题：", err=True)
            for err in errors:
                click.echo(f"  [ERR] {err}", err=True)
            storage.close()
            sys.exit(1)
    except ValueError as e:
        click.echo(f"错误：{e}", err=True)
        storage.close()
        sys.exit(1)
    storage.close()


if __name__ == "__main__":
    main()
