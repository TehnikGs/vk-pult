"""vk-admin-bot — телеграм-пульт «Подслушано МЭЗ \\ Малаховка» (vk.com/kak_slishno).

Управление сообществом целиком из Telegram:
  * личные сообщения группе -> карточка с готовыми ответами в один тап;
  * предложка -> карточка с кнопками: опубликовать, отложить, перенести в тему,
    ответить про платное размещение, запросить детали, отклонить с объяснением;
  * ответ своими словами — просто ответь (swipe reply) на карточку;
  * бронь рекламных дат, отчёт статистики по понедельникам.

Тексты сообщений — в texts.py. Настройки — в .env.
"""

import asyncio
import html
import json
import logging
import os
import re
import sqlite3
import time
from datetime import date, datetime, timedelta
from zoneinfo import ZoneInfo

import aiohttp
from aiogram import Bot, Dispatcher, F
from aiogram.client.default import DefaultBotProperties
from aiogram.client.session.aiohttp import AiohttpSession
from aiogram.filters import Command, CommandStart
from aiogram.types import (CallbackQuery, ForceReply, InlineKeyboardButton,
                           InlineKeyboardMarkup, Message)
from dotenv import load_dotenv

import classify as C
import orders as O
import texts as T

BASE = os.path.dirname(os.path.abspath(__file__))
load_dotenv(os.path.join(BASE, ".env"))

log = logging.getLogger("vk-admin-bot")
logging.basicConfig(level=logging.INFO,
                    format="%(asctime)s %(levelname)s %(name)s: %(message)s")

# ---------------------------------------------------------------- настройки
VK_COMMUNITY_TOKEN = os.getenv("VK_COMMUNITY_TOKEN", "").strip()
VK_USER_TOKEN = os.getenv("VK_USER_TOKEN", "").strip()
GROUP_ID = int(os.getenv("VK_GROUP_ID", str(T.GROUP_ID)))
TG_BOT_TOKEN = os.getenv("TG_BOT_TOKEN", "").strip()
TG_PROXY = os.getenv("TG_PROXY", "").strip()
PAY_DETAILS = os.getenv("PAY_DETAILS", "").strip() or \
    "Реквизиты пришлю следующим сообщением."
EVENING_HOUR = int(os.getenv("EVENING_HOUR", "19"))
DB_PATH = os.getenv("DB_PATH", os.path.join(BASE, "pult.db"))
SUGGEST_POLL_SECONDS = int(os.getenv("SUGGEST_POLL_SECONDS", "180"))
AUTO_MIN_CONFIDENCE = float(os.getenv("AUTO_MIN_CONFIDENCE", "0.75"))

# ── ПРЕДОХРАНИТЕЛИ ───────────────────────────────────────────────────────────
# Пока выключено, бот НИЧЕГО не пишет в ВК от имени сообщества: только
# показывает в Telegram, что он бы ответил. Включать, когда станете админом.
def flag(name: str, env: str) -> bool:
    v = os.getenv(env, "").strip().lower()
    if v in ("on", "1", "yes", "да"):
        return True
    if v in ("off", "0", "no", "нет"):
        return False
    return kv_get(name, "0") == "1"


def vk_writing_on() -> bool:
    """Разрешена ли вообще отправка сообщений жителям."""
    return flag("vk_send", "VK_SEND")


# автоответов больше нет: бот пишет людям только когда вы нажали кнопку

MSK = ZoneInfo("Europe/Moscow")
VK_API = "https://api.vk.com/method/"
VK_VER = "5.199"

MONTHS = ["января", "февраля", "марта", "апреля", "мая", "июня", "июля",
          "августа", "сентября", "октября", "ноября", "декабря"]
WEEKDAYS = ["пн", "вт", "ср", "чт", "пт", "сб", "вс"]
AD_WORDS = ("реклам", "прайс", "стоимост", "сколько стоит", "разместить",
            "размещение", "цена", "цены", "сотрудничеств", "пиар")

# ---------------------------------------------------------------- база
db = sqlite3.connect(DB_PATH, check_same_thread=False)
db.execute("CREATE TABLE IF NOT EXISTS seen_suggests (post_id INTEGER PRIMARY KEY)")
db.execute("CREATE TABLE IF NOT EXISTS kv (k TEXT PRIMARY KEY, v TEXT)")
db.execute("CREATE TABLE IF NOT EXISTS bookings ("
           "day TEXT PRIMARY KEY, client TEXT, amount INTEGER, created TEXT)")
db.execute("CREATE TABLE IF NOT EXISTS site_bookings ("
           "id INTEGER PRIMARY KEY, data TEXT, status TEXT)")
db.execute("CREATE TABLE IF NOT EXISTS sent_log ("
           "id INTEGER PRIMARY KEY AUTOINCREMENT, ts TEXT, user_id INTEGER, kind TEXT)")
db.commit()
O.init(db)


def kv_get(k, default=None):
    row = db.execute("SELECT v FROM kv WHERE k=?", (k,)).fetchone()
    return row[0] if row else default


def kv_set(k, v):
    db.execute("INSERT OR REPLACE INTO kv (k, v) VALUES (?, ?)", (k, str(v)))
    db.commit()


# ---------------------------------------------------------------- VK API
class VKError(Exception):
    def __init__(self, code, msg):
        self.code = code
        super().__init__(f"VK error {code}: {msg}")


_http: aiohttp.ClientSession | None = None


async def http() -> aiohttp.ClientSession:
    global _http
    if _http is None or _http.closed:
        _http = aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=40))
    return _http


def token_for(method: str) -> str:
    if VK_USER_TOKEN and method.split(".")[0] in ("wall", "stats", "board", "photos"):
        return VK_USER_TOKEN
    return VK_COMMUNITY_TOKEN


async def vk(method: str, **params):
    tok = params.pop("_token", None) or token_for(method)
    params["access_token"] = tok
    params["v"] = VK_VER
    sess = await http()
    async with sess.post(VK_API + method, data=params) as r:
        data = await r.json()
    if "error" in data:
        raise VKError(data["error"].get("error_code"), data["error"].get("error_msg"))
    return data.get("response")


_names: dict[int, str] = {}


async def user_name(user_id: int) -> str:
    if user_id < 0:
        return f"сообщество id{-user_id}"
    if user_id not in _names:
        try:
            u = (await vk("users.get", user_ids=user_id))[0]
            _names[user_id] = f"{u['first_name']} {u['last_name']}"
        except Exception:
            _names[user_id] = f"id{user_id}"
    return _names[user_id]


async def vk_send(user_id: int, text: str, kind: str = "",
                  keyboard: str | None = None) -> str:
    """Отправить сообщение жителю от имени сообщества. Возвращает пометку для карточки."""
    if not vk_writing_on():
        log.info("тихий режим: НЕ отправлено id%s [%s]", user_id, kind)
        return ("🔇 <b>не отправлено</b> — бот в тихом режиме, "
                "в группе ничего не появилось. Включить: /replies on")
    try:
        extra = {"keyboard": keyboard} if keyboard else {}
        await vk("messages.send", user_id=user_id,
                 random_id=int(time.time() * 1000) % 2_000_000_000,
                 message=text, _token=VK_COMMUNITY_TOKEN, **extra)
        db.execute("INSERT INTO sent_log (ts, user_id, kind) VALUES (?,?,?)",
                   (datetime.now(MSK).isoformat(timespec="seconds"), user_id, kind))
        db.commit()
        return "✉️ отправлено"
    except VKError as e:
        if e.code in (901, 902):
            return "✉️ не дошло: у человека закрыты сообщения от сообществ"
        return f"✉️ ошибка отправки: {e}"


