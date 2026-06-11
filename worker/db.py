import sqlite3
import json
from pathlib import Path
from config import DATABASE_PATH


def get_db() -> sqlite3.Connection:
    conn = sqlite3.connect(DATABASE_PATH, timeout=10)
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA busy_timeout=10000")
    conn.row_factory = sqlite3.Row
    return conn


def init_db():
    conn = get_db()
    conn.executescript("""
        CREATE TABLE IF NOT EXISTS local_clients (
            slug       TEXT PRIMARY KEY,
            status     TEXT DEFAULT 'active',
            created_at TEXT DEFAULT (datetime('now'))
        );

        CREATE TABLE IF NOT EXISTS local_runs (
            id           TEXT PRIMARY KEY,
            run_id       TEXT NOT NULL,
            client_slug  TEXT NOT NULL,
            script       TEXT NOT NULL,
            status       TEXT DEFAULT 'running',
            pid          INTEGER,
            log_path     TEXT,
            output_files TEXT DEFAULT '[]',
            error_msg    TEXT,
            started_at   TEXT DEFAULT (datetime('now')),
            finished_at  TEXT
        );
    """)
    conn.commit()
    conn.close()


def get_client(slug: str) -> sqlite3.Row | None:
    conn = get_db()
    row = conn.execute("SELECT * FROM local_clients WHERE slug = ?", (slug,)).fetchone()
    conn.close()
    return row


def insert_client(slug: str):
    conn = get_db()
    conn.execute("INSERT INTO local_clients (slug) VALUES (?)", (slug,))
    conn.commit()
    conn.close()


def update_client_status(slug: str, status: str):
    conn = get_db()
    conn.execute("UPDATE local_clients SET status = ? WHERE slug = ?", (status, slug))
    conn.commit()
    conn.close()


def delete_client(slug: str):
    conn = get_db()
    conn.execute("DELETE FROM local_clients WHERE slug = ?", (slug,))
    conn.commit()
    conn.close()


def insert_run(vps_run_id: str, run_id: str, client_slug: str, script: str, log_path: str):
    conn = get_db()
    conn.execute(
        "INSERT INTO local_runs (id, run_id, client_slug, script, log_path) VALUES (?, ?, ?, ?, ?)",
        (vps_run_id, run_id, client_slug, script, log_path),
    )
    conn.commit()
    conn.close()


def update_run_pid(vps_run_id: str, pid: int):
    conn = get_db()
    conn.execute("UPDATE local_runs SET pid = ? WHERE id = ?", (pid, vps_run_id))
    conn.commit()
    conn.close()


def finish_run(vps_run_id: str, status: str, output_files: list[str], error_msg: str | None):
    conn = get_db()
    conn.execute(
        """UPDATE local_runs
           SET status = ?, output_files = ?, error_msg = ?, finished_at = datetime('now')
           WHERE id = ?""",
        (status, json.dumps(output_files), error_msg, vps_run_id),
    )
    conn.commit()
    conn.close()


def get_run(vps_run_id: str) -> sqlite3.Row | None:
    conn = get_db()
    row = conn.execute("SELECT * FROM local_runs WHERE id = ?", (vps_run_id,)).fetchone()
    conn.close()
    return row


def count_active_runs(client_slug: str) -> int:
    conn = get_db()
    count = conn.execute(
        "SELECT COUNT(*) FROM local_runs WHERE client_slug = ? AND status = 'running'",
        (client_slug,),
    ).fetchone()[0]
    conn.close()
    return count


def count_all_active_runs() -> int:
    conn = get_db()
    count = conn.execute(
        "SELECT COUNT(*) FROM local_runs WHERE status = 'running'"
    ).fetchone()[0]
    conn.close()
    return count


def count_active_clients() -> int:
    conn = get_db()
    count = conn.execute(
        "SELECT COUNT(*) FROM local_clients WHERE status = 'active'"
    ).fetchone()[0]
    conn.close()
    return count
