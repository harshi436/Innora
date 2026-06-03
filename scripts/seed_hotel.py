"""
scripts/seed_hotel.py — Seed a test hotel tenant in MongoDB.

Run once before testing:
  python scripts/seed_hotel.py

This creates a sample hotel document linked to your Twilio number.
Update TWILIO_PHONE_NUMBER and MANAGER_PHONE before running.
"""
import asyncio
import sys
import os
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from database.mongodb import mongo_client
from config import settings


SAMPLE_HOTEL = {
    "hotel_id": "grand_royal_001",
    "name": "Grand Royal Hotel",
    # ⬇ This MUST match your Twilio phone number exactly
    "dialed_number": settings.twilio_phone_number,
    "manager_phone": "+91XXXXXXXXXX",  # ← Replace with real manager phone number
    "address": "123 Main Street, Indore, MP, India",
    "system_prompt": (
        "You are a warm and professional AI concierge for the Grand Royal Hotel in Indore. "
        "Speak naturally as if on a phone call. Keep responses concise — 1-3 sentences. "
        "You can place room service orders, request housekeeping, answer hotel questions, "
        "or transfer to a manager. Always confirm actions clearly with the guest."
    ),
    "amenities": [
        "Swimming Pool (9am-9pm)",
        "Spa & Wellness Center",
        "Restaurant: The Royal Grill (7am-11pm)",
        "Gym (24 hours)",
        "Conference Rooms",
        "Complimentary WiFi",
        "Airport Shuttle (advance booking required)",
    ],
}


async def seed():
    await mongo_client.connect()

    existing = await mongo_client.get_hotel_by_id(SAMPLE_HOTEL["hotel_id"])
    if existing:
        print(f"⚠️  Hotel already exists: {SAMPLE_HOTEL['hotel_id']}")
        print("   Updating dialed_number and system_prompt...")
        await mongo_client.update_hotel(
            SAMPLE_HOTEL["hotel_id"],
            {
                "dialed_number": SAMPLE_HOTEL["dialed_number"],
                "system_prompt": SAMPLE_HOTEL["system_prompt"],
                "amenities": SAMPLE_HOTEL["amenities"],
            },
        )
        print("✅ Hotel updated.")
    else:
        hotel_id = await mongo_client.create_hotel(SAMPLE_HOTEL)
        print(f"✅ Hotel created: {hotel_id}")

    # Verify
    hotel = await mongo_client.get_hotel_by_id(SAMPLE_HOTEL["hotel_id"])
    print(f"\n📋 Hotel record:")
    print(f"   hotel_id     : {hotel['hotel_id']}")
    print(f"   name         : {hotel['name']}")
    print(f"   dialed_number: {hotel['dialed_number']}")
    print(f"   manager_phone: {hotel['manager_phone']}")
    print(f"\n🎉 Ready! Call {hotel['dialed_number']} to test.")

    await mongo_client.disconnect()


if __name__ == "__main__":
    asyncio.run(seed())
