# """
# websocket/websocket_server.py

# FIXES v9:
#    ✅ TOTAL BARGE-IN RESOLUTION: `processing` state variables are handled using strict 
#       try/finally blocks and explicit cancellation checks to prevent engine deadlocks.
#    ✅ INSTANT RE-LISTEN: Enhanced task teardown to clear buffers instantaneously so 
#       the agent halts mid-syllable and resets for the user's voice turn.
#    ✅ FILLER WORD REJECTION: Guarded the farewell intent from dropping the call when 
#       the guest speaks short placeholder words like "ok" or "hello".
# """

# import asyncio
# import audioop
# import base64
# import json
# import re
# import time
# from typing import Optional, List, Dict

# import httpx
# from fastapi import APIRouter, WebSocket, WebSocketDisconnect
# from loguru import logger

# from agents.qwen_service import qwen_service
# from database.mongodb import mongo_client
# from database.redis_client import redis_client
# from speech.deepgram_service import DeepgramService
# from tts.tts_service import tts_service, WordStreamBuffer
# from tts.Greeting_cache import (
#     get_cached_greeting_b64,
#     get_cached_greeting_text,
#     pre_warm_greeting,
#     set_cached_greeting,
# )
# from rag.retrieval_service import retrieval_service
# from utils.email_sender import send_email_async
# from config import settings

# router = APIRouter()

# MIN_TRANSCRIPT_LEN    = 2   # lowered: even 2-char barge-in words count
# MIN_WORD_COUNT        = 1   # lowered: single word triggers processing
# DEBOUNCE_DELAY        = 0.05
# GREETING_GRACE_SEC    = 3.5

# # Twilio sends 20ms PCMU/mu-law frames. Waiting for STT interim text can make
# # barge-in feel late, so detect caller speech from the raw inbound audio first.
# BARGE_IN_RMS_THRESHOLD = 550
# BARGE_IN_HIT_FRAMES    = 2
# BARGE_IN_COOLDOWN_SEC  = 0.8
# BARGE_IN_TRANSCRIPT_MIN_CHARS = 4
# MAX_PLAYBACK_TRACK_SEC = 8.0

# ROOM_REQUIRED_INTENTS = {"food_order", "room_cleaning", "spa_service", "essential_needs"}

# # Intents that NEVER need RAG — saves ~150-200ms per call
# NO_RAG_INTENTS = {"farewell", "escalation", "event_inquiry", "small_talk", "greeting"}

# # Intents that need RAG
# RAG_INTENTS = {"food_order", "room_cleaning", "spa_service", "essential_needs", "inquiry"}

# _FOOD_KEYWORDS = {
#     'rice', 'roti', 'pizza', 'burger', 'naan', 'biryani',
#     'sandwich', 'coffee', 'tea', 'chai', 'order', 'food',
#     'cake', 'soup', 'pasta', 'curry', 'dal', 'paneer',
#     'chicken', 'mutton', 'fish', 'juice', 'water', 'beer',
#     'wine', 'dessert', 'salad', 'idli', 'dosa', 'samosa',
#     'kebab', 'tikka', 'paratha', 'lassi', 'shake', 'item',
#     'dish', 'menu', 'meal', 'breakfast', 'lunch', 'dinner',
#     'snack', 'starter', 'main', 'course', 'plate', 'serve',
# }

# _TTS_DONE  = object()
# _TTS_STOP  = object()


# # ─────────────────────────────────────────────────────────────────────────────
# # ROOM NUMBER EXTRACTION
# # ─────────────────────────────────────────────────────────────────────────────

# def _extract_room_from_text(text: str) -> Optional[str]:
#     text_lower = text.lower()

#     m = re.search(
#         r'\b(?:room|kamra|room\s*number|room\s*no\.?)\s*[:#]?\s*(\d{1,4})\b',
#         text_lower,
#     )
#     if m:
#         num = int(m.group(1))
#         if 1 <= num <= 9999:
#             return str(num)

#     words_in_text = set(text_lower.split())
#     has_food       = bool(words_in_text & _FOOD_KEYWORDS)
#     if not has_food:
#         m = re.search(r'\b(\d{3,4})\b', text)
#         if m:
#             num = int(m.group(1))
#             if 100 <= num <= 9999:
#                 return str(num)

#     if 'room' in text_lower or 'kamra' in text_lower:
#         word_map = {
#             "zero": "0", "one": "1", "two": "2", "three": "3", "four": "4",
#             "five": "5", "six": "6", "seven": "7", "eight": "8", "nine": "9",
#         }
#         words      = text_lower.split()
#         digits     = []
#         collecting = False
#         for w in words:
#             if w in ('room', 'kamra', 'number', 'no'):
#                 collecting = True
#                 continue
#             if collecting:
#                 if w in word_map:
#                     digits.append(word_map[w])
#                 elif digits:
#                     break
#         if 1 <= len(digits) <= 4:
#             return "".join(digits)

#     return None


# # ─────────────────────────────────────────────────────────────────────────────
# # LANGUAGE + EMOTION AWARE SYSTEM PROMPT
# # ─────────────────────────────────────────────────────────────────────────────

# def _build_language_prompt(system_prompt: str, detected_lang: str, manager_contact: str) -> str:
#     # ⚡ ULTRA-CONCISE PROMPTS FOR LATENCY
#     if detected_lang == "english":
#         lang_rule = (
#             "Reply ONLY in clear, natural English. Do not use Hindi or Roman Hindi words. "
#             "Always keep the same language as the guest. "
#             "Keep answers short and hotel-focused. If no reliable answer is available, mention manager contact."
#         )
#     elif detected_lang == "hindi":
#         lang_rule = (
#             "Reply ONLY in Roman Hindi. Do not use English words except unavoidable proper nouns. "
#             "Always keep the same language as the guest. "
#             "Keep responses short and hotel-focused. If no reliable answer is available, mention manager contact."
#         )
#     else:
#         lang_rule = (
#             "Reply in natural Hinglish with both Hindi and English. Blend them smoothly. "
#             "Always keep the same language as the guest. "
#             "Keep responses short and hotel-focused. If no reliable answer is available, mention manager contact."
#         )

#     return (
#         f"{lang_rule}\n\n"
#         f"You are hotel concierge. ONLY hotel services. Never general knowledge.\n"
#         f"Respond in the detected language only: {detected_lang}.\n"
#         f"Manager: {manager_contact}\n"
#         f"{system_prompt}"
#     )


# # ─────────────────────────────────────────────────────────────────────────────
# # HELPERS
# # ─────────────────────────────────────────────────────────────────────────────

# def _normalize_items(items: list) -> List[str]:
#     result = []
#     for item in items:
#         if isinstance(item, str):
#             result.append(item)
#         elif isinstance(item, dict):
#             name = item.get("name") or item.get("item") or str(item)
#             qty  = item.get("quantity") or item.get("qty")
#             result.append(f"{qty} {name}" if qty else name)
#         else:
#             result.append(str(item))
#     return result


# _ORDER_NEGATION_RE = re.compile(
#     r"\b(?:do\s*not|don't|did\s*not|didn't|no|not|cancel|wrong)\b.*\b(?:order|ordered|select|selected)\b"
#     r"|\b(?:order|ordered|select|selected)\b.*\b(?:mat|nahi|nahin|not|cancel|wrong)\b",
#     re.IGNORECASE,
# )
# _OPTIONS_REQUEST_RE = re.compile(
#     r"\b(?:option|options|menu|suggest|suggestion|recommend|recommendation|provide|show|list|available|what\s+.*(?:have|serve))\b",
#     re.IGNORECASE,
# )
# _ORDER_ACTION_RE = re.compile(
#     r"\b(?:order|book|send|bring|deliver|confirm|select|choose|provide|give|serve|want|would\s+like|please\s+(?:bring|send|deliver|provide|give|serve))\b",
#     re.IGNORECASE,
# )
# _GENERIC_FOOD_ITEMS = {
#     "food", "khana", "meal", "drink", "drinks", "starter", "starters",
#     "option", "options", "starter option", "drink option", "food option",
# }
# _FAREWELL_RE = re.compile(
#     r"\b(?:bye|goodbye|thank\s*you|thanks|that's\s*all|thats\s*all|cut\s+the\s+call|end\s+the\s+call)\b",
#     re.IGNORECASE,
# )
# _GREETING_RE = re.compile(r"^\s*(?:hi|hello|hey|namaste|good\s*(?:morning|evening|afternoon))[\s?.!]*$", re.IGNORECASE)
# _YES_RE = re.compile(
#     r"\b(?:yes|haan|ha|hmm|got\s*it|received|mila|mil\s*gaya|mujhe\s*mila|ya|yeah)\b",
#     re.IGNORECASE,
# )
# _NO_RE = re.compile(
#     r"\b(?:no|nahi|nahin|not\s*yet|not\s*received|didn't\s*receive|didnt\s*receive|i\s*don't\s*receive|i\s*didn't\s*receive|mujhe\s*nahi\s*mila|mila\s*nahi)\b",
#     re.IGNORECASE,
# )
# _FOOD_OPTIONS_RE = re.compile(
#     r"\b(?:food|drink|drinks|starter|starters|menu|dish|dishes)\b.*\b(?:option|options|menu|available|provide|show|list|suggest|recommend)\b"
#     r"|\b(?:option|options|menu|available|provide|show|list|suggest|recommend)\b.*\b(?:food|drink|drinks|starter|starters|dish|dishes)\b",
#     re.IGNORECASE,
# )


# def _estimate_mulaw_audio_seconds(b64_payload: str) -> float:
#     try:
#         padding = "=" * (-len(b64_payload) % 4)
#         byte_count = len(base64.b64decode(b64_payload + padding))
#         return byte_count / 8000.0
#     except Exception:
#         return 0.0


# def _words_lower(text: str) -> set[str]:
#     return set(re.findall(r"[a-zA-Z']+", text.lower()))


# def _fast_intent_from_text(text: str) -> Optional[Dict]:
#     clean = text.strip()
#     if not clean:
#         return None

#     words = _words_lower(clean)
#     lang = _infer_language_from_text(clean, fallback="hinglish")

#     if _GREETING_RE.match(clean):
#         return {"intent": "small_talk", "language": lang, "room_number": None, "items": []}

#     if _FAREWELL_RE.search(clean):
#         return {"intent": "farewell", "language": lang, "room_number": None, "items": []}

