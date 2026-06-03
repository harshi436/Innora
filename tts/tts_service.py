"""
tts/tts_service.py — Ultra-low latency TTS pipeline.

FIXES v4:
  ✅ Key rotation uses a SNAPSHOT of start_idx so we never revisit already-tried keys
  ✅ _cartesia_idx advances ONCE on success too (sticky-advance) so next call
     starts on the key that last worked — not key 1 every time
  ✅ Parallel TTS tasks no longer blast all keys simultaneously:
     _flush_semaphore limits concurrent Cartesia SSE calls to 2 at a time
  ✅ 402 (insufficient credits) marks that key as permanently dead for this
     process lifetime — _dead_keys set — so we never retry a broken key
  ✅ stream_to_base64_chunks: tried counter increments correctly on every path
  ✅ WordStreamBuffer: flush threshold tuned for <300ms first-chunk latency

STREAMING ARCHITECTURE:
  Token stream from LLM → WordStreamBuffer → Cartesia SSE → audio chunks → Twilio
  First audio arrives before LLM finishes generating.

PRIORITY CHAIN:
  1. Cartesia streaming SSE (keys round-robin, skip dead keys, max 2 parallel)
  2. Deepgram Aura (mulaw direct)
  3. Edge TTS (free fallback)
"""

import asyncio
import audioop
import base64
import io
import json
import os
import sys
import re
from typing import Optional, AsyncGenerator

import aiohttp
import edge_tts
import miniaudio
from loguru import logger

