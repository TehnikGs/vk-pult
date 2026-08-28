#!/bin/bash
# Переносит телеграм-пульт на сервер, чтобы он работал круглосуточно.
# Ничего чужого не трогает: своя папка /opt/vk-pult, своя служба vk-pult.
# Mini-WMS (/root/myapp/Mini-WMS, служба mini-wms) остаётся как есть.
set -e

DIR=/opt/vk-pult
REPO=${REPO:-https://github.com/TehnikGs/vk-pult.git}

echo "→ забираю код"
if [ -d "$DIR/.git" ]; then
  git -C "$DIR" pull --ff-only
else
  git clone --depth 1 "$REPO" "$DIR"
fi

echo "→ окружение"
if [ ! -d "$DIR/.venv" ]; then
  python3 -m venv "$DIR/.venv"
fi
"$DIR/.venv/bin/pip" install -q --upgrade pip
"$DIR/.venv/bin/pip" install -q -r "$DIR/requirements.txt"

# ─────────────────────────── настройки ───────────────────────────
if [ ! -f "$DIR/.env" ]; then
  echo
  echo "Теперь нужны ключи. Вставляй по одному, каждый — Enter."
  echo "Они есть в файле .env на твоём ноутбуке (папка vk-admin-bot),"
  echo "или спроси у Claude — он их знает."
  echo
  read -rp "1/4 Ключ сообщества ВК (vk1.a....): " VKC
  read -rp "2/4 Личный ключ админа ВК (vk1.a....): " VKU
  read -rp "3/4 Токен телеграм-бота (цифры:буквы): " TGT
  read -rp "4/4 Реквизиты для оплаты (одной строкой): " PAYD
  cat > "$DIR/.env" <<ENVFILE
VK_COMMUNITY_TOKEN=$VKC
VK_USER_TOKEN=$VKU
VK_GROUP_ID=69451964
TG_BOT_TOKEN=$TGT
TG_PROXY=
TG_ADMIN_CHAT_ID=
PAY_DETAILS=$PAYD
SITE_API=
SITE_TOKEN=
ENVFILE
  chmod 600 "$DIR/.env"
  echo "  .env создан"
else
  echo "→ .env уже есть, не трогаю"
fi

# ─────────────── Телеграм из России часто закрыт: проверим ───────────────
echo "→ проверяю доступ к Telegram с этого сервера"
if curl -s --max-time 12 https://api.telegram.org >/dev/null 2>&1; then
  echo "  Telegram доступен напрямую — прокси не нужен"
else
  echo "  ⚠️  Telegram отсюда НЕ открывается."
  echo "     Нужен прокси: впиши строку TG_PROXY=socks5://логин:пароль@адрес:порт"
  echo "     в файл $DIR/.env (тот же прокси, что у задачника task-bot)."
fi

echo "→ служба"
sed "s|/opt/vk-pult|$DIR|g" "$DIR/deploy/vk-pult.service" > /etc/systemd/system/vk-pult.service
systemctl daemon-reload
systemctl enable --now vk-pult
sleep 3
systemctl --no-pager -l status vk-pult | head -14

echo
echo "ГОТОВО."
echo "1) Останови бота на ноутбуке (закрой чёрное окно) — иначе они будут мешать друг другу."
echo "2) Напиши боту в Telegram /start — он привяжется к твоему чату уже с сервера."
echo "3) Проверь: /check и /grafik"
echo
echo "Полезное:"
echo "  журнал:      journalctl -u vk-pult -f"
echo "  перезапуск:  systemctl restart vk-pult"
echo "  обновление:  cd $DIR && git pull && systemctl restart vk-pult"
