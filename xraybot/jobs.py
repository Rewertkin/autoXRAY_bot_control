"""Фоновые задачи: сбор трафика, онлайн и сессии, истечение гостей, лимиты, контроль конфига."""

from __future__ import annotations

import asyncio
import logging
import time
from collections.abc import Awaitable, Callable

from . import fmt
from .diag import OK, chain_check, format_report, problems_key, run_diagnostics
from .service import ClientService
from .xray import XrayError

log = logging.getLogger(__name__)

Notify = Callable[[str, object | None], Awaitable[None]]  # (текст, клавиатура или None)

QUOTA_LEVELS = (80, 100)


class Jobs:
    def __init__(
        self,
        settings,
        service: ClientService,
        notify: Notify,
        sync_keyboard_factory,
        diag_keyboard_factory=None,
        socks_url: str | None = None,
    ):
        self.s = settings
        self.svc = service
        self.db = service.db
        self.notify = notify
        self.sync_keyboard_factory = sync_keyboard_factory
        self._tasks: list[asyncio.Task] = []
        self._sync_alerted = False
        self.diag_keyboard_factory = diag_keyboard_factory
        self.socks_url = socks_url
        self._last_problems: list[str] = []

    def start(self) -> None:
        self._tasks = [
            asyncio.create_task(self._loop("traffic", self.s.traffic_poll_sec, self.poll_traffic)),
            asyncio.create_task(self._loop("online", self.s.online_poll_sec, self.poll_online)),
            asyncio.create_task(self._loop("expiry", 60, self.expire_guests)),
            asyncio.create_task(self._loop("config", 120, self.check_config)),
            asyncio.create_task(self._loop("cleanup", 24 * 3600, self.db.prune_sessions)),
            asyncio.create_task(self._loop("chain", self.s.chain_probe_sec, self.probe_chain)),
            asyncio.create_task(self._loop("autodiag", self.s.autodiag_min * 60, self.autodiag, initial_delay=60)),
        ]

    async def stop(self) -> None:
        for t in self._tasks:
            t.cancel()
        await asyncio.gather(*self._tasks, return_exceptions=True)

    async def _loop(self, name: str, interval: int, fn, initial_delay: int = 0) -> None:
        # автодиагностику не запускаем сразу: после рестарта Xray нужно время подняться
        await asyncio.sleep(initial_delay)
        while True:
            try:
                await fn()
            except asyncio.CancelledError:
                raise
            except Exception:  # noqa: BLE001 - задача не должна умирать из-за единичной ошибки
                log.exception("job %s failed", name)
            await asyncio.sleep(interval)

    async def poll_traffic(self) -> None:
        try:
            stats = await self.svc.xray.api.query_traffic(reset=True)
        except XrayError as e:
            log.warning("traffic poll: %s", e)
            return
        if not stats:
            return
        clients = {c.name: c for c in await self.db.list_clients()}
        deltas = {clients[email].id: v for email, v in stats.items() if email in clients}
        if deltas:
            await self.db.add_traffic(deltas, int(time.time()))
            await self.check_quotas([c for c in clients.values() if c.id in deltas])

    async def check_quotas(self, clients) -> None:
        now = int(time.time())
        for c in clients:
            if not c.quota_bytes:
                continue
            used = await self.db.period_usage(c, now)
            pct = used * 100 / c.quota_bytes
            for level in QUOTA_LEVELS:
                if pct >= level and await self.db.mark_quota_alert(c.id, c.period_index(now), level):
                    _, end = c.period_bounds(now)
                    icon = "🔴" if level >= 100 else "🟡"
                    await self.notify(
                        f"{icon} <b>{fmt.h(c.name)}</b>: израсходовано {level}% лимита\n"
                        f"{fmt.size(used)} из {fmt.size(c.quota_bytes)}, период до {fmt.dt(end)}.\n"
                        f"Доступ не ограничивается, это только предупреждение.",
                        None,
                    )

    async def poll_online(self) -> None:
        clients = {c.name: c for c in await self.db.list_clients()}
        try:
            online = await self.svc.xray.api.online(list(clients))
        except XrayError as e:
            log.warning("online poll: %s", e)
            return
        now = int(time.time())
        snapshot = {}
        for email, ips in online.items():
            client = clients.get(email)
            if not client:
                continue
            snapshot[email] = ips
            for ip, seen in ips.items():
                await self.db.touch_session(client.id, ip, min(seen or now, now))
        self.svc.online = snapshot

    async def expire_guests(self) -> None:
        now = int(time.time())
        for c in await self.db.expired_guests(now):
            up, down = await self.db.traffic_total(c.id)
            errors = await self.svc.delete(c)
            text = (
                f"⌛ Гостевой доступ <b>{fmt.h(c.name)}</b> истёк и удалён.\n"
                f"Передано за всё время: {fmt.size(up + down)}."
            )
            if errors:
                text += "\n⚠️ Xray: " + fmt.h("; ".join(errors))
            await self.notify(text, None)

    async def check_config(self) -> None:
        users = await self.svc.all_managed()
        ok, why = self.svc.xray.config_in_sync(users)
        if ok:
            self._sync_alerted = False
            return
        if not self._sync_alerted:
            self._sync_alerted = True
            await self.notify(
                "⚠️ <b>Конфиг Xray не совпадает с ботом</b>\n"
                f"{fmt.h(why)}\n\n"
                "Обычно так бывает после повторного запуска скрипта моста. "
                "Нажми кнопку, чтобы вернуть клиентов и статистику в конфиг (Xray будет перезапущен).",
                self.sync_keyboard_factory(),
            )

    async def autodiag(self) -> None:
        """Фоновая диагностика. Пишет, когда появились или изменились проблемы, и когда всё починилось."""
        if not await self.svc.autodiag_enabled():
            self._last_problems = []
            return
        users = await self.svc.all_managed()
        checks = await run_diagnostics(
            self.s, self.svc.xray.api, self.svc.xray, users, self.socks_url, stability=await self.svc.stability_check()
        )
        problems = problems_key(checks)
        if problems and problems != self._last_problems:
            text = "🚨 <b>Автодиагностика нашла проблемы</b>\n\n" + format_report(
                checks, fmt.dt(int(time.time())), self.s.autodiag_min
            )
            markup = self.diag_keyboard_factory(True) if self.diag_keyboard_factory else None
            await self.notify(text, markup)
        elif not problems and self._last_problems:
            await self.notify("✅ <b>Автодиагностика</b>: проблемы устранены, всё работает.", None)
        self._last_problems = problems

    async def probe_chain(self) -> None:
        """Проверка цепочки RU -> NL. Разрыв засчитывается, только если и повторная попытка не прошла."""
        if not self.socks_url:
            return
        check = await chain_check(self.socks_url)
        if check.status != OK:
            await asyncio.sleep(5)
            check = await chain_check(self.socks_url)
        await self.svc.record_chain(check.status == OK)
