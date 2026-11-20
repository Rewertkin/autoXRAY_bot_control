"""Работа с Xray: файл конфигурации моста и gRPC API через CLI `xray api ...`."""

from __future__ import annotations

import asyncio
import json
import logging
import os
import shutil
import tempfile
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import quote

log = logging.getLogger(__name__)

VISION_FLOW = "xtls-rprx-vision"
# Отдельный SOCKS-вход для связи бота с Telegram. Направлен прямо в NL-outbound,
# в обход балансировщика моста: тот при медленном пинге отправляет трафик в block.
BOT_SOCKS_TAG = "xraybot-tg"


class XrayError(RuntimeError):
    pass


async def run_cmd(*args: str, timeout: float = 15) -> tuple[int, str, str]:
    try:
        proc = await asyncio.create_subprocess_exec(
            *args, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE
        )
    except FileNotFoundError:
        return 127, "", f"command not found: {args[0]}"
    try:
        out, err = await asyncio.wait_for(proc.communicate(), timeout)
    except asyncio.TimeoutError:
        proc.kill()
        await proc.wait()
        return 124, "", f"timeout after {timeout}s"
    return proc.returncode or 0, out.decode(errors="replace"), err.decode(errors="replace")


def _parse_json(text: str) -> dict:
    text = text.strip()
    if not text:
        return {}
    start = text.find("{")
    if start < 0:
        return {}
    try:
        return json.loads(text[start:])
    except json.JSONDecodeError:
        return {}


def _int(value) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return 0


def _ts(value) -> int:
    """Метки времени из Xray: секунды (на всякий случай поддерживаем и миллисекунды)."""
    v = _int(value)
    return v // 1000 if v > 10**12 else v


def parse_traffic(data: dict) -> dict[str, tuple[int, int]]:
    """Ответ statsquery -> {email: (up, down)}."""
    result: dict[str, list[int]] = {}
    for stat in data.get("stat") or []:
        parts = str(stat.get("name", "")).split(">>>")
        if len(parts) != 4 or parts[0] != "user" or parts[2] != "traffic":
            continue
        email, direction = parts[1], parts[3]
        pair = result.setdefault(email, [0, 0])
        if direction == "uplink":
            pair[0] += _int(stat.get("value"))
        elif direction == "downlink":
            pair[1] += _int(stat.get("value"))
    return {k: (v[0], v[1]) for k, v in result.items()}


def parse_online_all(data: dict) -> dict[str, dict[str, int]]:
    """Ответ statsonlineiplist -all -> {email: {ip: last_seen}}."""
    result: dict[str, dict[str, int]] = {}
    for user in data.get("users") or []:
        email = user.get("email")
        if not email:
            continue
        ips: dict[str, int] = {}
        for entry in user.get("ips") or []:
            ip = entry.get("ip")
            if ip:
                ips[ip] = _ts(entry.get("lastSeen", entry.get("last_seen")))
        if ips:
            result[email] = ips
    return result


def parse_online_single(data: dict) -> dict[str, int]:
    """Ответ statsonlineiplist -email X -> {ip: last_seen}."""
    return {ip: _ts(ts) for ip, ts in (data.get("ips") or {}).items()}


def _uuid_key(uuid: str) -> str:
    """UUID без третьей группы: Xray её игнорирует при поиске пользователя (vlessRoute)."""
    parts = uuid.lower().split("-")
    if len(parts) == 5:
        parts[2] = "0000"
    return "-".join(parts)


@dataclass
class ManagedUser:
    """Клиент бота в терминах конфигурации Xray."""

    email: str
    uuid: str
    use_xhttp: bool
    use_vision: bool


