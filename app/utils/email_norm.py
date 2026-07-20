"""Normalize / validate login emails."""

from __future__ import annotations

import re

_EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")


def normalize_email(raw: str | None) -> str:
    return (raw or "").strip().lower()


def is_valid_email(email: str) -> bool:
    return bool(email) and len(email) <= 255 and bool(_EMAIL_RE.match(email))


def username_from_email(email: str) -> str:
    """Stable username derived from email (unique via email uniqueness)."""
    local = email.split("@", 1)[0].strip() or "user"
    # Keep username column constraints (2–64); use full email if local is too short.
    candidate = local[:64]
    if len(candidate) < 2:
        candidate = email[:64]
    return candidate
