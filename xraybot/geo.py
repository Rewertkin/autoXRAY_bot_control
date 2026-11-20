"""Геолокация IP с ротацией бесплатных сервисов и кэшем в базе.

Важно: запросы идут напрямую с сервера, а не через мост: в маршрутизации
autoXRAY сервисы определения IP заблокированы.
"""

from __future__ import annotations

import ipaddress
import logging
import time
from dataclasses import dataclass

import httpx

log = logging.getLogger(__name__)

CACHE_TTL = 7 * 24 * 3600
COOLDOWN_SEC = 15 * 60  # сервис, ответивший ошибкой или лимитом, пропускаем какое-то время


@dataclass
class Geo:
    country: str = ""
    city: str = ""
    isp: str = ""
    source: str = ""

    def short(self) -> str:
        place = ", ".join(x for x in (self.city, self.country) if x)
        parts = [p for p in (place, self.isp) if p]
        return " · ".join(parts) if parts else "неизвестно"


def _ip_api(d: dict) -> Geo | None:
    if d.get("status") != "success":
        return None
    return Geo(d.get("country", ""), d.get("city", ""), d.get("isp") or d.get("org", ""))


def _ipwho(d: dict) -> Geo | None:
    if not d.get("success"):
        return None
    conn = d.get("connection") or {}
    return Geo(d.get("country", ""), d.get("city", ""), conn.get("isp") or conn.get("org", ""))


def _ipinfo(d: dict) -> Geo | None:
    if "bogon" in d or not (d.get("country") or d.get("city")):
        return None
    org = d.get("org", "")
    if org.startswith("AS") and " " in org:
        org = org.split(" ", 1)[1]
    return Geo(d.get("country", ""), d.get("city", ""), org)


def _ipapi_co(d: dict) -> Geo | None:
    if d.get("error"):
        return None
    return Geo(d.get("country_name", ""), d.get("city", ""), d.get("org", ""))


PROVIDERS = [
    ("ip-api.com", "http://ip-api.com/json/{ip}?fields=status,country,city,isp,org&lang=ru", _ip_api),
    ("ipwho.is", "https://ipwho.is/{ip}?lang=ru", _ipwho),
    ("ipinfo.io", "https://ipinfo.io/{ip}/json", _ipinfo),
    ("ipapi.co", "https://ipapi.co/{ip}/json/", _ipapi_co),
]


class GeoResolver:
    def __init__(self, db):
        self.db = db
        self._next = 0
        self._cooldown: dict[str, float] = {}
        self._client = httpx.AsyncClient(timeout=6, trust_env=False, headers={"User-Agent": "xraybot/1.0"})

    async def close(self) -> None:
        await self._client.aclose()

    async def lookup(self, ip: str) -> Geo:
        try:
            addr = ipaddress.ip_address(ip)
            if addr.is_private or addr.is_loopback or addr.is_reserved:
                return Geo(isp="локальная сеть")
        except ValueError:
            return Geo()

        row = await self.db.geo_get(ip, CACHE_TTL)
        if row:
            return Geo(row["country"] or "", row["city"] or "", row["isp"] or "", row["source"] or "")

        n = len(PROVIDERS)
        start = self._next
        self._next = (self._next + 1) % n  # ротация: каждый новый IP начинаем со следующего сервиса
        now = time.monotonic()
        for i in range(n):
            name, url, parse = PROVIDERS[(start + i) % n]
            if self._cooldown.get(name, 0) > now:
                continue
            try:
                resp = await self._client.get(url.format(ip=ip))
                if resp.status_code == 429:
                    raise httpx.HTTPStatusError("rate limited", request=resp.request, response=resp)
                resp.raise_for_status()
                geo = parse(resp.json())
            except Exception as e:  # noqa: BLE001 - любой сбой сервиса = пробуем следующий
                log.info("geo %s failed for %s: %s", name, ip, e)
                self._cooldown[name] = now + COOLDOWN_SEC
                continue
            if geo:
                geo.source = name
                await self.db.geo_put(ip, geo.country, geo.city, geo.isp, name)
                return geo
        return Geo()
