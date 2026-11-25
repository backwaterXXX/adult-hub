"""Errors the Telegram provider raises that callers must tell apart by type.

They live in their own leaf module so the ranges proxy can import them without pulling
the provider (and Telethon) into every module that only needs the type.
"""


class TelegramMessageNotFound(ValueError):
    """The referenced Telegram message is gone, so no byte of it can be served.

    Subclasses ValueError so every existing ``except ValueError`` keeps its old
    behaviour; the HTTP proxy catches it first to answer 404 instead of starting a
    response it cannot finish.
    """
