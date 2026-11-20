"""Настройки бота из файла .env и переменных окружения."""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

DEFAULT_ENV_PATH = Path("/opt/xraybot/.env")


def load_env_file(path: Path) -> None:
    """Простой парсер KEY=VALUE. Переменные окружения имеют приоритет над файлом."""
    if not path.exists():
        return
    for raw in path.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        key = key.strip()
        value = value.strip().strip('"').strip("'")
        os.environ.setdefault(key, value)


def _int(name: str, default: int) -> int:
    value = os.environ.get(name, "").strip()
    return int(value) if value else default


@dataclass(frozen=True)
class Settings:
    bot_token: str
    admin_ids: frozenset[int]
    domain: str
    xray_config: Path
    xray_bin: str
    api_addr: str
    db_path: Path
    tg_proxy: str  # "auto", "" (без прокси) или URL socks5://...
    timezone: str
    xhttp_tag: str
    vision_tag: str
    socks_tag: str
    route_id: int
    cert_path: Path
    web_root: Path
    link_template_xhttp: str
    link_template_vision: str
    traffic_poll_sec: int
    online_poll_sec: int
    online_window_sec: int
    bot_socks_port: int
    autodiag_min: int
    chain_probe_sec: int

    @classmethod
    def load(cls, env_path: Path | None = None) -> "Settings":
        load_env_file(env_path or Path(os.environ.get("XRAYBOT_ENV", DEFAULT_ENV_PATH)))
        token = os.environ.get("BOT_TOKEN", "").strip()
        admins = frozenset(
            int(x) for x in os.environ.get("ADMIN_IDS", "").replace(" ", "").split(",") if x
        )
        domain = os.environ.get("DOMAIN", "").strip()
        missing = [n for n, v in (("BOT_TOKEN", token), ("ADMIN_IDS", admins), ("DOMAIN", domain)) if not v]
        if missing:
            raise SystemExit(f"Не заданы обязательные параметры в .env: {', '.join(missing)}")
        return cls(
            bot_token=token,
            admin_ids=admins,
            domain=domain,
            xray_config=Path(os.environ.get("XRAY_CONFIG", "/usr/local/etc/xray/config.json")),
            xray_bin=os.environ.get("XRAY_BIN", "/usr/local/bin/xray"),
            api_addr=os.environ.get("XRAY_API", "127.0.0.1:10085"),
            db_path=Path(os.environ.get("DB_PATH", "/var/lib/xraybot/bot.db")),
            tg_proxy=os.environ.get("TG_PROXY", "auto").strip(),
            timezone=os.environ.get("TZ_NAME", "Europe/Moscow"),
            xhttp_tag=os.environ.get("XHTTP_TAG", "RUbrEUxhttpTLS"),
            vision_tag=os.environ.get("VISION_TAG", "RUbrEUtlsVISION"),
            socks_tag=os.environ.get("SOCKS_TAG", "RUsocks5"),
            route_id=_int("ROUTE_ID", 1),
            cert_path=Path(os.environ.get("CERT_PATH", "/var/lib/xray/cert/fullchain.pem")),
            web_root=Path(os.environ.get("WEB_ROOT", f"/var/www/{domain}")),
            link_template_xhttp=os.environ.get("LINK_TEMPLATE_XHTTP", "").strip(),
            link_template_vision=os.environ.get("LINK_TEMPLATE_VISION", "").strip(),
            traffic_poll_sec=_int("TRAFFIC_POLL_SEC", 60),
            online_poll_sec=_int("ONLINE_POLL_SEC", 30),
            online_window_sec=_int("ONLINE_WINDOW_SEC", 120),
            bot_socks_port=_int("BOT_SOCKS_PORT", 10444),
            autodiag_min=max(1, _int("AUTODIAG_MIN", 15)),
            chain_probe_sec=max(15, _int("CHAIN_PROBE_SEC", 60)),
        )
