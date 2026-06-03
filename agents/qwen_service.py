"""
agents/qwen_service.py — Multi-provider LLM service.

UPDATES:
  ✅ classify_intent: llama-3.1-8b-instant (fast) + non-streaming → ~200ms intent
  ✅ classify_intent: language detection fixed — english/hindi/hinglish returned properly
  ✅ Groq persistent session (one ClientSession per service lifetime)
  ✅ MAX_TOKENS = 120 for complete responses
  ✅ Provider chain: Groq → Gemini → Mistral → hardcoded fallback
"""

import json
import asyncio
from typing import AsyncGenerator, List, Dict, Optional

import aiohttp
from dotenv import load_dotenv
from loguru import logger

from config import settings

load_dotenv()

GROQ_BASE_URL    = settings.groq_base_url.rstrip("/")
GROQ_API_KEYS    = [k.strip() for k in settings.groq_api_keys.split(",") if k.strip()]
GEMINI_API_KEYS  = [k.strip() for k in settings.gemini_api_key.split(",") if k.strip()]
MISTRAL_API_KEYS = [k.strip() for k in settings.mistral_api_key.split(",") if k.strip()]

GROQ_MODELS    = ["llama-3.1-8b-instant", "llama-3.3-70b-versatile"]  # Speed-first ordering
GEMINI_MODELS  = ["gemini-2.0-flash"]
MISTRAL_MODELS = ["mistral-small-latest"]

GROQ_INTENT_MODEL = "llama-3.1-8b-instant"   # Fast model for intent only

GEMINI_BASE_URL  = "https://generativelanguage.googleapis.com/v1beta/models"
MISTRAL_BASE_URL = "https://api.mistral.ai/v1"

MAX_HISTORY       = 2          # ⚡ Reduced from 4 → faster LLM context
MAX_MESSAGE_CHARS = 300        # ⚡ Reduced from 500 → smaller messages
MAX_RAG_CHARS     = 800        # ⚡ Reduced from 1200 → shorter RAG context
MAX_TOKENS        = 80         # ⚡ Reduced from 100 → shorter responses, faster generation

# ── Shared HTTP sessions (created once, reused for lifetime) ──────────────────
_groq_session:    Optional[aiohttp.ClientSession] = None
_mistral_session: Optional[aiohttp.ClientSession] = None


def _get_groq_session() -> aiohttp.ClientSession:
    global _groq_session
    if _groq_session is None or _groq_session.closed:
        connector = aiohttp.TCPConnector(
            limit=20,
            limit_per_host=10,
            keepalive_timeout=30,
            enable_cleanup_closed=True,
        )
        _groq_session = aiohttp.ClientSession(connector=connector)
    return _groq_session


def _get_mistral_session() -> aiohttp.ClientSession:
    global _mistral_session
    if _mistral_session is None or _mistral_session.closed:
        connector = aiohttp.TCPConnector(
            limit=10,
            keepalive_timeout=30,
            enable_cleanup_closed=True,
        )
        _mistral_session = aiohttp.ClientSession(connector=connector)
    return _mistral_session


