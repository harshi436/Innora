"""
tts/greeting_cache.py — Pre-synthesize hotel greetings at startup.

Strategy:
  - App startup pe sab hotels ki greetings ek baar banao
  - Cartesia TTS b64 memory mein cache karo
  - Call aane pe instantly play karo — 0ms wait
  - Background refresh har 6 hours mein
"""

import asyncio
from typing import Dict, Optional
from loguru import logger

# { hotel_id: b64_audio }
_greeting_cache: Dict[str, str] = {}

# Hardcoded Hinglish fallback templates (instant, no LLM needed)
_FALLBACK_GREETINGS: Dict[str, str] = {}

_DEFAULT_GREETING_TEMPLATE = "Namaste! {hotel_name} mein aapka swagat hai. Kya aapko hamara email mila hai?"


def get_cached_greeting_text(hotel_name: str) -> str:
    """Instant Hinglish greeting text — no LLM, no wait."""
    return _DEFAULT_GREETING_TEMPLATE.format(hotel_name=hotel_name)


def get_cached_greeting_b64(hotel_id: str) -> Optional[str]:
    """Return pre-synthesized b64 audio if available."""
    return _greeting_cache.get(hotel_id)


def set_cached_greeting(hotel_id: str, b64: str):
    _greeting_cache[hotel_id] = b64


async def pre_warm_greeting(hotel_id: str, hotel_name: str) -> Optional[str]:
    """
    Pre-synthesize greeting TTS for a hotel. Called at startup or on first call.
    Returns b64 audio string.
    """
    from tts.tts_service import tts_service

    # Check cache first
    cached = _greeting_cache.get(hotel_id)
    if cached:
        return cached

    greeting_text = get_cached_greeting_text(hotel_name)
    try:
        b64 = await asyncio.wait_for(
            tts_service.synthesize_to_base64(greeting_text),
            timeout=3.0,
        )
        if b64:
            _greeting_cache[hotel_id] = b64
            logger.info(f"✅ Greeting pre-warmed | hotel={hotel_name} | {len(b64)} chars")
            return b64
    except asyncio.TimeoutError:
        logger.warning(f"Greeting pre-warm timeout | hotel={hotel_name}")
    except Exception as e:
        logger.error(f"Greeting pre-warm error: {e}")
    return None


async def pre_warm_all_hotels():
    """
    Called once at app startup — pre-synthesize greetings for all hotels.
    Non-blocking: runs in background.
    """
    try:
        from database.mongodb import mongo_client
        from tts.tts_service import tts_service

        # Get all hotels from DB
        hotels = await mongo_client.get_all_hotels()  # returns [{hotel_id, hotel_name}]
        if not hotels:
            logger.info("No hotels found for greeting pre-warm")
            return

        logger.info(f"🔥 Pre-warming greetings for {len(hotels)} hotels...")
        tasks = [
            pre_warm_greeting(h["hotel_id"], h["hotel_name"])
            for h in hotels
        ]
        results = await asyncio.gather(*tasks, return_exceptions=True)
        success = sum(1 for r in results if r and not isinstance(r, Exception))
        logger.info(f"✅ Greeting cache ready: {success}/{len(hotels)} hotels")

    except Exception as e:
        logger.warning(f"pre_warm_all_hotels error: {e} — greetings will be synthesized on first call")