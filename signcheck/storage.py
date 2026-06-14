import sqlite3
import json
import os
from datetime import datetime
from typing import List, Optional, Dict, Any, Tuple
from .models import (
    EnrollmentRecord,
    SigninRecord,
    MatchRule,
    ReconcileResult,
    ImportErrorRecord,
    UndoAction,
    FieldMapping,
    SessionRecord,
)

def _resolve_db_dir():
    env_dir = os.environ.get("SIGNCHECK_DB_DIR")
    if env_dir:
        return os.path.join(env_dir, ".signcheck")
    return os.path.join(os.getcwd(), ".signcheck")

SCHEMA = """
CREATE TABLE IF NOT EXISTS enrollments (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    name TEXT NOT NULL,
    phone TEXT,
    session TEXT NOT NULL,
    source_file TEXT,
    source_row INTEGER,
    imported_at TEXT NOT NULL,
    UNIQUE(phone, session)
);

CREATE TABLE IF NOT EXISTS signins (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    name TEXT NOT NULL,
    phone TEXT,
    session TEXT NOT NULL,
    scan_time TEXT,
    source_file TEXT,
    source_row INTEGER,
    imported_at TEXT NOT NULL,
    UNIQUE(phone, session, scan_time)
);

CREATE TABLE IF NOT EXISTS rules (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    field_name TEXT NOT NULL,
    match_type TEXT NOT NULL DEFAULT 'exact',
    threshold REAL,
    priority INTEGER NOT NULL DEFAULT 0,
    imported_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS field_mapping (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    mapping_type TEXT NOT NULL,
    field_name TEXT NOT NULL,
    csv_column TEXT NOT NULL,
    imported_at TEXT NOT NULL
);

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
);

CREATE TABLE IF NOT EXISTS undo_history (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    action_type TEXT NOT NULL,
    action_data TEXT NOT NULL,
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS import_errors (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    source_type TEXT NOT NULL,
    source_file TEXT,
    row_number INTEGER NOT NULL,
    error_type TEXT NOT NULL,
    error_message TEXT NOT NULL,
    raw_data TEXT
);

CREATE TABLE IF NOT EXISTS saved_views (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    name TEXT NOT NULL UNIQUE,
    filters TEXT NOT NULL,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS sessions (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    name TEXT NOT NULL UNIQUE,
    status TEXT NOT NULL DEFAULT 'open',
    start_time TEXT,
    end_time TEXT,
    description TEXT,
    created_at TEXT NOT NULL,
    closed_at TEXT
);
"""


