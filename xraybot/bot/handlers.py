"""Обработчики Telegram: меню, создание клиента, карточка, трафик, диагностика."""

from __future__ import annotations

import io
import logging
import math
import time

import qrcode
from aiogram import F, Router
from aiogram.exceptions import TelegramBadRequest
from aiogram.filters import Command, CommandStart
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import BufferedInputFile, CallbackQuery, Message

from .. import fmt
from ..bridge import check_bridge
from ..diag import autodiag_footer, chain_check, format_report, run_diagnostics
from ..service import ClientService
from ..xray import XrayError
from . import keyboards as kb

log = logging.getLogger(__name__)
router = Router(name="xraybot")


class NewClient(StatesGroup):
    name = State()
    wizard = State()


def setup_access(admin_ids: frozenset[int]) -> None:
    router.message.filter(F.from_user.id.in_(admin_ids))
    router.callback_query.filter(F.from_user.id.in_(admin_ids))


async def safe_edit(cb: CallbackQuery, text: str, markup=None) -> None:
    try:
        await cb.message.edit_text(text, reply_markup=markup, disable_web_page_preview=True)
    except TelegramBadRequest as e:
        if "message is not modified" not in str(e):
            raise


# ---------------------------------------------------------------- меню


@router.message(CommandStart())
@router.message(Command("menu"))
async def cmd_start(message: Message, state: FSMContext) -> None:
    await state.clear()
    await message.answer(
        "Бот управления клиентами моста autoXRAY.\nВыбери действие в меню ниже.", reply_markup=kb.main_menu()
    )


# ---------------------------------------------------------------- новый клиент


def _wizard_text(d: dict) -> str:
    lines = [f"➕ <b>Новый клиент: {fmt.h(d['name'])}</b>"]
    if d.get("kind"):
        kind = "постоянный" if d["kind"] == "permanent" else "гостевой"
        if d.get("days"):
            kind += f", {d['days']} дн."
        lines.append(f"Тип: {kind}")
    if d.get("conn_done"):
        conns = [n for n, on in (("XHTTP", d.get("xhttp")), ("VISION", d.get("vision"))) if on]
        lines.append("Подключения: " + ", ".join(conns))
    if "quota" in d:
        lines.append("Лимит: " + (f"{d['quota']} ГБ / 30 дней" if d["quota"] else "без лимита"))
    return "\n".join(lines)


@router.message(F.text == kb.BTN_NEW)
@router.message(Command("new"))
async def new_start(message: Message, state: FSMContext) -> None:
    await state.clear()
    await state.set_state(NewClient.name)
    await message.answer("Как назвать клиента? Например: <code>my_phone</code> или <code>guest-ivan</code>")


@router.message(NewClient.name, F.text)
async def new_name(message: Message, state: FSMContext, svc: ClientService) -> None:
    name = message.text.strip()
    if name in (kb.BTN_NEW, kb.BTN_LIST, kb.BTN_TRAFFIC, kb.BTN_DIAG, kb.BTN_BRIDGE):
        await state.clear()
        await message.answer("Создание отменено.")
        return
    error = svc.validate_name(name)
    if not error and await svc.db.get_client_by_name(name):
        error = "Клиент с таким именем уже есть."
    if error:
        await message.answer(f"{error}\nПопробуй другое имя.")
        return
    await state.set_state(NewClient.wizard)
    await state.update_data(name=name, xhttp=True, vision=True)
    await message.answer(_wizard_text({"name": name}) + "\n\nВыбери тип:", reply_markup=kb.new_kind())


