#!/bin/bash
# Переносит телеграм-пульт на сервер, чтобы он работал круглосуточно.
# Ничего чужого не трогает: своя папка /opt/vk-pult, своя служба vk-pult.
# Mini-WMS (/root/myapp/Mini-WMS, служба mini-wms) остаётся как есть.
set -e

DIR=/opt/vk-pult
REPO=${REPO:-https://github.com/TehnikGs/vk-pult.git}

echo "→ проверяю, что есть git и python"
for pkg in git python3-venv curl; do
  case "$pkg" in
    git)  command -v git  >/dev/null || MISSING="$MISSING git" ;;
    curl) command -v curl >/dev/null || MISSING="$MISSING curl" ;;
    python3-venv) python3 -c "import venv" 2>/dev/null || MISSING="$MISSING python3-venv" ;;
  esac
done
if [ -n "$MISSING" ]; then
  echo "  ставлю:$MISSING"
  apt-get update -qq && apt-get install -y -qq $MISSING
fi

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
  # ключи можно передать заранее переменными, иначе спросим по одному
  VKC="${VK_COMMUNITY_TOKEN:-}"
  VKU="${VK_USER_TOKEN:-}"
  TGT="${TG_BOT_TOKEN:-}"
  PAYD="${PAY_DETAILS:-Оплата на ВТБ по номеру телефона 8 926 033-77-22. На Сбер переводить не нужно — платёж не дойдёт.}"
  if [ -z "$VKC" ] || [ -z "$VKU" ] || [ -z "$TGT" ]; then
    echo
    echo "Теперь нужны ключи. Вставляй по одному, каждый — Enter."
    echo "Они есть в файле .env на твоём ноутбуке (папка vk-admin-bot),"
    echo "или спроси у Claude — он их знает."
    echo
    [ -n "$VKC" ] || read -rp "1/4 Ключ сообщества ВК (vk1.a....): " VKC
    [ -n "$VKU" ] || read -rp "2/4 Личный ключ админа ВК (vk1.a....): " VKU
    [ -n "$TGT" ] || read -rp "3/4 Токен телеграм-бота (цифры:буквы): " TGT
    read -rp "4/4 Реквизиты для оплаты (Enter — оставить как есть): " PAYD_IN
    [ -n "$PAYD_IN" ] && PAYD="$PAYD_IN"
  fi
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
  echo "  ⚠️  Telegram отсюда НЕ открывается — нужен прокси."
  FOUND=$(grep -rh "^TG_PROXY=" /root/myapp/task-bot/.env /root/task-bot/.env 2>/dev/null | head -1)
  if [ -n "$FOUND" ]; then
    echo "  Нашёл рабочий прокси у задачника, беру его же:"
    echo "  $FOUND"
    sed -i "s|^TG_PROXY=.*|$FOUND|" "$DIR/.env"
    "$DIR/.venv/bin/pip" install -q aiohttp-socks
  else
    echo "     Впиши строку TG_PROXY=socks5://логин:пароль@адрес:порт"
    echo "     в файл $DIR/.env — тот же прокси, что у задачника task-bot."
  fi
fi

# ─────────────── дела, переданные предыдущим админом ───────────────
if [ ! -f "$DIR/pult.db" ]; then
  echo "→ переношу клиентов и график закрепа"
  (cd "$DIR" && "$DIR/.venv/bin/python" import_lera.py | head -6)
fi

echo "→ служба"
sed "s|/opt/vk-pult|$DIR|g" "$DIR/deploy/vk-pult.service" > /etc/systemd/system/vk-pult.service
systemctl daemon-reload
systemctl enable --now vk-pult
sleep 3
systemctl --no-pager -l status vk-pult | head -14

echo
echo "ГОТОВО."
echo "1) Останови бота на ноутбуке (закрой чёрное окно) — иначе они мешают друг другу."
echo "2) Напиши боту в Telegram /start — он привяжется к твоему чату уже с сервера."
echo "3) Разреши отправку: /replies on   (без этого кнопки ответов молчат)"
echo "4) Проверь: /check, /pins, /clients"
echo
echo "Полезное:"
echo "  журнал:      journalctl -u vk-pult -f"
echo "  перезапуск:  systemctl restart vk-pult"
echo "  обновление:  cd $DIR && git pull && systemctl restart vk-pult"