class Storage:
    def __init__(self, db_path: Optional[str] = None):
        if db_path is None:
            db_dir = _resolve_db_dir()
            db_path = os.path.join(db_dir, "signcheck.db")
        self.db_path = db_path
        os.makedirs(os.path.dirname(db_path), exist_ok=True)
        self.conn = sqlite3.connect(db_path)
        self.conn.row_factory = sqlite3.Row
        self.conn.execute("PRAGMA journal_mode=WAL")
        self.conn.execute("PRAGMA foreign_keys=ON")
        self._init_tables()

    def _init_tables(self):
        self.conn.executescript(SCHEMA)
        self._migrate_tables()
        self.conn.commit()

    def _migrate_tables(self):
        self._ensure_import_errors_table()
        dropped_enroll = self._migrate_unique_constraint("enrollments", ["phone", "session"])
        dropped_signin = self._migrate_unique_constraint("signins", ["phone", "session", "scan_time"])
        if dropped_enroll or dropped_signin:
            self.conn.commit()

    def _ensure_import_errors_table(self):
        self.conn.execute("""
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

    def _migrate_unique_constraint(self, table: str, unique_cols: List[str]) -> int:
        cur = self.conn.execute(f"PRAGMA index_list({table})")
        has_unique = False
        for idx in cur.fetchall():
            if idx["origin"] == "u":
                has_unique = True
                break
        if has_unique:
            return 0

        cols_sql = ",".join(unique_cols)
        tmp_table = f"{table}_tmp"
        cur = self.conn.execute(f"SELECT sql FROM sqlite_master WHERE type='table' AND name='{table}'")
        row = cur.fetchone()
        if row is None:
            return 0
        old_sql = row["sql"]
        new_sql = old_sql.rstrip(")") + f", UNIQUE({cols_sql}))"

        cur = self.conn.execute(f"PRAGMA table_info({table})")
        col_names = [r["name"] for r in cur.fetchall()]
        cols_csv = ",".join(col_names)

        cur = self.conn.execute(f"SELECT COUNT(*) as c FROM {table}")
        total_before = cur.fetchone()["c"]
        if total_before == 0:
            self.conn.execute(f"DROP TABLE IF EXISTS {table}")
            self.conn.execute(new_sql)
            return 0

        group_cols = ",".join(unique_cols)

        try:
            self.conn.execute(f"ALTER TABLE {table} RENAME TO {tmp_table}")
        except Exception:
            return 0

        self.conn.execute(new_sql)

        self.conn.execute(f"""
            INSERT INTO {table} ({cols_csv})
            SELECT t.{cols_csv.replace(',', ',t.')}
            FROM {tmp_table} t
            WHERE t.id IN (
                SELECT MIN(id) FROM {tmp_table}
                GROUP BY {group_cols}
            )
        """)

        cur = self.conn.execute(f"SELECT COUNT(*) as c FROM {table}")
        total_after = cur.fetchone()["c"]
        dropped_count = total_before - total_after

        if dropped_count > 0:
            cur = self.conn.execute(f"""
                SELECT t.* FROM {tmp_table} t
                WHERE t.id NOT IN (
                    SELECT MIN(id) FROM {tmp_table}
                    GROUP BY {group_cols}
                )
            """)
            duplicate_rows = cur.fetchall()
            for dup in duplicate_rows:
                dup_dict = dict(dup)
                raw = json.dumps({k: v for k, v in dup_dict.items() if v}, ensure_ascii=False)
                key_vals = ", ".join(f"{c}={dup_dict.get(c, '')!r}" for c in unique_cols)
                matched_keep_id = self.conn.execute(f"""
                    SELECT MIN(id) as keep_id FROM {tmp_table}
                    WHERE {' AND '.join(f'{c}=?' for c in unique_cols)}
                """, tuple(dup_dict.get(c) for c in unique_cols)).fetchone()
                keep_id = matched_keep_id["keep_id"] if matched_keep_id else "?"
                message = f"迁移去重：{table} 表重复记录 ({key_vals})，已丢弃 ID={dup_dict.get('id')}，保留 ID={keep_id}"
                self.conn.execute(
                    "INSERT INTO import_errors (source_type,source_file,row_number,error_type,error_message,raw_data) VALUES (?,?,?,?,?,?)",
                    (
                        f"migration_{table}",
                        dup_dict.get("source_file") or "",
                        dup_dict.get("source_row") or 0,
                        "duplicate_dropped",
                        message,
                        raw,
                    ),
                )

        self.conn.execute(f"DROP TABLE {tmp_table}")
        return dropped_count

    def close(self):
        self.conn.close()

    def _now(self) -> str:
        return datetime.now().isoformat()

    # ── Enrollment ──────────────────────────────────────────────

    def add_enrollment(self, record: EnrollmentRecord) -> int:
        cur = self.conn.execute(
            "INSERT INTO enrollments (name,phone,session,source_file,source_row,imported_at) VALUES (?,?,?,?,?,?)",
            (record.name, record.phone, record.session, record.source_file, record.source_row, self._now()),
        )
        self.conn.commit()
        return cur.lastrowid

    def add_enrollments(self, records: List[EnrollmentRecord]) -> Tuple[List[int], List[int]]:
        new_ids: List[int] = []
        existing_ids: List[int] = []
        now = self._now()
        for r in records:
            existing = self.conn.execute(
                "SELECT id FROM enrollments WHERE phone=? AND session=?",
                (r.phone, r.session),
            ).fetchone()
            if existing is not None:
                existing_ids.append(existing["id"])
                continue
            cur = self.conn.execute(
                "INSERT INTO enrollments (name,phone,session,source_file,source_row,imported_at) VALUES (?,?,?,?,?,?)",
                (r.name, r.phone, r.session, r.source_file, r.source_row, now),
            )
            new_ids.append(cur.lastrowid)
        self.conn.commit()
        return new_ids, existing_ids

    def find_enrollment_id(self, phone: str, session: str) -> Optional[int]:
        row = self.conn.execute(
            "SELECT id FROM enrollments WHERE phone=? AND session=?",
            (phone, session),
        ).fetchone()
        return row["id"] if row else None

    def get_all_enrollments(self) -> List[EnrollmentRecord]:
        rows = self.conn.execute("SELECT * FROM enrollments ORDER BY id").fetchall()
        return [EnrollmentRecord(**dict(r)) for r in rows]

    def get_enrollment_sessions(self) -> set:
        rows = self.conn.execute("SELECT DISTINCT session FROM enrollments").fetchall()
        return {r["session"] for r in rows}

    def delete_enrollments_by_ids(self, ids: List[int]):
        if not ids:
            return
        placeholders = ",".join("?" * len(ids))
        self.conn.execute(f"DELETE FROM enrollments WHERE id IN ({placeholders})", ids)
        self.conn.commit()

    def clear_enrollments(self):
        self.conn.execute("DELETE FROM enrollments")
        self.conn.commit()

    # ── Sign-in ────────────────────────────────────────────────

    def add_signin(self, record: SigninRecord) -> int:
        cur = self.conn.execute(
            "INSERT INTO signins (name,phone,session,scan_time,source_file,source_row,imported_at) VALUES (?,?,?,?,?,?,?)",
            (record.name, record.phone, record.session, record.scan_time, record.source_file, record.source_row, self._now()),
        )
        self.conn.commit()
        return cur.lastrowid

    def add_signins(self, records: List[SigninRecord]) -> Tuple[List[int], List[int]]:
        new_ids: List[int] = []
        existing_ids: List[int] = []
        now = self._now()
        for r in records:
            existing = self.conn.execute(
                "SELECT id FROM signins WHERE phone=? AND session=? AND scan_time=?",
                (r.phone, r.session, r.scan_time),
            ).fetchone()
            if existing is not None:
                existing_ids.append(existing["id"])
                continue
            cur = self.conn.execute(
                "INSERT INTO signins (name,phone,session,scan_time,source_file,source_row,imported_at) VALUES (?,?,?,?,?,?,?)",
                (r.name, r.phone, r.session, r.scan_time, r.source_file, r.source_row, now),
            )
            new_ids.append(cur.lastrowid)
        self.conn.commit()
        return new_ids, existing_ids

    def find_signin_id(self, phone: str, session: str, scan_time: str) -> Optional[int]:
        row = self.conn.execute(
            "SELECT id FROM signins WHERE phone=? AND session=? AND scan_time=?",
            (phone, session, scan_time),
        ).fetchone()
        return row["id"] if row else None

    def get_all_signins(self) -> List[SigninRecord]:
        rows = self.conn.execute("SELECT * FROM signins ORDER BY id").fetchall()
        return [SigninRecord(**dict(r)) for r in rows]

    def delete_signins_by_ids(self, ids: List[int]):
        if not ids:
            return
        placeholders = ",".join("?" * len(ids))
        self.conn.execute(f"DELETE FROM signins WHERE id IN ({placeholders})", ids)
        self.conn.commit()

    def clear_signins(self):
        self.conn.execute("DELETE FROM signins")
        self.conn.commit()

    # ── Rules ──────────────────────────────────────────────────

    def add_rules(self, rules: List[MatchRule]) -> List[int]:
        ids = []
        now = self._now()
        for r in rules:
            cur = self.conn.execute(
                "INSERT INTO rules (field_name,match_type,threshold,priority,imported_at) VALUES (?,?,?,?,?)",
                (r.field_name, r.match_type, r.threshold, r.priority, now),
            )
            ids.append(cur.lastrowid)
        self.conn.commit()
        return ids

    def get_all_rules(self) -> List[MatchRule]:
        rows = self.conn.execute("SELECT * FROM rules ORDER BY priority").fetchall()
        return [MatchRule(**dict(r)) for r in rows]

    def clear_rules(self):
        self.conn.execute("DELETE FROM rules")
        self.conn.commit()

    # ── Field Mapping ──────────────────────────────────────────

    def save_field_mapping(self, mapping: FieldMapping):
        self.conn.execute("DELETE FROM field_mapping")
        now = self._now()
        for field_name, csv_col in mapping.enroll.items():
            self.conn.execute(
                "INSERT INTO field_mapping (mapping_type,field_name,csv_column,imported_at) VALUES (?,?,?,?)",
                ("enroll", field_name, csv_col, now),
            )
        for field_name, csv_col in mapping.signin.items():
            self.conn.execute(
                "INSERT INTO field_mapping (mapping_type,field_name,csv_column,imported_at) VALUES (?,?,?,?)",
                ("signin", field_name, csv_col, now),
            )
        self.conn.commit()

    def get_field_mapping(self) -> FieldMapping:
        mapping = FieldMapping()
        rows = self.conn.execute("SELECT * FROM field_mapping").fetchall()
        for r in rows:
            if r["mapping_type"] == "enroll":
                mapping.enroll[r["field_name"]] = r["csv_column"]
            elif r["mapping_type"] == "signin":
                mapping.signin[r["field_name"]] = r["csv_column"]
        return mapping

    # ── Reconcile Results ──────────────────────────────────────

    def add_reconcile_result(self, result: ReconcileResult) -> int:
        cur = self.conn.execute(
            "INSERT INTO reconcile_results (enroll_id,signin_id,name,phone,session,status,manual_mark,notes,created_at) VALUES (?,?,?,?,?,?,?,?,?)",
            (result.enroll_id, result.signin_id, result.name, result.phone, result.session, result.status, result.manual_mark, result.notes, self._now()),
        )
        self.conn.commit()
        return cur.lastrowid

    def add_reconcile_results(self, results: List[ReconcileResult]) -> List[int]:
        ids = []
        now = self._now()
        for r in results:
            cur = self.conn.execute(
                "INSERT INTO reconcile_results (enroll_id,signin_id,name,phone,session,status,manual_mark,notes,created_at) VALUES (?,?,?,?,?,?,?,?,?)",
                (r.enroll_id, r.signin_id, r.name, r.phone, r.session, r.status, r.manual_mark, r.notes, now),
            )
            ids.append(cur.lastrowid)
        self.conn.commit()
        return ids

    def get_all_reconcile_results(self) -> List[ReconcileResult]:
        rows = self.conn.execute("SELECT * FROM reconcile_results ORDER BY id").fetchall()
        return [ReconcileResult(**dict(r)) for r in rows]

    def get_reconcile_result_by_id(self, result_id: int) -> Optional[ReconcileResult]:
        row = self.conn.execute("SELECT * FROM reconcile_results WHERE id=?", (result_id,)).fetchone()
        if row is None:
            return None
        return ReconcileResult(**dict(row))

    def update_reconcile_result_mark(self, result_id: int, manual_mark: Optional[str], notes: Optional[str]):
        self.conn.execute(
            "UPDATE reconcile_results SET manual_mark=?, notes=? WHERE id=?",
            (manual_mark, notes, result_id),
        )
        self.conn.commit()

    def clear_reconcile_results(self):
        self.conn.execute("DELETE FROM reconcile_results")
        self.conn.commit()

    def count_reconcile_results_by_status(self) -> Dict[str, int]:
        rows = self.conn.execute("SELECT status, COUNT(*) as cnt FROM reconcile_results GROUP BY status").fetchall()
        return {r["status"]: r["cnt"] for r in rows}

    def count_reconcile_results_by_session_and_status(self) -> List[Dict[str, Any]]:
        rows = self.conn.execute(
            "SELECT session, status, COUNT(*) as cnt FROM reconcile_results GROUP BY session, status ORDER BY session, status"
        ).fetchall()
        return [{"session": r["session"], "status": r["status"], "count": r["cnt"]} for r in rows]

    def get_reconcile_sessions(self) -> List[str]:
        rows = self.conn.execute("SELECT DISTINCT session FROM reconcile_results ORDER BY session").fetchall()
        return [r["session"] for r in rows]

    def query_reconcile_results(
        self,
        status: Optional[str] = None,
        session: Optional[str] = None,
        mark: Optional[str] = None,
        keyword: Optional[str] = None,
        limit: Optional[int] = None,
        sort_by: str = "id",
        sort_order: str = "asc",
    ) -> List[ReconcileResult]:
        sql = "SELECT * FROM reconcile_results"
        conditions = []
        params = []
        if status:
            conditions.append("status = ?")
            params.append(status)
        if session:
            conditions.append("session = ?")
            params.append(session)
        if mark:
            conditions.append("manual_mark = ?")
            params.append(mark)
        if keyword:
            conditions.append("(name LIKE ? OR phone LIKE ?)")
            params.extend([f"%{keyword}%", f"%{keyword}%"])
        if conditions:
            sql += " WHERE " + " AND ".join(conditions)
        valid_sort = {"id": "id", "status": "status"}
        order_col = valid_sort.get(sort_by, "id")
        order_dir = "DESC" if sort_order.lower() == "desc" else "ASC"
        sql += f" ORDER BY {order_col} {order_dir}"
        if limit is not None and limit > 0:
            sql += " LIMIT ?"
            params.append(limit)
        rows = self.conn.execute(sql, params).fetchall()
        return [ReconcileResult(**dict(r)) for r in rows]

    def save_view(self, name: str, filters: str, overwrite: bool = False) -> bool:
        existing = self.conn.execute("SELECT id FROM saved_views WHERE name=?", (name,)).fetchone()
        if existing:
            if not overwrite:
                return False
            self.conn.execute(
                "UPDATE saved_views SET filters=?, updated_at=? WHERE name=?",
                (filters, self._now(), name),
            )
            self.conn.commit()
            return True
        self.conn.execute(
            "INSERT INTO saved_views (name,filters,created_at,updated_at) VALUES (?,?,?,?)",
            (name, filters, self._now(), self._now()),
        )
        self.conn.commit()
        return True

    def get_view(self, name: str) -> Optional[Dict[str, Any]]:
        row = self.conn.execute("SELECT * FROM saved_views WHERE name=?", (name,)).fetchone()
        if row is None:
            return None
        return dict(row)

    def list_views(self) -> List[Dict[str, Any]]:
        rows = self.conn.execute("SELECT * FROM saved_views ORDER BY name").fetchall()
        return [dict(r) for r in rows]

    def delete_view(self, name: str) -> bool:
        cur = self.conn.execute("DELETE FROM saved_views WHERE name=?", (name,))
        self.conn.commit()
        return cur.rowcount > 0

    # ── Import Errors ──────────────────────────────────────────

    def add_import_error(self, error: ImportErrorRecord) -> int:
        cur = self.conn.execute(
            "INSERT INTO import_errors (source_type,source_file,row_number,error_type,error_message,raw_data) VALUES (?,?,?,?,?,?)",
            (error.source_type, error.source_file, error.row_number, error.error_type, error.error_message, error.raw_data),
        )
        self.conn.commit()
        return cur.lastrowid

    def add_import_errors(self, errors: List[ImportErrorRecord]) -> List[int]:
        ids = []
        for e in errors:
            cur = self.conn.execute(
                "INSERT INTO import_errors (source_type,source_file,row_number,error_type,error_message,raw_data) VALUES (?,?,?,?,?,?)",
                (e.source_type, e.source_file, e.row_number, e.error_type, e.error_message, e.raw_data),
            )
            ids.append(cur.lastrowid)
        self.conn.commit()
        return ids

    def get_all_import_errors(self) -> List[ImportErrorRecord]:
        rows = self.conn.execute("SELECT * FROM import_errors ORDER BY id").fetchall()
        return [ImportErrorRecord(**dict(r)) for r in rows]

    def clear_import_errors(self):
        self.conn.execute("DELETE FROM import_errors")
        self.conn.commit()

    # ── Undo History ───────────────────────────────────────────

    def add_undo_action(self, action_type: str, action_data: str) -> int:
        cur = self.conn.execute(
            "INSERT INTO undo_history (action_type,action_data,created_at) VALUES (?,?,?)",
            (action_type, action_data, self._now()),
        )
        self.conn.commit()
        return cur.lastrowid

    def get_last_undo_action(self) -> Optional[UndoAction]:
        row = self.conn.execute("SELECT * FROM undo_history ORDER BY id DESC LIMIT 1").fetchone()
        if row is None:
            return None
        return UndoAction(**dict(row))

    def pop_last_undo_action(self) -> Optional[UndoAction]:
        action = self.get_last_undo_action()
        if action is not None:
            self.conn.execute("DELETE FROM undo_history WHERE id=?", (action.id,))
            self.conn.commit()
        return action

    def get_undo_count(self) -> int:
        row = self.conn.execute("SELECT COUNT(*) as cnt FROM undo_history").fetchone()
        return row["cnt"]

    # ── Sessions ───────────────────────────────────────────────

    def create_session(
        self,
        name: str,
        start_time: Optional[str] = None,
        end_time: Optional[str] = None,
        description: Optional[str] = None,
    ) -> Optional[int]:
        existing = self.conn.execute("SELECT id FROM sessions WHERE name=?", (name,)).fetchone()
        if existing:
            return None
        cur = self.conn.execute(
            "INSERT INTO sessions (name, status, start_time, end_time, description, created_at) VALUES (?, 'open', ?, ?, ?, ?)",
            (name, start_time, end_time, description, self._now()),
        )
        self.conn.commit()
        return cur.lastrowid

    def get_session(self, name: str) -> Optional[SessionRecord]:
        row = self.conn.execute("SELECT * FROM sessions WHERE name=?", (name,)).fetchone()
        if row is None:
            return None
        return SessionRecord(**dict(row))

    def get_all_sessions(self) -> List[SessionRecord]:
        rows = self.conn.execute("SELECT * FROM sessions ORDER BY created_at DESC").fetchall()
        return [SessionRecord(**dict(r)) for r in rows]

    def close_session(self, name: str) -> bool:
        cur = self.conn.execute(
            "UPDATE sessions SET status='closed', closed_at=? WHERE name=? AND status='open'",
            (self._now(), name),
        )
        self.conn.commit()
        return cur.rowcount > 0

    def is_session_closed(self, name: str) -> bool:
        row = self.conn.execute("SELECT status FROM sessions WHERE name=?", (name,)).fetchone()
        if row is None:
            return False
        return row["status"] == "closed"

    def get_open_sessions(self) -> List[str]:
        rows = self.conn.execute("SELECT name FROM sessions WHERE status='open' ORDER BY name").fetchall()
        return [r["name"] for r in rows]

    def get_session_names(self) -> List[str]:
        rows = self.conn.execute("SELECT name FROM sessions ORDER BY name").fetchall()
        return [r["name"] for r in rows]

    # ── Statistics ─────────────────────────────────────────────

    def get_stats(self) -> Dict[str, Any]:
        enroll_cnt = self.conn.execute("SELECT COUNT(*) as c FROM enrollments").fetchone()["c"]
        signin_cnt = self.conn.execute("SELECT COUNT(*) as c FROM signins").fetchone()["c"]
        rules_cnt = self.conn.execute("SELECT COUNT(*) as c FROM rules").fetchone()["c"]
        result_cnt = self.conn.execute("SELECT COUNT(*) as c FROM reconcile_results").fetchone()["c"]
        undo_cnt = self.conn.execute("SELECT COUNT(*) as c FROM undo_history").fetchone()["c"]
        error_cnt = self.conn.execute("SELECT COUNT(*) as c FROM import_errors").fetchone()["c"]
        status_counts = self.count_reconcile_results_by_status()
        return {
            "enrollment_count": enroll_cnt,
            "signin_count": signin_cnt,
            "rules_count": rules_cnt,
            "result_count": result_cnt,
            "undo_count": undo_cnt,
            "error_count": error_cnt,
            "status_counts": status_counts,
        }