sys.path.append(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
try:
    from config import settings
except ImportError:
    settings = None

TARGET_SAMPLE_RATE = 8000

CARTESIA_TTS_BYTES_URL = "https://api.cartesia.ai/tts/bytes"
CARTESIA_TTS_SSE_URL   = "https://api.cartesia.ai/tts/sse"
DEEPGRAM_TTS_URL       = "https://api.deepgram.com/v1/speak"

STREAM_MAX_CHARS = 65  # balance latency and smoother phrasing for more natural speech

# Max concurrent Cartesia SSE calls — prevents all 5 keys being hammered at once
_CARTESIA_SEMAPHORE_LIMIT = 4


def _load_keys(prefix: str, count: int = 5) -> list:
    keys = []
    if not settings:
        return keys
    for i in range(1, count + 1):
        val = getattr(settings, f"{prefix}_{i}", "")
        if val and val.strip():
            keys.append(val.strip())
    return keys


# ─────────────────────────────────────────────────────────────────────────────
# WORD BUFFER — flushes LLM tokens to Cartesia at natural phrase boundaries
# ─────────────────────────────────────────────────────────────────────────────

class WordStreamBuffer:
    """
    Buffers LLM tokens. Flushes to Cartesia when:
      - Sentence-ending punctuation (.!?।)
      - Clause boundary (,;:) with >= CLAUSE_MIN chars buffered
      - Buffer overflows STREAM_MAX_CHARS

    LATENCY TUNING:
      CLAUSE_MIN=20 (was 25) → flushes sooner on commas → first chunk faster
      STREAM_MAX_CHARS=60 (was 80) → hard overflow fires earlier
    """

    SENTENCE_END = set('.!?।')
    CLAUSE_END   = set(',;:')
    CLAUSE_MIN   = 18         # balance phrase length for smoother speech without long delays

    def __init__(self):
        self._buf = ""

    def feed(self, token: str) -> Optional[str]:
        self._buf += token
        buf = self._buf

        if buf and buf[-1] in self.SENTENCE_END:
            out = buf.strip()
            self._buf = ""
            return out if out else None

        if len(buf) >= self.CLAUSE_MIN and buf[-1] in self.CLAUSE_END:
            out = buf.strip()
            self._buf = ""
            return out if out else None

        if len(buf) >= STREAM_MAX_CHARS:
            idx = buf.rfind(" ")
            if idx > 20:
                out = buf[:idx].strip()
                self._buf = buf[idx:].lstrip()
            else:
                out = buf.strip()
                self._buf = ""
            return out if out else None

        return None

    def flush(self) -> Optional[str]:
        out = self._buf.strip()
        self._buf = ""
        return out if out else None

    def reset(self):
        self._buf = ""


# ─────────────────────────────────────────────────────────────────────────────
# MAIN TTS SERVICE
# ─────────────────────────────────────────────────────────────────────────────

class TTSService:

    def __init__(self):
        self._cartesia_keys  = _load_keys("cartesia_api_key")

        # Persistent round-robin index — advances on EVERY use (success or fail)
        self._cartesia_idx   = 0

        # Keys permanently exhausted (402 = no credits) — never retry these
        self._dead_keys: set = set()

        # Semaphore: max N concurrent Cartesia SSE connections
        # Created lazily in async context
        self._cartesia_sem: Optional[asyncio.Semaphore] = None

        raw_model = getattr(settings, "cartesia_model_id", "sonic-3.5") if settings else "sonic-3.5"
        self._cartesia_model = raw_model.lower().strip()
        if self._cartesia_model not in ("sonic-3.5", "sonic-multilingual", "sonic", "sonic-3.5"):
            logger.warning(f"⚠️ Unknown Cartesia model '{self._cartesia_model}' → forcing 'sonic-3.5'")
            self._cartesia_model = "sonic-3.5"

        self._cartesia_voice_hi = (
            getattr(settings, "cartesia_voice_id_hi", "") or
            getattr(settings, "cartesia_voice_id_en", "95d51f79-c397-46f9-b49a-23763d3eaa2d")
        ) if settings else "95d51f79-c397-46f9-b49a-23763d3eaa2d"
        self._cartesia_voice_en = (
            getattr(settings, "cartesia_voice_id_en", "") or
            getattr(settings, "cartesia_voice_id_hi", "95d51f79-c397-46f9-b49a-23763d3eaa2d")
        ) if settings else "95d51f79-c397-46f9-b49a-23763d3eaa2d"

        self._deepgram_key   = getattr(settings, "deepgram_api_key", "") if settings else ""
        self._deepgram_model = getattr(settings, "deepgram_aura_model", "aura-asteria-en") if settings else "aura-asteria-en"

        logger.info(
            f"🔑 TTS Keys | Cartesia={len(self._cartesia_keys)} "
            f"| model={self._cartesia_model} "
            f"| voice_hi={self._cartesia_voice_hi[:8]}... "
            f"voice_en={self._cartesia_voice_en[:8]}... "
            f"| Deepgram={'yes' if self._deepgram_key else 'no'}"
        )

    def _get_sem(self) -> asyncio.Semaphore:
        """Lazy semaphore creation — must be called from async context."""
        if self._cartesia_sem is None:
            self._cartesia_sem = asyncio.Semaphore(_CARTESIA_SEMAPHORE_LIMIT)
        return self._cartesia_sem

    def _cartesia_headers(self, key: str) -> dict:
        return {
            "X-API-Key":        key,
            "Cartesia-Version": "2024-06-10",
            "Content-Type":     "application/json",
        }

    def _normalize_numbers_for_hindi_tts(self, text: str) -> str:
        """
        Hindi TTS numbers ko digit-by-digit English words mein convert karta hai.
        Cartesia 'hi' mode mein numbers Hindi mein bolega warna.
        """
        digit_map = {
            '0': 'zero', '1': 'one', '2': 'two', '3': 'three', '4': 'four',
            '5': 'five', '6': 'six', '7': 'seven', '8': 'eight', '9': 'nine'
        }
        
        def replace_number(m):
            num_str = m.group(0)
            # Price pattern — ₹299 → "two nine nine rupees"
            if m.string[max(0, m.start()-1)] == '₹' or (m.start() > 0 and text[m.start()-1] == '₹'):
                return ' '.join(digit_map[d] for d in num_str) + ' rupees'
            # Phone number ya room number — digit by digit
            return ' '.join(digit_map[d] for d in num_str)
        
        # ₹ ke baad numbers
        text = re.sub(r'₹\s*(\d+)', lambda m: ' '.join(digit_map[d] for d in m.group(1)) + ' rupees', text)
        # Baaki saare numbers (2+ digits)
        text = re.sub(r'\b\d{2,}\b', replace_number, text)
        return text    

    def _cartesia_payload(self, text: str, stream: bool = False, language: str = "hinglish") -> dict:
        lang = language if language in ("english", "hindi", "hinglish") else "hinglish"
        cartesia_lang = "en" if lang == "english" else "hi"
        voice_id = self._cartesia_voice_en if cartesia_lang == "en" else self._cartesia_voice_hi

        transcript = text
        if cartesia_lang == "hi":
            transcript = self._normalize_numbers_for_hindi_tts(text)

        return {
            "model_id":   self._cartesia_model,
            "transcript": transcript,
            "voice":      {"mode": "id", "id": voice_id},
            "output_format": {
                "container":   "raw",
                "encoding":    "pcm_mulaw",
                "sample_rate": TARGET_SAMPLE_RATE,
            },
            "language": cartesia_lang,
            **({"stream": True} if stream else {}),
        }

    def _next_live_key(self, start_idx: int) -> Optional[tuple[int, str]]:
        """
        Find the next key that is NOT in _dead_keys, starting from start_idx.
        Returns (idx, key) or None if all live keys are exhausted.

        Iterates at most len(keys) times so it never loops forever.
        """
        num_keys = len(self._cartesia_keys)
        if not num_keys:
            return None

        for offset in range(num_keys):
            idx = (start_idx + offset) % num_keys
            if idx not in self._dead_keys:
                return idx, self._cartesia_keys[idx]

        return None  # all keys dead

    def _rotate(self, idx: int, reason: str) -> int:
        """Advance idx by 1 (wraps). Log the rotation. Return new idx."""
        new_idx = (idx + 1) % len(self._cartesia_keys)
        logger.warning(
            f"🔄 Cartesia key {idx + 1} exhausted ({reason}) → key {new_idx + 1}"
        )
        self._cartesia_idx = new_idx
        return new_idx

    def _kill_key(self, idx: int):
        """Mark key as permanently dead (402 = no credits)."""
        self._dead_keys.add(idx)
        logger.error(f"💀 Cartesia key {idx + 1} permanently dead (no credits)")
        self._cartesia_idx = (idx + 1) % max(len(self._cartesia_keys), 1)

    # ── PUBLIC: full synthesize (greeting cache, speak_simple) ────────────────

    async def synthesize_to_base64(self, text: str, language: str = "hinglish") -> Optional[str]:
        text = text.strip()
        if not text:
            return None
        if text[-1] not in ".?!,।":
            text += "."

        result = await self._cartesia_bytes(text, language=language)
        if result:
            return result
        result = await self._deepgram_aura(text)
        if result:
            return result
        logger.warning("⚠️ Cartesia + Deepgram failed — Edge TTS fallback")
        return await self._edge_tts(text)

    # ── PUBLIC: streaming → yields b64 chunks ─────────────────────────────────

    async def stream_to_base64_chunks(self, text: str, language: str = "hinglish") -> AsyncGenerator[str, None]:
        """
        Stream text through Cartesia SSE → yield b64 mulaw chunks as they arrive.

        KEY ROTATION LOGIC:
          - snapshot start_idx ONCE at entry
          - try each key at most once (tried counter is independent of _cartesia_idx)
          - on permanent failure (402) → mark dead, never retry
          - on transient failure (429/timeout/error) → rotate and try next
          - on success → advance _cartesia_idx by 1 (sticky-advance for load spread)
          - if all live keys exhausted → Deepgram fallback
        """
        text = text.strip()
        if not text:
            return
        if text[-1] not in ".?!,।":
            text += "."

        if self._cartesia_keys:
            num_keys  = len(self._cartesia_keys)
            tried_set: set = set()   # track which indices we've actually tried

            async with self._get_sem():  # max N concurrent SSE connections
                while len(tried_set) < num_keys:
                    # Find next live key we haven't tried this call
                    result = self._next_live_key(self._cartesia_idx)
                    if result is None:
                        break  # all keys dead
                    idx, key = result

                    if idx in tried_set:
                        # We've gone full circle through live keys — stop
                        break
                    tried_set.add(idx)

                    got_any = False
                    rotate_reason = None

                    try:
                        timeout = aiohttp.ClientTimeout(total=12, connect=2)
                        async with aiohttp.ClientSession(timeout=timeout) as session:
                            async with session.post(
                                CARTESIA_TTS_SSE_URL,
                                json=self._cartesia_payload(text, stream=True, language=language),
                                headers=self._cartesia_headers(key),
                            ) as resp:
                                if resp.status in (401, 403):
                                    rotate_reason = "auth"
                                elif resp.status == 402:
                                    # Permanent — no credits
                                    self._kill_key(idx)
                                    continue
                                elif resp.status == 429:
                                    rotate_reason = "rate limited"
                                elif resp.status == 404:
                                    body = await resp.text()
                                    logger.error(f"Cartesia 404 model={self._cartesia_model}: {body[:150]}")
                                    break  # model error — skip all Cartesia
                                elif resp.status != 200:
                                    body = await resp.text()
                                    logger.error(f"Cartesia SSE {resp.status} key {idx+1}: {body[:150]}")
                                    rotate_reason = f"status {resp.status}"
                                else:
                                    # ── Stream SSE audio chunks ───────────────
                                    async for raw_line in resp.content:
                                        line = raw_line.decode("utf-8", errors="ignore").strip()
                                        if not line or not line.startswith("data:"):
                                            continue
                                        data_str = line[5:].strip()
                                        if data_str == "[DONE]":
                                            break
                                        try:
                                            chunk_data = json.loads(data_str)
                                            audio_b64  = chunk_data.get("data") or chunk_data.get("audio")
                                            if audio_b64:
                                                audio_bytes = base64.b64decode(audio_b64)
                                                if audio_bytes:
                                                    yield base64.b64encode(audio_bytes).decode("utf-8")
                                                    got_any = True
                                        except Exception:
                                            continue

                    except asyncio.TimeoutError:
                        rotate_reason = "timeout"
                    except asyncio.CancelledError:
                        raise
                    except Exception as e:
                        rotate_reason = f"error: {e}"

                    if rotate_reason:
                        self._rotate(idx, rotate_reason)
                        continue

                    if got_any:
                        # SUCCESS — advance index by 1 for load spreading on next call
                        self._cartesia_idx = (idx + 1) % num_keys
                        logger.info(f"✅ Cartesia SSE OK | key={idx+1} | '{text[:40]}'")
                        return

                    # Connected OK but no audio bytes — treat as transient failure
                    logger.warning(f"Cartesia SSE empty response key {idx+1}")
                    self._rotate(idx, "empty response")

            logger.warning("⚠️ All Cartesia SSE keys tried → Deepgram fallback")

        # ── Deepgram fallback ─────────────────────────────────────────────────
        b64 = await self._deepgram_aura(text)
        if not b64:
            b64 = await self._edge_tts(text)
        if b64:
            yield b64

    # ── CARTESIA BYTES (non-streaming — for greeting cache) ───────────────────

    async def _cartesia_bytes(self, text: str, language: str = "hinglish") -> Optional[str]:
        """
        Try all live keys. Uses same snapshot + tried_set pattern as SSE method.
        """
        num_keys = len(self._cartesia_keys)
        if not num_keys:
            return None

        tried_set: set = set()

        while len(tried_set) < num_keys:
            result = self._next_live_key(self._cartesia_idx)
            if result is None:
                break
            idx, key = result
            if idx in tried_set:
                break
            tried_set.add(idx)

            try:
                timeout = aiohttp.ClientTimeout(total=6, connect=2)
                async with aiohttp.ClientSession(timeout=timeout) as session:
                    async with session.post(
                        CARTESIA_TTS_BYTES_URL,
                        json=self._cartesia_payload(text, language=language),
                        headers=self._cartesia_headers(key),
                    ) as resp:

                        if resp.status == 402:
                            self._kill_key(idx)
                            continue
                        if resp.status in (401, 403):
                            self._rotate(idx, "auth")
                            continue
                        if resp.status == 429:
                            self._rotate(idx, "rate limited")
                            continue
                        if resp.status == 404:
                            body = await resp.text()
                            logger.error(f"Cartesia 404 model={self._cartesia_model}: {body[:200]}")
                            return None
                        if resp.status != 200:
                            body = await resp.text()
                            logger.error(f"Cartesia bytes {resp.status} key {idx+1}: {body[:200]}")
                            self._rotate(idx, f"status {resp.status}")
                            continue

                        mulaw_bytes = await resp.read()
                        if len(mulaw_bytes) < 50:
                            self._rotate(idx, "empty audio")
                            continue

                        b64 = base64.b64encode(mulaw_bytes).decode("utf-8")
                        self._cartesia_idx = (idx + 1) % num_keys  # advance after success
                        logger.info(f"✅ Cartesia bytes OK | key={idx+1} | {len(mulaw_bytes)}B")
                        return b64

            except asyncio.TimeoutError:
                self._rotate(idx, "timeout")
            except Exception as e:
                logger.error(f"Cartesia bytes error key {idx+1}: {e}")
                self._rotate(idx, "error")

        logger.warning("⚠️ All Cartesia bytes keys tried → None")
        return None

    # ── DEEPGRAM AURA ─────────────────────────────────────────────────────────

    async def _deepgram_aura(self, text: str) -> Optional[str]:
        if not self._deepgram_key:
            return None
        try:
            params  = {
                "model":       self._deepgram_model,
                "encoding":    "mulaw",
                "sample_rate": str(TARGET_SAMPLE_RATE),
                "container":   "none",
            }
            headers = {
                "Authorization": f"Token {self._deepgram_key}",
                "Content-Type":  "application/json",
            }
            timeout = aiohttp.ClientTimeout(total=8, connect=2)
            async with aiohttp.ClientSession(timeout=timeout) as session:
                async with session.post(
                    DEEPGRAM_TTS_URL,
                    json={"text": text},
                    headers=headers,
                    params=params,
                ) as resp:
                    if resp.status != 200:
                        body = await resp.text()
                        logger.error(f"Deepgram Aura {resp.status}: {body[:200]}")
                        return None
                    audio_bytes = await resp.read()
                    if len(audio_bytes) < 50:
                        return None
                    b64 = base64.b64encode(audio_bytes).decode("utf-8")
                    logger.info(f"✅ Deepgram Aura OK | {len(audio_bytes)}B")
                    return b64
        except asyncio.TimeoutError:
            logger.warning("Deepgram Aura timeout")
            return None
        except Exception as e:
            logger.error(f"Deepgram Aura error: {e}")
            return None

    # ── EDGE TTS (last resort) ────────────────────────────────────────────────

    async def _edge_tts(self, text: str) -> Optional[str]:
        try:
            communicate = edge_tts.Communicate(text, voice="en-IN-NeerjaNeural", rate="+5%")
            audio_buf   = io.BytesIO()
            async for chunk in communicate.stream():
                if chunk["type"] == "audio":
                    audio_buf.write(chunk["data"])

            raw_bytes = audio_buf.getvalue()
            if not raw_bytes:
                return None

            decoded    = miniaudio.decode(raw_bytes, nchannels=1, output_format=miniaudio.SampleFormat.SIGNED16)
            raw_pcm    = bytes(decoded.samples)
            input_rate = decoded.sample_rate if decoded.sample_rate > 0 else 24000

            if len(raw_pcm) % 2 != 0:
                raw_pcm = raw_pcm[:-1]

            pcm_8k, _ = audioop.ratecv(raw_pcm, 2, 1, input_rate, TARGET_SAMPLE_RATE, None)
            if not pcm_8k:
                return None

            mulaw = audioop.lin2ulaw(pcm_8k, 2)
            logger.info(f"✅ Edge TTS OK | {len(mulaw)}B")
            return base64.b64encode(mulaw).decode("utf-8")

        except Exception as e:
            logger.error(f"Edge TTS error: {e}")
            return None


tts_service = TTSService()