# -*- coding: utf-8 -*-
"""Заказ рекламы прямо в переписке ВК: кнопки, календарь, единый график.

Здесь только «что показать и что записать» — сама отправка в vk_admin_bot.py.
Один график на всё: и на заказы из диалога, и на ручные брони админа.
"""

import json
import sqlite3
import time
from datetime import date, datetime, timedelta

MONTHS = ["января", "февраля", "марта", "апреля", "мая", "июня", "июля",
          "августа", "сентября", "октября", "ноября", "декабря"]
WD = ["пн", "вт", "ср", "чт", "пт", "сб", "вс"]
MON_SHORT = ["янв", "фев", "мар", "апр", "мая", "июн",
             "июл", "авг", "сен", "окт", "ноя", "дек"]

# время выхода: id, подпись, час публикации, цена в будни
SLOTS = [
    ("08-11", "8:00 – 11:00", "09:00", 400, "утро, дорога на работу"),
    ("12-14", "12:00 – 14:00", "12:30", 500, "обед, пик просмотров"),
    ("15-17", "15:00 – 17:00", "15:30", 300, "день"),
    ("18-20", "18:00 – 20:00", "19:00", 500, "вечер, второй пик"),
]
WEEKEND_PRICE = 400
HOLD_HOURS = 3

# форматы, которые человек может заказать сам, без переписки
FORMATS = {
    "post":      {"title": "Объявление в ленте", "kind": "slot", "price": None,
                  "hint": "цена зависит от времени: 300–500 ₽"},
    "post_story": {"title": "Объявление + история", "kind": "slot", "price": None,
                   "extra": 200, "hint": "к цене объявления +200 ₽"},
    "story":     {"title": "История на 24 часа", "kind": "slot", "price": 300,
                  "hint": "300 ₽"},
    "review":    {"title": "Обзор «Сходили — проверили»", "kind": "slot", "price": 1500,
                  "hint": "1500 ₽, приходим и снимаем сами"},
    "clip":      {"title": "Клип", "kind": "slot", "price": 1500,
                  "hint": "1500 ₽, видят и не подписчики"},
    "article":   {"title": "Статья в сообществе", "kind": "slot", "price": 1200,
                  "hint": "1200 ₽, остаётся навсегда"},
    "greeting":  {"title": "Поздравление", "kind": "slot", "price": 300,
                  "hint": "300 ₽"},
    "pin_day":   {"title": "Закреп на сутки", "kind": "pin", "price": 700, "days": 1,
                  "hint": "700 ₽"},
    "pin_week":  {"title": "Закреп на неделю", "kind": "pin", "price": 3000, "days": 7,
                  "hint": "3000 ₽"},
    "pin_month": {"title": "Закреп на месяц", "kind": "pin", "price": 7000, "days": 30,
                  "hint": "7000 ₽"},
}

# порядок кнопок в меню форматов
FORMAT_ORDER = ["post", "post_story", "story", "review", "clip", "article",
                "greeting", "pin_day", "pin_week", "pin_month"]


# ────────────────────────────────── база ──────────────────────────────────

def init(db: sqlite3.Connection) -> None:
    db.execute("""CREATE TABLE IF NOT EXISTS ad_orders (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        created TEXT, ts INTEGER, source TEXT,
        user_id INTEGER, name TEXT, contact TEXT,
        format TEXT, kind TEXT, title TEXT, price INTEGER,
        date TEXT, slot TEXT, date_to TEXT,
        status TEXT, post_id INTEGER, note TEXT)""")
    db.execute("""CREATE TABLE IF NOT EXISTS vk_dialog (
        user_id INTEGER PRIMARY KEY, state TEXT, data TEXT, updated INTEGER)""")
    db.commit()


LIVE = ("hold", "paid_wait", "confirmed", "posted")


def _live_rows(db, extra_sql: str = "", args=()):
    cutoff = int(time.time()) - HOLD_HOURS * 3600
    return db.execute(
        "SELECT * FROM ad_orders WHERE status IN ('paid_wait','confirmed','posted') "
        "OR (status='hold' AND ts > ?) " + extra_sql, (cutoff,) + tuple(args)).fetchall()


