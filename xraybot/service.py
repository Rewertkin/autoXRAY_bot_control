"""Бизнес-логика: создание и удаление клиентов, статус, статистика."""

from __future__ import annotations

import logging
import re
import time
from dataclasses import dataclass, field

from .db import Client, Database
from .diag import FAIL, OK, Check
from .fmt import dt, local_midnight, span
from .geo import Geo, GeoResolver
from .links import LinkBuilder, new_uuid
from .xray import ManagedUser, XrayConfigFile, XrayManager

log = logging.getLogger(__name__)

NAME_RE = re.compile(r"^[0-9A-Za-zА-Яа-яЁё_.\-]{1,32}$")
GB = 1024**3


def to_managed(c: Client) -> ManagedUser:
    return ManagedUser(email=c.name, uuid=c.uuid, use_xhttp=c.use_xhttp, use_vision=c.use_vision)


@dataclass
class ClientStatus:
    online: bool
    last_seen: int | None
    current_ips: list[tuple[str, Geo]] = field(default_factory=list)
    period_used: int = 0
    period_start: int = 0
    period_end: int = 0
    total: int = 0
    today: int = 0


class ClientService:
    def __init__(self, settings, db: Database, xray: XrayManager, links: LinkBuilder, geo: GeoResolver):
        self.s = settings
        self.db = db
        self.xray = xray
        self.links = links
        self.geo = geo
        # последний снимок онлайна: email -> {ip: last_seen}
        self.online: dict[str, dict[str, int]] = {}

    async def autodiag_enabled(self) -> bool:
        return await self.db.meta_get("autodiag") == "1"

    async def set_autodiag(self, enabled: bool) -> None:
        await self.db.meta_set("autodiag", "1" if enabled else "0")

    # ---------- стабильность цепочки RU -> NL ----------

    async def _meta_int(self, key: str) -> int | None:
        value = await self.db.meta_get(key)
        return int(value) if value else None

    async def record_chain(self, up: bool, now: int | None = None) -> None:
        """Результат фоновой проверки цепочки. Хранит начало текущего периода без разрывов и последний разрыв."""
        now = now or int(time.time())
        down_since = await self._meta_int("chain_down_since")
        ok_since = await self._meta_int("chain_ok_since")
        if up:
            if down_since:
                await self.db.meta_set("chain_last_outage_start", str(down_since))
                await self.db.meta_set("chain_last_outage_end", str(now))
                await self.db.meta_set("chain_down_since", "")
                await self.db.meta_set("chain_ok_since", str(now))
            elif not ok_since:
                await self.db.meta_set("chain_ok_since", str(now))
        elif not down_since:
            await self.db.meta_set("chain_down_since", str(now))
            await self.db.meta_set("chain_ok_since", "")

    async def stability_check(self, now: int | None = None) -> Check:
        now = now or int(time.time())
        down_since = await self._meta_int("chain_down_since")
        if down_since:
            return Check(FAIL, "Без разрывов", f"связи RU → NL нет с {dt(down_since)} (уже {span(now - down_since)})")
        ok_since = await self._meta_int("chain_ok_since")
        if not ok_since:
            return Check(OK, "Без разрывов", "наблюдение только началось")
        detail = span(now - ok_since)
        start = await self._meta_int("chain_last_outage_start")
        end = await self._meta_int("chain_last_outage_end")
        if start and end:
            detail += f" (последний разрыв {dt(start)}, длился {span(end - start)})"
        else:
            detail += " (разрывов не было с начала наблюдения)"
        return Check(OK, "Без разрывов", detail)

    async def all_managed(self) -> list[ManagedUser]:
        return [to_managed(c) for c in await self.db.list_clients()]

    @staticmethod
    def validate_name(name: str) -> str | None:
        if not NAME_RE.match(name):
            return "Имя: 1-32 символа, буквы, цифры, точка, дефис или подчёркивание, без пробелов."
        return None

    async def create(
        self, name: str, kind: str, days: int | None, quota_gb: int | None, use_xhttp: bool, use_vision: bool
    ) -> tuple[Client, list[str]]:
        now = int(time.time())
        expires = now + days * 86400 if (kind == "guest" and days) else None
        quota = quota_gb * GB if quota_gb else None
        client = await self.db.add_client(name, new_uuid(), kind, expires, quota, use_xhttp, use_vision, now)
        try:
            await self.xray.write_users(await self.all_managed())
        except Exception:
            await self.db.delete_client(client.id)
            raise
        warnings = await self.xray.live_add(to_managed(client))
        return client, warnings

    async def import_base_client(self) -> str | None:
        """Берёт под управление общий клиент из скрипта моста (UUID без email) под именем main.

        UUID не меняется, поэтому ссылки со страницы подписки autoXRAY продолжают работать.
        Если скрипт моста запускали повторно и UUID новый, обновляет его у main.
        Изменения вступают в силу после записи конфига и перезапуска Xray (это делает patch).
        """
        cfg = self.xray.config.load()
        found: dict[str, set[str]] = {}
        for tag, key in ((self.s.xhttp_tag, "xhttp"), (self.s.vision_tag, "vision")):
            inbound = XrayConfigFile.find_inbound(cfg, tag) or {}
            for c in (inbound.get("settings") or {}).get("clients") or []:
                if not c.get("email") and c.get("id"):
                    found.setdefault(c["id"], set()).add(key)
        if not found:
            return None
        uuid, where = next(iter(found.items()))
        existing = await self.db.get_system_client()
        if existing:
            await self.db.update_system_client(existing.id, uuid, "xhttp" in where, "vision" in where)
            return existing.name
        name = "main"
        if await self.db.get_client_by_name(name):
            name = "main-bridge"
        await self.db.add_client(
            name, uuid, "permanent", None, None, "xhttp" in where, "vision" in where, is_system=True
        )
        return name

    async def delete(self, client: Client) -> list[str]:
        if client.is_system:
            raise ValueError("Клиент main создан скриптом моста, его удалять нельзя")
        await self.db.delete_client(client.id)
        await self.xray.write_users(await self.all_managed())
        self.online.pop(client.name, None)
        return await self.xray.live_remove(client.name)

    async def extend(self, client: Client, days: int) -> Client:
        base = max(client.expires_at or 0, int(time.time()))
        await self.db.set_expires(client.id, base + days * 86400)
        return await self.db.get_client(client.id)  # type: ignore[return-value]

    async def set_quota(self, client: Client, quota_gb: int | None) -> Client:
        await self.db.set_quota(client.id, quota_gb * GB if quota_gb else None)
        return await self.db.get_client(client.id)  # type: ignore[return-value]

    def is_online(self, client: Client, last_seen: int | None) -> bool:
        if client.name in self.online:
            return True
        return bool(last_seen and time.time() - last_seen <= self.s.online_window_sec)

    async def status(self, client: Client, with_geo: bool = True) -> ClientStatus:
        now = int(time.time())
        last_seen = await self.db.last_seen(client.id)
        current = self.online.get(client.name, {})
        if current:
            last_seen = max([last_seen or 0, *current.values()])
        ips = list(current) or await self.db.ips_seen_since(client.id, now - self.s.online_window_sec)
        geo_ips = []
        if with_geo:
            for ip in ips[:5]:
                geo_ips.append((ip, await self.geo.lookup(ip)))
        start, end = client.period_bounds(now)
        up, down = await self.db.traffic_since(client.id, start)
        tu, td = await self.db.traffic_total(client.id)
        du, dd = await self.db.traffic_since(client.id, local_midnight(now))
        return ClientStatus(
            online=self.is_online(client, last_seen),
            last_seen=last_seen,
            current_ips=geo_ips,
            period_used=up + down,
            period_start=start,
            period_end=end,
            total=tu + td,
            today=du + dd,
        )
