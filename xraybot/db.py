"""Хранилище бота (SQLite): клиенты, трафик по часам, сессии, кэш геолокации."""

from __future__ import annotations

import time
from dataclasses import dataclass
from pathlib import Path

import aiosqlite

PERIOD_SEC = 30 * 24 * 3600  # лимит трафика считается периодами по 30 дней от даты создания

SCHEMA = """
PRAGMA journal_mode=WAL;
PRAGMA foreign_keys=ON;

CREATE TABLE IF NOT EXISTS clients (
    id          INTEGER PRIMARY KEY,
    name        TEXT    NOT NULL UNIQUE,   -- используется как email в Xray
    uuid        TEXT    NOT NULL UNIQUE,   -- третья группа = 0000 (vlessRoute)
    kind        TEXT    NOT NULL CHECK (kind IN ('permanent', 'guest')),
    created_at  INTEGER NOT NULL,
    expires_at  INTEGER,                   -- NULL у постоянных
    quota_bytes INTEGER,                   -- NULL = без лимита
    use_xhttp   INTEGER NOT NULL DEFAULT 1,
    use_vision  INTEGER NOT NULL DEFAULT 0,
    is_system   INTEGER NOT NULL DEFAULT 0   -- клиент из скрипта моста (main)
);

CREATE TABLE IF NOT EXISTS traffic (
    client_id INTEGER NOT NULL REFERENCES clients(id) ON DELETE CASCADE,
    hour      INTEGER NOT NULL,            -- unix-время начала часа
    up        INTEGER NOT NULL DEFAULT 0,
    down      INTEGER NOT NULL DEFAULT 0,
    PRIMARY KEY (client_id, hour)
);

CREATE TABLE IF NOT EXISTS sessions (
    id         INTEGER PRIMARY KEY,
    client_id  INTEGER NOT NULL REFERENCES clients(id) ON DELETE CASCADE,
    ip         TEXT    NOT NULL,
    first_seen INTEGER NOT NULL,
    last_seen  INTEGER NOT NULL
);
CREATE INDEX IF NOT EXISTS sessions_client_ip ON sessions (client_id, ip, last_seen);

CREATE TABLE IF NOT EXISTS quota_alerts (
    client_id INTEGER NOT NULL REFERENCES clients(id) ON DELETE CASCADE,
    period    INTEGER NOT NULL,
    level     INTEGER NOT NULL,
    PRIMARY KEY (client_id, period, level)
);

CREATE TABLE IF NOT EXISTS geo_cache (
    ip      TEXT PRIMARY KEY,
    country TEXT,
    city    TEXT,
    isp     TEXT,
    source  TEXT,
    ts      INTEGER NOT NULL
);

CREATE TABLE IF NOT EXISTS meta (
    key   TEXT PRIMARY KEY,
    value TEXT
);
"""

# Новая сессия начинается, если IP не был виден дольше этого времени
SESSION_GAP_SEC = 5 * 60