#     if _ORDER_NEGATION_RE.search(clean):
#         return {"intent": "inquiry", "language": lang, "room_number": None, "items": []}

#     if _FOOD_OPTIONS_RE.search(clean):
#         return {"intent": "inquiry", "language": lang, "room_number": None, "items": []}

#     return None


# def _is_generic_order_text(text: str) -> bool:
#     generic = {
#         "food", "khana", "meal", "drink", "drinks", "starter",
#         "starters", "menu", "dish", "dishes", "order", "service",
#         "food service", "room service", "food order", "drink service",
#         "want", "need", "please", "give", "bring", "provide", "like",
#         "some", "all",
#     }
#     normalized = set(re.findall(r"[a-z0-9]+", text.lower()))
#     return bool(normalized) and normalized <= generic


# def _filter_items_mentioned_in_text(items: List[str], user_text: str) -> List[str]:
#     text_lower = user_text.lower()
#     filtered = []
#     for item in items:
#         item_clean = re.sub(r"\s+", " ", item.strip().lower())
#         if not item_clean:
#             continue
#         if _is_generic_order_text(item_clean):
#             continue
#         words = [w for w in re.findall(r"[a-z0-9]+", item_clean) if len(w) > 2]
#         if not words:
#             continue
#         if item_clean in text_lower or (words and all(w in text_lower for w in words)):
#             filtered.append(item)
#     return filtered


# def _sanitize_food_intent(intent: str, user_text: str, items: List[str]) -> tuple[str, List[str]]:
#     if intent != "food_order":
#         return intent, items

#     text = user_text.strip()
#     if _ORDER_NEGATION_RE.search(text):
#         logger.info("Food order blocked: guest negated/cancelled order")
#         return "inquiry", []

#     filtered_items = _filter_items_mentioned_in_text(items, text)
#     asks_for_options = bool(_OPTIONS_REQUEST_RE.search(text))
#     has_order_action = bool(_ORDER_ACTION_RE.search(text))

#     if asks_for_options and not has_order_action:
#         logger.info("Food order converted to inquiry: guest asked for options/menu")
#         return "inquiry", []

#     if not filtered_items:
#         if has_order_action:
#             logger.info("Food order needs clarification: no explicit item in transcript")
#             return intent, []
#         logger.info("Food order converted to inquiry: classifier inferred unspoken items")
#         return "inquiry", []

#     return intent, filtered_items


# def _is_order_status_request(text: str) -> bool:
#     return bool(re.search(
#         r"\b(?:what\s+did\s+i\s+order|what\s+is\s+my\s+order|what\s+have\s+i\s+ordered|what\s+did\s+i\s+buy|my\s+order|order\s+details|order\s+status|what\s+is\s+my\s+current\s+order)\b",
#         text,
#         re.IGNORECASE,
#     ))


# async def _find_guest_order_summary(
#         hotel_id: str,
#         guest_number: str,
#         guest_room: str,
#         current_call_items: List[str],
#         language: str = "hinglish",
#     ) -> Optional[str]:
#         if not hotel_id or not guest_number:
#             return None

#         if current_call_items:
#             room = guest_room or "current room"
#             items_str = ", ".join(current_call_items)
#             if language == "english":
#                 return (
#                     f"Your current order for room {room} is: {items_str}. "
#                     "If you want to change or add anything, please tell me."
#                 )
#             return (
#                 f"Is call mein aapka order room {room} ke liye hai: {items_str}. "
#                 "Agar aap badalwana ya kuch aur order karna chahte hain, toh batayein."
#             )

#         return None


# async def _twilio_hangup(call_sid: str):
#     if not call_sid:
#         return
#     try:
#         url = (
#             f"https://api.twilio.com/2010-04-01/Accounts/"
#             f"{settings.twilio_account_sid}/Calls/{call_sid}.json"
#         )
#         async with httpx.AsyncClient() as client:
#             resp = await client.post(
#                 url,
#                 data={"Status": "completed"},
#                 auth=(settings.twilio_account_sid, settings.twilio_auth_token),
#                 timeout=10,
#             )
#             if resp.status_code in (200, 204):
#                 logger.info(f"0️⃣ Call ended | call_sid={call_sid}")
#             else:
#                 logger.warning(f"Twilio hangup {resp.status_code}: {resp.text[:200]}")
#     except Exception as e:
#         logger.error(f"_twilio_hangup error: {e}")


# # ─────────────────────────────────────────────────────────────────────────────
# # DB WRITE HELPERS — each intent saves to correct collection
# # ─────────────────────────────────────────────────────────────────────────────

# async def _do_db_write(
#     intent: str,
#     hotel_id: str,
#     hotel_name: str,
#     caller_number: str,
#     guest_room: str,
#     extracted_items: List[str],
#     user_text: str,
# ):
#     try:
#         if intent == "food_order":
#             if not extracted_items:
#                 logger.info("DB: skipped food_order save because no explicit items were provided")
#                 return
#             items = extracted_items
#             await mongo_client.upsert_food_order(
#                 hotel_id=hotel_id, hotel_name=hotel_name,
#                 guest_number=caller_number, guest_room=guest_room,
#                 items=items,
#             )
#             logger.info(f"💾 DB: food_order saved | items={items} | room={guest_room}")

#         elif intent == "room_cleaning":
#             items = extracted_items or [user_text]
#             await mongo_client.upsert_room_cleaning(
#                 hotel_id=hotel_id, hotel_name=hotel_name,
#                 guest_number=caller_number, guest_room=guest_room,
#                 requests=items,
#             )
#             logger.info(f"💾 DB: room_cleaning saved | items={items}")

#         elif intent == "spa_service":
#             items = extracted_items or [user_text]
#             await mongo_client.upsert_spa_service(
#                 hotel_id=hotel_id, hotel_name=hotel_name,
#                 guest_number=caller_number, guest_room=guest_room,
#                 services=items,
#             )
#             logger.info(f"💾 DB: spa_service saved | items={items}")

#         elif intent == "essential_needs":
#             items = extracted_items or [user_text]
#             await mongo_client.upsert_essential_needs(
#                 hotel_id=hotel_id, hotel_name=hotel_name,
#                 guest_number=caller_number, guest_room=guest_room,
#                 needs=items,
#             )
#             logger.info(f"💾 DB: essential_needs saved | items={items}")

#         elif intent == "inquiry":
#             await mongo_client.upsert_inquiry(
#                 hotel_id=hotel_id, hotel_name=hotel_name,
#                 guest_number=caller_number, guest_room=guest_room,
#                 question=user_text,
#             )
#             logger.info(f"💾 DB: inquiry saved | q={user_text[:60]}")

#         elif intent in ("escalation", "event_inquiry"):
#             await mongo_client.upsert_inquiry(
#                 hotel_id=hotel_id, hotel_name=hotel_name,
#                 guest_number=caller_number, guest_room=guest_room,
#                 question=f"[{intent.upper()}] {user_text}",
#             )
#             logger.info(f"💾 DB: {intent} saved as inquiry")

#     except Exception as e:
#         logger.error(f"DB write error intent={intent}: {e}")


# # ─────────────────────────────────────────────────────────────────────────────
# # BUILD LLM INSTRUCTION
# # ─────────────────────────────────────────────────────────────────────────────

# def _is_food_related_inquiry(text: str) -> bool:
#     return bool(re.search(
#         r"\b(?:food|menu|starter|dish|dishes|breakfast|lunch|dinner|snack|drink|beverage|coffee|tea|order|meal)\b",
#         text,
#         re.IGNORECASE,
#     ))


# def _build_instruction(
#     intent: str,
#     user_text: str,
#     extracted_items: List[str],
#     guest_room: str,
#     manager_contact: str,
#     rag_context: str,
# ) -> str:
#     items_str = ", ".join(extracted_items) if extracted_items else "the requested items"

#     if intent == "food_order":
#         return (
#             f"The guest placed a food order. Items: {items_str}. "
#             f"Room: {guest_room or 'not provided'}. "
#             f"Original request: \"{user_text}\". "
#             "Confirm the order warmly. Mention items and room. "
#             "Use delivery time from knowledge base if available — do NOT invent. "
#             "Short, no bullets."
#         )
#     elif intent == "room_cleaning":
#         return (
#             f"Guest requested room cleaning/housekeeping. "
#             f"Requests: {items_str}. Room: {guest_room or 'not provided'}. "
#             f"Original: \"{user_text}\". "
#             "Confirm warmly. Short."
#         )
#     elif intent == "spa_service":
#         return (
#             f"Guest booked spa service. Services: {items_str}. "
#             f"Room: {guest_room or 'not provided'}. "
#             f"Original: \"{user_text}\". "
#             "Confirm warmly with timing info from knowledge base if available."
#         )
#     elif intent == "essential_needs":
#         return (
#             f"Guest needs essentials/amenities. Items: {items_str}. "
#             f"Room: {guest_room or 'not provided'}. "
#             f"Original: \"{user_text}\". "
#             "Confirm warmly, mention delivery time if in knowledge base."
#         )
#     elif intent == "inquiry":
#         if _is_food_related_inquiry(user_text):
#             if rag_context and len(rag_context.strip()) >= 40:
#                 return (
#                     "Guest asked about hotel food or menu. Answer with food menu options or food service availability only. "
#                     "Do NOT mention unrelated hotel services, manager contact, or database details. "
#                     "Keep it concise and helpful.\n\nGuest Question:\n" + user_text
#                 )
#             return (
#                 "Guest asked about hotel food or menu. Provide only the food options or food service availability. "
#                 "Do NOT mention unrelated hotel services. Keep the response short and friendly."
#             )
#         if rag_context and len(rag_context.strip()) >= 40:
#             return (
#                 "Answer ONLY using hotel knowledge provided in context. "
#                 "If answer not clearly available, say information is unavailable and offer manager contact. "
#                 "No general knowledge. No invented details.\n\nGuest Question:\n" + user_text
#             )
#         else:
#             return (
#                 "Guest asked a question but no relevant hotel information found. "
#                 "Politely say you can only help with hotel services. "
#                 "Suggest calling manager if needed. Warm, brief, 1-2 sentences."
#             )
#     elif intent == "event_inquiry":
#         return (
#             "Guest asking about event/party/special occasion booking. "
#             "Explain event bookings are managed by manager. "
#             "Share manager contact from system context. "
#             "Do NOT say 'transfer' or 'connect'. Just provide the number. Warm, brief."
#         )
#     elif intent == "escalation":
#         return (
#             "Guest wants manager or has urgent issue. "
#             "Acknowledge warmly. Share manager contact from system context. "
#             "Do NOT say 'transfer' or 'connect'. Just the number. Brief, reassuring."
#         )
#     elif intent == "farewell":
#         return (
#             "Guest ending call. Warm brief farewell as hotel concierge. "
#             "Mention the hotel name and wish the guest a pleasant stay. "
#             "1-2 sentences. Do NOT ask if there's anything else — the call is ending."
#         )
#     elif intent in ("small_talk", "greeting"):
#         return (
#             f"Guest said: \"{user_text}\". "
#             "Respond warmly as a hotel concierge. Keep it brief and friendly. "
#             "Gently ask how you can help them with hotel services."
#         )
#     else:
#         return user_text


