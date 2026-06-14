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


def _setup_reconciled_data(ctx):
    enroll_path = ctx.copy_sample("enroll.csv")
    signin_path = ctx.copy_sample("signin.csv")
    rules_path = ctx.copy_sample("rules.json")
    ctx.run(["import-enroll", enroll_path])
    ctx.run(["import-signin", signin_path])
    ctx.run(["import-rules", rules_path])
    ctx.run(["reconcile"])


def _write_csv(tmpdir, filename, header, rows):
    path = os.path.join(tmpdir, filename)
    with open(path, "w", encoding="utf-8-sig", newline="") as f:
        writer = csv.writer(f)
        writer.writerow(header)
        for row in rows:
            writer.writerow(row)
    return path


def test_batch_mark_happy_path():
    print("\n=== Test 9: batch-mark happy path ===")
    ctx = TestContext()
    try:
        _setup_reconciled_data(ctx)

        results = ctx.db_execute("SELECT id, manual_mark, notes FROM reconcile_results ORDER BY id")
        result_ids = [r["id"] for r in results]

        csv_path = _write_csv(ctx.tmpdir, "batch1.csv",
                              ["result_id", "mark_text", "notes"],
                              [(result_ids[0], "confirmed", "phone ok"),
                               (result_ids[1], "reviewed", "")])

        r = ctx.run(["batch-mark", csv_path])
        assert "2" in r.stdout, f"Should report 2 imports: {r.stdout}"
        assert r.returncode == 0

        updated = ctx.db_execute("SELECT id, manual_mark, notes FROM reconcile_results WHERE id IN (?,?)",
                                 (result_ids[0], result_ids[1]))
        mark_map = {row["id"]: row for row in updated}
        assert mark_map[result_ids[0]]["manual_mark"] == "confirmed", f"mark_text mismatch"
        assert mark_map[result_ids[0]]["notes"] == "phone ok", f"notes mismatch"
        assert mark_map[result_ids[1]]["manual_mark"] == "reviewed", f"mark_text mismatch for 2nd"
        assert mark_map[result_ids[1]]["notes"] is None, f"empty notes should be None"

        print("  [PASS] batch-mark happy path")

    finally:
        ctx.cleanup()


def test_batch_mark_nonexistent_result():
    print("\n=== Test 10: batch-mark with non-existent result_id ===")
    ctx = TestContext()
    try:
        _setup_reconciled_data(ctx)

        csv_path = _write_csv(ctx.tmpdir, "batch_bad.csv",
                              ["result_id", "mark_text", "notes"],
                              [(99999, "confirmed", "ghost")])

        r = ctx.run(["batch-mark", csv_path], expect_fail=True)
        assert r.returncode != 0, "Should fail on non-existent result_id"
        assert "不存在" in r.stderr, f"Should mention not found: {r.stderr}"

        results = ctx.db_execute("SELECT * FROM reconcile_results WHERE manual_mark IS NOT NULL")
        assert len(results) == 0, "No data should be written when validation fails"

        print("  [PASS] batch-mark non-existent result_id")

    finally:
        ctx.cleanup()


def test_batch_mark_duplicate_result_id():
    print("\n=== Test 11: batch-mark with duplicate result_id in CSV ===")
    ctx = TestContext()
    try:
        _setup_reconciled_data(ctx)

        csv_path = _write_csv(ctx.tmpdir, "batch_dup.csv",
                              ["result_id", "mark_text", "notes"],
                              [(1, "confirmed", ""), (1, "reviewed", "dup")])

        r = ctx.run(["batch-mark", csv_path], expect_fail=True)
        assert r.returncode != 0, "Should fail on duplicate result_id"
        assert "重复" in r.stderr, f"Should mention duplicate: {r.stderr}"

        results = ctx.db_execute("SELECT * FROM reconcile_results WHERE manual_mark IS NOT NULL")
        assert len(results) == 0, "No data should be written"

        print("  [PASS] batch-mark duplicate result_id")

    finally:
        ctx.cleanup()


def test_batch_mark_empty_mark_text():
    print("\n=== Test 12: batch-mark with empty mark_text ===")
    ctx = TestContext()
    try:
        _setup_reconciled_data(ctx)

        csv_path = _write_csv(ctx.tmpdir, "batch_empty.csv",
                              ["result_id", "mark_text", "notes"],
                              [(1, "", "some notes")])

        r = ctx.run(["batch-mark", csv_path], expect_fail=True)
        assert r.returncode != 0, "Should fail on empty mark_text"
        assert "mark_text" in r.stderr, f"Should mention mark_text: {r.stderr}"

        results = ctx.db_execute("SELECT * FROM reconcile_results WHERE notes = 'some notes'")
        assert len(results) == 0, "No data should be written"

        print("  [PASS] batch-mark empty mark_text")

    finally:
        ctx.cleanup()