@router.callback_query(NewClient.wizard, kb.NewCb.filter())
async def new_wizard(cb: CallbackQuery, callback_data: kb.NewCb, state: FSMContext, svc: ClientService) -> None:
    step, value = callback_data.step, callback_data.value
    d = await state.get_data()

    if step == "cancel":
        await state.clear()
        await safe_edit(cb, "Создание клиента отменено.")
    elif step == "kind":
        await state.update_data(kind=value, days=None)
        d = await state.get_data()
        if value == "guest":
            await safe_edit(cb, _wizard_text(d) + "\n\nНа какой срок?", kb.new_days())
        else:
            await safe_edit(cb, _wizard_text(d) + "\n\nКакие подключения выдать?", kb.new_conn(d["xhttp"], d["vision"]))
    elif step == "days":
        await state.update_data(days=int(value))
        d = await state.get_data()
        await safe_edit(cb, _wizard_text(d) + "\n\nКакие подключения выдать?", kb.new_conn(d["xhttp"], d["vision"]))
    elif step == "conn":
        if value in ("xhttp", "vision"):
            await state.update_data(**{value: not d.get(value)})
            d = await state.get_data()
            await safe_edit(cb, _wizard_text(d) + "\n\nКакие подключения выдать?", kb.new_conn(d["xhttp"], d["vision"]))
        else:
            if not (d.get("xhttp") or d.get("vision")):
                await cb.answer("Нужно выбрать хотя бы одно подключение", show_alert=True)
                return
            await state.update_data(conn_done=True)
            d = await state.get_data()
            await safe_edit(cb, _wizard_text(d) + "\n\nЛимит трафика на 30 дней (только предупреждение):", kb.new_quota())
    elif step == "quota":
        await state.update_data(quota=int(value))
        d = await state.get_data()
        await safe_edit(cb, _wizard_text(d) + "\n\nВсё верно?", kb.new_confirm())
    elif step == "ok":
        await state.clear()
        await safe_edit(cb, _wizard_text(d) + "\n\n⏳ Создаю...")
        try:
            client, warnings = await svc.create(
                d["name"], d["kind"], d.get("days"), d.get("quota") or None, d["xhttp"], d["vision"]
            )
        except XrayError as e:
            await safe_edit(cb, f"❌ Не удалось создать клиента:\n<code>{fmt.h(e)}</code>")
            await cb.answer()
            return
        text = _wizard_text(d) + "\n\n✅ Клиент создан."
        if warnings:
            text += "\n⚠️ Xray применит его после перезапуска: " + fmt.h("; ".join(warnings))
        await safe_edit(cb, text)
        await send_links(cb.message, svc, client.id)
        await show_card(cb.message, svc, client.id, edit=False)
    await cb.answer()


@router.callback_query(kb.NewCb.filter())
async def new_wizard_stale(cb: CallbackQuery) -> None:
    await cb.answer("Это старое меню создания, начни заново", show_alert=True)


# ---------------------------------------------------------------- список и карточка


async def _client_label(svc: ClientService, c) -> str:
    last = await svc.db.last_seen(c.id)
    dot = "🟢" if svc.is_online(c, last) else "⚪"
    tail = f" · 🎫 {fmt.left(c.expires_at)}" if c.is_guest else ""
    if c.is_system:
        tail = " · основной"
    return f"{dot} {c.name}{tail}"


async def render_list(svc: ClientService, page: int) -> tuple[str, object]:
    clients = await svc.db.list_clients()
    if not clients:
        return "Клиентов пока нет. Нажми «➕ Новый клиент».", None
    pages = max(1, math.ceil(len(clients) / kb.PAGE_SIZE))
    page = min(max(0, page), pages - 1)
    chunk = clients[page * kb.PAGE_SIZE : (page + 1) * kb.PAGE_SIZE]
    items = [(c.id, await _client_label(svc, c)) for c in chunk]
    online = sum(1 for c in clients if c.name in svc.online)
    text = f"👥 <b>Клиенты</b>: {len(clients)}, онлайн {online}"
    if pages > 1:
        text += f"\nСтраница {page + 1} из {pages}"
    return text, kb.client_list(items, page, pages)


@router.message(F.text == kb.BTN_LIST)
@router.message(Command("clients"))
async def list_clients(message: Message, state: FSMContext, svc: ClientService) -> None:
    await state.clear()
    text, markup = await render_list(svc, 0)
    await message.answer(text, reply_markup=markup)


@router.callback_query(kb.ClientCb.filter(F.action == "list"))
async def list_page(cb: CallbackQuery, callback_data: kb.ClientCb, svc: ClientService) -> None:
    text, markup = await render_list(svc, callback_data.arg)
    await safe_edit(cb, text, markup)
    await cb.answer()


