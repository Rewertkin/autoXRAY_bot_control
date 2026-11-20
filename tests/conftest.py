import json
import os
import stat
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from xraybot.config import Settings  # noqa: E402

# Упрощённая копия конфига моста autoXRAY (bridgeTLSxhttp.sh)
BRIDGE_CONFIG = {
    "log": {"access": "none", "error": "none", "loglevel": "none"},
    "inbounds": [
        {
            "tag": "RUbrEUtlsVISION",
            "port": 443,
            "listen": "0.0.0.0",
            "protocol": "vless",
            "settings": {
                "clients": [{"id": "11111111-2222-0000-3333-444444444444", "flow": "xtls-rprx-vision"}],
                "decryption": "none",
                "fallbacks": [{"dest": "/dev/shm/nginx.sock", "xver": 2}],
            },
            "streamSettings": {"network": "raw", "security": "tls"},
        },
        {
            "tag": "RUbrEUxhttpTLS",
            "port": 3333,
            "listen": "127.0.0.1",
            "protocol": "vless",
            "settings": {"clients": [{"id": "11111111-2222-0000-3333-444444444444"}], "decryption": "none"},
            "streamSettings": {"network": "xhttp", "xhttpSettings": {"mode": "stream-up", "path": "/abc123"}},
        },
        {
            "tag": "RUsocks5",
            "port": 10443,
            "listen": "127.0.0.1",
            "protocol": "mixed",
            "settings": {"auth": "password", "accounts": [{"user": "u1", "pass": "p@ss/w"}]},
        },
    ],
    "outbounds": [
        {
            "tag": "proxy-0",
            "protocol": "vless",
            "settings": {"vnext": [{"port": 443, "users": [{"id": "x"}], "address": "nl.example.org"}]},
            "streamSettings": {
                "network": "xhttp",
                "xhttpSettings": {"mode": "stream-up", "path": "/nlpath"},
                "security": "tls",
                "tlsSettings": {"serverName": "nl.example.org", "fingerprint": "chrome"},
            },
        },
        {"tag": "direct", "protocol": "freedom"},
    ],
    "routing": {"rules": []},
}


@pytest.fixture
def env(tmp_path, monkeypatch):
    fake_dir = tmp_path / "fake"
    fake_dir.mkdir()
    fake = Path(__file__).with_name("fake_xray.py")
    fake.chmod(fake.stat().st_mode | stat.S_IEXEC)
    cfg = tmp_path / "config.json"
    cfg.write_text(json.dumps(BRIDGE_CONFIG))
    web = tmp_path / "www"
    web.mkdir()
    for k, v in {
        "BOT_TOKEN": "123:abc",
        "ADMIN_IDS": "42",
        "DOMAIN": "link.example.com",
        "XRAY_CONFIG": str(cfg),
        "XRAY_BIN": str(fake),
        "DB_PATH": str(tmp_path / "bot.db"),
        "WEB_ROOT": str(web),
        "FAKE_XRAY_DIR": str(fake_dir),
    }.items():
        monkeypatch.setenv(k, v)
    settings = Settings.load(tmp_path / "missing.env")
    return settings, fake_dir


def read_cfg(settings):
    return json.loads(Path(settings.xray_config).read_text())


os.environ.setdefault("PYTHONDONTWRITEBYTECODE", "1")
