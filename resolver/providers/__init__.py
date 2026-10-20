"""Public provider registry (Telegram-only).

Only the built-in Telegram provider is registered. Any config that
enables another provider fails fast via
:func:`ensure_config_providers_registered`.
"""

import re
from typing import Dict, List

from .base import BaseProvider
from .telegram import TelegramProvider

# Provider names: non-empty, unique, lowercase letters/digits, no spaces.
_NAME_RE = re.compile(r"^[a-z0-9]+$")

_PROVIDERS: Dict[str, BaseProvider] = {}


def register(provider: BaseProvider) -> BaseProvider:
    """Register a provider instance. Validates type, name and duplicates.

    Raises:
        TypeError: provider is not a ``BaseProvider`` instance.
        ValueError: name empty/invalid (must match ``^[a-z0-9]+$``) or
            already registered.
    """
    if not isinstance(provider, BaseProvider):
        raise TypeError(
            f"Provider must be a BaseProvider instance, "
            f"got {type(provider).__name__}."
        )
    name = getattr(provider, "name", "")
    if not isinstance(name, str) or not name:
        raise ValueError("Provider 'name' must be a non-empty string.")
    if not _NAME_RE.fullmatch(name):
        raise ValueError(
            f"Invalid provider name {name!r}: use lowercase letters/digits "
            f"only, no spaces (regex ^[a-z0-9]+$)."
        )
    if name in _PROVIDERS:
        raise ValueError(f"Provider '{name}' is already registered.")
    _PROVIDERS[name] = provider
    return provider


def get_provider(name: str) -> BaseProvider:
    if name not in _PROVIDERS:
        raise KeyError(
            f"Provider '{name}' not found. Available: {sorted(_PROVIDERS.keys())}"
        )
    return _PROVIDERS[name]


def list_providers() -> Dict[str, BaseProvider]:
    return dict(_PROVIDERS)


register(TelegramProvider())


# ---------------------------------------------------------------------------
# Config vs registry consistency
# ---------------------------------------------------------------------------

def missing_config_providers(config) -> List[str]:
    """Enabled providers from config that are NOT registered (sorted).

    ``config`` is an AppConfig (or any object with ``.sources`` mapping
    names to objects with ``.enabled``). Returns [] when ``config`` is None.
    """
    if config is None:
        return []
    sources = getattr(config, "sources", {}) or {}
    missing: List[str] = []
    for raw_name, src in sources.items():
        name = str(raw_name)
        try:
            enabled = bool(src.enabled)
        except AttributeError:
            enabled = True
        if enabled and name not in _PROVIDERS:
            missing.append(name)
    return sorted(missing)


def ensure_config_providers_registered(config) -> None:
    """Raise ValueError if config enables a provider that is not registered."""
    missing = missing_config_providers(config)
    if missing:
        raise ValueError(
            "Configuration enables provider(s) that are not registered: "
            + ", ".join(missing)
            + ". Available: "
            + ", ".join(sorted(_PROVIDERS.keys()))
        )
