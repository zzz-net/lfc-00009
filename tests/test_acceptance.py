import csv
import json
import os
import shutil
import sqlite3
import subprocess
import sys
import tempfile

SIGNCHECK_CMD = [sys.executable, "-m", "signcheck.cli"]
SAMPLE_DIR = os.path.join(os.path.dirname(__file__), "..", "sample_data")


def _env_for_subprocess(tmpdir):
    env = os.environ.copy()
    env["SIGNCHECK_DB_DIR"] = tmpdir
    env["PYTHONUTF8"] = "1"
    project_root = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
    env["PYTHONPATH"] = project_root + os.pathsep + env.get("PYTHONPATH", "")
    return env


def read_export_csv(path):
    with open(path, "r", encoding="utf-8-sig") as f:
        reader = csv.DictReader(f)
        return list(reader)


class TestContext:
    def __init__(self):
        self.tmpdir = tempfile.mkdtemp(prefix="signcheck_test_")
        self.db_dir = os.path.join(self.tmpdir, ".signcheck")

    def cleanup(self):
        shutil.rmtree(self.tmpdir, ignore_errors=True)

    def run(self, args, expect_fail=False):
        env = _env_for_subprocess(self.tmpdir)
        result = subprocess.run(
            SIGNCHECK_CMD + args,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            cwd=self.tmpdir,
            env=env,
        )
        if not expect_fail and result.returncode != 0:
            print(f"[FAIL] signcheck {' '.join(args)}")
            print(f"  STDOUT: {result.stdout}")
            print(f"  STDERR: {result.stderr}")
            sys.exit(1)
        return result

    def copy_sample(self, filename):
        src = os.path.join(SAMPLE_DIR, filename)
        dst = os.path.join(self.tmpdir, filename)
        shutil.copy2(src, dst)
        return dst

    def get_db_path(self):
        return os.path.join(self.db_dir, "signcheck.db")

    def db_execute(self, sql, params=None):
        db_path = self.get_db_path()
        if not os.path.exists(db_path):
            return []
        conn = sqlite3.connect(db_path)
        conn.row_factory = sqlite3.Row
        if params:
            rows = conn.execute(sql, params).fetchall()
        else:
            rows = conn.execute(sql).fetchall()
        result = [dict(r) for r in rows]
        conn.close()
        return result


def test_basic_reconcile():
    print("\n=== Test 1: Basic reconcile ===")
    ctx = TestContext()
    try:
        enroll_path = ctx.copy_sample("enroll.csv")
        signin_path = ctx.copy_sample("signin.csv")
        rules_path = ctx.copy_sample("rules.json")

        r = ctx.run(["import-enroll", enroll_path])
        assert "8" in r.stdout, f"Expected 8 enrollments: {r.stdout}"

        r = ctx.run(["import-signin", signin_path])
        assert "6" in r.stdout, f"Expected 6 signins: {r.stdout}"

        r = ctx.run(["import-rules", rules_path])
        assert "2" in r.stdout, f"Expected 2 rules: {r.stdout}"

        r = ctx.run(["reconcile"])
        assert "normal" in r.stdout or "4" in r.stdout, f"Reconcile should show results: {r.stdout}"

        output_path = os.path.join(ctx.tmpdir, "result.csv")
        r = ctx.run(["export", "--output", output_path])
        rows = read_export_csv(output_path)
        assert len(rows) > 0, "Export should not be empty"

        status_map = {}
        for row in rows:
            status = row.get("\ufeff状态") or row.get("状态", "")
            status_map[status] = status_map.get(status, 0) + 1

        print(f"  Result: {status_map}")
        print("  [PASS] Basic reconcile")

    finally:
        ctx.cleanup()


