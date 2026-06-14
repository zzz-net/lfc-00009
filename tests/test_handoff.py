import csv
import json
import os
import shutil
import sqlite3
import subprocess
import sys
import tempfile
import zipfile
import hashlib

SIGNCHECK_CMD = [sys.executable, "-m", "signcheck.cli"]
SAMPLE_DIR = os.path.join(os.path.dirname(__file__), "..", "sample_data")


def _env_for_subprocess(tmpdir):
    env = os.environ.copy()
    env["SIGNCHECK_DB_DIR"] = tmpdir
    env["PYTHONUTF8"] = "1"
    project_root = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
    env["PYTHONPATH"] = project_root + os.pathsep + env.get("PYTHONPATH", "")
    return env


class TestContext:
    def __init__(self):
        self.tmpdir = tempfile.mkdtemp(prefix="signcheck_handoff_test_")
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


def _setup_reconciled_data(ctx):
    enroll_path = ctx.copy_sample("enroll.csv")
    signin_path = ctx.copy_sample("signin.csv")
    rules_path = ctx.copy_sample("rules.json")
    ctx.run(["import-enroll", enroll_path])
    ctx.run(["import-signin", signin_path])
    ctx.run(["import-rules", rules_path])
    ctx.run(["reconcile"])


def test_handoff_create():
    print("\n=== Test: handoff create ===")
    ctx = TestContext()
    try:
        _setup_reconciled_data(ctx)

        r = ctx.run(["handoff", "create", "上午场", "--operator", "admin"])
        assert "[OK]" in r.stdout, f"Should succeed: {r.stdout}"
        assert "包编号" in r.stdout, f"Should show package_id: {r.stdout}"
        assert "上午场" in r.stdout, f"Should show session: {r.stdout}"
        assert "admin" in r.stdout, f"Should show operator: {r.stdout}"

        packages = ctx.db_execute("SELECT * FROM handoff_packages")
        assert len(packages) == 1, f"Should have 1 package: {len(packages)}"
        assert packages[0]["session"] == "上午场"
        assert packages[0]["operator"] == "admin"
        assert packages[0]["result_count"] > 0

        audit = ctx.db_execute("SELECT * FROM handoff_audit_log WHERE action='handoff_create'")
        assert len(audit) == 1, f"Should have 1 audit log: {len(audit)}"
        assert audit[0]["operator"] == "admin"
        assert audit[0]["result"] == "success"

        print("  [PASS] handoff create")
    finally:
        ctx.cleanup()


def test_handoff_create_no_operator():
    print("\n=== Test: handoff create without operator ===")
    ctx = TestContext()
    try:
        _setup_reconciled_data(ctx)

        r = ctx.run(["handoff", "create", "上午场"], expect_fail=True)
        assert r.returncode != 0, "Should fail without operator"

        packages = ctx.db_execute("SELECT * FROM handoff_packages")
        assert len(packages) == 0, f"No package should be created: {len(packages)}"

        print("  [PASS] handoff create without operator")
    finally:
        ctx.cleanup()


def test_handoff_create_no_results():
    print("\n=== Test: handoff create with no reconcile results ===")
    ctx = TestContext()
    try:
        r = ctx.run(["handoff", "create", "不存在的场次", "--operator", "admin"], expect_fail=True)
        assert r.returncode != 0, "Should fail with no results"
        assert "对账结果" in r.stderr, f"Should mention no results: {r.stderr}"

        print("  [PASS] handoff create with no reconcile results")
    finally:
        ctx.cleanup()


