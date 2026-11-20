import json
import time

import pytest

from conftest import read_cfg
from xraybot import fmt
from xraybot.__main__ import build, resolve_tg_proxy
from xraybot.bot import keyboards as kb
from xraybot.db import PERIOD_SEC
from xraybot.jobs import Jobs
from xraybot.links import build_link, discover_templates, fallback_templates, make_template, new_uuid, route_uuid
from xraybot.xray import XrayConfigFile, parse_online_all, parse_online_single, parse_traffic

pytestmark = pytest.mark.asyncio

XHTTP_LINK = (
    "vless://aaaaaaaa-bbbb-0001-cccc-dddddddddddd@link.example.com:443?security=tls&alpn=h2&type=xhttp"
    "&mode=stream-up&path=%2Fabc123&extra=%7B%22noGRPCHeader%22%3Atrue%7D&sni=link.example.com&fp=chrome"
    "#RU%3EEU_xhttp_x"
)
VISION_LINK = (
    "vless://aaaaaaaa-bbbb-0001-cccc-dddddddddddd@link.example.com:443?security=tls&type=tcp&headerType="
    "&path=&host=&flow=xtls-rprx-vision&sni=link.example.com&fp=chrome&spx=%2F#RU%3EEU_raw_x"
)
DIRECT_LINK = "vless://eeee@nl.example.org:443?security=tls&type=xhttp#Direct"


# ---------------------------------------------------------------- ссылки и UUID


async def test_uuid_route_group():
    u = new_uuid()
    parts = u.split("-")
    assert [len(p) for p in parts] == [8, 4, 4, 4, 12]
    assert parts[2] == "0000"
    assert route_uuid(u, 1).split("-")[2] == "0001"
    assert route_uuid(u, 1).replace("-0001-", "-0000-") == u


async def test_discover_templates_from_html(tmp_path):
    page = f"<a href='{XHTTP_LINK.replace('&', '&amp;')}'>x</a> {VISION_LINK.replace('&', '&amp;')} {DIRECT_LINK}"
    (tmp_path / "sub.html").write_text(page)
    t = discover_templates(tmp_path, "link.example.com")
    assert set(t) == {"xhttp", "vision"}
    assert t["xhttp"].startswith("vless://{uuid}@link.example.com:443?") and t["xhttp"].endswith("#{remark}")
    assert "&amp;" not in t["xhttp"]
    link = build_link(t["vision"], "12345678-1234-0000-1234-123456789012", 1, "мама VISION")
    assert link.startswith("vless://12345678-1234-0001-1234-123456789012@link.example.com:443?")
    assert "flow=xtls-rprx-vision" in link
    assert link.endswith("#%D0%BC%D0%B0%D0%BC%D0%B0%20VISION")


async def test_fallback_templates_use_config_path():
    from conftest import BRIDGE_CONFIG

    t = fallback_templates(BRIDGE_CONFIG, "link.example.com", "RUbrEUxhttpTLS")
    assert "path=%2Fabc123" in t["xhttp"] and "type=xhttp" in t["xhttp"]
    assert "flow=xtls-rprx-vision" in t["vision"]


async def test_make_template_rejects_garbage():
    with pytest.raises(ValueError):
        make_template("https://example.com")


# ---------------------------------------------------------------- конфиг Xray