def test_batch_mark_header_mismatch():
    print("\n=== Test 13: batch-mark with CSV header mismatch ===")
    ctx = TestContext()
    try:
        _setup_reconciled_data(ctx)

        csv_path = _write_csv(ctx.tmpdir, "batch_header.csv",
                              ["id", "mark", "comment"],
                              [(1, "confirmed", "")])

        r = ctx.run(["batch-mark", csv_path], expect_fail=True)
        assert r.returncode != 0, "Should fail on header mismatch"
        assert "表头" in r.stderr, f"Should mention header: {r.stderr}"

        print("  [PASS] batch-mark header mismatch")

    finally:
        ctx.cleanup()


def test_batch_mark_partial_invalid_rows():
    print("\n=== Test 14: batch-mark with some valid and some invalid rows ===")
    ctx = TestContext()
    try:
        _setup_reconciled_data(ctx)

        csv_path = _write_csv(ctx.tmpdir, "batch_partial.csv",
                              ["result_id", "mark_text", "notes"],
                              [(1, "confirmed", "ok"),
                               (99999, "ghost", "not found"),
                               (2, "", "empty mark")])

        r = ctx.run(["batch-mark", csv_path], expect_fail=True)
        assert r.returncode != 0, "Should fail when any row is invalid"
        assert "不存在" in r.stderr, f"Should report not found: {r.stderr}"
        assert "mark_text" in r.stderr, f"Should report empty mark: {r.stderr}"

        results = ctx.db_execute("SELECT * FROM reconcile_results WHERE manual_mark IS NOT NULL")
        assert len(results) == 0, "All-or-nothing: no data should be written"

        print("  [PASS] batch-mark partial invalid rows (all-or-nothing)")

    finally:
        ctx.cleanup()


def test_batch_mark_undo():
    print("\n=== Test 15: batch-mark undo restores all previous marks ===")
    ctx = TestContext()
    try:
        _setup_reconciled_data(ctx)

        ctx.run(["mark", "1", "--mark-text", "old_mark", "--notes", "old_note"])

        result_before = ctx.db_execute("SELECT id, manual_mark, notes FROM reconcile_results WHERE id=1")[0]
        assert result_before["manual_mark"] == "old_mark", f"Pre-condition failed"
        assert result_before["notes"] == "old_note", f"Pre-condition failed"

        csv_path = _write_csv(ctx.tmpdir, "batch_undo.csv",
                              ["result_id", "mark_text", "notes"],
                              [(1, "new_mark", "new_note"),
                               (2, "another", "another_note")])

        ctx.run(["batch-mark", csv_path])

        result_after = ctx.db_execute("SELECT id, manual_mark, notes FROM reconcile_results WHERE id=1")[0]
        assert result_after["manual_mark"] == "new_mark", f"Mark should be updated"
        assert result_after["notes"] == "new_note", f"Notes should be updated"

        ctx.run(["undo"])

        result_restored = ctx.db_execute("SELECT id, manual_mark, notes FROM reconcile_results WHERE id=1")[0]
        assert result_restored["manual_mark"] == "old_mark", f"Undo should restore old mark: got {result_restored['manual_mark']}"
        assert result_restored["notes"] == "old_note", f"Undo should restore old notes: got {result_restored['notes']}"

        result2_restored = ctx.db_execute("SELECT id, manual_mark, notes FROM reconcile_results WHERE id=2")[0]
        assert result2_restored["manual_mark"] is None, f"Undo should restore 2nd to None"
        assert result2_restored["notes"] is None, f"Undo should restore 2nd notes to None"

        print("  [PASS] batch-mark undo restores all previous marks")

    finally:
        ctx.cleanup()


