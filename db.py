"""
База данных SnapSell Bot — SQLite (без внешних зависимостей)
"""

import sqlite3
import threading
from datetime import datetime, timedelta


class Database:
    def __init__(self, db_path: str = "snapsell.db"):
        self.db_path = db_path
        self._lock = threading.Lock()
        self._init_db()

    def _conn(self):
        conn = sqlite3.connect(self.db_path, check_same_thread=False)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA foreign_keys=ON")
        return conn

    def _init_db(self):
        with self._lock, self._conn() as conn:
            conn.executescript("""
                CREATE TABLE IF NOT EXISTS users (
                    user_id     INTEGER PRIMARY KEY,
                    username    TEXT DEFAULT '',
                    first_name  TEXT DEFAULT '',
                    plan        TEXT DEFAULT 'free',
                    free_uses   INTEGER DEFAULT 0,
                    paid_left   INTEGER DEFAULT 0,
                    pro_until   TEXT DEFAULT NULL,
                    created_at  TEXT DEFAULT (datetime('now')),
                    updated_at  TEXT DEFAULT (datetime('now'))
                );

                CREATE TABLE IF NOT EXISTS generations (
                    id          INTEGER PRIMARY KEY AUTOINCREMENT,
                    user_id     INTEGER NOT NULL,
                    product     TEXT DEFAULT '',
                    created_at  TEXT DEFAULT (datetime('now')),
                    FOREIGN KEY(user_id) REFERENCES users(user_id)
                );

                CREATE INDEX IF NOT EXISTS idx_gen_user ON generations(user_id);
            """)

    def ensure_user(self, user_id: int, username: str = "", first_name: str = ""):
        with self._lock, self._conn() as conn:
            conn.execute("""
                INSERT INTO users (user_id, username, first_name)
                VALUES (?, ?, ?)
                ON CONFLICT(user_id) DO UPDATE SET
                    username   = excluded.username,
                    first_name = excluded.first_name,
                    updated_at = datetime('now')
            """, (user_id, username, first_name))

    def get_uses(self, user_id: int) -> int:
        with self._conn() as conn:
            row = conn.execute(
                "SELECT free_uses FROM users WHERE user_id = ?", (user_id,)
            ).fetchone()
        return row["free_uses"] if row else 0

    def get_plan(self, user_id: int) -> str:
        with self._conn() as conn:
            row = conn.execute(
                "SELECT plan, pro_until FROM users WHERE user_id = ?", (user_id,)
            ).fetchone()
        if not row:
            return "free"
        if row["plan"] == "pro" and row["pro_until"]:
            if datetime.fromisoformat(row["pro_until"]) > datetime.utcnow():
                return "pro"
            else:
                # PRO истёк
                self._expire_pro(user_id)
                return "free"
        return row["plan"]

    def _expire_pro(self, user_id: int):
        with self._lock, self._conn() as conn:
            conn.execute(
                "UPDATE users SET plan='free', pro_until=NULL, updated_at=datetime('now') WHERE user_id=?",
                (user_id,)
            )

    def get_paid_remaining(self, user_id: int) -> int:
        with self._conn() as conn:
            row = conn.execute(
                "SELECT paid_left FROM users WHERE user_id = ?", (user_id,)
            ).fetchone()
        return row["paid_left"] if row else 0

    def can_generate(self, user_id: int) -> bool:
        plan = self.get_plan(user_id)
        if plan == "pro":
            return True
        if plan == "basic":
            return self.get_paid_remaining(user_id) > 0
        # free plan
        from config import config
        return self.get_uses(user_id) < config.FREE_GENERATIONS

    def increment_uses(self, user_id: int) -> int:
        """Списываем одну генерацию. Возвращает новое кол-во free_uses."""
        plan = self.get_plan(user_id)
        with self._lock, self._conn() as conn:
            if plan == "basic":
                conn.execute(
                    "UPDATE users SET paid_left = MAX(0, paid_left - 1), updated_at=datetime('now') WHERE user_id=?",
                    (user_id,)
                )
            elif plan == "free":
                conn.execute(
                    "UPDATE users SET free_uses = free_uses + 1, updated_at=datetime('now') WHERE user_id=?",
                    (user_id,)
                )
            # PRO — не списываем
            row = conn.execute(
                "SELECT free_uses FROM users WHERE user_id=?", (user_id,)
            ).fetchone()
        return row["free_uses"] if row else 0

    def set_plan(self, user_id: int, plan: str, generations: int = 0, days: int = 0):
        with self._lock, self._conn() as conn:
            if plan == "basic":
                conn.execute("""
                    UPDATE users SET
                        plan = 'basic',
                        paid_left = paid_left + ?,
                        updated_at = datetime('now')
                    WHERE user_id = ?
                """, (generations, user_id))
            elif plan == "pro":
                pro_until = (datetime.utcnow() + timedelta(days=days)).isoformat()
                conn.execute("""
                    UPDATE users SET
                        plan = 'pro',
                        pro_until = ?,
                        updated_at = datetime('now')
                    WHERE user_id = ?
                """, (pro_until, user_id))

    def log_generation(self, user_id: int, product: str = ""):
        with self._lock, self._conn() as conn:
            conn.execute(
                "INSERT INTO generations (user_id, product) VALUES (?, ?)",
                (user_id, product)
            )

    # ── Статистика (для /admin) ──

    def get_stats(self) -> dict:
        with self._conn() as conn:
            total_users = conn.execute("SELECT COUNT(*) as n FROM users").fetchone()["n"]
            total_gens  = conn.execute("SELECT COUNT(*) as n FROM generations").fetchone()["n"]
            paid_users  = conn.execute(
                "SELECT COUNT(*) as n FROM users WHERE plan != 'free'"
            ).fetchone()["n"]
            today_gens  = conn.execute(
                "SELECT COUNT(*) as n FROM generations WHERE date(created_at)=date('now')"
            ).fetchone()["n"]
        return {
            "total_users": total_users,
            "paid_users":  paid_users,
            "total_gens":  total_gens,
            "today_gens":  today_gens,
        }
