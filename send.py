#!/usr/bin/env python3
"""
Telegram mass messaging script.
Reads recipients from a CSV file and sends messages via Telegram Bot API.
"""

import asyncio
import csv
import logging
import os
import sys
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

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

# Delay between requests to avoid hitting rate limits (messages per second)
SEND_DELAY = float(os.getenv("SEND_DELAY", "0.05"))
# Retry count on transient errors
MAX_RETRIES = int(os.getenv("MAX_RETRIES", "3"))


@dataclass
class Recipient:
    # chat_id can be a user numeric ID, @username, or channel/group @username / numeric ID
    chat_id: str
    # optional per-recipient message override; if empty, uses global message
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


async def send_message(
    client: httpx.AsyncClient,
    chat_id: str,
    text: str,
    parse_mode: str = "HTML",
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

            # 429 Too Many Requests — respect retry_after
            if err_code == 429:
                retry_after = data.get("parameters", {}).get("retry_after", 5)
                log.warning("Rate limited for %s. Sleeping %ss.", chat_id, retry_after)
                await asyncio.sleep(retry_after)
                continue

            # Non-retriable errors
            log.error("Failed to send to %s: [%s] %s", chat_id, err_code, err_desc)
            return False

        except (httpx.TimeoutException, httpx.NetworkError) as exc:
            log.warning("Network error sending to %s (attempt %d/%d): %s", chat_id, attempt, MAX_RETRIES, exc)
            await asyncio.sleep(2 ** attempt)

    log.error("Giving up on %s after %d attempts.", chat_id, MAX_RETRIES)
    return False


async def run(csv_path: str, default_message: str, parse_mode: str) -> None:
    if not BOT_TOKEN:
        log.error("TELEGRAM_BOT_TOKEN env variable is not set.")
        sys.exit(1)

    recipients = load_recipients(csv_path, default_message)
    if not recipients:
        log.error("No recipients found in %s", csv_path)
        sys.exit(1)

    log.info("Loaded %d recipients from %s", len(recipients), csv_path)

    ok_count = 0
    fail_count = 0

    async with httpx.AsyncClient() as client:
        for i, r in enumerate(recipients, 1):
            success = await send_message(client, r.chat_id, r.message, parse_mode)
            if success:
                ok_count += 1
                log.info("[%d/%d] OK -> %s", i, len(recipients), r.chat_id)
            else:
                fail_count += 1
                log.warning("[%d/%d] FAIL -> %s", i, len(recipients), r.chat_id)

            # Polite delay between sends
            if i < len(recipients):
                await asyncio.sleep(SEND_DELAY)

    log.info("Done. Sent: %d, Failed: %d", ok_count, fail_count)


def main() -> None:
    import argparse

    parser = argparse.ArgumentParser(description="Telegram mass messaging script")
    parser.add_argument(
        "--csv",
        default="recipients.csv",
        help="Path to CSV file with recipients (default: recipients.csv)",
    )
    parser.add_argument(
        "--message",
        "-m",
        default="",
        help="Default message text to send (used when CSV row has no 'message' column). Supports HTML tags.",
    )
    parser.add_argument(
        "--message-file",
        "-f",
        default="",
        help="Read default message text from a file instead of --message.",
    )
    parser.add_argument(
        "--parse-mode",
        choices=["HTML", "Markdown", "MarkdownV2"],
        default="HTML",
        help="Telegram parse mode (default: HTML)",
    )
    args = parser.parse_args()

    default_message = args.message
    if args.message_file:
        default_message = Path(args.message_file).read_text(encoding="utf-8").strip()

    if not default_message and not Path(args.csv).exists():
        parser.error(f"CSV file '{args.csv}' not found.")

    asyncio.run(run(args.csv, default_message, args.parse_mode))


if __name__ == "__main__":
    main()
