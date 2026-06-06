#!/usr/bin/env bash
# One-shot installer for the Parser on a fresh Ubuntu 22.04 VDS (e.g. FirstVDS).
# Run as root:  bash install.sh parser.example.ru your_password
set -euo pipefail

DOMAIN="${1:-}"
PASSWORD="${2:-}"
BRANCH="claude/blissful-feynman-lT3f6"
REPO="https://github.com/kirillrand4-alt/parser"
DIR="/opt/parser"

if [[ -z "$DOMAIN" || -z "$PASSWORD" ]]; then
  echo "Usage: bash install.sh <domain> <webui_password>"
  echo "Example: bash install.sh parser.example.ru 'MyStrongPass123'"
  exit 1
fi

echo "==> System packages"
apt-get update -y
apt-get install -y python3 python3-pip python3-venv git nginx certbot python3-certbot-nginx

echo "==> Clone / update code"
if [[ -d "$DIR/.git" ]]; then
  git -C "$DIR" fetch origin "$BRANCH" && git -C "$DIR" reset --hard "origin/$BRANCH"
else
  git clone -b "$BRANCH" "$REPO" "$DIR"
fi

echo "==> Python deps"
pip3 install -q -r "$DIR/requirements.txt"
pip3 install -q flask

echo "==> Data dirs"
mkdir -p "$DIR/data" "$DIR/cache" "$DIR/http_cache"

echo "==> systemd service"
sed -e "s/ИЗМЕНИ_МЕНЯ/${PASSWORD}/" \
    "$DIR/deploy/parser-webui.service" > /etc/systemd/system/parser-webui.service
systemctl daemon-reload
systemctl enable --now parser-webui

echo "==> nginx"
sed "s/parser.example.ru/${DOMAIN}/g" \
    "$DIR/deploy/nginx-parser.conf" > /etc/nginx/sites-available/parser
ln -sf /etc/nginx/sites-available/parser /etc/nginx/sites-enabled/parser
rm -f /etc/nginx/sites-enabled/default
nginx -t && systemctl reload nginx

echo "==> SSL certificate (Let's Encrypt)"
certbot --nginx -d "$DOMAIN" --non-interactive --agree-tos \
    -m "admin@${DOMAIN}" --redirect || \
    echo "!! certbot failed — check that the domain's A-record points to this server, then re-run: certbot --nginx -d ${DOMAIN}"

echo
echo "================================================================"
echo " Готово!  Открывайте: https://${DOMAIN}"
echo " Логин: admin    Пароль: (тот что вы задали)"
echo " Логи UI:     journalctl -u parser-webui -f"
echo " Перезапуск:  systemctl restart parser-webui"
echo "================================================================"