async def test_patch_config_idempotent_and_keeps_base_client():
    from conftest import BRIDGE_CONFIG
    from xraybot.xray import ManagedUser

    cfg = json.loads(json.dumps(BRIDGE_CONFIG))
    assert XrayConfigFile.ensure_api_and_stats(cfg, "127.0.0.1:10085") is True
    assert XrayConfigFile.ensure_api_and_stats(cfg, "127.0.0.1:10085") is False
    assert XrayConfigFile.api_enabled(cfg)
    assert cfg["policy"]["levels"]["0"]["statsUserOnline"] is True

    users = [
        ManagedUser("mama", "aaaaaaaa-0000-0000-0000-000000000001", True, True),
        ManagedUser("guest", "aaaaaaaa-0000-0000-0000-000000000002", True, False),
    ]
    XrayConfigFile.apply_users(cfg, users, "RUbrEUxhttpTLS", "RUbrEUtlsVISION")
    vision = XrayConfigFile.find_inbound(cfg, "RUbrEUtlsVISION")["settings"]["clients"]
    xhttp = XrayConfigFile.find_inbound(cfg, "RUbrEUxhttpTLS")["settings"]["clients"]
    assert vision[0] == BRIDGE_CONFIG["inbounds"][0]["settings"]["clients"][0]  # общий UUID не тронут
    assert [c.get("email") for c in vision[1:]] == ["mama"]
    assert vision[1]["flow"] == "xtls-rprx-vision"
    assert [c.get("email") for c in xhttp[1:]] == ["mama", "guest"]
    assert "flow" not in xhttp[1]

    XrayConfigFile.apply_users(cfg, users[:1], "RUbrEUxhttpTLS", "RUbrEUtlsVISION")
    assert XrayConfigFile.managed_emails(cfg, "RUbrEUxhttpTLS") == {"mama"}


async def test_socks_url_is_quoted(env):
    settings, _ = env
    db, config, svc = build(settings)
    assert config.socks_proxy_url("RUsocks5") == "socks5://u1:p%40ss%2Fw@127.0.0.1:10443"
    assert resolve_tg_proxy(settings, config).startswith("socks5://u1:")


# ---------------------------------------------------------------- парсинг ответов API


async def test_parse_traffic():
    data = {
        "stat": [
            {"name": "user>>>mama>>>traffic>>>uplink", "value": 100},
            {"name": "user>>>mama>>>traffic>>>downlink", "value": 900},
            {"name": "user>>>papa>>>traffic>>>downlink"},  # value опущен, когда 0
            {"name": "inbound>>>api>>>traffic>>>uplink", "value": 5},
        ]
    }
    assert parse_traffic(data) == {"mama": (100, 900), "papa": (0, 0)}


async def test_parse_online():
    data = {"users": [{"email": "mama", "ips": [{"ip": "1.2.3.4", "lastSeen": "1700000000"}]}, {"email": "x"}]}
    assert parse_online_all(data) == {"mama": {"1.2.3.4": 1700000000}}
    assert parse_online_single({"name": "user>>>mama>>>online", "ips": {"5.6.7.8": 1700000000123}}) == {
        "5.6.7.8": 1700000000
    }


# ---------------------------------------------------------------- сценарий целиком на фейковом xray