# # ─────────────────────────────────────────────────────────────────────────────
# # WEBSOCKET HANDLER
# # ─────────────────────────────────────────────────────────────────────────────

# @router.websocket("/media-stream")
# async def media_stream(websocket: WebSocket):
#     await websocket.accept()
#     logger.info("🔌 WebSocket connected")

#     call_sid:        Optional[str] = None
#     stream_sid:      Optional[str] = None
#     hotel_id:        Optional[str] = None
#     hotel_name:      str           = "Hotel"
#     system_prompt:   str           = ""
#     manager_contact: str           = ""
#     hotel_email:     str           = ""
#     caller_number:   str           = ""
#     guest_room:      str           = ""
#     past_history:    List[Dict]    = []

#     stt: Optional[DeepgramService] = None

#     _audio_queue:       asyncio.Queue = asyncio.Queue(maxsize=256)
#     _audio_player_task: Optional[asyncio.Task] = None
#     _tts_queue:         asyncio.Queue = asyncio.Queue(maxsize=64)
#     _tts_worker_task:   Optional[asyncio.Task] = None

#     is_speaking    = False
#     processing     = False
#     _call_ending   = False
#     _playback_buffer_sec = 0.0
#     _current_call_order_items: List[str] = []

#     # ── BARGE-IN state ─────────────────────────────────────────────────────────
#     _agent_talking = False

#     _agent_interrupted:   bool  = False
#     _barge_in_enabled_at: float = 0.0
#     _barge_audio_hits:    int   = 0
#     _last_barge_in_at:    float = 0.0
#     _room_asked:          bool  = False
#     _items_asked:         bool  = False
#     _order_confirmed:     bool  = False

#     _awaiting_email_confirmation: bool = False
#     _email_sent:                  bool = False
#     _email_confirmed:             bool = False
#     _email_reminder_task:         Optional[asyncio.Task] = None

#     _current_tts_language: str = "hinglish"

#     _debounce_task:       Optional[asyncio.Task] = None
#     _llm_task:            Optional[asyncio.Task] = None
#     _pending_transcript: List[str]              = []

#     _AUDIO_DONE = object()

#     # ─────────────────────────────────────────────────────────────────────────
#     # TTS WORKER
#     # ─────────────────────────────────────────────────────────────────────────

#     async def tts_worker():
#         pending: asyncio.Queue = asyncio.Queue()

#         async def _fetch_phrase(text: str, language: str) -> list:
#             chunks = []
#             try:
#                 async for b64 in tts_service.stream_to_base64_chunks(text, language=language):
#                     chunks.append(b64)
#             except asyncio.CancelledError:
#                 raise
#             except Exception as e:
#                 logger.error(f"tts_worker fetch: {e}")

#             if not chunks:
#                 try:
#                     fallback_b64 = await tts_service.synthesize_to_base64(text, language=language)
#                     if fallback_b64:
#                         chunks.append(fallback_b64)
#                 except Exception as e:
#                     logger.error(f"tts_worker fallback synthesize: {e}")
#             return chunks

#         async def dispatcher():
#             while True:
#                 item = None
#                 done_called = False
#                 try:
#                     item = await _tts_queue.get()
#                     done_called = False

#                     if item is _TTS_STOP:
#                         _tts_queue.task_done()
#                         done_called = True
#                         await pending.put(_TTS_STOP)
#                         break

#                     if item is _TTS_DONE:
#                         _tts_queue.task_done()
#                         done_called = True
#                         await pending.put(_TTS_DONE)
#                         continue

#                     if isinstance(item, tuple) and len(item) == 2:
#                         text, lang = item
#                     else:
#                         text, lang = item, "hinglish"

#                     fut = asyncio.ensure_future(_fetch_phrase(text, lang))
#                     await pending.put(fut)
#                     _tts_queue.task_done()
#                     done_called = True

#                 except asyncio.CancelledError:
#                     if not done_called and item is not None:
#                         try:
#                             _tts_queue.task_done()
#                         except ValueError:
#                             pass
#                     await pending.put(_TTS_STOP)
#                     break
#                 except Exception as e:
#                     logger.error(f"tts dispatcher: {e}")
#                     if not done_called and item is not None:
#                         try:
#                             _tts_queue.task_done()
#                         except ValueError:
#                             pass

#         async def flusher():
#             while True:
#                 try:
#                     item = await pending.get()
#                     if item is _TTS_STOP:
#                         break
#                     if item is _TTS_DONE:
#                         await _audio_queue.put(_AUDIO_DONE)
#                         continue
#                     try:
#                         chunks = await item
#                         for chunk in chunks:
#                             await _audio_queue.put(chunk)
#                     except asyncio.CancelledError:
#                         raise
#                     except Exception as e:
#                         logger.error(f"tts flusher: {e}")
#                 except asyncio.CancelledError:
#                     while True:
#                         try:
#                             leftover = pending.get_nowait()
#                             if asyncio.isfuture(leftover):
#                                 leftover.cancel()
#                         except asyncio.QueueEmpty:
#                             break
#                     break

#         d_task = asyncio.create_task(dispatcher())
#         f_task = asyncio.create_task(flusher())
#         try:
#             await asyncio.gather(d_task, f_task)
#         except asyncio.CancelledError:
#             d_task.cancel()
#             f_task.cancel()
#             try:
#                 await asyncio.gather(d_task, f_task, return_exceptions=True)
#             except Exception:
#                 pass
#             raise

#     # ─────────────────────────────────────────────────────────────────────────
#     # AUDIO PLAYER
#     # ─────────────────────────────────────────────────────────────────────────

#     async def audio_player():
#         nonlocal is_speaking, _agent_talking, _playback_buffer_sec
#         while True:
#             try:
#                 item = await _audio_queue.get()

#                 if item is _AUDIO_DONE:
#                     _audio_queue.task_done()
#                     playback_wait = min(_playback_buffer_sec, MAX_PLAYBACK_TRACK_SEC)
#                     _playback_buffer_sec = 0.0
#                     if playback_wait > 0:
#                         await asyncio.sleep(playback_wait)
#                     is_speaking    = False
#                     _agent_talking = False  
#                     logger.debug("🔇 Agent done speaking — barge-in disabled")
#                     continue

#                 if not _agent_talking:
#                     _agent_talking = True
#                     logger.debug("🔊 Agent started speaking — barge-in enabled")

#                 is_speaking = True
#                 await _send_b64(item)
#                 _audio_queue.task_done()

#             except asyncio.CancelledError:
#                 is_speaking    = False
#                 _agent_talking = False
#                 break
#             except Exception as e:
#                 logger.error(f"Audio player: {e}")
#                 is_speaking = False
#                 try:
#                     _audio_queue.task_done()
#                 except ValueError:
#                     pass

#     async def _send_b64(b64: str):
#         nonlocal _playback_buffer_sec
#         try:
#             if not b64:
#                 logger.warning("⚠️ _send_b64 called with empty payload")
#                 return
#             if not stream_sid:
#                 logger.warning("⚠️ _send_b64 skipped because stream_sid is missing")
#                 return
#             if websocket.client_state.value == 2:  # 2 == DISCONNECTED
#                 logger.warning("⚠️ Tried writing base64 payload to closed websocket.")
#                 return
#             await websocket.send_text(json.dumps({
#                 "event":     "media",
#                 "streamSid": stream_sid,
#                 "media":     {"payload": b64},
#             }))
#             _playback_buffer_sec = min(
#                 _playback_buffer_sec + _estimate_mulaw_audio_seconds(b64),
#                 MAX_PLAYBACK_TRACK_SEC,
#             )
#         except Exception as e:
#             logger.error(f"_send_b64 error: {e}")

#     async def _enqueue_tts(text: str, language: str = "hinglish"):
#         _ensure_workers()
#         try:
#             _tts_queue.put_nowait((text, language))
#         except asyncio.QueueFull:
#             logger.warning("TTS queue full — dropping phrase")

#     async def _enqueue_tts_done():
#         try:
#             _tts_queue.put_nowait(_TTS_DONE)
#         except asyncio.QueueFull:
#             pass

#     async def _speak_simple(text: str, language: str = "hinglish"):
#         _ensure_workers()
#         try:
#             b64 = await tts_service.synthesize_to_base64(text, language=language)
#             if b64:
#                 _agent_talking = True
#                 await _audio_queue.put(b64)
#                 await _audio_queue.put(_AUDIO_DONE)
#         except Exception as e:
#             logger.error(f"_speak_simple: {e}")

#     async def _speak_stream(text: str, language: str = "hinglish"):
#         if len(text.strip()) <= 80:
#             await _speak_simple(text, language=language)
#             return

#         _ensure_workers()
#         try:
#             await _enqueue_tts(text, language)
#             await _enqueue_tts_done()
#         except Exception as e:
#             logger.error(f"_speak_stream: {e}")

#     async def _cancel_email_reminder():
#         nonlocal _email_reminder_task
#         if _email_reminder_task and not _email_reminder_task.done():
#             _email_reminder_task.cancel()
#             try:
#                 await _email_reminder_task
#             except asyncio.CancelledError:
#                 pass
#         _email_reminder_task = None

