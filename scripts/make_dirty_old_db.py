import sqlite3
import os
import sys

DB_DIR = os.path.join(os.path.dirname(__file__), "..", ".signcheck")
DB_PATH = os.path.join(DB_DIR, "signcheck.db")

os.makedirs(DB_DIR, exist_ok=True)
if os.path.exists(DB_PATH):
    os.remove(DB_PATH)

conn = sqlite3.connect(DB_PATH)
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

cur = conn.execute("PRAGMA index_list(enrollments)")
for idx in cur.fetchall():
    if idx["origin"] == "u":
        print("[FAIL] enrollments has UNIQUE constraint, not an old schema")
        sys.exit(1)

cur = conn.execute("PRAGMA index_list(signins)")
for idx in cur.fetchall():
    if idx["origin"] == "u":
        print("[FAIL] signins has UNIQUE constraint, not an old schema")
        sys.exit(1)

enrollments_data = [
    ("张三", "13800001111", "上午场", "enroll.csv", 2, "2025-01-01T09:00:00"),
    ("张三", "13800001111", "上午场", "enroll.csv", 2, "2025-01-01T09:05:00"),
    ("张三", "13800001111", "上午场", "enroll.csv", 2, "2025-01-01T09:10:00"),
    ("李四", "13800002222", "上午场", "enroll.csv", 3, "2025-01-01T09:00:00"),
    ("李四", "13800002222", "上午场", "enroll.csv", 3, "2025-01-01T09:06:00"),
    ("王五", "13800003333", "下午场", "enroll.csv", 4, "2025-01-01T09:00:00"),
]
for r in enrollments_data:
    conn.execute(
        "INSERT INTO enrollments (name,phone,session,source_file,source_row,imported_at) VALUES (?,?,?,?,?,?)",
        r,
    )

signins_data = [
    ("张三", "13800001111", "上午场", "09:01:15", "signin.csv", 2, "2025-01-01T10:00:00"),
    ("张三", "13800001111", "上午场", "09:01:15", "signin.csv", 2, "2025-01-01T10:01:00"),
    ("李四", "13800002222", "上午场", "09:05:40", "signin.csv", 3, "2025-01-01T10:00:00"),
]
for r in signins_data:
    conn.execute(
        "INSERT INTO signins (name,phone,session,scan_time,source_file,source_row,imported_at) VALUES (?,?,?,?,?,?,?)",
        r,
    )

conn.commit()

cur = conn.execute("SELECT COUNT(*) as c FROM enrollments")
print(f"Enrollments before: {cur.fetchone()['c']}")
cur = conn.execute("SELECT COUNT(*) as c FROM signins")
print(f"Signins before: {cur.fetchone()['c']}")

conn.close()
print(f"[OK] Dirty old schema DB created at {DB_PATH}")