async def test_create_traffic_online_expire(env):
    settings, fake_dir = env
    db, config, svc = build(settings)
    await db.open()
    try:
        # первичный патч, как делает install.sh
        assert await svc.xray.write_users([], patch_api=True) is True
        assert XrayConfigFile.api_enabled(read_cfg(settings))

        client, warnings = await svc.create("mama", "permanent", None, 100, True, True)
        assert warnings == []
        guest, _ = await svc.create("guest-1", "guest", 1, 10, True, False)
        cfg = read_cfg(settings)
        assert XrayConfigFile.managed_emails(cfg, "RUbrEUxhttpTLS") == {"mama", "guest-1"}
        assert XrayConfigFile.managed_emails(cfg, "RUbrEUtlsVISION") == {"mama"}
        assert svc.xray.config_in_sync(await svc.all_managed()) == (True, "ok")
        calls = (fake_dir / "calls.log").read_text()
        assert calls.count("api adu") == 3  # mama x2 + guest x1

        # ссылки: шаблоны из конфига, UUID с маршрутом 0001
        links = svc.links.links_for(client)
        assert [t for t, _ in links] == ["XHTTP TLS", "RAW VISION"]
        assert route_uuid(client.uuid, 1) in links[0][1]

        notes = []

        async def notify(text, markup=None):
            notes.append(text)

        jobs = Jobs(settings, svc, notify, kb.sync_config)

        # трафик: 85 ГБ у mama -> предупреждение 80%, повторно не шлётся
        (fake_dir / "stats.json").write_text(
            json.dumps({"stat": [{"name": "user>>>mama>>>traffic>>>downlink", "value": 85 * 1024**3}]})
        )
        await jobs.poll_traffic()
        assert await db.period_usage(client) == 85 * 1024**3
        assert len(notes) == 1 and "80%" in notes[0]
        (fake_dir / "stats.json").write_text(
            json.dumps({"stat": [{"name": "user>>>mama>>>traffic>>>uplink", "value": 1024**2}]})
        )
        await jobs.poll_traffic()
        assert len(notes) == 1

        # онлайн и история
        now = int(time.time())
        (fake_dir / "online.json").write_text(
            json.dumps({"users": [{"email": "mama", "ips": [{"ip": "10.0.0.5", "lastSeen": now}]}]})
        )
        await jobs.poll_online()
        st = await svc.status(client)
        assert st.online and st.current_ips[0][0] == "10.0.0.5"
        assert st.current_ips[0][1].isp == "локальная сеть"
        assert len(await db.recent_sessions(client.id)) == 1

        # гость истекает -> удаляется из базы и конфига
        await db.set_expires(guest.id, now - 1)
        await jobs.expire_guests()
        assert await db.get_client(guest.id) is None
        assert XrayConfigFile.managed_emails(read_cfg(settings), "RUbrEUxhttpTLS") == {"mama"}
        assert any("истёк" in n for n in notes)
        assert "api rmu --server=127.0.0.1:10085 -tag=RUbrEUxhttpTLS guest-1" in (fake_dir / "calls.log").read_text()

        # скрипт моста перезаписал конфиг -> бот замечает расхождение один раз
        from conftest import BRIDGE_CONFIG

        settings.xray_config.write_text(json.dumps(BRIDGE_CONFIG))
        await jobs.check_config()
        await jobs.check_config()
        assert sum("не совпадает" in n for n in notes) == 1
    finally:
        await svc.geo.close()
        await db.close()


async def test_period_math(env):
    settings, _ = env
    db, _, _ = build(settings)
    await db.open()
    try:
        created = int(time.time()) - PERIOD_SEC - 3600
        c = await db.add_client("old", new_uuid(), "permanent", None, 1024, True, False, created_at=created)
        assert c.period_index() == 1
        start, end = c.period_bounds()
        assert start == created + PERIOD_SEC and end - start == PERIOD_SEC
        await db.add_traffic({c.id: (10, 20)}, created + 100)  # прошлый период
        await db.add_traffic({c.id: (1, 2)}, int(time.time()))
        assert await db.period_usage(c) == 3
        assert await db.traffic_total(c.id) == (11, 22)
    finally:
        await db.close()


async def test_sessions_gap(env):
    settings, _ = env
    db, _, _ = build(settings)
    await db.open()
    try:
        c = await db.add_client("s", new_uuid(), "permanent", None, None, True, False)
        t = 1_700_000_000
        await db.touch_session(c.id, "1.1.1.1", t)
        await db.touch_session(c.id, "1.1.1.1", t + 60)
        await db.touch_session(c.id, "1.1.1.1", t + 60 + 10 * 60)  # пауза 10 минут -> новая сессия
        rows = await db.recent_sessions(c.id)
        assert len(rows) == 2 and rows[1]["last_seen"] == t + 60
    finally:
        await db.close()


# ---------------------------------------------------------------- UI


async def test_callback_data_fits_telegram_limit():
    for cb in (
        kb.ClientCb(action="delok", id=10**9, arg=500),
        kb.NewCb(step="quota", value="500"),
        kb.SysCb(action="sync"),
    ):
        assert len(cb.pack().encode()) <= 64


async def test_keyboards_build():
    kb.main_menu()
    kb.new_kind()
    kb.new_days()
    kb.new_conn(True, False)
    kb.new_quota()
    kb.client_card(1, True)
    kb.client_card(1, False)
    kb.client_list([(1, "a")], 0, 3)
    kb.extend(1)
    kb.quota(1)