def busy_slots(db, day: str) -> set:
    """Какие окна заняты в этот день."""
    cutoff = int(time.time()) - HOLD_HOURS * 3600
    rows = db.execute(
        "SELECT slot FROM ad_orders WHERE date=? AND kind='slot' AND "
        "(status IN ('paid_wait','confirmed','posted') OR (status='hold' AND ts > ?))",
        (day, cutoff)).fetchall()
    return {r[0] for r in rows if r[0]}


def pin_busy(db, day: str) -> bool:
    cutoff = int(time.time()) - HOLD_HOURS * 3600
    row = db.execute(
        "SELECT 1 FROM ad_orders WHERE kind='pin' AND date<=? AND date_to>=? AND "
        "(status IN ('paid_wait','confirmed','posted') OR (status='hold' AND ts > ?)) LIMIT 1",
        (day, day, cutoff)).fetchone()
    return row is not None


def slot_price(slot_id: str, day: str) -> int:
    weekend = date.fromisoformat(day).weekday() >= 5
    for sid, _t, _h, price, _hint in SLOTS:
        if sid == slot_id:
            return WEEKEND_PRICE if weekend else price
    return WEEKEND_PRICE


def order_price(fmt: str, slot_id: str, day: str) -> int:
    f = FORMATS[fmt]
    if f["kind"] == "pin":
        return f["price"]
    if fmt == "post":
        return slot_price(slot_id, day)
    if fmt == "post_story":
        return slot_price(slot_id, day) + f["extra"]
    return f["price"]


def free_days(db, fmt: str, limit: int = 6, start_offset: int = 1) -> list[str]:
    """Ближайшие дни, где ещё есть место."""
    kind = FORMATS[fmt]["kind"]
    out, d = [], date.today() + timedelta(days=start_offset)
    for _ in range(60):
        ds = d.isoformat()
        if kind == "pin":
            if not pin_busy(db, ds):
                out.append(ds)
        else:
            if len(busy_slots(db, ds)) < len(SLOTS):
                out.append(ds)
        if len(out) >= limit:
            break
        d += timedelta(days=1)
    return out


def create_order(db, *, user_id: int, name: str, contact: str, fmt: str,
                 day: str, slot_id: str = "", source: str = "vk") -> int:
    f = FORMATS[fmt]
    date_to = ""
    if f["kind"] == "pin":
        date_to = (date.fromisoformat(day) + timedelta(days=f["days"] - 1)).isoformat()
    price = order_price(fmt, slot_id, day)
    cur = db.execute(
        "INSERT INTO ad_orders (created, ts, source, user_id, name, contact, format, "
        "kind, title, price, date, slot, date_to, status, post_id, note) "
        "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,'hold',0,'')",
        (datetime.now().isoformat(timespec="seconds"), int(time.time()), source,
         user_id, name, contact, fmt, f["kind"], f["title"], price, day,
         slot_id, date_to))
    db.commit()
    return cur.lastrowid


def get_order(db, oid: int) -> dict | None:
    db.row_factory = sqlite3.Row
    row = db.execute("SELECT * FROM ad_orders WHERE id=?", (oid,)).fetchone()
    db.row_factory = None
    return dict(row) if row else None


def user_active_order(db, user_id: int) -> dict | None:
    """Последняя живая заявка человека — чтобы понять, к чему относится скриншот."""
    cutoff = int(time.time()) - HOLD_HOURS * 3600
    db.row_factory = sqlite3.Row
    row = db.execute(
        "SELECT * FROM ad_orders WHERE user_id=? AND "
        "(status IN ('paid_wait','confirmed') OR (status='hold' AND ts > ?)) "
        "ORDER BY id DESC LIMIT 1", (user_id, cutoff)).fetchone()
    db.row_factory = None
    return dict(row) if row else None


def set_status(db, oid: int, status: str) -> None:
    db.execute("UPDATE ad_orders SET status=? WHERE id=?", (status, oid))
    db.commit()


def schedule_text(db, days: int = 14) -> str:
    """График на ближайшие дни — для команды /grafik."""
    lines = ["📅 <b>График рекламы</b>"]
    d = date.today()
    for _ in range(days):
        ds = d.isoformat()
        db.row_factory = sqlite3.Row
        rows = db.execute(
            "SELECT * FROM ad_orders WHERE date=? AND status IN "
            "('hold','paid_wait','confirmed','posted') ORDER BY slot", (ds,)).fetchall()
        db.row_factory = None
        mark = f"{d.strftime('%d.%m')} {WD[d.weekday()]}"
        if not rows:
            lines.append(f"{mark} — 🟢 свободно")
        else:
            parts = []
            for r in rows:
                icon = {"hold": "⏳", "paid_wait": "💰", "confirmed": "✅",
                        "posted": "📤"}.get(r["status"], "•")
                parts.append(f"{icon} {r['slot'] or r['title']} {r['name']} "
                             f"({r['price']} ₽)")
            lines.append(f"{mark} — " + "; ".join(parts))
        d += timedelta(days=1)
    lines.append("\n⏳ бронь · 💰 ждёт проверки оплаты · ✅ оплачено · 📤 опубликовано")
    return "\n".join(lines)


