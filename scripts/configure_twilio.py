"""
scripts/configure_twilio.py — Auto-configure Twilio webhook URL.

Run after starting ngrok:
  python scripts/configure_twilio.py
"""
import sys
import os
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from twilio.rest import Client
from config import settings


def configure():
    client = Client(settings.twilio_account_sid, settings.twilio_auth_token)

    webhook_url = f"{settings.ngrok_url}/incoming-call"

    numbers = client.incoming_phone_numbers.list()
    target = None
    for num in numbers:
        if num.phone_number == settings.twilio_phone_number:
            target = num
            break

    if not target:
        print(f"❌ Phone number {settings.twilio_phone_number} not found.")
        print("   Available numbers:")
        for n in numbers:
            print(f"     {n.phone_number}")
        sys.exit(1)

    target.update(
        voice_url=webhook_url,
        voice_method="POST",
    )

    print(f"✅ Twilio configured!")
    print(f"   Phone : {settings.twilio_phone_number}")
    print(f"   Webhook: {webhook_url}")


if __name__ == "__main__":
    configure()
