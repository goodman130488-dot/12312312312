#!/usr/bin/env python3
"""
Telegram mass messaging script.

Two modes:
  bot   — sends via Telegram Bot API (requires TELEGRAM_BOT_TOKEN)
  user  — sends via real user accounts loaded from tdata folders (Telegram Desktop)
"""

import asyncio
import csv
import logging
import os
import sys
from dataclasses import dataclass
from pathlib import Path

import httpx

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[
        logging.StreamHandler(),
        logging.FileHandler("send.log", encoding="utf-8"),
    ],
)
log = logging.getLogger(__name__)

BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "")
API_URL = f"https://api.telegram.org/bot{BOT_TOKEN}"

SEND_DELAY = float(os.getenv("SEND_DELAY", "0.05"))
MAX_RETRIES = int(os.getenv("MAX_RETRIES", "3"))


# ---------------------------------------------------------------------------
# Data model
# ---------------------------------------------------------------------------

@dataclass
class Recipient:
    chat_id: str
    message: str = ""


def load_recipients(csv_path: str, default_message: str) -> list[Recipient]:
    recipients: list[Recipient] = []
    with open(csv_path, newline="", encoding="utf-8") as f:
        reader = csv.DictReader(f)
        for row in reader:
            chat_id = row.get("chat_id", "").strip()
            if not chat_id:
                continue
            message = row.get("message", "").strip() or default_message
            recipients.append(Recipient(chat_id=chat_id, message=message))
    return recipients


# ---------------------------------------------------------------------------
# Bot mode (HTTP API)
# ---------------------------------------------------------------------------

async def _bot_send_one(
    client: httpx.AsyncClient,
    chat_id: str,
    text: str,
    parse_mode: str,
) -> bool:
    payload = {"chat_id": chat_id, "text": text, "parse_mode": parse_mode}
    for attempt in range(1, MAX_RETRIES + 1):
        try:
            resp = await client.post(f"{API_URL}/sendMessage", json=payload, timeout=30)
            data = resp.json()
            if data.get("ok"):
                return True

            err_code = data.get("error_code", 0)
            err_desc = data.get("description", "")

            if err_code == 429:
                retry_after = data.get("parameters", {}).get("retry_after", 5)
                log.warning("Rate limited for %s. Sleeping %ss.", chat_id, retry_after)
                await asyncio.sleep(retry_after)
                continue

            log.error("Failed to send to %s: [%s] %s", chat_id, err_code, err_desc)
            return False

        except (httpx.TimeoutException, httpx.NetworkError) as exc:
            log.warning("Network error for %s (attempt %d/%d): %s", chat_id, attempt, MAX_RETRIES, exc)
            await asyncio.sleep(2 ** attempt)

    log.error("Giving up on %s after %d attempts.", chat_id, MAX_RETRIES)
    return False


async def run_bot(csv_path: str, default_message: str, parse_mode: str) -> None:
    if not BOT_TOKEN:
        log.error("TELEGRAM_BOT_TOKEN env variable is not set.")
        sys.exit(1)

    recipients = load_recipients(csv_path, default_message)
    if not recipients:
        log.error("No recipients found in %s", csv_path)
        sys.exit(1)

    log.info("Bot mode: sending to %d recipients.", len(recipients))
    ok, fail = 0, 0

    async with httpx.AsyncClient() as client:
        for i, r in enumerate(recipients, 1):
            success = await _bot_send_one(client, r.chat_id, r.message, parse_mode)
            if success:
                ok += 1
                log.info("[%d/%d] OK -> %s", i, len(recipients), r.chat_id)
            else:
                fail += 1
                log.warning("[%d/%d] FAIL -> %s", i, len(recipients), r.chat_id)
            if i < len(recipients):
                await asyncio.sleep(SEND_DELAY)

    log.info("Done. Sent: %d, Failed: %d", ok, fail)


# ---------------------------------------------------------------------------
# User mode (tdata / Telethon)
# ---------------------------------------------------------------------------

