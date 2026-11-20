"""Краткая диагностика сервера: каждая проверка - одна строка со статусом."""

from __future__ import annotations

import asyncio
import os
import socket
import time
from dataclasses import dataclass
from datetime import datetime, timezone

import httpx

from .xray import run_cmd

OK, WARN, FAIL = "✅", "⚠️", "❌"
TRACE_URL = "https://www.cloudflare.com/cdn-cgi/trace"


@dataclass
class Check:
    status: str
    title: str
    detail: str

    def line(self) -> str:
        return f"{self.status} <b>{self.title}</b>: {self.detail}"


async def _service(name: str, required: bool = True) -> Check:
    rc, out, _ = await run_cmd("systemctl", "is-active", name, timeout=5)
    state = out.strip() or "unknown"
    if state == "active":
        return Check(OK, name, "работает")
    if not required and state in ("inactive", "unknown"):
        rc2, out2, _ = await run_cmd("systemctl", "list-unit-files", f"{name}.service", timeout=5)
        if name not in out2:
            return Check(WARN, name, "не установлен")
    return Check(FAIL if required else WARN, name, state)


def _parse_trace(text: str) -> dict[str, str]:
    return dict(line.split("=", 1) for line in text.splitlines() if "=" in line)


async def _trace(proxy: str | None) -> tuple[dict[str, str], float]:
    t0 = time.monotonic()
    # без прокси принудительно IPv4, чтобы сравнить адрес с A-записью домена
    kwargs = {"proxy": proxy} if proxy else {"transport": httpx.AsyncHTTPTransport(local_address="0.0.0.0")}
    async with httpx.AsyncClient(timeout=10, trust_env=False, **kwargs) as client:
        resp = await client.get(TRACE_URL)
        resp.raise_for_status()
    return _parse_trace(resp.text), (time.monotonic() - t0) * 1000


async def chain_check(proxy: str | None) -> Check:
    if not proxy:
        return Check(WARN, "Цепочка RU → NL", "не найден SOCKS-вход моста, проверка пропущена")
    try:
        info, ms = await _trace(proxy)
    except Exception as e:  # noqa: BLE001
        return Check(FAIL, "Цепочка RU → NL", f"не работает ({type(e).__name__})")
    loc, ip = info.get("loc", "?"), info.get("ip", "?")
    status = OK if loc != "RU" else FAIL
    return Check(status, "Цепочка RU → NL", f"выход {ip} ({loc}), {ms:.0f} мс")


async def _direct() -> tuple[Check, str | None]:
    try:
        info, ms = await _trace(None)
    except Exception as e:  # noqa: BLE001
        return Check(FAIL, "Интернет с сервера", f"нет доступа ({type(e).__name__})"), None
    return Check(OK, "Интернет с сервера", f"{info.get('ip', '?')} ({info.get('loc', '?')}), {ms:.0f} мс"), info.get("ip")


async def _cert(path) -> Check:
    if not os.path.exists(path):
        return Check(FAIL, "TLS-сертификат", f"файл {path} не найден")
    rc, out, err = await run_cmd("openssl", "x509", "-enddate", "-noout", "-in", str(path), timeout=5)
    if rc != 0 or "=" not in out:
        return Check(WARN, "TLS-сертификат", "не удалось прочитать срок")
    end = datetime.strptime(out.strip().split("=", 1)[1], "%b %d %H:%M:%S %Y %Z").replace(tzinfo=timezone.utc)
    days = (end - datetime.now(timezone.utc)).days
    status = OK if days >= 14 else (WARN if days >= 3 else FAIL)
    return Check(status, "TLS-сертификат", f"действует ещё {days} дн. (до {end:%d.%m.%Y})")


async def _ports() -> Check:
    rc, out, _ = await run_cmd("ss", "-Htln", timeout=5)
    listening = set()
    for line in out.splitlines():
        cols = line.split()
        if len(cols) >= 4:
            listening.add(cols[3].rsplit(":", 1)[-1])
    missing = [p for p in ("80", "443") if p not in listening]
    if missing:
        return Check(FAIL, "Порты", "не слушаются: " + ", ".join(missing))
    return Check(OK, "Порты", "80 и 443 слушаются")


async def _dns(domain: str, server_ip: str | None) -> Check:
    loop = asyncio.get_running_loop()
    try:
        infos = await loop.getaddrinfo(domain, 443, family=socket.AF_INET)
    except socket.gaierror:
        return Check(FAIL, "DNS", f"{domain} не резолвится")
    ips = sorted({i[4][0] for i in infos})
    if server_ip and ":" not in server_ip and server_ip not in ips:
        return Check(WARN, "DNS", f"{domain} → {', '.join(ips)}, а сервер {server_ip}")
    return Check(OK, "DNS", f"{domain} → {', '.join(ips)}")


def _uptime() -> Check:
    with open("/proc/uptime") as f:
        up = float(f.read().split()[0])
    return Check(OK, "Аптайм", f"{int(up // 86400)} д {int(up % 86400 // 3600)} ч")


def problems_key(checks: list[Check]) -> list[str]:
    """Набор проблем без деталей (задержки и проценты меняются при каждом запуске)."""
    return sorted(f"{c.status} {c.title}" for c in checks if c.status != OK)


def format_report(checks: list[Check], when: str, autodiag_min: int | None) -> str:
    bad = len(problems_key(checks))
    head = "🩺 <b>Диагностика</b>: " + ("всё в порядке" if not bad else f"есть замечания: {bad}")
    text = head + f"\n<i>{when}</i>\n\n" + "\n".join(c.line() for c in checks)
    return text + autodiag_footer(autodiag_min)


def autodiag_footer(autodiag_min: int | None) -> str:
    return f"\n\n🔔 Автодиагностика включена (каждые {autodiag_min} минут)" if autodiag_min else ""


async def run_diagnostics(
    settings, api, manager, users, socks_url: str | None, stability: Check | None = None
) -> list[Check]:
    services = await asyncio.gather(_service("xray"), _service("nginx"), _service("fail2ban", required=False))
    (direct, server_ip), chain, cert, ports, version, api_ok = await asyncio.gather(
        _direct(), chain_check(socks_url), _cert(settings.cert_path), _ports(), api.version(), api.ping()
    )
    dns = await _dns(settings.domain, server_ip)

    in_sync, why = manager.config_in_sync(users)
    checks = [
        *services,
        Check(OK if api_ok else FAIL, "Xray API", f"отвечает, {version}" if api_ok else "не отвечает"),
        Check(OK if in_sync else FAIL, "Клиенты в конфиге", f"{len(users)} шт., совпадают с ботом" if in_sync else why),
        chain,
        direct,
        dns,
        cert,
        ports,
        *([stability] if stability else []),
        _uptime(),
    ]
    return checks
