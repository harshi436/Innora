"""
websocket/websocket_server.py

FIXES v14:
   ✅ Pure STT High-Speed Barge-In with zero noise/cough false positives.
   ✅ Fixed LLM Memory Amnesia: Direct injection of live dynamic order array into the LLM system prompt context loop.
   ✅ No more hallucinations: Agent strictly remembers items present in PDF context and dynamically added during the call session.
   ✅ Mitigated ASGI WebSocket closed state flooding errors.
"""

import asyncio
import audioop
import base64
import json
import re
import time
from typing import Optional, List, Dict

import httpx
from fastapi import APIRouter, WebSocket, WebSocketDisconnect
from loguru import logger

from agents.qwen_service import qwen_service
from database.mongodb import mongo_client
from database.redis_client import redis_client
from speech.deepgram_service import DeepgramService
from tts.tts_service import tts_service, WordStreamBuffer
from tts.Greeting_cache import (
    get_cached_greeting_b64,
    get_cached_greeting_text,
    pre_warm_greeting,
    set_cached_greeting,
)
from rag.retrieval_service import retrieval_service
from utils.email_sender import send_email_async
from config import settings    
from whatsapp.whatsapp_service import send_post_call_summary     #this is for whatsapp setting 

router = APIRouter()

MIN_TRANSCRIPT_LEN    = 2
MIN_WORD_COUNT        = 1
DEBOUNCE_DELAY        = 0.05
GREETING_GRACE_SEC    = 3.5

BARGE_IN_COOLDOWN_SEC  = 1.0
BARGE_IN_TRANSCRIPT_MIN_CHARS = 2
MAX_PLAYBACK_TRACK_SEC = 8.0

ROOM_REQUIRED_INTENTS = {"food_order", "room_cleaning", "spa_service", "essential_needs"}

NO_RAG_INTENTS = {"farewell", "escalation", "event_inquiry", "small_talk", "greeting"}

RAG_INTENTS = {"food_order", "room_cleaning", "spa_service", "essential_needs", "inquiry"}

_FOOD_KEYWORDS = {
    'rice', 'roti', 'pizza', 'burger', 'naan', 'biryani',
    'sandwich', 'coffee', 'tea', 'chai', 'order', 'food',
    'cake', 'soup', 'pasta', 'curry', 'dal', 'paneer',
    'chicken', 'mutton', 'fish', 'juice', 'water', 'beer',
    'wine', 'dessert', 'salad', 'idli', 'dosa', 'samosa',
    'kebab', 'tikka', 'paratha', 'lassi', 'shake', 'item',
    'dish', 'menu', 'meal', 'breakfast', 'lunch', 'dinner',
    'snack', 'starter', 'main', 'course', 'plate', 'serve',
}

_TTS_DONE  = object()
_TTS_STOP  = object()


def _words_lower(text: str) -> set:
    return set(re.findall(r"[a-zA-Z']+", text.lower()))


def _infer_language_from_text(text: str, fallback: str = "hinglish") -> str:
    if not text or len(text.strip()) <= 2:
        return fallback
    words = _words_lower(text)
    if not words:
        return fallback

    hindi_keywords = {
        "aap", "aapka", "aapke", "mujhe", "mera", "meri", "kya", "hai", "nahi", "nahin",
        "chahiye", "ka", "ke", "ki", "kyun", "kab", "kahan", "ho", "hoon", "hai", "tha",
        "thi", "raha", "rahi", "hain", "tum", "bataiye", "karo", "karna", "do", "tera",
        "yeh", "woh", "hain", "bhai", "sahi", "samajh", "mila", "mujhe",
    }
    english_keywords = {
        "please", "order", "menu", "options", "what", "when", "where", "who", "can", "could",
        "would", "will", "yes", "no", "help", "hello", "hi", "thank", "thanks", "good",
        "morning", "evening", "service", "drink", "food", "manager", "receive", "provide",
        "list", "available", "any", "other", "want", "need", "sure", "okay", "sorry",
    }
    weak_english = {"email", "room", "service", "menu", "help", "contact", "phone", "number"}

    count_hi      = len(words & hindi_keywords)
    count_en      = len(words & english_keywords)
    count_weak_en = len(words & weak_english)

    if count_hi >= 2 and count_en == 0:
        return "hindi"
    if count_hi > count_en and count_hi >= 1 and count_en <= 1:
        return "hindi"
    if count_en >= 2 and count_hi == 0:
        return "english"
    if count_en > count_hi and count_en >= 2:
        return "english"
    if count_hi > 0 and count_en > 0:
        return "hinglish"
    if count_hi > 0 and count_en == 0:
        return "hindi"
    if count_en > 0 and count_hi == 0:
        return "english"

    if re.search(
        r"\b(?:kya|hai|nahi|nahin|chahiye|mujhe|aap|aapko|aapka|aapki|kaise|kab|kahan)\b",
        text, re.IGNORECASE,
    ):
        return "hindi" if count_en == 0 else "hinglish"
    if re.search(
        r"\b(?:please|order|menu|options|email|receive|help|room|service|what|when|where|why|can)\b",
        text, re.IGNORECASE,
    ):
        return "english"

    if count_weak_en > 0 and count_hi > 0:
        return "hinglish"

    return fallback


def _detect_language(text: str, fallback: str = "hinglish") -> str:
    return _infer_language_from_text(text, fallback=fallback)


def _extract_room_from_text(text: str) -> Optional[str]:
    text_lower = text.lower()

    m = re.search(
        r'\b(?:room|kamra|room\s*number|room\s*no\.?)\s*[:#]?\s*(\d{1,4})\b',
        text_lower,
    )
    if m:
        num = int(m.group(1))
        if 1 <= num <= 9999:
            return str(num)

    words_in_text = set(text_lower.split())
    has_food      = bool(words_in_text & _FOOD_KEYWORDS)
    if not has_food:
        m = re.search(r'\b(\d{3,4})\b', text)
        if m:
            num = int(m.group(1))
            if 100 <= num <= 9999:
                return str(num)

    if 'room' in text_lower or 'kamra' in text_lower:
        word_map = {
            "zero": "0", "one": "1", "two": "2", "three": "3", "four": "4",
            "five": "5", "six": "6", "seven": "7", "eight": "8", "nine": "9",
        }
        words      = text_lower.split()
        digits     = []
        collecting = False
        for w in words:
            if w in ('room', 'kamra', 'number', 'no'):
                collecting = True
                continue
            if collecting:
                if w in word_map:
                    digits.append(word_map[w])
                elif digits:
                    break
        if 1 <= len(digits) <= 4:
            return "".join(digits)

    return None


def _build_language_prompt(system_prompt: str, detected_lang: str, manager_contact: str) -> str:
    if detected_lang == "english":
        lang_rule = (
            "Reply ONLY in clear, natural English. Do not use Hindi or Roman Hindi words. "
            "Match the guest's language exactly."
        )
    elif detected_lang == "hindi":
        lang_rule = (
            "Reply ONLY in Roman Hindi (Hindi written in English letters). "
            "Do not use full English sentences. Match the guest's language exactly."
        )
    else:
        lang_rule = (
            "Reply in natural Hinglish — blend Hindi and English smoothly. "
            "Match the guest's language exactly."
        )

    hotel_only_rule = (
        "You are an AI hotel concierge. You ONLY answer questions about this hotel's services, "
        "facilities, food menu, room service, spa, cleaning, amenities, timings, and policies. "
        "For services, amenities, spa, housekeeping, essentials, and menu options, mention ONLY items "
        "that are explicitly present in the hotel PDF/knowledge context. Do not suggest, recommend, "
        "or invent any option that is not present in that context. "
        "If the guest asks for an unavailable option or asks for options not listed in the PDF, "
        "say it is not available in the hotel records and ask them to contact the manager. "
        "Meal timing defaults are allowed only for timing questions: breakfast 7 AM to 9 AM, "
        "lunch 12 PM to 3 PM, dinner 7 PM to 10 PM, unless the PDF says different timings. "
        "If a guest asks anything unrelated to the hotel (weather, news, general knowledge, "
        "politics, sports, personal advice, etc.), politely decline in the detected language "
        "and redirect them to hotel services. "
        "Do NOT answer general knowledge questions under any circumstance. "
        "Do NOT mention that you are an AI or a language model. "
        "Keep all responses short — 1 to 3 sentences maximum. "
        "No bullet points. Speak naturally as if on a phone call."
    )

    return (
        f"{lang_rule}\n\n"
        f"{hotel_only_rule}\n\n"
        f"Manager contact: {manager_contact}\n\n"
        f"{system_prompt}"
    )