# ─────────────────────────────── клавиатуры ВК ───────────────────────────────

def _btn(label: str, payload: dict, color: str = "secondary") -> dict:
    return {"action": {"type": "text", "label": label[:40],
                       "payload": json.dumps(payload, ensure_ascii=False)},
            "color": color}


def kb(rows: list, one_time: bool = False) -> str:
    return json.dumps({"one_time": one_time, "inline": False, "buttons": rows},
                      ensure_ascii=False)


def kb_menu() -> str:
    return kb([
        [_btn("📅 Заказать рекламу", {"c": "order"}, "positive")],
        [_btn("📋 Весь прайс", {"c": "price"})],
        [_btn("💬 Задать вопрос", {"c": "ask"})],
    ])


def kb_formats() -> str:
    rows, buf = [], []
    for fid in FORMAT_ORDER:
        buf.append(_btn(FORMATS[fid]["title"], {"c": "fmt", "v": fid}))
        if len(buf) == 2:
            rows.append(buf)
            buf = []
    if buf:
        rows.append(buf)
    rows.append([_btn("❌ Отмена", {"c": "cancel"}, "negative")])
    return kb(rows)


def day_label(ds: str) -> str:
    d = date.fromisoformat(ds)
    return f"{d.day} {MON_SHORT[d.month - 1]} ({WD[d.weekday()]})"


def kb_days(days: list[str], offset: int) -> str:
    rows, buf = [], []
    for ds in days:
        buf.append(_btn(day_label(ds), {"c": "day", "v": ds}))
        if len(buf) == 2:
            rows.append(buf)
            buf = []
    if buf:
        rows.append(buf)
    rows.append([_btn("📆 Другие даты", {"c": "more", "v": offset + len(days)})])
    rows.append([_btn("← Форматы", {"c": "order"}), _btn("❌ Отмена", {"c": "cancel"}, "negative")])
    return kb(rows)


def kb_slots(db, fmt: str, day: str) -> str:
    busy = busy_slots(db, day)
    rows = []
    for sid, title, _h, _p, hint in SLOTS:
        if sid in busy:
            continue
        price = order_price(fmt, sid, day)
        rows.append([_btn(f"{title} — {price} ₽", {"c": "slot", "v": sid})])
    rows.append([_btn("← Другой день", {"c": "fmt", "v": fmt}),
                 _btn("❌ Отмена", {"c": "cancel"}, "negative")])
    return kb(rows)


def kb_pay() -> str:
    return kb([
        [_btn("✅ Я оплатил", {"c": "paid"}, "positive")],
        [_btn("❌ Отменить бронь", {"c": "cancel"}, "negative")],
    ])


# ─────────────────────────────── состояние диалога ───────────────────────────

def get_state(db, user_id: int) -> tuple[str, dict]:
    row = db.execute("SELECT state, data FROM vk_dialog WHERE user_id=?",
                     (user_id,)).fetchone()
    if not row:
        return "idle", {}
    try:
        return row[0], json.loads(row[1] or "{}")
    except Exception:
        return row[0], {}


def set_state(db, user_id: int, state: str, data: dict | None = None) -> None:
    db.execute("INSERT OR REPLACE INTO vk_dialog (user_id, state, data, updated) "
               "VALUES (?,?,?,?)",
               (user_id, state, json.dumps(data or {}, ensure_ascii=False),
                int(time.time())))
    db.commit()


def fmt_day_ru(ds: str) -> str:
    d = date.fromisoformat(ds)
    return f"{d.day} {MONTHS[d.month - 1]}"


def slot_title(sid: str) -> str:
    for s in SLOTS:
        if s[0] == sid:
            return s[1]
    return sid


def slot_hour(sid: str) -> str:
    for s in SLOTS:
        if s[0] == sid:
            return s[2]
    return "12:30"