def test_import_errors():
    print("\n=== Test 2: Import errors (missing phone, invalid session) ===")
    ctx = TestContext()
    try:
        enroll_path = ctx.copy_sample("enroll.csv")
        rules_path = ctx.copy_sample("rules.json")

        ctx.run(["import-enroll", enroll_path])
        ctx.run(["import-rules", rules_path])

        enroll_count_before = ctx.db_execute("SELECT COUNT(*) as c FROM enrollments")[0]["c"]

        enroll_err_path = ctx.copy_sample("enroll_errors.csv")
        r = ctx.run(["import-enroll", enroll_err_path])
        assert "手机" in r.stdout, f"Should report phone error: {r.stdout}"
        assert "场次" in r.stdout, f"Should report session error: {r.stdout}"

        signin_err_path = ctx.copy_sample("signin_errors.csv")
        r = ctx.run(["import-signin", signin_err_path])
        assert "手机" in r.stdout, f"Should report phone error: {r.stdout}"
        assert "不存在" in r.stdout, f"Should report invalid session: {r.stdout}"

        errors = ctx.db_execute("SELECT * FROM import_errors")
        assert len(errors) >= 4, f"Should have at least 4 import errors: {len(errors)}"

        enroll_count_after = ctx.db_execute("SELECT COUNT(*) as c FROM enrollments")[0]["c"]
        assert enroll_count_after == enroll_count_before, f"Enrollment count should be unchanged due to idempotency: {enroll_count_before} vs {enroll_count_after}"

        r = ctx.run(["status"])
        assert str(enroll_count_before) in r.stdout, f"Status should show enrollment count: {r.stdout}"

        print(f"  Import errors: {len(errors)}, Enrollments preserved: {enroll_count_after}")
        print("  [PASS] Import errors handled correctly")

    finally:
        ctx.cleanup()


def test_duplicate_scan():
    print("\n=== Test 3: Duplicate scan produces only duplicate anomaly ===")
    ctx = TestContext()
    try:
        enroll_path = ctx.copy_sample("enroll.csv")
        signin_path = ctx.copy_sample("signin.csv")
        rules_path = ctx.copy_sample("rules.json")

        ctx.run(["import-enroll", enroll_path])
        ctx.run(["import-signin", signin_path])
        ctx.run(["import-rules", rules_path])
        ctx.run(["reconcile"])

        output_path = os.path.join(ctx.tmpdir, "result.csv")
        ctx.run(["export", "--output", output_path])
        rows = read_export_csv(output_path)

        status_key = "\ufeff状态" if "\ufeff状态" in rows[0] else "状态"
        name_key = "\ufeff姓名" if "\ufeff姓名" in rows[0] else "姓名"

        duplicate_count = sum(1 for r in rows if r[status_key] == "重复扫码")
        normal_zhangsan = [r for r in rows if r[name_key] == "张三" and r[status_key] == "正常签到"]

        assert duplicate_count == 1, f"Expected 1 duplicate, got {duplicate_count}"
        assert len(normal_zhangsan) == 1, f"Expected 1 normal for Zhang San, got {len(normal_zhangsan)}"

        print(f"  Duplicate: {duplicate_count}, Normal for Zhang San: {len(normal_zhangsan)}")
        print("  [PASS] Duplicate scan produces only duplicate anomaly")

    finally:
        ctx.cleanup()


