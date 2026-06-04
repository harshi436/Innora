"""
whatsapp/whatsapp_service.py

WhatsApp integration for Hotel AI Voice Assistant.

FEATURES:
  ✅ Post-call summary sent to guest via WhatsApp
     — Order items + total amount + thank-you message
  ✅ Inbound WhatsApp messages handled (hotel-only)
     — Same LLM pipeline as voice, but text-based
     — "Call me" / "Agent se baat karo" → triggers outbound Twilio call
  ✅ Final order confirmation message after guest replies
  ✅ Fire-and-forget send (asyncio.create_task) → zero latency impact on call flow

DESIGN:
  - Uses existing TWILIO_ACCOUNT_SID / TWILIO_AUTH_TOKEN / TWILIO_PHONE_NUMBER
  - Automated sandbox fallback protects against Channel 400 errors.
  - All LLM responses go through qwen_service (same as voice)
  - Redis stores per-guest WhatsApp conversation state (7-day TTL)
  - MongoDB stores inbound/outbound WhatsApp messages in call_logs
"""

import asyncio
import json
from typing import Optional, List, Dict

import aiohttp
from loguru import logger

from config import settings
from database.redis_client import redis_client
from database.mongodb import mongo_client
from agents.qwen_service import qwen_service
from rag.retrieval_service import retrieval_service


# ── Constants ─────────────────────────────────────────────────────────────────

TWILIO_MESSAGES_URL = (
    f"https://api.twilio.com/2010-04-01/Accounts/"
    f"{settings.twilio_account_sid}/Messages.json"
)

# ⚡ OMNICHANNEL FORMATTING FIX
# Outbound calls demand the original settings phone carrier format
raw_number = str(settings.twilio_phone_number).strip()
clean_from_number = raw_number if raw_number.startswith("+") else f"+{raw_number}"

# 🛠️ AUTOMATED SANDBOX ROUTING ENHANCEMENT
# Replaces dynamic reference with standard Twilio Global Sandbox channel (+14155238886)
# if twilio_whatsapp_sender variable is not declared in environment variables.
if hasattr(settings, "twilio_whatsapp_sender") and settings.twilio_whatsapp_sender:
    raw_wa = str(settings.twilio_whatsapp_sender).strip()
    clean_wa = raw_wa if raw_wa.startswith("+") else f"+{raw_wa}"
    _WA_FROM = f"whatsapp:{clean_wa}"
else:
    _WA_FROM = "whatsapp:+14155238886"

# Redis TTL for WhatsApp conversation state (7 days)
WA_SESSION_TTL = 7 * 24 * 3600

# Max history turns to inject into LLM for WhatsApp replies
WA_MAX_HISTORY = 6

# Intents that should trigger an outbound call back instantly
_CALL_REQUEST_PATTERNS = [
    "call me", "call kar", "call karo", "phone karo", "call back", "firse call", "dubara call",
    "agent se baat", "manager se baat", "connect karo", "transfer karo", "phone lagao",
    "baat karni hai", "call chahiye", "please call", "mujhe call", "firse phone",
    "speak to someone", "talk to someone", "human se baat", "call me back"
]


# ─────────────────────────────────────────────────────────────────────────────
# LOW-LEVEL SEND ENGINE
# ─────────────────────────────────────────────────────────────────────────────

async def _send_whatsapp(to_number: str, body: str) -> bool:
    """
    Send a WhatsApp message via Twilio REST API.
    to_number must be E.164 format e.g. +919876543210
    Returns True on success.
    """
    clean_to = to_number.strip()
    if not clean_to.startswith("+"):
        clean_to = f"+{clean_to}"
        
    wa_to = f"whatsapp:{clean_to}"
    
    try:
        async with aiohttp.ClientSession() as session:
            async with session.post(
                TWILIO_MESSAGES_URL,
                data={
                    "From": _WA_FROM,
                    "To":   wa_to,
                    "Body": body,
                },
                auth=aiohttp.BasicAuth(
                    settings.twilio_account_sid,
                    settings.twilio_auth_token,
                ),
                timeout=aiohttp.ClientTimeout(total=10, connect=3),
            ) as resp:
                if resp.status in (200, 201):
                    logger.info(f"✅ WhatsApp sent | from={_WA_FROM} to={clean_to[-4:]}*** | {len(body)} chars")
                    return True
                else:
                    body_text = await resp.text()
                    logger.error(f"WhatsApp send failed {resp.status}: {body_text[:200]}")
                    return False
    except asyncio.TimeoutError:
        logger.warning("WhatsApp send timeout")
        return False
    except Exception as e:
        logger.error(f"WhatsApp send error: {e}")
        return False