#     async def _email_followup_prompt(detected_lang: str = "hinglish"):
#         try:
#             await asyncio.sleep(2.0)
#             if _awaiting_email_confirmation:
#                 prompt = (
#                     "Did you receive the email? Please let me know."
#                     if detected_lang == "english"
#                     else "Kya aapko email mila? Kripya bataiye."
#                 )
#                 await _speak_stream(prompt, language=detected_lang)
#         except asyncio.CancelledError:
#             pass

#     def _infer_language_from_text(text: str, fallback: str = "hinglish") -> str:
#         if not text or len(text.strip()) <= 2:
#             return fallback
#         words = _words_lower(text)
#         if not words:
#             return fallback

#         hindi_keywords = {
#             "aap", "aapka", "aapke", "mujhe", "mera", "meri", "kya", "hai", "nahi", "nahin",
#             "chahiye", "ka", "ke", "ki", "kyun", "kab", "kahan", "ho", "hoon", "hai", "tha",
#             "thi", "raha", "rahi", "hain", "tum", "bataiye", "karo", "karna", "do", "tera",
#             "yeh", "woh", "hain", "bhai", "sahi", "samajh", "mila", "mujhe",
#         }
#         english_keywords = {
#             "please", "order", "menu", "options", "what", "when", "where", "who", "can", "could",
#             "would", "will", "yes", "no", "help", "hello", "hi", "thank", "thanks", "good",
#             "morning", "evening", "service", "drink", "food", "manager", "receive", "provide",
#             "list", "available", "any", "other", "want", "need", "sure", "okay", "sorry",
#         }
#         weak_english = {"email", "room", "service", "menu", "help", "contact", "phone", "number"}

#         count_hi = len(words & hindi_keywords)
#         count_en = len(words & english_keywords)
#         count_weak_en = len(words & weak_english)

#         if count_hi >= 2 and count_en == 0:
#             return "hindi"
#         if count_hi > count_en and count_hi >= 1 and count_en <= 1:
#             return "hindi"
#         if count_en >= 2 and count_hi == 0:
#             return "english"
#         if count_en > count_hi and count_en >= 2:
#             return "english"
#         if count_hi > 0 and count_en > 0:
#             return "hinglish"
#         if count_hi > 0 and count_en == 0:
#             return "hindi"
#         if count_en > 0 and count_hi == 0:
#             return "english"

#         if re.search(r"\b(?:kya|hai|nahi|nahin|chahiye|mujhe|aap|aapko|aapka|aapki|kaise|kab|kahan)\b", text, re.IGNORECASE):
#             return "hindi" if count_en == 0 else "hinglish"
#         if re.search(r"\b(?:please|order|menu|options|email|receive|help|room|service|what|when|where|why|can)\b", text, re.IGNORECASE):
#             return "english"

#         if count_weak_en > 0 and count_hi > 0:
#             return "hinglish"

#         return fallback

#     def _detect_language(text: str, fallback: str = "hinglish") -> str:
#         return _infer_language_from_text(text, fallback=fallback)

#     def _ensure_workers():
#         nonlocal _audio_player_task, _tts_worker_task
#         if not _audio_player_task or _audio_player_task.done():
#             _audio_player_task = asyncio.create_task(audio_player())
#         if not _tts_worker_task or _tts_worker_task.done():
#             _tts_worker_task = asyncio.create_task(tts_worker())

#     # ─────────────────────────────────────────────────────────────────────────
#     # PIPELINE
#     # ─────────────────────────────────────────────────────────────────────────

#     async def _pipeline(
#         user_text: str,
#         intent: str,
#         detected_lang: str,
#         extracted_items: List[str],
#         merged_history: List[Dict],
#         active_prompt: str,
#         rag_prefetch: str = "",
#     ) -> str:
#         nonlocal guest_room, _agent_talking

#         rag_ctx = ""
#         if intent in RAG_INTENTS:
#             if rag_prefetch:
#                 rag_ctx = rag_prefetch
#                 logger.debug("⚡ RAG reused from prefetch")
#             else:
#                 rag_ctx = await retrieval_service.search(user_text, hotel_id, top_k=3)

#         asyncio.create_task(_do_db_write(
#             intent=intent, hotel_id=hotel_id, hotel_name=hotel_name,
#             caller_number=caller_number, guest_room=guest_room,
#             extracted_items=extracted_items, user_text=user_text,
#         ))

#         instruction = _build_instruction(
#             intent=intent, user_text=user_text, extracted_items=extracted_items,
#             guest_room=guest_room, manager_contact=manager_contact, rag_context=rag_ctx,
#         )
#         messages_for_llm = merged_history + [{"role": "user", "content": instruction}]

#         word_buf = WordStreamBuffer()
#         full_response = []
#         should_end    = (intent == "farewell")

#         token_stream = qwen_service.stream_response(
#             messages=messages_for_llm,
#             hotel_id=hotel_id,
#             hotel_name=hotel_name,
#             system_prompt=active_prompt,
#             rag_context=rag_ctx,
#             manager_contact=manager_contact,
#             guest_room=guest_room,
#         )

#         try:
#             async for token in token_stream:
#                 full_response.append(token)
#                 flush_text = word_buf.feed(token)
#                 if flush_text:
#                     _agent_talking = True
#                     await _enqueue_tts(flush_text, detected_lang)
#         except Exception as exc:
#             logger.error(f"LLM stream error: {exc}")
#             # Fallback to spoken apology if the LLM stream fails entirely.
#             full_response = []
#         finally:
#             remainder = word_buf.flush()
#             if remainder:
#                 await _enqueue_tts(remainder, detected_lang)
#             await _enqueue_tts_done()

#         response_text = "".join(full_response).strip()
#         if not response_text:
#             response_text = "Maafi chahta hoon, kya aap dobara bol sakte hain?"
#             b64 = await tts_service.synthesize_to_base64(response_text, language=detected_lang)
#             if b64:
#                 _agent_talking = True
#                 await _audio_queue.put(b64)
#                 await _audio_queue.put(_AUDIO_DONE)

#         if should_end:
#             asyncio.create_task(_end_call_after_audio())

#         return response_text

#     # ─────────────────────────────────────────────────────────────────────────
#     # BARGE-IN ACTION
#     # ─────────────────────────────────────────────────────────────────────────

#     _barge_in_lock = asyncio.Lock()

#     async def _detect_audio_barge_in(b64_payload: str):
#         nonlocal _barge_audio_hits, _last_barge_in_at

#         if _call_ending or time.time() < _barge_in_enabled_at:
#             _barge_audio_hits = 0
#             return
#         if not (_agent_talking or is_speaking):
#             _barge_audio_hits = 0
#             return

#         now = time.monotonic()
#         if now - _last_barge_in_at < BARGE_IN_COOLDOWN_SEC:
#             return

#         try:
#             mulaw_bytes = base64.b64decode(b64_payload)
#             pcm16 = audioop.ulaw2lin(mulaw_bytes, 2)
#             rms = audioop.rms(pcm16, 2)
#         except Exception:
#             return

#         if rms >= BARGE_IN_RMS_THRESHOLD:
#             _barge_audio_hits += 1
#         else:
#             _barge_audio_hits = 0

#         if _barge_audio_hits >= BARGE_IN_HIT_FRAMES:
#             _barge_audio_hits = 0
#             _last_barge_in_at = now
#             logger.info(f"🛑 Barge-in triggered by caller audio | rms={rms}")
#             await barge_in()

#     async def barge_in():
#         nonlocal is_speaking, processing, _llm_task, _debounce_task
#         nonlocal _audio_player_task, _tts_worker_task, _agent_interrupted, _agent_talking
#         nonlocal _barge_audio_hits, _playback_buffer_sec, _last_barge_in_at

#         now = time.monotonic()
#         if time.time() < _barge_in_enabled_at:
#             logger.debug("Barge-in blocked — grace period")
#             return

#         if _barge_in_lock.locked():
#             return

#         async with _barge_in_lock:
#             if not _agent_talking and not is_speaking:
#                 return  

#             _last_barge_in_at = now
#             logger.info("⚡ BARGE-IN → stopping agent immediately")
#             _agent_interrupted = True
#             _agent_talking     = False
#             is_speaking        = False
#             _barge_audio_hits  = 0
#             _playback_buffer_sec = 0.0

#             for task in (_debounce_task, _llm_task):
#                 if task and not task.done():
#                     task.cancel()
#                     try:
#                         await asyncio.wait_for(asyncio.shield(task), timeout=0.1)
#                     except (asyncio.CancelledError, asyncio.TimeoutError):
#                         pass

#             if _tts_worker_task and not _tts_worker_task.done():
#                 _tts_worker_task.cancel()
#                 try:
#                     await asyncio.wait_for(asyncio.shield(_tts_worker_task), timeout=0.1)
#                 except (asyncio.CancelledError, asyncio.TimeoutError):
#                     pass

#             if _audio_player_task and not _audio_player_task.done():
#                 _audio_player_task.cancel()
#                 try:
#                     await asyncio.wait_for(asyncio.shield(_audio_player_task), timeout=0.1)
#                 except (asyncio.CancelledError, asyncio.TimeoutError):
#                     pass

#             for q in (_tts_queue, _audio_queue):
#                 while True:
#                     try:
#                         q.get_nowait()
#                     except asyncio.QueueEmpty:
#                         break
#                 try:
#                     q._unfinished_tasks = 0  # type: ignore
#                     q.all_tasks_done.notify_all()  # type: ignore
#                 except Exception:
#                     pass

#             if stream_sid:
#                 try:
#                     if websocket.client_state.value != 2:
#                         await websocket.send_text(json.dumps({
#                             "event":     "clear",
#                             "streamSid": stream_sid,
#                         }))
#                 except Exception:
#                     pass

#             # FIXED HERE: Explicitly force unlock processing flag inside lock wrapper 
#             # to make sure the loop accepts consecutive turns perfectly.
#             processing     = False
#             _llm_task      = None

#             _audio_player_task = asyncio.create_task(audio_player())
#             _tts_worker_task   = asyncio.create_task(tts_worker())

#             if stt:
#                 stt.reset()
#             logger.info("✅ Barge-in complete — listening")

#     # ─────────────────────────────────────────────────────────────────────────
#     # STT CALLBACKS
#     # ─────────────────────────────────────────────────────────────────────────

#     async def on_interim_transcript(transcript: str):
#         clean = transcript.strip()
#         if not clean:
#             return