def _normalize_items(items: list) -> List[str]:
    result = []
    for item in items:
        if isinstance(item, str):
            result.append(item)
        elif isinstance(item, dict):
            name = item.get("name") or item.get("item") or str(item)
            qty  = item.get("quantity") or item.get("qty")
            result.append(f"{qty} {name}" if qty else name)
        else:
            result.append(str(item))
    return result


def _estimate_mulaw_audio_seconds(b64_payload: str) -> float:
    try:
        padding    = "=" * (-len(b64_payload) % 4)
        byte_count = len(base64.b64decode(b64_payload + padding))
        return byte_count / 8000.0
    except Exception:
        return 0.0


_ORDER_NEGATION_RE = re.compile(
    r"\b(?:do\s*not|don't|did\s*not|didn't|no|not|cancel|wrong)\b.*\b(?:order|ordered|select|selected)\b"
    r"|\b(?:order|ordered|select|selected)\b.*\b(?:mat|nahi|nahin|not|cancel|wrong)\b",
    re.IGNORECASE,
)

_ORDER_CANCEL_DIRECT_RE = re.compile(
    r"\b(?:cancel|hatao|band\s*karo|mat\s*lao|mat\s*bhejo|cancel\s*karo|cancel\s*kar|wapas\s*karo)\b",
    re.IGNORECASE,
)

_ORDER_CHANGE_RE = re.compile(
    r"\b(?:change|badlo|replace|instead|nahi|nahin|cancel|hatao|remove|delete|woh nahi|yeh nahi|mat bhejo|mat lao|cancel karo|order change|order badlo|pehle wala nahi|replace with|instead of|not that|not those)\b",
    re.IGNORECASE,
)
_OPTIONS_REQUEST_RE = re.compile(
    r"\b(?:option|options|menu|suggest|suggestion|recommend|recommendation|provide|show|list|available|what\s+.*(?:have|serve))\b",
    re.IGNORECASE,
)
_ORDER_ACTION_RE = re.compile(
    r"\b(?:order|book|send|bring|deliver|confirm|select|choose|provide|give|serve|want|would\s+like|please\s+(?:bring|send|deliver|provide|give|serve))\b",
    re.IGNORECASE,
)
_GENERIC_FOOD_ITEMS = {
    "food", "khana", "meal", "drink", "drinks", "starter", "starters",
    "option", "options", "starter option", "drink option", "food option",
}
_FAREWELL_RE = re.compile(
    r"\b(?:bye|goodbye|thank\s*you|thanks|that's\s*all|thats\s*all|cut\s+the\s+call|end\s+the\s+call)\b",
    re.IGNORECASE,
)
_GREETING_RE = re.compile(
    r"^\s*(?:hi|hello|hey|namaste|good\s*(?:morning|evening|afternoon))[\s?.!]*$",
    re.IGNORECASE,
)
_YES_RE = re.compile(
    r"\b(?:yes|haan|ha|hmm|got\s*it|received|mila|mil\s*gaya|mujhe\s*mila|ya|yeah)\b",
    re.IGNORECASE,
)
_NO_RE = re.compile(
    r"\b(?:no|nahi|nahin|not\s*yet|not\s*received|didn't\s*receive|didnt\s*receive|i\s*don't\s*receive|i\s*didn't\s*receive|mujhe\s*nahi\s*mila|mila\s*nahi)\b",
    re.IGNORECASE,
)
_FOOD_OPTIONS_RE = re.compile(
    r"\b(?:food|drink|drinks|starter|starters|menu|dish|dishes)\b.*\b(?:option|options|menu|available|provide|show|list|suggest|recommend)\b"
    r"|\b(?:option|options|menu|available|provide|show|list|suggest|recommend)\b.*\b(?:food|drink|drinks|starter|starters|dish|dishes)\b",
    re.IGNORECASE,
)


def _is_generic_order_text(text: str) -> bool:
    generic = {
        "food", "khana", "meal", "drink", "drinks", "starter",
        "starters", "menu", "dish", "dishes", "order", "service",
        "food service", "room service", "food order", "drink service",
        "want", "need", "please", "give", "bring", "provide", "like",
        "some", "all",
    }
    normalized = set(re.findall(r"[a-z0-9]+", text.lower()))
    return bool(normalized) and normalized <= generic


def _filter_items_mentioned_in_text(items: List[str], user_text: str) -> List[str]:
    text_lower = user_text.lower()
    filtered   = []
    for item in items:
        item_clean = re.sub(r"\s+", " ", item.strip().lower())
        if not item_clean:
            continue
        if _is_generic_order_text(item_clean):
            continue
        words = [w for w in re.findall(r"[a-z0-9]+", item_clean) if len(w) > 2]
        if not words:
            continue
        if item_clean in text_lower or (words and all(w in text_lower for w in words)):
            filtered.append(item)
    return filtered


def _sanitize_food_intent(intent: str, user_text: str, items: List[str]) -> tuple:
    if intent != "food_order":
        return intent, items

    text = user_text.strip()
    words = text.split()

    if len(words) <= 3 and not any(w.lower() in _FOOD_KEYWORDS for w in words):
        logger.info(f"Food order blocked: transcript too short/vague: '{text}'")
        return "inquiry", []

    if _YES_RE.match(text.strip()) or (len(words) == 1 and _YES_RE.search(text)):
        logger.info("Food order blocked: single yes/haan transcript")
        return "inquiry", []

    if _ORDER_CANCEL_DIRECT_RE.search(text):
        logger.info("Food order blocked: direct cancel/remove request detected")
        return "inquiry", []

    if _ORDER_NEGATION_RE.search(text):
        logger.info("Food order blocked: guest negated/cancelled order")
        return "inquiry", []

    filtered_items    = _filter_items_mentioned_in_text(items, text)
    asks_for_options  = bool(_OPTIONS_REQUEST_RE.search(text))
    has_order_action  = bool(_ORDER_ACTION_RE.search(text))

    if asks_for_options and not has_order_action:
        logger.info("Food order converted to inquiry: guest asked for options/menu")
        return "inquiry", []

    if not filtered_items:
        text_words_lower = _words_lower(text)
        timing_only = {"lunch", "dinner", "breakfast", "morning", "evening", "afternoon", "pm", "am"}
        has_real_food_keyword = bool(text_words_lower & (_FOOD_KEYWORDS - timing_only - {"order", "food", "meal", "want", "need"}))

        if not has_real_food_keyword:
            logger.info(f"Food order blocked: no real food item in transcript: '{text}'")
            return "inquiry", []

        if has_order_action and has_real_food_keyword:
            logger.info("Food order needs clarification: food keyword present but no specific item matched")
            return intent, []

        logger.info("Food order converted to inquiry: classifier inferred unspoken items")
        return "inquiry", []

    return intent, filtered_items


def _is_order_status_request(text: str) -> bool:
    return bool(re.search(
        r"\b(?:what\s+did\s+i\s+order|what\s+is\s+my\s+order|what\s+have\s+i\s+ordered|what\s+did\s+i\s+buy|my\s+order|order\s+details|order\s+status|what\s+is\s+my\s+current\s+order|recall\s+my\s+order|recall\s+my\s+orders)\b",
        text, re.IGNORECASE,
    ))


def _fast_intent_from_text(text: str) -> Optional[Dict]:
    clean = text.strip()
    if not clean:
        return None

    lang = _infer_language_from_text(clean, fallback="hinglish")
    words = clean.split()

    if _GREETING_RE.match(clean):
        return {"intent": "small_talk", "language": lang, "room_number": None, "items": []}

    if _FAREWELL_RE.search(clean):
        return {"intent": "farewell", "language": lang, "room_number": None, "items": []}

    if len(words) <= 3 and not any(w.lower() in _FOOD_KEYWORDS for w in words):
        if _YES_RE.search(clean) or _NO_RE.search(clean):
            return {"intent": "small_talk", "language": lang, "room_number": None, "items": []}

    if _ORDER_CANCEL_DIRECT_RE.search(clean):
        return {"intent": "inquiry", "language": lang, "room_number": None, "items": []}

    if _ORDER_NEGATION_RE.search(clean):
        return {"intent": "inquiry", "language": lang, "room_number": None, "items": []}

    if _FOOD_OPTIONS_RE.search(clean):
        return {"intent": "inquiry", "language": lang, "room_number": None, "items": []}

    return None