class XrayConfigFile:
    """Чтение, патч и атомарная запись config.json моста.

    Правило: клиенты с полем "email" управляются ботом. Клиенты без email
    (например, общий UUID из скрипта autoXRAY) никогда не трогаются.
    """

    def __init__(self, path: Path, xray_bin: str):
        self.path = path
        self.xray_bin = xray_bin
        self.lock = asyncio.Lock()

    def load(self) -> dict:
        return json.loads(self.path.read_text(encoding="utf-8"))

    def mtime(self) -> float:
        return self.path.stat().st_mtime

    @staticmethod
    def find_inbound(cfg: dict, tag: str) -> dict | None:
        for inbound in cfg.get("inbounds") or []:
            if inbound.get("tag") == tag:
                return inbound
        return None

    @staticmethod
    def ensure_api_and_stats(cfg: dict, api_addr: str) -> bool:
        """Включает API, статистику и онлайн-трекинг пользователей. Возвращает True, если что-то изменилось."""
        before = json.dumps(cfg, sort_keys=True)
        api = cfg.setdefault("api", {})
        api.setdefault("tag", "api")
        api["listen"] = api_addr
        services = set(api.get("services") or [])
        services.update({"HandlerService", "StatsService"})
        api["services"] = sorted(services)
        cfg.setdefault("stats", {})
        policy = cfg.setdefault("policy", {})
        level0 = policy.setdefault("levels", {}).setdefault("0", {})
        level0["statsUserUplink"] = True
        level0["statsUserDownlink"] = True
        level0["statsUserOnline"] = True
        return json.dumps(cfg, sort_keys=True) != before

    @staticmethod
    def first_proxy_outbound(cfg: dict) -> str | None:
        for out in cfg.get("outbounds") or []:
            if out.get("protocol") == "vless" and str(out.get("tag", "")).startswith("proxy"):
                return out["tag"]
        return None

    @classmethod
    def ensure_bot_socks(cls, cfg: dict, port: int) -> bool:
        """Добавляет SOCKS-вход бота и правило маршрутизации в первый NL-outbound. True, если что-то изменилось."""
        target = cls.first_proxy_outbound(cfg)
        if not target:
            return False
        before = json.dumps(cfg, sort_keys=True)
        inbound = {
            "tag": BOT_SOCKS_TAG,
            "listen": "127.0.0.1",
            "port": port,
            "protocol": "socks",
            "settings": {"auth": "noauth", "udp": False},
        }
        inbounds = [i for i in cfg.setdefault("inbounds", []) if i.get("tag") != BOT_SOCKS_TAG]
        inbounds.append(inbound)
        cfg["inbounds"] = inbounds
        routing = cfg.setdefault("routing", {})
        rules = [r for r in routing.get("rules") or [] if BOT_SOCKS_TAG not in (r.get("inboundTag") or [])]
        rules.insert(0, {"inboundTag": [BOT_SOCKS_TAG], "outboundTag": target})
        routing["rules"] = rules
        return json.dumps(cfg, sort_keys=True) != before

    @classmethod
    def bot_socks_ok(cls, cfg: dict) -> bool:
        if cls.find_inbound(cfg, BOT_SOCKS_TAG) is None:
            return False
        rules = (cfg.get("routing") or {}).get("rules") or []
        return bool(rules) and BOT_SOCKS_TAG in (rules[0].get("inboundTag") or [])

    @staticmethod
    def api_enabled(cfg: dict) -> bool:
        api = cfg.get("api") or {}
        lvl = ((cfg.get("policy") or {}).get("levels") or {}).get("0") or {}
        return (
            "StatsService" in (api.get("services") or [])
            and "HandlerService" in (api.get("services") or [])
            and "stats" in cfg
            and lvl.get("statsUserUplink")
            and lvl.get("statsUserOnline")
        )

    @staticmethod
    def client_entry(user: ManagedUser, vision: bool) -> dict:
        entry = {"id": user.uuid, "email": user.email, "level": 0}
        if vision:
            entry["flow"] = VISION_FLOW
        return entry

    @classmethod
    def apply_users(cls, cfg: dict, users: list[ManagedUser], xhttp_tag: str, vision_tag: str) -> None:
        """Заменяет всех клиентов с email на актуальный список бота, сохраняя прочих."""
        for tag, is_vision in ((xhttp_tag, False), (vision_tag, True)):
            inbound = cls.find_inbound(cfg, tag)
            if inbound is None:
                raise XrayError(f"В конфиге нет inbound с тегом {tag}")
            settings = inbound.setdefault("settings", {})
            managed_ids = {_uuid_key(u.uuid) for u in users}
            # клиенты без email не трогаем, кроме тех, кого бот взял под управление (main)
            keep = [
                c
                for c in settings.get("clients") or []
                if not c.get("email") and _uuid_key(str(c.get("id", ""))) not in managed_ids
            ]
            for user in users:
                if (user.use_vision if is_vision else user.use_xhttp):
                    keep.append(cls.client_entry(user, is_vision))
            settings["clients"] = keep

    @staticmethod
    def managed_emails(cfg: dict, tag: str) -> set[str]:
        inbound = XrayConfigFile.find_inbound(cfg, tag) or {}
        return {c["email"] for c in (inbound.get("settings") or {}).get("clients") or [] if c.get("email")}

    async def validate(self, cfg: dict) -> None:
        fd, tmp = tempfile.mkstemp(prefix="xraybot-", suffix=".json")
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as f:
                json.dump(cfg, f, ensure_ascii=False, indent=2)
            rc, out, err = await run_cmd(self.xray_bin, "run", "-test", "-c", tmp, timeout=60)
            if rc != 0:
                raise XrayError(f"xray -test не прошёл: {(err or out).strip()[-500:]}")
        finally:
            os.unlink(tmp)

    async def save(self, cfg: dict, validate: bool = True) -> None:
        if validate:
            await self.validate(cfg)
        backup = self.path.with_suffix(".json.bak")
        if self.path.exists():
            shutil.copy2(self.path, backup)
        tmp = self.path.with_suffix(".json.tmp")
        tmp.write_text(json.dumps(cfg, ensure_ascii=False, indent=2), encoding="utf-8")
        if self.path.exists():
            st = self.path.stat()
            os.chmod(tmp, st.st_mode)
            try:
                os.chown(tmp, st.st_uid, st.st_gid)
            except PermissionError:
                pass
        os.replace(tmp, self.path)

    def bot_proxy_url(self, fallback_tag: str) -> str | None:
        """SOCKS для выхода бота через NL: свой вход бота, иначе общий SOCKS моста."""
        try:
            cfg = self.load()
        except (OSError, json.JSONDecodeError):
            return None
        inbound = self.find_inbound(cfg, BOT_SOCKS_TAG)
        if inbound and self.bot_socks_ok(cfg):
            return f"socks5://{inbound.get('listen') or '127.0.0.1'}:{inbound.get('port')}"
        return self.socks_proxy_url(fallback_tag)

    def socks_proxy_url(self, socks_tag: str) -> str | None:
        """URL общего SOCKS-входа моста (идёт через балансировщик autoXRAY)."""
        try:
            inbound = self.find_inbound(self.load(), socks_tag)
        except (OSError, json.JSONDecodeError):
            return None
        if not inbound:
            return None
        listen = inbound.get("listen") or "127.0.0.1"
        port = inbound.get("port")
        accounts = (inbound.get("settings") or {}).get("accounts") or []
        if accounts:
            user = quote(str(accounts[0].get("user", "")), safe="")
            pwd = quote(str(accounts[0].get("pass", "")), safe="")
            return f"socks5://{user}:{pwd}@{listen}:{port}"
        return f"socks5://{listen}:{port}"


