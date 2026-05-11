#!/usr/bin/env python3
"""
Telegram bot controller for mass messaging.

Flow:
  /start → upload CSV → type message → choose mode → confirm → mailing starts
  /cancel — stop active mailing
  /status — check progress
"""

import asyncio
import csv
import io
import logging
import os
import sys
from dataclasses import dataclass

from aiogram import Bot, Dispatcher, F
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode
from aiogram.exceptions import TelegramForbiddenError, TelegramNotFound, TelegramRetryAfter
from aiogram.filters import Command, CommandStart
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.types import (
    CallbackQuery,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    Message,
)

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[
        logging.StreamHandler(),
        logging.FileHandler("bot.log", encoding="utf-8"),
    ],
)
log = logging.getLogger(__name__)

BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "")
# Comma-separated admin user IDs. If empty — all users are admins.
ADMIN_IDS = {int(x) for x in os.getenv("ADMIN_IDS", "").split(",") if x.strip()}
ACCOUNTS_DIR = os.getenv("ACCOUNTS_DIR", "accounts")
SEND_DELAY = float(os.getenv("SEND_DELAY", "0.5"))
MAX_RETRIES = int(os.getenv("MAX_RETRIES", "3"))
# Send a progress update every N messages
PROGRESS_EVERY = int(os.getenv("PROGRESS_EVERY", "10"))

# admin_id -> running asyncio.Task
active_tasks: dict[int, asyncio.Task] = {}


# ---------------------------------------------------------------------------
# Data
# ---------------------------------------------------------------------------

@dataclass
class Recipient:
    chat_id: str
    message: str


def parse_csv(content: str) -> list[Recipient]:
    recipients: list[Recipient] = []
    reader = csv.DictReader(io.StringIO(content))
    for row in reader:
        chat_id = row.get("chat_id", "").strip()
        if not chat_id:
            continue
        recipients.append(Recipient(chat_id=chat_id, message=row.get("message", "").strip()))
    return recipients


# ---------------------------------------------------------------------------
# FSM states
# ---------------------------------------------------------------------------

class Form(StatesGroup):
    waiting_csv = State()
    waiting_message = State()
    confirm = State()


# ---------------------------------------------------------------------------
# Keyboards
# ---------------------------------------------------------------------------

def mode_kb() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[[
        InlineKeyboardButton(text="🤖 Bot API", callback_data="mode:bot"),
        InlineKeyboardButton(text="👤 User accounts (tdata)", callback_data="mode:user"),
    ]])


def confirm_kb() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[[
        InlineKeyboardButton(text="✅ Запустить", callback_data="action:confirm"),
        InlineKeyboardButton(text="❌ Отмена", callback_data="action:cancel"),
    ]])


def back_kb() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[[
        InlineKeyboardButton(text="« Назад к выбору режима", callback_data="action:back"),
    ]])


# ---------------------------------------------------------------------------
# Dispatcher
# ---------------------------------------------------------------------------

dp = Dispatcher(storage=MemoryStorage())


def is_admin(user_id: int) -> bool:
    return not ADMIN_IDS or user_id in ADMIN_IDS


# ── /start ──────────────────────────────────────────────────────────────────

@dp.message(CommandStart())
async def cmd_start(message: Message, state: FSMContext) -> None:
    if not is_admin(message.from_user.id):
        return
    await state.clear()
    await message.answer(
        "<b>Бот для массовой рассылки</b>\n\n"
        "Загрузи CSV-файл со списком получателей.\n\n"
        "<b>Формат CSV:</b>\n"
        "<code>chat_id,message\n"
        "@username,Привет!\n"
        "123456789,\n"
        "-1001234567890,Объявление</code>\n\n"
        "Колонка <code>message</code> опциональна — можно задать общий текст после загрузки.\n\n"
        "/help — справка  /cancel — остановить рассылку",
    )
    await state.set_state(Form.waiting_csv)


# ── /help ────────────────────────────────────────────────────────────────────

@dp.message(Command("help"))
async def cmd_help(message: Message) -> None:
    if not is_admin(message.from_user.id):
        return
    await message.answer(
        "<b>Команды:</b>\n"
        "/start — начать новую рассылку\n"
        "/status — прогресс текущей рассылки\n"
        "/cancel — остановить рассылку\n\n"
        "<b>Режимы отправки:</b>\n"
        "• <b>Bot API</b> — отправляет через этого бота (только пользователи, "
        "которые написали боту)\n"
        "• <b>User accounts</b> — отправляет через реальные аккаунты из папки "
        f"<code>{ACCOUNTS_DIR}/</code> (tdata)\n\n"
        "<b>Формат CSV:</b>\n"
        "<code>chat_id,message</code>\n"
        "chat_id: @username, числовой ID пользователя или -100... ID группы/канала",
    )


# ── /status ──────────────────────────────────────────────────────────────────

@dp.message(Command("status"))
async def cmd_status(message: Message) -> None:
    if not is_admin(message.from_user.id):
        return
    task = active_tasks.get(message.from_user.id)
    if task and not task.done():
        await message.answer("Рассылка активна. Используй /cancel чтобы остановить.")
    else:
        await message.answer("Активной рассылки нет. /start чтобы начать.")