def _is_food_related_inquiry(text: str) -> bool:
    return bool(re.search(
        r"\b(?:food|menu|starter|dish|dishes|breakfast|lunch|dinner|snack|drink|beverage|coffee|tea|order|meal)\b",
        text, re.IGNORECASE,
    ))


async def _find_guest_order_summary(
    hotel_id: str,
    guest_number: str,
    guest_room: str,
    current_call_items: List[str],
    language: str = "hinglish",
) -> Optional[str]:
    if not hotel_id or not guest_number:
        return None
    if current_call_items:
        room      = guest_room or "your room"
        items_str = ", ".join(current_call_items)
        if language == "english":
            return (
                f"Your current call order for room {room} includes: {items_str}. "
                "Would you like to add or change anything?"
            )
        return (
            f"Is call mein aapka confirm order room {room} ke liye hai: {items_str}. "
            "Kya aap isme kuch aur add ya change karna chahte hain?"
        )
    if language == "english":
        return f"There are no active orders placed under room {guest_room or 'records'} yet in this call."
    return f"Abhi tak is call par room {guest_room or 'records'} ke liye koi order book nahi kiya gaya hai."


async def _twilio_hangup(call_sid: str):
    if not call_sid:
        return
    try:
        url = (
            f"https://api.twilio.com/2010-04-01/Accounts/"
            f"{settings.twilio_account_sid}/Calls/{call_sid}.json"
        )
        async with httpx.AsyncClient() as client:
            resp = await client.post(
                url,
                data={"Status": "completed"},
                auth=(settings.twilio_account_sid, settings.twilio_auth_token),
                timeout=10,
            )
            if resp.status_code in (200, 204):
                logger.info(f"0️⃣ Call ended | call_sid={call_sid}")
            else:
                logger.warning(f"Twilio hangup {resp.status_code}: {resp.text[:200]}")
    except Exception as e:
        logger.error(f"__twilio_hangup error: {e}")


async def _do_db_write(
    intent: str,
    hotel_id: str,
    hotel_name: str,
    caller_number: str,
    guest_room: str,
    extracted_items: List[str],
    user_text: str,
    replace: bool = False,
):
    try:
        if intent == "food_order":
            if not extracted_items:
                logger.info("DB: skipped food_order save — no explicit items")
                return
            await mongo_client.upsert_food_order(
                hotel_id=hotel_id, hotel_name=hotel_name,
                guest_number=caller_number, guest_room=guest_room,
                items=extracted_items,
                replace=replace,
            )
            logger.info(f"💾 DB: food_order saved | items={extracted_items} | room={guest_room}")

        elif intent == "room_cleaning":
            items = extracted_items or [user_text]
            await mongo_client.upsert_room_cleaning(
                hotel_id=hotel_id, hotel_name=hotel_name,
                guest_number=caller_number, guest_room=guest_room,
                requests=items,
                replace=replace,
            )
            logger.info(f"💾 DB: room_cleaning saved | items={items}")

        elif intent == "spa_service":
            items = extracted_items or [user_text]
            await mongo_client.upsert_spa_service(
                hotel_id=hotel_id, hotel_name=hotel_name,
                guest_number=caller_number, guest_room=guest_room,
                services=items,
                replace=replace,
            )
            logger.info(f"💾 DB: spa_service saved | items={items}")

        elif intent == "essential_needs":
            items = extracted_items or [user_text]
            await mongo_client.upsert_essential_needs(
                hotel_id=hotel_id, hotel_name=hotel_name,
                guest_number=caller_number, guest_room=guest_room,
                needs=items,
                replace=replace,
            )
            logger.info(f"💾 DB: essential_needs saved | items={items}")

        elif intent == "inquiry":
            await mongo_client.upsert_inquiry(
                hotel_id=hotel_id, hotel_name=hotel_name,
                guest_number=caller_number, guest_room=guest_room,
                question=user_text,
            )
            logger.info(f"💾 DB: inquiry saved | q={user_text[:60]}")

        elif intent in ("escalation", "event_inquiry"):
            await mongo_client.upsert_inquiry(
                hotel_id=hotel_id, hotel_name=hotel_name,
                guest_number=caller_number, guest_room=guest_room,
                question=f"[{intent.upper()}] {user_text}",
            )
            logger.info(f"💾 DB: {intent} saved as inquiry")

    except Exception as e:
        logger.error(f"DB write error intent={intent}: {e}")


def _build_instruction(
    intent: str,
    user_text: str,
    extracted_items: List[str],
    guest_room: str,
    manager_contact: str,
    rag_context: str,
    hotel_name: str = "",
) -> str:
    items_str = ", ".join(extracted_items) if extracted_items else "the requested items"
    room_str  = guest_room or "not yet provided"
    pdf_only_rule = (
        "Core rule: Use ONLY services, amenities, spa items, housekeeping items, essentials, "
        "and food/menu options explicitly present in the hotel PDF/knowledge context. "
        "Never invent, suggest, recommend, or list an option that is not in the PDF/knowledge context. "
        "If the requested item/service is not listed, say it is not available in the hotel records "
        "and ask the guest to contact the manager. "
        "Meal timing defaults are allowed only for timing questions: breakfast 7 AM to 9 AM, "
        "lunch 12 PM to 3 PM, dinner 7 PM to 10 PM, unless the PDF says different timings."
    )

    if intent == "food_order":
        return (
            f"The guest has placed a food order.\n"
            f"Items ordered: {items_str}.\n"
            f"Guest room: {room_str}.\n"
            f"Original request: \"{user_text}\".\n"
            f"{pdf_only_rule}\n"
            "Task: Confirm the order only when the requested food items are available in the PDF/knowledge context. "
            "If unavailable, do not offer substitutes. Keep it short, no bullet points."
        )

    elif intent == "room_cleaning":
        return (
            f"The guest has requested room cleaning or housekeeping.\n"
            f"Requests: {items_str}.\n"
            f"Guest room: {room_str}.\n"
            f"Original request: \"{user_text}\".\n"
            f"{pdf_only_rule}\n"
            "Task: Confirm only if this housekeeping service is available in the PDF/knowledge context. "
            "Include timing only if available in the PDF/knowledge context. Keep it short."
        )

    elif intent == "spa_service":
        return (
            f"The guest has booked a spa or wellness service.\n"
            f"Services: {items_str}.\n"
            f"Guest room: {room_str}.\n"
            f"Original request: \"{user_text}\".\n"
            f"{pdf_only_rule}\n"
            "Task: Confirm only if this spa/wellness service is available in the PDF/knowledge context. "
            "Include timing only if available in the PDF/knowledge context. Keep it short."
        )

    elif intent == "essential_needs":
        return (
            f"The guest has requested essential items or amenities.\n"
            f"Items needed: {items_str}.\n"
            f"Guest room: {room_str}.\n"
            f"Original request: \"{user_text}\".\n"
            f"{pdf_only_rule}\n"
            "Task: Confirm delivery only if these items are available in the PDF/knowledge context. "
            "Mention delivery time only if available in the PDF/knowledge context. Keep it short."
        )

    elif intent == "inquiry":
        if not rag_context or len(rag_context.strip()) < 40:
            return (
                f"The guest asked: \"{user_text}\".\n"
                "The hotel knowledge base does not contain relevant information for this query.\n"
                f"{pdf_only_rule}\n"
                "Task: If this is a breakfast/lunch/dinner timing question, answer with the default meal timings. "
                "Otherwise say the information is not available in hotel records and ask the guest to contact the manager. "
                "For non-hotel questions, politely say you can only assist with hotel services. Be warm and brief."
            )
        if _is_food_related_inquiry(user_text):
            return (
                f"The guest asked about food or the menu: \"{user_text}\".\n"
                f"{pdf_only_rule}\n"
                "Task: Answer using ONLY food/menu items explicitly listed in the PDF/knowledge context. "
                "Do not mention unrelated hotel services. If the guest asks for meal timings and the PDF has no timing, "
                "use the default breakfast/lunch/dinner timings. Keep it concise."
            )
        return (
            f"The guest asked: \"{user_text}\".\n"
            f"{pdf_only_rule}\n"
            "Task: Answer using ONLY the hotel PDF/knowledge context. "
            "If the answer is not in the context, say it is unavailable in hotel records and ask them to contact the manager. "
            "Keep it short."
        )

    elif intent == "event_inquiry":
        return (
            f"The guest is asking about an event, party, or special occasion: \"{user_text}\".\n"
            "Task: Explain that event bookings are managed by the hotel manager. "
            "Share the manager's contact number. Do NOT say you will transfer or connect the guest. "
            "Be warm and brief."
        )

    elif intent == "escalation":
        return (
            f"The guest wants to speak with a manager or has an urgent concern: \"{user_text}\".\n"
            "Task: Acknowledge their concern warmly. Share the manager's contact number. "
            "Do NOT say you will transfer or connect the guest. Just provide the number and say they can call directly."
        )

    elif intent == "farewell":
        return (
            f"The guest is ending the call: \"{user_text}\".\n"
            "Task: Give a warm, brief farewell as a hotel concierge. "
            f"Mention {hotel_name} by name. Wish them a pleasant stay."
        )

    return (
        f"Guest said: \"{user_text}\".\n"
        f"{pdf_only_rule}\n"
        "Task: Reply briefly as a hotel concierge using only hotel PDF/knowledge context. "
        "If unavailable, ask them to contact the manager."
    )