# ─────────────────────────────────────────────────────────────────────────────
# POST-CALL SUMMARY GENERATION
# ─────────────────────────────────────────────────────────────────────────────

async def send_post_call_summary(
    guest_number: str,
    hotel_id: str,
    hotel_name: str,
    order_items: List[str],
    manager_contact: str = "",
) -> bool:
    """
    Send a WhatsApp thank-you + order summary after the call ends.
    Called as asyncio.create_task() from websocket_server — non-blocking.
    """
    if not guest_number:
        logger.warning("WhatsApp: empty guest number received")
        return False

    # ── Build order summary section ───────────────────────────────────────────
    order_section = ""
    if order_items:
        items_text = "\n".join(f"  • {item}" for item in order_items)
        order_section = f"\n\n🛎️ *Your Order:*\n{items_text}"

        try:
            price_context = await retrieval_service.search(
                " ".join(order_items), hotel_id, top_k=3
            )
            if price_context and len(price_context.strip()) > 20:
                price_prompt = (
                    f"Based on the hotel menu below, calculate the total price for these items: "
                    f"{', '.join(order_items)}.\n\n"
                    f"Menu information:\n{price_context}\n\n"
                    f"Return ONLY a short line like 'Total: ₹X' or 'Total amount: ₹X'. "
                    f"If prices are not clearly available, return empty string."
                )
                total_line = await qwen_service.get_full_response(
                    messages=[{"role": "user", "content": price_prompt}],
                    hotel_id=hotel_id,
                    hotel_name=hotel_name,
                    system_prompt="You are a hotel billing assistant. Return only the total price line.",
                )
                total_line = total_line.strip()
                if total_line and ("₹" in total_line or "total" in total_line.lower()):
                    order_section += f"\n\n💰 *{total_line}*"
        except Exception as e:
            logger.warning(f"Price fetch error: {e}")

    # ── Build clean full Hinglish template layout ─────────────────────────────
    message = (
        f"🙏 *{hotel_name}* mein aapka dhanyawad!\n\n"
        f"Aapki call complete ho gayi hai. Hum aapki seva karke khush hain."
        f"{order_section}\n\n"
        f"Agar aapko kuch aur chahiye, yahaan message karein — "
        f"hum turant reply karenge! 😊"
    )
    
    if manager_contact:
        message += f"\n\nKisi bhi zaroorat ke liye:\n📞 Manager: {manager_contact}"

    success = await _send_whatsapp(guest_number, message)

    # ── Save to Redis state management tracker ────────────────────────────────
    if success:
        try:
            wa_key = f"wa_session:{hotel_id}:{guest_number}"
            existing = await redis_client.get(wa_key)
            session_data = json.loads(existing) if existing else {}
            session_data.update({
                "hotel_id":       hotel_id,
                "hotel_name":     hotel_name,
                "manager_contact": manager_contact,
                "summary_sent":   True,
                "order_items":    order_items,
                "history":        [],
            })
            await redis_client.set(wa_key, json.dumps(session_data), ttl=WA_SESSION_TTL)
        except Exception as e:
            logger.warning(f"WhatsApp session save error: {e}")

    return success


# ─────────────────────────────────────────────────────────────────────────────
# FINAL ORDER CONFIRMATION
# ─────────────────────────────────────────────────────────────────────────────

async def send_final_order_confirmation(
    guest_number: str,
    hotel_id: str,
    hotel_name: str,
    order_items: List[str],
) -> bool:
    """Send final confirmed order confirmation pipeline layout"""
    if not order_items:
        return False

    items_text = "\n".join(f"  ✅ {item}" for item in order_items)
    message = (
        f"✅ *Final Order Confirmed — {hotel_name}*\n\n"
        f"{items_text}\n\n"
        f"Aapka order confirmed hai! Hum jald hi deliver karenge. 🚀\n"
        f"Koi aur zaroorat ho toh batayein!"
    )
    return await _send_whatsapp(guest_number, message)