async def card_text(svc: ClientService, client) -> str:
    st = await svc.status(client)
    head = f"👤 <b>{fmt.h(client.name)}</b>  "
    head += "🟢 онлайн" if st.online else f"⚪ офлайн, был {fmt.ago(st.last_seen)}"
    lines = [head]
    if client.is_guest:
        lines.append(f"Тип: 🎫 гостевой, осталось {fmt.left(client.expires_at)} (до {fmt.dt(client.expires_at)})")
    elif client.is_system:
        lines.append("Тип: ♾ постоянный · основной клиент из скрипта моста")
    else:
        lines.append("Тип: ♾ постоянный")
    conns = [n for n, on in (("XHTTP", client.use_xhttp), ("VISION", client.use_vision)) if on]
    lines.append(f"Подключения: {', '.join(conns)}")
    lines.append(f"Создан: {fmt.dt(client.created_at, with_year=True)}")

    lines.append("")
    if st.current_ips:
        lines.append("🌐 <b>Сейчас подключён с:</b>" if st.online else "🌐 <b>Последние адреса:</b>")
        for ip, geo in st.current_ips:
            lines.append(f"• <code>{ip}</code> - {fmt.h(geo.short())}")
    else:
        sessions = await svc.db.recent_sessions(client.id, limit=1)
        if sessions:
            s = sessions[0]
            geo = await svc.geo.lookup(s["ip"])
            lines.append(f"🌐 Последний IP: <code>{s['ip']}</code> - {fmt.h(geo.short())}")
        else:
            lines.append("🌐 Ещё не подключался")

    lines.append("")
    lines.append("📊 <b>Трафик</b>")
    lines.append(f"Сегодня: {fmt.size(st.today)}")
    if client.quota_bytes:
        lines.append(
            f"Период до {fmt.dt(st.period_end)}: {fmt.size(st.period_used)} из {fmt.size(client.quota_bytes)}"
        )
        lines.append(fmt.bar(st.period_used, client.quota_bytes))
    else:
        lines.append(f"Период до {fmt.dt(st.period_end)}: {fmt.size(st.period_used)} (без лимита)")
    lines.append(f"Всего: {fmt.size(st.total)}")
    return "\n".join(lines)


async def show_card(message: Message, svc: ClientService, client_id: int, edit: bool, cb: CallbackQuery | None = None):
    client = await svc.db.get_client(client_id)
    if not client:
        text, markup = "Клиент не найден (возможно, уже удалён).", None
    else:
        text, markup = await card_text(svc, client), kb.client_card(client.id, client.is_guest, client.is_system)
    if edit and cb:
        await safe_edit(cb, text, markup)
    else:
        await message.answer(text, reply_markup=markup, disable_web_page_preview=True)


@router.callback_query(kb.ClientCb.filter(F.action == "show"))
async def client_show(cb: CallbackQuery, callback_data: kb.ClientCb, svc: ClientService) -> None:
    await cb.answer()
    if cb.message.photo or cb.message.text is None:
        await show_card(cb.message, svc, callback_data.id, edit=False)
    else:
        await show_card(cb.message, svc, callback_data.id, edit=True, cb=cb)


def make_qr(data: str) -> bytes:
    qr = qrcode.QRCode(error_correction=qrcode.constants.ERROR_CORRECT_L, box_size=6, border=3)
    qr.add_data(data)
    qr.make(fit=True)
    buf = io.BytesIO()
    qr.make_image(fill_color="black", back_color="white").save(buf, format="PNG")
    return buf.getvalue()


async def send_links(message: Message, svc: ClientService, client_id: int) -> None:
    client = await svc.db.get_client(client_id)
    if not client:
        await message.answer("Клиент не найден.")
        return
    for title, link in svc.links.links_for(client):
        png = make_qr(link)
        await message.answer_photo(
            BufferedInputFile(png, filename=f"{client.name}-{title.split()[0].lower()}.png"),
            caption=f"📱 <b>{fmt.h(client.name)}</b> · {title}",
        )
        await message.answer(f"<code>{fmt.h(link)}</code>", disable_web_page_preview=True)