# ---------------------------------------------------------------- даты и бронь
def fmt_day(d: date) -> str:
    return f"{d.day} {MONTHS[d.month - 1]}"


def booked_days() -> dict[str, tuple[str, int]]:
    return {r[0]: (r[1], r[2] or 0)
            for r in db.execute("SELECT day, client, amount FROM bookings")}


def free_dates(n: int = 3) -> str:
    """Ближайшие дни, где есть свободное время — из общего графика."""
    days = O.free_days(db, "post", limit=n)
    if not days:
        return "уточню и напишу"
    return ", ".join(fmt_day(date.fromisoformat(x)) for x in days)


def fill(text: str) -> str:
    """Подставить в текст ссылки, даты и реквизиты."""
    return text.format(
        dates=free_dates(), pay=PAY_DETAILS, link="",
        market=T.topic_url(T.T_MARKET), jobs=T.topic_url(T.T_JOBS),
        rent=T.topic_url(T.T_RENT), pets=T.PETS_CHAT_URL,
        lost=T.topic_url(T.T_LOST),
    ) if "{" in text else text


# ---------------------------------------------------------------- Telegram
tg_session = None
if TG_PROXY:
    try:
        from aiohttp_socks import ProxyConnector

        class Socks5RdnsSession(AiohttpSession):
            async def create_session(self) -> aiohttp.ClientSession:
                if self._session is None or self._session.closed:
                    self._session = aiohttp.ClientSession(
                        connector=ProxyConnector.from_url(TG_PROXY, rdns=True),
                        json_serialize=self.json_dumps)
                    self._should_reset_connector = False
                return self._session

        tg_session = Socks5RdnsSession()
    except ImportError:
        log.warning("aiohttp_socks не установлен — TG_PROXY игнорирую")

bot = Bot(TG_BOT_TOKEN, session=tg_session,
          default=DefaultBotProperties(parse_mode="HTML"))
dp = Dispatcher()


def admin_chat() -> int | None:
    v = os.getenv("TG_ADMIN_CHAT_ID", "").strip() or kv_get("admin_chat")
    return int(v) if v else None


async def tg_admin(text: str, **kw):
    chat = admin_chat()
    if chat:
        try:
            await bot.send_message(chat, text, **kw)
        except Exception:
            log.exception("не отправилось в телеграм")


def is_admin(ev) -> bool:
    chat = admin_chat()
    return chat is not None and ev.from_user and ev.from_user.id == chat


# ---------------------------------------------------------------- клавиатуры
def b(text: str, data: str) -> InlineKeyboardButton:
    return InlineKeyboardButton(text=text, callback_data=data)


def msg_kb(uid: int, menu: str = "root") -> InlineKeyboardMarkup:
    if menu == "ads":
        keys = ["price_short", "price_full", "price_exp", "price_dates", "pay", "ask_goal"]
        rows = [[b(T.REPLIES[k][0], f"m:{k}:{uid}")] for k in keys]
        rows.append([b("← назад", f"mn:root:{uid}")])
    elif menu == "news":
        keys = ["news_how", "news_details", "news_thanks", "news_anon", "news_rumors"]
        rows = [[b(T.REPLIES[k][0], f"m:{k}:{uid}")] for k in keys]
        rows.append([b("← назад", f"mn:root:{uid}")])
    else:
        rows = [
            [b("💰 Реклама и прайс", f"mn:ads:{uid}"),
             b("📰 Про новости", f"mn:news:{uid}")],
            [b(T.REPLIES["pets"][0], f"m:pets:{uid}"),
             b(T.REPLIES["ads_free"][0], f"m:ads_free:{uid}")],
            [b("✍️ Ответить своими словами", f"mw:{uid}")],
            [InlineKeyboardButton(text="🔗 Диалог в ВК",
                                  url=f"https://vk.com/gim{GROUP_ID}?sel={uid}"),
             b("✅ Обработано", f"md:{uid}")],
        ]
    return InlineKeyboardMarkup(inline_keyboard=rows)


def sug_kb(pid: int, aid: int, menu: str = "root",
           rec: str | None = None) -> InlineKeyboardMarkup:
    if menu == "move":
        rows = [[b(T.MOVE_TARGETS[k][2], f"s:mv_{k}:{pid}:{aid}")]
                for k in ("market", "jobs", "rent", "lost")]
        rows.append([b("← назад", f"sn:root:{pid}:{aid}")])
    elif menu == "book":
        rows = []
        for r in db.execute(
                "SELECT id, date, slot, name FROM ad_orders WHERE status='confirmed' "
                "AND kind='slot' ORDER BY date").fetchall():
            rows.append([b(f"№{r[0]} · {O.fmt_day_ru(r[1])} {O.slot_title(r[2])} "
                           f"· {r[3]}"[:60], f"s:bk_{r[0]}:{pid}:{aid}")])
        if not rows:
            rows = [[b("нет оплаченных заявок", f"sn:root:{pid}:{aid}")]]
        rows.append([b("← назад", f"sn:root:{pid}:{aid}")])
    elif menu == "dec":
        rows = [[b("🙅 Не наша тема", f"s:dec_offtopic:{pid}:{aid}")],
                [b("❔ Нет подтверждения", f"s:dec_proof:{pid}:{aid}")],
                [b("♻️ Уже публиковали", f"s:dec_dup:{pid}:{aid}")],
                [b("← назад", f"sn:root:{pid}:{aid}")]]
    else:
        rows = []
        if rec and rec in T.MOVE_TARGETS:
            rows.append([b(f"⭐ {T.MOVE_TARGETS[rec][2]} + ответ автору — рекомендую",
                           f"s:mv_{rec}:{pid}:{aid}")])
        elif rec == "commercial":
            rows.append([b("⭐ Коммерция: прислать прайс — рекомендую",
                           f"s:sug_comm:{pid}:{aid}")])
        rows += [
            [b("✅ Опубликовать", f"s:pub:{pid}:{aid}"),
             b(f"🕖 В {EVENING_HOUR}:00", f"s:sch:{pid}:{aid}")],
        ]
        if rec != "pets":
            rows.append([b("🐾 В чат потеряшек + ответ автору", f"s:mv_pets:{pid}:{aid}")])
        rows += [
            [b("📂 В другую тему", f"sn:move:{pid}:{aid}"),
             b("💰 Коммерция", f"s:sug_comm:{pid}:{aid}")],
            [b("📅 По брони", f"sn:book:{pid}:{aid}")],
            [b("📷 Нужны детали", f"s:sug_details:{pid}:{aid}"),
             b("✍️ Свой ответ", f"mw:{aid}")],
            [b("❌ Отклонить", f"s:del:{pid}:{aid}"),
             b("❌✉️ С объяснением", f"sn:dec:{pid}:{aid}")],
        ]
    return InlineKeyboardMarkup(inline_keyboard=rows)


async def set_kb(cb: CallbackQuery, kb: InlineKeyboardMarkup | None):
    try:
        await cb.message.edit_reply_markup(reply_markup=kb)
    except Exception:
        pass


async def note(cb: CallbackQuery, text: str, keep_kb: bool = True):
    """Дописать строку-итог под карточку."""
    try:
        if cb.message.photo:
            base = cb.message.caption or ""
            await cb.message.edit_caption(
                caption=f"{base}\n\n{text}",
                reply_markup=cb.message.reply_markup if keep_kb else None)
        else:
            base = cb.message.html_text or cb.message.text or ""
            await cb.message.edit_text(
                f"{base}\n\n{text}",
                reply_markup=cb.message.reply_markup if keep_kb else None,
                disable_web_page_preview=True)
    except Exception:
        await cb.message.answer(text)


