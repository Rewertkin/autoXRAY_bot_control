#!/usr/bin/env bash
# Установка бота на RU сервер с мостом autoXRAY (bridgeTLSxhttp.sh).
# Запуск из каталога репозитория: sudo bash install.sh
set -euo pipefail

APP_DIR=/opt/xraybot
DATA_DIR=/var/lib/xraybot
SRC_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

[[ $EUID -eq 0 ]] || { echo "Нужны права root: sudo bash install.sh"; exit 1; }
[[ -f /usr/local/etc/xray/config.json ]] || { echo "Не найден /usr/local/etc/xray/config.json. Сначала установи мост autoXRAY."; exit 1; }

echo "==> Пакеты"
apt-get update -qq
apt-get install -y -qq python3 python3-venv python3-pip openssl iproute2 >/dev/null

echo "==> Файлы в $APP_DIR"
mkdir -p "$APP_DIR" "$DATA_DIR"
chmod 700 "$DATA_DIR"
rm -rf "$APP_DIR/xraybot"
cp -r "$SRC_DIR/xraybot" "$APP_DIR/"
cp "$SRC_DIR/requirements.txt" "$APP_DIR/"

echo "==> Виртуальное окружение"
[[ -d "$APP_DIR/venv" ]] || python3 -m venv "$APP_DIR/venv"
"$APP_DIR/venv/bin/pip" install -q --upgrade pip
"$APP_DIR/venv/bin/pip" install -q -r "$APP_DIR/requirements.txt"

if [[ ! -f "$APP_DIR/.env" ]]; then
    echo "==> Настройка .env"
    cp "$SRC_DIR/.env.example" "$APP_DIR/.env"
    read -rp "Токен бота от @BotFather: " BOT_TOKEN
    read -rp "Твой Telegram ID (через @userinfobot): " ADMIN_IDS
    read -rp "Домен RU-моста (например my.vpn.ru): " DOMAIN
    sed -i "s|^BOT_TOKEN=.*|BOT_TOKEN=${BOT_TOKEN}|; s|^ADMIN_IDS=.*|ADMIN_IDS=${ADMIN_IDS}|; s|^DOMAIN=.*|DOMAIN=${DOMAIN}|" "$APP_DIR/.env"
fi
chmod 600 "$APP_DIR/.env"

echo "==> Включаю API и статистику в конфиге Xray (резервная копия: config.json.bak)"
cd "$APP_DIR"
XRAYBOT_ENV="$APP_DIR/.env" "$APP_DIR/venv/bin/python" -m xraybot patch

echo "==> Проверка"
XRAYBOT_ENV="$APP_DIR/.env" "$APP_DIR/venv/bin/python" -m xraybot check || true

echo "==> Служба systemd"
cp "$SRC_DIR/deploy/xraybot.service" /etc/systemd/system/xraybot.service
systemctl daemon-reload
systemctl enable --now xraybot
systemctl restart xraybot
sleep 3
systemctl --no-pager --lines=5 status xraybot || true

echo
echo "Готово. Напиши боту /start в Telegram."
echo "Логи: journalctl -u xraybot -f"