def test_batch_mark_undo_then_reimport():
    print("\n=== Test 16: batch-mark undo then re-import ===")
    ctx = TestContext()
    try:
        _setup_reconciled_data(ctx)

        csv_path = _write_csv(ctx.tmpdir, "batch_reimport.csv",
                              ["result_id", "mark_text", "notes"],
                              [(1, "first", "round1"),
                               (2, "second", "round1")])

        ctx.run(["batch-mark", csv_path])

        ctx.run(["undo"])

        csv_path2 = _write_csv(ctx.tmpdir, "batch_reimport2.csv",
                               ["result_id", "mark_text", "notes"],
                               [(1, "revised", "round2"),
                                (3, "third", "round2")])

        r = ctx.run(["batch-mark", csv_path2])
        assert "2" in r.stdout, f"Should import 2 marks: {r.stdout}"

        result1 = ctx.db_execute("SELECT manual_mark, notes FROM reconcile_results WHERE id=1")[0]
        assert result1["manual_mark"] == "revised", f"Should have revised mark"
        assert result1["notes"] == "round2", f"Should have round2 notes"

        result3 = ctx.db_execute("SELECT manual_mark, notes FROM reconcile_results WHERE id=3")[0]
        assert result3["manual_mark"] == "third", f"Should have third mark"
        assert result3["notes"] == "round2", f"Should have round2 notes"

        print("  [PASS] batch-mark undo then re-import")

    finally:
        ctx.cleanup()


def test_batch_mark_export_csv_fields():
    print("\n=== Test 17: batch-mark results appear in export CSV ===")
    ctx = TestContext()
    try:
        _setup_reconciled_data(ctx)

        csv_path = _write_csv(ctx.tmpdir, "batch_export.csv",
                              ["result_id", "mark_text", "notes"],
                              [(1, "confirmed", "verified by phone"),
                               (2, "reviewed", "needs follow-up")])

        ctx.run(["batch-mark", csv_path])

        output_path = os.path.join(ctx.tmpdir, "export_result.csv")
        ctx.run(["export", "--output", output_path])
        rows = read_export_csv(output_path)

        mark_key = "\ufeff标记" if "\ufeff标记" in rows[0] else "标记"
        notes_key = "\ufeff备注" if "\ufeff备注" in rows[0] else "备注"
        id_key = "\ufeffID" if "\ufeffID" in rows[0] else "ID"

        row1 = next(r for r in rows if r[id_key] == "1")
        assert row1[mark_key] == "confirmed", f"Export should contain mark: {row1[mark_key]}"
        assert row1[notes_key] == "verified by phone", f"Export should contain notes: {row1[notes_key]}"

        row2 = next(r for r in rows if r[id_key] == "2")
        assert row2[mark_key] == "reviewed", f"Export should contain mark: {row2[mark_key]}"
        assert row2[notes_key] == "needs follow-up", f"Export should contain notes: {row2[notes_key]}"

        print("  [PASS] batch-mark results appear in export CSV")

    finally:
        ctx.cleanup()


def test_batch_mark_persistence_across_sessions():
    print("\n=== Test 18: batch-mark data persists across process restarts ===")
    ctx = TestContext()
    try:
        _setup_reconciled_data(ctx)

        csv_path = _write_csv(ctx.tmpdir, "batch_persist.csv",
                              ["result_id", "mark_text", "notes"],
                              [(1, "persistent", "survives restart")])

        ctx.run(["batch-mark", csv_path])

        r1 = ctx.run(["status"])
        assert "1" in r1.stdout or "result" in r1.stdout.lower(), f"Status should work: {r1.stdout}"

        r2 = ctx.run(["status"])
        assert "1" in r2.stdout or "result" in r2.stdout.lower(), f"Status consistent after restart: {r2.stdout}"

        result = ctx.db_execute("SELECT manual_mark, notes FROM reconcile_results WHERE id=1")[0]
        assert result["manual_mark"] == "persistent", f"Data should persist in DB"
        assert result["notes"] == "survives restart", f"Notes should persist in DB"

        output_path = os.path.join(ctx.tmpdir, "persist_check.csv")
        ctx.run(["export", "--output", output_path])
        rows = read_export_csv(output_path)
        mark_key = "\ufeff标记" if "\ufeff标记" in rows[0] else "标记"
        row1 = next(r for r in rows if r.get("\ufeffID", r.get("ID")) == "1")
        assert row1[mark_key] == "persistent", f"Export after restart should show mark"

        r3 = ctx.run(["errors"])
        assert r3.returncode == 0

        print("  [PASS] batch-mark data persists across process restarts")

    finally:
        ctx.cleanup()


