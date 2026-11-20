"""UUID с поддержкой vlessRoute и сборка ссылок vless:// для клиентов моста."""

from __future__ import annotations

import html
import json
import re
import uuid as uuidlib
from pathlib import Path
from urllib.parse import quote

VLESS_RE = re.compile(r"vless://[^\s\"'<>|]+")

# Параметр extra для XHTTP, как в скрипте autoXRAY (используется, если шаблон не найден)
DEFAULT_XHTTP_EXTRA = {
    "noGRPCHeader": True,
    "xPaddingBytes": "150-400",
    "scMaxEachPostBytes": 500000,
    "scMinPostsIntervalMs": "5-15",
    "scMaxBufferedPosts": 20,
    "scStreamUpServerSecs": "90-180",
    "xmux": {"maxConcurrency": "2-4", "cMaxReuseTimes": "800-1500", "hMaxReusableSecs": "900-1200"},
}


def new_uuid() -> str:
    """Случайный UUID, у которого третья группа = 0000.

    Xray при проверке пользователя обнуляет байты 6-7 (третья группа) и отдаёт их
    в маршрутизацию как vlessRoute. Поэтому в конфиг пишем 0000, а в ссылку - номер маршрута.
    """
    raw = uuidlib.uuid4().hex
    return f"{raw[0:8]}-{raw[8:12]}-0000-{raw[16:20]}-{raw[20:32]}"


def route_uuid(base_uuid: str, route_id: int) -> str:
    parts = base_uuid.split("-")
    parts[2] = f"{route_id:04x}"
    return "-".join(parts)


def _classify(link: str, domain: str) -> str | None:
    head = link.split("#", 1)[0]
    if f"@{domain}:" not in head:
        return None  # например, Direct EU на NL домене
    if "type=xhttp" in head:
        return "xhttp"
    if "flow=xtls-rprx-vision" in head:
        return "vision"
    return None


def make_template(link: str) -> str:
    """vless://UUID@host...#remark -> vless://{uuid}@host...#{remark}"""
    body = link.split("#", 1)[0]
    m = re.match(r"vless://([^@]+)@(.*)$", body)
    if not m:
        raise ValueError("не похоже на ссылку vless://")
    return f"vless://{{uuid}}@{m.group(2)}#{{remark}}"


def discover_templates(web_root: Path, domain: str) -> dict[str, str]:
    """Ищет ссылки моста в файлах страницы подписки autoXRAY и делает из них шаблоны."""
    found: dict[str, str] = {}
    if not web_root.is_dir():
        return found
    for path in sorted(web_root.glob("*.html")) + sorted(web_root.glob("*.txt")):
        try:
            text = html.unescape(path.read_text(encoding="utf-8", errors="replace"))
        except OSError:
            continue
        for link in VLESS_RE.findall(text):
            kind = _classify(link, domain)
            if kind and kind not in found:
                try:
                    found[kind] = make_template(link)
                except ValueError:
                    continue
        if len(found) == 2:
            break
    return found


def fallback_templates(cfg: dict, domain: str, xhttp_tag: str, fp: str = "chrome") -> dict[str, str]:
    """Шаблоны, собранные из конфига Xray, если страница подписки недоступна."""
    path = "/"
    for inbound in cfg.get("inbounds") or []:
        if inbound.get("tag") == xhttp_tag:
            path = ((inbound.get("streamSettings") or {}).get("xhttpSettings") or {}).get("path", "/")
    extra = quote(json.dumps(DEFAULT_XHTTP_EXTRA, separators=(",", ":")), safe="")
    xhttp = (
        f"vless://{{uuid}}@{domain}:443?security=tls&alpn=h2&type=xhttp&mode=stream-up"
        f"&path={quote(path, safe='')}&extra={extra}&sni={domain}&fp={fp}#{{remark}}"
    )
    vision = (
        f"vless://{{uuid}}@{domain}:443?security=tls&type=tcp&headerType=&path=&host="
        f"&flow=xtls-rprx-vision&sni={domain}&fp={fp}&spx=%2F#{{remark}}"
    )
    return {"xhttp": xhttp, "vision": vision}


def build_link(template: str, base_uuid: str, route_id: int, remark: str) -> str:
    return template.replace("{uuid}", route_uuid(base_uuid, route_id)).replace("{remark}", quote(remark, safe=""))


class LinkBuilder:
    def __init__(self, settings, load_config):
        self.s = settings
        self._load_config = load_config
        self._templates: dict[str, str] | None = None

    def templates(self) -> dict[str, str]:
        if self._templates is None:
            t: dict[str, str] = {}
            if self.s.link_template_xhttp:
                t["xhttp"] = make_template(self.s.link_template_xhttp)
            if self.s.link_template_vision:
                t["vision"] = make_template(self.s.link_template_vision)
            if len(t) < 2:
                for k, v in discover_templates(self.s.web_root, self.s.domain).items():
                    t.setdefault(k, v)
            if len(t) < 2:
                for k, v in fallback_templates(self._load_config(), self.s.domain, self.s.xhttp_tag).items():
                    t.setdefault(k, v)
            self._templates = t
        return self._templates

    def reset(self) -> None:
        self._templates = None

    def links_for(self, client) -> list[tuple[str, str]]:
        """[(название, ссылка)] для включённых у клиента подключений."""
        t = self.templates()
        result = []
        if client.use_xhttp:
            result.append(("XHTTP TLS", build_link(t["xhttp"], client.uuid, self.s.route_id, f"{client.name} XHTTP")))
        if client.use_vision:
            result.append(("RAW VISION", build_link(t["vision"], client.uuid, self.s.route_id, f"{client.name} VISION")))
        return result
