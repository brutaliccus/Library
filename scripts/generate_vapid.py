#!/usr/bin/env python3
"""Generate VAPID keys for Web Push. Add these to your .env file."""
import sys

try:
    from py_vapid import Vapid
    from py_vapid.utils import b64urlencode
    from cryptography.hazmat.primitives import serialization
except ImportError:
    print("Install py-vapid: pip install py-vapid")
    sys.exit(1)

vapid = Vapid()
vapid.generate_keys()

priv_pem = vapid.private_pem().decode()
pub_raw = vapid.public_key.public_bytes(
    serialization.Encoding.X962,
    serialization.PublicFormat.UncompressedPoint,
)
pub_b64 = b64urlencode(pub_raw)
if isinstance(pub_b64, bytes):
    pub_b64 = pub_b64.decode()

print("Add these to your .env file:")
print()
# PEM as single line for .env (newlines as literal \n)
priv_escaped = priv_pem.strip().replace("\n", "\\n")
print(f'VAPID_PRIVATE_KEY="{priv_escaped}"')
print(f"VAPID_PUBLIC_KEY={pub_b64}")
print()
