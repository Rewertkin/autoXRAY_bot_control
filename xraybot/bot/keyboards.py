"""Клавиатуры и callback-данные."""

from __future__ import annotations

from aiogram.filters.callback_data import CallbackData
from aiogram.types import InlineKeyboardMarkup, KeyboardButton, ReplyKeyboardMarkup
from aiogram.utils.keyboard import InlineKeyboardBuilder

BTN_NEW = "➕ Новый клиент"
BTN_LIST = "👥 Клиенты"
BTN_TRAFFIC = "📊 Трафик"
BTN_DIAG = "🩺 Диагностика"
BTN_BRIDGE = "🌉 Проверить мост"

GUEST_DAYS = [(1, "1 день"), (7, "7 дней"), (30, "30 дней"), (28, "4 недели")]
QUOTAS_GB = [10, 50, 100, 200, 500]
PAGE_SIZE = 10


class NewCb(CallbackData, prefix="n"):
    step: str
    value: str = ""


class ClientCb(CallbackData, prefix="c"):
    action: str
    id: int = 0
    arg: int = 0


class SysCb(CallbackData, prefix="s"):
    action: str


def main_menu() -> ReplyKeyboardMarkup:
    return ReplyKeyboardMarkup(
        keyboard=[
            [KeyboardButton(text=BTN_NEW), KeyboardButton(text=BTN_LIST)],
            [KeyboardButton(text=BTN_TRAFFIC), KeyboardButton(text=BTN_DIAG)],
            [KeyboardButton(text=BTN_BRIDGE)],
        ],
        resize_keyboard=True,
        is_persistent=True,
    )


def new_kind() -> InlineKeyboardMarkup:
    kb = InlineKeyboardBuilder()
    kb.button(text="♾ Постоянный", callback_data=NewCb(step="kind", value="permanent"))
    kb.button(text="🎫 Гостевой", callback_data=NewCb(step="kind", value="guest"))
    kb.button(text="✖️ Отмена", callback_data=NewCb(step="cancel"))
    kb.adjust(2, 1)
    return kb.as_markup()


def new_days() -> InlineKeyboardMarkup:
    kb = InlineKeyboardBuilder()
    for days, label in GUEST_DAYS:
        kb.button(text=label, callback_data=NewCb(step="days", value=str(days)))
    kb.button(text="✖️ Отмена", callback_data=NewCb(step="cancel"))
    kb.adjust(2, 2, 1)
    return kb.as_markup()


def new_conn(xhttp: bool, vision: bool) -> InlineKeyboardMarkup:
    kb = InlineKeyboardBuilder()
    kb.button(text=f"{'✅' if xhttp else '◻️'} XHTTP TLS stream-up", callback_data=NewCb(step="conn", value="xhttp"))
    kb.button(text=f"{'✅' if vision else '◻️'} RAW VISION", callback_data=NewCb(step="conn", value="vision"))
    kb.button(text="Далее ➡️", callback_data=NewCb(step="conn", value="next"))
    kb.button(text="✖️ Отмена", callback_data=NewCb(step="cancel"))
    kb.adjust(1, 1, 2)
    return kb.as_markup()


def quota_buttons(make_cb) -> InlineKeyboardMarkup:
    kb = InlineKeyboardBuilder()
    for gb in QUOTAS_GB:
        kb.button(text=f"{gb} ГБ", callback_data=make_cb(gb))
    kb.button(text="Без лимита", callback_data=make_cb(0))
    kb.adjust(3, 3)
    return kb


def new_quota() -> InlineKeyboardMarkup:
    kb = quota_buttons(lambda gb: NewCb(step="quota", value=str(gb)))
    kb.button(text="✖️ Отмена", callback_data=NewCb(step="cancel"))
    kb.adjust(3, 3, 1)
    return kb.as_markup()