# ---------------------------------------------------------------- карточки
def att_summary(attachments) -> str:
    names = {"photo": "фото", "video": "видео", "doc": "файл", "audio": "аудио",
             "poll": "опрос", "link": "ссылка", "wall": "репост", "market": "товар"}
    c: dict[str, int] = {}
    for a in attachments or []:
        t = names.get(a.get("type"), a.get("type", "вложение"))
        c[t] = c.get(t, 0) + 1
    return ", ".join(f"{t}×{n}" if n > 1 else t for t, n in c.items())


def best_photo(attachments) -> str | None:
    for a in attachments or []:
        if a.get("type") == "photo":
            sizes = a["photo"].get("sizes") or []
            if sizes:
                return max(sizes, key=lambda s: s.get("width", 0)).get("url")
    return None


def att_ids(attachments) -> list[str]:
    out = []
    for a in attachments or []:
        t = a.get("type")
        if t not in ("photo", "video", "doc", "audio"):
            continue
        o = a[t]
        s = f"{t}{o['owner_id']}_{o['id']}"
        if o.get("access_key"):
            s += f"_{o['access_key']}"
        out.append(s)
    return out


async def reupload_photos(attachments) -> list[str]:
    """Перезалить чужие фото на сервер группы, если ВК не даёт прикрепить чужие."""
    out, sess = [], await http()
    for a in attachments or []:
        if a.get("type") != "photo":
            continue
        sizes = a["photo"].get("sizes") or []
        if not sizes:
            continue
        try:
            async with sess.get(max(sizes, key=lambda s: s.get("width", 0))["url"]) as r:
                blob = await r.read()
            up = await vk("photos.getWallUploadServer", group_id=GROUP_ID)
            form = aiohttp.FormData()
            form.add_field("photo", blob, filename="photo.jpg", content_type="image/jpeg")
            async with sess.post(up["upload_url"], data=form) as r:
                res = await r.json(content_type=None)
            saved = await vk("photos.saveWallPhoto", group_id=GROUP_ID,
                             photo=res["photo"], server=res["server"], hash=res["hash"])
            out.append(f"photo{saved[0]['owner_id']}_{saved[0]['id']}")
        except Exception:
            log.exception("не перезалилось фото")
    return out


async def send_suggest_card(item: dict):
    aid = item.get("from_id", 0)
    name = await user_name(aid)
    when = datetime.fromtimestamp(item.get("date", time.time()), MSK).strftime("%d.%m %H:%M")
    text = (item.get("text") or "").strip()
    att = att_summary(item.get("attachments"))
    kv_set(f"post:{item['id']}", json.dumps(
        {"text": text, "attachments": item.get("attachments") or [], "from_id": aid},
        ensure_ascii=False))
    verdict = C.classify(text, item.get("attachments"))
    kind, conf = verdict["kind"], verdict["confidence"]
    rec = kind if (kind != "news" and conf >= 0.45) else None

    # полный автомат: сам переносим в тему и пишем автору
    if (auto_on() and vk_writing_on() and kind in T.MOVE_TARGETS
            and conf >= AUTO_MIN_CONFIDENCE):
        try:
            res = await move_to_topic(item["id"], aid, kind)
            undo = InlineKeyboardMarkup(inline_keyboard=[[
                b("↩️ Вернуть: убрать из ветки", f"s:undo:{item['id']}:{aid}")]])
            await tg_admin(f"🤖 <b>Само:</b> похоже на «{C.RU[kind]}» "
                           f"({int(conf * 100)}%) — перенёс, автору написал.\n\n"
                           f"<i>{html.escape(text[:180])}</i>\n\n{res}",
                           reply_markup=undo, disable_web_page_preview=True)
            return
        except Exception:
            log.exception("автоперенос не удался — показываю карточку")

    head = (f"📥 <b>Предложка</b> от <a href=\"https://vk.com/id{aid}\">"
            f"{html.escape(name)}</a> · {when}")
    if att:
        head += f"\n📎 {att}"
    if rec:
        head += (f"\n🤖 похоже на «{C.RU[kind]}» — {int(conf * 100)}%"
                 + (f" ({', '.join(verdict['hits'][:3])})" if verdict["hits"] else ""))
    body = html.escape(text)[:3200] if text else "<i>(без текста)</i>"
    card = f"{head}\n\n{body}\n\n<code>#u{aid}</code>"
    kb = sug_kb(item["id"], aid, rec=rec)
    chat = admin_chat()
    if not chat:
        return
    photo = best_photo(item.get("attachments"))
    try:
        if photo and len(card) <= 1000:
            await bot.send_photo(chat, photo, caption=card, reply_markup=kb)
        else:
            await bot.send_message(chat, card, reply_markup=kb,
                                   disable_web_page_preview=True)
    except Exception:
        log.exception("карточка предложки не ушла")


# ---------------------------------------------------------------- заказ в переписке ВК
def order_card(o: dict, extra: str = "") -> str:
    when = f"{O.fmt_day_ru(o['date'])}"
    if o["kind"] == "slot" and o["slot"]:
        when += f", {O.slot_title(o['slot'])}"
    elif o["kind"] == "pin":
        when += f" — {O.fmt_day_ru(o['date_to'])}"
    lines = [f"🧾 <b>Заявка №{o['id']}</b>{extra}",
             f"{html.escape(o['title'])} · {when}",
             f"💵 {o['price']} ₽",
             f"👤 <a href=\"https://vk.com/id{o['user_id']}\">{html.escape(o['name'])}</a>",
             f"<code>#u{o['user_id']}</code>"]
    return "\n".join(lines)


def order_kb(oid: int, uid: int) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [b("✅ Оплата пришла", f"o:ok:{oid}"), b("❌ Отменить", f"o:no:{oid}")],
        [b("✍️ Написать клиенту", f"mw:{uid}")]])


async def make_order(uid: int, fid: str, ds: str, sid: str) -> bool:
    """Создаёт бронь и присылает человеку реквизиты."""
    if sid and sid in O.busy_slots(db, ds):
        await vk_send(uid, "Это время только что заняли. Выберите другое:",
                      kind="order_taken", keyboard=O.kb_slots(db, fid, ds))
        return True
    name = await user_name(uid)
    oid = O.create_order(db, user_id=uid, name=name, contact=f"vk.com/id{uid}",
                         fmt=fid, day=ds, slot_id=sid)
    o = O.get_order(db, oid)
    when = O.fmt_day_ru(ds) + (f", {O.slot_title(sid)}" if sid else
                               f" — {O.fmt_day_ru(o['date_to'])}")
    O.set_state(db, uid, "pay", {"order": oid})
    await vk_send(
        uid,
        f"Забронировал: {o['title']}, {when}.\nК оплате — {o['price']} ₽.\n\n"
        f"{PAY_DETAILS}\n\n"
        "После оплаты пришлите сюда скриншот перевода — именно скриншот или фото, "
        "не файл-документ. Как подтвердим, попрошу у вас текст объявления и картинки.\n\n"
        f"Место держим за вами {O.HOLD_HOURS} часа.",
        kind="order_hold", keyboard=O.kb_pay())
    await tg_admin(order_card(o, " — новая бронь, ждём оплату"),
                   reply_markup=order_kb(oid, uid), disable_web_page_preview=True)
    return True