def test_batch_mark_no_notes_column():
    print("\n=== Test 19: batch-mark CSV without notes column ===")
    ctx = TestContext()
    try:
        _setup_reconciled_data(ctx)

        csv_path = _write_csv(ctx.tmpdir, "batch_no_notes.csv",
                              ["result_id", "mark_text"],
                              [(1, "confirmed")])

        r = ctx.run(["batch-mark", csv_path])
        assert r.returncode == 0, f"Should succeed without notes column: {r.stderr}"

        result = ctx.db_execute("SELECT manual_mark, notes FROM reconcile_results WHERE id=1")[0]
        assert result["manual_mark"] == "confirmed", f"Mark should be set"
        assert result["notes"] is None, f"Notes should remain None"

        print("  [PASS] batch-mark CSV without notes column")

    finally:
        ctx.cleanup()


def test_batch_mark_empty_csv():
    print("\n=== Test 20: batch-mark empty CSV file ===")
    ctx = TestContext()
    try:
        csv_path = _write_csv(ctx.tmpdir, "empty.csv",
                              ["result_id", "mark_text", "notes"],
                              [])

        r = ctx.run(["batch-mark", csv_path], expect_fail=True)
        assert r.returncode != 0, "Should fail on empty CSV"
        assert "为空" in r.stderr, f"Should mention empty: {r.stderr}"

        print("  [PASS] batch-mark empty CSV file")

    finally:
        ctx.cleanup()


def test_list_empty_results():
    print("\n=== Test 21: list with no reconcile results ===")
    ctx = TestContext()
    try:
        r = ctx.run(["list"])
        assert "暂无" in r.stdout, f"Should show empty message: {r.stdout}"
        assert r.returncode == 0

        print("  [PASS] list empty results")
    finally:
        ctx.cleanup()


def test_list_with_filters():
    print("\n=== Test 22: list with status/session/keyword filters ===")
    ctx = TestContext()
    try:
        _setup_reconciled_data(ctx)

        r = ctx.run(["list", "--status", "absent"])
        assert "缺席" in r.stdout, f"Should show absent results: {r.stdout}"
        assert "正常签到" not in r.stdout, f"Should not show normal results: {r.stdout}"

        r = ctx.run(["list", "--status", "normal"])
        assert "正常签到" in r.stdout, f"Should show normal results: {r.stdout}"

        r = ctx.run(["list", "--keyword", "张"])
        assert "张" in r.stdout, f"Should show results with keyword: {r.stdout}"

        r = ctx.run(["list", "--keyword", "13800001111"])
        assert "13800001111" in r.stdout, f"Should show results with phone keyword: {r.stdout}"

        r = ctx.run(["list", "--limit", "2"])
        lines = [l for l in r.stdout.strip().split("\n") if l and not l.startswith("-") and not l.startswith("共")]
        data_lines = [l for l in lines if not l.startswith("ID")]
        assert len(data_lines) <= 2, f"Should limit to 2 results: {len(data_lines)}"

        r = ctx.run(["list", "--sort-by", "status", "--sort-order", "desc"])
        assert r.returncode == 0

        print("  [PASS] list with filters")
    finally:
        ctx.cleanup()


def test_list_invalid_status():
    print("\n=== Test 23: list with invalid status ===")
    ctx = TestContext()
    try:
        _setup_reconciled_data(ctx)

        r = ctx.run(["list", "--status", "invalid_status"], expect_fail=True)
        assert r.returncode != 0, "Should fail on invalid status"
        assert "非法状态" in r.stderr, f"Should mention invalid status: {r.stderr}"

        print("  [PASS] list invalid status")
    finally:
        ctx.cleanup()


def test_list_view_not_found():
    print("\n=== Test 24: list with non-existent view ===")
    ctx = TestContext()
    try:
        _setup_reconciled_data(ctx)

        r = ctx.run(["list", "--view", "nonexistent"], expect_fail=True)
        assert r.returncode != 0, "Should fail on non-existent view"
        assert "不存在" in r.stderr, f"Should mention view not found: {r.stderr}"

        print("  [PASS] list view not found")
    finally:
        ctx.cleanup()


def test_list_save_view_conflict():
    print("\n=== Test 25: list save-view with name conflict ===")
    ctx = TestContext()
    try:
        _setup_reconciled_data(ctx)

        ctx.run(["list", "--status", "absent", "--save-view", "abs_only"])

        r = ctx.run(["list", "--status", "normal", "--save-view", "abs_only"], expect_fail=True)
        assert r.returncode != 0, "Should fail on duplicate view name"
        assert "已存在" in r.stderr, f"Should mention name conflict: {r.stderr}"

        views = ctx.db_execute("SELECT * FROM saved_views WHERE name='abs_only'")
        saved_filters = json.loads(views[0]["filters"])
        assert saved_filters["status"] == "absent", "Original view should not be overwritten"

        print("  [PASS] list save-view name conflict")
    finally:
        ctx.cleanup()


