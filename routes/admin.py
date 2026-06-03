"""
routes/admin.py — Hotel registration and management API.
"""
import os
import uuid
import shutil
import hashlib
from typing import Optional, List

from fastapi import APIRouter, HTTPException, UploadFile, File
from pydantic import BaseModel
from loguru import logger

from database.mongodb import mongo_client
from rag.retrieval_service import ingest_pdf

router = APIRouter(prefix="/admin", tags=["Admin"])
UPLOAD_DIR = "uploads/hotel_pdfs"
os.makedirs(UPLOAD_DIR, exist_ok=True)
MAX_PDFS = 5


class HotelCreate(BaseModel):
    hotel_name: str
    hotel_id: str           # Unique slug e.g. "grand_royal_001"
    hotel_number: str       # Hotel's own landline/mobile
    manager_contact: str    # Manager phone — used for escalations
    hotel_address: str
    hotel_email: str
    password: str
    dialed_number: str      # Twilio number assigned to this hotel
    system_prompt: Optional[str] = ""


class HotelUpdate(BaseModel):
    hotel_name: Optional[str] = None
    hotel_number: Optional[str] = None
    manager_contact: Optional[str] = None
    hotel_address: Optional[str] = None
    hotel_email: Optional[str] = None
    system_prompt: Optional[str] = None


@router.post("/hotels")
async def register_hotel(payload: HotelCreate):
    if await mongo_client.get_hotel_by_id(payload.hotel_id):
        raise HTTPException(409, f"hotel_id '{payload.hotel_id}' already exists")

    data = payload.model_dump()
    data["password"] = hashlib.sha256(payload.password.encode()).hexdigest()
    if not data.get("system_prompt"):
        data["system_prompt"] = (
            f"You are a warm, professional AI concierge for {payload.hotel_name}. "
            "Speak naturally on a phone call — be helpful, concise, and human-like."
        )

    await mongo_client.create_hotel(data)
    logger.info(f"🏨 Hotel registered: {payload.hotel_id}")
    return {"status": "registered", "hotel_id": payload.hotel_id, "hotel_name": payload.hotel_name}


@router.get("/hotels")
async def list_hotels():
    return {"hotels": await mongo_client.list_hotels()}


@router.get("/hotels/{hotel_id}")
async def get_hotel(hotel_id: str):
    h = await mongo_client.get_hotel_by_id(hotel_id)
    if not h:
        raise HTTPException(404, "Hotel not found")
    h.pop("password", None)
    return h


@router.put("/hotels/{hotel_id}")
async def update_hotel(hotel_id: str, payload: HotelUpdate):
    if not await mongo_client.get_hotel_by_id(hotel_id):
        raise HTTPException(404, "Hotel not found")
    data = {k: v for k, v in payload.model_dump().items() if v is not None}
    await mongo_client.update_hotel(hotel_id, data)
    return {"status": "updated", "hotel_id": hotel_id}


@router.post("/hotels/{hotel_id}/upload-pdf")
async def upload_pdfs(hotel_id: str, files: List[UploadFile] = File(...)):
    """Upload 1-5 PDFs. Chunked + embedded into Qdrant. Metadata saved to hotel_pdf."""
    hotel = await mongo_client.get_hotel_by_id(hotel_id)
    if not hotel:
        raise HTTPException(404, "Hotel not found")

    if not 1 <= len(files) <= MAX_PDFS:
        raise HTTPException(400, f"Upload between 1 and {MAX_PDFS} PDFs")

    existing = await mongo_client.count_hotel_pdfs(hotel_id)
    if existing + len(files) > MAX_PDFS:
        raise HTTPException(400, f"Already has {existing} PDFs. Max is {MAX_PDFS}.")

    results = []
    hotel_name = hotel.get("hotel_name") or hotel.get("name", "")

    for f in files:
        if not f.filename.lower().endswith(".pdf"):
            results.append({"filename": f.filename, "status": "skipped", "reason": "Not a PDF"})
            continue

        fname = f"{hotel_id}_{uuid.uuid4().hex[:8]}_{f.filename}"
        fpath = os.path.join(UPLOAD_DIR, fname)
        with open(fpath, "wb") as out:
            shutil.copyfileobj(f.file, out)

        chunks = await ingest_pdf(filepath=fpath, hotel_id=hotel_id)
        await mongo_client.save_hotel_pdf(hotel_id, hotel_name, fname, fpath, chunks)

        results.append({"filename": f.filename, "chunks": chunks, "status": "ingested"})
        logger.info(f"✅ PDF ingested | {hotel_id} | {chunks} chunks")

    return {"hotel_id": hotel_id, "results": results}


# View endpoints
@router.get("/hotels/{hotel_id}/food-orders")
async def food_orders(hotel_id: str):
    return await mongo_client.get_food_orders(hotel_id) or {}

@router.get("/hotels/{hotel_id}/cleaning")
async def cleaning(hotel_id: str):
    return await mongo_client.get_room_cleaning(hotel_id) or {}

@router.get("/hotels/{hotel_id}/spa")
async def spa(hotel_id: str):
    return await mongo_client.get_spa_services(hotel_id) or {}

@router.get("/hotels/{hotel_id}/essentials")
async def essentials(hotel_id: str):
    return await mongo_client.get_essential_needs(hotel_id) or {}

@router.get("/hotels/{hotel_id}/inquiries")
async def inquiries(hotel_id: str):
    return await mongo_client.get_inquiries(hotel_id) or {}

@router.get("/hotels/{hotel_id}/calls")
async def calls(hotel_id: str):
    return await mongo_client.get_call_logs(hotel_id) or {}

@router.get("/hotels/{hotel_id}/pdfs")
async def pdfs(hotel_id: str):
    return await mongo_client.get_hotel_pdfs(hotel_id) or {}