async def test_format_helpers():
    assert fmt.size(1536) == "2 КБ"
    assert fmt.size(5 * 1024**3) == "5.00 ГБ"
    assert fmt.size(None) == "∞"
    assert fmt.bar(50, 100).endswith("50%")
    assert fmt.left(int(time.time()) - 5) == "истёк"


async def test_render_texts(env, monkeypatch):
    """Карточка, список, история и диагностика собираются без ошибок (сеть замокана)."""
    from xraybot import diag
    from xraybot.bot import handlers

    settings, fake_dir = env
    db, config, svc = build(settings)
    await db.open()
    try:
        await svc.xray.write_users([], patch_api=True)
        c, _ = await svc.create("papa", "guest", 7, 50, True, False)
        await db.touch_session(c.id, "192.168.1.10", int(time.time()) - 600)
        await db.add_traffic({c.id: (1024**3, 2 * 1024**3)}, int(time.time()))

        text = await handlers.card_text(svc, c)
        assert "papa" in text and "гостевой" in text and "192.168.1.10" in text and "3.00 ГБ" in text
        text, markup = await handlers.render_list(svc, 0)
        assert "Клиенты" in text and markup is not None

        async def fake_trace(proxy):
            return ({"ip": "178.0.0.1", "loc": "NL"} if proxy else {"ip": "95.0.0.1", "loc": "RU"}), 42.0

        monkeypatch.setattr(diag, "_trace", fake_trace)
        out, auto = await handlers.diag_text(svc, "socks5://x:y@127.0.0.1:1")
        assert auto is False and "Автодиагностика" not in out
        assert "Цепочка RU → NL" in out and "178.0.0.1 (NL)" in out
        assert "Клиенты в конфиге" in out
    finally:
        await svc.geo.close()
        await db.close()


async def test_bridge_targets_and_title():
    from conftest import BRIDGE_CONFIG
    from xraybot.bridge import bridge_targets, page_title

    [t] = bridge_targets(BRIDGE_CONFIG)
    assert (t.tag, t.address, t.port, t.sni, t.network, t.path) == (
        "proxy-0", "nl.example.org", 443, "nl.example.org", "xhttp", "/nlpath"
    )
    assert page_title("<html><head><TITLE>\n Мой &amp; блог </TITLE></head>") == "Мой & блог"
    assert page_title("no title") == ""


async def test_bridge_check_texts(env, monkeypatch):
    from datetime import datetime, timedelta, timezone

    import httpx
    from xraybot import bridge, diag
    from xraybot.bot import handlers

    settings, _ = env
    db, config, svc = build(settings)

    async def resolve(host):
        return ["178.0.0.1"]

    async def tls_ok(target):
        return 35.0, datetime.now(timezone.utc) + timedelta(days=60), "TLSv1.3"

    async def stub_ok(target):
        return 200, "Cooking Blog", 4096, 80.0

    async def trace(proxy):
        return {"ip": "178.0.0.1", "loc": "NL"}, 50.0

    monkeypatch.setattr(bridge, "_resolve", resolve)
    monkeypatch.setattr(bridge, "_tls", tls_ok)
    monkeypatch.setattr(bridge, "_stub_page", stub_ok)
    monkeypatch.setattr(diag, "_trace", trace)
    text = await handlers.bridge_text(svc, "socks5://x:y@127.0.0.1:1")
    assert "всё работает" in text and "«Cooking Blog»" in text and "nl.example.org" in text

    async def stub_fail(target):
        raise httpx.ConnectTimeout("timeout")

    async def tls_fail(target):
        raise OSError("refused")

    monkeypatch.setattr(bridge, "_stub_page", stub_fail)
    text = await handlers.bridge_text(svc, "socks5://x:y@127.0.0.1:1")
    assert "есть проблемы" in text and "страница не открылась" in text

    monkeypatch.setattr(bridge, "_tls", tls_fail)
    text = await handlers.bridge_text(svc, None)
    assert "нет соединения с nl.example.org:443" in text
    await svc.geo.close()