@dataclass
class Client:
    id: int
    name: str
    uuid: str
    kind: str
    created_at: int
    expires_at: int | None
    quota_bytes: int | None
    use_xhttp: bool
    use_vision: bool
    is_system: bool = False

    @property
    def is_guest(self) -> bool:
        return self.kind == "guest"

    def period_index(self, now: int | None = None) -> int:
        now = now or int(time.time())
        return max(0, (now - self.created_at) // PERIOD_SEC)

    def period_bounds(self, now: int | None = None) -> tuple[int, int]:
        start = self.created_at + self.period_index(now) * PERIOD_SEC
        return start, start + PERIOD_SEC


def _row_to_client(row: aiosqlite.Row) -> Client:
    return Client(
        id=row["id"],
        name=row["name"],
        uuid=row["uuid"],
        kind=row["kind"],
        created_at=row["created_at"],
        expires_at=row["expires_at"],
        quota_bytes=row["quota_bytes"],
        use_xhttp=bool(row["use_xhttp"]),
        use_vision=bool(row["use_vision"]),
        is_system=bool(row["is_system"]),
    )


class Database:
    def __init__(self, path: Path):
        self.path = path
        self._db: aiosqlite.Connection | None = None

    async def open(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._db = await aiosqlite.connect(self.path)
        self._db.row_factory = aiosqlite.Row
        await self._db.executescript(SCHEMA)
        # миграция баз, созданных до появления клиента main
        async with self._db.execute("PRAGMA table_info(clients)") as cur:
            columns = {r[1] for r in await cur.fetchall()}
        if "is_system" not in columns:
            await self._db.execute("ALTER TABLE clients ADD COLUMN is_system INTEGER NOT NULL DEFAULT 0")
        await self._db.commit()

    async def close(self) -> None:
        if self._db:
            await self._db.close()

    @property
    def db(self) -> aiosqlite.Connection:
        assert self._db is not None, "Database is not opened"
        return self._db

    # ---------- клиенты ----------

    async def add_client(
        self,
        name: str,
        uuid: str,
        kind: str,
        expires_at: int | None,
        quota_bytes: int | None,
        use_xhttp: bool,
        use_vision: bool,
        created_at: int | None = None,
        is_system: bool = False,
    ) -> Client:
        created_at = created_at or int(time.time())
        cur = await self.db.execute(
            "INSERT INTO clients (name, uuid, kind, created_at, expires_at, quota_bytes, use_xhttp, use_vision, is_system)"
            " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (name, uuid, kind, created_at, expires_at, quota_bytes, int(use_xhttp), int(use_vision), int(is_system)),
        )
        await self.db.commit()
        client = await self.get_client(cur.lastrowid)
        assert client is not None
        return client

    async def get_client(self, client_id: int) -> Client | None:
        async with self.db.execute("SELECT * FROM clients WHERE id = ?", (client_id,)) as cur:
            row = await cur.fetchone()
        return _row_to_client(row) if row else None

    async def get_client_by_name(self, name: str) -> Client | None:
        async with self.db.execute("SELECT * FROM clients WHERE name = ? COLLATE NOCASE", (name,)) as cur:
            row = await cur.fetchone()
        return _row_to_client(row) if row else None

    async def get_system_client(self) -> Client | None:
        async with self.db.execute("SELECT * FROM clients WHERE is_system = 1 LIMIT 1") as cur:
            row = await cur.fetchone()
        return _row_to_client(row) if row else None

    async def update_system_client(self, client_id: int, uuid: str, use_xhttp: bool, use_vision: bool) -> None:
        await self.db.execute(
            "UPDATE clients SET uuid = ?, use_xhttp = ?, use_vision = ? WHERE id = ?",
            (uuid, int(use_xhttp), int(use_vision), client_id),
        )
        await self.db.commit()

    async def list_clients(self) -> list[Client]:
        async with self.db.execute(
            "SELECT * FROM clients ORDER BY is_system DESC, kind DESC, name COLLATE NOCASE"
        ) as cur:
            rows = await cur.fetchall()
        return [_row_to_client(r) for r in rows]

    async def expired_guests(self, now: int) -> list[Client]:
        async with self.db.execute(
            "SELECT * FROM clients WHERE kind = 'guest' AND expires_at IS NOT NULL AND expires_at <= ?", (now,)
        ) as cur:
            rows = await cur.fetchall()
        return [_row_to_client(r) for r in rows]

    async def delete_client(self, client_id: int) -> None:
        await self.db.execute("DELETE FROM clients WHERE id = ?", (client_id,))
        await self.db.commit()

    async def set_expires(self, client_id: int, expires_at: int | None) -> None:
        await self.db.execute("UPDATE clients SET expires_at = ? WHERE id = ?", (expires_at, client_id))
        await self.db.commit()

    async def set_quota(self, client_id: int, quota_bytes: int | None) -> None:
        await self.db.execute("UPDATE clients SET quota_bytes = ? WHERE id = ?", (quota_bytes, client_id))
        await self.db.commit()

    # ---------- трафик ----------

    async def add_traffic(self, deltas: dict[int, tuple[int, int]], ts: int) -> None:
        """deltas: client_id -> (up, down) за последний опрос."""
        hour = ts - ts % 3600
        await self.db.executemany(
            "INSERT INTO traffic (client_id, hour, up, down) VALUES (?, ?, ?, ?)"
            " ON CONFLICT (client_id, hour) DO UPDATE SET up = up + excluded.up, down = down + excluded.down",
            [(cid, hour, up, down) for cid, (up, down) in deltas.items() if up or down],
        )
        await self.db.commit()

    async def traffic_since(self, client_id: int, since: int) -> tuple[int, int]:
        since_hour = since - since % 3600
        async with self.db.execute(
            "SELECT COALESCE(SUM(up), 0), COALESCE(SUM(down), 0) FROM traffic WHERE client_id = ? AND hour >= ?",
            (client_id, since_hour),
        ) as cur:
            up, down = await cur.fetchone()
        return int(up), int(down)

    async def traffic_total(self, client_id: int) -> tuple[int, int]:
        return await self.traffic_since(client_id, 0)

    async def traffic_all_since(self, since: int) -> dict[int, int]:
        since_hour = since - since % 3600
        async with self.db.execute(
            "SELECT client_id, SUM(up + down) FROM traffic WHERE hour >= ? GROUP BY client_id", (since_hour,)
        ) as cur:
            rows = await cur.fetchall()
        return {r[0]: int(r[1]) for r in rows}

    async def period_usage(self, client: Client, now: int | None = None) -> int:
        start, _ = client.period_bounds(now)
        up, down = await self.traffic_since(client.id, start)
        return up + down

    async def mark_quota_alert(self, client_id: int, period: int, level: int) -> bool:
        """True, если такого предупреждения в этом периоде ещё не было."""
        cur = await self.db.execute(
            "INSERT OR IGNORE INTO quota_alerts (client_id, period, level) VALUES (?, ?, ?)",
            (client_id, period, level),
        )
        await self.db.commit()
        return cur.rowcount > 0

    # ---------- сессии ----------

    async def touch_session(self, client_id: int, ip: str, seen: int) -> None:
        async with self.db.execute(
            "SELECT id, last_seen FROM sessions WHERE client_id = ? AND ip = ? ORDER BY last_seen DESC LIMIT 1",
            (client_id, ip),
        ) as cur:
            row = await cur.fetchone()
        if row and seen - row["last_seen"] <= SESSION_GAP_SEC:
            if seen > row["last_seen"]:
                await self.db.execute("UPDATE sessions SET last_seen = ? WHERE id = ?", (seen, row["id"]))
        else:
            await self.db.execute(
                "INSERT INTO sessions (client_id, ip, first_seen, last_seen) VALUES (?, ?, ?, ?)",
                (client_id, ip, seen, seen),
            )
        await self.db.commit()

    async def recent_sessions(self, client_id: int, limit: int = 15) -> list[aiosqlite.Row]:
        async with self.db.execute(
            "SELECT ip, first_seen, last_seen FROM sessions WHERE client_id = ? ORDER BY last_seen DESC LIMIT ?",
            (client_id, limit),
        ) as cur:
            return list(await cur.fetchall())

    async def last_seen(self, client_id: int) -> int | None:
        async with self.db.execute("SELECT MAX(last_seen) FROM sessions WHERE client_id = ?", (client_id,)) as cur:
            (value,) = await cur.fetchone()
        return value

    async def ips_seen_since(self, client_id: int, since: int) -> list[str]:
        async with self.db.execute(
            "SELECT ip FROM sessions WHERE client_id = ? AND last_seen >= ? GROUP BY ip ORDER BY MAX(last_seen) DESC",
            (client_id, since),
        ) as cur:
            return [r[0] for r in await cur.fetchall()]

    async def prune_sessions(self, keep_days: int = 90) -> None:
        await self.db.execute("DELETE FROM sessions WHERE last_seen < ?", (int(time.time()) - keep_days * 86400,))
        await self.db.commit()

    # ---------- геокэш ----------

    async def geo_get(self, ip: str, max_age: int) -> aiosqlite.Row | None:
        async with self.db.execute(
            "SELECT * FROM geo_cache WHERE ip = ? AND ts >= ?", (ip, int(time.time()) - max_age)
        ) as cur:
            return await cur.fetchone()

    async def geo_put(self, ip: str, country: str, city: str, isp: str, source: str) -> None:
        await self.db.execute(
            "INSERT OR REPLACE INTO geo_cache (ip, country, city, isp, source, ts) VALUES (?, ?, ?, ?, ?, ?)",
            (ip, country, city, isp, source, int(time.time())),
        )
        await self.db.commit()

    # ---------- meta ----------

    async def meta_get(self, key: str) -> str | None:
        async with self.db.execute("SELECT value FROM meta WHERE key = ?", (key,)) as cur:
            row = await cur.fetchone()
        return row[0] if row else None

    async def meta_set(self, key: str, value: str) -> None:
        await self.db.execute("INSERT OR REPLACE INTO meta (key, value) VALUES (?, ?)", (key, value))
        await self.db.commit()
