"""
scripts/register_hotel.py — Terminal-based hotel registration.

Run:
  python scripts/register_hotel.py

This script prompts for all hotel details and saves them to MongoDB.
After registration it optionally uploads PDFs (1-5).
"""
import asyncio
import sys
import os
import getpass
import shutil
import uuid

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from database.mongodb import mongo_client
from rag.retrieval_service import ingest_pdf

UPLOAD_DIR = "uploads/hotel_pdfs"
os.makedirs(UPLOAD_DIR, exist_ok=True)


def prompt(label: str, required: bool = True, secret: bool = False) -> str:
    while True:
        if secret:
            value = getpass.getpass(f"  {label}: ")
        else:
            value = input(f"  {label}: ").strip()
        if value or not required:
            return value
        print(f"    ⚠  {label} is required.")


async def register():
    await mongo_client.connect()

    print("\n" + "=" * 55)
    print("  🏨  HOTEL AI VOICE ASSISTANT — Hotel Registration")
    print("=" * 55 + "\n")

    hotel_name    = prompt("Hotel Name (e.g. Grand Royal Hotel)")
    hotel_id      = prompt("Hotel ID / Slug (e.g. grand_royal_001) — must be unique")
    hotel_number  = prompt("Hotel Contact Number (e.g. +911234567890)")
    manager_contact = prompt("Manager Contact Number (e.g. +919876543210)")
    hotel_address = prompt("Hotel Address")
    hotel_email   = prompt("Hotel Email")
    password      = prompt("Password", secret=True)
    dialed_number = prompt("Twilio Number assigned to this hotel (e.g. +17479665797)")

    # Check uniqueness
    existing = await mongo_client.get_hotel_by_id(hotel_id)
    if existing:
        print(f"\n❌ hotel_id '{hotel_id}' is already registered. Choose a different ID.")
        await mongo_client.disconnect()
        return

    import hashlib
    hotel_data = {
        "hotel_name": hotel_name,
        "hotel_id": hotel_id,
        "hotel_number": hotel_number,
        "manager_contact": manager_contact,
        "hotel_address": hotel_address,
        "hotel_email": hotel_email,
        "password": hashlib.sha256(password.encode()).hexdigest(),
        "dialed_number": dialed_number,
        "system_prompt": (
            f"You are a professional, warm, and helpful AI concierge for {hotel_name}. "
            "Respond naturally on a phone call — concise, clear, and human-like. "
            "Assist with food orders, room cleaning, spa services, essential needs, and hotel inquiries. "
            "For events or matters outside your scope, provide the manager's contact number."
        ),
    }

    await mongo_client.create_hotel(hotel_data)
    print(f"\n✅ Hotel '{hotel_name}' registered successfully!")
    print(f"   hotel_id     : {hotel_id}")
    print(f"   Twilio number: {dialed_number}")

    # ── PDF Upload ───────────────────────────────────────────
    print("\n" + "-" * 55)
    print("  📄  Upload Hotel Knowledge Base PDFs (1–5 PDFs)")
    print("-" * 55)
    print("  You must upload at least 1 PDF and at most 5 PDFs.")
    print("  These PDFs will be used by the AI to answer guest queries.\n")

    pdf_paths = []
    while len(pdf_paths) < 5:
        path = input(
            f"  PDF path {len(pdf_paths) + 1}"
            f"{'  (or press Enter to finish)' if pdf_paths else ''}: "
        ).strip()

        if not path:
            if not pdf_paths:
                print("  ⚠  You must upload at least 1 PDF.")
                continue
            break

        if not os.path.isfile(path):
            print(f"  ❌ File not found: {path}")
            continue

        if not path.lower().endswith(".pdf"):
            print(f"  ❌ Only PDF files are accepted.")
            continue

        pdf_paths.append(path)
        print(f"  ✓ Added: {path}")

        if len(pdf_paths) == 5:
            print("  ℹ  Maximum of 5 PDFs reached.")
            break

    # Process each PDF
    print(f"\n  Processing {len(pdf_paths)} PDF(s)...")
    for pdf_path in pdf_paths:
        filename = f"{hotel_id}_{uuid.uuid4().hex[:8]}_{os.path.basename(pdf_path)}"
        dest = os.path.join(UPLOAD_DIR, filename)
        shutil.copy2(pdf_path, dest)

        chunk_count = await ingest_pdf(filepath=dest, hotel_id=hotel_id)
        await mongo_client.save_hotel_pdf(
            hotel_id=hotel_id,
            hotel_name=hotel_name,
            filename=filename,
            filepath=dest,
            chunk_count=chunk_count,
        )
        print(f"  ✅ {os.path.basename(pdf_path)} → {chunk_count} chunks stored in Qdrant")

    print("\n" + "=" * 55)
    print(f"  🎉 Setup complete for '{hotel_name}'")
    print(f"  Call {dialed_number} to test the AI concierge.")
    print("=" * 55 + "\n")

    await mongo_client.disconnect()


if __name__ == "__main__":
    asyncio.run(register())