async def test_startup_survives_network_errors():
    from xraybot.__main__ import startup

    sent = []

    class FailingBot:
        async def set_my_commands(self, commands):
            raise OSError("connection reset")

    async def notify(text, markup=None):
        sent.append(text)

    await startup(FailingBot(), notify, kb)
    assert sent == ["🤖 Бот запущен."]


async def test_bot_socks_bypasses_balancer(env):
    """Бот ходит в Telegram через свой SOCKS-вход, направленный прямо в proxy-0, а не в балансировщик."""
    from xraybot.xray import BOT_SOCKS_TAG

    settings, _ = env
    db, config, svc = build(settings)
    await db.open()
    try:
        assert "10443" in resolve_tg_proxy(settings, config)  # до patch: общий SOCKS моста
        await svc.xray.write_users([], patch_api=True)
        cfg = read_cfg(settings)
        rule = cfg["routing"]["rules"][0]
        assert rule == {"inboundTag": [BOT_SOCKS_TAG], "outboundTag": "proxy-0"}
        inbound = XrayConfigFile.find_inbound(cfg, BOT_SOCKS_TAG)
        assert inbound["listen"] == "127.0.0.1" and inbound["port"] == 10444
        assert resolve_tg_proxy(settings, config) == "socks5://127.0.0.1:10444"

        # повторный patch ничего не дублирует
        assert XrayConfigFile.ensure_bot_socks(cfg, 10444) is False
        assert sum(1 for i in cfg["inbounds"] if i["tag"] == BOT_SOCKS_TAG) == 1

        # после перезаписи конфига скриптом моста бот это замечает
        from conftest import BRIDGE_CONFIG

        cfg2 = json.loads(json.dumps(BRIDGE_CONFIG))
        XrayConfigFile.ensure_api_and_stats(cfg2, settings.api_addr)
        settings.xray_config.write_text(json.dumps(cfg2))
        ok, why = svc.xray.config_in_sync([])
        assert not ok and "SOCKS-входа бота" in why
    finally:
        await svc.geo.close()
        await db.close()


async def test_autodiag_job(env, monkeypatch):
    from xraybot import jobs as jobs_mod
    from xraybot.diag import FAIL, OK, Check

    settings, _ = env
    db, config, svc = build(settings)
    await db.open()
    try:
        notes = []

        async def notify(text, markup=None):
            notes.append((text, markup))

        state = {"checks": [Check(OK, "xray", "работает")]}

        async def fake_diag(*args, **kwargs):
            return state["checks"]

        monkeypatch.setattr(jobs_mod, "run_diagnostics", fake_diag)
        j = Jobs(settings, svc, notify, kb.sync_config, kb.diag_again)

        state["checks"] = [Check(FAIL, "DNS", "не резолвится")]
        await j.autodiag()
        assert notes == []  # выключена по умолчанию

        await svc.set_autodiag(True)
        assert await svc.autodiag_enabled()
        await j.autodiag()
        assert len(notes) == 1 and "нашла проблемы" in notes[0][0] and "каждые 15 минут" in notes[0][0]
        await j.autodiag()
        assert len(notes) == 1  # та же проблема, не спамим

        state["checks"] = [Check(FAIL, "DNS", "x"), Check(FAIL, "nginx", "failed")]
        await j.autodiag()
        assert len(notes) == 2  # проблем стало больше

        state["checks"] = [Check(OK, "DNS", "ok")]
        await j.autodiag()
        assert "устранены" in notes[-1][0]
        await j.autodiag()
        assert len(notes) == 3

        await svc.set_autodiag(False)
        assert not await svc.autodiag_enabled()
    finally:
        await svc.geo.close()
        await db.close()


