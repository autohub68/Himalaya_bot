import sqlite3
from contextlib import contextmanager
from datetime import datetime, timezone

from .config import settings


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


@contextmanager
def connection():
    conn = sqlite3.connect(settings.database_path)
    conn.row_factory = sqlite3.Row
    try:
        yield conn
        conn.commit()
    finally:
        conn.close()


def init_db() -> None:
    with connection() as conn:
        conn.executescript(
            """
            CREATE TABLE IF NOT EXISTS candidates (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                external_id TEXT UNIQUE NOT NULL,
                name TEXT NOT NULL,
                profile_url TEXT DEFAULT '',
                summary TEXT DEFAULT '',
                stack_json TEXT DEFAULT '[]',
                category TEXT DEFAULT 'unknown',
                source_json TEXT DEFAULT '{}',
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS messages (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                candidate_id INTEGER NOT NULL REFERENCES candidates(id),
                direction TEXT NOT NULL CHECK(direction IN ('outbound', 'inbound')),
                body TEXT NOT NULL,
                status TEXT NOT NULL DEFAULT 'queued',
                send_after TEXT,
                sent_at TEXT,
                error TEXT,
                created_at TEXT NOT NULL
            );
            CREATE INDEX IF NOT EXISTS idx_messages_ready ON messages(status, send_after);
            CREATE TABLE IF NOT EXISTS oauth_state (
                state TEXT PRIMARY KEY,
                code_verifier TEXT NOT NULL,
                created_at TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS oauth_tokens (
                id INTEGER PRIMARY KEY CHECK(id = 1),
                access_token TEXT NOT NULL,
                refresh_token TEXT,
                expires_at REAL,
                client_id TEXT NOT NULL,
                client_secret TEXT
            );
            """
        )
        columns = {row[1] for row in conn.execute("PRAGMA table_info(messages)")}
        candidate_columns = {row[1] for row in conn.execute("PRAGMA table_info(candidates)")}
        if "error" not in columns:
            conn.execute("ALTER TABLE messages ADD COLUMN error TEXT")
        if "external_id" not in columns:
            conn.execute("ALTER TABLE messages ADD COLUMN external_id TEXT")
        if "read_at" not in columns:
            conn.execute("ALTER TABLE messages ADD COLUMN read_at TEXT")
        if "github_username" not in candidate_columns:
            conn.execute("ALTER TABLE candidates ADD COLUMN github_username TEXT")
        if "github_email" not in candidate_columns:
            conn.execute("ALTER TABLE candidates ADD COLUMN github_email TEXT")
        if "github_invited_at" not in candidate_columns:
            conn.execute("ALTER TABLE candidates ADD COLUMN github_invited_at TEXT")
        conn.execute("CREATE UNIQUE INDEX IF NOT EXISTS idx_messages_external_id ON messages(external_id) WHERE external_id IS NOT NULL")