@dp.callback_query(F.data.startswith("o:"))
async def on_order_action(cb: CallbackQuery):
    if not is_admin(cb):
        await cb.answer("не твой пульт", show_alert=True)
        return
    _, act, oid = cb.data.split(":")
    o = O.get_order(db, int(oid))
    if not o:
        await cb.answer("заявка не найдена", show_alert=True)
        return
    if act == "ok":
        O.set_status(db, o["id"], "confirmed")
        res = await vk_send(o["user_id"],
                            "Оплату получили, спасибо! Место за вами закреплено.\n\n"
                            "Пришлите, пожалуйста, текст объявления и картинки — "
                            "поставим в очередь на выбранное время. После выхода "
                            "пришлю статистику.",
                            kind="order_confirm")
        await note(cb, f"✅ Оплата подтверждена · {res}\n"
                       "📅 Когда пришлёт текст — на карточке поста жми «📅 По брони».",
                   keep_kb=False)
    else:
        O.set_status(db, o["id"], "cancel")
        O.set_state(db, o["user_id"], "idle", {})
        res = await vk_send(o["user_id"],
                            "Бронь отменили — оплата не поступила. Если ещё актуально, "
                            "напишите «реклама», и подберём новую дату.",
                            kind="order_cancel_admin")
        await note(cb, f"❌ Бронь снята, дата свободна · {res}", keep_kb=False)
    await cb.answer("Готово")


@dp.message(Command("grafik"))
async def on_grafik(m: Message):
    if not is_admin(m):
        return
    await m.answer(O.schedule_text(db))


async def send_message_card(msg: dict):
    uid = msg.get("from_id", 0)
    if uid <= 0:
        return
    name = await user_name(uid)
    text = (msg.get("text") or "").strip()
    att = att_summary(msg.get("attachments"))
    body = html.escape(text)[:3000] if text else "<i>(без текста)</i>"
    if att:
        body += f"\n📎 {att}"
    menu = "ads" if any(w in text.lower() for w in AD_WORDS) else "root"
    hint = "\n\n<i>Похоже на вопрос о рекламе — ответы ниже. Можно просто " \
           "ответить на это сообщение своим текстом.</i>" if menu == "ads" else \
           "\n\n<i>Ответить своими словами: свайп-ответ на это сообщение.</i>"
    card = (f"💬 <b>Сообщение группе</b> от <a href=\"https://vk.com/id{uid}\">"
            f"{html.escape(name)}</a>\n\n{body}{hint}\n\n<code>#u{uid}</code>")
    await tg_admin(card, reply_markup=msg_kb(uid, menu), disable_web_page_preview=True)


# ---------------------------------------------------------------- команды
@dp.message(CommandStart())
async def on_start(m: Message):
    saved = admin_chat()
    if saved is None:
        kv_set("admin_chat", m.chat.id)
        await m.answer(
            mode_line() + "\n\n"
            "🎛 <b>Пульт «Подслушано МЭЗ \\ Малаховка» подключён.</b>\n\n"
            "Сюда приходят предложка и сообщения группы — с кнопками ответов.\n"
            "Чтобы ответить своими словами, сделай свайп-ответ на карточку.\n\n"
            "Команды:\n"
            "/check — что работает\n"
            "/suggests — перечитать предложку\n"
            "/slots — календарь рекламы\n"
            "/book 03.09 Кафе Прага 1500 — забронировать дату\n"
            "/unbook 03.09 — снять бронь\n"
            "/income — доход за месяц\n"
            "/report — отчёт статистики")
    elif m.chat.id == saved:
        await m.answer("Пульт уже подключён. /check — самопроверка.")
    else:
        await m.answer("Этот пульт приватный и привязан к другому чату.")


@dp.message(Command("check"))
async def on_check(m: Message):
    if not is_admin(m):
        return
    lines = ["🔎 <b>Самопроверка</b>", mode_line(), ""]
    try:
        g = await vk("groups.getById", group_id=GROUP_ID, fields="members_count",
                     _token=VK_COMMUNITY_TOKEN)
        g0 = g["groups"][0] if isinstance(g, dict) else g[0]
        lines.append(f"✅ группа: «{g0['name']}», {g0.get('members_count','?')} подписчиков")
    except Exception as e:
        lines.append(f"❌ ключ сообщества: {e}")
    try:
        s = await vk("wall.get", owner_id=-GROUP_ID, filter="suggests", count=1)
        lines.append(f"✅ предложка читается — сейчас {s.get('count', 0)} шт.")
    except VKError as e:
        lines.append(f"❌ предложка: {e}")
    try:
        t = await vk("board.getTopics", group_id=GROUP_ID, count=1)
        lines.append(f"✅ обсуждения доступны ({t.get('count', 0)} тем)")
    except VKError as e:
        lines.append(f"❌ обсуждения: {e}")
    try:
        await vk("stats.get", group_id=GROUP_ID, interval="day",
                 timestamp_from=int(time.time()) - 86400, timestamp_to=int(time.time()))
        lines.append("✅ статистика доступна")
    except VKError as e:
        lines.append(f"⚠️ статистика ВК недоступна ({e}) — отчёт будет без охватов")
    lines.append(f"📅 свободные даты: {free_dates()}")
    await m.answer("\n".join(lines))


def auto_on() -> bool:
    return kv_get("auto_move", "0") == "1"


def mode_line() -> str:
    """Одной строкой: пишет бот в группу или молчит."""
    if not vk_writing_on():
        return ("🔇 <b>ТИХИЙ РЕЖИМ</b> — бот не пишет в группу ничего. "
                "Кнопки ответов показывают текст, но не отправляют его.")
    parts = ["🔊 <b>Отправка включена</b> — кнопки реально пишут людям",
             "💬 сам бот никому не отвечает: только по вашей кнопке",
             "🤖 автоперенос предложки: " + ("включён" if auto_on() else "выключен")]
    return "\n".join(parts)


@dp.message(Command("replies"))
async def on_replies(m: Message):
    if not is_admin(m):
        return
    arg = (m.text or "").split()[-1].lower()
    if arg in ("on", "вкл", "1"):
        kv_set("vk_send", "1")
        await m.answer("🔊 <b>Отправка включена.</b> Теперь кнопки ответов реально "
                       "пишут людям от имени сообщества.\n\nВыключить: /replies off")
    elif arg in ("off", "выкл", "0"):
        kv_set("vk_send", "0")
        await m.answer("🔇 <b>Тихий режим.</b> Бот больше ничего не пишет в группу — "
                       "ни ответов, ни автопереносов. Всё приходит только сюда.")
    else:
        await m.answer(mode_line() + "\n\n/replies on — разрешить писать\n"
                                     "/replies off — тихий режим")


@dp.message(Command("auto"))
async def on_auto(m: Message):
    if not is_admin(m):
        return
    arg = (m.text or "").split()[-1].lower()
    if arg in ("on", "вкл", "1"):
        kv_set("auto_move", "1")
        await m.answer(
            "🤖 <b>Автомат включён.</b>\n\n"
            "Объявления про животных, вещи, вакансии, аренду и продажу бот теперь "
            "переносит в нужную ветку сам и сам пишет автору — если уверен минимум "
            f"на {int(AUTO_MIN_CONFIDENCE * 100)}%.\n\n"
            "Ты получишь уведомление с кнопкой «Вернуть», если что-то не так. "
            "Всё сомнительное по-прежнему придёт карточкой на твоё решение.\n\n"
            "Выключить: /auto off")
    elif arg in ("off", "выкл", "0"):
        kv_set("auto_move", "0")
        await m.answer("🤖 Автомат выключен. Всё приходит карточками, решаешь ты.")
    else:
        state = "включён" if auto_on() else "выключен"
        await m.answer(f"🤖 Автоперенос сейчас <b>{state}</b>.\n"
                       f"Порог уверенности: {int(AUTO_MIN_CONFIDENCE * 100)}%.\n\n"
                       "/auto on — включить, /auto off — выключить")