async def test_diag_report_footer():
    from xraybot.diag import OK, Check, autodiag_footer, format_report

    checks = [Check(OK, "xray", "работает")]
    on = format_report(checks, "20.11 17:30", 15)
    off = format_report(checks, "20.11 17:30", None)
    assert on.endswith("🔔 Автодиагностика включена (каждые 15 минут)")
    assert "Автодиагностика" not in off
    assert on.replace(autodiag_footer(15), "") == off
    texts = [b.text for row in kb.diag_again(True).inline_keyboard for b in row]
    assert "🔕 Отключить автодиагностику" in texts
    texts = [b.text for row in kb.diag_again(False).inline_keyboard for b in row]
    assert "🔔 Включить автодиагностику" in texts


async def test_chain_stability(env, monkeypatch):
    from xraybot import jobs as jobs_mod
    from xraybot.diag import FAIL, OK, Check

    settings, _ = env
    db, config, svc = build(settings)
    await db.open()
    try:
        t0 = 1_700_000_000
        assert (await svc.stability_check(t0)).detail == "наблюдение только началось"

        await svc.record_chain(True, t0)
        c = await svc.stability_check(t0 + 2 * 3600 + 5 * 60)
        assert c.status == OK and c.detail == "2 ч 5 мин (разрывов не было с начала наблюдения)"

        await svc.record_chain(False, t0 + 3 * 3600)
        await svc.record_chain(False, t0 + 3 * 3600 + 60)  # разрыв продолжается, начало не сдвигается
        c = await svc.stability_check(t0 + 3 * 3600 + 10 * 60)
        assert c.status == FAIL and "уже 10 мин" in c.detail

        await svc.record_chain(True, t0 + 3 * 3600 + 15 * 60)
        c = await svc.stability_check(t0 + 3 * 3600 + 15 * 60 + 86400 + 7200)
        assert c.status == OK and c.detail.startswith("1 д 2 ч (последний разрыв") and "длился 15 мин" in c.detail

        # фоновая проверка: одиночный сбой с успешным повтором разрывом не считается
        results = iter([Check(FAIL, "c", "x"), Check(OK, "c", "ok")])

        async def flaky(proxy):
            return next(results)

        async def no_sleep(_):
            return None

        monkeypatch.setattr(jobs_mod, "chain_check", flaky)
        monkeypatch.setattr(jobs_mod.asyncio, "sleep", no_sleep)
        j = Jobs(settings, svc, lambda *a: None, kb.sync_config, socks_url="socks5://127.0.0.1:1")
        await j.probe_chain()
        assert (await svc.stability_check()).status == OK
    finally:
        await svc.geo.close()
        await db.close()


async def test_diag_has_no_cpu_ram_disk(env, monkeypatch):
    from xraybot import diag
    from xraybot.bot import handlers

    settings, _ = env
    db, config, svc = build(settings)
    await db.open()
    try:
        async def trace(proxy):
            return {"ip": "1.2.3.4", "loc": "NL" if proxy else "RU"}, 10.0

        monkeypatch.setattr(diag, "_trace", trace)
        text, _ = await handlers.diag_text(svc, "socks5://127.0.0.1:1")
        for gone in ("CPU", "RAM", "Диск"):
            assert f"<b>{gone}</b>" not in text
        assert text.index("<b>Без разрывов</b>") < text.index("<b>Аптайм</b>")
    finally:
        await svc.geo.close()
        await db.close()