def new_confirm() -> InlineKeyboardMarkup:
    kb = InlineKeyboardBuilder()
    kb.button(text="✅ Создать", callback_data=NewCb(step="ok"))
    kb.button(text="✖️ Отмена", callback_data=NewCb(step="cancel"))
    return kb.as_markup()


def client_list(items: list[tuple[int, str]], page: int, pages: int) -> InlineKeyboardMarkup:
    kb = InlineKeyboardBuilder()
    for cid, label in items:
        kb.button(text=label, callback_data=ClientCb(action="show", id=cid))
    kb.adjust(1)
    nav = []
    if page > 0:
        nav.append(("⬅️", ClientCb(action="list", arg=page - 1)))
    if page < pages - 1:
        nav.append(("➡️", ClientCb(action="list", arg=page + 1)))
    if nav:
        builder = InlineKeyboardBuilder()
        for text, cb in nav:
            builder.button(text=text, callback_data=cb)
        kb.attach(builder)
    return kb.as_markup()


def client_card(cid: int, is_guest: bool, is_system: bool = False) -> InlineKeyboardMarkup:
    kb = InlineKeyboardBuilder()
    rows = [
        [("📱 QR и ссылки", "qr"), ("🕓 История", "hist")],
        ([("⏳ Продлить", "ext")] if is_guest else []) + [("📦 Лимит", "quota")],
        [("🔄 Обновить", "show")] + ([] if is_system else [("🗑 Удалить", "del")]),
    ]
    for row in rows:
        for text, action in row:
            kb.button(text=text, callback_data=ClientCb(action=action, id=cid))
    kb.button(text="⬅️ К списку", callback_data=ClientCb(action="list"))
    kb.adjust(*[len(r) for r in rows], 1)
    return kb.as_markup()


def back_to(cid: int) -> InlineKeyboardMarkup:
    kb = InlineKeyboardBuilder()
    kb.button(text="⬅️ Назад", callback_data=ClientCb(action="show", id=cid))
    return kb.as_markup()


def extend(cid: int) -> InlineKeyboardMarkup:
    kb = InlineKeyboardBuilder()
    for days, label in GUEST_DAYS:
        kb.button(text=f"+{label}", callback_data=ClientCb(action="extd", id=cid, arg=days))
    kb.button(text="⬅️ Назад", callback_data=ClientCb(action="show", id=cid))
    kb.adjust(2, 2, 1)
    return kb.as_markup()


def quota(cid: int) -> InlineKeyboardMarkup:
    kb = quota_buttons(lambda gb: ClientCb(action="setq", id=cid, arg=gb))
    kb.button(text="⬅️ Назад", callback_data=ClientCb(action="show", id=cid))
    kb.adjust(3, 3, 1)
    return kb.as_markup()


def confirm_delete(cid: int) -> InlineKeyboardMarkup:
    kb = InlineKeyboardBuilder()
    kb.button(text="🗑 Да, удалить", callback_data=ClientCb(action="delok", id=cid))
    kb.button(text="⬅️ Нет", callback_data=ClientCb(action="show", id=cid))
    return kb.as_markup()


def diag_again(autodiag: bool) -> InlineKeyboardMarkup:
    kb = InlineKeyboardBuilder()
    kb.button(text="🔄 Повторить", callback_data=SysCb(action="diag"))
    if autodiag:
        kb.button(text="🔕 Отключить автодиагностику", callback_data=SysCb(action="autodiag_off"))
    else:
        kb.button(text="🔔 Включить автодиагностику", callback_data=SysCb(action="autodiag_on"))
    kb.adjust(1)
    return kb.as_markup()


def sync_config() -> InlineKeyboardMarkup:
    kb = InlineKeyboardBuilder()
    kb.button(text="🛠 Восстановить конфиг", callback_data=SysCb(action="sync"))
    return kb.as_markup()


def bridge_again() -> InlineKeyboardMarkup:
    kb = InlineKeyboardBuilder()
    kb.button(text="🔄 Повторить", callback_data=SysCb(action="bridge"))
    return kb.as_markup()