async def _user_send_one(pool, chat_id: str, text: str) -> bool:
    from telethon.errors import (
        FloodWaitError,
        UserPrivacyRestrictedError,
        ChatWriteForbiddenError,
        PeerIdInvalidError,
        InputUserDeactivatedError,
        UserBannedInChannelError,
    )

    for attempt in range(1, MAX_RETRIES + 1):
        client, account_name = pool.next()
        try:
            await client.send_message(chat_id, text)
            return True

        except FloodWaitError as e:
            log.warning("[%s] FloodWait %ds for %s.", account_name, e.seconds, chat_id)
            await asyncio.sleep(e.seconds)
            # retry with same or next account on next iteration

        except (
            UserPrivacyRestrictedError,
            PeerIdInvalidError,
            InputUserDeactivatedError,
        ) as e:
            log.warning("Skipping %s: %s", chat_id, type(e).__name__)
            return False

        except (ChatWriteForbiddenError, UserBannedInChannelError) as e:
            log.warning("No permission to write to %s: %s", chat_id, type(e).__name__)
            return False

        except Exception as exc:
            log.warning(
                "[%s] Error sending to %s (attempt %d/%d): %s",
                account_name, chat_id, attempt, MAX_RETRIES, exc,
            )
            await asyncio.sleep(2 ** attempt)

    log.error("Giving up on %s after %d attempts.", chat_id, MAX_RETRIES)
    return False


async def run_user(csv_path: str, default_message: str, accounts_dir: str) -> None:
    from tdata_loader import load_accounts

    recipients = load_recipients(csv_path, default_message)
    if not recipients:
        log.error("No recipients found in %s", csv_path)
        sys.exit(1)

    pool = await load_accounts(accounts_dir)
    log.info("User mode: %d account(s), %d recipients.", len(pool), len(recipients))

    ok, fail = 0, 0
    try:
        for i, r in enumerate(recipients, 1):
            success = await _user_send_one(pool, r.chat_id, r.message)
            if success:
                ok += 1
                log.info("[%d/%d] OK -> %s", i, len(recipients), r.chat_id)
            else:
                fail += 1
                log.warning("[%d/%d] FAIL -> %s", i, len(recipients), r.chat_id)
            if i < len(recipients):
                await asyncio.sleep(SEND_DELAY)
    finally:
        await pool.close_all()

    log.info("Done. Sent: %d, Failed: %d", ok, fail)


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

def main() -> None:
    import argparse

    parser = argparse.ArgumentParser(
        description="Telegram mass messaging script",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
  # Bot mode (default)
  python send.py --mode bot -m "Hello!" --csv recipients.csv

  # User mode — accounts loaded from tdata folders
  python send.py --mode user --accounts-dir accounts/ -m "Hello!"
""",
    )
    parser.add_argument(
        "--mode",
        choices=["bot", "user"],
        default="bot",
        help="'bot' uses Bot API token; 'user' uses real accounts from tdata (default: bot)",
    )
    parser.add_argument("--csv", default="recipients.csv", help="Path to recipients CSV")
    parser.add_argument("--message", "-m", default="", help="Default message text (HTML supported)")
    parser.add_argument("--message-file", "-f", default="", help="Read message text from a file")
    parser.add_argument(
        "--parse-mode",
        choices=["HTML", "Markdown", "MarkdownV2"],
        default="HTML",
        help="Parse mode for bot mode (default: HTML)",
    )
    parser.add_argument(
        "--accounts-dir",
        default="accounts",
        help="Directory containing tdata account subfolders (user mode only, default: accounts/)",
    )

    args = parser.parse_args()

    default_message = args.message
    if args.message_file:
        default_message = Path(args.message_file).read_text(encoding="utf-8").strip()

    if not Path(args.csv).exists():
        parser.error(f"CSV file '{args.csv}' not found.")

    if args.mode == "bot":
        asyncio.run(run_bot(args.csv, default_message, args.parse_mode))
    else:
        asyncio.run(run_user(args.csv, default_message, args.accounts_dir))


if __name__ == "__main__":
    main()