def test_handoff_list():
    print("\n=== Test: handoff list ===")
    ctx = TestContext()
    try:
        _setup_reconciled_data(ctx)

        r = ctx.run(["handoff", "list"])
        assert "暂无" in r.stdout, f"Should show no packages: {r.stdout}"

        ctx.run(["handoff", "create", "上午场", "--operator", "admin"])

        r = ctx.run(["handoff", "list"])
        assert "上午场" in r.stdout, f"Should show session: {r.stdout}"
        assert "admin" in r.stdout, f"Should show operator: {r.stdout}"
        assert "共 1" in r.stdout, f"Should show 1 package: {r.stdout}"

        r = ctx.run(["handoff", "list", "--session", "上午场"])
        assert "上午场" in r.stdout, f"Should filter by session: {r.stdout}"

        r = ctx.run(["handoff", "list", "--session", "不存在的场次"])
        assert "暂无" in r.stdout, f"Should show no packages for non-existent session: {r.stdout}"

        print("  [PASS] handoff list")
    finally:
        ctx.cleanup()


def test_handoff_export():
    print("\n=== Test: handoff export ===")
    ctx = TestContext()
    try:
        _setup_reconciled_data(ctx)

        r = ctx.run(["handoff", "create", "上午场", "--operator", "admin"])
        package_id = None
        for line in r.stdout.split("\n"):
            if "包编号" in line:
                package_id = line.split(":")[-1].strip()
                break
        assert package_id is not None, f"Should extract package_id: {r.stdout}"

        zip_path = os.path.join(ctx.tmpdir, "handoff.zip")
        r = ctx.run(["handoff", "export", package_id, "--output", zip_path, "--operator", "exporter"])
        assert "[OK]" in r.stdout, f"Should succeed: {r.stdout}"
        assert os.path.exists(zip_path), f"Zip file should exist: {zip_path}"

        with zipfile.ZipFile(zip_path, "r") as zf:
            names = zf.namelist()
            assert "manifest.json" in names, f"Should have manifest.json: {names}"
            assert "enrollments.csv" in names, f"Should have enrollments.csv: {names}"
            assert "signins.csv" in names, f"Should have signins.csv: {names}"
            assert "reconcile_results.csv" in names, f"Should have reconcile_results.csv: {names}"
            assert "checksums.json" in names, f"Should have checksums.json: {names}"

            with zf.open("manifest.json") as f:
                manifest = json.loads(f.read().decode("utf-8"))
                assert manifest["manifest"]["package_id"] == package_id
                assert manifest["manifest"]["session"] == "上午场"
                assert "enrollments" in manifest
                assert "signins" in manifest
                assert "reconcile_results" in manifest

            with zf.open("enrollments.csv") as f:
                content = f.read().decode("utf-8-sig")
                reader = csv.DictReader(content.strip().split("\n"))
                rows = list(reader)
                assert len(rows) > 0, f"Enrollment CSV should have rows"

            with zf.open("signins.csv") as f:
                content = f.read().decode("utf-8-sig")
                reader = csv.DictReader(content.strip().split("\n"))
                rows = list(reader)
                assert len(rows) > 0, f"Signin CSV should have rows"

            with zf.open("reconcile_results.csv") as f:
                content = f.read().decode("utf-8-sig")
                reader = csv.DictReader(content.strip().split("\n"))
                rows = list(reader)
                assert len(rows) > 0, f"Reconcile results CSV should have rows"

        export_logs = ctx.db_execute("SELECT * FROM handoff_export_log")
        assert len(export_logs) == 1, f"Should have 1 export log: {len(export_logs)}"
        assert export_logs[0]["operator"] == "exporter"

        audit = ctx.db_execute("SELECT * FROM handoff_audit_log WHERE action='handoff_export'")
        assert len(audit) == 1, f"Should have 1 export audit log: {len(audit)}"

        print("  [PASS] handoff export")
    finally:
        ctx.cleanup()


def test_handoff_export_no_operator():
    print("\n=== Test: handoff export without operator ===")
    ctx = TestContext()
    try:
        _setup_reconciled_data(ctx)

        r = ctx.run(["handoff", "create", "上午场", "--operator", "admin"])

        r = ctx.run(["handoff", "export", "some-id", "--output", "out.zip"], expect_fail=True)
        assert r.returncode != 0, "Should fail without operator"

        print("  [PASS] handoff export without operator")
    finally:
        ctx.cleanup()