class XrayAPI:
    def __init__(self, xray_bin: str, addr: str):
        self.bin = xray_bin
        self.addr = addr

    async def _api(self, command: str, *args: str, timeout: float = 10) -> tuple[int, str, str]:
        # Xray CLI разбирает флаги Go-пакетом flag: они должны идти ДО позиционных аргументов
        # (файла для adu, email для rmu), иначе --server примется за ещё один файл или email.
        return await run_cmd(self.bin, "api", command, f"--server={self.addr}", *args, timeout=timeout)

    async def ping(self) -> bool:
        rc, _, _ = await self._api("statsquery", "-pattern", "xraybot-ping")
        return rc == 0

    async def sys_stats(self) -> dict:
        rc, out, _ = await self._api("statssys")
        return _parse_json(out) if rc == 0 else {}

    async def add_user(self, tag: str, port: int, entry: dict) -> None:
        payload = {
            "inbounds": [
                {
                    "tag": tag,
                    "listen": "127.0.0.1",
                    "port": port,
                    "protocol": "vless",
                    "settings": {"clients": [entry], "decryption": "none"},
                }
            ]
        }
        fd, tmp = tempfile.mkstemp(prefix="xraybot-adu-", suffix=".json")
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as f:
                json.dump(payload, f)
            rc, out, err = await self._api("adu", tmp)
        finally:
            os.unlink(tmp)
        text = f"{out}\n{err}"
        if rc != 0 or "Added 1" not in text:
            if "already exists" in text.lower():
                return
            raise XrayError(f"adu {tag}/{entry.get('email')}: {text.strip()[-300:]}")

    async def remove_user(self, tag: str, email: str) -> None:
        rc, out, err = await self._api("rmu", f"-tag={tag}", email)
        text = f"{out}\n{err}"
        if rc != 0 or "Removed 1" not in text:
            if "not found" in text.lower():
                return
            raise XrayError(f"rmu {tag}/{email}: {text.strip()[-300:]}")

    async def query_traffic(self, reset: bool = True) -> dict[str, tuple[int, int]]:
        args = ["statsquery", "-pattern", "user>>>"]
        if reset:
            args.append("-reset")
        rc, out, err = await self._api(*args)
        if rc != 0:
            raise XrayError(f"statsquery: {(err or out).strip()[-300:]}")
        return parse_traffic(_parse_json(out))

    async def online(self, emails: list[str]) -> dict[str, dict[str, int]]:
        rc, out, _ = await self._api("statsonlineiplist", "-all")
        if rc == 0:
            return parse_online_all(_parse_json(out))
        # старые версии Xray без -all: опрашиваем по одному
        result: dict[str, dict[str, int]] = {}
        for email in emails:
            rc, out, _ = await self._api("statsonlineiplist", "-email", email)
            if rc == 0:
                ips = parse_online_single(_parse_json(out))
                if ips:
                    result[email] = ips
        return result

    async def version(self) -> str:
        rc, out, _ = await run_cmd(self.bin, "version", timeout=5)
        return out.splitlines()[0].strip() if rc == 0 and out else "?"


