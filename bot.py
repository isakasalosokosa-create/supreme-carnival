#!/usr/bin/env python3
# -*- coding: utf-8 -*-

import logging
import random
import re
import sqlite3
import time
from contextlib import contextmanager
from datetime import datetime, timezone

from telegram import Update
from telegram.ext import (
    ApplicationBuilder,
    CommandHandler,
    MessageHandler,
    ContextTypes,
    filters,
)

# ================== КОНФИГУРАЦИЯ ==================
TOKEN = "7991920232:AAEKMDzj0s4L8U81pNK4EVpeEazn0UoJYv0"

DB_PATH = "alcometr.db"

COOLDOWN_SECONDS = 8 * 60          # 8 минут
MAX_MESSAGE_AGE = 10               # игнорировать сообщения старше 10 сек

BASE_VOLUME = 0.5                  # базовая порция алкоголя, л
VOLUME_INCREMENT = 0.05            # прирост за каждое употребление

BOTTLE_DROP_CHANCE = 0.25          # шанс выпадения бутылки при "алко"

CASINO_WIN_CHANCE = 0.5            # шанс выигрыша в казино на одну бутылку
CASINO_BASE_WIN = 0.5              # базовый выигрыш литров
CASINO_BONUS_PER_DRINK = 0.05      # бонус за каждое предыдущее употребление

# ================== БАЗА ДАННЫХ ==================
def init_db():
    with sqlite3.connect(DB_PATH) as conn:
        conn.execute("""
            CREATE TABLE IF NOT EXISTS users (
                user_id INTEGER PRIMARY KEY,
                username TEXT,
                total_volume REAL DEFAULT 0,
                drink_count INTEGER DEFAULT 0,
                last_drink_time INTEGER DEFAULT 0,
                bottles INTEGER DEFAULT 0
            )
        """)
        # Добавляем поле bottles, если таблица уже существовала без него
        try:
            conn.execute("ALTER TABLE users ADD COLUMN bottles INTEGER DEFAULT 0")
        except sqlite3.OperationalError:
            pass  # поле уже есть

@contextmanager
def get_db():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    try:
        yield conn
    finally:
        conn.close()

def get_user(user_id: int):
    with get_db() as conn:
        return conn.execute(
            "SELECT * FROM users WHERE user_id = ?", (user_id,)
        ).fetchone()

def update_user_alco(user_id: int, username: str, added_volume: float, bottle_gained: bool):
    now = int(time.time())
    with get_db() as conn:
        cur = conn.execute(
            "SELECT drink_count, bottles FROM users WHERE user_id = ?", (user_id,)
        ).fetchone()
        if cur is None:
            drink_count = 0
            bottles = 1 if bottle_gained else 0
            conn.execute("""
                INSERT INTO users (user_id, username, total_volume, drink_count, last_drink_time, bottles)
                VALUES (?, ?, ?, 1, ?, ?)
            """, (user_id, username, added_volume, now, bottles))
        else:
            drink_count = cur["drink_count"]
            bottles = cur["bottles"] + (1 if bottle_gained else 0)
            conn.execute("""
                UPDATE users SET
                    username = ?,
                    total_volume = total_volume + ?,
                    drink_count = drink_count + 1,
                    last_drink_time = ?,
                    bottles = ?
                WHERE user_id = ?
            """, (username, added_volume, now, bottles, user_id))
        conn.commit()
        row = conn.execute(
            "SELECT total_volume, bottles FROM users WHERE user_id = ?", (user_id,)
        ).fetchone()
        return row["total_volume"], row["bottles"]

def update_user_casino(user_id: int, username: str, bet: int, won_count: int, liters_won: float):
    with get_db() as conn:
        conn.execute("""
            UPDATE users SET
                username = ?,
                total_volume = total_volume + ?,
                bottles = bottles - ?
            WHERE user_id = ?
        """, (username, liters_won, bet, user_id))
        conn.commit()
        row = conn.execute(
            "SELECT total_volume, bottles FROM users WHERE user_id = ?", (user_id,)
        ).fetchone()
        return row["total_volume"], row["bottles"]

def get_top_users(limit=20):
    with get_db() as conn:
        return conn.execute(
            "SELECT username, total_volume FROM users ORDER BY total_volume DESC LIMIT ?",
            (limit,)
        ).fetchall()

# ================== ВСПОМОГАТЕЛЬНЫЕ ФУНКЦИИ ==================
def format_username(user) -> str:
    if user.username:
        return f"@{user.username}"
    return user.first_name

def can_drink(last_drink_time: int) -> tuple:
    now = int(time.time())
    diff = now - last_drink_time
    if diff >= COOLDOWN_SECONDS or last_drink_time == 0:
        return True, 0
    return False, COOLDOWN_SECONDS - diff

def calculate_added_volume(drink_count: int) -> float:
    return BASE_VOLUME + VOLUME_INCREMENT * drink_count

def is_message_too_old(update: Update) -> bool:
    if not update.message or not update.message.date:
        return False
    msg_time = update.message.date.replace(tzinfo=timezone.utc)
    now = datetime.now(timezone.utc)
    age = (now - msg_time).total_seconds()
    return age > MAX_MESSAGE_AGE

def calculate_casino_win(drink_count: int) -> float:
    return CASINO_BASE_WIN + CASINO_BONUS_PER_DRINK * drink_count

# ================== ОБРАБОТЧИКИ КОМАНД ==================
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    username = format_username(user)
    text = (
        f"🍺 {username}, добро пожаловать в алкогольный игровой бот!\n"
        "Напиши <b>помощь</b> чтобы увидеть команды\n"
        "Напиши <b>топ алко</b> чтобы увидеть топ\n"
        "Напиши <b>алко</b> чтобы начать"
    )
    await update.message.reply_text(text, parse_mode="HTML")