# ─────────────────────────────────────────────────────────────────────────────
# INBOUND MESSAGE WEBHOOK ENGINE
# ─────────────────────────────────────────────────────────────────────────────

def _is_call_request(text: str) -> bool:
    """Evaluate intents dynamically to extract call-back commands"""
    text_lower = text.lower().strip()
    return any(pattern in text_lower for pattern in _CALL_REQUEST_PATTERNS)


async def _get_wa_history(guest_number: str, hotel_id: str) -> List[Dict]:
    """Load WhatsApp logs history metrics directly out of Redis keys"""
    try:
        wa_key = f"wa_session:{hotel_id}:{guest_number}"
        raw = await redis_client.get(wa_key)
        if raw:
            data = json.loads(raw)
            return data.get("history", [])[-WA_MAX_HISTORY:]
    except Exception as e:
        logger.warning(f"WA history load error: {e}")
    return []


async def _save_wa_turn(guest_number: str, hotel_id: str, hotel_name: str, manager_contact: str, role: str, content: str):
    """Saves session execution transactions context into memory streams"""
    try:
        wa_key = f"wa_session:{hotel_id}:{guest_number}"
        raw = await redis_client.get(wa_key)
        session_data = json.loads(raw) if raw else {
            "hotel_id":        hotel_id,
            "hotel_name":      hotel_name,
            "manager_contact": manager_contact,
            "summary_sent":    False,
            "order_items":     [],
            "history":         [],
        }
        history = session_data.get("history", [])
        history.append({"role": role, "content": content})
        session_data["history"] = history[-20:]
        await redis_client.set(wa_key, json.dumps(session_data), ttl=WA_SESSION_TTL)
    except Exception as e:
        logger.warning(f"WA session save turn error: {e}")


async def _trigger_outbound_call(guest_number: str, hotel_id: str) -> bool:
    """Fires instantaneous automated telephonic outbound connection"""
    try:
        ngrok_base  = settings.ngrok_url.rstrip("/")
        webhook_url = f"{ngrok_base}/incoming-call"

        async with aiohttp.ClientSession() as session:
            async with session.post(
                f"https://api.twilio.com/2010-04-01/Accounts/{settings.twilio_account_sid}/Calls.json",
                data={
                    "To":     guest_number,
                    "From":   clean_from_number, # Reuses original twilio voice number
                    "Url":    webhook_url,
                    "Method": "POST",
                },
                auth=aiohttp.BasicAuth(
                    settings.twilio_account_sid,
                    settings.twilio_auth_token,
                ),
                timeout=aiohttp.ClientTimeout(total=10, connect=3),
            ) as resp:
                if resp.status in (200, 201):
                    logger.info(f"📞 Outbound call triggered | to={guest_number[-4:]}***")
                    return True
                else:
                    body = await resp.text()
                    logger.error(f"Outbound call failed {resp.status}: {body[:200]}")
                    return False
    except Exception as e:
        logger.error(f"Outbound call trigger error: {e}")
        return False