@router.callback_query(kb.ClientCb.filter(F.action == "qr"))
async def client_qr(cb: CallbackQuery, callback_data: kb.ClientCb, svc: ClientService) -> None:
    await cb.answer()
    await send_links(cb.message, svc, callback_data.id)
    await cb.message.answer("Вернуться к клиенту:", reply_markup=kb.back_to(callback_data.id))


@router.callback_query(kb.ClientCb.filter(F.action == "hist"))
async def client_history(cb: CallbackQuery, callback_data: kb.ClientCb, svc: ClientService) -> None:
    client = await svc.db.get_client(callback_data.id)
    if not client:
        await cb.answer("Клиент не найден", show_alert=True)
        return
    rows = await svc.db.recent_sessions(client.id, limit=15)
    lines = [f"🕓 <b>{fmt.h(client.name)}</b>: последние подключения"]
    if not rows:
        lines.append("Подключений ещё не было.")
    now = time.time()
    for r in rows:
        geo = await svc.geo.lookup(r["ip"])
        active = now - r["last_seen"] <= svc.s.online_window_sec
        end = "сейчас" if active else fmt.dt(r["last_seen"])
        span = fmt.duration(r["last_seen"] - r["first_seen"])
        lines.append(
            f"• {fmt.dt(r['first_seen'])} → {end} ({span})\n"
            f"   <code>{r['ip']}</code> - {fmt.h(geo.short())}"
        )
    await safe_edit(cb, "\n".join(lines), kb.back_to(client.id))
    await cb.answer()


@router.callback_query(kb.ClientCb.filter(F.action == "ext"))
async def client_extend_menu(cb: CallbackQuery, callback_data: kb.ClientCb, svc: ClientService) -> None:
    client = await svc.db.get_client(callback_data.id)
    if not client:
        await cb.answer("Клиент не найден", show_alert=True)
        return
    await safe_edit(
        cb,
        f"⏳ Продлить <b>{fmt.h(client.name)}</b>\nСейчас до {fmt.dt(client.expires_at)} (осталось {fmt.left(client.expires_at)})",
        kb.extend(client.id),
    )
    await cb.answer()


@router.callback_query(kb.ClientCb.filter(F.action == "extd"))
async def client_extend(cb: CallbackQuery, callback_data: kb.ClientCb, svc: ClientService) -> None:
    client = await svc.db.get_client(callback_data.id)
    if client:
        client = await svc.extend(client, callback_data.arg)
        await cb.answer(f"Продлено до {fmt.dt(client.expires_at)}")
    await show_card(cb.message, svc, callback_data.id, edit=True, cb=cb)


@router.callback_query(kb.ClientCb.filter(F.action == "quota"))
async def client_quota_menu(cb: CallbackQuery, callback_data: kb.ClientCb, svc: ClientService) -> None:
    client = await svc.db.get_client(callback_data.id)
    if not client:
        await cb.answer("Клиент не найден", show_alert=True)
        return
    await safe_edit(
        cb,
        f"📦 Лимит для <b>{fmt.h(client.name)}</b> на 30 дней\nСейчас: {fmt.size(client.quota_bytes) if client.quota_bytes else 'без лимита'}\n"
        "При превышении бот только предупреждает, доступ не отключается.",
        kb.quota(client.id),
    )
    await cb.answer()


@router.callback_query(kb.ClientCb.filter(F.action == "setq"))
async def client_set_quota(cb: CallbackQuery, callback_data: kb.ClientCb, svc: ClientService) -> None:
    client = await svc.db.get_client(callback_data.id)
    if client:
        await svc.set_quota(client, callback_data.arg or None)
        await cb.answer("Лимит обновлён")
    await show_card(cb.message, svc, callback_data.id, edit=True, cb=cb)


