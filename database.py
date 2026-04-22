import aiosqlite
import os
from datetime import datetime

from config import DATABASE_PATH


async def init_db() -> None:
    os.makedirs(os.path.dirname(DATABASE_PATH) or ".", exist_ok=True)
    async with aiosqlite.connect(DATABASE_PATH) as db:
        await db.execute("""
            CREATE TABLE IF NOT EXISTS history (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id INTEGER NOT NULL,
                article TEXT NOT NULL,
                filter_label TEXT NOT NULL,
                reviews_count INTEGER DEFAULT 0,
                questions_count INTEGER DEFAULT 0,
                avg_rating REAL DEFAULT 0,
                reviews_file TEXT,
                questions_file TEXT,
                archive_file TEXT,
                created_at TEXT NOT NULL
            )
        """)
        await db.execute(
            "CREATE INDEX IF NOT EXISTS idx_history_user_created "
            "ON history (user_id, created_at DESC)"
        )
        await db.commit()


async def save_run(
    user_id: int,
    article: str,
    filter_label: str,
    reviews_count: int,
    questions_count: int,
    avg_rating: float,
    reviews_file: str,
    questions_file: str,
    archive_file: str,
) -> int:
    async with aiosqlite.connect(DATABASE_PATH) as db:
        cursor = await db.execute(
            """
            INSERT INTO history (
                user_id, article, filter_label, reviews_count, questions_count,
                avg_rating, reviews_file, questions_file, archive_file, created_at
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                user_id, article, filter_label, reviews_count, questions_count,
                round(float(avg_rating or 0), 2),
                reviews_file, questions_file, archive_file,
                datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
            ),
        )
        await db.commit()
        return cursor.lastrowid


async def get_history(user_id: int, limit: int = 10) -> list[dict]:
    async with aiosqlite.connect(DATABASE_PATH) as db:
        db.row_factory = aiosqlite.Row
        async with db.execute(
            "SELECT * FROM history WHERE user_id = ? "
            "ORDER BY created_at DESC LIMIT ?",
            (user_id, limit),
        ) as cursor:
            rows = await cursor.fetchall()
            return [dict(r) for r in rows]


async def get_run_by_id(run_id: int, user_id: int) -> dict | None:
    async with aiosqlite.connect(DATABASE_PATH) as db:
        db.row_factory = aiosqlite.Row
        async with db.execute(
            "SELECT * FROM history WHERE id = ? AND user_id = ?",
            (run_id, user_id),
        ) as cursor:
            row = await cursor.fetchone()
            return dict(row) if row else None
