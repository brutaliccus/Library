#!/usr/bin/env python3
"""Register a WireGuard key with Mullvad and print gluetun env vars."""
from __future__ import annotations

import base64
import json
import os
import sys
import urllib.error
import urllib.request

try:
    from cryptography.hazmat.primitives.asymmetric.x25519 import X25519PrivateKey
    from cryptography.hazmat.primitives import serialization
except ImportError:
    print("MISSING_CRYPTO", file=sys.stderr)
    sys.exit(2)


def main() -> int:
    acct = "".join(c for c in os.environ.get("MULLVAD_ACCOUNT", "") if c.isdigit())
    if len(acct) != 16:
        print(f"bad account length {len(acct)}", file=sys.stderr)
        return 1

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
    priv_b64 = base64.b64encode(priv_bytes).decode()
    pub_b64 = base64.b64encode(pub_bytes).decode()

    # Modern Mullvad app API
    req = urllib.request.Request(
        "https://api.mullvad.net/app/v1/devices",
        data=json.dumps({"pubkey": pub_b64}).encode(),
        headers={
            "Content-Type": "application/json",
            "Authorization": f"Token {acct}",
        },
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            body = json.loads(resp.read().decode())
            print("MODE=devices")
            print(json.dumps(body, indent=2))
            ipv4 = None
            addrs = body.get("ipv4_address") or body.get("ipv4Address")
            if addrs:
                ipv4 = addrs if "/" in str(addrs) else f"{addrs}/32"
            if not ipv4 and isinstance(body.get("addresses"), dict):
                ipv4 = body["addresses"].get("ipv4")
            if ipv4:
                print(f"WIREGUARD_PRIVATE_KEY={priv_b64}")
                print(f"WIREGUARD_ADDRESSES={ipv4}")
                return 0
    except urllib.error.HTTPError as e:
        print(f"devices_http={e.code} {e.read().decode()[:500]}", file=sys.stderr)
    except Exception as e:
        print(f"devices_err={e!r}", file=sys.stderr)

    # Legacy /wg endpoint returns assigned IPv4
    data = f"account={acct}&pubkey={pub_b64}".encode()
    req2 = urllib.request.Request("https://api.mullvad.net/wg/", data=data, method="POST")
    try:
        with urllib.request.urlopen(req2, timeout=30) as resp:
            ip = resp.read().decode().strip()
            print("MODE=legacy_wg")
            print(f"WIREGUARD_PRIVATE_KEY={priv_b64}")
            print(f"WIREGUARD_ADDRESSES={ip if '/' in ip else ip + '/32'}")
            return 0
    except urllib.error.HTTPError as e:
        print(f"legacy_http={e.code} {e.read().decode()[:500]}", file=sys.stderr)
    except Exception as e:
        print(f"legacy_err={e!r}", file=sys.stderr)

    return 1


if __name__ == "__main__":
    raise SystemExit(main())
