import asyncio
from app.services.discord import notify_account_request

asyncio.run(notify_account_request("test-user", "Testing webhook setup"))
print("Webhook sent successfully!")