def test_list_save_view_overwrite():
    print("\n=== Test 26: list save-view with overwrite ===")
    ctx = TestContext()
    try:
        _setup_reconciled_data(ctx)

        ctx.run(["list", "--status", "absent", "--save-view", "myview"])

        r = ctx.run(["list", "--status", "normal", "--save-view", "myview", "--overwrite"])
        assert r.returncode == 0, f"Should succeed with overwrite: {r.stderr}"
        assert "已保存" in r.stdout, f"Should confirm save: {r.stdout}"

        views = ctx.db_execute("SELECT * FROM saved_views WHERE name='myview'")
        saved_filters = json.loads(views[0]["filters"])
        assert saved_filters["status"] == "normal", f"View should be updated: {saved_filters}"

        print("  [PASS] list save-view overwrite")
    finally:
        ctx.cleanup()


def test_list_use_view_after_restart():
    print("\n=== Test 27: use saved view after process restart ===")
    ctx = TestContext()
    try:
        _setup_reconciled_data(ctx)

        ctx.run(["list", "--status", "absent", "--save-view", "abs_view"])

        r = ctx.run(["list", "--view", "abs_view"])
        assert "缺席" in r.stdout, f"Should show absent results via view: {r.stdout}"
        assert "正常签到" not in r.stdout, f"Should not show normal results: {r.stdout}"

        r2 = ctx.run(["list", "--view", "abs_view"])
        assert r2.stdout == r.stdout, "Result should be stable across restarts"

        print("  [PASS] use saved view after restart")
    finally:
        ctx.cleanup()


def test_view_list_and_delete():
    print("\n=== Test 28: view-list and view-delete commands ===")
    ctx = TestContext()
    try:
        _setup_reconciled_data(ctx)

        r = ctx.run(["view-list"])
        assert "暂无" in r.stdout, f"Should show no views: {r.stdout}"

        ctx.run(["list", "--status", "absent", "--save-view", "v1"])
        ctx.run(["list", "--status", "normal", "--session", "上午场", "--save-view", "v2"])

        r = ctx.run(["view-list"])
        assert "v1" in r.stdout, f"Should list v1: {r.stdout}"
        assert "v2" in r.stdout, f"Should list v2: {r.stdout}"
        assert "共 2" in r.stdout, f"Should show 2 views: {r.stdout}"

        r = ctx.run(["view-delete", "v1"])
        assert "已删除" in r.stdout, f"Should confirm delete: {r.stdout}"

        r = ctx.run(["view-list"])
        assert "v1" not in r.stdout, f"v1 should be deleted: {r.stdout}"
        assert "v2" in r.stdout, f"v2 should still exist: {r.stdout}"

        r = ctx.run(["view-delete", "nonexistent"], expect_fail=True)
        assert r.returncode != 0, "Should fail on non-existent view delete"
        assert "不存在" in r.stderr, f"Should mention not found: {r.stderr}"

        print("  [PASS] view-list and view-delete")
    finally:
        ctx.cleanup()


def test_list_filter_with_mark():
    print("\n=== Test 29: list filter by manual mark ===")
    ctx = TestContext()
    try:
        _setup_reconciled_data(ctx)

        ctx.run(["mark", "1", "--mark-text", "confirmed", "--notes", "verified"])

        r = ctx.run(["list", "--mark", "confirmed"])
        assert "confirmed" in r.stdout, f"Should show marked results: {r.stdout}"

        r = ctx.run(["list", "--mark", "nonexistent_mark"])
        assert "暂无" in r.stdout, f"Should show empty for non-existent mark: {r.stdout}"

        print("  [PASS] list filter by manual mark")
    finally:
        ctx.cleanup()


