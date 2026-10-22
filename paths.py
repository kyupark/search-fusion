"""Resolve profile-aware state paths when used, rather than at import time."""
from pathlib import Path


def hermes_home() -> Path:
    try:
        from hermes_constants import get_hermes_home
    except ImportError:
        return Path.home() / ".hermes"
    return Path(get_hermes_home())
