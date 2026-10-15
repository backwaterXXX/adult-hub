"""Telegram account session setup and authenticated Telethon client access."""

from __future__ import annotations

import asyncio
import os
from typing import Optional


async def require_client(session_state: dict, *, api_id_fn, api_hash_fn, session_path_fn):
    """Connect the singleton client and require an already authorized session."""
    try:
        from telethon import TelegramClient
    except ImportError as exc:
        raise RuntimeError(
            "Telethon is not installed. Add 'telethon>=1.36' to "
            "resolver/requirements.txt and install it to use the "
            "telegram provider."
        ) from exc
    api_id = api_id_fn()
    api_hash = api_hash_fn()
    if session_state["lock"] is None:
        session_state["lock"] = asyncio.Lock()
    async with session_state["lock"]:
        if session_state["client"] is None:
            session_path = session_path_fn()
            # SQLiteSession creates a file on first connect. Refuse to create a
            # blank session during playback or an admin access check.
            if not os.path.isfile(session_path):
                raise RuntimeError("Telegram session file is missing. Log in first.")
            session_state["client"] = TelegramClient(session_path, api_id, api_hash)
            await session_state["client"].connect()
    try:
        authorized = await session_state["client"].is_user_authorized()
    except Exception as exc:
        raise RuntimeError(
            "Telegram session check failed. Ensure the session file is "
            "present and logged in (see providers.telegram.login)."
        ) from exc
    if not authorized:
        raise RuntimeError(
            "Telegram session is not authorized. Log in first via "
            "providers.telegram.login(phone) (code from SMS), then retry."
        )
    return session_state["client"]


async def login(
    phone: str,
    code: Optional[str] = None,
    password: Optional[str] = None,
    *,
    api_id_fn,
    api_hash_fn,
    session_path_fn,
) -> str:
    """Perform Telegram's two-step SMS login and disconnect the temporary client."""
    try:
        from telethon import TelegramClient
        from telethon.errors import SessionPasswordNeededError
    except ImportError as exc:
        raise RuntimeError(
            "Telethon is not installed. Add 'telethon>=1.36' to "
            "resolver/requirements.txt and install it first."
        ) from exc
    client = TelegramClient(session_path_fn(), api_id_fn(), api_hash_fn())
    await client.connect()
    try:
        if await client.is_user_authorized():
            return "already_authorized"
        if code is None:
            await client.send_code_request(phone)
            return f"code_sent:{phone}"
        try:
            await client.sign_in(phone, code)
        except SessionPasswordNeededError:
            if not password:
                raise RuntimeError(
                    "Two-factor password required: call login() again "
                    "with password set."
                ) from None
            await client.sign_in(password=password)
        return "authorized"
    finally:
        try:
            await client.disconnect()
        except Exception:
            pass