@dp.message(Command("suggests"))
async def on_suggests(m: Message):
    if not is_admin(m):
        return
    n = await poll_suggests(force_all=True)
    await m.answer(f"Прислал карточек: {n}" if n else "Предложка пуста 🎉")


@dp.message(Command("report"))
async def on_report(m: Message):
    if not is_admin(m):
        return
    await send_weekly_report()


@dp.message(Command("slots"))
@dp.message(Command("grafik"))
async def on_slots(m: Message):
    if not is_admin(m):
        return
    await m.answer(O.schedule_text(db) +
                   "\n\nЗабронировать: /book 03.09 12-14 Кафе Прага 1500"
                   "\nСнять: /unbook 12")


@dp.message(Command("book"))
async def on_book(m: Message):
    """Ручная бронь: /book 03.09 [время] Клиент [сумма]."""
    if not is_admin(m):
        return
    parts = (m.text or "").split()[1:]
    if len(parts) < 2:
        await m.answer("Формат: <code>/book 03.09 12-14 Кафе Прага 1500</code>\n"
                       "Время можно не писать — поставлю 12-14.\n"
                       "Окна: 08-11, 12-14, 15-17, 18-20")
        return
    try:
        dd, mm = parts[0].split(".")[:2]
        y = date.today().year
        day = date(y, int(mm), int(dd))
        if day < date.today():
            day = date(y + 1, int(mm), int(dd))
    except Exception:
        await m.answer("Дату пиши как 03.09")
        return

    tail = parts[1:]
    slot = "12-14"
    if tail and tail[0] in [s[0] for s in O.SLOTS]:
        slot = tail[0]
        tail = tail[1:]
    amount = None
    if tail and tail[-1].isdigit():
        amount = int(tail[-1])
        tail = tail[:-1]
    client = " ".join(tail) or "клиент"

    if slot in O.busy_slots(db, day.isoformat()):
        await m.answer(f"⚠️ {fmt_day(day)} в {O.slot_title(slot)} уже занято. "
                       f"Свободные дни: {free_dates()}")
        return

    oid = O.create_order(db, user_id=0, name=client, contact="", fmt="post",
                         day=day.isoformat(), slot_id=slot, source="manual")
    if amount is not None:
        db.execute("UPDATE ad_orders SET price=? WHERE id=?", (amount, oid))
        db.commit()
    O.set_status(db, oid, "confirmed")
    o = O.get_order(db, oid)
    await m.answer(f"✅ Заявка №{oid}: {fmt_day(day)}, {O.slot_title(slot)} — "
                   f"{client}, {o['price']} ₽\n"
                   f"Ближайшие свободные дни: {free_dates()}\n\n"
                   "Когда клиент пришлёт текст в предложку — на карточке жми "
                   "«📅 По брони» и выбери эту заявку.")


@dp.message(Command("unbook"))
async def on_unbook(m: Message):
    """Снять бронь: /unbook 12 (номер заявки из /grafik)."""
    if not is_admin(m):
        return
    parts = (m.text or "").split()[1:]
    if not parts or not parts[0].isdigit():
        await m.answer("Формат: <code>/unbook 12</code> — номер заявки из /grafik")
        return
    oid = int(parts[0])
    o = O.get_order(db, oid)
    if not o:
        await m.answer("Заявки с таким номером нет.")
        return
    O.set_status(db, oid, "cancel")
    await m.answer(f"Заявка №{oid} снята: {O.fmt_day_ru(o['date'])} "
                   f"{O.slot_title(o['slot'] or '')} — {o['name']}. Время снова свободно.")



@dp.message(Command("income"))
async def on_income(m: Message):
    if not is_admin(m):
        return
    ym = date.today().strftime("%Y-%m")
    rows = db.execute("SELECT day, client, amount FROM bookings WHERE day LIKE ? "
                      "ORDER BY day", (ym + "%",)).fetchall()
    if not rows:
        await m.answer("В этом месяце броней пока нет.")
        return
    total = sum(r[2] or 0 for r in rows)
    lines = [f"💰 <b>Доход за {MONTHS[date.today().month - 1]}</b>"]
    lines += [f"{r[0][8:10]}.{r[0][5:7]} — {r[1]}: {r[2] or 0} ₽" for r in rows]
    lines.append(f"\n<b>Итого: {total} ₽</b> за {len(rows)} размещений")
    await m.answer("\n".join(lines))


# ---------------------------------------------------------------- ответ своими словами
MARK = re.compile(r"#u(\d+)")


@dp.message(F.reply_to_message)
async def on_custom_reply(m: Message):
    if not is_admin(m):
        return
    src = m.reply_to_message
    found = MARK.search((src.text or "") + " " + (src.caption or ""))
    if not found:
        await m.answer("Не понял, кому отправить: отвечай на карточку сообщения "
                       "или предложки.")
        return
    uid = int(found.group(1))
    if not (m.text or "").strip():
        await m.answer("Пока умею отправлять только текст.")
        return
    res = await vk_send(uid, m.text.strip(), kind="custom")
    name = await user_name(uid)
    await m.answer(f"{res} → {html.escape(name)}")


# ---------------------------------------------------------------- кнопки: сообщения
@dp.callback_query(F.data.startswith("mn:"))
async def on_menu(cb: CallbackQuery):
    if not is_admin(cb):
        await cb.answer("не твой пульт", show_alert=True)
        return
    _, menu, uid = cb.data.split(":")
    await set_kb(cb, msg_kb(int(uid), menu))
    await cb.answer()


@dp.callback_query(F.data.startswith("md:"))
async def on_done(cb: CallbackQuery):
    if not is_admin(cb):
        return
    await set_kb(cb, None)
    await note(cb, "✅ Обработано", keep_kb=False)
    await cb.answer("Готово")


@dp.callback_query(F.data.startswith("mw:"))
async def on_write(cb: CallbackQuery):
    if not is_admin(cb):
        return
    uid = int(cb.data.split(":")[1])
    name = await user_name(uid)
    await cb.message.answer(
        f"✍️ Напиши ответ для <b>{html.escape(name)}</b> — отправлю от имени "
        f"сообщества.\n\n<code>#u{uid}</code>",
        reply_markup=ForceReply(input_field_placeholder="Текст ответа..."))
    await cb.answer()


@dp.callback_query(F.data.startswith("m:"))
async def on_reply_button(cb: CallbackQuery):
    if not is_admin(cb):
        await cb.answer("не твой пульт", show_alert=True)
        return
    _, key, uid = cb.data.split(":")
    uid = int(uid)
    label, template = T.REPLIES[key]
    text = template
    if key == "pets":
        text = template.format(link=T.PETS_CHAT_URL)
    else:
        text = fill(template)
    await cb.answer("Отправляю…")
    res = await vk_send(uid, text, kind=key)
    await note(cb, f"{res} · <i>{html.escape(label)}</i>")


# ---------------------------------------------------------------- кнопки: предложка
@dp.callback_query(F.data.startswith("sn:"))
async def on_sug_menu(cb: CallbackQuery):
    if not is_admin(cb):
        return
    _, menu, pid, aid = cb.data.split(":")
    await set_kb(cb, sug_kb(int(pid), int(aid), menu))
    await cb.answer()