def test_persistence():
    print("\n=== Test 4: Data persistence across sessions ===")
    ctx = TestContext()
    try:
        enroll_path = ctx.copy_sample("enroll.csv")
        signin_path = ctx.copy_sample("signin.csv")
        rules_path = ctx.copy_sample("rules.json")

        ctx.run(["import-enroll", enroll_path])
        ctx.run(["import-signin", signin_path])
        ctx.run(["import-rules", rules_path])
        ctx.run(["reconcile"])

        r = ctx.run(["mark", "1", "--mark-text", "confirmed", "--notes", "phone verified"])
        assert "1" in r.stdout, f"Mark should succeed: {r.stdout}"

        output1 = os.path.join(ctx.tmpdir, "result1.csv")
        ctx.run(["export", "--output", output1])
        rows1 = read_export_csv(output1)

        mark_key = "\ufeff标记" if "\ufeff标记" in rows1[0] else "标记"
        marked1 = [r for r in rows1 if r.get(mark_key, "") == "confirmed"]
        assert len(marked1) >= 1, f"Should have at least 1 marked result: {marked1}"

        output2 = os.path.join(ctx.tmpdir, "result2.csv")
        ctx.run(["export", "--output", output2])
        rows2 = read_export_csv(output2)

        assert len(rows1) == len(rows2), f"Row count should match: {len(rows1)} vs {len(rows2)}"

        undo_count_before = ctx.db_execute("SELECT COUNT(*) as c FROM undo_history")[0]["c"]

        r = ctx.run(["undo"])
        assert "undo" in r.stdout.lower() or "撤销" in r.stdout, f"Undo should succeed: {r.stdout}"

        output3 = os.path.join(ctx.tmpdir, "result3.csv")
        ctx.run(["export", "--output", output3])
        rows3 = read_export_csv(output3)

        marked3 = [r for r in rows3 if r.get(mark_key, "") == "confirmed"]
        assert len(marked3) == 0, f"Mark should be cleared after undo: {len(marked3)}"

        r = ctx.run(["status"])
        undo_count_after = ctx.db_execute("SELECT COUNT(*) as c FROM undo_history")[0]["c"]

        print(f"  Export 1: {len(rows1)} rows, marked: {len(marked1)}")
        print(f"  Export 2: {len(rows2)} rows (consistency check)")
        print(f"  After undo: marked {len(marked3)}")
        print(f"  Undo history: {undo_count_before} -> {undo_count_after}")
        print("  [PASS] Data persistence and undo")

    finally:
        ctx.cleanup()


def test_undo_import():
    print("\n=== Test 5: Undo import operation ===")
    ctx = TestContext()
    try:
        enroll_path = ctx.copy_sample("enroll.csv")
        rules_path = ctx.copy_sample("rules.json")

        ctx.run(["import-enroll", enroll_path])
        ctx.run(["import-rules", rules_path])

        enroll_count_before = ctx.db_execute("SELECT COUNT(*) as c FROM enrollments")[0]["c"]
        assert enroll_count_before == 8, f"Expected 8 enrollments, got {enroll_count_before}"

        ctx.run(["undo"])
        r = ctx.run(["undo"])

        enroll_count_after = ctx.db_execute("SELECT COUNT(*) as c FROM enrollments")[0]["c"]
        assert enroll_count_after == 0, f"Expected 0 enrollments after undo, got {enroll_count_after}"

        print(f"  Before undo: {enroll_count_before}, After undo: {enroll_count_after}")
        print("  [PASS] Undo import operation")

    finally:
        ctx.cleanup()