class QwenService:

    def __init__(self):
        self._groq_idx         = 0
        self._groq_intent_idx  = 0   # Separate rotation for intent calls
        self._gemini_idx       = 0
        self._mistral_idx      = 0

    def _next_key(self, keys: list, attr: str) -> Optional[str]:
        if not keys:
            return None
        idx = getattr(self, attr)
        key = keys[idx % len(keys)]
        setattr(self, attr, (idx + 1) % len(keys))
        return key

    def _build_system_prompt(
        self,
        hotel_name: str,
        hotel_id: str,
        custom_prompt: str,
        rag_context: str,
        manager_contact: str,
        guest_room: str = "",
    ) -> str:
        # ⚡ ULTRA-CONCISE for latency optimization
        base = custom_prompt or (
            f"Hotel concierge for {hotel_name}. Keep replies <20 words. "
            "Phone call. No AI mention. No invention. Use knowledge base only."
        )
        base += "\nAlways follow the language instruction in the system prompt exactly."
        if guest_room:
            base += f" Guest room: {guest_room}."
        if rag_context and rag_context.strip():
            base += f"\n\nKNOWLEDGE: {rag_context[:MAX_RAG_CHARS]}"
        base += f"\n\nManager: {manager_contact}"
        return base

   
    async def classify_intent(
        self,
        user_text: str,
        hotel_id: str,
        hotel_name: str,
    ) -> Dict:
        """
        Fast intent classifier using llama-3.1-8b-instant (non-streaming).
        Target: < 300ms latency.
        Returns: {intent, language, room_number, items}

        FIXES v2:
          ✅ Tighter intent definitions — "ok/theek/chalo" = farewell/small_talk
          ✅ Better language detection examples
          ✅ small_talk intent added for greetings/chitchat
          ✅ Stricter room_number rule — ONLY extract if explicitly said
          ✅ items must be actual food/service names, not filler words
        """
        prompt = (
            f'Classify hotel guest voice message. Return ONLY valid JSON — no markdown, no extra text.\n\n'
            f'Guest said: "{user_text}"\n\n'
            f'Available intents (pick the MOST specific match):\n'
            f'  food_order      — guest is ordering food, drinks, or asking to place an order\n'
            f'  room_cleaning   — housekeeping, clean room, change sheets, towels\n'
            f'  spa_service     — spa, massage, wellness, beauty treatment\n'
            f'  essential_needs — toiletries, toothbrush, pillow, extra blanket, amenities\n'
            f'  inquiry         — questions about hotel facilities, menu prices, timings, availability\n'
            f'  event_inquiry   — events, party, banquet, wedding, conference booking\n'
            f'  escalation      — wants to speak to manager, complaint, urgent problem\n'
            f'  farewell        — goodbye, thanks, ok bye, theek hai, chalo, call ending\n'
            f'  small_talk      — hello, hi, namaste, how are you, general chitchat NOT about hotel services\n\n'
            f'IMPORTANT RULES FOR INTENT:\n'
            f'  - "theek hai", "ok", "chalo", "shukriya", "thanks" alone = farewell or small_talk\n'
            f'  - "hello", "hi", "namaste", "good morning" = small_talk\n'
            f'  - Only use food_order if guest actually names a food item OR says "order karna hai"\n'
            f'  - Only use inquiry if guest asks a question with "?", "kya", "kitna", "kab", "kahan"\n'
            f'  - General knowledge questions (weather, news, sports) = inquiry but note no hotel context\n\n'
            f'Language detection (what language did the guest USE):\n'
            f'  "english"  — mostly English with little or no Hindi words: "I want to order pizza please"\n'
            f'  "hindi"    — mostly Hindi in Roman script, with almost no English words: "mujhe khana chahiye", "kamra saaf karo"\n'
            f'  "hinglish" — clear mixture of Hindi and English in the same sentence: "mujhe coffee chahiye please", "room clean kar do yaar"\n'
            f'  - If the guest text is purely English, return english.\n'
            f'  - If the guest text is purely Roman Hindi, return hindi.\n'
            f'  - If the guest text mixes both, return hinglish.\n'
            f'  - Do not choose hinglish for a purely English sentence.\n\n'
            f'Return exactly this JSON:\n'
            f'{{"intent":"<intent>","language":"english|hindi|hinglish",'
            f'"room_number":<number_or_null>,"items":[<list_of_strings_or_empty>]}}\n\n'
            f'Rules for fields:\n'
            f'- room_number: extract ONLY if guest EXPLICITLY said their room number (e.g. "room 302", "mera room 101 hai")\n'
            f'  Do NOT extract if number is a phone number or quantity\n'
            f'- items: ONLY actual food dish names or service names mentioned by guest\n'
            f'  e.g. ["Biryani", "Coffee"] for food_order; ["massage"] for spa_service\n'
            f'  Empty list [] if no specific items mentioned\n'
            f'- Return null for room_number if not mentioned'
        )

        # ── Try Groq fast path (non-streaming, 8B model) ──────────────────────
        if GROQ_API_KEYS:
            try:
                session = _get_groq_session()
                key = self._next_key(GROQ_API_KEYS, "_groq_intent_idx")

                async with session.post(
                    f"{GROQ_BASE_URL}/chat/completions",
                    json={
                        "model":       GROQ_INTENT_MODEL,
                        "messages": [
                            {"role": "system", "content": "You are a JSON classifier. Return ONLY valid JSON. No markdown. No explanation. No preamble."},
                            {"role": "user",   "content": prompt},
                        ],
                        "temperature": 0.0,
                        "max_tokens":  150,
                        "stream":      False,
                    },
                    headers={
                        "Authorization": f"Bearer {key}",
                        "Content-Type":  "application/json",
                    },
                    timeout=aiohttp.ClientTimeout(total=4, connect=1),
                ) as resp:
                    if resp.status == 200:
                        data = await resp.json()
                        content = data["choices"][0]["message"]["content"].strip()

                        # Extract JSON using bracket depth matching
                        s = content.find("{")
                        if s != -1:
                            depth, e = 0, -1
                            for i, ch in enumerate(content[s:], s):
                                if ch == "{":
                                    depth += 1
                                elif ch == "}":
                                    depth -= 1
                                    if depth == 0:
                                        e = i
                                        break

                        if s != -1 and e != -1:
                            try:
                                parsed = json.loads(content[s:e + 1])
                                intent   = parsed.get("intent", "inquiry")
                                language = parsed.get("language", "hinglish")

                                # Validate intent against known values
                                VALID_INTENTS = {
                                    "food_order", "room_cleaning", "spa_service",
                                    "essential_needs", "inquiry", "event_inquiry",
                                    "escalation", "farewell", "small_talk",
                                }
                                if intent not in VALID_INTENTS:
                                    intent = "inquiry"

                                # Validate language
                                if language not in ("english", "hindi", "hinglish"):
                                    language = "hinglish"

                                result = {
                                    "intent":      intent,
                                    "language":    language,
                                    "room_number": parsed.get("room_number"),
                                    "items":       parsed.get("items", []),
                                }
                                logger.info(
                                    f"🎯 Intent: {result['intent']} | lang: {result['language']} "
                                    f"| room: {result['room_number']} | items: {result['items']}"
                                )
                                return result
                            except json.JSONDecodeError as je:
                                logger.warning(f"Intent JSON parse failed: {je} | content={content[:100]}")
                    elif resp.status == 429:
                        logger.warning("Intent: Groq 429 — trying fallback")
                    else:
                        body = await resp.text()
                        logger.warning(f"Intent Groq {resp.status}: {body[:100]}")

            except asyncio.TimeoutError:
                logger.warning("Intent: Groq timeout — using fallback")
            except Exception as ex:
                logger.warning(f"Intent fast-path error: {ex}")

        # ── Fallback: use full get_full_response (slower but reliable) ─────────
        try:
            response = await self.get_full_response(
                messages=[{"role": "user", "content": prompt}],
                hotel_id=hotel_id,
                hotel_name=hotel_name,
                system_prompt="You are a JSON classifier. Return only valid JSON. No markdown.",
            )
            cleaned = response.strip()
            s, e    = cleaned.find("{"), cleaned.rfind("}")
            if s != -1 and e != -1:
                cleaned = cleaned[s:e + 1]
            data = json.loads(cleaned)

            intent   = data.get("intent", "inquiry")
            language = data.get("language", "hinglish")
            VALID_INTENTS = {
                "food_order", "room_cleaning", "spa_service",
                "essential_needs", "inquiry", "event_inquiry",
                "escalation", "farewell", "small_talk",
            }
            if intent not in VALID_INTENTS:
                intent = "inquiry"
            if language not in ("english", "hindi", "hinglish"):
                language = "hinglish"

            return {
                "intent":      intent,
                "language":    language,
                "room_number": data.get("room_number"),
                "items":       data.get("items", []),
            }
        except Exception as ex:
            logger.warning(f"Intent fallback failed: {ex} | text={user_text[:60]}")

        return {
            "intent":      "inquiry",
            "language":    "hinglish",
            "room_number": None,
            "items":       [],
        }
    # ─────────────────────────────────────────────────────────────
    # MAIN RESPONSE STREAM
    # ─────────────────────────────────────────────────────────────

    async def stream_response(
        self,
        messages: List[Dict],
        hotel_id: str,
        hotel_name: str,
        system_prompt: str,
        rag_context: str = "",
        manager_contact: str = "",
        guest_room: str = "",
    ) -> AsyncGenerator[str, None]:

        full_system = self._build_system_prompt(
            hotel_name=hotel_name,
            hotel_id=hotel_id,
            custom_prompt=system_prompt,
            rag_context=rag_context,
            manager_contact=manager_contact,
            guest_room=guest_room,
        )
        trimmed = [
            {
                "role":    msg.get("role", "user"),
                "content": str(msg.get("content", ""))[:MAX_MESSAGE_CHARS],
            }
            for msg in messages[-MAX_HISTORY:]
        ]
        full_messages = [{"role": "system", "content": full_system}, *trimmed]

        # 1. Groq (shared persistent session)
        if GROQ_API_KEYS:
            try:
                session = _get_groq_session()
                for model in GROQ_MODELS:
                    gen = await self._try_groq(session, model, full_messages)
                    if gen == "ERROR":
                        continue
                    async for chunk in gen:
                        yield chunk
                    return
            except Exception as e:
                logger.error(f"Groq session error: {e}")

        # 2. Gemini
        if GEMINI_API_KEYS:
            for model in GEMINI_MODELS:
                gen = await self._try_gemini(model, full_system, trimmed)
                if gen == "ERROR":
                    continue
                async for chunk in gen:
                    yield chunk
                return

        # 3. Mistral (shared persistent session)
        if MISTRAL_API_KEYS:
            try:
                session = _get_mistral_session()
                for model in MISTRAL_MODELS:
                    gen = await self._try_mistral(session, model, full_messages)
                    if gen == "ERROR":
                        continue
                    async for chunk in gen:
                        yield chunk
                    return
            except Exception as e:
                logger.error(f"Mistral session error: {e}")

        logger.error(f"All LLM providers failed | hotel_id={hotel_id}")
        yield "Main abhi thoda busy hoon. Kripya manager se baat karein."

    # ─────────────────────────────────────────────────────────────
    # PROVIDER INTERNALS
    # ─────────────────────────────────────────────────────────────

    async def _try_groq(
        self,
        session: aiohttp.ClientSession,
        model: str,
        messages: list,
    ):
        for _ in range(max(1, len(GROQ_API_KEYS))):
            key = self._next_key(GROQ_API_KEYS, "_groq_idx")
            if not key:
                return "ERROR"
            headers = {
                "Authorization": f"Bearer {key}",
                "Content-Type":  "application/json",
            }
            payload = {
                "model":       model,
                "messages":    messages,
                "temperature": 0.4,
                "max_tokens":  MAX_TOKENS,
                "stream":      True,
            }
            try:
                resp = await session.post(
                    f"{GROQ_BASE_URL}/chat/completions",
                    json=payload,
                    headers=headers,
                    timeout=aiohttp.ClientTimeout(total=12, connect=3),
                )
                if resp.status == 429:
                    logger.warning(f"Groq 429 | model={model} | key rotated")
                    await asyncio.sleep(0.2)
                    continue
                if resp.status != 200:
                    body = await resp.text()
                    logger.error(f"Groq {resp.status}: {body[:200]}")
                    continue
                logger.info(f"✅ GROQ: {model}")
                return self._stream_openai(resp)
            except asyncio.TimeoutError:
                logger.warning(f"Groq timeout | model={model}")
            except Exception as e:
                logger.error(f"Groq error: {e}")
        return "ERROR"

    async def _try_gemini(self, model: str, system_prompt: str, messages: list):
        contents = []
        for msg in messages:
            role = "user" if msg["role"] == "user" else "model"
            contents.append({"role": role, "parts": [{"text": msg["content"]}]})
        payload = {
            "system_instruction": {"parts": [{"text": system_prompt}]},
            "contents": contents,
            "generationConfig": {
                "temperature":    0.4,
                "maxOutputTokens": MAX_TOKENS,
            },
        }
        for _ in range(max(1, len(GEMINI_API_KEYS))):
            key = self._next_key(GEMINI_API_KEYS, "_gemini_idx")
            if not key:
                return "ERROR"
            url = f"{GEMINI_BASE_URL}/{model}:streamGenerateContent?key={key}&alt=sse"
            try:
                async with aiohttp.ClientSession() as session:
                    resp = await session.post(
                        url,
                        json=payload,
                        timeout=aiohttp.ClientTimeout(total=12, connect=3),
                    )
                    if resp.status == 429:
                        await asyncio.sleep(0.2)
                        continue
                    if resp.status != 200:
                        body = await resp.text()
                        logger.error(f"Gemini {resp.status}: {body[:200]}")
                        continue
                    logger.info(f"✅ GEMINI: {model}")
                    return self._stream_gemini(resp)
            except asyncio.TimeoutError:
                logger.warning("Gemini timeout")
            except Exception as e:
                logger.error(f"Gemini error: {e}")
        return "ERROR"

    async def _try_mistral(
        self,
        session: aiohttp.ClientSession,
        model: str,
        messages: list,
    ):
        for _ in range(max(1, len(MISTRAL_API_KEYS))):
            key = self._next_key(MISTRAL_API_KEYS, "_mistral_idx")
            if not key:
                return "ERROR"
            headers = {
                "Authorization": f"Bearer {key}",
                "Content-Type":  "application/json",
            }
            payload = {
                "model":       model,
                "messages":    messages,
                "temperature": 0.4,
                "max_tokens":  MAX_TOKENS,
                "stream":      True,
            }
            try:
                resp = await session.post(
                    f"{MISTRAL_BASE_URL}/chat/completions",
                    json=payload,
                    headers=headers,
                    timeout=aiohttp.ClientTimeout(total=12, connect=3),
                )
                if resp.status == 429:
                    await asyncio.sleep(0.2)
                    continue
                if resp.status != 200:
                    body = await resp.text()
                    logger.error(f"Mistral {resp.status}: {body[:200]}")
                    continue
                logger.info(f"✅ MISTRAL: {model}")
                return self._stream_openai(resp)
            except asyncio.TimeoutError:
                logger.warning("Mistral timeout")
            except Exception as e:
                logger.error(f"Mistral error: {e}")
        return "ERROR"

    async def _stream_openai(self, resp) -> AsyncGenerator[str, None]:
        async for raw_line in resp.content:
            try:
                line = raw_line.decode("utf-8", errors="ignore").strip()
                if not line:
                    continue
                if line == "data: [DONE]":
                    break
                if line.startswith("data: "):
                    line = line[6:]
                chunk = json.loads(line)
                text  = (
                    chunk.get("choices", [{}])[0]
                    .get("delta", {})
                    .get("content", "")
                )
                if text:
                    yield text
            except asyncio.CancelledError:
                raise
            except Exception:
                continue

    async def _stream_gemini(self, resp) -> AsyncGenerator[str, None]:
        async for raw_line in resp.content:
            try:
                line = raw_line.decode("utf-8", errors="ignore").strip()
                if not line or not line.startswith("data: "):
                    continue
                line = line[6:]
                if line == "[DONE]":
                    break
                chunk = json.loads(line)
                text  = (
                    chunk.get("candidates", [{}])[0]
                    .get("content", {})
                    .get("parts", [{}])[0]
                    .get("text", "")
                )
                if text:
                    yield text
            except asyncio.CancelledError:
                raise
            except Exception:
                continue

    async def get_full_response(
        self,
        messages: List[Dict],
        hotel_id: str,
        hotel_name: str,
        system_prompt: str,
        rag_context: str = "",
        manager_contact: str = "",
        guest_room: str = "",
    ) -> str:
        parts = []
        async for chunk in self.stream_response(
            messages=messages,
            hotel_id=hotel_id,
            hotel_name=hotel_name,
            system_prompt=system_prompt,
            rag_context=rag_context,
            manager_contact=manager_contact,
            guest_room=guest_room,
        ):
            parts.append(chunk)
        return "".join(parts).strip()


qwen_service = QwenService()