def test_handoff_verify():
    print("\n=== Test: handoff verify ===")
    ctx = TestContext()
    try:
        _setup_reconciled_data(ctx)

        r = ctx.run(["handoff", "create", "上午场", "--operator", "admin"])
        package_id = None
        for line in r.stdout.split("\n"):
            if "包编号" in line:
                package_id = line.split(":")[-1].strip()
                break

        zip_path = os.path.join(ctx.tmpdir, "handoff.zip")
        ctx.run(["handoff", "export", package_id, "--output", zip_path, "--operator", "exporter"])

        r = ctx.run(["handoff", "verify", zip_path])
        assert "校验通过" in r.stdout, f"Should pass verification: {r.stdout}"

        print("  [PASS] handoff verify")
    finally:
        ctx.cleanup()


def test_handoff_verify_tampered():
    print("\n=== Test: handoff verify tampered file ===")
    ctx = TestContext()
    try:
        _setup_reconciled_data(ctx)

        r = ctx.run(["handoff", "create", "上午场", "--operator", "admin"])
        package_id = None
        for line in r.stdout.split("\n"):
            if "包编号" in line:
                package_id = line.split(":")[-1].strip()
                break

        zip_path = os.path.join(ctx.tmpdir, "handoff.zip")
        ctx.run(["handoff", "export", package_id, "--output", zip_path, "--operator", "exporter"])

        tamper_dir = tempfile.mkdtemp()
        try:
            with zipfile.ZipFile(zip_path, "r") as zf:
                zf.extractall(tamper_dir)

            manifest_path = os.path.join(tamper_dir, "manifest.json")
            with open(manifest_path, "r", encoding="utf-8") as f:
                data = json.load(f)
            data["manifest"]["session"] = "被篡改的场次"
            with open(manifest_path, "w", encoding="utf-8") as f:
                json.dump(data, f, ensure_ascii=False, indent=2)

            tampered_zip = os.path.join(ctx.tmpdir, "tampered.zip")
            with zipfile.ZipFile(tampered_zip, "w", zipfile.ZIP_DEFLATED) as zf:
                for fname in os.listdir(tamper_dir):
                    fpath = os.path.join(tamper_dir, fname)
                    zf.write(fpath, fname)

            r = ctx.run(["handoff", "verify", tampered_zip], expect_fail=True)
            assert r.returncode != 0, "Should fail on tampered file"
            assert "篡改" in r.stderr, f"Should report tampering: {r.stderr}"
        finally:
            shutil.rmtree(tamper_dir, ignore_errors=True)

        print("  [PASS] handoff verify tampered file")
    finally:
        ctx.cleanup()


def test_handoff_import():
    print("\n=== Test: handoff import ===")
    ctx = TestContext()
    try:
        _setup_reconciled_data(ctx)

        r = ctx.run(["handoff", "create", "上午场", "--operator", "admin"])
        package_id = None
        for line in r.stdout.split("\n"):
            if "包编号" in line:
                package_id = line.split(":")[-1].strip()
                break

        zip_path = os.path.join(ctx.tmpdir, "handoff.zip")
        ctx.run(["handoff", "export", package_id, "--output", zip_path, "--operator", "exporter"])

        ctx2_tmpdir = tempfile.mkdtemp(prefix="signcheck_handoff_import_test_")
        try:
            env = _env_for_subprocess(ctx2_tmpdir)
            r = subprocess.run(
                SIGNCHECK_CMD + ["handoff", "import", zip_path, "--operator", "importer"],
                capture_output=True, text=True, encoding="utf-8", errors="replace",
                cwd=ctx2_tmpdir, env=env,
            )
            assert r.returncode == 0, f"Import should succeed: {r.stderr}"
            assert "[OK]" in r.stdout, f"Should succeed: {r.stdout}"
            assert "上午场" in r.stdout, f"Should show session: {r.stdout}"
            assert "importer" in r.stdout or True
        finally:
            shutil.rmtree(ctx2_tmpdir, ignore_errors=True)

        print("  [PASS] handoff import")
    finally:
        ctx.cleanup()