async def help_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = (
        "🍺 Доступные команды:\n"
        "<b>алко</b> – отметить, что выпил (кулдаун 8 минут)\n"
        "<b>топ алко</b> – показать топ-20 алкоголиков\n"
        "<b>казино N</b> – поставить N бутылок, шанс выиграть литры\n"
        "<b>бот</b> – Алкоголь тут🍺\n"
        "<b>помощь</b> – эта справка\n"
        "/start, /help – тоже работают"
    )
    await update.message.reply_text(text, parse_mode="HTML")

async def alco_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if is_message_too_old(update):
        logging.info(f"Ignored old message from {update.effective_user.id}")
        return

    user = update.effective_user
    user_id = user.id
    username = format_username(user)

    db_user = get_user(user_id)
    if db_user:
        can, remaining = can_drink(db_user["last_drink_time"])
        if not can:
            minutes = remaining // 60
            seconds = remaining % 60
            await update.message.reply_text(
                f"⏳ {username}, ты уже выпил! Подожди ещё {minutes} мин {seconds} сек."
            )
            return
        drink_count = db_user["drink_count"]
    else:
        drink_count = 0

    added = calculate_added_volume(drink_count)
    bottle_gained = random.random() < BOTTLE_DROP_CHANCE

    total, bottles = update_user_alco(
        user_id,
        username.lstrip('@') if user.username else username,
        added,
        bottle_gained
    )

    response = (
        f"{username}, ты выпил(а) {added:.2f} л. алкоголя 🍺.\n"
        f"Выпито всего – {total:.2f} л."
    )
    if bottle_gained:
        response += (
            f"\nТакже выбита одна бутылка алкоголя 🍾\n"
            f"Используй команду \"казино N\" чтобы сыграть."
        )
    await update.message.reply_text(response)

async def top_alco_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if is_message_too_old(update):
        return

    top = get_top_users(20)
    if not top:
        await update.message.reply_text("🍺 Пока никто не пил. Будь первым!")
        return

    lines = ["🍺 Топ 20 алкоголиков:"]
    for row in top:
        name = row["username"] if row["username"] else "аноним"
        lines.append(f"@{name} выпито {row['total_volume']:.2f} л")
    await update.message.reply_text("\n".join(lines))

async def bot_response(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("Алкоголь тут🍺")

async def casino_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if is_message_too_old(update):
        return

    user = update.effective_user
    user_id = user.id
    username = format_username(user)

    text = update.message.text.strip()
    match = re.match(r'^(?i)казино\s+(\d+)$', text)  # здесь (?i) в начале, это корректно
    if not match:
        await update.message.reply_text("Укажи сколько бутылок поставить, например: казино 2")
        return

    bet = int(match.group(1))
    if bet <= 0:
        await update.message.reply_text("Количество бутылок должно быть больше нуля.")
        return

    db_user = get_user(user_id)
    if not db_user:
        await update.message.reply_text("У тебя нет бутылок. Сначала используй команду \"алко\" и попробуй выбить бутылку.")
        return

    if db_user["bottles"] < bet:
        await update.message.reply_text(f"У тебя только {db_user['bottles']} бутылок 🍾, не хватает.")
        return

    drink_count = db_user["drink_count"]
    won_count = 0
    liters_won = 0.0

    for _ in range(bet):
        if random.random() < CASINO_WIN_CHANCE:
            won_count += 1
            liters_won += calculate_casino_win(drink_count)

    new_total, new_bottles = update_user_casino(user_id, username, bet, won_count, liters_won)

    if won_count == 0:
        response = (
            f"🙅‍♂️ {username}, тебе не повезло — ты профукал все бутылки. Может, в следующий раз повезёт?\n"
            f"Баланс литров: {new_total:.2f} л\n"
            f"Бутылок: {new_bottles} 🍾"
        )
    else:
        response = (
            f"🪙 {username}, ты выиграл! 🏆\n"
            f"Поставлено бутылок: {bet} 🍾\n"
        )
        if won_count < bet:
            response += f"Из них сыграло: {won_count}\n"
        response += (
            f"Получено литров: {liters_won:.2f} л\n"
            f"Баланс литров: {new_total:.2f} л\n"
            f"Осталось бутылок: {new_bottles} 🍾"
        )

    await update.message.reply_text(response)

# ================== ФИЛЬТРЫ (исправлены) ==================
ALCO_FILTER = filters.Regex(r'^алко$', flags=re.IGNORECASE)
TOP_FILTER = filters.Regex(r'^топ алко$', flags=re.IGNORECASE)
BOT_FILTER = filters.Regex(r'^бот$', flags=re.IGNORECASE)
HELP_FILTER = filters.Regex(r'^помощь$', flags=re.IGNORECASE)
CASINO_FILTER = filters.Regex(r'^казино\s+\d+$', flags=re.IGNORECASE)

# ================== ЗАПУСК ==================
def main():
    logging.basicConfig(
        format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
        level=logging.INFO
    )
    init_db()

    app = ApplicationBuilder().token(TOKEN).build()

    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("help", help_cmd))

    app.add_handler(MessageHandler(ALCO_FILTER, alco_command))
    app.add_handler(MessageHandler(TOP_FILTER, top_alco_command))
    app.add_handler(MessageHandler(BOT_FILTER, bot_response))
    app.add_handler(MessageHandler(HELP_FILTER, help_cmd))
    app.add_handler(MessageHandler(CASINO_FILTER, casino_command))

    print("Бот запущен...")
    app.run_polling()

if __name__ == "__main__":
    main()