class XrayManager:
    """Высокоуровневые операции: синхронизация пользователей бота с конфигом и живым Xray."""

    def __init__(self, settings, config: XrayConfigFile, api: XrayAPI):
        self.s = settings
        self.config = config
        self.api = api

    def _port(self, cfg: dict, tag: str) -> int:
        inbound = XrayConfigFile.find_inbound(cfg, tag)
        if not inbound:
            raise XrayError(f"В конфиге нет inbound {tag}")
        return int(inbound.get("port"))

    async def write_users(self, users: list[ManagedUser], patch_api: bool = False) -> bool:
        """Записывает пользователей в config.json. Возвращает True, если включали API (нужен рестарт)."""
        async with self.config.lock:
            cfg = self.config.load()
            api_changed = False
            if patch_api:
                api_changed = XrayConfigFile.ensure_api_and_stats(cfg, self.s.api_addr)
                api_changed = XrayConfigFile.ensure_bot_socks(cfg, self.s.bot_socks_port) or api_changed
            XrayConfigFile.apply_users(cfg, users, self.s.xhttp_tag, self.s.vision_tag)
            await self.config.save(cfg)
            return api_changed

    async def live_add(self, user: ManagedUser) -> list[str]:
        """Добавляет пользователя в работающий Xray без перезапуска. Возвращает список ошибок."""
        cfg = self.config.load()
        errors = []
        for tag, wanted, vision in (
            (self.s.xhttp_tag, user.use_xhttp, False),
            (self.s.vision_tag, user.use_vision, True),
        ):
            if not wanted:
                continue
            try:
                await self.api.add_user(tag, self._port(cfg, tag), XrayConfigFile.client_entry(user, vision))
            except XrayError as e:
                errors.append(str(e))
        return errors

    async def live_remove(self, email: str) -> list[str]:
        errors = []
        for tag in (self.s.xhttp_tag, self.s.vision_tag):
            try:
                await self.api.remove_user(tag, email)
            except XrayError as e:
                errors.append(str(e))
        return errors

    def config_in_sync(self, users: list[ManagedUser]) -> tuple[bool, str]:
        """Проверка, что конфиг на диске совпадает с базой бота (например, после перезапуска скрипта моста)."""
        try:
            cfg = self.config.load()
        except (OSError, json.JSONDecodeError) as e:
            return False, f"конфиг не читается: {e}"
        if not XrayConfigFile.api_enabled(cfg):
            return False, "в конфиге выключены API/статистика"
        if XrayConfigFile.first_proxy_outbound(cfg) and not XrayConfigFile.bot_socks_ok(cfg):
            return False, "нет SOCKS-входа бота до NL"
        want_x = {u.email for u in users if u.use_xhttp}
        want_v = {u.email for u in users if u.use_vision}
        have_x = XrayConfigFile.managed_emails(cfg, self.s.xhttp_tag)
        have_v = XrayConfigFile.managed_emails(cfg, self.s.vision_tag)
        if want_x != have_x or want_v != have_v:
            missing = (want_x - have_x) | (want_v - have_v)
            extra = (have_x - want_x) | (have_v - want_v)
            parts = []
            if missing:
                parts.append("нет в конфиге: " + ", ".join(sorted(missing)))
            if extra:
                parts.append("лишние в конфиге: " + ", ".join(sorted(extra)))
            return False, "; ".join(parts)
        return True, "ok"

    async def restart_xray(self) -> tuple[bool, str]:
        rc, out, err = await run_cmd("systemctl", "restart", "xray", timeout=30)
        if rc != 0:
            return False, (err or out).strip()
        for _ in range(10):
            await asyncio.sleep(1)
            if await self.api.ping():
                return True, "ok"
        return False, "Xray перезапущен, но API не отвечает"

