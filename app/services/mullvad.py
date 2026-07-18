"""Generate + register a Mullvad WireGuard device for gluetun.

Used by Admin → Integrations when a Mullvad account number is saved, and by
scripts/mullvad_register_wg.py for one-shot WireGuard registration.
"""

from __future__ import annotations

import base64
import json
import logging
import urllib.error
import urllib.request

logger = logging.getLogger(__name__)


def _gen_keypair() -> tuple[str, str]:
    from cryptography.hazmat.primitives.asymmetric.x25519 import X25519PrivateKey
    from cryptography.hazmat.primitives import serialization

    priv = X25519PrivateKey.generate()
    priv_bytes = priv.private_bytes(
        encoding=serialization.Encoding.Raw,
        format=serialization.PrivateFormat.Raw,
        encryption_algorithm=serialization.NoEncryption(),
    )
    pub_bytes = priv.public_key().public_bytes(
        encoding=serialization.Encoding.Raw,
        format=serialization.PublicFormat.Raw,
    )
    return base64.b64encode(priv_bytes).decode(), base64.b64encode(pub_bytes).decode()


def register_wireguard(account_digits: str) -> tuple[str, str]:
    """Register a new WG key with Mullvad. Returns (private_key, ipv4/32)."""
    acct = "".join(c for c in account_digits if c.isdigit())
    if len(acct) != 16:
        raise ValueError("Mullvad account number must be 16 digits")

    priv_b64, pub_b64 = _gen_keypair()

    # Legacy /wg endpoint — returns assigned IPv4 (+ optional IPv6).
    data = f"account={acct}&pubkey={pub_b64}".encode()
    req = urllib.request.Request("https://api.mullvad.net/wg/", data=data, method="POST")
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            raw = resp.read().decode().strip()
    except urllib.error.HTTPError as e:
        detail = e.read().decode()[:300]
        raise RuntimeError(f"Mullvad WireGuard registration failed ({e.code}): {detail}") from e

    # Response is often "10.x.x.x/32,fc00:…/128" — gluetun wants the IPv4 CIDR.
    ipv4 = raw.split(",")[0].strip()
    if "/" not in ipv4:
        ipv4 = f"{ipv4}/32"
    if not ipv4.startswith("10."):
        raise RuntimeError(f"Unexpected Mullvad WG address response: {raw!r}")
    return priv_b64, ipv4


def write_gluetun_env(path: str, *, private_key: str, addresses: str, account: str = "") -> None:
    """Write env file consumed by docker compose / gluetun."""
    from pathlib import Path

    p = Path(path)
    p.parent.mkdir(parents=True, exist_ok=True)
    lines = [
        f"WIREGUARD_PRIVATE_KEY={private_key}",
        f"WIREGUARD_ADDRESSES={addresses}",
    ]
    if account:
        lines.append(f"MULLVAD_ACCOUNT_NUMBER={account}")
    p.write_text("\n".join(lines) + "\n", encoding="utf-8")
