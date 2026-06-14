import os
import sys
import tempfile
import shutil
import csv
import json
from datetime import datetime

import pytest

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from signcheck.storage import Storage
from signcheck.csv_utils import read_csv_file, parse_csv_content, apply_field_mapping
from signcheck.import_service import (
    validate_enroll_rows,
    validate_signin_rows,
    import_enrollments,
    import_signins,
    validate_rules_data,
    import_rules,
    get_enroll_mapping,
    get_signin_mapping,
    ImportEnrollResult,
    ImportSigninResult,
    ImportRulesResult,
)
from signcheck.export_service import (
    result_to_dict,
    compute_status_counts,
    format_csv_content,
    build_json_data,
    build_html_report,
    build_session_stats_for_report,
)
from signcheck.handoff_service import (
    create_handoff,
    export_handoff,
    import_handoff,
    verify_handoff,
    write_audit_log,
)
from signcheck.constants import (
    STATUS_LABELS,
    VALID_STATUSES,
    DEFAULT_ENROLL_MAPPING,
    DEFAULT_SIGNIN_MAPPING,
)
from signcheck.reconcile import reconcile

SAMPLE_DIR = os.path.join(os.path.dirname(__file__), "..", "sample_data")


@pytest.fixture
def tmp_workspace():
    tmpdir = tempfile.mkdtemp(prefix="signcheck_service_test_")
    yield tmpdir
    shutil.rmtree(tmpdir, ignore_errors=True)


@pytest.fixture
def storage(tmp_workspace):
    db_dir = os.path.join(tmp_workspace, ".signcheck")
    os.makedirs(db_dir, exist_ok=True)
    db_path = os.path.join(db_dir, "signcheck.db")
    s = Storage(db_path)
    yield s
    s.close()


@pytest.fixture
def populated_storage(storage):
    enroll_path = os.path.join(SAMPLE_DIR, "enroll.csv")
    signin_path = os.path.join(SAMPLE_DIR, "signin.csv")
    rules_path = os.path.join(SAMPLE_DIR, "rules.json")

    enroll_rows, _ = read_csv_file(enroll_path)
    enroll_mapped = apply_field_mapping(enroll_rows, get_enroll_mapping(storage))
    import_enrollments(storage, enroll_mapped, "enroll.csv")

    signin_rows, _ = read_csv_file(signin_path)
    signin_mapped = apply_field_mapping(signin_rows, get_signin_mapping(storage))
    import_signins(storage, signin_mapped, "signin.csv")

    with open(rules_path, "r", encoding="utf-8") as f:
        rules_data = json.load(f)
    import_rules(storage, rules_data.get("match_rules", []), rules_data.get("field_mapping"))

    reconcile(storage)
    return storage


class TestCsvUtils:
    def test_read_csv_file(self):
        path = os.path.join(SAMPLE_DIR, "enroll.csv")
        rows, abs_path = read_csv_file(path)
        assert isinstance(rows, list)
        assert len(rows) > 0
        assert isinstance(rows[0], dict)
        assert abs_path.endswith("enroll.csv")

    def test_parse_csv_content(self):
        content = "name,phone,场次\n张三,13800138000,上午场\n李四,13800138001,上午场\n".encode("utf-8-sig")
        rows = parse_csv_content(content)
        assert len(rows) == 2
        assert rows[0]["name"] == "张三"
        assert rows[0]["phone"] == "13800138000"

    def test_apply_field_mapping(self):
        rows = [{"姓名": "张三", "手机号": "13800138000", "场次": "上午场"}]
        mapping = {"name": "姓名", "phone": "手机号", "session": "场次"}
        mapped = apply_field_mapping(rows, mapping)
        assert mapped[0]["name"] == "张三"
        assert mapped[0]["phone"] == "13800138000"
        assert mapped[0]["session"] == "上午场"

    def test_apply_field_mapping_missing_fields(self):
        rows = [{"姓名": "张三", "手机号": "13800138000"}]
        mapping = {"name": "姓名", "phone": "手机号", "session": "场次"}
        mapped = apply_field_mapping(rows, mapping)
        assert mapped[0]["session"] == ""

    def test_apply_field_mapping_strips_whitespace(self):
        rows = [{"姓名": " 张三 ", "手机号": " 13800138000 "}]
        mapping = {"name": "姓名", "phone": "手机号"}
        mapped = apply_field_mapping(rows, mapping)
        assert mapped[0]["name"] == "张三"
        assert mapped[0]["phone"] == "13800138000"

    def test_constants_are_defined(self):
        assert "normal" in STATUS_LABELS
        assert "absent" in STATUS_LABELS
        assert "normal" in VALID_STATUSES
        assert "name" in DEFAULT_ENROLL_MAPPING
        assert "name" in DEFAULT_SIGNIN_MAPPING