async def handle_inbound_whatsapp(from_number: str, body: str, to_number: str) -> str:
    """Main Omnichannel Core Routing Hub"""
    guest_number = from_number.replace("whatsapp:", "").strip()
    hotel_wa_num = to_number.replace("whatsapp:", "").strip()

    logger.info(f"📲 WA inbound | from={guest_number[-4:]}*** | msg={body[:60]}")

    # ── Load hotel ────────────────────────────────────────────────────────────
    hotel = await mongo_client.get_hotel_by_dialed_number(hotel_wa_num)
    
    # 🔥 ULTRA LOCAL OVERRIDE FALLBACK FOR SANDBOX TESTING (Zero DB Error Dependencies)
    if not hotel:
        logger.warning(f"WA: no hotel for number {hotel_wa_num}. Using local fallback context override for Taj Hotel (Shri_003)")
        hotel = {
            "hotel_id": "Shri_003",
            "hotel_name": "Taj",
            "manager_contact": "9327279077",
            "system_prompt": "You are a professional, warm, and helpful AI concierge for SHRI MAYA. Answer accurately and keep responses brief."
        }

    hotel_id        = hotel.get("hotel_id", "")
    hotel_name      = hotel.get("hotel_name") or hotel.get("name", "Hotel")
    manager_contact = hotel.get("manager_contact") or hotel.get("manager_phone", "")
    system_prompt   = hotel.get("system_prompt", "")

    wa_key = f"wa_session:{hotel_id}:{guest_number}"
    raw_session = await redis_client.get(wa_key)
    session_data = json.loads(raw_session) if raw_session else {}
    summary_sent      = session_data.get("summary_sent", False)
    order_items       = session_data.get("order_items", [])
    first_reply_after = summary_sent and not session_data.get("first_reply_done", False)

    await _save_wa_turn(guest_number, hotel_id, hotel_name, manager_contact, "user", body)

    # 🚀 CONDITIONAL INTERCEPT: Outbound call triggering structure loop
    if _is_call_request(body):
        logger.info(f"📞 WA call request | guest={guest_number[-4:]}***")
        call_triggered = await _trigger_outbound_call(guest_number, hotel_id)

        if call_triggered:
            reply = (
                f"✅ Hum aapko abhi call kar rahe hain! 📞\n"
                f"Kripya apna phone ready rakhein — "
                f"{clean_from_number} se call aayegi."
            )
        else:
            reply = (
                f"😔 Call trigger mein thodi dikkat aayi. Kripya seedha manager ko call karein:\n"
                f"📞 {manager_contact}"
            ) if manager_contact else "😔 Call trigger mein thodi dikkat aayi. Kripya hotel reception se sampark karein."

        await _save_wa_turn(guest_number, hotel_id, hotel_name, manager_contact, "assistant", reply)
        return reply

    # Confirm list tracks if customer engages text stream back
    if first_reply_after and order_items:
        asyncio.create_task(
            send_final_order_confirmation(guest_number, hotel_id, hotel_name, order_items)
        )
        try:
            session_data["first_reply_done"] = True
            await redis_client.set(wa_key, json.dumps(session_data), ttl=WA_SESSION_TTL)
        except Exception:
            pass

    history = await _get_wa_history(guest_number, hotel_id)

    rag_context = ""
    try:
        rag_context = await retrieval_service.search(body, hotel_id, top_k=3)
    except Exception as e:
        logger.warning(f"WA RAG error: {e}")

    # Build system text configurations context instructions for LLM
    wa_system_prompt = (
        f"{system_prompt}\n\n"
        "You are replying via WhatsApp text message — NOT on a phone call. "
        "You may use 2-4 sentences. Use simple formatting (bold with * is OK). "
        "You ONLY answer hotel-related questions: food, room service, spa, "
        "cleaning, amenities, timings, policies, menu. "
        "For non-hotel questions, politely decline and redirect to hotel services. "
        "Do NOT mention AI or language models. "
        f"Manager contact: {manager_contact}"
    )

    messages = history + [{"role": "user", "content": body}]

    # 📝 CONDITIONAL CHAT GENERATION VIA QWEN ENGINE
    try:
        reply = await qwen_service.get_full_response(
            messages=messages,
            hotel_id=hotel_id,
            hotel_name=hotel_name,
            system_prompt=wa_system_prompt,
            rag_context=rag_context,
            manager_contact=manager_contact,
        )
    except Exception as e:
        logger.error(f"WA LLM error: {e}")
        reply = (
            f"Maafi chahte hain, abhi thodi technical dikkat hai. "
            f"Kripya manager se sampark karein: {manager_contact}"
        ) if manager_contact else "Maafi chahte hain, abhi thodi technical dikkat hai. Kripya hotel reception se sampark karein."

    await _save_wa_turn(guest_number, hotel_id, hotel_name, manager_contact, "assistant", reply)
    logger.info(f"📲 WA reply | to={guest_number[-4:]}*** | {reply[:60]}")
    return reply