@router.websocket("/media-stream")
async def media_stream(websocket: WebSocket):
    await websocket.accept()
    logger.info("🔌 WebSocket connected")

    call_sid:        Optional[str] = None
    stream_sid:      Optional[str] = None
    hotel_id:        Optional[str] = None
    hotel_name:      str           = "Hotel"
    system_prompt:   str           = ""
    manager_contact: str           = ""
    hotel_email:     str           = ""
    caller_number:   str           = ""
    guest_room:      str           = ""
    past_history:    List[Dict]    = []

    stt: Optional[DeepgramService] = None

    _audio_queue:       asyncio.Queue = asyncio.Queue(maxsize=256)
    _audio_player_task: Optional[asyncio.Task] = None
    _tts_queue:         asyncio.Queue = asyncio.Queue(maxsize=64)
    _tts_worker_task:   Optional[asyncio.Task] = None

    is_speaking    = False
    processing     = False
    _call_ending   = False
    _playback_buffer_sec = 0.0
    _current_call_order_items: List[str] = []

    _agent_talking = False

    _agent_interrupted:   bool  = False
    _barge_in_enabled_at: float = 0.0
    _barge_audio_hits:    int   = 0
    _last_barge_in_at:    float = 0.0
    _room_asked:          bool  = False
    _items_asked:         bool  = False
    _order_confirmed:     bool  = False
    _is_order_change:     bool  = False   # True when user is modifying a previous order

    _awaiting_email_confirmation: bool = False
    _email_sent:                  bool = False
    _email_confirmed:             bool = False
    _email_reminder_task:         Optional[asyncio.Task] = None

    _current_tts_language: str = "hinglish"

    _debounce_task:      Optional[asyncio.Task] = None
    _llm_task:           Optional[asyncio.Task] = None
    _pending_transcript: List[str]              = []

    _AUDIO_DONE = object()

  
    async def tts_worker():
        pending: asyncio.Queue = asyncio.Queue()

        async def _fetch_phrase(text: str, language: str) -> list:
            chunks = []
            try:
                async for b64 in tts_service.stream_to_base64_chunks(text, language=language):
                    chunks.append(b64)
            except asyncio.CancelledError:
                raise
            except Exception as e:
                logger.error(f"tts_worker fetch: {e}")

            if not chunks:
                try:
                    fallback_b64 = await tts_service.synthesize_to_base64(text, language=language)
                    if fallback_b64:
                        chunks.append(fallback_b64)
                except Exception as e:
                    logger.error(f"tts_worker fallback synthesize: {e}")
            return chunks

        async def dispatcher():
            while True:
                item = None
                done_called = False
                try:
                    item = await _tts_queue.get()
                    done_called = False

                    if item is _TTS_STOP:
                        _tts_queue.task_done()
                        done_called = True
                        await pending.put(_TTS_STOP)
                        break

                    if item is _TTS_DONE:
                        _tts_queue.task_done()
                        done_called = True
                        await pending.put(_TTS_DONE)
                        continue

                    if isinstance(item, tuple) and len(item) == 2:
                        text, lang = item
                    else:
                        text, lang = item, "hinglish"

                    fut = asyncio.ensure_future(_fetch_phrase(text, lang))
                    await pending.put(fut)
                    _tts_queue.task_done()
                    done_called = True

                except asyncio.CancelledError:
                    if not done_called and item is not None:
                        try:
                            _tts_queue.task_done()
                        except ValueError:
                            pass
                    await pending.put(_TTS_STOP)
                    break
                except Exception as e:
                    logger.error(f"tts dispatcher: {e}")
                    if not done_called and item is not None:
                        try:
                            _tts_queue.task_done()
                        except ValueError:
                            pass

        async def flusher():
            while True:
                try:
                    item = await pending.get()
                    if item is _TTS_STOP:
                        break
                    if item is _TTS_DONE:
                        await _audio_queue.put(_AUDIO_DONE)
                        continue
                    try:
                        chunks = await item
                        for chunk in chunks:
                            await _audio_queue.put(chunk)
                    except asyncio.CancelledError:
                        raise
                    except Exception as e:
                        logger.error(f"tts flusher: {e}")
                except asyncio.CancelledError:
                    while True:
                        try:
                            leftover = pending.get_nowait()
                            if asyncio.isfuture(leftover):
                                leftover.cancel()
                        except asyncio.QueueEmpty:
                            break
                    break

        d_task = asyncio.create_task(dispatcher())
        f_task = asyncio.create_task(flusher())
        try:
            await asyncio.gather(d_task, f_task)
        except asyncio.CancelledError:
            d_task.cancel()
            f_task.cancel()
            try:
                await asyncio.gather(d_task, f_task, return_exceptions=True)
            except Exception:
                pass
            raise


    async def audio_player():
        nonlocal is_speaking, _agent_talking, _playback_buffer_sec
        while True:
            try:
                item = await _audio_queue.get()

                if item is _AUDIO_DONE:
                    _audio_queue.task_done()
                    playback_wait        = min(_playback_buffer_sec, MAX_PLAYBACK_TRACK_SEC)
                    _playback_buffer_sec = 0.0
                    if playback_wait > 0:
                        await asyncio.sleep(playback_wait)
                    is_speaking    = False
                    _agent_talking = False
                    logger.debug("🔊 Agent done speaking — barge-in disabled")
                    continue

                if not _agent_talking:
                    _agent_talking = True
                    logger.debug("🔊 Agent started speaking — barge-in enabled")

                is_speaking = True
                await _send_b64(item)
                _audio_queue.task_done()

            except asyncio.CancelledError:
                is_speaking    = False
                _agent_talking = False
                break
            except Exception as e:
                logger.error(f"Audio player: {e}")
                is_speaking = False
                try:
                    _audio_queue.task_done()
                except ValueError:
                    pass

    async def _send_b64(b64: str):
        nonlocal _playback_buffer_sec
        try:
            if not b64 or not stream_sid or _call_ending:
                return
            if websocket.client_state.value >= 2:
                return
            await websocket.send_text(json.dumps({
                "event":     "media",
                "streamSid": stream_sid,
                "media":     {"payload": b64},
            }))
            _playback_buffer_sec = min(
                _playback_buffer_sec + _estimate_mulaw_audio_seconds(b64),
                MAX_PLAYBACK_TRACK_SEC,
            )
        except Exception as e:
            logger.error(f"_send_b64 error: {e}")

    async def _enqueue_tts(text: str, language: str = "hinglish"):
        _ensure_workers()
        try:
            _tts_queue.put_nowait((text, language))
        except asyncio.QueueFull:
            logger.warning("TTS queue full — dropping phrase")

    async def _enqueue_tts_done():
        try:
            _tts_queue.put_nowait(_TTS_DONE)
        except asyncio.QueueFull:
            pass

    def _log_agent_speech(text: str):
        clean = " ".join((text or "").split())
        if clean:
            line = f"[AGENT SAYS] {clean}"
            print(line, flush=True)
            logger.info(line)

    async def _speak_simple(text: str, language: str = "hinglish"):
        _ensure_workers()
        try:
            _log_agent_speech(text)
            b64 = await tts_service.synthesize_to_base64(text, language=language)
            if b64:
                nonlocal _agent_talking
                _agent_talking = True
                await _audio_queue.put(b64)
                await _audio_queue.put(_AUDIO_DONE)
        except Exception as e:
            logger.error(f"_speak_simple: {e}")

    async def _speak_stream(text: str, language: str = "hinglish"):
        if len(text.strip()) <= 80:
            await _speak_simple(text, language=language)
            return
        _ensure_workers()
        try:
            _log_agent_speech(text)
            await _enqueue_tts(text, language)
            await _enqueue_tts_done()
        except Exception as e:
            logger.error(f"_speak_stream: {e}")

    async def _cancel_email_reminder():
        nonlocal _email_reminder_task
        if _email_reminder_task and not _email_reminder_task.done():
            _email_reminder_task.cancel()
            try:
                await _email_reminder_task
            except asyncio.CancelledError:
                pass
        _email_reminder_task = None

    async def _email_followup_prompt(detected_lang: str = "hinglish"):
        try:
            await asyncio.sleep(2.0)
            if _awaiting_email_confirmation:
                active_prompt = _build_language_prompt(
                    system_prompt=system_prompt,
                    detected_lang=detected_lang,
                    manager_contact=manager_contact,
                )
                instruction = (
                    "Ask the guest if they have received the welcome email from the hotel. "
                    "Keep it short — one sentence only."
                )
                response = await qwen_service.get_full_response(
                    messages=[{"role": "user", "content": instruction}],
                    hotel_id=hotel_id or "",
                    hotel_name=hotel_name,
                    system_prompt=active_prompt,
                )
                if response:
                    await _speak_stream(response, language=detected_lang)
        except asyncio.CancelledError:
            pass

    def _ensure_workers():
        nonlocal _audio_player_task, _tts_worker_task
        if not _audio_player_task or _audio_player_task.done():
            _audio_player_task = asyncio.create_task(audio_player())
        if not _tts_worker_task or _tts_worker_task.done():
            _tts_worker_task = asyncio.create_task(tts_worker())


    async def _pipeline(
        user_text: str,
        intent: str,
        detected_lang: str,
        extracted_items: List[str],
        merged_history: List[Dict],
        active_prompt: str,
        rag_prefetch: str = "",
        replace: bool = False,
    ) -> str:
        nonlocal guest_room, _agent_talking

        rag_ctx = ""
        if intent in RAG_INTENTS:
            if rag_prefetch:
                rag_ctx = rag_prefetch
                logger.debug("⚡ RAG reused from prefetch")
            else:
                rag_ctx = await retrieval_service.search(user_text, hotel_id, top_k=3)

        asyncio.create_task(_do_db_write(
            intent=intent, hotel_id=hotel_id, hotel_name=hotel_name,
            caller_number=caller_number, guest_room=guest_room,
            extracted_items=extracted_items, user_text=user_text,
            replace=replace,
        ))

        instruction = _build_instruction(
            intent=intent,
            user_text=user_text,
            extracted_items=extracted_items,
            guest_room=guest_room,
            manager_contact=manager_contact,
            rag_context=rag_ctx,
            hotel_name=hotel_name,
        )
        messages_for_llm = merged_history + [{"role": "user", "content": instruction}]

        word_buf      = WordStreamBuffer()
        full_response = []
        should_end    = (intent == "farewell")

        token_stream = qwen_service.stream_response(
            messages=messages_for_llm,
            hotel_id=hotel_id,
            hotel_name=hotel_name,
            system_prompt=active_prompt,
            rag_context=rag_ctx,
            manager_contact=manager_contact,
            guest_room=guest_room,
        )

        try:
            async for token in token_stream:
                full_response.append(token)
                flush_text = word_buf.feed(token)
                if flush_text:
                    _agent_talking = True
                    await _enqueue_tts(flush_text, detected_lang)
        except Exception as exc:
            logger.error(f"LLM stream error: {exc}")
            full_response = []
        finally:
            remainder = word_buf.flush()
            if remainder:
                await _enqueue_tts(remainder, detected_lang)
            await _enqueue_tts_done()

        response_text = "".join(full_response).strip()
        _log_agent_speech(response_text)

        if not response_text:
            logger.warning("Empty LLM response — requesting fallback from LLM")
            try:
                response_text = await qwen_service.get_full_response(
                    messages=[{"role": "user", "content": "Briefly apologize and ask the guest to repeat."}],
                    hotel_id=hotel_id or "",
                    hotel_name=hotel_name,
                    system_prompt=active_prompt,
                )
            except Exception:
                response_text = ""
            if response_text:
                _log_agent_speech(response_text)
                b64 = await tts_service.synthesize_to_base64(response_text, language=detected_lang)
                if b64:
                    _agent_talking = True
                    await _audio_queue.put(b64)
                    await _audio_queue.put(_AUDIO_DONE)

        if should_end:
            asyncio.create_task(_end_call_after_audio())

        return response_text


    _barge_in_lock = asyncio.Lock()


    async def barge_in():
        nonlocal is_speaking, processing, _llm_task, _debounce_task
        nonlocal _audio_player_task, _tts_worker_task, _agent_interrupted, _agent_talking
        nonlocal _barge_audio_hits, _playback_buffer_sec, _last_barge_in_at

        now = time.monotonic()
        if time.time() < _barge_in_enabled_at:
            logger.debug("Barge-in blocked — grace period")
            return

        if _barge_in_lock.locked():
            return

        async with _barge_in_lock:
            if not _agent_talking and not is_speaking:
                return

            _last_barge_in_at    = now
            logger.info("⚡ BARGE-IN → stopping agent immediately")
            _agent_interrupted   = True
            _agent_talking       = False
            is_speaking          = False
            _barge_audio_hits    = 0
            _playback_buffer_sec = 0.0

            for task in (_debounce_task, _llm_task):
                if task and not task.done():
                    task.cancel()
                    try:
                        await asyncio.wait_for(asyncio.shield(task), timeout=0.1)
                    except (asyncio.CancelledError, asyncio.TimeoutError):
                        pass

            if _tts_worker_task and not _tts_worker_task.done():
                _tts_worker_task.cancel()
                try:
                    await asyncio.wait_for(asyncio.shield(_tts_worker_task), timeout=0.1)
                except (asyncio.CancelledError, asyncio.TimeoutError):
                    pass

            if _audio_player_task and not _audio_player_task.done():
                _audio_player_task.cancel()
                try:
                    await asyncio.wait_for(asyncio.shield(_audio_player_task), timeout=0.1)
                except (asyncio.CancelledError, asyncio.TimeoutError):
                    pass

            for q in (_tts_queue, _audio_queue):
                while True:
                    try:
                        q.get_nowait()
                    except asyncio.QueueEmpty:
                        break
                try:
                    q._unfinished_tasks = 0       # type: ignore
                    q.all_tasks_done.notify_all() # type: ignore
                except Exception:
                    pass

            if stream_sid:
                try:
                    if websocket.client_state.value != 2:
                        await websocket.send_text(json.dumps({
                            "event":     "clear",
                            "streamSid": stream_sid,
                        }))
                except Exception:
                    pass

            processing         = False
            _llm_task          = None
            _audio_player_task = asyncio.create_task(audio_player())
            _tts_worker_task   = asyncio.create_task(tts_worker())

            if stt:
                stt.reset()
            logger.info("✅ Barge-in complete — listening")


    async def on_interim_transcript(transcript: str, confidence: float = 0.0):
        clean = transcript.strip()
        if not clean or len(clean) < BARGE_IN_TRANSCRIPT_MIN_CHARS:
            return

        if _agent_talking or is_speaking:
            now = time.monotonic()
            if now - _last_barge_in_at < BARGE_IN_COOLDOWN_SEC:
                logger.debug("Barge-in interim suppressed by cooldown")
                return
                
            words = clean.split()
            
            if len(words) == 1 and confidence < 0.40:
                logger.debug(f"Interim noise text dropped due to low confidence: '{clean}' ({confidence:.2f})")
                return

            logger.info(f"🎯 Pure STT Barge-In Triggered: '{clean}' (Confidence: {confidence:.2f})")
            await barge_in()


    async def on_final_transcript(transcript: str, confidence: float = 0.0):
        nonlocal _debounce_task
        if _call_ending:
            return
        clean = transcript.strip()
        if not clean or len(clean) < MIN_TRANSCRIPT_LEN:
            return

        if _agent_talking or is_speaking:
            now = time.monotonic()
            if now - _last_barge_in_at >= BARGE_IN_COOLDOWN_SEC:
                words = clean.split()
                should_trigger = False
                
                if len(words) >= 2:
                    if confidence >= 0.35:
                        should_trigger = True
                elif len(words) == 1:
                    if confidence >= 0.40:
                        should_trigger = True
                        
                if should_trigger:
                    await barge_in()
                else:
                    logger.debug(f"Final transcript barge-in suppressed (Noise filter fallback): '{clean}'")
            else:
                logger.debug("Final transcript barge-in suppressed by cooldown")

        _pending_transcript.append(clean)

        if _debounce_task and not _debounce_task.done():
            _debounce_task.cancel()
        _debounce_task = asyncio.create_task(_debounced_process_joined())


    _FILLER_ONLY_WORDS = {
        "so", "i", "um", "uh", "hmm", "ah", "okay", "ok", "right",
        "yeah", "yep", "and", "but", "or", "the", "a", "an",
    }

    async def _debounced_process_joined():
        nonlocal _pending_transcript
        try:
            await asyncio.sleep(DEBOUNCE_DELAY)
            if _pending_transcript:
                full = " ".join(_pending_transcript).strip()
                _pending_transcript.clear()
                if not full:
                    return
                words_set = set(re.findall(r"[a-zA-Zऀ-ॿ]+", full.lower()))
                if len(full.split()) <= 2 and words_set <= _FILLER_ONLY_WORDS:
                    logger.debug(f"Debounce: skipping filler transcript: '{full}'")
                    return
                if len(full.split()) >= MIN_WORD_COUNT:
                    await _process_transcript(full)
        except asyncio.CancelledError:
            pass


    async def _process_transcript(transcript: str):
        nonlocal processing, guest_room, _llm_task, _agent_interrupted
        nonlocal _room_asked, _items_asked, _order_confirmed, _current_call_order_items
        nonlocal _awaiting_email_confirmation, _email_sent, _email_reminder_task
        nonlocal _current_tts_language

        if processing:
            return
        processing = True
        t0 = time.monotonic()
        logger.info(f"💬 [{hotel_id}] Guest: {transcript}")
        asyncio.create_task(_safe_append(hotel_id, caller_number, "guest", transcript))

        try:
            if not guest_room:
                extracted_room = _extract_room_from_text(transcript)
                if extracted_room:
                    guest_room = extracted_room
                    asyncio.create_task(
                        redis_client.update_session(f"call:{call_sid}", {"guest_room": guest_room})
                    )
                    asyncio.create_task(
                        redis_client.save_guest_room(caller_number, hotel_id, guest_room)
                    )
                    logger.info(f"🏠 Room set (early): {guest_room}")

            if _awaiting_email_confirmation:
                lower_transcript = transcript.lower()
                detected_lang    = _detect_language(transcript, fallback=_current_tts_language)
                _current_tts_language = detected_lang

                if _YES_RE.search(lower_transcript):
                    _awaiting_email_confirmation = False
                    await _cancel_email_reminder()
                    active_prompt = _build_language_prompt(
                        system_prompt=system_prompt,
                        detected_lang=detected_lang,
                        manager_contact=manager_contact,
                    )
                    instruction = (
                        "The guest confirmed they received the hotel's welcome email. "
                        "Acknowledge warmly and ask how you can assist them with hotel services today. "
                        "Keep it to 1-2 sentences."
                    )
                    response = await qwen_service.get_full_response(
                        messages=[{"role": "user", "content": instruction}],
                        hotel_id=hotel_id or "",
                        hotel_name=hotel_name,
                        system_prompt=active_prompt,
                    )
                    if response:
                        await _speak_stream(response, language=detected_lang)
                    processing = False
                    return

                if _NO_RE.search(lower_transcript):
                    await _cancel_email_reminder()
                    active_prompt = _build_language_prompt(
                        system_prompt=system_prompt,
                        detected_lang=detected_lang,
                        manager_contact=manager_contact,
                    )
                    if hotel_email:
                        sending_msg_instruction = (
                            "Tell the guest you are sending the welcome email right now and ask them to wait a moment. "
                            "1 sentence only."
                        )
                        sending_msg = await qwen_service.get_full_response(
                            messages=[{"role": "user", "content": sending_msg_instruction}],
                            hotel_id=hotel_id or "",
                            hotel_name=hotel_name,
                            system_prompt=active_prompt,
                        )
                        if sending_msg:
                            await _speak_stream(sending_msg, language=detected_lang)
                        try:
                            email_sent = await send_email_async(
                                hotel_email,
                                f"Thank you from {hotel_name}",
                                (
                                    f"Dear Valued Guest,\n\n"
                                    f"Thank you for connecting with {hotel_name}.\n\n"
                                    "We are happy to help you with room service, food delivery, room cleaning, "
                                    "spa bookings, and any other guest requests.\n\n"
                                    f"Guest phone: {caller_number}.\n\n"
                                    "Please reply if you need anything else — we are here to serve you.\n\n"
                                    f"Warm regards,\n{hotel_name} Concierge Team"
                                ),
                            )
                            if email_sent:
                                _email_sent = True
                                sent_instruction = (
                                    "Tell the guest the welcome email has been sent and ask them to check it. "
                                    "Also let them know to say 'yes' once they receive it. "
                                    "1-2 sentences."
                                )
                            else:
                                sent_instruction = (
                                    "Apologize that the email could not be sent due to a technical issue. "
                                    "Ask how else you can assist with hotel services. "
                                    "1-2 sentences."
                                )
                        except Exception as e:
                            logger.error(f"Email send error: {e}")
                            sent_instruction = (
                                "Apologize briefly for a technical difficulty with the email. "
                                "Offer to help with hotel services instead. 1 sentence."
                            )
                        sent_response = await qwen_service.get_full_response(
                            messages=[{"role": "user", "content": sent_instruction}],
                            hotel_id=hotel_id or "",
                            hotel_name=hotel_name,
                            system_prompt=active_prompt,
                        )
                        if sent_response:
                            await _speak_stream(sent_response, language=detected_lang)
                    else:
                        no_email_instruction = (
                            "Apologize that the hotel email address is not configured. "
                            "Ask how you can assist with hotel services. 1-2 sentences."
                        )
                        no_email_response = await qwen_service.get_full_response(
                            messages=[{"role": "user", "content": no_email_instruction}],
                            hotel_id=hotel_id or "",
                            hotel_name=hotel_name,
                            system_prompt=active_prompt,
                        )
                        if no_email_response:
                            await _speak_stream(no_email_response, language=detected_lang)
                        _awaiting_email_confirmation = False

                    if _awaiting_email_confirmation:
                        _email_reminder_task = asyncio.create_task(
                            _email_followup_prompt(detected_lang)
                        )
                    processing = False
                    return

                active_prompt = _build_language_prompt(
                    system_prompt=system_prompt,
                    detected_lang=detected_lang,
                    manager_contact=manager_contact,
                )
                reask_instruction = (
                    "Ask the guest again whether they received the welcome email. "
                    "Keep it to one sentence."
                )
                reask_response = await qwen_service.get_full_response(
                    messages=[{"role": "user", "content": reask_instruction}],
                    hotel_id=hotel_id or "",
                    hotel_name=hotel_name,
                    system_prompt=active_prompt,
                )
                if reask_response:
                    await _speak_stream(reask_response, language=detected_lang)
                processing = False
                return

            detected_lang         = _detect_language(transcript, fallback=_current_tts_language)
            _current_tts_language = detected_lang

            # Intercept "recall order" intent directly using native pipeline summary
            if _is_order_status_request(transcript):
                stock_text = await _find_guest_order_summary(
                    hotel_id, caller_number, guest_room,
                    _current_call_order_items, detected_lang,
                )
                if stock_text:
                    await _speak_stream(stock_text, language=detected_lang)
                processing = False
                return

            fast_intent_data = _fast_intent_from_text(transcript)
            intent_task      = None
            if fast_intent_data:
                logger.debug(f"Fast intent matched | intent={fast_intent_data.get('intent')}")
            else:
                intent_task = asyncio.create_task(
                    qwen_service.classify_intent(
                        user_text=transcript,
                        hotel_id=hotel_id or "",
                        hotel_name=hotel_name,
                    )
                )

            history_task = asyncio.create_task(
                redis_client.get_history(f"call:{call_sid}")
            )

            _SKIP_RAG_WORDS  = {
                'bye', 'goodbye', 'thanks', 'thank', 'ok', 'okay', 'hello',
                'hi', 'namaste', 'shukriya', 'dhanyawad', 'alvida', 'chalo',
                'theek', 'tha', 'haan', 'nahi', 'koi', 'baat', 'nahi',
            }
            transcript_words = _words_lower(transcript)
            _likely_no_rag   = bool(transcript_words & _SKIP_RAG_WORDS) and len(transcript_words) <= 4

            rag_early_task = None
            if not _likely_no_rag:
                rag_early_task = asyncio.create_task(
                    retrieval_service.search(transcript, hotel_id, top_k=1)
                )

            if fast_intent_data:
                if rag_early_task:
                    call_history, rag_early = await asyncio.gather(
                        history_task, rag_early_task, return_exceptions=True
                    )
                else:
                    call_history = await history_task
                    rag_early    = ""
                intent_data = fast_intent_data
            elif rag_early_task:
                intent_data, call_history, rag_early = await asyncio.gather(
                    intent_task, history_task, rag_early_task, return_exceptions=True
                )
            else:
                intent_data, call_history = await asyncio.gather(
                    intent_task, history_task, return_exceptions=True
                )
                rag_early = ""

            if isinstance(intent_data, Exception):
                logger.error(f"Intent failed: {intent_data}")
                intent_data = {"intent": "inquiry", "language": "hinglish", "room_number": None, "items": []}
            if isinstance(call_history, Exception):
                call_history = []
            if isinstance(rag_early, Exception):
                rag_early = ""

            intent          = intent_data.get("intent", "inquiry")
            classifier_lang = intent_data.get("language", "hinglish")
            guessed_lang    = _infer_language_from_text(transcript, fallback=_current_tts_language)
            detected_lang   = classifier_lang
            if classifier_lang != guessed_lang and guessed_lang in ("english", "hindi"):
                logger.debug(
                    f"Language override | classifier={classifier_lang} -> guessed={guessed_lang}"
                )
                detected_lang = guessed_lang

            extracted_items = _normalize_items(intent_data.get("items", []))
            detected_room   = intent_data.get("room_number")
            intent, extracted_items = _sanitize_food_intent(intent, transcript, extracted_items)

            if intent == "food_order" and not extracted_items:
                transcript_words = set(re.findall(r"[a-zA-Zऀ-ॿ]+", transcript.lower()))
                filler_set = {
                    "so", "i", "um", "uh", "hmm", "ok", "okay", "yes", "haan", "ha", "ya",
                    "yeah", "and", "can", "you", "will", "it",
                }
                if transcript_words and transcript_words <= filler_set:
                    logger.info(f"food_order with no items + filler transcript → small_talk: '{transcript}'")
                    intent = "small_talk"

            if intent == "farewell" and len(transcript.split()) <= 2 and (
                _YES_RE.search(transcript) or _NO_RE.search(transcript)
            ):
                intent = "small_talk"

            intent_ms = (time.monotonic() - t0) * 1000
            logger.info(
                f"⚡ Intent {intent_ms:.0f}ms | intent={intent} | "
                f"lang={detected_lang} | room={detected_room} | items={extracted_items}"
            )

            if detected_room and not guest_room:
                guest_room = str(detected_room)
                asyncio.create_task(
                    redis_client.update_session(f"call:{call_sid}", {"guest_room": guest_room})
                )
                asyncio.create_task(
                    redis_client.save_guest_room(caller_number, hotel_id, guest_room)
                )
                logger.info(f"🏠 Room set (intent): {guest_room}")

            if intent == "inquiry" and _ORDER_CANCEL_DIRECT_RE.search(transcript):
                if _current_call_order_items:
                    _current_call_order_items.clear()
                    logger.info("🗑️ In-memory order cleared on cancel request")

            if intent in ROOM_REQUIRED_INTENTS and not guest_room and not _room_asked:
                _room_asked   = True
                active_prompt = _build_language_prompt(
                    system_prompt=system_prompt,
                    detected_lang=detected_lang,
                    manager_contact=manager_contact,
                )
                room_q_instruction = (
                    "Ask the guest for their room number politely. "
                    "1 sentence only."
                )
                room_q = await qwen_service.get_full_response(
                    messages=[{"role": "user", "content": room_q_instruction}],
                    hotel_id=hotel_id or "",
                    hotel_name=hotel_name,
                    system_prompt=active_prompt,
                )
                if room_q:
                    _ensure_workers()
                    await _speak_simple(room_q, language=detected_lang)
                processing = False
                return

            if intent == "food_order" and not extracted_items:
                _items_asked  = True
                active_prompt = _build_language_prompt(
                    system_prompt=system_prompt,
                    detected_lang=detected_lang,
                    manager_contact=manager_contact,
                )
                items_q_instruction = (
                    "Ask the guest what listed PDF/menu item they would like to order. "
                    "Do not suggest or list options. 1 sentence only."
                )
                items_q = await qwen_service.get_full_response(
                    messages=[{"role": "user", "content": items_q_instruction}],
                    hotel_id=hotel_id or "",
                    hotel_name=hotel_name,
                    system_prompt=active_prompt,
                )
                if items_q:
                    _ensure_workers()
                    await _speak_simple(items_q, language=detected_lang)
                processing = False
                return

            _is_order_change = False
            if (
                intent in ("food_order", "room_cleaning", "spa_service", "essential_needs")
                and extracted_items
                and _order_confirmed
                and bool(_ORDER_CHANGE_RE.search(transcript))
            ):
                _is_order_change = True
                logger.info(f"🔄 Order CHANGE detected | new items={extracted_items}")

            if intent == "food_order" and extracted_items and guest_room:
                _order_confirmed = True
                _items_asked     = True

            rag_for_pipeline = ""
            if intent in RAG_INTENTS and isinstance(rag_early, str):
                rag_for_pipeline = rag_early

            # --- DYNAMIC INJECTION LOOP TO PREVENT HALLUCINATIONS ---
            # Append confirmed call memory arrays into systemic injection block before LLM compilation
            merged = (past_history[-6:] + (call_history if isinstance(call_history, list) else []))
            if _current_call_order_items:
                live_items_context = ", ".join(_current_call_order_items)
                memory_injection = (
                    f"CRITICAL LIVE CONTEXT: The guest has explicitly ordered/requested ONLY these items so far "
                    f"in the current call session: [{live_items_context}]. "
                    f"If the user asks to confirm or recall what they ordered, mention ONLY these exact items. "
                    f"Do not inventory any other item or hallucinate any alternate service history records."
                )
                merged.append({"role": "system", "content": memory_injection})

            if detected_lang == "english":
                lang_reminder_content = "REMINDER: Respond in English only."
            elif detected_lang == "hindi":
                lang_reminder_content = "REMINDER: Respond in Roman Hindi only."
            else:
                lang_reminder_content = "REMINDER: Respond in natural Hinglish."
            merged = merged + [{"role": "system", "content": lang_reminder_content}]

            enriched = transcript
            if _agent_interrupted:
                enriched       = transcript + "\n[Note: Agent was interrupted mid-response.]"
                _agent_interrupted = False

            active_prompt = _build_language_prompt(
                system_prompt=system_prompt,
                detected_lang=detected_lang,
                manager_contact=manager_contact,
            )

            _ensure_workers()
            _agent_talking = True

            _llm_task = asyncio.create_task(
                _pipeline(
                    user_text=enriched,
                    intent=intent,
                    detected_lang=detected_lang,
                    extracted_items=extracted_items,
                    merged_history=merged,
                    active_prompt=active_prompt,
                    rag_prefetch=rag_for_pipeline,
                    replace=_is_order_change,
                )
            )

            try:
                response_text = await _llm_task
            except asyncio.CancelledError:
                logger.info("Pipeline cancelled (barge-in)")
                return
            finally:
                processing = False

            total_ms = (time.monotonic() - t0) * 1000
            logger.info(f"⚡ Done {total_ms:.0f}ms | response={response_text[:60]}")

            if intent in ROOM_REQUIRED_INTENTS and extracted_items:
                _order_confirmed = True
                _items_asked     = True
                if _is_order_change:
                    _current_call_order_items.clear()
                    _current_call_order_items.extend(extracted_items)
                    logger.info(f"🔄 In-memory order replaced | new={extracted_items}")
                else:
                    existing_lower = {i.lower() for i in _current_call_order_items}
                    new_items = [i for i in extracted_items if i.lower() not in existing_lower]
                    if new_items:
                        _current_call_order_items.extend(new_items)
                    elif extracted_items:
                        logger.info(f"ℹ️ Same items re-ordered, not duplicating: {extracted_items}")

            asyncio.create_task(redis_client.append_message(f"call:{call_sid}", "user", transcript))
            asyncio.create_task(redis_client.append_message(f"call:{call_sid}", "assistant", response_text))
            asyncio.create_task(redis_client.append_guest_memory(caller_number, hotel_id, "user", transcript))
            asyncio.create_task(redis_client.append_guest_memory(caller_number, hotel_id, "assistant", response_text))
            asyncio.create_task(_safe_append(hotel_id, caller_number, "agent", response_text))

        except asyncio.CancelledError:
            raise
        except Exception as e:
            logger.error(f"_process_transcript error: {e}", exc_info=True)
            try:
                err_prompt = _build_language_prompt(
                    system_prompt=system_prompt,
                    detected_lang=_current_tts_language,
                    manager_contact=manager_contact,
                )
                err_response = await qwen_service.get_full_response(
                    messages=[{"role": "user", "content": "Briefly apologize and ask the guest to repeat what they said."}],
                    hotel_id=hotel_id or "",
                    hotel_name=hotel_name,
                    system_prompt=err_prompt,
                )
                if err_response:
                    _ensure_workers()
                    await _speak_simple(err_response, language=_current_tts_language)
            except Exception:
                pass
        finally:
            processing = False


    async def _end_call_after_audio():
        nonlocal _call_ending
        _call_ending = True
        logger.info(f"👋 Waiting for farewell audio | call_sid={call_sid}")
        try:
            await asyncio.wait_for(_tts_queue.join(), timeout=6.0)
            await asyncio.wait_for(_audio_queue.join(), timeout=6.0)
        except asyncio.TimeoutError:
            logger.warning("Farewell audio timeout — hanging up anyway")
        except Exception:
            pass
        
        if caller_number and hotel_id:
            asyncio.create_task(
                send_post_call_summary(
                    guest_number=caller_number,
                    hotel_id=hotel_id,
                    hotel_name=hotel_name,
                    order_items=list(_current_call_order_items),
                    manager_contact=manager_contact,
                )
            )

        await _twilio_hangup(call_sid)
        try:
            if websocket.application_state.value != 2:
                await websocket.close()
        except Exception:
            pass


    try:
        _audio_player_task = asyncio.create_task(audio_player())
        _tts_worker_task   = asyncio.create_task(tts_worker())

        async for raw_message in websocket.iter_text():
            try:
                msg = json.loads(raw_message)
            except json.JSONDecodeError:
                continue

            event = msg.get("event", "")

            if event == "connected":
                logger.info("Twilio: connected")

            elif event == "start":
                start_data      = msg.get("start", {})
                stream_sid      = msg.get("streamSid", "")
                call_sid        = start_data.get("callSid", "")

                session         = await redis_client.get_session(f"call:{call_sid}") or {}
                hotel_id        = session.get("hotel_id", "")
                hotel_name      = session.get("hotel_name", "Hotel")
                system_prompt   = session.get("system_prompt", "")
                manager_contact = session.get("manager_contact", "")
                hotel_email     = session.get("hotel_email", "")
                caller_number   = session.get("caller_number", "")
                guest_room      = session.get("guest_room", "")

                logger.info(
                    f"📞 Stream start | call_sid={call_sid} | "
                    f"hotel={hotel_name} | hotel_id={hotel_id}"
                )

                if not guest_room and caller_number and hotel_id:
                    remembered = await redis_client.get_guest_room(caller_number, hotel_id)
                    if remembered:
                        guest_room = remembered
                        logger.info(f"🧠 Room from memory: {guest_room}")

                if caller_number and hotel_id:
                    past_history = await redis_client.get_guest_memory(
                        caller_number, hotel_id, last_n=10
                    )
                    if past_history:
                        logger.info(f"🧠 {len(past_history)} past turns | guest {caller_number[-4:]}***")

                stt = DeepgramService(
                    hotel_id=hotel_id,
                    on_final=on_final_transcript,
                    on_interim=on_interim_transcript,
                )

                _ensure_workers()
                cached_b64    = get_cached_greeting_b64(hotel_id)
                greeting_text = get_cached_greeting_text(hotel_name)

                if cached_b64:
                    logger.info(f"⚡ Cached greeting | hotel={hotel_name}")
                    _log_agent_speech(greeting_text)
                    asyncio.create_task(stt.connect())
                    _barge_in_enabled_at = time.time() + GREETING_GRACE_SEC
                    _agent_talking       = True
                    await _send_b64(cached_b64)
                    await _audio_queue.put(_AUDIO_DONE)
                else:
                    logger.info(f"🔄 Cache miss → synthesize | hotel={hotel_name}")
                    stt_t = asyncio.create_task(stt.connect())
                    tts_t = asyncio.create_task(tts_service.synthesize_to_base64(greeting_text))
                    greeting_b64, _ = await asyncio.gather(tts_t, stt_t, return_exceptions=True)
                    if not isinstance(greeting_b64, Exception) and greeting_b64:
                        set_cached_greeting(hotel_id, greeting_b64)
                    else:
                        greeting_b64 = None
                    _barge_in_enabled_at = time.time() + GREETING_GRACE_SEC
                    if greeting_b64:
                        _log_agent_speech(greeting_text)
                        _agent_talking = True
                        await _send_b64(greeting_b64)
                        await _audio_queue.put(_AUDIO_DONE)
                        logger.debug(f"ℹ️ Greeting: {greeting_text[:60]}")
                    else:
                        logger.warning("⚠️ Greeting TTS failed — using _speak_simple")
                        _agent_talking = True
                        await _speak_simple(greeting_text)

                _awaiting_email_confirmation = True
                _email_sent                  = False
                await _cancel_email_reminder()
                _email_reminder_task = asyncio.create_task(_email_followup_prompt())

                asyncio.create_task(pre_warm_greeting(hotel_id, hotel_name))

                async def _create_log():
                    try:
                        await mongo_client.create_call_log(
                            hotel_id=hotel_id, hotel_name=hotel_name,
                            guest_number=caller_number, guest_room=guest_room,
                            call_sid=call_sid,
                        )
                    except Exception as e:
                        logger.warning(f"create_call_log: {e}")
                asyncio.create_task(_create_log())

            elif event == "media":
                if stt:
                    payload = msg.get("media", {}).get("payload", "")
                    if payload:
                        await stt.send_base64_chunk(payload)

            elif event == "stop":
                logger.info(f"📴 Stream stopped | call_sid={call_sid}")
                break

    except WebSocketDisconnect:
        logger.info(f"WS disconnected | call_sid={call_sid}")
    except asyncio.CancelledError:
        logger.info(f"WS cancelled | call_sid={call_sid}")
    except Exception as e:
        logger.error(f"WS error: {e}", exc_info=True)
    finally:
        logger.info(f"🧹 Cleanup | call_sid={call_sid}")

        for t in (_debounce_task, _llm_task):
            if t and not t.done():
                t.cancel()

        try:
            _tts_queue.put_nowait(_TTS_STOP)
        except Exception:
            pass
        if _tts_worker_task and not _tts_worker_task.done():
            _tts_worker_task.cancel()
            try:
                await _tts_worker_task
            except asyncio.CancelledError:
                pass

        try:
            _audio_queue.put_nowait(_AUDIO_DONE)
        except Exception:
            pass
        if _audio_player_task and not _audio_player_task.done():
            _audio_player_task.cancel()
            try:
                await _audio_player_task
            except asyncio.CancelledError:
                pass

        if _email_reminder_task and not _email_reminder_task.done():
            _email_reminder_task.cancel()
            try:
                await _email_reminder_task
            except asyncio.CancelledError:
                pass

        if stt:
            try:
                await stt.disconnect()
            except Exception as e:
                logger.warning(f"STT disconnect: {e}")
        if call_sid:
            try:
                await redis_client.delete_session(f"call:{call_sid}")
            except Exception:
                pass
        logger.info(f"✅ Done | call_sid={call_sid}")


async def _safe_append(hotel_id: str, caller_number: str, role: str, text: str):
    try:
        await mongo_client.append_conversation(
            hotel_id=hotel_id,
            guest_number=caller_number,
            role=role,
            message=text,
        )
    except Exception as e:
        logger.warning(f"append_conversation: {e}")