class TestImportService:
    def test_validate_enroll_rows_valid(self, storage):
        rows = [
            {"name": "张三", "phone": "13800138000", "session": "上午场"},
            {"name": "李四", "phone": "13800138001", "session": "上午场"},
        ]
        valid_records, errors = validate_enroll_rows(rows, "test.csv", storage)
        assert len(valid_records) == 2
        assert len(errors) == 0

    def test_validate_enroll_rows_missing_phone(self, storage):
        rows = [{"name": "张三", "phone": "", "session": "上午场"}]
        valid_records, errors = validate_enroll_rows(rows, "test.csv", storage)
        assert len(errors) > 0
        assert "手机号" in errors[0]["error_message"]

    def test_validate_enroll_rows_missing_session(self, storage):
        rows = [{"name": "张三", "phone": "13800138000", "session": ""}]
        valid_records, errors = validate_enroll_rows(rows, "test.csv", storage)
        assert len(errors) > 0

    def test_validate_signin_rows_valid(self, storage):
        rows = [
            {"name": "张三", "phone": "13800138000", "session": "上午场", "scan_time": "2024-01-01 09:00:00"},
        ]
        valid_records, errors = validate_signin_rows(rows, "test.csv", storage)
        assert len(valid_records) == 1
        assert len(errors) == 0

    def test_validate_signin_rows_missing_phone(self, storage):
        rows = [{"name": "张三", "phone": "", "session": "上午场"}]
        valid_records, errors = validate_signin_rows(rows, "test.csv", storage)
        assert len(errors) > 0

    def test_import_enrollments(self, storage):
        rows = [
            {"name": "张三", "phone": "13800138000", "session": "上午场"},
            {"name": "李四", "phone": "13800138001", "session": "上午场"},
        ]
        result = import_enrollments(storage, rows, "test.csv")
        assert isinstance(result, ImportEnrollResult)
        assert len(result.new_ids) == 2
        assert len(result.errors) == 0
        assert len(storage.get_all_enrollments()) == 2

    def test_import_enrollments_duplicate(self, storage):
        rows = [
            {"name": "张三", "phone": "13800138000", "session": "上午场"},
            {"name": "张三", "phone": "13800138000", "session": "上午场"},
        ]
        result = import_enrollments(storage, rows, "test.csv")
        assert len(result.new_ids) == 1
        assert len(result.existing_ids) == 1

    def test_import_signins(self, storage):
        rows = [
            {"name": "张三", "phone": "13800138000", "session": "上午场", "scan_time": "2024-01-01 09:00:00"},
        ]
        result = import_signins(storage, rows, "test.csv")
        assert isinstance(result, ImportSigninResult)
        assert len(result.new_ids) == 1

    def test_import_rules(self, storage):
        match_rules = [
            {"field": "name", "match_type": "contains", "session": "上午场"},
        ]
        result = import_rules(storage, match_rules)
        assert isinstance(result, ImportRulesResult)
        assert len(result.rules) == 1

    def test_validate_rules_data_invalid(self, storage):
        rules = [{"type": "invalid_type"}]
        result = validate_rules_data(rules, storage)
        assert isinstance(result, ImportRulesResult)
        assert len(result.errors) > 0

    def test_get_enroll_mapping_default(self, storage):
        m = get_enroll_mapping(storage)
        assert m == DEFAULT_ENROLL_MAPPING

    def test_get_signin_mapping_default(self, storage):
        m = get_signin_mapping(storage)
        assert m == DEFAULT_SIGNIN_MAPPING


class TestExportService:
    def test_result_to_dict(self, populated_storage):
        results = populated_storage.get_all_reconcile_results()
        assert len(results) > 0
        d = result_to_dict(results[0])
        assert "id" in d
        assert "name" in d
        assert "status" in d
        assert "code" in d["status"]
        assert "label" in d["status"]

    def test_compute_status_counts(self, populated_storage):
        results = populated_storage.get_all_reconcile_results()
        counts = compute_status_counts(results)
        assert isinstance(counts, dict)
        assert sum(counts.values()) == len(results)

    def test_format_csv_content(self, populated_storage):
        results = populated_storage.get_all_reconcile_results()
        csv_content = format_csv_content(results)
        assert isinstance(csv_content, str)
        lines = csv_content.strip().split("\n")
        assert len(lines) == len(results) + 1
        assert "姓名" in lines[0]
        assert "状态" in lines[0]

    def test_build_json_data(self, populated_storage):
        results = populated_storage.get_all_reconcile_results()
        counts = compute_status_counts(results)
        data = build_json_data(results, counts)
        assert "meta" in data
        assert "records" in data
        assert data["meta"]["total"] == len(results)

    def test_build_session_stats_for_report(self, populated_storage):
        results = populated_storage.get_all_reconcile_results()
        sessions = sorted({r.session for r in results})
        global_stats, session_stats, results_by_session = build_session_stats_for_report(results, sessions)
        assert isinstance(global_stats, dict)
        assert isinstance(session_stats, list)
        assert isinstance(results_by_session, dict)
        assert len(session_stats) == len(sessions)

    def test_build_html_report(self, populated_storage):
        results = populated_storage.get_all_reconcile_results()
        sessions = sorted({r.session for r in results})
        global_stats, session_stats, results_by_session = build_session_stats_for_report(results, sessions)
        html = build_html_report(
            title="测试报告",
            global_stats=global_stats,
            session_stats=session_stats,
            results_by_session=results_by_session,
            generated_at=datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        )
        assert isinstance(html, str)
        assert "<html" in html
        assert "测试报告" in html