# ── /cancel ──────────────────────────────────────────────────────────────────

@dp.message(Command("cancel"))
async def cmd_cancel(message: Message, state: FSMContext) -> None:
    if not is_admin(message.from_user.id):
        return
    admin_id = message.from_user.id
    task = active_tasks.get(admin_id)
    if task and not task.done():
        task.cancel()
        await message.answer("Останавливаю рассылку...")
    else:
        await state.clear()
        await message.answer("Нет активной рассылки. /start чтобы начать новую.")


# ── Step 1: receive CSV ───────────────────────────────────────────────────────

@dp.message(Form.waiting_csv, F.document)
async def got_csv(message: Message, state: FSMContext, bot: Bot) -> None:
    doc = message.document
    if not (doc.file_name or "").lower().endswith(".csv"):
        await message.answer("Нужен файл с расширением .csv")
        return

    file = await bot.get_file(doc.file_id)
    buf = io.BytesIO()
    await bot.download_file(file.file_path, destination=buf)

    try:
        content = buf.getvalue().decode("utf-8")
    except UnicodeDecodeError:
        content = buf.getvalue().decode("cp1251")  # fallback for Windows-saved files

    recipients = parse_csv(content)
    if not recipients:
        await message.answer(
            "В файле нет получателей. Убедись, что первая строка содержит "
            "заголовок <code>chat_id</code>.",
        )
        return

    await state.update_data(recipients=[{"chat_id": r.chat_id, "message": r.message} for r in recipients])
    await state.set_state(Form.waiting_message)
    await message.answer(
        f"Загружено <b>{len(recipients)}</b> получателей.\n\n"
        "Введи текст сообщения по умолчанию.\n"
        "Он будет использован для строк без колонки <code>message</code>.\n\n"
        "Если все сообщения уже в CSV — отправь <code>-</code>",
    )


@dp.message(Form.waiting_csv)
async def waiting_csv_wrong(message: Message) -> None:
    await message.answer("Пожалуйста, загрузи CSV-файл как документ.")


# ── Step 2: receive default message ──────────────────────────────────────────

@dp.message(Form.waiting_message)
async def got_message(message: Message, state: FSMContext) -> None:
    text = "" if message.text.strip() == "-" else message.text.strip()
    await state.update_data(default_message=text)

    data = await state.get_data()
    recipients = data["recipients"]
    count = len(recipients)

    # Count how many would have no message at all
    no_msg = sum(1 for r in recipients if not r["message"] and not text)

    warning = ""
    if no_msg:
        warning = f"\n⚠️ {no_msg} получателей без сообщения — они будут пропущены."

    preview = (text[:200] + "…") if len(text) > 200 else text
    preview_block = f"\n<b>Сообщение:</b>\n<code>{preview}</code>" if text else "\n<i>(сообщения из CSV)</i>"

    await state.set_state(Form.confirm)
    await message.answer(
        f"<b>Получатели:</b> {count}{warning}{preview_block}\n\n"
        "Выбери режим отправки:",
        reply_markup=mode_kb(),
    )


# ── Step 3: mode selected → show confirm ─────────────────────────────────────

@dp.callback_query(Form.confirm, F.data.startswith("mode:"))
async def chose_mode(call: CallbackQuery, state: FSMContext) -> None:
    mode = call.data.split(":")[1]
    await state.update_data(mode=mode)
    mode_label = "🤖 Bot API" if mode == "bot" else "👤 User accounts (tdata)"

    data = await state.get_data()
    count = len(data["recipients"])
    text = data.get("default_message", "")
    no_msg = sum(1 for r in data["recipients"] if not r["message"] and not text)
    effective = count - no_msg

    await call.message.edit_text(
        f"<b>Режим:</b> {mode_label}\n"
        f"<b>Получатели:</b> {effective} (из {count})\n\n"
        "Запустить рассылку?",
        reply_markup=confirm_kb(),
    )
    await call.answer()


@dp.callback_query(Form.confirm, F.data == "action:back")
async def back_to_mode(call: CallbackQuery, state: FSMContext) -> None:
    data = await state.get_data()
    count = len(data["recipients"])
    text = data.get("default_message", "")
    preview = (text[:200] + "…") if len(text) > 200 else text
    preview_block = f"\n<b>Сообщение:</b>\n<code>{preview}</code>" if text else "\n<i>(сообщения из CSV)</i>"
    await call.message.edit_text(
        f"<b>Получатели:</b> {count}{preview_block}\n\nВыбери режим отправки:",
        reply_markup=mode_kb(),
    )
    await call.answer()


@dp.callback_query(Form.confirm, F.data == "action:cancel")
async def cancel_confirm(call: CallbackQuery, state: FSMContext) -> None:
    await state.clear()
    await call.message.edit_text("Рассылка отменена. /start чтобы начать заново.")
    await call.answer()


# ── Step 4: confirmed → launch background task ────────────────────────────────

