"""Проверка моста: доступен ли с RU сервера зарубежный сервер и отдаёт ли он сайт-заглушку."""

from __future__ import annotations

import asyncio
import re
import socket
import ssl
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from html import escape as html_escape
from html import unescape

import httpx

from .diag import FAIL, OK, WARN, Check

TITLE_RE = re.compile(r"<title[^>]*>(.*?)</title>", re.IGNORECASE | re.DOTALL)


@dataclass
class BridgeTarget:
    tag: str
    address: str
    port: int
    sni: str
    network: str
    path: str = ""


@dataclass
class BridgeReport:
    target: BridgeTarget
    checks: list[Check] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return all(c.status == OK for c in self.checks)


def bridge_targets(cfg: dict) -> list[BridgeTarget]:
    """Зарубежные серверы из outbound'ов моста autoXRAY (теги proxy-0, proxy-1, ...)."""
    result = []
    for out in cfg.get("outbounds") or []:
        tag = out.get("tag", "")
        if out.get("protocol") != "vless" or not tag.startswith("proxy"):
            continue
        vnext = ((out.get("settings") or {}).get("vnext") or [{}])[0]
        address = vnext.get("address")
        if not address:
            continue
        stream = out.get("streamSettings") or {}
        tls = stream.get("tlsSettings") or {}
        xhttp = stream.get("xhttpSettings") or {}
        result.append(
            BridgeTarget(
                tag=tag,
                address=address,
                port=int(vnext.get("port") or 443),
                sni=tls.get("serverName") or address,
                network=stream.get("network", "tcp"),
                path=xhttp.get("path", ""),
            )
        )
    return result


def page_title(html_text: str) -> str:
    m = TITLE_RE.search(html_text)
    return re.sub(r"\s+", " ", unescape(m.group(1))).strip()[:60] if m else ""


async def _resolve(host: str) -> list[str]:
    loop = asyncio.get_running_loop()
    infos = await loop.getaddrinfo(host, None, family=socket.AF_INET, type=socket.SOCK_STREAM)
    return sorted({i[4][0] for i in infos})


async def _tls(target: BridgeTarget) -> tuple[float, datetime | None, str]:
    """TLS-рукопожатие с проверкой сертификата. Возвращает (мс, срок сертификата, версия TLS)."""
    ctx = ssl.create_default_context()
    ctx.set_alpn_protocols(["h2", "http/1.1"])
    t0 = time.monotonic()
    reader, writer = await asyncio.wait_for(
        asyncio.open_connection(target.address, target.port, ssl=ctx, server_hostname=target.sni), timeout=10
    )
    ms = (time.monotonic() - t0) * 1000
    try:
        cert = writer.get_extra_info("peercert") or {}
        ssl_obj = writer.get_extra_info("ssl_object")
        version = ssl_obj.version() if ssl_obj else "?"
        not_after = cert.get("notAfter")
        expires = (
            datetime.strptime(not_after, "%b %d %H:%M:%S %Y %Z").replace(tzinfo=timezone.utc) if not_after else None
        )
    finally:
        writer.close()
        try:
            await writer.wait_closed()
        except Exception:  # noqa: BLE001
            pass
    return ms, expires, version


async def _stub_page(target: BridgeTarget) -> tuple[int, str, int, float]:
    """GET главной страницы, как браузер. Возвращает (код, заголовок страницы, размер, мс)."""
    url = f"https://{target.sni}:{target.port}/" if target.port != 443 else f"https://{target.sni}/"
    headers = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/131.0 Safari/537.36",
        "Accept": "text/html,application/xhtml+xml",
    }
    t0 = time.monotonic()
    async with httpx.AsyncClient(timeout=15, trust_env=False, follow_redirects=True, headers=headers) as client:
        resp = await client.get(url)
    ms = (time.monotonic() - t0) * 1000
    ctype = resp.headers.get("content-type", "")
    title = page_title(resp.text) if "html" in ctype else ""
    return resp.status_code, title, len(resp.content), ms


async def check_target(target: BridgeTarget) -> BridgeReport:
    report = BridgeReport(target)
    add = report.checks.append

    try:
        ips = await _resolve(target.address)
        add(Check(OK, "DNS", f"{target.address} → {', '.join(ips)}"))
    except OSError:
        add(Check(FAIL, "DNS", f"{target.address} не резолвится"))
        return report

    try:
        ms, expires, version = await _tls(target)
    except ssl.SSLCertVerificationError as e:
        add(Check(FAIL, "TLS", f"сертификат не прошёл проверку: {html_escape(str(e.verify_message))}"))
        return report
    except asyncio.TimeoutError:
        add(Check(FAIL, "TLS", f"нет соединения с {target.address}:{target.port} (таймаут 10 с)"))
        return report
    except OSError as e:
        reason = html_escape((str(e).strip() or type(e).__name__)[:120])
        add(Check(FAIL, "TLS", f"нет соединения с {target.address}:{target.port}: {reason}"))
        return report
    detail = f"{version}, рукопожатие {ms:.0f} мс"
    status = OK
    if expires:
        days = (expires - datetime.now(timezone.utc)).days
        detail += f", сертификат ещё {days} дн."
        if days < 14:
            status = WARN if days >= 3 else FAIL
    add(Check(status, "TLS", detail))

    try:
        code, title, size, ms = await _stub_page(target)
    except httpx.HTTPError as e:
        reason = str(e).strip() or type(e).__name__
        add(Check(FAIL, "Сайт-заглушка", f"страница не открылась: {html_escape(reason[:120])}"))
        return report
    if code == 200 and size > 0:
        name = f"«{title}»" if title else "страница без заголовка"
        add(Check(OK, "Сайт-заглушка", f"{name}, {size / 1024:.1f} КБ за {ms:.0f} мс"))
    else:
        add(Check(FAIL, "Сайт-заглушка", f"ответ HTTP {code}, {size} байт"))
    return report


async def check_bridge(cfg: dict) -> list[BridgeReport]:
    targets = bridge_targets(cfg)
    return list(await asyncio.gather(*(check_target(t) for t in targets)))
