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

        enroll_err_path = ctx.copy_sample("enroll_errors.csv")
        r = ctx.run(["import-enroll", enroll_err_path])
        assert "2" in r.stdout, f"Should import 2 valid enroll records: {r.stdout}"

        signin_err_path = ctx.copy_sample("signin_errors.csv")
        r = ctx.run(["import-signin", signin_err_path])
        assert "2" in r.stdout, f"Should import 2 valid signin records: {r.stdout}"

        errors = ctx.db_execute("SELECT * FROM import_errors")
        assert len(errors) >= 4, f"Should have at least 4 import errors: {len(errors)}"

        error_msgs = [e["error_message"] for e in errors]
        has_phone_error = any("phone" in e.lower() or "手机" in e for e in error_msgs)
        has_session_error = any("session" in e.lower() or "场次" in e for e in error_msgs)
        assert has_phone_error, f"Should have phone error: {error_msgs}"
        assert has_session_error, f"Should have session error: {error_msgs}"

        enroll_count = ctx.db_execute("SELECT COUNT(*) as c FROM enrollments")[0]["c"]
        assert enroll_count == 10, f"Old data should be preserved: {enroll_count}"

        print(f"  Import errors: {len(errors)}, Enrollments preserved: {enroll_count}")
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


if __name__ == "__main__":
    os.chdir(os.path.dirname(__file__) + "/..")
    print("Running acceptance tests...")
    test_basic_reconcile()
    test_import_errors()
    test_duplicate_scan()
    test_persistence()
    test_undo_import()
    print("\n[OK] All acceptance tests passed!")