#         if _agent_talking or is_speaking:
#             now = time.monotonic()
#             if now - _last_barge_in_at < BARGE_IN_COOLDOWN_SEC:
#                 logger.debug("Barge-in interim suppressed by cooldown")
#                 return

#             words = clean.split()
#             if len(words) == 1 and len(clean) <= BARGE_IN_TRANSCRIPT_MIN_CHARS:
#                 logger.debug(f"Barge-in interim ignored short transcript: '{clean}'")
#                 return

#             logger.info(f"🛑 Barge-in triggered by interim: '{clean}'")
#             await barge_in()

#     async def on_final_transcript(transcript: str):
#         nonlocal _debounce_task
#         if _call_ending:
#             return
#         clean = transcript.strip()
#         if not clean or len(clean) < MIN_TRANSCRIPT_LEN:
#             return

#         if _agent_talking or is_speaking:
#             now = time.monotonic()
#             if now - _last_barge_in_at >= BARGE_IN_COOLDOWN_SEC:
#                 await barge_in()
#             else:
#                 logger.debug("Final transcript barge-in suppressed by cooldown")

#         _pending_transcript.append(clean)

#         if _debounce_task and not _debounce_task.done():
#             _debounce_task.cancel()
#         _debounce_task = asyncio.create_task(_debounced_process_joined())

#     async def _debounced_process_joined():
#         nonlocal _pending_transcript
#         try:
#             await asyncio.sleep(DEBOUNCE_DELAY)
#             if _pending_transcript:
#                 full = " ".join(_pending_transcript).strip()
#                 _pending_transcript.clear()
#                 if full and len(full.split()) >= MIN_WORD_COUNT:
#                     await _process_transcript(full)
#         except asyncio.CancelledError:
#             pass

#     # ─────────────────────────────────────────────────────────────────────────
#     # CORE PROCESSING
#     # ─────────────────────────────────────────────────────────────────────────

#     async def _process_transcript(transcript: str):
#         nonlocal processing, guest_room, _llm_task, _agent_interrupted
#         nonlocal _room_asked, _items_asked, _order_confirmed, _current_call_order_items
#         nonlocal _awaiting_email_confirmation, _email_sent, _email_reminder_task
#         nonlocal _current_tts_language

#         if processing:
#             return
#         processing = True
#         t0 = time.monotonic()
#         logger.info(f"💬 [{hotel_id}] Guest: {transcript}")
#         asyncio.create_task(_safe_append(hotel_id, caller_number, "guest", transcript))

#         try:
#             if not guest_room:
#                 extracted_room = _extract_room_from_text(transcript)
#                 if extracted_room:
#                     guest_room = extracted_room
#                     asyncio.create_task(
#                         redis_client.update_session(f"call:{call_sid}", {"guest_room": guest_room})
#                     )
#                     asyncio.create_task(
#                         redis_client.save_guest_room(caller_number, hotel_id, guest_room)
#                     )
#                     logger.info(f"🏠 Room set (early): {guest_room}")

#             if _awaiting_email_confirmation:
#                 lower_transcript = transcript.lower()
#                 detected_lang = _detect_language(transcript, fallback=_current_tts_language)
#                 _current_tts_language = detected_lang

#                 if _YES_RE.search(lower_transcript):
#                     _awaiting_email_confirmation = False
#                     await _cancel_email_reminder()
#                     if _email_sent:
#                         reply = (
#                             "Great! I have sent the email. How can I help you now?"
#                             if detected_lang == "english"
#                             else "Great! Aapko email mil gaya hai. Ab bataiye main aapki kaise madad kar sakta hoon?"
#                         )
#                     else:
#                         reply = (
#                             "Great! How can I help you now?"
#                             if detected_lang == "english"
#                             else "Great! Ab bataiye, main aapki kaise madad kar sakta hoon?"
#                         )
#                     await _speak_stream(reply, language=detected_lang)
#                     processing = False
#                     return

#                 if _NO_RE.search(lower_transcript):
#                     await _cancel_email_reminder()
#                     if hotel_email:
#                         await _speak_stream(
#                             "Theek hai, main turant email bhej raha hoon. Kripya thoda intezaar kijiye.",
#                             language=detected_lang,
#                         )
#                         try:
#                             email_sent = await send_email_async(
#                                 hotel_email,
#                                 f"Thank you from {hotel_name}",
#                                 (
#                                     f"Dear Valued Guest,\n\n"
#                                     f"Thank you for connecting with {hotel_name}.\n\n"
#                                     "We are happy to help you with room service, food delivery, room cleaning, "
#                                     "spa bookings, and any other guest requests.\n\n"
#                                     f"Guest phone: {caller_number}.\n\n"
#                                     "Please reply if you need anything else—we are here to serve you.\n\n"
#                                     f"Warm regards,\n{hotel_name} Concierge Team"
#                                 ),
#                             )
#                             if email_sent:
#                                 _email_sent = True
#                                 await _speak_stream(
#                                     "Maine email bhej diya hai. Kripya check kijiye. Jaise hi aapko mil jaye, haan bol dijiye.",
#                                     language=detected_lang,
#                                 )
#                             else:
#                                 await _speak_stream(
#                                     "Maaf kijiye, email bhejne mein dikkat aayi. Phir se koshish karoonga.",
#                                     language=detected_lang,
#                                 )
#                         except Exception as e:
#                             logger.error(f"Email send error: {e}")
#                             await _speak_stream(
#                                 "Maaf kijiye, email bhejne mein dikkat aayi. Thoda baad phir se boliyega.",
#                                 language=detected_lang,
#                             )
#                     else:
#                         await _speak_stream(
#                             "Maaf kijiye, mujhe hotel ki email address nahi mili. Ab aap bataiye main aapki kaise madad kar sakta hoon?",
#                             language=detected_lang,
#                         )
#                         _awaiting_email_confirmation = False
#                     if _awaiting_email_confirmation:
#                         _email_reminder_task = asyncio.create_task(_email_followup_prompt(detected_lang))
#                     processing = False
#                     return

#                 await _speak_stream(
#                     "Kya aapko email mila? Kripya bataiye.",
#                     language=detected_lang,
#                 )
#                 processing = False
#                 return

#             detected_lang = _detect_language(transcript, fallback=_current_tts_language)
#             _current_tts_language = detected_lang
#             if _is_order_status_request(transcript):
#                 stock_text = await _find_guest_order_summary(
#                     hotel_id, caller_number, guest_room, _current_call_order_items, detected_lang
#                 )
#                 if stock_text:
#                     await _speak_stream(stock_text, language=detected_lang)
#                 else:
#                     prompt = (
#                         "I don't see any order placed during this call. Would you like to order now?"
#                         if detected_lang == "english"
#                         else "Is call mein abhi tak koi order nahi hua hai. Kya aap ab order karna chahte hain?"
#                     )
#                     await _speak_stream(prompt, language=detected_lang)
#                 processing = False
#                 return

#             fast_intent_data = _fast_intent_from_text(transcript)
#             intent_task = None
#             if fast_intent_data:
#                 logger.debug(f"Fast intent matched | intent={fast_intent_data.get('intent')}")
#             else:
#                 intent_task = asyncio.create_task(
#                     qwen_service.classify_intent(
#                         user_text=transcript, hotel_id=hotel_id or "", hotel_name=hotel_name,
#                     )
#                 )
#             history_task = asyncio.create_task(
#                 redis_client.get_history(f"call:{call_sid}")
#             )

#             _SKIP_RAG_WORDS = {
#                 'bye', 'goodbye', 'thanks', 'thank', 'ok', 'okay', 'hello',
#                 'hi', 'namaste', 'shukriya', 'dhanyawad', 'alvida', 'chalo',
#                 'theek', 'tha', 'haan', 'nahi', 'koi', 'baat', 'nahi',
#             }
#             transcript_words = _words_lower(transcript)
#             _likely_no_rag = bool(transcript_words & _SKIP_RAG_WORDS) and len(transcript_words) <= 4

#             rag_early_task = None
#             if not _likely_no_rag:
#                 rag_early_task = asyncio.create_task(
#                     retrieval_service.search(transcript, hotel_id, top_k=1)  # ⚡ Reduced from 2 for speed
#                 )

#             if fast_intent_data:
#                 if rag_early_task:
#                     call_history, rag_early = await asyncio.gather(
#                         history_task, rag_early_task, return_exceptions=True
#                     )
#                 else:
#                     call_history = await history_task
#                     rag_early = ""
#                 intent_data = fast_intent_data
#             elif rag_early_task:
#                 intent_data, call_history, rag_early = await asyncio.gather(
#                     intent_task, history_task, rag_early_task, return_exceptions=True
#                 )
#             else:
#                 intent_data, call_history = await asyncio.gather(
#                     intent_task, history_task, return_exceptions=True
#                 )
#                 rag_early = ""

#             if isinstance(intent_data, Exception):
#                 logger.error(f"Intent failed: {intent_data}")
#                 intent_data = {"intent": "inquiry", "language": "hinglish", "room_number": None, "items": []}
#             if isinstance(call_history, Exception):
#                 call_history = []
#             if isinstance(rag_early, Exception):
#                 rag_early = ""

#             intent          = intent_data.get("intent", "inquiry")
#             classifier_lang = intent_data.get("language", "hinglish")
#             guessed_lang    = _infer_language_from_text(transcript, fallback=_current_tts_language)
#             detected_lang   = classifier_lang
#             if classifier_lang != guessed_lang and guessed_lang in ("english", "hindi"):
#                 logger.debug(
#                     f"Language override | classifier={classifier_lang} -> guessed={guessed_lang} | text={transcript[:120]}"
#                 )
#                 detected_lang = guessed_lang
#             extracted_items = _normalize_items(intent_data.get("items", []))
#             detected_room   = intent_data.get("room_number")
#             intent, extracted_items = _sanitize_food_intent(intent, transcript, extracted_items)

#             # FIXED HERE: Avoid single word short fillers from triggering hangups
#             if intent == "farewell" and len(transcript.split()) <= 2 and (
#                 _YES_RE.search(transcript) or _NO_RE.search(transcript)
#             ):
#                 intent = "small_talk"

#             intent_ms = (time.monotonic() - t0) * 1000
#             logger.info(
#                 f"⚡ Intent {intent_ms:.0f}ms | "
#                 f"intent={intent} | lang={detected_lang} | "
#                 f"room={detected_room} | items={extracted_items}"
#             )

