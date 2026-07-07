"""Quick script to test VAPID key loading."""
from app.config import get_settings
s = get_settings()
k = s.vapid_private_key
print("Key length:", len(k))
print("First 80 chars repr:", repr(k[:80]))
print("Has backslash-n:", "\\n" in k)
try:
    from py_vapid import Vapid
    v = Vapid.from_string(private_key=k)
    print("VAPID key loaded OK")
except Exception as e:
    print("Error:", e)
