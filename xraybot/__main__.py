"""Точка входа.

    python -m xraybot run      запустить бота (по умолчанию)
    python -m xraybot patch    включить API/статистику в конфиге Xray, записать клиентов и перезапустить Xray
    python -m xraybot check    проверить настройки: API, шаблоны ссылок, прокси для Telegram
"""

from __future__ import annotations

import asyncio
import logging
import sys

from . import fmt
from .config import Settings
from .db import Database
from .geo import GeoResolver
from .links import LinkBuilder
from .service import ClientService
from .xray import XrayAPI, XrayConfigFile, XrayManager

log = logging.getLogger("xraybot")


def build(settings: Settings):
    fmt.set_timezone(settings.timezone)
    db = Database(settings.db_path)
    config = XrayConfigFile(settings.xray_config, settings.xray_bin)
    api = XrayAPI(settings.xray_bin, settings.api_addr)
    manager = XrayManager(settings, config, api)
    links = LinkBuilder(settings, config.load)
    geo = GeoResolver(db)
    svc = ClientService(settings, db, manager, links, geo)
    return db, config, svc


def resolve_tg_proxy(settings: Settings, config: XrayConfigFile) -> str | None:
    value = settings.tg_proxy
    if value.lower() in ("", "none", "off", "no"):
        return None
    if value.lower() == "auto":
        return config.bot_proxy_url(settings.socks_tag)
    return value


async def cmd_patch(settings: Settings) -> int:
    db, config, svc = build(settings)
    await db.open()
    try:
        main_name = await svc.import_base_client()
        if main_name:
            print(f"Общий клиент из скрипта моста под управлением бота: {main_name}")
        changed = await svc.xray.write_users(await svc.all_managed(), patch_api=True)
        print("Конфиг Xray записан" + (" (включены API и статистика)" if changed else ""))
        ok, info = await svc.xray.restart_xray()
        print("Xray перезапущен, API отвечает" if ok else f"Ошибка: {info}")
        return 0 if ok else 1
    finally:
        await db.close()


async def cmd_check(settings: Settings) -> int:
    db, config, svc = build(settings)
    await db.open()
    try:
        print("Xray API:", "отвечает" if await svc.xray.api.ping() else "НЕ отвечает")
        print("Версия:", await svc.xray.api.version())
        ok, why = svc.xray.config_in_sync(await svc.all_managed())
        print("Клиенты в конфиге:", "совпадают" if ok else why)
        for kind, tpl in svc.links.templates().items():
            print(f"Шаблон {kind}: {tpl[:90]}...")
        proxy = resolve_tg_proxy(settings, config)
        print("Прокси для Telegram:", proxy.split("@")[-1] if proxy else "нет")
        for label, url in (("через SOCKS моста", proxy), ("напрямую", None)):
            if label.startswith("через") and not url:
                continue
            print(f"Telegram {label}:", await _tg_probe(settings, url))
        return 0
    finally:
        await svc.geo.close()
        await db.close()


async def _tg_probe(settings: Settings, proxy: str | None) -> str:
    from aiogram import Bot
    from aiogram.client.session.aiohttp import AiohttpSession

    bot = Bot(settings.bot_token, session=AiohttpSession(proxy=proxy) if proxy else AiohttpSession())
    try:
        me = await asyncio.wait_for(bot.get_me(), timeout=20)
        return f"OK, бот @{me.username}"
    except Exception as e:  # noqa: BLE001
        return f"ОШИБКА {type(e).__name__}: {e}"
    finally:
        await bot.session.close()


async def cmd_run(settings: Settings) -> int:
    from aiogram import Bot, Dispatcher
    from aiogram.client.default import DefaultBotProperties
    from aiogram.client.session.aiohttp import AiohttpSession
    from aiogram.enums import ParseMode

    from .bot import handlers, keyboards
    from .jobs import Jobs

    db, config, svc = build(settings)
    await db.open()

    def make_bot(proxy_url: str | None) -> Bot:
        session = AiohttpSession(proxy=proxy_url) if proxy_url else AiohttpSession()
        return Bot(settings.bot_token, session=session, default=DefaultBotProperties(parse_mode=ParseMode.HTML))

    async def probe(candidate: Bot, attempts: int = 3) -> str | None:
        """None, если Telegram отвечает, иначе текст последней ошибки."""
        error = "?"
        for i in range(attempts):
            try:
                me = await asyncio.wait_for(candidate.get_me(), timeout=20)
                log.info("Telegram отвечает, бот @%s", me.username)
                return None
            except Exception as e:  # noqa: BLE001
                error = f"{type(e).__name__}: {e}"
                log.warning("Telegram недоступен (попытка %d/%d): %s", i + 1, attempts, error)
                await asyncio.sleep(3)
        return error

    proxy = resolve_tg_proxy(settings, config)
    bot = make_bot(proxy)
    if proxy:
        error = await probe(bot)
        if error and settings.tg_proxy.lower() == "auto":
            direct = make_bot(None)
            if await probe(direct, attempts=2) is None:
                log.warning("Через SOCKS моста Telegram недоступен (%s), работаю напрямую", error)
                await bot.session.close()
                bot, proxy = direct, None
            else:
                await direct.session.close()
                log.error("Telegram недоступен ни через мост, ни напрямую. Продолжаю попытки через мост.")
    log.info("Telegram через %s", "SOCKS моста" if proxy else "прямое подключение")

    async def notify(text: str, markup=None) -> None:
        for admin in settings.admin_ids:
            try:
                await bot.send_message(admin, text, reply_markup=markup, disable_web_page_preview=True)
            except Exception:  # noqa: BLE001
                log.exception("notify %s failed", admin)

    handlers.setup_access(settings.admin_ids)
    dp = Dispatcher(svc=svc, socks_url=config.bot_proxy_url(settings.socks_tag))
    dp.include_router(handlers.router)

    jobs = Jobs(
        settings,
        svc,
        notify,
        keyboards.sync_config,
        keyboards.diag_again,
        socks_url=config.bot_proxy_url(settings.socks_tag),
    )
    jobs.start()
    try:
        await startup(bot, notify, keyboards)
        await dp.start_polling(bot)
    finally:
        await jobs.stop()
        await svc.geo.close()
        await bot.session.close()
        await db.close()
    return 0


async def startup(bot, notify, keyboards) -> None:
    """Команды меню и приветствие. Сетевые ошибки здесь не должны ронять бота."""
    from aiogram.types import BotCommand

    try:
        await bot.set_my_commands(
            [
                BotCommand(command="menu", description="Главное меню"),
                BotCommand(command="new", description="Новый клиент"),
                BotCommand(command="clients", description="Клиенты"),
                BotCommand(command="traffic", description="Трафик"),
                BotCommand(command="diag", description="Диагностика"),
                BotCommand(command="bridge", description="Проверить мост"),
            ]
        )
    except Exception as e:  # noqa: BLE001
        log.warning("set_my_commands: %s", e)
    await notify("🤖 Бот запущен.", keyboards.main_menu())


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    logging.getLogger("httpx").setLevel(logging.WARNING)
    logging.getLogger("aiogram.event").setLevel(logging.WARNING)
    command = sys.argv[1] if len(sys.argv) > 1 else "run"
    settings = Settings.load()
    handler = {"run": cmd_run, "patch": cmd_patch, "check": cmd_check}.get(command)
    if handler is None:
        print(__doc__)
        sys.exit(2)
    sys.exit(asyncio.run(handler(settings)))


if __name__ == "__main__":
    main()