#             if detected_room and not guest_room:
#                 guest_room = str(detected_room)
#                 asyncio.create_task(
#                     redis_client.update_session(f"call:{call_sid}", {"guest_room": guest_room})
#                 )
#                 asyncio.create_task(
#                     redis_client.save_guest_room(caller_number, hotel_id, guest_room)
#                 )
#                 logger.info(f"🏠 Room set (intent): {guest_room}")

#             if intent in ROOM_REQUIRED_INTENTS and not guest_room and not _room_asked:
#                 _room_asked = True
#                 q = ("Could you please tell me your room number?"
#                      if detected_lang == "english"
#                      else "Aapka room number kya hai please?")
#                 _ensure_workers()
#                 await _speak_simple(q, language=detected_lang)
#                 processing = False
#                 return

#             if intent == "food_order" and not extracted_items:
#                 _items_asked = True
#                 q = ("What would you like to order from our menu?"
#                      if detected_lang == "english"
#                      else "Aap kya order karna chahte hain? Menu se koi dish batayein.")
#                 _ensure_workers()
#                 await _speak_simple(q, language=detected_lang)
#                 processing = False
#                 return

#             if intent == "food_order" and extracted_items and guest_room:
#                 _order_confirmed = True
#                 _items_asked     = True

#             rag_for_pipeline = ""
#             if intent in RAG_INTENTS and isinstance(rag_early, str):
#                 rag_for_pipeline = rag_early

#             merged = (past_history[-6:] + (call_history if isinstance(call_history, list) else []))

#             if detected_lang == "english":
#                 _lang_reminder = {"role": "system", "content": "REMINDER: Respond in English only. Do not use Hindi or Hinglish."}
#             elif detected_lang == "hindi":
#                 _lang_reminder = {"role": "system", "content": "REMINDER: Respond in Roman Hindi only. Do not switch to full English."}
#             else:
#                 _lang_reminder = {"role": "system", "content": "REMINDER: Respond in natural Hinglish. Blend Hindi and English naturally."}
#             merged = merged + [_lang_reminder]

#             enriched = transcript
#             if _agent_interrupted:
#                 enriched = transcript + "\n[Note: Agent was interrupted mid-response.]"
#                 _agent_interrupted = False

#             active_prompt = _build_language_prompt(
#                 system_prompt=system_prompt,
#                 detected_lang=detected_lang,
#                 manager_contact=manager_contact,
#             )

#             _ensure_workers()
#             _agent_talking = True

#             _llm_task = asyncio.create_task(
#                 _pipeline(
#                     user_text=enriched,
#                     intent=intent,
#                     detected_lang=detected_lang,
#                     extracted_items=extracted_items,
#                     merged_history=merged,
#                     active_prompt=active_prompt,
#                     rag_prefetch=rag_for_pipeline,
#                 )
#             )

#             try:
#                 response_text = await _llm_task
#             except asyncio.CancelledError:
#                 logger.info("Pipeline cancelled (barge-in)")
#                 return
#             finally:
#                 # FIXED HERE: Ensure processing flag is cleared even on tasks 
#                 # that get caught by async pipeline cancellations.
#                 processing = False

#             total_ms = (time.monotonic() - t0) * 1000
#             logger.info(
#                 f"⚡ Done {total_ms:.0f}ms | "
#                 f"response={response_text[:60]}"
#             )

#             if intent == "food_order" and extracted_items:
#                 _order_confirmed = True
#                 _items_asked     = True
#                 _current_call_order_items.extend(extracted_items)

#             asyncio.create_task(redis_client.append_message(f"call:{call_sid}", "user", transcript))
#             asyncio.create_task(redis_client.append_message(f"call:{call_sid}", "assistant", response_text))
#             asyncio.create_task(redis_client.append_guest_memory(caller_number, hotel_id, "user", transcript))
#             asyncio.create_task(redis_client.append_guest_memory(caller_number, hotel_id, "assistant", response_text))
#             asyncio.create_task(_safe_append(hotel_id, caller_number, "agent", response_text))

#         except asyncio.CancelledError:
#             raise
#         except Exception as e:
#             logger.error(f"_process_transcript error: {e}", exc_info=True)
#             _ensure_workers()
#             await _speak_simple("Maafi chahta hoon, kya aap dobara bol sakte hain?")
#         finally:
#             processing = False

#     # ─────────────────────────────────────────────────────────────────────────
#     # TERMINATION CLEANUP
#     # ─────────────────────────────────────────────────────────────────────────

#     async def _end_call_after_audio():
#         nonlocal _call_ending
#         _call_ending = True
#         logger.info(f"👋 Waiting for farewell audio | call_sid={call_sid}")
#         try:
#             await asyncio.wait_for(_tts_queue.join(), timeout=6.0)
#             await asyncio.wait_for(_audio_queue.join(), timeout=6.0)
#         except asyncio.TimeoutError:
#             logger.warning("Farewell audio timeout — hanging up anyway")
#         except Exception:
#             pass

#         await _twilio_hangup(call_sid)
#         try:
#             if websocket.application_state.value != 2:
#                 await websocket.close()
#         except Exception:
#             pass

#     # ─────────────────────────────────────────────────────────────────────────
#     # CONNECTION LOOP
#     # ─────────────────────────────────────────────────────────────────────────

#     try:
#         _audio_player_task = asyncio.create_task(audio_player())
#         _tts_worker_task   = asyncio.create_task(tts_worker())

#         async for raw_message in websocket.iter_text():
#             try:
#                 msg = json.loads(raw_message)
#             except json.JSONDecodeError:
#                 continue

#             event = msg.get("event", "")

#             if event == "connected":
#                 logger.info("Twilio: connected")

#             elif event == "start":
#                 start_data      = msg.get("start", {})
#                 stream_sid      = msg.get("streamSid", "")
#                 call_sid        = start_data.get("callSid", "")

#                 session         = await redis_client.get_session(f"call:{call_sid}") or {}
#                 hotel_id        = session.get("hotel_id", "")
#                 hotel_name      = session.get("hotel_name", "Hotel")
#                 system_prompt   = session.get("system_prompt", "")
#                 manager_contact = session.get("manager_contact", "")
#                 hotel_email     = session.get("hotel_email", "")
#                 caller_number   = session.get("caller_number", "")
#                 guest_room      = session.get("guest_room", "")

#                 logger.info(
#                     f"📞 Stream start | call_sid={call_sid} | "
#                     f"hotel={hotel_name} | hotel_id={hotel_id}"
#                 )

#                 if not guest_room and caller_number and hotel_id:
#                     remembered = await redis_client.get_guest_room(caller_number, hotel_id)
#                     if remembered:
#                         guest_room = remembered
#                         logger.info(f"🧠 Room from memory: {guest_room}")

#                 if caller_number and hotel_id:
#                     past_history = await redis_client.get_guest_memory(caller_number, hotel_id, last_n=10)
#                     if past_history:
#                         logger.info(f"🧠 {len(past_history)} past turns | guest {caller_number[-4:]}***")

#                 stt = DeepgramService(
#                     hotel_id=hotel_id,
#                     on_final=on_final_transcript,
#                     on_interim=on_interim_transcript,
#                 )

#                 _ensure_workers()
#                 cached_b64    = get_cached_greeting_b64(hotel_id)
#                 greeting_text = get_cached_greeting_text(hotel_name)

#                 if cached_b64:
#                     logger.info(f"⚡ Cached greeting | hotel={hotel_name}")
#                     asyncio.create_task(stt.connect())
#                     _barge_in_enabled_at = time.time() + GREETING_GRACE_SEC
#                     _agent_talking = True
#                     await _send_b64(cached_b64)
#                     await _audio_queue.put(_AUDIO_DONE)
#                 else:
#                     logger.info(f"🔄 Cache miss → synthesize | hotel={hotel_name}")
#                     stt_t = asyncio.create_task(stt.connect())
#                     tts_t = asyncio.create_task(tts_service.synthesize_to_base64(greeting_text))
#                     greeting_b64, _ = await asyncio.gather(tts_t, stt_t, return_exceptions=True)
#                     if not isinstance(greeting_b64, Exception) and greeting_b64:
#                         set_cached_greeting(hotel_id, greeting_b64)
#                     else:
#                         greeting_b64 = None
#                     _barge_in_enabled_at = time.time() + GREETING_GRACE_SEC
#                     if greeting_b64:
#                         _agent_talking = True
#                         await _send_b64(greeting_b64)
#                         await _audio_queue.put(_AUDIO_DONE)
#                         logger.debug(f"🔊 Greeting: {greeting_text[:60]}")
#                     else:
#                         logger.warning("⚠️ Greeting TTS failed, using fallback speak_simple")
#                         _agent_talking = True
#                         await _speak_simple(greeting_text)

#                 _awaiting_email_confirmation = True
#                 _email_sent = False
#                 await _cancel_email_reminder()
#                 _email_reminder_task = asyncio.create_task(_email_followup_prompt())

#                 asyncio.create_task(pre_warm_greeting(hotel_id, hotel_name))

#                 async def _create_log():
#                     try:
#                         await mongo_client.create_call_log(
#                             hotel_id=hotel_id, hotel_name=hotel_name,
#                             guest_number=caller_number, guest_room=guest_room,
#                             call_sid=call_sid,
#                         )
#                     except Exception as e:
#                         logger.warning(f"create_call_log: {e}")
#                 asyncio.create_task(_create_log())

#             elif event == "media":
#                 if stt:
#                     payload = msg.get("media", {}).get("payload", "")
#                     if payload:
#                         await _detect_audio_barge_in(payload)
#                         await stt.send_base64_chunk(payload)

#             elif event == "stop":
#                 logger.info(f"📴 Stream stopped | call_sid={call_sid}")
#                 break

#     except WebSocketDisconnect:
#         logger.info(f"WS disconnected | call_sid={call_sid}")
#     except asyncio.CancelledError:
#         logger.info(f"WS cancelled | call_sid={call_sid}")
#     except Exception as e:
#         logger.error(f"WS error: {e}", exc_info=True)
#     finally:
#         logger.info(f"🧹 Cleanup | call_sid={call_sid}")

#         for t in (_debounce_task, _llm_task):
#             if t and not t.done():
#                 t.cancel()

