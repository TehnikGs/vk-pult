# -*- coding: utf-8 -*-
"""Долги перед клиентами и график закрепов — то, что передала Лера.

Две вещи, которые нельзя забыть:
  * у пакетных клиентов остались оплаченные посты — их надо выпустить;
  * закреп вверху ленты продан по месяцам, менять строго в срок.

Бот сам напоминает про смену закрепа заранее и дёргает по неоплаченным броням.
"""

import sqlite3
from datetime import date, timedelta

MONTHS = ["января", "февраля", "марта", "апреля", "мая", "июня", "июля",
          "августа", "сентября", "октября", "ноября", "декабря"]


def init(db: sqlite3.Connection) -> None:
    db.execute("""CREATE TABLE IF NOT EXISTS clients (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        name TEXT, posts_left INTEGER DEFAULT 0,
        note TEXT DEFAULT '', active INTEGER DEFAULT 1)""")
    db.execute("""CREATE TABLE IF NOT EXISTS pins (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        client TEXT, date_from TEXT, date_to TEXT,
        paid INTEGER DEFAULT 0, note TEXT DEFAULT '',
        placed INTEGER DEFAULT 0)""")
    db.execute("""CREATE TABLE IF NOT EXISTS reminders_sent (
        key TEXT PRIMARY KEY, ts TEXT)""")
    db.commit()


def fmt(d: str) -> str:
    x = date.fromisoformat(d)
    return f"{x.day} {MONTHS[x.month - 1]}"


# ─────────────────────────────── клиенты ───────────────────────────────

def add_client(db, name: str, posts: int, note: str = "") -> int:
    cur = db.execute("INSERT INTO clients (name, posts_left, note) VALUES (?,?,?)",
                     (name, posts, note))
    db.commit()
    return cur.lastrowid


def clients_list(db, only_active: bool = True) -> list:
    db.row_factory = sqlite3.Row
    sql = "SELECT * FROM clients"
    if only_active:
        sql += " WHERE active=1"
    sql += " ORDER BY posts_left DESC, name"
    rows = [dict(r) for r in db.execute(sql).fetchall()]
    db.row_factory = None
    return rows


def spend_post(db, cid: int, n: int = 1) -> dict | None:
    db.row_factory = sqlite3.Row
    row = db.execute("SELECT * FROM clients WHERE id=?", (cid,)).fetchone()
    db.row_factory = None
    if not row:
        return None
    left = max(0, (row["posts_left"] or 0) - n)
    db.execute("UPDATE clients SET posts_left=?, active=? WHERE id=?",
               (left, 1 if left else 0, cid))
    db.commit()
    d = dict(row)
    d["posts_left"] = left
    return d


def clients_text(db) -> str:
    rows = clients_list(db)
    if not rows:
        return "Долгов по постам нет — все пакеты выпущены."
    total = sum(r["posts_left"] for r in rows)
    lines = [f"📦 <b>Оплаченные посты, которые мы должны</b> — всего {total}"]
    for r in rows:
        line = f"№{r['id']} · <b>{r['name']}</b> — {r['posts_left']} шт."
        if r["note"]:
            line += f"\n     <i>{r['note']}</i>"
        lines.append(line)
    lines.append("\nСписать один пост после выхода: /post 3")
    return "\n".join(lines)


# ─────────────────────────────── закрепы ───────────────────────────────

def add_pin(db, client: str, d_from: str, d_to: str, paid: bool, note: str = "") -> int:
    cur = db.execute("INSERT INTO pins (client, date_from, date_to, paid, note) "
                     "VALUES (?,?,?,?,?)", (client, d_from, d_to, 1 if paid else 0, note))
    db.commit()
    return cur.lastrowid


def pins_list(db) -> list:
    db.row_factory = sqlite3.Row
    rows = [dict(r) for r in db.execute(
        "SELECT * FROM pins ORDER BY date_from").fetchall()]
    db.row_factory = None
    return rows


def pins_text(db) -> str:
    rows = pins_list(db)
    if not rows:
        return "Закрепы не заведены."
    today = date.today().isoformat()
    lines = ["📌 <b>График закрепа вверху ленты</b>"]
    for r in rows:
        if r["date_to"] < today:
            mark = "✔️ прошёл"
        elif r["date_from"] <= today <= r["date_to"]:
            mark = "🔴 идёт сейчас"
        else:
            mark = "⏳ впереди"
        money = "оплачено" if r["paid"] else "❗ НЕ ОПЛАЧЕНО"
        lines.append(f"№{r['id']} · {fmt(r['date_from'])} — {fmt(r['date_to'])} · "
                     f"<b>{r['client']}</b> · {money} · {mark}")
        if r["note"]:
            lines.append(f"     <i>{r['note']}</i>")
    lines.append("\nОтметить оплату: /pinpaid 3 · снять: /pindel 3")
    return "\n".join(lines)


# ─────────────────────────────── напоминания ───────────────────────────────

def _once(db, key: str) -> bool:
    """Напоминание с таким ключом ещё не отправляли?"""
    if db.execute("SELECT 1 FROM reminders_sent WHERE key=?", (key,)).fetchone():
        return False
    db.execute("INSERT INTO reminders_sent (key, ts) VALUES (?, datetime('now'))",
               (key,))
    db.commit()
    return True


def due_reminders(db, today: date | None = None) -> list[str]:
    """Что нужно напомнить сегодня. Каждое напоминание уходит один раз."""
    today = today or date.today()
    out = []
    for p in pins_list(db):
        start = date.fromisoformat(p["date_from"])
        end = date.fromisoformat(p["date_to"])
        who, pid = p["client"], p["id"]

        # за 5 дней до старта — просим материал и оплату
        if start - timedelta(days=5) == today and _once(db, f"pin{pid}-5"):
            money = "" if p["paid"] else " И ОПЛАТУ — закреп пока не оплачен!"
            out.append(f"📌 Через 5 дней, {fmt(p['date_from'])}, начинается закреп "
                       f"<b>{who}</b>.\nНапомните клиенту прислать материал{money}")

        # за 2 дня — последний окрик
        if start - timedelta(days=2) == today and _once(db, f"pin{pid}-2"):
            out.append(f"📌 Послезавтра ({fmt(p['date_from'])}) ставим закреп "
                       f"<b>{who}</b>. Материал уже на руках?")

        # день старта
        if start == today and _once(db, f"pin{pid}-0"):
            money = "оплачено" if p["paid"] else "❗ ОПЛАТЫ НЕТ — уточните до публикации"
            out.append(f"📌 <b>СЕГОДНЯ меняем закреп</b> — ставим {who} "
                       f"до {fmt(p['date_to'])}. {money}")

        # за 3 дня до конца — предупредить следующего
        if end - timedelta(days=3) == today and _once(db, f"pin{pid}-end3"):
            out.append(f"📌 Через 3 дня ({fmt(p['date_to'])}) заканчивается закреп "
                       f"<b>{who}</b>. Проверьте, кто следующий и есть ли материал.")

        # неоплаченная бронь — дёргать раз в неделю, пока не оплатят
        if not p["paid"] and today < start:
            days = (start - today).days
            if days in (30, 21, 14, 10, 7, 3) and _once(db, f"pin{pid}-pay{days}"):
                out.append(f"💰 Закреп <b>{who}</b> с {fmt(p['date_from'])} "
                           f"<b>не оплачен</b>, осталось {days} дн. "
                           f"Напомните про оплату за месяц вперёд.")
    return out