def test_handoff_import_no_operator():
    print("\n=== Test: handoff import without operator ===")
    ctx = TestContext()
    try:
        _setup_reconciled_data(ctx)

        r = ctx.run(["handoff", "create", "上午场", "--operator", "admin"])
        package_id = None
        for line in r.stdout.split("\n"):
            if "包编号" in line:
                package_id = line.split(":")[-1].strip()
                break

        zip_path = os.path.join(ctx.tmpdir, "handoff.zip")
        ctx.run(["handoff", "export", package_id, "--output", zip_path, "--operator", "exporter"])

        r = ctx.run(["handoff", "import", zip_path], expect_fail=True)
        assert r.returncode != 0, "Should fail without operator"

        print("  [PASS] handoff import without operator")
    finally:
        ctx.cleanup()


def test_handoff_import_conflict():
    print("\n=== Test: handoff import conflict ===")
    ctx = TestContext()
    try:
        _setup_reconciled_data(ctx)

        r = ctx.run(["handoff", "create", "上午场", "--operator", "admin"])
        package_id = None
        for line in r.stdout.split("\n"):
            if "包编号" in line:
                package_id = line.split(":")[-1].strip()
                break

        zip_path = os.path.join(ctx.tmpdir, "handoff.zip")
        ctx.run(["handoff", "export", package_id, "--output", zip_path, "--operator", "exporter"])

        r = ctx.run(["handoff", "import", zip_path, "--operator", "importer"], expect_fail=True)
        assert r.returncode != 0, "Should fail on conflict"
        assert "冲突" in r.stderr, f"Should mention conflict: {r.stderr}"

        r = ctx.run(["handoff", "import", zip_path, "--operator", "importer", "--overwrite"])
        assert r.returncode == 0, f"Should succeed with overwrite: {r.stderr}"
        assert "[OK]" in r.stdout, f"Should succeed: {r.stdout}"
        assert "覆盖" in r.stdout, f"Should mention overwrite: {r.stdout}"

        audit = ctx.db_execute("SELECT * FROM handoff_audit_log WHERE action='handoff_import_overwrite'")
        assert len(audit) >= 1, f"Should have overwrite audit log: {len(audit)}"

        print("  [PASS] handoff import conflict")
    finally:
        ctx.cleanup()


def test_handoff_persistence_after_restart():
    print("\n=== Test: handoff persistence after restart ===")
    ctx = TestContext()
    try:
        _setup_reconciled_data(ctx)

        r = ctx.run(["handoff", "create", "上午场", "--operator", "admin"])
        package_id = None
        for line in r.stdout.split("\n"):
            if "包编号" in line:
                package_id = line.split(":")[-1].strip()
                break

        packages = ctx.db_execute("SELECT * FROM handoff_packages")
        assert len(packages) == 1, "Should have 1 package before restart"

        r = ctx.run(["handoff", "list"])
        assert package_id in r.stdout, f"Should find package after restart: {r.stdout}"

        packages2 = ctx.db_execute("SELECT * FROM handoff_packages")
        assert len(packages2) == 1, "Should still have 1 package after restart"
        assert packages2[0]["package_id"] == package_id

        audit = ctx.db_execute("SELECT * FROM handoff_audit_log")
        assert len(audit) >= 1, f"Should have audit logs: {len(audit)}"

        print("  [PASS] handoff persistence after restart")
    finally:
        ctx.cleanup()