#         try:
#             _tts_queue.put_nowait(_TTS_STOP)
#         except Exception:
#             pass
#         if _tts_worker_task and not _tts_worker_task.done():
#             _tts_worker_task.cancel()
#             try:
#                 await _tts_worker_task
#             except asyncio.CancelledError:
#                 pass

#         try:
#             _audio_queue.put_nowait(_AUDIO_DONE)
#         except Exception:
#             pass
#         if _audio_player_task and not _audio_player_task.done():
#             _audio_player_task.cancel()
#             try:
#                 await _audio_player_task
#             except asyncio.CancelledError:
#                 pass

#         if _email_reminder_task and not _email_reminder_task.done():
#             _email_reminder_task.cancel()
#             try:
#                 await _email_reminder_task
#             except asyncio.CancelledError:
#                 pass

#         if stt:
#             try:
#                 await stt.disconnect()
#             except Exception as e:
#                 logger.warning(f"STT disconnect: {e}")
#         if call_sid:
#             try:
#                 await redis_client.delete_session(f"call:{call_sid}")
#             except Exception:
#                 pass
#         logger.info(f"✅ Done | call_sid={call_sid}")


# async def _safe_append(hotel_id: str, caller_number: str, role: str, text: str):
#     try:
#         await mongo_client.append_conversation(
#             hotel_id=hotel_id,
#             guest_number=caller_number,
#             role=role,
#             message=text,
#         )
#     except Exception as e:
#         logger.warning(f"append_conversation: {e}")