def test_import_idempotency():
    print("\n=== Test 6: Import idempotency (duplicate import) ===")
    ctx = TestContext()
    try:
        enroll_path = ctx.copy_sample("enroll.csv")
        signin_path = ctx.copy_sample("signin.csv")
        rules_path = ctx.copy_sample("rules.json")

        r1 = ctx.run(["import-enroll", enroll_path])
        assert "8" in r1.stdout and "跳过 0" in r1.stdout, f"First import: {r1.stdout}"

        enroll_count_1 = ctx.db_execute("SELECT COUNT(*) as c FROM enrollments")[0]["c"]
        assert enroll_count_1 == 8, f"Expected 8 after first import, got {enroll_count_1}"

        r2 = ctx.run(["import-enroll", enroll_path])
        assert "跳过 8" in r2.stdout, f"Second import should skip 8: {r2.stdout}"

        enroll_count_2 = ctx.db_execute("SELECT COUNT(*) as c FROM enrollments")[0]["c"]
        assert enroll_count_2 == 8, f"Expected 8 after second import, got {enroll_count_2}"

        ctx.run(["import-rules", rules_path])

        r3 = ctx.run(["import-signin", signin_path])
        assert "6" in r3.stdout and "跳过 0" in r3.stdout, f"First signin import: {r3.stdout}"

        signin_count_1 = ctx.db_execute("SELECT COUNT(*) as c FROM signins")[0]["c"]
        assert signin_count_1 == 6, f"Expected 6 after first signin import, got {signin_count_1}"

        r4 = ctx.run(["import-signin", signin_path])
        assert "跳过 6" in r4.stdout, f"Second signin import should skip 6: {r4.stdout}"

        signin_count_2 = ctx.db_execute("SELECT COUNT(*) as c FROM signins")[0]["c"]
        assert signin_count_2 == 6, f"Expected 6 after second signin import, got {signin_count_2}"

        ctx.run(["reconcile"])

        output1 = os.path.join(ctx.tmpdir, "result1.csv")
        ctx.run(["export", "--output", output1])
        rows1 = read_export_csv(output1)

        r_status = ctx.run(["status"])
        assert "8" in r_status.stdout, f"Should still have 8 enrollments in status: {r_status.stdout}"
        assert "6" in r_status.stdout, f"Should still have 6 signins in status: {r_status.stdout}"

        status_key = "\ufeff状态" if "\ufeff状态" in rows1[0] else "状态"
        absent_count = sum(1 for r in rows1 if r[status_key] == "缺席")
        normal_count = sum(1 for r in rows1 if r[status_key] == "正常签到")

        ctx.run(["import-enroll", enroll_path])
        ctx.run(["import-signin", signin_path])
        ctx.run(["reconcile"])

        output2 = os.path.join(ctx.tmpdir, "result2.csv")
        ctx.run(["export", "--output", output2])
        rows2 = read_export_csv(output2)

        absent_count_2 = sum(1 for r in rows2 if r[status_key] == "缺席")
        normal_count_2 = sum(1 for r in rows2 if r[status_key] == "正常签到")

        assert absent_count == absent_count_2, f"Absent count changed: {absent_count} -> {absent_count_2}"
        assert normal_count == normal_count_2, f"Normal count changed: {normal_count} -> {normal_count_2}"
        assert len(rows1) == len(rows2), f"Row count changed: {len(rows1)} -> {len(rows2)}"

        print(f"  Enroll: {enroll_count_1} -> {enroll_count_2} (stable)")
        print(f"  Signin: {signin_count_1} -> {signin_count_2} (stable)")
        print(f"  Reconcile rows: {len(rows1)} -> {len(rows2)} (stable)")
        print(f"  Absent: {absent_count} -> {absent_count_2}, Normal: {normal_count} -> {normal_count_2}")
        print("  [PASS] Import idempotency")

    finally:
        ctx.cleanup()


def test_import_bom_json():
    print("\n=== Test 7: Import rules with UTF-8 BOM ===")
    ctx = TestContext()
    try:
        bom_path = ctx.copy_sample("rules_bom.json")

        with open(bom_path, "rb") as f:
            header = f.read(3)
            assert header == b"\xef\xbb\xbf", f"File should have BOM, got {header!r}"

        r = ctx.run(["import-rules", bom_path])
        assert "2" in r.stdout, f"Should import 2 rules: {r.stdout}"

        rules_count = ctx.db_execute("SELECT COUNT(*) as c FROM rules")[0]["c"]
        assert rules_count == 2, f"Expected 2 rules, got {rules_count}"

        enroll_path = ctx.copy_sample("enroll.csv")
        signin_path = ctx.copy_sample("signin.csv")
        ctx.run(["import-enroll", enroll_path])
        ctx.run(["import-signin", signin_path])

        r = ctx.run(["reconcile"])
        assert "normal" in r.stdout or "4" in r.stdout, f"Reconcile should work: {r.stdout}"

        print(f"  BOM rules imported: {rules_count}")
        print("  [PASS] UTF-8 BOM JSON import")

    finally:
        ctx.cleanup()