@router.callback_query(kb.ClientCb.filter(F.action == "del"))
async def client_delete_ask(cb: CallbackQuery, callback_data: kb.ClientCb, svc: ClientService) -> None:
    client = await svc.db.get_client(callback_data.id)
    if not client:
        await cb.answer("Клиент не найден", show_alert=True)
        return
    if client.is_system:
        await cb.answer(
            "main создан скриптом моста: его ссылки на странице подписки autoXRAY. Удалить его нельзя.",
            show_alert=True,
        )
        return
    await safe_edit(
        cb,
        f"🗑 Удалить <b>{fmt.h(client.name)}</b>?\nЕго ссылки сразу перестанут работать, история и статистика будут удалены.",
        kb.confirm_delete(client.id),
    )
    await cb.answer()


@router.callback_query(kb.ClientCb.filter(F.action == "delok"))
async def client_delete(cb: CallbackQuery, callback_data: kb.ClientCb, svc: ClientService) -> None:
    client = await svc.db.get_client(callback_data.id)
    if not client:
        await cb.answer("Уже удалён")
        return
    try:
        errors = await svc.delete(client)
    except ValueError as e:
        await cb.answer(str(e), show_alert=True)
        return
    except XrayError as e:
        await safe_edit(cb, f"❌ Ошибка при удалении:\n<code>{fmt.h(e)}</code>")
        await cb.answer()
        return
    text = f"🗑 Клиент <b>{fmt.h(client.name)}</b> удалён."
    if errors:
        text += "\n⚠️ Xray: " + fmt.h("; ".join(errors))
    await safe_edit(cb, text)
    await cb.answer()


# ---------------------------------------------------------------- трафик


@router.message(F.text == kb.BTN_TRAFFIC)
@router.message(Command("traffic"))
async def traffic(message: Message, state: FSMContext, svc: ClientService) -> None:
    await state.clear()
    clients = await svc.db.list_clients()
    if not clients:
        await message.answer("Клиентов пока нет.")
        return
    now = int(time.time())
    today = await svc.db.traffic_all_since(fmt.local_midnight(now))
    month = await svc.db.traffic_all_since(now - 30 * 86400)
    rows = []
    for c in clients:
        used = await svc.db.period_usage(c, now)
        tu, td = await svc.db.traffic_total(c.id)
        rows.append((used, c, tu + td))
    rows.sort(key=lambda r: r[0], reverse=True)

    lines = [
        "📊 <b>Трафик</b>",
        f"Сегодня всего: {fmt.size(sum(today.values()))}",
        f"За 30 дней всего: {fmt.size(sum(month.values()))}",
        f"За всё время всего: {fmt.size(sum(r[2] for r in rows))}",
        "",
    ]
    for used, c, total in rows:
        if c.quota_bytes:
            pct = used * 100 / c.quota_bytes
            mark = "🔴" if pct >= 100 else ("🟡" if pct >= 80 else "🟢")
            period = f"{fmt.size(used)} / {fmt.size(c.quota_bytes)} ({pct:.0f}%) {mark}"
        else:
            period = f"{fmt.size(used)} / ∞"
        lines.append(
            f"<b>{fmt.h(c.name)}</b>\n"
            f"   сегодня {fmt.size(today.get(c.id, 0))} · период {period}\n"
            f"   всего {fmt.size(total)}"
        )
    await message.answer("\n".join(lines))


# ---------------------------------------------------------------- диагностика


async def diag_text(svc: ClientService, socks_url: str | None) -> tuple[str, bool]:
    users = await svc.all_managed()
    checks = await run_diagnostics(
        svc.s, svc.xray.api, svc.xray, users, socks_url, stability=await svc.stability_check()
    )
    auto = await svc.autodiag_enabled()
    return format_report(checks, fmt.dt(int(time.time())), svc.s.autodiag_min if auto else None), auto


@router.message(F.text == kb.BTN_DIAG)
@router.message(Command("diag"))
async def diagnostics(message: Message, state: FSMContext, svc: ClientService, socks_url: str | None) -> None:
    await state.clear()
    msg = await message.answer("🩺 Проверяю сервер...")
    text, auto = await diag_text(svc, socks_url)
    await msg.edit_text(text, reply_markup=kb.diag_again(auto))


