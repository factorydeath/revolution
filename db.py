from __future__ import annotations

import asyncio
import datetime
import logging
import re
import sqlite3
import time
from collections import OrderedDict
from pathlib import Path
from typing import Any, Iterable, Optional

import aiosqlite

logger = logging.getLogger(__name__)

__all__ = ["Database", "utc_now", "get_today_date", "parse_date_from_iso", "parse_phone"]

try:
    from zoneinfo import ZoneInfo

    MSK = ZoneInfo("Europe/Moscow")
except Exception:
    MSK = datetime.timezone(datetime.timedelta(hours=3))


def utc_now() -> str:
    return datetime.datetime.now(datetime.timezone.utc).isoformat()


def get_today_date() -> str:
    return datetime.datetime.now(MSK).strftime("%Y-%m-%d")


def parse_date_from_iso(ts: str | None) -> str:
    if not ts:
        return get_today_date()
    try:
        clean_ts = ts.strip().replace("Z", "+00:00")
        dt = datetime.datetime.fromisoformat(clean_ts)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=datetime.timezone.utc)
        return dt.astimezone(MSK).strftime("%Y-%m-%d")
    except Exception:
        return get_today_date()


def parse_phone(raw: str) -> tuple[str, str]:
    digits = re.sub(r"\D", "", raw or "")
    if digits.startswith("380") and len(digits) == 12:
        return digits, "UA"
    if len(digits) == 10 and digits.startswith("0"):
        return "380" + digits[1:], "UA"
    if len(digits) == 9:
        return "380" + digits, "UA"
    if digits.startswith("8") and len(digits) == 11:
        digits = "7" + digits[1:]
    if len(digits) == 10:
        digits = "7" + digits
    if digits.startswith("7") and len(digits) == 11:
        return digits, "RU"
    return digits, "UA" if digits.startswith("380") else "RU"


_MISSING = object()


class _TTLCache:
    __slots__ = ("_data", "_max")

    def __init__(self, max_size: int = 4096) -> None:
        self._data: OrderedDict[str, tuple[float, Any]] = OrderedDict()
        self._max = max_size

    def get(self, key: str, default: Any = None) -> Any:
        item = self._data.get(key)
        if item is None:
            return default
        expires, value = item
        if expires <= time.monotonic():
            self._data.pop(key, None)
            return default
        self._data.move_to_end(key)
        return value

    def set(self, key: str, value: Any, ttl: float = 60.0) -> None:
        if key not in self._data and len(self._data) >= self._max:
            self._data.popitem(last=False)
        self._data[key] = (time.monotonic() + ttl, value)
        self._data.move_to_end(key)

    def drop(self, prefix: str) -> None:
        for key in [k for k in self._data if k.startswith(prefix)]:
            self._data.pop(key, None)