async def reupload_photos_for_message(attachments) -> list[str]:
    """Перезалить фото так, чтобы их можно было отправить сообщением в чат."""
    out, sess = [], await http()
    for a in attachments or []:
        if a.get("type") != "photo":
            continue
        sizes = a["photo"].get("sizes") or []
        if not sizes:
            continue
        try:
            async with sess.get(max(sizes, key=lambda s: s.get("width", 0))["url"]) as r:
                blob = await r.read()
            up = await vk("photos.getMessagesUploadServer", peer_id=T.PETS_CHAT_PEER,
                          _token=VK_COMMUNITY_TOKEN)
            form = aiohttp.FormData()
            form.add_field("photo", blob, filename="photo.jpg", content_type="image/jpeg")
            async with sess.post(up["upload_url"], data=form) as r:
                res = await r.json(content_type=None)
            saved = await vk("photos.saveMessagesPhoto", photo=res["photo"],
                             server=res["server"], hash=res["hash"],
                             _token=VK_COMMUNITY_TOKEN)
            out.append(f"photo{saved[0]['owner_id']}_{saved[0]['id']}")
        except Exception:
            log.exception("не перезалилось фото для чата")
    return out


async def move_to_pets_chat(pid: int, aid: int) -> str:
    """Объявление о животных уходит в чат сообщества к волонтёрам."""
    raw = kv_get(f"post:{pid}")
    cached = json.loads(raw) if raw else {}
    if not cached:
        resp = await vk("wall.get", owner_id=-GROUP_ID, filter="suggests", count=100)
        for it in resp.get("items", []):
            if it["id"] == pid:
                cached = {"text": it.get("text") or "",
                          "attachments": it.get("attachments") or [],
                          "from_id": it.get("from_id", aid)}
                break
    text = (cached.get("text") or "").strip()
    attachments = cached.get("attachments") or []
    name = await user_name(aid)
    body = ("🐾 Объявление из сообщества\n\n" + (text or "(без текста)") +
            f"\n\nАвтор: [id{aid}|{name}] — пишите ему напрямую.")

    photos = await reupload_photos_for_message(attachments)
    out = []
    try:
        await vk("messages.send", peer_id=T.PETS_CHAT_PEER,
                 random_id=int(time.time() * 1000) % 2_000_000_000,
                 message=body, attachment=",".join(photos),
                 _token=VK_COMMUNITY_TOKEN)
        out.append("🐾 Опубликовано в чате потеряшек")
        if attachments and not photos:
            out[-1] += " (фото не перенеслись)"
    except VKError as e:
        return f"⚠️ Не смог написать в чат: {e}"

    try:
        await vk("wall.delete", owner_id=-GROUP_ID, post_id=pid)
        out.append("убрано из предложки")
    except VKError as e:
        out.append(f"из предложки не удалилось ({e})")

    res = await vk_send(aid, T.SUG_PETS.format(link=T.PETS_CHAT_URL), kind="move_pets")
    db.execute("DELETE FROM kv WHERE k=?", (f"post:{pid}",))
    db.commit()
    return ", ".join(out) + chr(10) + res + chr(10) + T.PETS_CHAT_URL


async def move_to_topic(pid: int, aid: int, key: str) -> str:
    if key == "pets":
        return await move_to_pets_chat(pid, aid)
    topic_id, reply_text, label = T.MOVE_TARGETS[key]
    raw = kv_get(f"post:{pid}")
    cached = json.loads(raw) if raw else {}
    if not cached:
        resp = await vk("wall.get", owner_id=-GROUP_ID, filter="suggests", count=100)
        for it in resp.get("items", []):
            if it["id"] == pid:
                cached = {"text": it.get("text") or "",
                          "attachments": it.get("attachments") or [],
                          "from_id": it.get("from_id", aid)}
                break
    text = (cached.get("text") or "").strip()
    attachments = cached.get("attachments") or []
    name = await user_name(aid)
    body = (text or "(без текста)") + f"\n\nАвтор объявления: [id{aid}|{name}]"

    async def create(att):
        return await vk("board.createComment", group_id=GROUP_ID, topic_id=topic_id,
                        message=body, attachments=att, from_group=1)

    ids = att_ids(attachments)
    try:
        comment_id = await create(",".join(ids) if ids else "")
        lost_att = ""
    except VKError:
        reup = await reupload_photos(attachments)
        comment_id = await create(",".join(reup) if reup else "")
        lost_att = "" if reup or not attachments else " (вложения не перенеслись)"

    kv_set(f"undo:{pid}", json.dumps({"topic": topic_id, "comment": comment_id,
                                      "text": text[:500], "aid": aid},
                                     ensure_ascii=False))
    link = f"{T.topic_url(topic_id)}?post={comment_id}"
    out = [f"{label}: опубликовано{lost_att}"]
    try:
        await vk("wall.delete", owner_id=-GROUP_ID, post_id=pid)
        out.append("убрано из предложки")
    except VKError as e:
        out.append(f"из предложки не удалилось ({e})")
    res = await vk_send(aid, reply_text.format(link=link), kind=f"move_{key}")
    db.execute("DELETE FROM kv WHERE k=?", (f"post:{pid}",))
    db.commit()
    return "🐾 " + ", ".join(out) + f"\n{res}\n{link}"


