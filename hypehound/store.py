import sqlite3
from contextlib import contextmanager
from datetime import datetime

from .config import settings


def init_db() -> None:
    with _conn() as conn:
        conn.executescript("""
            CREATE TABLE IF NOT EXISTS emails (
                id          INTEGER PRIMARY KEY,
                message_id  TEXT UNIQUE NOT NULL,
                sender      TEXT NOT NULL,
                subject     TEXT NOT NULL,
                received_at TEXT NOT NULL,
                processed_at TEXT,
                raw_text    TEXT,
                deleted     INTEGER DEFAULT 0
            );

            CREATE TABLE IF NOT EXISTS items (
                id          INTEGER PRIMARY KEY,
                email_id    INTEGER NOT NULL REFERENCES emails(id),
                category    TEXT NOT NULL,
                headline    TEXT NOT NULL,
                summary     TEXT NOT NULL
            );

            CREATE TABLE IF NOT EXISTS citations (
                id      INTEGER PRIMARY KEY,
                item_id INTEGER NOT NULL REFERENCES items(id),
                url     TEXT NOT NULL,
                title   TEXT
            );

            CREATE TABLE IF NOT EXISTS digests (
                id           INTEGER PRIMARY KEY,
                period_start TEXT NOT NULL,
                period_end   TEXT NOT NULL,
                generated_at TEXT NOT NULL,
                content      TEXT NOT NULL
            );

            CREATE TABLE IF NOT EXISTS digest_items (
                digest_id INTEGER NOT NULL REFERENCES digests(id),
                item_id   INTEGER NOT NULL REFERENCES items(id),
                PRIMARY KEY (digest_id, item_id)
            );
        """)


@contextmanager
def _conn():
    conn = sqlite3.connect(settings.db_path)
    conn.row_factory = sqlite3.Row
    try:
        yield conn
        conn.commit()
    finally:
        conn.close()


def get_stats() -> dict:
    with _conn() as conn:
        emails = conn.execute("SELECT COUNT(*) FROM emails WHERE processed_at IS NOT NULL").fetchone()[0]
        items = conn.execute("SELECT COUNT(*) FROM items").fetchone()[0]
        digested = conn.execute("SELECT COUNT(*) FROM digest_items").fetchone()[0]
        pending = items - digested
        last_ingest = conn.execute(
            "SELECT MAX(processed_at) FROM emails WHERE processed_at IS NOT NULL"
        ).fetchone()[0]
        last_digest = conn.execute(
            "SELECT MAX(generated_at) FROM digests"
        ).fetchone()[0]
    return {
        "emails": emails,
        "items": items,
        "pending": pending,
        "digested": digested,
        "last_ingest": last_ingest,
        "last_digest": last_digest,
    }


def is_processed(message_id: str) -> bool:
    with _conn() as conn:
        row = conn.execute(
            "SELECT processed_at FROM emails WHERE message_id = ?", (message_id,)
        ).fetchone()
        return row is not None and row["processed_at"] is not None


def save_email(message_id: str, sender: str, subject: str, received_at: datetime, raw_text: str) -> int:
    with _conn() as conn:
        conn.execute(
            "INSERT OR IGNORE INTO emails (message_id, sender, subject, received_at, raw_text) VALUES (?, ?, ?, ?, ?)",
            (message_id, sender, subject, received_at.isoformat(), raw_text),
        )
        return conn.execute(
            "SELECT id FROM emails WHERE message_id = ?", (message_id,)
        ).fetchone()["id"]


def mark_processed(email_id: int) -> None:
    with _conn() as conn:
        conn.execute(
            "UPDATE emails SET processed_at = ? WHERE id = ?",
            (datetime.now().isoformat(), email_id),
        )


def save_items(email_id: int, items: list[dict]) -> None:
    with _conn() as conn:
        for item in items:
            cursor = conn.execute(
                "INSERT INTO items (email_id, category, headline, summary) VALUES (?, ?, ?, ?)",
                (email_id, item["category"], item["headline"], item["summary"]),
            )
            item_id = cursor.lastrowid
            for citation in item.get("citations", []):
                conn.execute(
                    "INSERT INTO citations (item_id, url, title) VALUES (?, ?, ?)",
                    (item_id, citation["url"], citation.get("title")),
                )


def get_undigested_items(since: datetime) -> list[sqlite3.Row]:
    with _conn() as conn:
        return conn.execute("""
            SELECT i.id, i.category, i.headline, i.summary,
                   e.sender, e.received_at
            FROM items i
            JOIN emails e ON e.id = i.email_id
            WHERE e.received_at >= ?
              AND i.id NOT IN (SELECT item_id FROM digest_items)
            ORDER BY i.category, e.received_at
        """, (since.isoformat(),)).fetchall()


def save_digest_items(digest_id: int, item_ids: list[int]) -> None:
    with _conn() as conn:
        conn.executemany(
            "INSERT OR IGNORE INTO digest_items (digest_id, item_id) VALUES (?, ?)",
            [(digest_id, item_id) for item_id in item_ids],
        )


def get_digest_by_id(digest_id: int) -> str | None:
    with _conn() as conn:
        row = conn.execute(
            "SELECT content FROM digests WHERE id = ?", (digest_id,)
        ).fetchone()
        return row[0] if row else None


def get_latest_digest_content() -> str | None:
    with _conn() as conn:
        row = conn.execute(
            "SELECT content FROM digests ORDER BY generated_at DESC LIMIT 1"
        ).fetchone()
    return row["content"] if row else None


def save_digest(period_start: datetime, period_end: datetime, content: str) -> int:
    with _conn() as conn:
        cursor = conn.execute(
            "INSERT INTO digests (period_start, period_end, generated_at, content) VALUES (?, ?, ?, ?)",
            (period_start.isoformat(), period_end.isoformat(), datetime.now().isoformat(), content),
        )
        return cursor.lastrowid


def reset_db() -> None:
    with _conn() as conn:
        conn.executescript("""
            DELETE FROM digest_items;
            DELETE FROM digests;
            DELETE FROM citations;
            DELETE FROM items;
            DELETE FROM emails;
        """)


def create_digest(period_start: datetime, period_end: datetime) -> int:
    """Create a digest record immediately so incremental updates have an ID to write to."""
    return save_digest(period_start, period_end, "{}")


def update_digest_content(digest_id: int, content: str) -> None:
    with _conn() as conn:
        conn.execute("UPDATE digests SET content = ? WHERE id = ?", (content, digest_id))


def get_emails_in_period(start: datetime, end: datetime) -> list[sqlite3.Row]:
    with _conn() as conn:
        return conn.execute(
            "SELECT sender, subject, received_at FROM emails "
            "WHERE received_at >= ? AND received_at <= ? AND processed_at IS NOT NULL "
            "ORDER BY received_at",
            (start.isoformat(), end.isoformat()),
        ).fetchall()