class Database:
    def __init__(self, path: str | Path) -> None:
        self.path = str(path)
        self._conn: aiosqlite.Connection | None = None
        self._lock = asyncio.Lock()
        self._cache = _TTLCache()

    def _require(self) -> aiosqlite.Connection:
        if self._conn is None:
            raise RuntimeError("database is not initialized")
        return self._conn

    async def init_db(self) -> None:
        conn = await aiosqlite.connect(self.path, timeout=10.0)
        await conn.execute("PRAGMA journal_mode=WAL;")
        await conn.execute("PRAGMA synchronous=NORMAL;")
        await conn.execute("PRAGMA foreign_keys=ON;")
        await conn.execute("PRAGMA busy_timeout=5000;")
        await conn.execute("PRAGMA cache_size=-16000;")
        await conn.execute("PRAGMA temp_store=MEMORY;")
        await conn.execute("PRAGMA mmap_size=268435456;")
        self._conn = conn
        await self._create_schema()
        await self._migrate()
        logger.info("database ready: %s", self.path)

    async def close(self) -> None:
        if self._conn is not None:
            try:
                await self._conn.close()
            finally:
                self._conn = None

    async def _create_schema(self) -> None:
        await self._require().executescript(
            """
            CREATE TABLE IF NOT EXISTS users (
                user_id INTEGER PRIMARY KEY,
                first_name TEXT,
                username TEXT,
                accepted_terms INTEGER DEFAULT 0,
                registration_date TEXT,
                total_attacks INTEGER DEFAULT 0
            );

            CREATE TABLE IF NOT EXISTS attacks (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id INTEGER,
                phone TEXT,
                duration INTEGER,
                started_at TEXT,
                stopped INTEGER DEFAULT 0,
                finished_at TEXT,
                status TEXT DEFAULT 'pending',
                FOREIGN KEY (user_id) REFERENCES users(user_id) ON DELETE CASCADE
            );

            CREATE TABLE IF NOT EXISTS banned_users (
                user_id INTEGER PRIMARY KEY,
                banned_at TEXT
            );

            CREATE TABLE IF NOT EXISTS users_with_privileges (
                user_id INTEGER PRIMARY KEY,
                expires_at TEXT
            );

            CREATE TABLE IF NOT EXISTS user_overrides (
                user_id INTEGER PRIMARY KEY,
                max_active INTEGER NOT NULL,
                max_duration INTEGER NOT NULL,
                created_at TEXT
            );

            CREATE TABLE IF NOT EXISTS protected_numbers (
                phone TEXT PRIMARY KEY,
                user_id INTEGER,
                added_at TEXT,
                FOREIGN KEY (user_id) REFERENCES users(user_id) ON DELETE CASCADE
            );

            CREATE TABLE IF NOT EXISTS payments (
                charge_id TEXT PRIMARY KEY,
                user_id INTEGER NOT NULL,
                amount INTEGER NOT NULL,
                currency TEXT NOT NULL,
                payload TEXT,
                status TEXT NOT NULL,
                created_at TEXT,
                updated_at TEXT
            );

            CREATE TABLE IF NOT EXISTS broadcast_state (
                id INTEGER PRIMARY KEY CHECK (id = 1),
                text TEXT,
                last_user_id INTEGER DEFAULT 0,
                sent INTEGER DEFAULT 0,
                failed INTEGER DEFAULT 0,
                active INTEGER DEFAULT 0,
                updated_at TEXT
            );

            CREATE INDEX IF NOT EXISTS idx_attacks_user_id ON attacks(user_id);
            CREATE INDEX IF NOT EXISTS idx_attacks_started_at ON attacks(started_at);
            CREATE INDEX IF NOT EXISTS idx_attacks_user_id_desc ON attacks(user_id, id DESC);
            CREATE INDEX IF NOT EXISTS idx_users_registration_date ON users(registration_date);
            CREATE INDEX IF NOT EXISTS idx_protected_user ON protected_numbers(user_id);
            CREATE INDEX IF NOT EXISTS idx_payments_user ON payments(user_id);

            CREATE TABLE IF NOT EXISTS xrocket_invoices (
                invoice_id TEXT PRIMARY KEY,
                user_id INTEGER NOT NULL,
                amount TEXT NOT NULL,
                currency TEXT NOT NULL,
                status TEXT NOT NULL,
                pay_url TEXT NOT NULL,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL
            );
            CREATE INDEX IF NOT EXISTS idx_xrocket_user ON xrocket_invoices(user_id);
            CREATE INDEX IF NOT EXISTS idx_xrocket_status ON xrocket_invoices(status);

            CREATE TABLE IF NOT EXISTS fsm (
                key TEXT PRIMARY KEY,
                state TEXT,
                data TEXT,
                updated_at REAL
            );

            CREATE TABLE IF NOT EXISTS daily_launches (
                user_id INTEGER,
                launch_date TEXT,
                count INTEGER DEFAULT 0,
                PRIMARY KEY (user_id, launch_date)
            );
            CREATE INDEX IF NOT EXISTS idx_daily_launches_date ON daily_launches(launch_date);

            CREATE TABLE IF NOT EXISTS vip_invite_links (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id INTEGER NOT NULL,
                invite_link TEXT NOT NULL UNIQUE,
                chat_id INTEGER NOT NULL,
                status TEXT NOT NULL DEFAULT 'active',
                created_at TEXT NOT NULL,
                used_at TEXT
            );
            CREATE INDEX IF NOT EXISTS idx_vip_invites_user ON vip_invite_links(user_id);
            CREATE INDEX IF NOT EXISTS idx_vip_invites_link ON vip_invite_links(invite_link);
            CREATE INDEX IF NOT EXISTS idx_vip_invites_status ON vip_invite_links(status);
            """
        )
        await self._require().commit()

    async def _migrate(self) -> None:
        conn = self._require()
        async with conn.execute("PRAGMA table_info(attacks)") as cursor:
            columns = {row[1] for row in await cursor.fetchall()}
        if "finished_at" not in columns:
            await conn.execute("ALTER TABLE attacks ADD COLUMN finished_at TEXT")
            await conn.commit()
            logger.info("db migrated: attacks.finished_at added")
        if "status" not in columns:
            await conn.execute("ALTER TABLE attacks ADD COLUMN status TEXT DEFAULT 'pending'")
            await conn.commit()
            logger.info("db migrated: attacks.status added")
        await conn.execute(
            "UPDATE attacks SET status = CASE "
            "WHEN status IS NULL THEN 'finished' "
            "WHEN status = 'pending' AND finished_at IS NOT NULL THEN 'finished' "
            "ELSE status END"
        )
        await conn.execute(
            """
            CREATE TABLE IF NOT EXISTS daily_launches (
                user_id INTEGER,
                launch_date TEXT,
                count INTEGER DEFAULT 0,
                PRIMARY KEY (user_id, launch_date)
            );
            """
        )
        await conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_daily_launches_date ON daily_launches(launch_date);"
        )
        await conn.commit()

        await conn.execute(
            "DELETE FROM protected_numbers WHERE rowid NOT IN "
            "(SELECT MIN(rowid) FROM protected_numbers GROUP BY user_id)"
        )
        await conn.execute(
            "CREATE UNIQUE INDEX IF NOT EXISTS idx_protected_user_unique "
            "ON protected_numbers(user_id)"
        )
        await conn.commit()

        async with conn.execute("PRAGMA table_info(users_with_privileges)") as cursor:
            vip_columns = {row[1] for row in await cursor.fetchall()}
        if "expires_at" not in vip_columns:
            await conn.execute("ALTER TABLE users_with_privileges ADD COLUMN expires_at TEXT")
            now_plus_month = (
                datetime.datetime.now(datetime.timezone.utc) + datetime.timedelta(days=30)
            ).isoformat()
            await conn.execute(
                "UPDATE users_with_privileges SET expires_at = ? WHERE expires_at IS NULL",
                (now_plus_month,),
            )
            await conn.commit()
            logger.info("db migrated: users_with_privileges.expires_at added")

        await conn.execute(
            """
            CREATE TABLE IF NOT EXISTS vip_invite_links (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id INTEGER NOT NULL,
                invite_link TEXT NOT NULL UNIQUE,
                chat_id INTEGER NOT NULL,
                status TEXT NOT NULL DEFAULT 'active',
                created_at TEXT NOT NULL,
                used_at TEXT
            );
            """
        )
        await conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_vip_invites_user ON vip_invite_links(user_id);"
        )
        await conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_vip_invites_link ON vip_invite_links(invite_link);"
        )
        await conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_vip_invites_status ON vip_invite_links(status);"
        )
        await conn.commit()

    async def _transaction(self, callback, *, retries: int = 3):
        conn = self._require()
        async with self._lock:
            for attempt in range(retries):
                try:
                    result = await callback(conn)
                    await conn.commit()
                    return result
                except sqlite3.OperationalError as exc:
                    try:
                        await conn.rollback()
                    except Exception:
                        pass
                    if "locked" not in str(exc).lower() or attempt == retries - 1:
                        raise
                    await asyncio.sleep(0.05 * (attempt + 1))
                except BaseException:
                    try:
                        await conn.rollback()
                    except Exception:
                        pass
                    raise
            raise RuntimeError("unreachable")

    async def _execute(self, sql: str, params: Iterable[Any] = (), *, commit: bool = True):
        async def run(conn):
            return await conn.execute(sql, tuple(params))

        return await self._transaction(run)

    async def _execute_batch(self, statements: list[tuple[str, Iterable[Any]]]):
        async def run(conn):
            cursors = []
            for sql, params in statements:
                cursors.append(await conn.execute(sql, tuple(params)))
            return cursors

        return await self._transaction(run)

    async def write(self, sql: str, params: Iterable[Any] = ()):
        return await self._execute(sql, params)

    async def _fetchone(self, sql: str, params: Iterable[Any] = ()) -> Optional[tuple]:
        async with self._lock:
            async with self._require().execute(sql, tuple(params)) as cursor:
                return await cursor.fetchone()

    async def _fetchall(self, sql: str, params: Iterable[Any] = ()) -> list[tuple]:
        async with self._lock:
            async with self._require().execute(sql, tuple(params)) as cursor:
                return list(await cursor.fetchall())

    async def _scalar(self, sql: str, params: Iterable[Any] = (), default: int = 0) -> int:
        row = await self._fetchone(sql, params)
        return int(row[0]) if row and row[0] is not None else default

    async def add_user(self, user_id: int, first_name: str, username: Optional[str] = None) -> None:
        await self._execute(
            """
            INSERT INTO users (user_id, first_name, username, registration_date)
            VALUES (?, ?, ?, ?)
            ON CONFLICT(user_id) DO UPDATE SET
                first_name = excluded.first_name,
                username = excluded.username
            WHERE users.first_name IS NOT excluded.first_name
               OR users.username IS NOT excluded.username
            """,
            (user_id, first_name, username, utc_now()),
        )

    async def user_exists(self, user_id: int) -> bool:
        return await self._fetchone("SELECT 1 FROM users WHERE user_id = ?", (user_id,)) is not None

    async def get_user_by_id(self, user_id: int) -> Optional[tuple]:
        return await self._fetchone(
            "SELECT first_name, username FROM users WHERE user_id = ?", (user_id,)
        )

    async def get_user_stats(self, user_id: int) -> Optional[tuple]:
        return await self._fetchone(
            "SELECT first_name, username, registration_date, total_attacks FROM users WHERE user_id = ?",
            (user_id,),
        )

    async def get_user_by_username(self, username: str) -> Optional[tuple]:
        handle = (username or "").lstrip("@").strip()
        if not handle:
            return None
        return await self._fetchone(
            "SELECT user_id, first_name, username FROM users WHERE lower(username) = lower(?)",
            (handle,),
        )

    async def search_users(self, query: str, limit: int = 10) -> list[tuple]:
        now_iso = utc_now()
        pattern = f"%{(query or '').strip().lower()}%"
        return await self._fetchall(
            """
            SELECT u.user_id, u.first_name, u.username, u.accepted_terms, u.total_attacks,
                   CASE WHEN p.user_id IS NOT NULL AND (p.expires_at IS NULL OR p.expires_at > ?) THEN 1 ELSE 0 END,
                   CASE WHEN b.user_id IS NOT NULL THEN 1 ELSE 0 END
            FROM users u
            LEFT JOIN users_with_privileges p ON p.user_id = u.user_id
            LEFT JOIN banned_users b ON b.user_id = u.user_id
            WHERE lower(u.first_name) LIKE ?
               OR lower(u.username) LIKE ?
               OR CAST(u.user_id AS TEXT) LIKE ?
            ORDER BY u.total_attacks DESC
            LIMIT ?
            """,
            (now_iso, pattern, pattern, pattern, limit),
        )

    async def get_all_users(self, page: int = 1, per_page: int = 10) -> list[tuple]:
        now_iso = utc_now()
        offset = max(0, (page - 1) * per_page)
        return await self._fetchall(
            """
            SELECT u.user_id, u.first_name, u.username, u.accepted_terms, u.total_attacks,
                   CASE WHEN p.user_id IS NOT NULL AND (p.expires_at IS NULL OR p.expires_at > ?) THEN 1 ELSE 0 END,
                   CASE WHEN b.user_id IS NOT NULL THEN 1 ELSE 0 END
            FROM users u
            LEFT JOIN users_with_privileges p ON p.user_id = u.user_id
            LEFT JOIN banned_users b ON b.user_id = u.user_id
            ORDER BY u.registration_date DESC
            LIMIT ? OFFSET ?
            """,
            (now_iso, per_page, offset),
        )

    async def get_all_user_ids(self) -> list[int]:
        rows = await self._fetchall("SELECT user_id FROM users")
        return [int(row[0]) for row in rows]

    async def get_total_users(self) -> int:
        return await self._scalar("SELECT COUNT(*) FROM users")

    async def get_top_attackers(self, limit: int = 10) -> list[tuple]:
        return await self._fetchall(
            "SELECT user_id, first_name, username, total_attacks FROM users "
            "ORDER BY total_attacks DESC LIMIT ?",
            (limit,),
        )

    async def accept_terms(self, user_id: int) -> None:
        await self._execute("UPDATE users SET accepted_terms = 1 WHERE user_id = ?", (user_id,))
        self._cache.set(f"terms:{user_id}", True, ttl=300)

    async def has_accepted_terms(self, user_id: int) -> bool:
        key = f"terms:{user_id}"
        cached = self._cache.get(key)
        if cached is not None:
            return bool(cached)
        row = await self._fetchone("SELECT accepted_terms FROM users WHERE user_id = ?", (user_id,))
        result = bool(row and row[0] == 1)
        self._cache.set(key, result, ttl=300)
        return result

    async def add_attack(self, user_id: int, phone: str, duration: int) -> int:
        today = get_today_date()
        cursors = await self._execute_batch(
            [
                (
                    "INSERT INTO attacks (user_id, phone, duration, started_at, status) "
                    "VALUES (?, ?, ?, ?, 'pending')",
                    (user_id, phone, duration, utc_now()),
                ),
                (
                    "UPDATE users SET total_attacks = total_attacks + 1 WHERE user_id = ?",
                    (user_id,),
                ),
                (
                    "INSERT INTO daily_launches (user_id, launch_date, count) VALUES (?, ?, 1) "
                    "ON CONFLICT(user_id, launch_date) DO UPDATE SET count = count + 1",
                    (user_id, today),
                ),
            ]
        )
        self._cache.drop(f"daily:{user_id}:")
        return int(cursors[0].lastrowid or 0)

    async def mark_started(self, attack_id: int) -> None:
        await self._execute(
            "UPDATE attacks SET status = 'started' WHERE id = ? AND status = 'pending'",
            (attack_id,),
        )

    async def undo_attack(self, attack_id: int) -> bool:
        async def run(conn):
            cursor = await conn.execute(
                "UPDATE attacks SET status = 'failed', finished_at = ?, stopped = 0 "
                "WHERE id = ? AND status != 'failed'",
                (utc_now(), attack_id),
            )
            if not cursor.rowcount:
                return False
            async with conn.execute(
                "SELECT user_id, started_at FROM attacks WHERE id = ?", (attack_id,)
            ) as reader:
                row = await reader.fetchone()
            if row and row[0] is not None:
                uid = int(row[0])
                await conn.execute(
                    "UPDATE users SET total_attacks = MAX(total_attacks - 1, 0) "
                    "WHERE user_id = ?",
                    (uid,),
                )
                attack_date = parse_date_from_iso(row[1]) if len(row) > 1 and row[1] else get_today_date()
                await conn.execute(
                    "UPDATE daily_launches SET count = MAX(count - 1, 0) "
                    "WHERE user_id = ? AND launch_date = ?",
                    (uid, attack_date),
                )
                self._cache.drop(f"daily:{uid}:")
            return True

        return bool(await self._transaction(run))

    async def mark_finished(self, attack_id: int) -> None:
        await self._execute(
            "UPDATE attacks SET stopped = 0, status = 'finished', finished_at = ? WHERE id = ?",
            (utc_now(), attack_id),
        )

    async def mark_stopped(self, attack_id: int) -> None:
        await self._execute(
            "UPDATE attacks SET stopped = 1, status = 'stopped', finished_at = ? WHERE id = ?",
            (utc_now(), attack_id),
        )

    async def close_dangling_attacks(self) -> int:
        async def run(conn):
            now = utc_now()
            async with conn.execute(
                "SELECT user_id, started_at FROM attacks "
                "WHERE status = 'pending' AND finished_at IS NULL"
            ) as reader:
                pending = await reader.fetchall()
            cursor = await conn.execute(
                "UPDATE attacks SET stopped = 0, status = 'failed', finished_at = ? "
                "WHERE status = 'pending' AND finished_at IS NULL",
                (now,),
            )
            pending_count = int(cursor.rowcount or 0)
            for row in pending:
                user_id = row[0]
                started_at = row[1] if len(row) > 1 else None
                if user_id is not None:
                    uid = int(user_id)
                    await conn.execute(
                        "UPDATE users SET total_attacks = MAX(total_attacks - 1, 0) "
                        "WHERE user_id = ?",
                        (uid,),
                    )
                    attack_date = parse_date_from_iso(started_at)
                    await conn.execute(
                        "UPDATE daily_launches SET count = MAX(count - 1, 0) "
                        "WHERE user_id = ? AND launch_date = ?",
                        (uid, attack_date),
                    )
                    self._cache.drop(f"daily:{uid}:")
            cursor = await conn.execute(
                "UPDATE attacks SET stopped = 0, status = 'finished', finished_at = ? "
                "WHERE finished_at IS NULL",
                (now,),
            )
            return pending_count + int(cursor.rowcount or 0)

        return int(await self._transaction(run))

    async def get_attack_history(self, user_id: int, limit: int = 10) -> list[tuple]:
        return await self._fetchall(
            "SELECT phone, duration, started_at, stopped FROM attacks "
            "WHERE user_id = ? AND status != 'failed' ORDER BY id DESC LIMIT ?",
            (user_id, limit),
        )

    async def get_total_attacks(self) -> int:
        return await self._scalar("SELECT COUNT(*) FROM attacks WHERE status != 'failed'")

    async def get_daily_attacks(
        self, user_id: int, date: str | None = None, *, fresh: bool = False
    ) -> int:
        target_date = date or get_today_date()
        key = f"daily:{user_id}:{target_date}"
        if not fresh:
            cached = self._cache.get(key)
            if cached is not None:
                return int(cached)
        row = await self._fetchone(
            "SELECT count FROM daily_launches WHERE user_id = ? AND launch_date = ?",
            (user_id, target_date),
        )
        count = int(row[0]) if row and row[0] is not None else 0
        self._cache.set(key, count, ttl=5)
        return count

    async def increment_daily_attacks(
        self, user_id: int, date: str | None = None
    ) -> int:
        target_date = date or get_today_date()
        await self._execute(
            "INSERT INTO daily_launches (user_id, launch_date, count) VALUES (?, ?, 1) "
            "ON CONFLICT(user_id, launch_date) DO UPDATE SET count = count + 1",
            (user_id, target_date),
        )
        self._cache.drop(f"daily:{user_id}:")
        return await self.get_daily_attacks(user_id, target_date, fresh=True)

    async def decrement_daily_attacks(
        self, user_id: int, date: str | None = None
    ) -> int:
        target_date = date or get_today_date()
        await self._execute(
            "UPDATE daily_launches SET count = MAX(count - 1, 0) "
            "WHERE user_id = ? AND launch_date = ?",
            (user_id, target_date),
        )
        self._cache.drop(f"daily:{user_id}:")
        return await self.get_daily_attacks(user_id, target_date, fresh=True)

    async def reset_daily_attacks(
        self, user_id: int | None = None, date: str | None = None
    ) -> int:
        target_date = date or get_today_date()
        if user_id is not None:
            cursor = await self._execute(
                "DELETE FROM daily_launches WHERE user_id = ? AND launch_date = ?",
                (user_id, target_date),
            )
            self._cache.drop(f"daily:{user_id}:")
            return int(cursor.rowcount or 0)
        else:
            cursor = await self._execute(
                "DELETE FROM daily_launches WHERE launch_date = ?",
                (target_date,),
            )
            self._cache.drop("daily:")
            return int(cursor.rowcount or 0)

    async def cleanup_old_daily_launches(self, keep_days: int = 30) -> int:
        cutoff = (datetime.datetime.now(MSK) - datetime.timedelta(days=keep_days)).strftime("%Y-%m-%d")
        cursor = await self._execute(
            "DELETE FROM daily_launches WHERE launch_date < ?",
            (cutoff,),
        )
        return int(cursor.rowcount or 0)

    def reset_daily_cache(self) -> None:
        self._cache.drop("daily:")

    async def dashboard_stats(self) -> tuple[int, int, int, int, int, int]:
        now_iso = utc_now()
        row = await self._fetchone(
            """
            SELECT (SELECT COUNT(*) FROM users),
                   (SELECT COUNT(*) FROM users_with_privileges WHERE expires_at IS NULL OR expires_at > ?),
                   (SELECT COUNT(*) FROM banned_users),
                   (SELECT COUNT(*) FROM protected_numbers),
                   (SELECT COUNT(*) FROM attacks WHERE status != 'failed'),
                   (SELECT COUNT(*) FROM attacks WHERE status = 'started')
            """,
            (now_iso,),
        )
        return tuple(int(value or 0) for value in (row or (0,) * 6))[:6]

    async def cleanup_fsm(self, max_age_days: float = 30.0) -> int:
        cutoff = time.time() - max_age_days * 86400.0
        cursor = await self._execute("DELETE FROM fsm WHERE updated_at < ?", (cutoff,))
        return int(cursor.rowcount or 0)

    async def ban_user(self, user_id: int) -> None:
        await self._execute(
            "INSERT OR IGNORE INTO banned_users (user_id, banned_at) VALUES (?, ?)",
            (user_id, utc_now()),
        )
        self._cache.set(f"ban:{user_id}", True, ttl=5)

    async def unban_user(self, user_id: int) -> None:
        await self._execute("DELETE FROM banned_users WHERE user_id = ?", (user_id,))
        self._cache.set(f"ban:{user_id}", False, ttl=5)

    async def is_user_banned(self, user_id: int, *, fresh: bool = False) -> bool:
        key = f"ban:{user_id}"
        if not fresh:
            cached = self._cache.get(key)
            if cached is not None:
                return bool(cached)
        result = await self._fetchone("SELECT 1 FROM banned_users WHERE user_id = ?", (user_id,)) is not None
        self._cache.set(key, result, ttl=5)
        return result

    async def get_banned_count(self) -> int:
        return await self._scalar("SELECT COUNT(*) FROM banned_users")

    async def add_user_privilege(self, user_id: int, days: int = 30) -> bool:
        now_dt = datetime.datetime.now(datetime.timezone.utc)
        now_iso = now_dt.isoformat()
        exp_iso = (now_dt + datetime.timedelta(days=days)).isoformat()

        async def run(conn):
            async with conn.execute(
                "SELECT expires_at FROM users_with_privileges WHERE user_id = ?",
                (user_id,),
            ) as reader:
                row = await reader.fetchone()

            if row is not None:
                exp = row[0]
                if exp is None or exp > now_iso:
                    return False
                await conn.execute(
                    "UPDATE users_with_privileges SET expires_at = ? WHERE user_id = ?",
                    (exp_iso, user_id),
                )
                return True

            await conn.execute(
                "INSERT INTO users_with_privileges (user_id, expires_at) VALUES (?, ?)",
                (user_id, exp_iso),
            )
            return True

        result = await self._transaction(run)
        if result:
            self._cache.set(f"vip:{user_id}", True, ttl=5)
        return bool(result)

    async def remove_user_privilege(self, user_id: int) -> bool:
        cursor = await self._execute(
            "DELETE FROM users_with_privileges WHERE user_id = ?", (user_id,)
        )
        self._cache.set(f"vip:{user_id}", False, ttl=5)
        return int(cursor.rowcount or 0) > 0

    async def check_user_exists_privilege(self, user_id: int, *, fresh: bool = False) -> bool:
        key = f"vip:{user_id}"
        if not fresh:
            cached = self._cache.get(key)
            if cached is not None:
                return bool(cached)
        row = await self._fetchone(
            "SELECT expires_at FROM users_with_privileges WHERE user_id = ?", (user_id,)
        )
        if row is None:
            self._cache.set(key, False, ttl=5)
            return False
        exp = row[0]
        if exp is not None and exp <= utc_now():
            await self._execute(
                "DELETE FROM users_with_privileges WHERE user_id = ?", (user_id,)
            )
            self._cache.set(key, False, ttl=5)
            return False
        self._cache.set(key, True, ttl=5)
        return True

    async def get_user_privilege_expiry(self, user_id: int) -> str | None:
        row = await self._fetchone(
            "SELECT expires_at FROM users_with_privileges WHERE user_id = ?", (user_id,)
        )
        if not row:
            return None
        exp = row[0]
        if exp is not None and exp <= utc_now():
            await self._execute(
                "DELETE FROM users_with_privileges WHERE user_id = ?", (user_id,)
            )
            self._cache.set(f"vip:{user_id}", False, ttl=5)
            return None
        return exp

    async def get_privileged_count(self) -> int:
        now_iso = utc_now()
        await self._execute(
            "DELETE FROM users_with_privileges WHERE expires_at IS NOT NULL AND expires_at <= ?",
            (now_iso,),
        )
        return await self._scalar("SELECT COUNT(*) FROM users_with_privileges")

    async def get_vip_users(self, page: int = 1, per_page: int = 10) -> tuple[list[tuple], int]:
        now_iso = utc_now()
        await self._execute(
            "DELETE FROM users_with_privileges WHERE expires_at IS NOT NULL AND expires_at <= ?",
            (now_iso,),
        )
        offset = max(0, (page - 1) * per_page)
        total = await self.get_privileged_count()
        rows = await self._fetchall(
            """
            SELECT u.user_id, u.first_name, u.username, u.total_attacks, u.registration_date
            FROM users_with_privileges p
            JOIN users u ON u.user_id = p.user_id
            ORDER BY u.registration_date DESC
            LIMIT ? OFFSET ?
            """,
            (per_page, offset),
        )
        return rows, total

    async def set_user_override(self, user_id: int, max_active: int, max_duration: int) -> None:
        await self._execute(
            """
            INSERT INTO user_overrides (user_id, max_active, max_duration, created_at)
            VALUES (?, ?, ?, ?)
            ON CONFLICT(user_id) DO UPDATE SET
                max_active = excluded.max_active,
                max_duration = excluded.max_duration
            """,
            (user_id, max_active, max_duration, utc_now()),
        )
        self._cache.set(f"ovr:{user_id}", (max_active, max_duration), ttl=5)

    async def get_user_override(self, user_id: int) -> Optional[tuple[int, int]]:
        key = f"ovr:{user_id}"
        cached = self._cache.get(key, _MISSING)
        if cached is not _MISSING:
            return tuple(cached) or None
        row = await self._fetchone(
            "SELECT max_active, max_duration FROM user_overrides WHERE user_id = ?", (user_id,)
        )
        result: tuple[int, ...] = (int(row[0]), int(row[1])) if row else ()
        self._cache.set(key, result, ttl=5)
        return tuple(result) or None

    async def remove_user_override(self, user_id: int) -> bool:
        cursor = await self._execute("DELETE FROM user_overrides WHERE user_id = ?", (user_id,))
        self._cache.set(f"ovr:{user_id}", (), ttl=5)
        return int(cursor.rowcount or 0) > 0

    async def is_phone_protected(self, phone: str, *, fresh: bool = False) -> bool:
        digits, _country = parse_phone(phone)
        key = f"prot:{digits}"
        if not fresh:
            cached = self._cache.get(key)
            if cached is not None:
                return bool(cached)
        result = await self._fetchone(
            "SELECT 1 FROM protected_numbers WHERE phone = ?", (digits,)
        ) is not None
        self._cache.set(key, result, ttl=5)
        return result

    async def filter_protected(
        self, phones: Iterable[str], *, fresh: bool = False
    ) -> tuple[list[str], list[str]]:
        items = list(phones)
        if not items:
            return [], []

        verdict: dict[str, bool] = {}
        pending: dict[str, list[str]] = {}
        for phone in items:
            if phone in verdict:
                continue
            digits, _country = parse_phone(phone)
            cached = None if fresh else self._cache.get(f"prot:{digits}")
            if cached is not None:
                verdict[phone] = bool(cached)
            else:
                pending.setdefault(digits, []).append(phone)

        if pending:
            digits_list = list(pending)
            found: set[str] = set()
            for start in range(0, len(digits_list), 500):
                chunk = digits_list[start : start + 500]
                placeholders = ",".join("?" * len(chunk))
                rows = await self._fetchall(
                    f"SELECT phone FROM protected_numbers WHERE phone IN ({placeholders})",
                    chunk,
                )
                found.update(str(row[0]) for row in rows)
            for digits, original in pending.items():
                protected = digits in found
                self._cache.set(f"prot:{digits}", protected, ttl=120)
                for phone in original:
                    verdict[phone] = protected

        allowed = [phone for phone in items if not verdict.get(phone)]
        hits = [phone for phone in items if verdict.get(phone)]
        return allowed, hits

    async def add_protected_number(self, phone: str, user_id: int) -> bool:
        digits, _country = parse_phone(phone)
        try:
            await self._execute(
                "INSERT INTO protected_numbers (phone, user_id, added_at) VALUES (?, ?, ?)",
                (digits, user_id, utc_now()),
            )
        except sqlite3.IntegrityError:
            row = await self._fetchone(
                "SELECT 1 FROM protected_numbers WHERE phone = ?", (digits,)
            )
            self._cache.set(f"prot:{digits}", row is not None, ttl=120)
            return False
        self._cache.set(f"prot:{digits}", True, ttl=120)
        return True

    async def remove_protected_number(self, phone: str) -> bool:
        digits, _country = parse_phone(phone)
        cursor = await self._execute(
            "DELETE FROM protected_numbers WHERE phone = ?", (digits,)
        )
        self._cache.set(f"prot:{digits}", False, ttl=120)
        return int(cursor.rowcount or 0) > 0

    async def get_protected_by_user(self, user_id: int) -> list[tuple]:
        return await self._fetchall(
            "SELECT phone, added_at FROM protected_numbers WHERE user_id = ? ORDER BY added_at",
            (user_id,),
        )

    async def get_protected_count(self) -> int:
        return await self._scalar("SELECT COUNT(*) FROM protected_numbers")

    async def complete_payment(
        self,
        user_id: int,
        charge_id: str,
        amount: int,
        currency: str,
        payload: str = "",
    ) -> str:
        async def run(conn):
            cursor = await conn.execute(
                "INSERT OR IGNORE INTO payments "
                "(charge_id, user_id, amount, currency, payload, status, created_at, updated_at) "
                "VALUES (?, ?, ?, ?, ?, 'pending', ?, ?)",
                (charge_id, user_id, amount, currency, payload, utc_now(), utc_now()),
            )
            if not cursor.rowcount:
                async with conn.execute(
                    "SELECT status FROM payments WHERE charge_id = ?", (charge_id,)
                ) as reader:
                    row = await reader.fetchone()
                previous = str(row[0]) if row and row[0] else ""
                if previous in ("granted", "refunded"):
                    return "duplicate"
                return "refund_pending"
            now_dt = datetime.datetime.now(datetime.timezone.utc)
            now_iso = now_dt.isoformat()
            exp_iso = (now_dt + datetime.timedelta(days=30)).isoformat()

            async with conn.execute(
                "SELECT expires_at FROM users_with_privileges WHERE user_id = ?",
                (user_id,),
            ) as reader:
                vip_row = await reader.fetchone()

            if vip_row is not None:
                exp = vip_row[0]
                if exp is None or exp > now_iso:
                    status = "already_vip"
                else:
                    await conn.execute(
                        "UPDATE users_with_privileges SET expires_at = ? WHERE user_id = ?",
                        (exp_iso, user_id),
                    )
                    status = "granted"
            else:
                await conn.execute(
                    "INSERT INTO users_with_privileges (user_id, expires_at) VALUES (?, ?)",
                    (user_id, exp_iso),
                )
                status = "granted"
            await conn.execute(
                "UPDATE payments SET status = ?, updated_at = ? WHERE charge_id = ?",
                (status, utc_now(), charge_id),
            )
            return status

        result = await self._transaction(run)
        if result == "granted":
            self._cache.set(f"vip:{user_id}", True, ttl=5)
        return str(result)

    async def mark_payment_refund_pending(self, charge_id: str) -> None:
        await self._execute(
            "UPDATE payments SET status = 'refund_pending', updated_at = ? "
            "WHERE charge_id = ? AND status NOT IN ('granted', 'refunded')",
            (utc_now(), charge_id),
        )

    async def mark_payment_refunded(self, charge_id: str) -> None:
        await self._execute(
            "UPDATE payments SET status = 'refunded', updated_at = ? WHERE charge_id = ?",
            (utc_now(), charge_id),
        )

    async def get_payment(self, charge_id: str) -> Optional[tuple]:
        return await self._fetchone(
            "SELECT charge_id, user_id, amount, currency, payload, status "
            "FROM payments WHERE charge_id = ?",
            (charge_id,),
        )

    async def save_xrocket_invoice(
        self,
        invoice_id: str,
        user_id: int,
        amount: str,
        currency: str,
        pay_url: str,
    ) -> None:
        await self._execute(
            "INSERT OR REPLACE INTO xrocket_invoices "
            "(invoice_id, user_id, amount, currency, status, pay_url, created_at, updated_at) "
            "VALUES (?, ?, ?, ?, 'active', ?, ?, ?)",
            (str(invoice_id), int(user_id), str(amount), str(currency), str(pay_url), utc_now(), utc_now()),
        )

    async def get_xrocket_invoice(self, invoice_id: str) -> Optional[tuple]:
        return await self._fetchone(
            "SELECT invoice_id, user_id, amount, currency, status, pay_url "
            "FROM xrocket_invoices WHERE invoice_id = ?",
            (str(invoice_id),),
        )

    async def get_active_xrocket_invoices(self) -> list[tuple]:
        return await self._fetchall(
            "SELECT invoice_id, user_id, amount, currency, pay_url "
            "FROM xrocket_invoices WHERE status = 'active'"
        )

    async def update_xrocket_invoice_status(
        self, invoice_id: str, status: str
    ) -> None:
        await self._execute(
            "UPDATE xrocket_invoices SET status = ?, updated_at = ? WHERE invoice_id = ?",
            (str(status), utc_now(), str(invoice_id)),
        )

    async def get_user_ids_after(self, last_id: int, limit: int = 200) -> list[int]:
        rows = await self._fetchall(
            "SELECT user_id FROM users WHERE user_id > ? ORDER BY user_id LIMIT ?",
            (int(last_id), int(limit)),
        )
        return [int(row[0]) for row in rows]

    async def get_broadcast_state(self) -> Optional[tuple]:
        return await self._fetchone(
            "SELECT text, last_user_id, sent, failed, active, updated_at "
            "FROM broadcast_state WHERE id = 1"
        )

    async def save_broadcast_state(
        self,
        text: str,
        last_user_id: int,
        sent: int,
        failed: int,
        active: bool,
    ) -> None:
        await self._execute(
            """
            INSERT INTO broadcast_state (id, text, last_user_id, sent, failed, active, updated_at)
            VALUES (1, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(id) DO UPDATE SET
                text = excluded.text,
                last_user_id = excluded.last_user_id,
                sent = excluded.sent,
                failed = excluded.failed,
                active = excluded.active,
                updated_at = excluded.updated_at
            """,
            (text, int(last_user_id), int(sent), int(failed), int(active), utc_now()),
        )

    async def clear_broadcast_state(self) -> None:
        await self._execute("DELETE FROM broadcast_state WHERE id = 1")

    async def save_vip_invite_link(
        self, user_id: int, invite_link: str, chat_id: int
    ) -> None:
        await self._execute(
            "INSERT INTO vip_invite_links (user_id, invite_link, chat_id, status, created_at) "
            "VALUES (?, ?, ?, 'active', ?)",
            (int(user_id), str(invite_link), int(chat_id), utc_now()),
        )

    async def get_active_vip_invites_for_user(self, user_id: int) -> list[str]:
        rows = await self._fetchall(
            "SELECT invite_link FROM vip_invite_links WHERE user_id = ? AND status = 'active' ORDER BY id DESC",
            (int(user_id),),
        )
        return [str(row[0]) for row in rows if row and row[0]]

    async def get_vip_invite_info(self, invite_link: str) -> Optional[dict[str, Any]]:
        row = await self._fetchone(
            "SELECT id, user_id, invite_link, chat_id, status, created_at, used_at "
            "FROM vip_invite_links WHERE invite_link = ?",
            (str(invite_link),),
        )
        if not row:
            return None
        return {
            "id": row[0],
            "user_id": row[1],
            "invite_link": row[2],
            "chat_id": row[3],
            "status": row[4],
            "created_at": row[5],
            "used_at": row[6],
        }

    async def mark_vip_invite_link_used(
        self, invite_link: str, user_id: Optional[int] = None
    ) -> bool:
        now = utc_now()
        if user_id:
            cursor = await self._execute(
                "UPDATE vip_invite_links SET status = 'used', used_at = ? "
                "WHERE (invite_link = ? OR (user_id = ? AND status = 'active'))",
                (now, str(invite_link), int(user_id)),
            )
        else:
            cursor = await self._execute(
                "UPDATE vip_invite_links SET status = 'used', used_at = ? WHERE invite_link = ?",
                (now, str(invite_link)),
            )
        return int(cursor.rowcount or 0) > 0

    async def mark_vip_invite_link_revoked(self, invite_link: str) -> bool:
        cursor = await self._execute(
            "UPDATE vip_invite_links SET status = 'revoked', used_at = ? WHERE invite_link = ?",
            (utc_now(), str(invite_link)),
        )
        return int(cursor.rowcount or 0) > 0

    async def has_user_joined_vip_community(self, user_id: int) -> bool:
        row = await self._fetchone(
            "SELECT 1 FROM vip_invite_links WHERE user_id = ? AND status = 'used' LIMIT 1",
            (int(user_id),),
        )
        return row is not None