@dp.callback_query(F.data.startswith("s:"))
async def on_sug_action(cb: CallbackQuery):
    if not is_admin(cb):
        await cb.answer("не твой пульт", show_alert=True)
        return
    _, act, pid, aid = cb.data.split(":")
    pid, aid = int(pid), int(aid)
    try:
        if act.startswith("mv_"):
            await cb.answer("Переношу…")
            try:
                res = await move_to_topic(pid, aid, act[3:])
            except Exception as e:
                log.exception("перенос упал")
                res = f"⚠️ Не смог перенести: {e}"
            await note(cb, res, keep_kb=False)
            return

        if act == "undo":
            raw = kv_get(f"undo:{pid}")
            if not raw:
                await cb.answer("нечего отменять", show_alert=True)
                return
            u = json.loads(raw)
            try:
                await vk("board.deleteComment", group_id=GROUP_ID,
                         topic_id=u["topic"], comment_id=u["comment"])
                msg = "↩️ Убрал из ветки. Текст поста сохранил ниже — из предложки он уже удалён."
            except VKError as e:
                msg = f"↩️ Не смог удалить комментарий ({e})"
            db.execute("DELETE FROM kv WHERE k=?", (f"undo:{pid}",))
            db.commit()
            await note(cb, msg + "\n\n<code>" + html.escape(u.get("text", "")) + "</code>",
                       keep_kb=False)
            await cb.answer("Готово")
            return
        if act.startswith("bk_"):
            bid = int(act[3:])
            bk = O.get_order(db, bid)
            if not bk:
                await cb.answer("заявка не найдена", show_alert=True)
                return
            hh, mm = O.slot_hour(bk.get("slot") or "").split(":")
            when = datetime.fromisoformat(bk["date"]).replace(
                hour=int(hh), minute=int(mm), tzinfo=MSK)
            if when <= datetime.now(MSK):
                when = datetime.now(MSK) + timedelta(minutes=5)
            await vk("wall.post", owner_id=-GROUP_ID, post_id=pid, from_group=1,
                     publish_date=int(when.timestamp()))
            O.set_status(db, bid, "posted")
            db.execute("UPDATE ad_orders SET post_id=? WHERE id=?", (pid, bid))
            db.commit()
            msg = f"📅 Поставлено по заявке №{bid} на {when.strftime('%d.%m %H:%M')}"
            if bk.get("user_id"):
                res = await vk_send(int(bk["user_id"]),
                                    "Ваше объявление поставлено в очередь и выйдет "
                                    f"{O.fmt_day_ru(bk['date'])} в "
                                    f"{O.slot_hour(bk.get('slot') or '')}. "
                                    "После выхода пришлю статистику.", kind="scheduled")
                msg += chr(10) + res
            await note(cb, msg, keep_kb=False)
            return
        if act == "pub":
            await vk("wall.post", owner_id=-GROUP_ID, post_id=pid, from_group=1)
            await note(cb, "✅ Опубликовано в ленте", keep_kb=False)
        elif act == "sch":
            now = datetime.now(MSK)
            tgt = now.replace(hour=EVENING_HOUR, minute=0, second=0, microsecond=0)
            if now >= tgt - timedelta(minutes=30):
                tgt += timedelta(days=1)
            await vk("wall.post", owner_id=-GROUP_ID, post_id=pid, from_group=1,
                     publish_date=int(tgt.timestamp()))
            await note(cb, f"🕖 В отложке на {tgt.strftime('%d.%m %H:%M')}", keep_kb=False)
        elif act == "del":
            await vk("wall.delete", owner_id=-GROUP_ID, post_id=pid)
            await note(cb, "❌ Отклонено (без письма)", keep_kb=False)
        elif act == "sug_comm":
            await cb.answer("Пишу автору…")
            res = await vk_send(aid, fill(T.SUG_COMMERCIAL), kind="commercial")
            await note(cb, f"💰 Отправлен прайс, пост остаётся в предложке · {res}")
            return
        elif act == "sug_details":
            await cb.answer("Пишу автору…")
            res = await vk_send(aid, T.SUG_NEED_DETAILS, kind="details")
            await note(cb, f"📷 Запросил детали, пост остаётся в предложке · {res}")
            return
        elif act.startswith("dec_"):
            await cb.answer("Отклоняю…")
            res = await vk_send(aid, T.SUGGEST_REPLIES[act], kind=act)
            try:
                await vk("wall.delete", owner_id=-GROUP_ID, post_id=pid)
                out = "❌ Отклонено и удалено из предложки"
            except VKError as e:
                out = f"❌ Письмо отправлено, но не удалилось ({e})"
            await note(cb, f"{out} · {res}", keep_kb=False)
            return
        await cb.answer("Готово")
    except VKError as e:
        await cb.answer(f"ВК ответил ошибкой: {e}", show_alert=True)
    except Exception:
        log.exception("действие по кнопке упало")
        await cb.answer("что-то сломалось, смотри логи", show_alert=True)


# ---------------------------------------------------------------- заявки с сайта
SITE_API = os.getenv("SITE_API", "").strip()
SITE_TOKEN = os.getenv("SITE_TOKEN", "").strip()
SLOT_START = {"08-11": "09:00", "12-14": "12:30", "15-17": "15:30", "18-20": "19:00"}
SLOT_TITLE = {"08-11": "8:00–11:00", "12-14": "12:00–14:00",
              "15-17": "15:00–17:00", "18-20": "18:00–20:00"}
MONTHS_GEN = ["января", "февраля", "марта", "апреля", "мая", "июня", "июля",
              "августа", "сентября", "октября", "ноября", "декабря"]
# что нужно сделать руками для форматов, которые не публикуются постом
MANUAL_HINT = {
    "menu": "Поменять кнопку в меню сообщества: Управление → Меню → загрузить картинку и ссылку.",
    "link": "Добавить ссылку: Управление → Ссылки → добавить.",
    "topic": "Отредактировать первое сообщение нужной темы обсуждений.",
    "rubric": "Добавить упоминание партнёра в шапку рубрики на месяц.",
    "digest": "Добавить строку спонсора в ежедневные сводки на весь период.",
    "expert": "Договориться с экспертом о графике и завести рубрику.",
    "promo": "Опубликовать пост про кодовое слово и добавить его в закреп.",
    "catalog": "Добавить карточку в каталог (раздел Товары).",
    "mailing": "Добавить строку спонсора в шаблон рассылки.",
    "pin_day": "Закрепить пост на сутки после публикации.",
    "pin_week": "Закрепить пост на неделю после публикации.",
    "pin_month": "Закрепить пост на месяц после публикации.",
}


def fmt_date(d: str) -> str:
    try:
        dd = date.fromisoformat(d)
        return f"{dd.day} {MONTHS_GEN[dd.month - 1]}"
    except Exception:
        return d


async def site_call(action: str, payload: dict | None = None):
    if not SITE_API:
        return None
    sess = await http()
    url = f"{SITE_API}?a={action}&t={SITE_TOKEN}"
    if payload is None:
        async with sess.get(url) as r:
            return await r.json(content_type=None)
    async with sess.post(url, json=payload) as r:
        return await r.json(content_type=None)


def booking_line(b: dict) -> str:
    when = ""
    if b.get("kind") == "slot" and b.get("date"):
        when = f"{fmt_date(b['date'])}, {SLOT_TITLE.get(b.get('slot',''), b.get('slot',''))}"
    elif b.get("kind") == "pin" and b.get("date"):
        when = f"с {fmt_date(b['date'])} по {fmt_date(b.get('to') or b['date'])}"
    elif b.get("kind") == "month" and b.get("month"):
        y, m = b["month"].split("-")
        when = f"{MONTHS_GEN[int(m) - 1].capitalize()} {y}"
    elif b.get("kind") == "digest":
        when = ("неделя" if b.get("period") == "week" else "месяц") + f" с {fmt_date(b.get('date',''))}"
    return when


def booking_kb(b: dict) -> InlineKeyboardMarkup:
    bid = b["id"]
    rows = [[b_btn("✅ Оплата пришла", f"b:ok:{bid}"),
             b_btn("❌ Отменить бронь", f"b:no:{bid}")]]
    if b.get("vk_user_id"):
        rows.append([b_btn("✍️ Написать клиенту", f"mw:{b['vk_user_id']}")])
    return InlineKeyboardMarkup(inline_keyboard=rows)


def b_btn(text: str, data: str) -> InlineKeyboardButton:
    return InlineKeyboardButton(text=text, callback_data=data)


async def send_booking_card(b: dict):
    status = "💰 нажал «я оплатил»" if b.get("status") == "paid" else "⏳ забронировал, ждём оплату"
    when = booking_line(b)
    parts = [f"🧾 <b>Заявка №{b['id']}</b> с сайта — {status}",
             f"<b>{html.escape(b.get('title',''))}</b>" + (f" · {when}" if when else ""),
             f"💵 {b.get('price', 0)} ₽"]
    who = html.escape(b.get("name", "")) + " · " + html.escape(b.get("contact", ""))
    parts.append(f"👤 {who}")
    if b.get("about"):
        parts.append(f"📝 {html.escape(b['about'])}")
    if b.get("vk_user_id"):
        parts.append(f"<a href=\"https://vk.com/id{b['vk_user_id']}\">профиль ВК</a>"
                     f"\n<code>#u{b['vk_user_id']}</code>")
    db.execute("INSERT OR REPLACE INTO site_bookings (id, data, status) VALUES (?,?,?)",
               (b["id"], json.dumps(b, ensure_ascii=False), b.get("status", "hold")))
    db.commit()
    await tg_admin("\n".join(parts), reply_markup=booking_kb(b), disable_web_page_preview=True)


