def create_app():
    from flask import Flask, session
    from config import SECRET_KEY

    app = Flask(__name__, template_folder="../templates", static_folder="../static")
    app.secret_key = SECRET_KEY

    # Migrations: add columns that may not exist in older DBs
    from app.db import get_db
    db = get_db()
    try:
        db.execute("ALTER TABLE operations_log ADD COLUMN params TEXT")
        db.commit()
    except Exception:
        pass  # column already exists
    try:
        db.execute("ALTER TABLE emails ADD COLUMN is_primary INTEGER DEFAULT 0")
        db.execute("""
            UPDATE emails SET is_primary = 1
            WHERE id IN (
                SELECT MIN(id) FROM emails WHERE business_id IS NOT NULL GROUP BY business_id
            ) AND is_primary = 0
        """)
        db.commit()
    except Exception:
        pass  # column already exists

    # Workspace migrations
    try:
        db.execute("""
            CREATE TABLE IF NOT EXISTS workspaces (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                name TEXT NOT NULL UNIQUE,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
        """)
        db.execute("INSERT OR IGNORE INTO workspaces (id, name) VALUES (1, 'Starlinkee')")
        db.commit()
    except Exception:
        pass
    for col, table in [
        ("workspace_id", "businesses"),
        ("workspace_id", "campaigns"),
        ("workspace_id", "mailboxes"),
        ("workspace_id", "scrape_areas"),
    ]:
        try:
            db.execute(f"ALTER TABLE {table} ADD COLUMN {col} INTEGER DEFAULT 1")
            db.commit()
        except Exception:
            pass
    try:
        db.execute("UPDATE businesses SET workspace_id = 1 WHERE workspace_id IS NULL")
        db.execute("UPDATE campaigns SET workspace_id = 1 WHERE workspace_id IS NULL")
        db.execute("UPDATE mailboxes SET workspace_id = 1 WHERE workspace_id IS NULL")
        db.execute("UPDATE scrape_areas SET workspace_id = 1 WHERE workspace_id IS NULL")
        db.commit()
    except Exception:
        pass
    db.close()

    from app.routes import main_bp
    app.register_blueprint(main_bp)

    @app.context_processor
    def inject_workspace():
        workspace_id = session.get("workspace_id", 1)
        db = get_db()
        workspaces = db.execute("SELECT id, name FROM workspaces ORDER BY id").fetchall()
        current = db.execute("SELECT id, name FROM workspaces WHERE id = ?", (workspace_id,)).fetchone()
        db.close()
        if not current and workspaces:
            current = workspaces[0]
        return {
            "all_workspaces": [dict(w) for w in workspaces],
            "current_workspace": dict(current) if current else {"id": 1, "name": "Starlinkee"},
        }

    return app