def _create_old_schema_db(db_path: str):
    os.makedirs(os.path.dirname(db_path), exist_ok=True)
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    conn.execute("""
        CREATE TABLE IF NOT EXISTS enrollments (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            name TEXT NOT NULL,
            phone TEXT,
            session TEXT NOT NULL,
            source_file TEXT,
            source_row INTEGER,
            imported_at TEXT NOT NULL
        )
    """)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS signins (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            name TEXT NOT NULL,
            phone TEXT,
            session TEXT NOT NULL,
            scan_time TEXT,
            source_file TEXT,
            source_row INTEGER,
            imported_at TEXT NOT NULL
        )
    """)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS rules (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            field_name TEXT NOT NULL,
            match_type TEXT NOT NULL DEFAULT 'exact',
            threshold REAL,
            priority INTEGER NOT NULL DEFAULT 0,
            imported_at TEXT NOT NULL
        )
    """)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS field_mapping (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            mapping_type TEXT NOT NULL,
            field_name TEXT NOT NULL,
            csv_column TEXT NOT NULL,
            imported_at TEXT NOT NULL
        )
    """)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS reconcile_results (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            enroll_id INTEGER,
            signin_id INTEGER,
            name TEXT NOT NULL,
            phone TEXT,
            session TEXT NOT NULL,
            status TEXT NOT NULL,
            manual_mark TEXT,
            notes TEXT,
            created_at TEXT NOT NULL
        )
    """)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS undo_history (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            action_type TEXT NOT NULL,
            action_data TEXT NOT NULL,
            created_at TEXT NOT NULL
        )
    """)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS import_errors (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            source_type TEXT NOT NULL,
            source_file TEXT,
            row_number INTEGER NOT NULL,
            error_type TEXT NOT NULL,
            error_message TEXT NOT NULL,
            raw_data TEXT
        )
    """)
    conn.commit()
    return conn


def _verify_no_unique_constraint(conn: sqlite3.Connection, table: str):
    cur = conn.execute(f"PRAGMA index_list({table})")
    for idx in cur.fetchall():
        assert idx["origin"] != "u", f"Old schema should not have UNIQUE constraint on {table}"