@router.callback_query(kb.SysCb.filter(F.action == "diag"))
async def diagnostics_again(cb: CallbackQuery, svc: ClientService, socks_url: str | None) -> None:
    await cb.answer("Проверяю...")
    text, auto = await diag_text(svc, socks_url)
    await safe_edit(cb, text, kb.diag_again(auto))


@router.callback_query(kb.SysCb.filter(F.action.in_({"autodiag_on", "autodiag_off"})))
async def autodiag_toggle(cb: CallbackQuery, callback_data: kb.SysCb, svc: ClientService) -> None:
    enable = callback_data.action == "autodiag_on"
    await svc.set_autodiag(enable)
    minutes = svc.s.autodiag_min
    # обновляем подпись внизу отчёта без повторного запуска проверок
    text = cb.message.html_text or ""
    for m in {minutes, 15}:
        text = text.replace(autodiag_footer(m), "")
    text = text.rstrip() + autodiag_footer(minutes if enable else None)
    await safe_edit(cb, text, kb.diag_again(enable))
    await cb.answer(
        f"Автодиагностика включена: проверка каждые {minutes} минут, сообщу о проблемах"
        if enable
        else "Автодиагностика отключена",
        show_alert=enable,
    )


# ---------------------------------------------------------------- проверка моста


async def bridge_text(svc: ClientService, socks_url: str | None) -> str:
    try:
        cfg = svc.xray.config.load()
    except Exception as e:  # noqa: BLE001
        return f"❌ Не удалось прочитать конфиг Xray: <code>{fmt.h(e)}</code>"
    reports = await check_bridge(cfg)
    if not reports:
        return "❌ В конфиге моста не найден зарубежный сервер (outbound proxy-*)."
    chain = await chain_check(socks_url)
    all_ok = all(r.ok for r in reports) and chain.status == "✅"
    lines = ["🌉 <b>Проверка моста</b>: " + ("всё работает" if all_ok else "есть проблемы"), f"<i>{fmt.dt(int(time.time()))}</i>"]
    for r in reports:
        t = r.target
        lines.append("")
        lines.append(f"{'✅' if r.ok else '❌'} <b>{fmt.h(t.sni)}</b>:{t.port} ({t.network}, {t.tag})")
        lines.extend(c.line() for c in r.checks)
    lines.append("")
    lines.append(chain.line())
    if not all_ok:
        lines.append("")
        lines.append(
            "<i>Если заглушка не открывается с RU сервера, проверь, что зарубежный сервер жив и что "
            "nginx на нём работает. Если заглушка открывается, а цепочка нет, смотри Xray на обоих серверах.</i>"
        )
    return "\n".join(lines)


@router.message(F.text == kb.BTN_BRIDGE)
@router.message(Command("bridge"))
async def bridge_check(message: Message, state: FSMContext, svc: ClientService, socks_url: str | None) -> None:
    await state.clear()
    msg = await message.answer("🌉 Проверяю мост...")
    await msg.edit_text(await bridge_text(svc, socks_url), reply_markup=kb.bridge_again(), disable_web_page_preview=True)


@router.callback_query(kb.SysCb.filter(F.action == "bridge"))
async def bridge_check_again(cb: CallbackQuery, svc: ClientService, socks_url: str | None) -> None:
    await cb.answer("Проверяю...")
    await safe_edit(cb, await bridge_text(svc, socks_url), kb.bridge_again())


@router.callback_query(kb.SysCb.filter(F.action == "sync"))
async def sync_config(cb: CallbackQuery, svc: ClientService) -> None:
    await cb.answer("Восстанавливаю...")
    try:
        await svc.import_base_client()
        await svc.xray.write_users(await svc.all_managed(), patch_api=True)
        ok, info = await svc.xray.restart_xray()
    except XrayError as e:
        ok, info = False, str(e)
    svc.links.reset()
    text = "✅ Конфиг восстановлен, Xray перезапущен." if ok else f"❌ Не получилось: <code>{fmt.h(info)}</code>"
    await safe_edit(cb, text)