@dp.callback_query(Form.confirm, F.data == "action:confirm")
async def start_mailing(call: CallbackQuery, state: FSMContext, bot: Bot) -> None:
    admin_id = call.from_user.id
    if admin_id in active_tasks and not active_tasks[admin_id].done():
        await call.answer("Рассылка уже запущена! /cancel чтобы остановить.", show_alert=True)
        return

    data = await state.get_data()
    await state.clear()

    default_message = data.get("default_message", "")
    mode = data.get("mode", "bot")
    raw = data.get("recipients", [])

    recipients = [
        Recipient(chat_id=r["chat_id"], message=r["message"] or default_message)
        for r in raw
    ]
    recipients = [r for r in recipients if r.message]

    if not recipients:
        await call.message.edit_text("Нет сообщений для отправки. /start чтобы начать заново.")
        await call.answer()
        return

    status_msg = await call.message.edit_text(
        f"Запускаю рассылку для <b>{len(recipients)}</b> получателей…",
    )
    await call.answer()

    task = asyncio.create_task(
        _mailing_task(bot, admin_id, status_msg.message_id, recipients, mode)
    )
    active_tasks[admin_id] = task
    task.add_done_callback(lambda _: active_tasks.pop(admin_id, None))


# ---------------------------------------------------------------------------
# Background mailing task
# ---------------------------------------------------------------------------

async def _mailing_task(
    bot: Bot,
    admin_id: int,
    status_msg_id: int,
    recipients: list[Recipient],
    mode: str,
) -> None:
    ok = fail = 0
    total = len(recipients)
    pool = None

    async def update_progress(current: int) -> None:
        try:
            await bot.edit_message_text(
                f"Рассылка: <b>{current}/{total}</b>\n✅ {ok}   ❌ {fail}",
                chat_id=admin_id,
                message_id=status_msg_id,
            )
        except Exception:
            pass

    try:
        if mode == "user":
            from tdata_loader import load_accounts
            pool = await load_accounts(ACCOUNTS_DIR)
            log.info("Loaded %d user account(s) for mailing.", len(pool))

        for i, r in enumerate(recipients, 1):
            try:
                if mode == "bot":
                    await _bot_send(bot, r.chat_id, r.message)
                else:
                    await _user_send(pool, r.chat_id, r.message)
                ok += 1
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                fail += 1
                log.warning("Failed %s: %s", r.chat_id, exc)

            if i % PROGRESS_EVERY == 0 or i == total:
                await update_progress(i)

            if i < total:
                await asyncio.sleep(SEND_DELAY)

    except asyncio.CancelledError:
        await bot.send_message(
            admin_id,
            f"⛔ Рассылка прервана.\n✅ {ok}   ❌ {fail}   (из {total})\n\n/start — новая рассылка",
        )
        return
    finally:
        if pool:
            await pool.close_all()

    await bot.send_message(
        admin_id,
        f"✅ Рассылка завершена!\n\n"
        f"Отправлено: {ok}\nОшибок: {fail}\nВсего: {total}\n\n/start — новая рассылка",
    )


# ---------------------------------------------------------------------------
# Sending helpers
# ---------------------------------------------------------------------------

async def _bot_send(bot: Bot, chat_id: str, text: str) -> None:
    for attempt in range(1, MAX_RETRIES + 1):
        try:
            await bot.send_message(chat_id, text)
            return
        except TelegramRetryAfter as e:
            log.warning("Rate limited, sleeping %ds.", e.retry_after)
            await asyncio.sleep(e.retry_after)
        except (TelegramForbiddenError, TelegramNotFound) as e:
            raise  # non-retriable
        except Exception:
            if attempt == MAX_RETRIES:
                raise
            await asyncio.sleep(2 ** attempt)


async def _user_send(pool, chat_id: str, text: str) -> None:
    from telethon.errors import (
        FloodWaitError,
        UserPrivacyRestrictedError,
        ChatWriteForbiddenError,
        PeerIdInvalidError,
        InputUserDeactivatedError,
        UserBannedInChannelError,
    )

    for attempt in range(1, MAX_RETRIES + 1):
        client, name = pool.next()
        try:
            await client.send_message(chat_id, text)
            return
        except FloodWaitError as e:
            log.warning("[%s] FloodWait %ds.", name, e.seconds)
            await asyncio.sleep(e.seconds)
        except (UserPrivacyRestrictedError, PeerIdInvalidError,
                InputUserDeactivatedError, ChatWriteForbiddenError,
                UserBannedInChannelError) as e:
            raise RuntimeError(type(e).__name__)
        except Exception as exc:
            if attempt == MAX_RETRIES:
                raise
            log.warning("[%s] attempt %d/%d for %s: %s", name, attempt, MAX_RETRIES, chat_id, exc)
            await asyncio.sleep(2 ** attempt)


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

async def main() -> None:
    if not BOT_TOKEN:
        log.error("TELEGRAM_BOT_TOKEN is not set.")
        sys.exit(1)

    bot = Bot(
        token=BOT_TOKEN,
        default=DefaultBotProperties(parse_mode=ParseMode.HTML),
    )
    log.info("Starting bot...")
    await dp.start_polling(bot, allowed_updates=["message", "callback_query"])


if __name__ == "__main__":
    asyncio.run(main())