def test_old_db_migration_with_duplicates():
    print("\n=== Test 8: Old DB migration with duplicate enrollments ===")
    ctx = TestContext()
    try:
        db_path = ctx.get_db_path()
        os.makedirs(os.path.dirname(db_path), exist_ok=True)

        conn = _create_old_schema_db(db_path)

        _verify_no_unique_constraint(conn, "enrollments")
        _verify_no_unique_constraint(conn, "signins")

        conn.execute(
            "INSERT INTO enrollments (name,phone,session,source_file,source_row,imported_at) VALUES (?,?,?,?,?,?)",
            ("张三", "13800001111", "上午场", "enroll.csv", 2, "2025-01-01T09:00:00"),
        )
        conn.execute(
            "INSERT INTO enrollments (name,phone,session,source_file,source_row,imported_at) VALUES (?,?,?,?,?,?)",
            ("张三", "13800001111", "上午场", "enroll.csv", 2, "2025-01-01T09:05:00"),
        )
        conn.execute(
            "INSERT INTO enrollments (name,phone,session,source_file,source_row,imported_at) VALUES (?,?,?,?,?,?)",
            ("张三", "13800001111", "上午场", "enroll.csv", 2, "2025-01-01T09:10:00"),
        )
        conn.execute(
            "INSERT INTO enrollments (name,phone,session,source_file,source_row,imported_at) VALUES (?,?,?,?,?,?)",
            ("李四", "13800002222", "上午场", "enroll.csv", 3, "2025-01-01T09:00:00"),
        )
        conn.execute(
            "INSERT INTO enrollments (name,phone,session,source_file,source_row,imported_at) VALUES (?,?,?,?,?,?)",
            ("李四", "13800002222", "上午场", "enroll.csv", 3, "2025-01-01T09:06:00"),
        )
        conn.execute(
            "INSERT INTO enrollments (name,phone,session,source_file,source_row,imported_at) VALUES (?,?,?,?,?,?)",
            ("王五", "13800003333", "下午场", "enroll.csv", 4, "2025-01-01T09:00:00"),
        )

        conn.execute(
            "INSERT INTO signins (name,phone,session,scan_time,source_file,source_row,imported_at) VALUES (?,?,?,?,?,?,?)",
            ("张三", "13800001111", "上午场", "09:01:15", "signin.csv", 2, "2025-01-01T10:00:00"),
        )
        conn.execute(
            "INSERT INTO signins (name,phone,session,scan_time,source_file,source_row,imported_at) VALUES (?,?,?,?,?,?,?)",
            ("张三", "13800001111", "上午场", "09:01:15", "signin.csv", 2, "2025-01-01T10:01:00"),
        )
        conn.execute(
            "INSERT INTO signins (name,phone,session,scan_time,source_file,source_row,imported_at) VALUES (?,?,?,?,?,?,?)",
            ("李四", "13800002222", "上午场", "09:05:40", "signin.csv", 3, "2025-01-01T10:00:00"),
        )

        cur = conn.execute("SELECT COUNT(*) as c FROM enrollments")
        assert cur.fetchone()["c"] == 6, f"Expected 6 enrollments before migration"
        cur = conn.execute("SELECT COUNT(*) as c FROM signins")
        assert cur.fetchone()["c"] == 3, f"Expected 3 signins before migration"
        conn.commit()
        conn.close()

        r = ctx.run(["status"])
        assert r.returncode == 0, f"Status command should not crash on dirty old DB: {r.stderr}"
        assert "8" not in r.stdout, f"Should not have 8 enrollments after dedup: {r.stdout}"

        errors = ctx.db_execute("SELECT * FROM import_errors WHERE source_type LIKE 'migration_%'")
        assert len(errors) >= 4, f"Should have at least 4 migration dedup errors (3 enroll + 1 signin): {len(errors)}"

        enroll_count = ctx.db_execute("SELECT COUNT(*) as c FROM enrollments")[0]["c"]
        signin_count = ctx.db_execute("SELECT COUNT(*) as c FROM signins")[0]["c"]
        assert enroll_count == 3, f"Expected 3 unique enrollments after dedup, got {enroll_count}"
        assert signin_count == 2, f"Expected 2 unique signins after dedup, got {signin_count}"

        enroll_path = ctx.copy_sample("enroll.csv")
        rules_path = ctx.copy_sample("rules.json")

        r = ctx.run(["import-enroll", enroll_path])
        assert r.returncode == 0, f"import-enroll should not crash after migration: {r.stderr}"
        assert "跳过 3" in r.stdout, f"Should skip 3 existing enrollments: {r.stdout}"

        ctx.run(["import-rules", rules_path])

        signin_path = ctx.copy_sample("signin.csv")
        r = ctx.run(["import-signin", signin_path])
        assert r.returncode == 0, f"import-signin should not crash after migration: {r.stderr}"

        r = ctx.run(["reconcile"])
        assert r.returncode == 0, f"reconcile should not crash after migration: {r.stderr}"

        output_path = os.path.join(ctx.tmpdir, "result.csv")
        r = ctx.run(["export", "--output", output_path])
        assert r.returncode == 0, f"export should not crash after migration: {r.stderr}"
        rows = read_export_csv(output_path)
        assert len(rows) > 0, f"Export should produce rows: {len(rows)}"

        r = ctx.run(["errors"])
        assert r.returncode == 0, f"errors command should not crash: {r.stderr}"
        assert "migration" in r.stdout, f"Should show migration dedup errors: {r.stdout}"

        print(f"  Before dedup: enroll=6, signin=3")
        print(f"  After dedup: enroll={enroll_count}, signin={signin_count}")
        print(f"  Migration errors logged: {len(errors)}")
        print(f"  Reconcile/Export rows: {len(rows)}")
        print("  [PASS] Old DB migration with duplicates")

    finally:
        ctx.cleanup()


if __name__ == "__main__":
    os.chdir(os.path.dirname(__file__) + "/..")
    print("Running acceptance tests...")
    test_basic_reconcile()
    test_import_errors()
    test_duplicate_scan()
    test_persistence()
    test_undo_import()
    test_import_idempotency()
    test_import_bom_json()
    test_old_db_migration_with_duplicates()
    print("\n[OK] All acceptance tests passed!")