class TestHandoffService:
    def test_create_handoff(self, populated_storage):
        pkg, data_hash = create_handoff(populated_storage, "上午场", "test_operator")
        assert pkg is not None
        assert pkg.package_id.startswith("HO-")
        assert pkg.session == "上午场"
        assert pkg.operator == "test_operator"
        assert data_hash is not None

    def test_create_handoff_no_operator(self, populated_storage):
        with pytest.raises(ValueError, match="操作者不能为空"):
            create_handoff(populated_storage, "上午场", "")

    def test_create_handoff_no_results(self, populated_storage):
        with pytest.raises(ValueError):
            create_handoff(populated_storage, "不存在的场次", "test_operator")

    def test_export_handoff(self, populated_storage, tmp_workspace):
        pkg, _ = create_handoff(populated_storage, "上午场", "creator")
        output_path = os.path.join(tmp_workspace, "handoff.zip")
        result_path = export_handoff(populated_storage, pkg.package_id, output_path, "exporter")
        assert os.path.exists(result_path)
        assert result_path == output_path

    def test_export_handoff_no_operator(self, populated_storage, tmp_workspace):
        pkg, _ = create_handoff(populated_storage, "上午场", "creator")
        output_path = os.path.join(tmp_workspace, "handoff.zip")
        with pytest.raises(ValueError, match="操作者不能为空"):
            export_handoff(populated_storage, pkg.package_id, output_path, "")

    def test_verify_handoff_valid(self, populated_storage, tmp_workspace):
        pkg, _ = create_handoff(populated_storage, "上午场", "creator")
        zip_path = os.path.join(tmp_workspace, "handoff.zip")
        export_handoff(populated_storage, pkg.package_id, zip_path, "exporter")
        valid, errors = verify_handoff(zip_path)
        assert valid is True
        assert len(errors) == 0

    def test_verify_handoff_tampered(self, populated_storage, tmp_workspace):
        import zipfile
        pkg, _ = create_handoff(populated_storage, "上午场", "creator")
        zip_path = os.path.join(tmp_workspace, "handoff.zip")
        export_handoff(populated_storage, pkg.package_id, zip_path, "exporter")
        tampered_zip = os.path.join(tmp_workspace, "tampered.zip")
        extract_dir = os.path.join(tmp_workspace, "extract")
        os.makedirs(extract_dir, exist_ok=True)
        with zipfile.ZipFile(zip_path, "r") as zf:
            zf.extractall(extract_dir)
        results_csv = os.path.join(extract_dir, "reconcile_results.csv")
        if os.path.exists(results_csv):
            with open(results_csv, "a", encoding="utf-8") as f:
                f.write("\n999,篡改,13800000000,上午场,正常签到,,")
        with zipfile.ZipFile(tampered_zip, "w") as zf:
            for root, dirs, files in os.walk(extract_dir):
                for fname in files:
                    fpath = os.path.join(root, fname)
                    arcname = os.path.relpath(fpath, extract_dir)
                    zf.write(fpath, arcname)
        valid, errors = verify_handoff(tampered_zip)
        assert valid is False

    def test_import_handoff(self, populated_storage, tmp_workspace):
        pkg, _ = create_handoff(populated_storage, "上午场", "creator")
        zip_path = os.path.join(tmp_workspace, "handoff.zip")
        export_handoff(populated_storage, pkg.package_id, zip_path, "exporter")
        db_dir2 = os.path.join(tmp_workspace, "db2", ".signcheck")
        os.makedirs(db_dir2, exist_ok=True)
        db_path2 = os.path.join(db_dir2, "signcheck.db")
        storage2 = Storage(db_path2)
        try:
            result = import_handoff(storage2, zip_path, "importer")
            assert "package_id" in result
            assert result["session"] == "上午场"
            assert result["overwritten"] is False
        finally:
            storage2.close()

    def test_import_handoff_conflict(self, populated_storage, tmp_workspace):
        pkg, _ = create_handoff(populated_storage, "上午场", "creator")
        zip_path = os.path.join(tmp_workspace, "handoff.zip")
        export_handoff(populated_storage, pkg.package_id, zip_path, "exporter")
        with pytest.raises(ValueError, match="冲突"):
            import_handoff(populated_storage, zip_path, "importer")

    def test_import_handoff_overwrite(self, populated_storage, tmp_workspace):
        pkg, _ = create_handoff(populated_storage, "上午场", "creator")
        zip_path = os.path.join(tmp_workspace, "handoff.zip")
        export_handoff(populated_storage, pkg.package_id, zip_path, "exporter")
        result = import_handoff(populated_storage, zip_path, "importer", overwrite=True)
        assert "package_id" in result
        assert result["overwritten"] is True

    def test_import_handoff_no_operator(self, populated_storage, tmp_workspace):
        pkg, _ = create_handoff(populated_storage, "上午场", "creator")
        zip_path = os.path.join(tmp_workspace, "handoff.zip")
        export_handoff(populated_storage, pkg.package_id, zip_path, "exporter")
        with pytest.raises(ValueError, match="操作者不能为空"):
            import_handoff(populated_storage, zip_path, "")

    def test_write_audit_log(self, storage):
        log_id = write_audit_log(storage, "operator1", "test_action", "target1", "success", "detail1")
        assert log_id > 0
        logs = storage.get_handoff_audit_logs()
        assert len(logs) >= 1
        found = False
        for log in logs:
            if log.operator == "operator1" and log.action == "test_action":
                found = True
                assert log.target == "target1"
                assert log.result == "success"
                assert log.detail == "detail1"
        assert found


