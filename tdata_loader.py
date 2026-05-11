"""
Loads Telegram user accounts from tdata folders (Telegram Desktop format)
and provides a round-robin account pool for sending messages.

Expected directory layout:
    accounts/
        account1/
            tdata/        <-- Telegram Desktop tdata folder
        account2/
            tdata/
    sessions/             <-- auto-created; cached Telethon session files
"""

import asyncio
import logging
from pathlib import Path

from opentele.td import TDesktop
from opentele.api import UseCurrentSession
from telethon import TelegramClient
from telethon.errors import (
    AuthKeyUnregisteredError,
    SessionPasswordNeededError,
    UserDeactivatedBanError,
)

log = logging.getLogger(__name__)

SESSIONS_DIR = Path("sessions")


class AccountPool:
    def __init__(self, clients: list[TelegramClient], names: list[str]) -> None:
        self._clients = clients
        self._names = names
        self._index = 0

    def __len__(self) -> int:
        return len(self._clients)

    def next(self) -> tuple[TelegramClient, str]:
        client = self._clients[self._index]
        name = self._names[self._index]
        self._index = (self._index + 1) % len(self._clients)
        return client, name

    async def close_all(self) -> None:
        for client in self._clients:
            try:
                await client.disconnect()
            except Exception:
                pass


async def _load_account(tdata_path: Path, session_path: Path, name: str) -> TelegramClient | None:
    try:
        tdesk = TDesktop(str(tdata_path))
        if not tdesk.isLoaded():
            log.warning("[%s] tdata failed to load, skipping.", name)
            return None

        client: TelegramClient = await tdesk.ToTelethon(
            session=str(session_path),
            flag=UseCurrentSession,
        )
        await client.connect()

        if not await client.is_user_authorized():
            log.warning("[%s] Account is not authorized (session expired?), skipping.", name)
            await client.disconnect()
            return None

        me = await client.get_me()
        display = f"@{me.username}" if me.username else f"id:{me.id}"
        log.info("[%s] Loaded account: %s", name, display)
        return client

    except UserDeactivatedBanError:
        log.error("[%s] Account is banned.", name)
    except AuthKeyUnregisteredError:
        log.error("[%s] Auth key unregistered (session revoked).", name)
    except SessionPasswordNeededError:
        log.error("[%s] Two-step verification required — cannot load automatically.", name)
    except Exception as exc:
        log.error("[%s] Unexpected error loading tdata: %s", name, exc)

    return None


async def load_accounts(accounts_dir: str) -> AccountPool:
    base = Path(accounts_dir)
    if not base.is_dir():
        raise FileNotFoundError(f"Accounts directory not found: {accounts_dir}")

    SESSIONS_DIR.mkdir(exist_ok=True)

    # Find all subdirectories that contain a tdata/ folder
    tdata_entries = sorted(
        (entry for entry in base.iterdir() if (entry / "tdata").is_dir()),
        key=lambda p: p.name,
    )

    if not tdata_entries:
        raise ValueError(
            f"No tdata folders found in '{accounts_dir}'. "
            "Each account should be a subfolder with a 'tdata' directory inside."
        )

    log.info("Found %d tdata folder(s) in '%s'.", len(tdata_entries), accounts_dir)

    tasks = [
        _load_account(
            tdata_path=entry / "tdata",
            session_path=SESSIONS_DIR / f"{entry.name}.session",
            name=entry.name,
        )
        for entry in tdata_entries
    ]
    results = await asyncio.gather(*tasks)

    clients: list[TelegramClient] = []
    names: list[str] = []
    for entry, client in zip(tdata_entries, results):
        if client is not None:
            clients.append(client)
            names.append(entry.name)

    if not clients:
        raise RuntimeError("No usable accounts were loaded from tdata.")

    log.info("Successfully loaded %d/%d account(s).", len(clients), len(tdata_entries))
    return AccountPool(clients, names)