"""
websocket/websocket_server.py

FIXES v10:
   ✅ _infer_language_from_text + _detect_language moved to MODULE LEVEL
      — were nested inside media_stream causing NameError on uvicorn --reload
   ✅ ALL hardcoded response strings removed — every reply goes through LLM prompt
   ✅ Hotel-only enforcement via system prompt — non-hotel questions politely declined
   ✅ No hardcoded "Maafi chahta hoon..." or similar strings in pipeline responses
   ✅ Fallback error message also prompt-driven where possible
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

router = APIRouter()

MIN_TRANSCRIPT_LEN    = 2
MIN_WORD_COUNT        = 1
DEBOUNCE_DELAY        = 0.05
GREETING_GRACE_SEC    = 3.5

BARGE_IN_RMS_THRESHOLD = 550
BARGE_IN_HIT_FRAMES    = 2
BARGE_IN_COOLDOWN_SEC  = 0.8
BARGE_IN_TRANSCRIPT_MIN_CHARS = 4
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


# ─────────────────────────────────────────────────────────────────────────────
# MODULE-LEVEL PURE UTILITY FUNCTIONS
# These must be at module level — NOT nested — to avoid NameError on reload
# ─────────────────────────────────────────────────────────────────────────────

def _words_lower(text: str) -> set:
    return set(re.findall(r"[a-zA-Z']+", text.lower()))


def _infer_language_from_text(text: str, fallback: str = "hinglish") -> str:
    """
    Pure function — no closure dependencies.
    Moved to module level to prevent NameError on uvicorn --reload.
    """
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


# ─────────────────────────────────────────────────────────────────────────────
# ROOM NUMBER EXTRACTION
# ─────────────────────────────────────────────────────────────────────────────

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


# ─────────────────────────────────────────────────────────────────────────────
# LANGUAGE + HOTEL-ONLY SYSTEM PROMPT BUILDER
# ─────────────────────────────────────────────────────────────────────────────

def _build_language_prompt(system_prompt: str, detected_lang: str, manager_contact: str) -> str:
    """
    Builds the active system prompt injected into every LLM call.
    HOTEL-ONLY rule is enforced here via prompt — no hardcoding in code.
    Language instruction is also prompt-driven.
    """
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


# ─────────────────────────────────────────────────────────────────────────────
# HELPERS
# ─────────────────────────────────────────────────────────────────────────────

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
        if has_order_action:
            logger.info("Food order needs clarification: no explicit item in transcript")
            return intent, []
        logger.info("Food order converted to inquiry: classifier inferred unspoken items")
        return "inquiry", []

    return intent, filtered_items


def _is_order_status_request(text: str) -> bool:
    return bool(re.search(
        r"\b(?:what\s+did\s+i\s+order|what\s+is\s+my\s+order|what\s+have\s+i\s+ordered|what\s+did\s+i\s+buy|my\s+order|order\s+details|order\s+status|what\s+is\s+my\s+current\s+order)\b",
        text, re.IGNORECASE,
    ))


def _fast_intent_from_text(text: str) -> Optional[Dict]:
    clean = text.strip()
    if not clean:
        return None

    lang = _infer_language_from_text(clean, fallback="hinglish")

    if _GREETING_RE.match(clean):
        return {"intent": "small_talk", "language": lang, "room_number": None, "items": []}

    if _FAREWELL_RE.search(clean):
        return {"intent": "farewell", "language": lang, "room_number": None, "items": []}

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
                f"Your current order for room {room} includes: {items_str}. "
                "Would you like to add or change anything?"
            )
        return (
            f"Is call mein aapka order room {room} ke liye hai: {items_str}. "
            "Kya aap kuch aur add ya change karna chahte hain?"
        )
    return None


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
        logger.error(f"_twilio_hangup error: {e}")


# ─────────────────────────────────────────────────────────────────────────────
# DB WRITE HELPERS
# ─────────────────────────────────────────────────────────────────────────────

async def _do_db_write(
    intent: str,
    hotel_id: str,
    hotel_name: str,
    caller_number: str,
    guest_room: str,
    extracted_items: List[str],
    user_text: str,
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
            )
            logger.info(f"💾 DB: food_order saved | items={extracted_items} | room={guest_room}")

        elif intent == "room_cleaning":
            items = extracted_items or [user_text]
            await mongo_client.upsert_room_cleaning(
                hotel_id=hotel_id, hotel_name=hotel_name,
                guest_number=caller_number, guest_room=guest_room,
                requests=items,
            )
            logger.info(f"💾 DB: room_cleaning saved | items={items}")

        elif intent == "spa_service":
            items = extracted_items or [user_text]
            await mongo_client.upsert_spa_service(
                hotel_id=hotel_id, hotel_name=hotel_name,
                guest_number=caller_number, guest_room=guest_room,
                services=items,
            )
            logger.info(f"💾 DB: spa_service saved | items={items}")

        elif intent == "essential_needs":
            items = extracted_items or [user_text]
            await mongo_client.upsert_essential_needs(
                hotel_id=hotel_id, hotel_name=hotel_name,
                guest_number=caller_number, guest_room=guest_room,
                needs=items,
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


# ─────────────────────────────────────────────────────────────────────────────
# LLM INSTRUCTION BUILDER — ALL PROMPT-DRIVEN, ZERO HARDCODED RESPONSES
# ─────────────────────────────────────────────────────────────────────────────

def _build_instruction(
    intent: str,
    user_text: str,
    extracted_items: List[str],
    guest_room: str,
    manager_contact: str,
    rag_context: str,
    hotel_name: str = "",
) -> str:
    """
    Builds the instruction injected as the final user message to the LLM.
    Every intent is handled via prompt — no hardcoded reply strings anywhere.
    The LLM generates all text including polite refusals for non-hotel queries.
    """
    items_str = ", ".join(extracted_items) if extracted_items else "the requested items"
    room_str  = guest_room or "not yet provided"

    if intent == "food_order":
        return (
            f"The guest has placed a food order.\n"
            f"Items ordered: {items_str}.\n"
            f"Guest room: {room_str}.\n"
            f"Original request: \"{user_text}\".\n"
            "Task: Confirm the order warmly and naturally. Mention the items and room number. "
            "Include estimated delivery time ONLY if it is mentioned in the hotel knowledge base — "
            "never invent a time. Keep it short, no bullet points."
        )

    elif intent == "room_cleaning":
        return (
            f"The guest has requested room cleaning or housekeeping.\n"
            f"Requests: {items_str}.\n"
            f"Guest room: {room_str}.\n"
            f"Original request: \"{user_text}\".\n"
            "Task: Confirm the housekeeping request warmly. Mention what will be done. "
            "Include timing only if available in the hotel knowledge base. Keep it short."
        )

    elif intent == "spa_service":
        return (
            f"The guest has booked a spa or wellness service.\n"
            f"Services: {items_str}.\n"
            f"Guest room: {room_str}.\n"
            f"Original request: \"{user_text}\".\n"
            "Task: Confirm the spa booking warmly. Include timing or availability "
            "only if present in the hotel knowledge base. Keep it short."
        )

    elif intent == "essential_needs":
        return (
            f"The guest has requested essential items or amenities.\n"
            f"Items needed: {items_str}.\n"
            f"Guest room: {room_str}.\n"
            f"Original request: \"{user_text}\".\n"
            "Task: Confirm warmly that the items will be delivered. "
            "Mention delivery time only if available in the hotel knowledge base. Keep it short."
        )

    elif intent == "inquiry":
        if not rag_context or len(rag_context.strip()) < 40:
            return (
                f"The guest asked: \"{user_text}\".\n"
                "The hotel knowledge base does not contain relevant information for this query.\n"
                "Task: If this is a hotel-related question, politely say the information is not "
                "currently available and suggest contacting the manager. "
                "If this is NOT a hotel-related question (general knowledge, weather, news, etc.), "
                "politely decline to answer and explain you can only assist with hotel services. "
                "Be warm and brief."
            )
        if _is_food_related_inquiry(user_text):
            return (
                f"The guest asked about food or the menu: \"{user_text}\".\n"
                "Task: Answer using ONLY the food/menu information from the hotel knowledge base. "
                "Do not mention unrelated hotel services. Keep it concise and helpful."
            )
        return (
            f"The guest asked: \"{user_text}\".\n"
            "Task: Answer using ONLY the hotel knowledge provided in the context above. "
            "Do not use general knowledge. Do not invent information. "
            "If the answer is not in the hotel knowledge base, say it is unavailable "
            "and suggest contacting the manager. Keep it short."
        )

    elif intent == "event_inquiry":
        return (
            f"The guest is asking about an event, party, or special occasion: \"{user_text}\".\n"
            "Task: Explain that event bookings are managed by the hotel manager. "
            "Share the manager's contact number. "
            "Do NOT say you will 'transfer' or 'connect' the guest — just provide the number. "
            "Be warm and brief."
        )

    elif intent == "escalation":
        return (
            f"The guest wants to speak with a manager or has an urgent concern: \"{user_text}\".\n"
            "Task: Acknowledge their concern warmly. Share the manager's contact number. "
            "Do NOT say you will 'transfer' or 'connect' the guest. "
            "Just provide the number and say they can call directly. Brief and reassuring."
        )

    elif intent == "farewell":
        return (
            f"The guest is ending the call: \"{user_text}\".\n"
            "Task: Give a warm, brief farewell as a hotel concierge. "
            f"Mention {hotel_name} by name. Wish them a pleasant stay. "
            "1 to 2 sentences only. Do NOT ask if there is anything else."
        )

    elif intent in ("small_talk", "greeting"):
        return (
            f"The guest said: \"{user_text}\".\n"
            "Task: Respond warmly and briefly as a hotel concierge. "
            "Gently ask how you can assist them with hotel services today. "
            "Keep it to 1-2 sentences."
        )

    else:
        # Unknown intent — treat as inquiry, let LLM decide
        return (
            f"The guest said: \"{user_text}\".\n"
            "Task: Respond helpfully. If this is a hotel-related request, assist. "
            "If it is unrelated to the hotel, politely decline and redirect to hotel services."
        )


# ─────────────────────────────────────────────────────────────────────────────
# WEBSOCKET HANDLER
# ─────────────────────────────────────────────────────────────────────────────

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

    _awaiting_email_confirmation: bool = False
    _email_sent:                  bool = False
    _email_confirmed:             bool = False
    _email_reminder_task:         Optional[asyncio.Task] = None

    _current_tts_language: str = "hinglish"

    _debounce_task:      Optional[asyncio.Task] = None
    _llm_task:           Optional[asyncio.Task] = None
    _pending_transcript: List[str]              = []

    _AUDIO_DONE = object()

    # ─────────────────────────────────────────────────────────────────────────
    # TTS WORKER
    # ─────────────────────────────────────────────────────────────────────────

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

    # ─────────────────────────────────────────────────────────────────────────
    # AUDIO PLAYER
    # ─────────────────────────────────────────────────────────────────────────

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
                    logger.debug("🔇 Agent done speaking — barge-in disabled")
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
            if not b64:
                return
            if not stream_sid:
                return
            if websocket.client_state.value == 2:
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

    async def _speak_simple(text: str, language: str = "hinglish"):
        _ensure_workers()
        try:
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
        """
        Prompt-driven email follow-up — LLM generates the question.
        """
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

    # ─────────────────────────────────────────────────────────────────────────
    # PIPELINE — fully LLM-driven, zero hardcoded responses
    # ─────────────────────────────────────────────────────────────────────────

    async def _pipeline(
        user_text: str,
        intent: str,
        detected_lang: str,
        extracted_items: List[str],
        merged_history: List[Dict],
        active_prompt: str,
        rag_prefetch: str = "",
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

        if not response_text:
            # Fallback: ask LLM for a brief apology — still prompt-driven
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
                b64 = await tts_service.synthesize_to_base64(response_text, language=detected_lang)
                if b64:
                    _agent_talking = True
                    await _audio_queue.put(b64)
                    await _audio_queue.put(_AUDIO_DONE)

        if should_end:
            asyncio.create_task(_end_call_after_audio())

        return response_text

    # ─────────────────────────────────────────────────────────────────────────
    # BARGE-IN
    # ─────────────────────────────────────────────────────────────────────────

    _barge_in_lock = asyncio.Lock()

    async def _detect_audio_barge_in(b64_payload: str):
        nonlocal _barge_audio_hits, _last_barge_in_at

        if _call_ending or time.time() < _barge_in_enabled_at:
            _barge_audio_hits = 0
            return
        if not (_agent_talking or is_speaking):
            _barge_audio_hits = 0
            return

        now = time.monotonic()
        if now - _last_barge_in_at < BARGE_IN_COOLDOWN_SEC:
            return

        try:
            mulaw_bytes = base64.b64decode(b64_payload)
            pcm16       = audioop.ulaw2lin(mulaw_bytes, 2)
            rms         = audioop.rms(pcm16, 2)
        except Exception:
            return

        if rms >= BARGE_IN_RMS_THRESHOLD:
            _barge_audio_hits += 1
        else:
            _barge_audio_hits = 0

        if _barge_audio_hits >= BARGE_IN_HIT_FRAMES:
            _barge_audio_hits = 0
            _last_barge_in_at = now
            logger.info(f"🛑 Barge-in triggered by caller audio | rms={rms}")
            await barge_in()

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

    # ─────────────────────────────────────────────────────────────────────────
    # STT CALLBACKS
    # ─────────────────────────────────────────────────────────────────────────

    async def on_interim_transcript(transcript: str):
        clean = transcript.strip()
        if not clean:
            return

        if _agent_talking or is_speaking:
            now = time.monotonic()
            if now - _last_barge_in_at < BARGE_IN_COOLDOWN_SEC:
                logger.debug("Barge-in interim suppressed by cooldown")
                return
            words = clean.split()
            if len(words) == 1 and len(clean) <= BARGE_IN_TRANSCRIPT_MIN_CHARS:
                logger.debug(f"Barge-in interim ignored short transcript: '{clean}'")
                return
            logger.info(f"🛑 Barge-in triggered by interim: '{clean}'")
            await barge_in()

    async def on_final_transcript(transcript: str):
        nonlocal _debounce_task
        if _call_ending:
            return
        clean = transcript.strip()
        if not clean or len(clean) < MIN_TRANSCRIPT_LEN:
            return

        if _agent_talking or is_speaking:
            now = time.monotonic()
            if now - _last_barge_in_at >= BARGE_IN_COOLDOWN_SEC:
                await barge_in()
            else:
                logger.debug("Final transcript barge-in suppressed by cooldown")

        _pending_transcript.append(clean)

        if _debounce_task and not _debounce_task.done():
            _debounce_task.cancel()
        _debounce_task = asyncio.create_task(_debounced_process_joined())

    async def _debounced_process_joined():
        nonlocal _pending_transcript
        try:
            await asyncio.sleep(DEBOUNCE_DELAY)
            if _pending_transcript:
                full = " ".join(_pending_transcript).strip()
                _pending_transcript.clear()
                if full and len(full.split()) >= MIN_WORD_COUNT:
                    await _process_transcript(full)
        except asyncio.CancelledError:
            pass

    # ─────────────────────────────────────────────────────────────────────────
    # CORE PROCESSING
    # ─────────────────────────────────────────────────────────────────────────

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
            # ── Early room extraction ─────────────────────────────────────────
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

            # ── Email confirmation flow ───────────────────────────────────────
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
                        # Inform guest we're sending the email — prompt-driven
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

                # Neither yes nor no — re-ask via LLM
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

            # ── Normal conversation flow ──────────────────────────────────────
            detected_lang         = _detect_language(transcript, fallback=_current_tts_language)
            _current_tts_language = detected_lang

            if _is_order_status_request(transcript):
                stock_text = await _find_guest_order_summary(
                    hotel_id, caller_number, guest_room,
                    _current_call_order_items, detected_lang,
                )
                if stock_text:
                    await _speak_stream(stock_text, language=detected_lang)
                else:
                    active_prompt = _build_language_prompt(
                        system_prompt=system_prompt,
                        detected_lang=detected_lang,
                        manager_contact=manager_contact,
                    )
                    no_order_instruction = (
                        "The guest asked about their order but no order has been placed in this call yet. "
                        "Inform them politely and ask if they would like to order something. "
                        "1-2 sentences."
                    )
                    no_order_response = await qwen_service.get_full_response(
                        messages=[{"role": "user", "content": no_order_instruction}],
                        hotel_id=hotel_id or "",
                        hotel_name=hotel_name,
                        system_prompt=active_prompt,
                    )
                    if no_order_response:
                        await _speak_stream(no_order_response, language=detected_lang)
                processing = False
                return

            # ── Intent classification ─────────────────────────────────────────
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

            # Guard: short filler words must not trigger farewell/hangup
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

            # ── Room required check ───────────────────────────────────────────
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

            # ── Food items check ──────────────────────────────────────────────
            if intent == "food_order" and not extracted_items:
                _items_asked  = True
                active_prompt = _build_language_prompt(
                    system_prompt=system_prompt,
                    detected_lang=detected_lang,
                    manager_contact=manager_contact,
                )
                items_q_instruction = (
                    "Ask the guest what they would like to order from the menu. "
                    "1 sentence only."
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

            if intent == "food_order" and extracted_items and guest_room:
                _order_confirmed = True
                _items_asked     = True

            rag_for_pipeline = ""
            if intent in RAG_INTENTS and isinstance(rag_early, str):
                rag_for_pipeline = rag_early

            merged = (past_history[-6:] + (call_history if isinstance(call_history, list) else []))

            # Language reminder injected as system message
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

            if intent == "food_order" and extracted_items:
                _order_confirmed = True
                _items_asked     = True
                _current_call_order_items.extend(extracted_items)

            asyncio.create_task(redis_client.append_message(f"call:{call_sid}", "user", transcript))
            asyncio.create_task(redis_client.append_message(f"call:{call_sid}", "assistant", response_text))
            asyncio.create_task(redis_client.append_guest_memory(caller_number, hotel_id, "user", transcript))
            asyncio.create_task(redis_client.append_guest_memory(caller_number, hotel_id, "assistant", response_text))
            asyncio.create_task(_safe_append(hotel_id, caller_number, "agent", response_text))

        except asyncio.CancelledError:
            raise
        except Exception as e:
            logger.error(f"_process_transcript error: {e}", exc_info=True)
            # Prompt-driven error response
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

    # ─────────────────────────────────────────────────────────────────────────
    # CALL END
    # ─────────────────────────────────────────────────────────────────────────

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

        await _twilio_hangup(call_sid)
        try:
            if websocket.application_state.value != 2:
                await websocket.close()
        except Exception:
            pass

    # ─────────────────────────────────────────────────────────────────────────
    # CONNECTION LOOP
    # ─────────────────────────────────────────────────────────────────────────

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
                        _agent_talking = True
                        await _send_b64(greeting_b64)
                        await _audio_queue.put(_AUDIO_DONE)
                        logger.debug(f"🔊 Greeting: {greeting_text[:60]}")
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
                        await _detect_audio_barge_in(payload)
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