async def test_import_base_client_main(env):
    """Общий клиент из скрипта моста становится клиентом main: тот же UUID, email для статистики, нельзя удалить."""
    from conftest import BRIDGE_CONFIG

    settings, fake_dir = env
    db, config, svc = build(settings)
    await db.open()
    try:
        base_uuid = BRIDGE_CONFIG["inbounds"][1]["settings"]["clients"][0]["id"]
        assert await svc.import_base_client() == "main"
        await svc.xray.write_users(await svc.all_managed(), patch_api=True)
        main = await db.get_client_by_name("main")
        assert main.is_system and main.uuid == base_uuid and main.use_xhttp and main.use_vision

        cfg = read_cfg(settings)
        vis = XrayConfigFile.find_inbound(cfg, "RUbrEUtlsVISION")["settings"]["clients"]
        xh = XrayConfigFile.find_inbound(cfg, "RUbrEUxhttpTLS")["settings"]["clients"]
        assert vis == [{"id": base_uuid, "email": "main", "level": 0, "flow": "xtls-rprx-vision"}]
        assert xh == [{"id": base_uuid, "email": "main", "level": 0}]
        assert svc.xray.config_in_sync(await svc.all_managed()) == (True, "ok")

        # ссылки main совпадают по UUID со ссылками страницы подписки (маршрут 0001)
        links = svc.links.links_for(main)
        assert len(links) == 2 and all(route_uuid(base_uuid, 1) in link for _, link in links)

        # трафик main учитывается
        (fake_dir / "stats.json").write_text(
            json.dumps({"stat": [{"name": "user>>>main>>>traffic>>>downlink", "value": 5000}]})
        )
        await Jobs(settings, svc, lambda *a: None, kb.sync_config).poll_traffic()
        assert await db.traffic_total(main.id) == (0, 5000)

        # повторный импорт ничего не дублирует, удалить нельзя
        assert await svc.import_base_client() is None  # клиентов без email больше нет
        assert len(await db.list_clients()) == 1
        with pytest.raises(ValueError):
            await svc.delete(main)
        texts = [b.text for row in kb.client_card(main.id, False, True).inline_keyboard for b in row]
        assert "🗑 Удалить" not in texts

        # скрипт моста перезапустили: новый UUID без email -> main получает новый UUID
        new_cfg = json.loads(json.dumps(BRIDGE_CONFIG))
        for inb in new_cfg["inbounds"][:2]:
            inb["settings"]["clients"][0]["id"] = "99999999-8888-0000-7777-666666666666"
        settings.xray_config.write_text(json.dumps(new_cfg))
        assert await svc.import_base_client() == "main"
        assert (await db.get_client(main.id)).uuid == "99999999-8888-0000-7777-666666666666"
        assert len(await db.list_clients()) == 1
    finally:
        await svc.geo.close()
        await db.close()


async def test_db_migration_adds_is_system(tmp_path):
    import aiosqlite

    from xraybot.db import Database

    path = tmp_path / "old.db"
    async with aiosqlite.connect(path) as conn:
        await conn.execute(
            "CREATE TABLE clients (id INTEGER PRIMARY KEY, name TEXT NOT NULL UNIQUE, uuid TEXT NOT NULL UNIQUE,"
            " kind TEXT NOT NULL, created_at INTEGER NOT NULL, expires_at INTEGER, quota_bytes INTEGER,"
            " use_xhttp INTEGER NOT NULL DEFAULT 1, use_vision INTEGER NOT NULL DEFAULT 0)"
        )
        await conn.execute("INSERT INTO clients (name, uuid, kind, created_at) VALUES ('old', 'u', 'permanent', 1)")
        await conn.commit()
    db = Database(path)
    await db.open()
    try:
        [c] = await db.list_clients()
        assert c.name == "old" and c.is_system is False
    finally:
        await db.close()


async def test_traffic_summary_all_time(env):
    from xraybot.bot import handlers

    settings, _ = env
    db, config, svc = build(settings)
    await db.open()
    try:
        await svc.xray.write_users([], patch_api=True)
        a, _ = await svc.create("a", "permanent", None, None, True, False)
        b, _ = await svc.create("b", "permanent", None, None, True, False)
        old = int(time.time()) - 60 * 86400
        await db.add_traffic({a.id: (1024**3, 0)}, old)  # 2 месяца назад
        await db.add_traffic({b.id: (0, 1024**3)}, int(time.time()))

        sent = []

        class Msg:
            async def answer(self, text, **kw):
                sent.append(text)

        class State:
            async def clear(self):
                pass

        await handlers.traffic(Msg(), State(), svc)
        text = sent[0]
        assert "За 30 дней всего: 1.00 ГБ" in text
        assert "За всё время всего: 2.00 ГБ" in text
    finally:
        await svc.geo.close()
        await db.close()
