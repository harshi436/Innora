"""
test.py — Trigger an outbound AI call to a guest.

Usage:
  python test.py +91XXXXXXXXXX
"""

import sys
import os
from dotenv import load_dotenv
from twilio.rest import Client

load_dotenv()

# =========================
# NUMBER
# =========================

to_number = sys.argv[1] if len(sys.argv) > 1 else "+917580966465"

# =========================
# ENV
# =========================

account_sid = os.getenv("TWILIO_ACCOUNT_SID")
auth_token = os.getenv("TWILIO_AUTH_TOKEN")
from_number = os.getenv("TWILIO_PHONE_NUMBER")
ngrok_url = os.getenv("NGROK_URL", "").rstrip("/")

if not all([account_sid, auth_token, from_number, ngrok_url]):
    print("❌ Missing .env values")
    sys.exit(1)

# =========================
# CLIENT
# =========================

client = Client(account_sid, auth_token)

# =========================
# CREATE CALL
# =========================

call = client.calls.create(
    to=to_number,
    from_=from_number,
    url=f"{ngrok_url}/incoming-call",
    method="POST",
)

# =========================
# OUTPUT
# =========================

print("\n✅ AI Call Initiated")
print(f"To      : {to_number}")
print(f"From    : {from_number}")
print(f"Call SID: {call.sid}")
print(f"Webhook : {ngrok_url}/incoming-call")
print("\n📱 Phone should ring in 5-10 seconds")
print("📡 Watch uvicorn terminal logs")