@dp.callback_query(F.data.startswith("b:"))
async def on_booking(cb: CallbackQuery):
    if not is_admin(cb):
        await cb.answer("не твой пульт", show_alert=True)
        return
    _, act, bid = cb.data.split(":")
    bid = int(bid)
    row = db.execute("SELECT data FROM site_bookings WHERE id=?", (bid,)).fetchone()
    b = json.loads(row[0]) if row else {"id": bid, "kind": "", "format": ""}
    if act == "ok":
        await site_call("set", {"id": bid, "status": "confirmed"})
        db.execute("UPDATE site_bookings SET status='confirmed' WHERE id=?", (bid,))
        db.commit()
        note_text = "✅ Оплата подтверждена."
        if b.get("kind") == "slot":
            note_text += ("\n📅 Когда клиент пришлёт пост в предложку — жми на его карточке "
                          "«📅 По брони» и выбери эту заявку, поставлю на нужное время.")
        hint = MANUAL_HINT.get(b.get("format", ""))
        if hint:
            note_text += f"\n🛠 Сделать руками: {hint}"
        if b.get("vk_user_id"):
            res = await vk_send(int(b["vk_user_id"]),
                                "Оплату получили, спасибо! Заявка №%d принята. "
                                "Пришлите, пожалуйста, текст объявления и картинки — "
                                "поставим в очередь на выбранное время." % bid,
                                kind="booking_ok")
            note_text += f"\n{res}"
        await note(cb, note_text, keep_kb=False)
    else:
        await site_call("set", {"id": bid, "status": "cancel"})
        db.execute("UPDATE site_bookings SET status='cancel' WHERE id=?", (bid,))
        db.commit()
        await note(cb, "❌ Бронь снята, место снова свободно.", keep_kb=False)
    await cb.answer("Готово")


@dp.message(Command("bookings"))
async def on_bookings(m: Message):
    if not is_admin(m):
        return
    rows = db.execute("SELECT data FROM site_bookings WHERE status='confirmed' "
                      "ORDER BY id DESC LIMIT 20").fetchall()
    if not rows:
        await m.answer("Оплаченных заявок с сайта пока нет.")
        return
    out = ["💼 <b>Оплаченные заявки</b>"]
    for (raw,) in rows:
        b = json.loads(raw)
        out.append(f"№{b['id']} · {b.get('title','')} · {booking_line(b)} · {b.get('price',0)} ₽ "
                   f"· {b.get('name','')}")
    await m.answer("\n".join(out))


async def site_loop():
    while True:
        try:
            if SITE_API and admin_chat():
                items = await site_call("pending")
                for b in items or []:
                    await send_booking_card(b)
                    await asyncio.sleep(0.5)
        except Exception:
            log.exception("site_loop")
        await asyncio.sleep(30)


# ---------------------------------------------------------------- фоновые циклы
_warned = False


async def poll_suggests(force_all: bool = False) -> int:
    global _warned
    try:
        resp = await vk("wall.get", owner_id=-GROUP_ID, filter="suggests", count=25)
    except VKError as e:
        if not _warned:
            _warned = True
            await tg_admin(f"⚠️ Не читается предложка: {e}")
        return 0
    sent = 0
    for item in sorted(resp.get("items", []), key=lambda i: i.get("date", 0)):
        seen = db.execute("SELECT 1 FROM seen_suggests WHERE post_id=?",
                          (item["id"],)).fetchone()
        if force_all or not seen:
            db.execute("INSERT OR IGNORE INTO seen_suggests (post_id) VALUES (?)",
                       (item["id"],))
            db.commit()
            await send_suggest_card(item)
            sent += 1
            await asyncio.sleep(1)
    return sent


async def suggests_loop():
    while True:
        try:
            if admin_chat():
                await poll_suggests()
        except Exception:
            log.exception("suggests_loop")
        await asyncio.sleep(SUGGEST_POLL_SECONDS)


async def messages_loop():
    while True:
        try:
            srv = await vk("groups.getLongPollServer", group_id=GROUP_ID,
                           _token=VK_COMMUNITY_TOKEN)
            server, key, ts = srv["server"], srv["key"], srv["ts"]
            sess = await http()
            while True:
                async with sess.get(server, params={"act": "a_check", "key": key,
                                                    "ts": ts, "wait": 25},
                                    timeout=aiohttp.ClientTimeout(total=40)) as r:
                    data = await r.json()
                if "failed" in data:
                    if data["failed"] == 1:
                        ts = data["ts"]
                        continue
                    break
                ts = data["ts"]
                for upd in data.get("updates", []):
                    if upd.get("type") == "message_new":
                        # бот никогда не отвечает сам: только карточка в Telegram
                        await send_message_card(upd["object"]["message"])
        except Exception:
            log.exception("messages_loop")
            await asyncio.sleep(10)


async def send_weekly_report():
    parts = ["📊 <b>Неделя в «Подслушано МЭЗ \\ Малаховка»</b>"]
    try:
        g = await vk("groups.getById", group_id=GROUP_ID, fields="members_count",
                     _token=VK_COMMUNITY_TOKEN)
        g0 = g["groups"][0] if isinstance(g, dict) else g[0]
        members = g0.get("members_count", 0)
        prev = kv_get("members_last")
        delta = f" ({int(members) - int(prev):+d} за неделю)" if prev else ""
        parts.append(f"👥 Подписчики: <b>{members}</b>{delta}")
        kv_set("members_last", members)
    except Exception as e:
        parts.append(f"👥 Подписчики: не получил ({e})")
    try:
        now = int(time.time())
        stats = await vk("stats.get", group_id=GROUP_ID, interval="day",
                         timestamp_from=now - 7 * 86400, timestamp_to=now)
        reach = sum((d.get("reach") or {}).get("reach", 0) for d in stats)
        views = sum((d.get("visitors") or {}).get("views", 0) for d in stats)
        parts.append(f"👁 Охват: <b>{reach}</b> · просмотры: <b>{views}</b>")
    except Exception:
        parts.append("👁 Охват: ВК не отдал статистику (их сбой), посмотри в группе")
    ym = date.today().strftime("%Y-%m")
    rows = db.execute("SELECT amount FROM bookings WHERE day LIKE ?", (ym + "%",)).fetchall()
    if rows:
        parts.append(f"💰 Реклама в этом месяце: {sum(r[0] or 0 for r in rows)} ₽ "
                     f"за {len(rows)} размещений")
    week_ago = (datetime.now(MSK) - timedelta(days=7)).isoformat(timespec="seconds")
    n = db.execute("SELECT COUNT(*) FROM sent_log WHERE ts > ?", (week_ago,)).fetchone()[0]
    parts.append(f"✉️ Ответов жителям за неделю: {n}")
    parts.append(f"📅 Свободные даты: {free_dates()}")
    await tg_admin("\n".join(parts))


async def weekly_report_loop():
    while True:
        now = datetime.now(MSK)
        tgt = (now + timedelta(days=(7 - now.weekday()) % 7)).replace(
            hour=9, minute=0, second=0, microsecond=0)
        if tgt <= now:
            tgt += timedelta(days=7)
        await asyncio.sleep((tgt - now).total_seconds())
        try:
            if admin_chat():
                await send_weekly_report()
        except Exception:
            log.exception("weekly_report_loop")


# ---------------------------------------------------------------- main
async def main():
    if not TG_BOT_TOKEN or not VK_COMMUNITY_TOKEN:
        raise SystemExit("Заполни TG_BOT_TOKEN и VK_COMMUNITY_TOKEN в .env")
    asyncio.create_task(suggests_loop())
    asyncio.create_task(messages_loop())
    asyncio.create_task(weekly_report_loop())
    asyncio.create_task(site_loop())
    log.info("пульт запущен, группа -%s", GROUP_ID)
    await dp.start_polling(bot)


if __name__ == "__main__":
    asyncio.run(main())