class TestSqlitePersistence:
    def test_data_persists_across_reopen(self, tmp_workspace):
        db_dir = os.path.join(tmp_workspace, ".signcheck")
        os.makedirs(db_dir, exist_ok=True)
        db_path = os.path.join(db_dir, "signcheck.db")
        s1 = Storage(db_path)
        try:
            rows = [{"name": "张三", "phone": "13800138000", "session": "上午场"}]
            import_enrollments(s1, rows, "test.csv")
            assert len(s1.get_all_enrollments()) == 1
        finally:
            s1.close()
        s2 = Storage(db_path)
        try:
            enrollments = s2.get_all_enrollments()
            assert len(enrollments) == 1
            assert enrollments[0].name == "张三"
        finally:
            s2.close()

    def test_handoff_persistence_after_restart(self, tmp_workspace):
        db_dir = os.path.join(tmp_workspace, ".signcheck")
        os.makedirs(db_dir, exist_ok=True)
        db_path = os.path.join(db_dir, "signcheck.db")
        s1 = Storage(db_path)
        try:
            enroll_rows, _ = read_csv_file(os.path.join(SAMPLE_DIR, "enroll.csv"))
            enroll_mapped = apply_field_mapping(enroll_rows, get_enroll_mapping(s1))
            import_enrollments(s1, enroll_mapped, "enroll.csv")
            signin_rows, _ = read_csv_file(os.path.join(SAMPLE_DIR, "signin.csv"))
            signin_mapped = apply_field_mapping(signin_rows, get_signin_mapping(s1))
            import_signins(s1, signin_mapped, "signin.csv")
            reconcile(s1)
            pkg, _ = create_handoff(s1, "上午场", "test_operator")
            package_id = pkg.package_id
            packages = s1.get_all_handoff_packages()
            assert len(packages) == 1
        finally:
            s1.close()
        s2 = Storage(db_path)
        try:
            packages = s2.get_all_handoff_packages()
            assert len(packages) == 1
            assert packages[0].package_id == package_id
            audit_logs = s2.get_handoff_audit_logs()
            assert len(audit_logs) >= 1
        finally:
            s2.close()


class TestConflictAndAudit:
    def test_handoff_import_overwrite_audit_log(self, populated_storage, tmp_workspace):
        pkg, _ = create_handoff(populated_storage, "上午场", "creator")
        zip_path = os.path.join(tmp_workspace, "handoff.zip")
        export_handoff(populated_storage, pkg.package_id, zip_path, "exporter")
        import_handoff(populated_storage, zip_path, "importer", overwrite=True)
        logs = populated_storage.get_handoff_audit_logs()
        overwrite_logs = [l for l in logs if l.action == "handoff_import_overwrite"]
        assert len(overwrite_logs) >= 1
        assert overwrite_logs[0].operator == "importer"

    def test_multiple_audit_logs(self, storage):
        write_audit_log(storage, "op1", "action1", "t1", "success", "d1")
        write_audit_log(storage, "op2", "action2", "t2", "failed", "d2")
        write_audit_log(storage, "op3", "action3", "t3", "success", "d3")
        logs = storage.get_handoff_audit_logs()
        assert len(logs) >= 3