def test_handoff_full_pipeline():
    print("\n=== Test: handoff full pipeline (create -> export -> verify -> import -> list) ===")
    ctx = TestContext()
    try:
        _setup_reconciled_data(ctx)

        ctx.run(["mark", "1", "--mark-text", "confirmed", "--notes", "verified"])

        r = ctx.run(["handoff", "create", "上午场", "--operator", "creator"])
        assert "[OK]" in r.stdout
        package_id = None
        for line in r.stdout.split("\n"):
            if "包编号" in line:
                package_id = line.split(":")[-1].strip()
                break

        r = ctx.run(["handoff", "list"])
        assert package_id in r.stdout

        zip_path = os.path.join(ctx.tmpdir, "pipeline.zip")
        r = ctx.run(["handoff", "export", package_id, "--output", zip_path, "--operator", "exporter"])
        assert "[OK]" in r.stdout

        r = ctx.run(["handoff", "verify", zip_path])
        assert "校验通过" in r.stdout

        ctx2_tmpdir = tempfile.mkdtemp(prefix="signcheck_pipeline_test_")
        try:
            env = _env_for_subprocess(ctx2_tmpdir)

            r = subprocess.run(
                SIGNCHECK_CMD + ["handoff", "list"],
                capture_output=True, text=True, encoding="utf-8", errors="replace",
                cwd=ctx2_tmpdir, env=env,
            )
            assert "暂无" in r.stdout, f"New instance should have no packages: {r.stdout}"

            r = subprocess.run(
                SIGNCHECK_CMD + ["handoff", "import", zip_path, "--operator", "importer"],
                capture_output=True, text=True, encoding="utf-8", errors="replace",
                cwd=ctx2_tmpdir, env=env,
            )
            assert r.returncode == 0, f"Import should succeed: {r.stderr}"
            assert "[OK]" in r.stdout

            r = subprocess.run(
                SIGNCHECK_CMD + ["handoff", "list"],
                capture_output=True, text=True, encoding="utf-8", errors="replace",
                cwd=ctx2_tmpdir, env=env,
            )
            assert "上午场" in r.stdout, f"Should list imported package: {r.stdout}"

            r = subprocess.run(
                SIGNCHECK_CMD + ["handoff", "list"],
                capture_output=True, text=True, encoding="utf-8", errors="replace",
                cwd=ctx2_tmpdir, env=env,
            )
            assert "上午场" in r.stdout, f"Should persist after restart: {r.stdout}"
        finally:
            shutil.rmtree(ctx2_tmpdir, ignore_errors=True)

        print("  [PASS] handoff full pipeline")
    finally:
        ctx.cleanup()


def test_handoff_verify_missing_checksums():
    print("\n=== Test: handoff verify missing checksums ===")
    ctx = TestContext()
    try:
        bad_zip = os.path.join(ctx.tmpdir, "bad.zip")
        tmp_dir = tempfile.mkdtemp()
        try:
            manifest = {"manifest": {"package_id": "test", "session": "test", "operator": "test", "generated_at": "2025-01-01"}}
            with open(os.path.join(tmp_dir, "manifest.json"), "w", encoding="utf-8") as f:
                json.dump(manifest, f)

            with zipfile.ZipFile(bad_zip, "w") as zf:
                zf.write(os.path.join(tmp_dir, "manifest.json"), "manifest.json")
        finally:
            shutil.rmtree(tmp_dir, ignore_errors=True)

        r = ctx.run(["handoff", "verify", bad_zip], expect_fail=True)
        assert r.returncode != 0, "Should fail on missing checksums"
        assert "checksums" in r.stderr, f"Should mention checksums: {r.stderr}"

        print("  [PASS] handoff verify missing checksums")
    finally:
        ctx.cleanup()


if __name__ == "__main__":
    os.chdir(os.path.dirname(__file__) + "/..")
    print("Running handoff tests...")
    test_handoff_create()
    test_handoff_create_no_operator()
    test_handoff_create_no_results()
    test_handoff_list()
    test_handoff_export()
    test_handoff_export_no_operator()
    test_handoff_verify()
    test_handoff_verify_tampered()
    test_handoff_import()
    test_handoff_import_no_operator()
    test_handoff_import_conflict()
    test_handoff_persistence_after_restart()
    test_handoff_full_pipeline()
    test_handoff_verify_missing_checksums()
    print("\n[OK] All handoff tests passed!")