def test_export_with_filters():
    print("\n=== Test 30: export with filter options ===")
    ctx = TestContext()
    try:
        _setup_reconciled_data(ctx)

        ctx.run(["mark", "1", "--mark-text", "confirmed", "--notes", "phone ok"])

        output_path = os.path.join(ctx.tmpdir, "filtered.csv")
        r = ctx.run(["export", "--status", "absent", "--output", output_path])
        rows = read_export_csv(output_path)

        status_key = "\ufeff状态" if "\ufeff状态" in rows[0] else "状态"
        for row in rows:
            assert row[status_key] == "缺席", f"All exported rows should be absent: {row[status_key]}"

        mark_key = "\ufeff标记" if "\ufeff标记" in rows[0] else "标记"
        notes_key = "\ufeff备注" if "\ufeff备注" in rows[0] else "备注"
        id_key = "\ufeffID" if "\ufeffID" in rows[0] else "ID"
        assert "标记" in rows[0], f"Export should have 标记 column"
        assert "备注" in rows[0] or "\ufeff备注" in rows[0], f"Export should have 备注 column"

        output2 = os.path.join(ctx.tmpdir, "all.csv")
        r = ctx.run(["export", "--output", output2])
        all_rows = read_export_csv(output2)
        assert len(all_rows) > len(rows), f"Unfiltered export should have more rows: {len(all_rows)} vs {len(rows)}"

        print("  [PASS] export with filters")
    finally:
        ctx.cleanup()


def test_export_from_view():
    print("\n=== Test 31: export using saved view ===")
    ctx = TestContext()
    try:
        _setup_reconciled_data(ctx)

        ctx.run(["list", "--status", "absent", "--save-view", "abs_export"])

        output_path = os.path.join(ctx.tmpdir, "view_export.csv")
        r = ctx.run(["export", "--view", "abs_export", "--output", output_path])
        rows = read_export_csv(output_path)

        status_key = "\ufeff状态" if "\ufeff状态" in rows[0] else "状态"
        for row in rows:
            assert row[status_key] == "缺席", f"All rows should be absent via view: {row[status_key]}"

        print("  [PASS] export from saved view")
    finally:
        ctx.cleanup()


def test_view_persistence_across_restart():
    print("\n=== Test 32: saved views persist across DB restart ===")
    ctx = TestContext()
    try:
        _setup_reconciled_data(ctx)

        ctx.run(["list", "--status", "absent", "--keyword", "孙", "--save-view", "persist_test"])

        views1 = ctx.db_execute("SELECT * FROM saved_views WHERE name='persist_test'")
        assert len(views1) == 1, "View should be in DB"
        saved_filters1 = json.loads(views1[0]["filters"])
        assert saved_filters1["status"] == "absent"
        assert saved_filters1["keyword"] == "孙"

        r = ctx.run(["list", "--view", "persist_test"])
        assert "缺席" in r.stdout, f"View should work after restart: {r.stdout}"

        views2 = ctx.db_execute("SELECT * FROM saved_views WHERE name='persist_test'")
        saved_filters2 = json.loads(views2[0]["filters"])
        assert saved_filters2 == saved_filters1, "Filters should be identical after restart"

        output_path = os.path.join(ctx.tmpdir, "persist_export.csv")
        ctx.run(["export", "--view", "persist_test", "--output", output_path])
        rows = read_export_csv(output_path)
        assert len(rows) > 0, "Export from persisted view should have rows"

        print("  [PASS] view persistence across restart")
    finally:
        ctx.cleanup()


def test_list_combined_filters():
    print("\n=== Test 33: list with combined filters and view override ===")
    ctx = TestContext()
    try:
        _setup_reconciled_data(ctx)

        ctx.run(["list", "--status", "absent", "--save-view", "base_abs"])

        r = ctx.run(["list", "--view", "base_abs", "--status", "normal"])
        assert "正常签到" in r.stdout, f"CLI option should override view status: {r.stdout}"
        assert "缺席" not in r.stdout, f"Should not show absent when overridden: {r.stdout}"

        print("  [PASS] list combined filters with view override")
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
    test_batch_mark_happy_path()
    test_batch_mark_nonexistent_result()
    test_batch_mark_duplicate_result_id()
    test_batch_mark_empty_mark_text()
    test_batch_mark_header_mismatch()
    test_batch_mark_partial_invalid_rows()
    test_batch_mark_undo()
    test_batch_mark_undo_then_reimport()
    test_batch_mark_export_csv_fields()
    test_batch_mark_persistence_across_sessions()
    test_batch_mark_no_notes_column()
    test_batch_mark_empty_csv()
    test_list_empty_results()
    test_list_with_filters()
    test_list_invalid_status()
    test_list_view_not_found()
    test_list_save_view_conflict()
    test_list_save_view_overwrite()
    test_list_use_view_after_restart()
    test_view_list_and_delete()
    test_list_filter_with_mark()
    test_export_with_filters()
    test_export_from_view()
    test_view_persistence_across_restart()
    test_list_combined_filters()
    print("\n[OK] All acceptance tests passed!")
