
"""
database/mongodb.py — Async MongoDB client. Database: Hotel

All 8 collections per spec:
  1. hotels
  2. Food_Orders
  3. Room_cleaning
  4. Spa_Services
  5. Essential_Needs
  6. hotel_pdf
  7. call_logs
  8. Inquiry
"""
from motor.motor_asyncio import AsyncIOMotorClient
from pymongo import ASCENDING, DESCENDING
from datetime import datetime
from loguru import logger
from typing import Optional, Dict, List
from bson import ObjectId

from config import settings


class MongoClient:
    def __init__(self):
        self._client: Optional[AsyncIOMotorClient] = None
        self._db = None

    async def connect(self):
        self._client = AsyncIOMotorClient(settings.mongo_uri)
        self._db = self._client[settings.db_name]
        await self._ensure_indexes()
        logger.info(f"✅ MongoDB connected → {settings.db_name}")

    async def disconnect(self):
        if self._client:
            self._client.close()

    async def _ensure_indexes(self):
        await self._db.hotels.create_index([("hotel_id", ASCENDING)], unique=True)
        try:
            await self._db.hotels.create_index(
                [("dialed_number", ASCENDING)],
                unique=True
            )
        except Exception as e:
            logger.warning(f"Index already exists: {e}")
        for col in ["Food_Orders", "Room_cleaning", "Spa_Services", "Essential_Needs", "Inquiry", "call_logs", "hotel_pdf"]:
            await self._db[col].create_index([("hotel_id", ASCENDING)])

    # ─────────────────────────────────────────────
    # hotels
    # ─────────────────────────────────────────────

    async def create_hotel(self, data: Dict) -> str:
        data.setdefault("created_at", datetime.utcnow())
        data.setdefault("updated_at", datetime.utcnow())
        await self._db.hotels.insert_one(data)
        return data["hotel_id"]

    async def get_hotel_by_id(self, hotel_id: str) -> Optional[Dict]:
        doc = await self._db.hotels.find_one({"hotel_id": hotel_id})
        if doc:
            doc["_id"] = str(doc["_id"])
        return doc

    async def get_hotel_by_dialed_number(self, number: str) -> Optional[Dict]:
        """Primary call-routing lookup: Twilio 'To' number → hotel."""
        doc = await self._db.hotels.find_one({"dialed_number": number})
        if doc:
            doc["_id"] = str(doc["_id"])
        return doc

    async def update_hotel(self, hotel_id: str, data: Dict) -> bool:
        data["updated_at"] = datetime.utcnow()
        r = await self._db.hotels.update_one({"hotel_id": hotel_id}, {"$set": data})
        return r.modified_count > 0

    async def list_hotels(self) -> List[Dict]:
        out = []
        async for h in self._db.hotels.find({}, {"password": 0}):
            h["_id"] = str(h["_id"])
            out.append(h)
        return out

    # ─────────────────────────────────────────────
    # Food_Orders
    # ─────────────────────────────────────────────

    async def upsert_food_order(self, hotel_id: str, hotel_name: str,
                                 guest_number: str, guest_room: str, items: List[str],
                                 replace: bool = False) -> str:
        return await self._upsert_service(
            col="Food_Orders",
            hotel_id=hotel_id, hotel_name=hotel_name,
            guest_number=guest_number, guest_room=guest_room,
            field="food_order", values=items,
            replace=replace,
        )

    async def get_food_orders(self, hotel_id: str) -> Optional[Dict]:
        return await self._get_service("Food_Orders", hotel_id)

    # ─────────────────────────────────────────────
    # Room_cleaning
    # ─────────────────────────────────────────────

    async def upsert_room_cleaning(self, hotel_id: str, hotel_name: str,
                                    guest_number: str, guest_room: str, requests: List[str],
                                    replace: bool = False) -> str:
        return await self._upsert_service(
            col="Room_cleaning",
            hotel_id=hotel_id, hotel_name=hotel_name,
            guest_number=guest_number, guest_room=guest_room,
            field="room_cleaning", values=requests,
            replace=replace,
        )

    async def get_room_cleaning(self, hotel_id: str) -> Optional[Dict]:
        return await self._get_service("Room_cleaning", hotel_id)

    # ─────────────────────────────────────────────
    # Spa_Services
    # ─────────────────────────────────────────────

    async def upsert_spa_service(self, hotel_id: str, hotel_name: str,
                                  guest_number: str, guest_room: str, services: List[str],
                                  replace: bool = False) -> str:
        return await self._upsert_service(
            col="Spa_Services",
            hotel_id=hotel_id, hotel_name=hotel_name,
            guest_number=guest_number, guest_room=guest_room,
            field="spa_services", values=services,
            replace=replace,
        )

    async def get_spa_services(self, hotel_id: str) -> Optional[Dict]:
        return await self._get_service("Spa_Services", hotel_id)

    # ─────────────────────────────────────────────
    # Essential_Needs
    # ─────────────────────────────────────────────

    async def upsert_essential_needs(self, hotel_id: str, hotel_name: str,
                                      guest_number: str, guest_room: str, needs: List[str],
                                      replace: bool = False) -> str:
        return await self._upsert_service(
            col="Essential_Needs",
            hotel_id=hotel_id, hotel_name=hotel_name,
            guest_number=guest_number, guest_room=guest_room,
            field="essential_needs", values=needs,
            replace=replace,
        )

    async def get_essential_needs(self, hotel_id: str) -> Optional[Dict]:
        return await self._get_service("Essential_Needs", hotel_id)

    # ─────────────────────────────────────────────
    # Inquiry
    # ─────────────────────────────────────────────

    async def upsert_inquiry(self, hotel_id: str, hotel_name: str,
                              guest_number: str, guest_room: str, question: str) -> str:
        entry = {"question": question, "timestamp": datetime.utcnow()}
        doc = await self._db.Inquiry.find_one({"hotel_id": hotel_id})
        now = datetime.utcnow()
        if doc:
            guests = doc.get("guests", {})
            gkey = self._guest_key(guests, guest_number)
            if gkey in guests:
                guests[gkey].setdefault("inquiry", []).append(entry)
            else:
                guests[gkey] = {"guest_number": guest_number,
                                 "guest_room_number": guest_room, "inquiry": [entry]}
            await self._db.Inquiry.update_one(
                {"hotel_id": hotel_id},
                {"$set": {"guests": guests, "updated_at": now}},
            )
            return str(doc["_id"])
        else:
            r = await self._db.Inquiry.insert_one({
                "hotel_name": hotel_name, "hotel_id": hotel_id,
                "guests": {"guest1": {"guest_number": guest_number,
                                       "guest_room_number": guest_room, "inquiry": [entry]}},
                "created_at": now, "updated_at": now,
            })
            return str(r.inserted_id)

    async def get_inquiries(self, hotel_id: str) -> Optional[Dict]:
        return await self._get_service("Inquiry", hotel_id)

    # ─────────────────────────────────────────────
    # hotel_pdf
    # ─────────────────────────────────────────────

    async def save_hotel_pdf(self, hotel_id: str, hotel_name: str,
                              filename: str, filepath: str, chunk_count: int) -> str:
        entry = {"filename": filename, "filepath": filepath,
                 "chunk_count": chunk_count, "uploaded_at": datetime.utcnow()}
        doc = await self._db.hotel_pdf.find_one({"hotel_id": hotel_id})
        now = datetime.utcnow()
        if doc:
            await self._db.hotel_pdf.update_one(
                {"hotel_id": hotel_id},
                {"$push": {"pdfs": entry}, "$set": {"updated_at": now}},
            )
            return str(doc["_id"])
        else:
            r = await self._db.hotel_pdf.insert_one({
                "hotel_name": hotel_name, "hotel_id": hotel_id,
                "pdfs": [entry], "created_at": now, "updated_at": now,
            })
            return str(r.inserted_id)

    async def count_hotel_pdfs(self, hotel_id: str) -> int:
        doc = await self._db.hotel_pdf.find_one({"hotel_id": hotel_id})
        return len(doc.get("pdfs", [])) if doc else 0

    async def get_hotel_pdfs(self, hotel_id: str) -> Optional[Dict]:
        doc = await self._db.hotel_pdf.find_one({"hotel_id": hotel_id})
        if doc:
            doc["_id"] = str(doc["_id"])
        return doc

    # ─────────────────────────────────────────────
    # call_logs
    # ─────────────────────────────────────────────

    async def create_call_log(self, hotel_id: str, hotel_name: str,
                               guest_number: str, guest_room: str, call_sid: str) -> str:
        """
        Create or find existing call_log doc for this hotel.
        Add/find this guest's entry.
        """
        doc = await self._db.call_logs.find_one({"hotel_id": hotel_id})
        now = datetime.utcnow()
        guest_entry = {
            "guest_phone_number": guest_number,
            "guest_room_number": guest_room,
            "call_sid": call_sid,
            "conversation": [],
            "started_at": now,
        }
        if doc:
            guests = doc.get("guests", {})
            gkey = self._guest_key(guests, guest_number)
            if gkey not in guests:
                guests[gkey] = guest_entry
            await self._db.call_logs.update_one(
                {"hotel_id": hotel_id},
                {"$set": {"guests": guests, "updated_at": now}},
            )
            return str(doc["_id"])
        else:
            r = await self._db.call_logs.insert_one({
                "hotel_name": hotel_name,
                "hotel_id": hotel_id,
                "guests": {"guest1": guest_entry},
                "created_at": now,
                "updated_at": now,
            })
            return str(r.inserted_id)

    async def append_conversation(self, hotel_id: str, guest_number: str,
                                   role: str, message: str) -> bool:
        """
        Append {agent: "..."} or {guest: "..."} to conversation list.
        role must be "agent" or "guest".
        """
        doc = await self._db.call_logs.find_one({"hotel_id": hotel_id})
        if not doc:
            return False
        guests = doc.get("guests", {})
        gkey = self._find_guest_key(guests, guest_number)
        if not gkey:
            return False
        entry = {role: message, "timestamp": datetime.utcnow()}
        await self._db.call_logs.update_one(
            {"hotel_id": hotel_id},
            {
                "$push": {f"guests.{gkey}.conversation": entry},
                "$set": {"updated_at": datetime.utcnow()},
            },
        )
        return True

    async def get_call_logs(self, hotel_id: str) -> Optional[Dict]:
        doc = await self._db.call_logs.find_one({"hotel_id": hotel_id})
        if doc:
            doc["_id"] = str(doc["_id"])
        return doc

    # ─────────────────────────────────────────────
    # Shared helpers
    # ─────────────────────────────────────────────

    async def _upsert_service(self, col: str, hotel_id: str, hotel_name: str,
                               guest_number: str, guest_room: str,
                               field: str, values: List[str],
                               replace: bool = False) -> str:
        """
        Upsert service record for a guest.
        replace=True  → completely replaces existing items with new values (order change)
        replace=False → appends new values to existing (order addition)
        """
        doc = await self._db[col].find_one({"hotel_id": hotel_id})
        now = datetime.utcnow()
        if doc:
            guests = doc.get("guests", {})
            gkey = self._guest_key(guests, guest_number)
            if gkey in guests:
                if replace:
                    # Replace: discard all previous items, use only new values
                    guests[gkey][field] = values
                    logger.info(f"🔄 DB replace | col={col} | guest={guest_number[-4:]}*** | new={values}")
                else:
                    # Append: add to existing
                    existing = guests[gkey].get(field, [])
                    guests[gkey][field] = existing + values
                    logger.info(f"➕ DB append | col={col} | guest={guest_number[-4:]}*** | added={values}")
            else:
                guests[gkey] = {
                    "guest_number": guest_number,
                    "guest_room_number": guest_room,
                    field: values,
                }
            await self._db[col].update_one(
                {"hotel_id": hotel_id},
                {"$set": {"guests": guests, "updated_at": now}},
            )
            return str(doc["_id"])
        else:
            r = await self._db[col].insert_one({
                "hotel_name": hotel_name, "hotel_id": hotel_id,
                "guests": {"guest1": {
                    "guest_number": guest_number,
                    "guest_room_number": guest_room,
                    field: values,
                }},
                "created_at": now, "updated_at": now,
            })
            return str(r.inserted_id)

    async def _get_service(self, col: str, hotel_id: str) -> Optional[Dict]:
        doc = await self._db[col].find_one({"hotel_id": hotel_id})
        if doc:
            doc["_id"] = str(doc["_id"])
        return doc

    def _guest_key(self, guests: Dict, number: str) -> str:
        """Return existing key for this guest number, or next available key."""
        existing = self._find_guest_key(guests, number)
        if existing:
            return existing
        return f"guest{len(guests) + 1}"

    def _find_guest_key(self, guests: Dict, number: str) -> Optional[str]:
        for k, v in guests.items():
            if v.get("guest_number") == number or v.get("guest_phone_number") == number:
                return k
        return None


mongo_client = MongoClient()



