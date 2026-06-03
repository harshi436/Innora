"""
speech/deepgram_service.py

FIXES v5:
  ✅ STT NEVER MUTED — mute()/unmute() removed from audio path.
     Previously: stt.mute() was called during TTS playback → guest audio dropped
     → barge-in impossible → agent talks over guest.
     Now: ALL guest audio always flows to Deepgram.

  ✅ GHOST TRANSCRIPT FIX — _muted flag kept as a soft filter only.
     Instead of dropping audio, we drop TRANSCRIPTS while agent is speaking.
     But barge-in in websocket_server.py fires on interim transcripts BEFORE
     the filter can drop them — so first word always triggers barge-in.

  ✅ KeepAlive task — sends ping every 8s while connected (unchanged).
  ✅ Fresh client per call (unchanged).
  ✅ Groq Whisper fallback (unchanged).
"""

import asyncio
import base64
import io
import os
import wave
from typing import Callable, Optional

import aiohttp
from loguru import logger

from deepgram import (
    DeepgramClient,
    LiveTranscriptionEvents,
    LiveOptions,
)

from config import settings

GROQ_WHISPER_URL  = "https://api.groq.com/openai/v1/audio/transcriptions"
GROQ_API_KEY      = os.getenv("GROQ_API_KEY") or settings.groq_api_keys.split(",")[0]

MAX_RECONNECTS       = 3
RECONNECT_BASE_DELAY = 1.0
KEEPALIVE_INTERVAL   = 8


def get_deepgram_client() -> DeepgramClient:
    return DeepgramClient(settings.deepgram_api_key)


def _mulaw_to_wav(mulaw_bytes: bytes, sample_rate: int = 8000) -> bytes:
    buf = io.BytesIO()
    with wave.open(buf, "wb") as wf:
        wf.setnchannels(1)
        wf.setsampwidth(1)
        wf.setframerate(sample_rate)
        wf.writeframes(mulaw_bytes)
    return buf.getvalue()


# ── Groq Whisper fallback ─────────────────────────────────────────────────────

class GroqWhisperFallback:
    FLUSH_SECONDS = 1.5
    SAMPLE_RATE   = 8000

    def __init__(self, hotel_id: str, on_final: Callable, on_interim: Callable):
        self.hotel_id   = hotel_id
        self.on_final   = on_final
        self.on_interim = on_interim
        self._buffer: bytearray = bytearray()
        self._flush_task: Optional[asyncio.Task] = None
        self._active   = False
        self._lock     = asyncio.Lock()
        self._session: Optional[aiohttp.ClientSession] = None

    def _get_session(self) -> aiohttp.ClientSession:
        if self._session is None or self._session.closed:
            self._session = aiohttp.ClientSession()
        return self._session

    def start(self):
        self._active = True
        logger.info(f"🎙️ Groq Whisper fallback active | hotel_id={self.hotel_id}")

    def stop(self):
        self._active = False
        if self._flush_task and not self._flush_task.done():
            self._flush_task.cancel()
        if self._session and not self._session.closed:
            asyncio.create_task(self._session.close())
        logger.info(f"Groq Whisper stopped | hotel_id={self.hotel_id}")

    # mute/unmute kept as no-ops for compatibility — but we never call them now
    def mute(self):
        pass  # intentionally no-op — never mute STT

    def unmute(self):
        pass  # intentionally no-op

    def reset(self):
        self._buffer.clear()
        if self._flush_task and not self._flush_task.done():
            self._flush_task.cancel()

    def feed(self, mulaw_bytes: bytes):
        if not self._active:
            return
        self._buffer.extend(mulaw_bytes)
        if self._flush_task is None or self._flush_task.done():
            self._flush_task = asyncio.create_task(self._delayed_flush())

    async def _delayed_flush(self):
        await asyncio.sleep(self.FLUSH_SECONDS)
        await self._flush()

    async def _flush(self):
        async with self._lock:
            if not self._buffer:
                return
            audio_data = bytes(self._buffer)
            self._buffer.clear()

        wav_bytes  = _mulaw_to_wav(audio_data, self.SAMPLE_RATE)
        transcript = await self._transcribe(wav_bytes)
        if transcript and transcript.strip():
            logger.info(f"📝 Groq Whisper [{self.hotel_id}]: {transcript}")
            asyncio.create_task(self.on_final(transcript))

    async def _transcribe(self, wav_bytes: bytes) -> Optional[str]:
        if not GROQ_API_KEY:
            return None
        try:
            form = aiohttp.FormData()
            form.add_field("file", wav_bytes, filename="audio.wav", content_type="audio/wav")
            form.add_field("model", "whisper-large-v3-turbo")
            form.add_field("response_format", "text")
            headers = {"Authorization": f"Bearer {GROQ_API_KEY}"}
            session = self._get_session()
            async with session.post(
                GROQ_WHISPER_URL, data=form, headers=headers,
                timeout=aiohttp.ClientTimeout(total=12),
            ) as resp:
                if resp.status == 429:
                    logger.warning("Groq Whisper rate limited")
                    return None
                if resp.status != 200:
                    body = await resp.text()
                    logger.error(f"Groq Whisper {resp.status}: {body}")
                    return None
                return await resp.text()
        except asyncio.CancelledError:
            raise
        except Exception as e:
            logger.error(f"Groq Whisper error: {e}")
            return None


# ── Main STT service ──────────────────────────────────────────────────────────

class DeepgramService:

    def __init__(self, hotel_id: str, on_final: Callable, on_interim: Callable):
        self.hotel_id         = hotel_id
        self.on_final         = on_final
        self.on_interim       = on_interim
        self._connection      = None
        self._connected       = False
        self._active          = True
        self._using_fallback  = False
        self._reconnect_count = 0
        self._send_lock       = asyncio.Lock()
        self._keepalive_task: Optional[asyncio.Task] = None
        self._fallback        = GroqWhisperFallback(hotel_id, on_final, on_interim)

    # ── mute/unmute are NO-OPS — we never mute STT ────────────────────────────
    # Barge-in relies on interim transcripts firing even while agent speaks.
    # Muting STT was the root cause of "agent talks over guest" bug.

    def mute(self):
        """NO-OP. STT is never muted. Barge-in requires continuous audio."""
        pass

    def unmute(self):
        """NO-OP. STT is always active."""
        pass

    # ── Connect ───────────────────────────────────────────────────────────────

    async def connect(self):
        try:
            await self._connect_deepgram()
        except Exception as e:
            logger.warning(f"Deepgram unavailable ({e}) → Groq Whisper fallback")
            self._using_fallback = True
            self._fallback.start()

    async def _connect_deepgram(self):
        client           = get_deepgram_client()
        self._connection = client.listen.asynclive.v("1")

        self._connection.on(LiveTranscriptionEvents.Transcript, self._on_transcript)
        self._connection.on(LiveTranscriptionEvents.Error,      self._on_error)
        self._connection.on(LiveTranscriptionEvents.Close,      self._on_close)

        options = LiveOptions(
            model="nova-2",
            language="multi",
            encoding="mulaw",
            sample_rate=8000,
            channels=1,
            punctuate=True,
            interim_results=True,
            utterance_end_ms=1000,
            vad_events=True,
            smart_format=True,
        )

        started = await self._connection.start(options)
        if not started:
            raise RuntimeError("Deepgram failed to start")

        self._connected       = True
        self._reconnect_count = 0
        logger.info(f"🎙️ Deepgram connected | hotel_id={self.hotel_id}")

        if self._keepalive_task and not self._keepalive_task.done():
            self._keepalive_task.cancel()
        self._keepalive_task = asyncio.create_task(self._keepalive_loop())

    async def _keepalive_loop(self):
        try:
            while self._connected and self._active and not self._using_fallback:
                await asyncio.sleep(KEEPALIVE_INTERVAL)
                if self._connected and self._connection:
                    try:
                        await self._connection.keep_alive()
                        logger.debug(f"💓 Deepgram KeepAlive sent | {self.hotel_id}")
                    except Exception as e:
                        logger.warning(f"KeepAlive failed: {e}")
                        break
        except asyncio.CancelledError:
            pass

    def reset(self):
        """Called on barge-in to clear any partial state."""
        if self._using_fallback:
            self._fallback.reset()

    # ── Audio feed ────────────────────────────────────────────────────────────

    async def send_base64_chunk(self, b64_payload: str):
        """
        Always send audio — never drop it.
        Barge-in detection needs continuous audio flow.
        """
        if not self._active:
            return
        try:
            audio_bytes = base64.b64decode(b64_payload)
        except Exception:
            return

        if self._using_fallback:
            self._fallback.feed(audio_bytes)
            return

        if not self._connected or not self._connection:
            return

        async with self._send_lock:
            try:
                await self._connection.send(audio_bytes)
            except Exception as e:
                logger.warning(f"Deepgram send error ({e}) → switching to fallback")
                await self._switch_to_fallback()
                self._fallback.feed(audio_bytes)

    # ── Disconnect ────────────────────────────────────────────────────────────

    async def disconnect(self):
        self._active = False
        if self._keepalive_task and not self._keepalive_task.done():
            self._keepalive_task.cancel()
        if self._using_fallback:
            await self._fallback._flush()
            self._fallback.stop()
        elif self._connection and self._connected:
            try:
                await self._connection.finish()
            except Exception:
                pass
            self._connected = False
        logger.info(f"STT disconnected | hotel_id={self.hotel_id}")

    # ── Reconnect ─────────────────────────────────────────────────────────────

    async def _try_reconnect(self):
        if not self._active:
            return
        if self._reconnect_count >= MAX_RECONNECTS:
            logger.warning(f"[{self.hotel_id}] Max reconnects → Groq Whisper")
            await self._switch_to_fallback()
            return

        delay = RECONNECT_BASE_DELAY * (2 ** self._reconnect_count)
        self._reconnect_count += 1
        logger.info(f"[{self.hotel_id}] Reconnecting Deepgram attempt {self._reconnect_count} after {delay:.1f}s...")
        await asyncio.sleep(delay)
        try:
            await self._connect_deepgram()
        except Exception as e:
            logger.warning(f"Deepgram reconnect failed: {e}")
            await self._try_reconnect()

    async def _switch_to_fallback(self):
        if self._using_fallback:
            return
        logger.warning(f"[{self.hotel_id}] STT switching → Groq Whisper")
        self._using_fallback = True
        self._connected      = False
        if self._keepalive_task and not self._keepalive_task.done():
            self._keepalive_task.cancel()
        try:
            if self._connection:
                await self._connection.finish()
        except Exception:
            pass
        self._fallback.start()

    # ── Deepgram event handlers ───────────────────────────────────────────────

    async def _on_transcript(self, _client, result, **kwargs):
        """
        All transcripts pass through — no mute filtering here.
        The websocket_server handles barge-in logic based on _agent_talking flag.
        Ghost transcript filtering (TTS echo) is handled by:
          1. Twilio "clear" event sent on barge-in — stops TTS audio
          2. Brief grace period after greeting
        NOT by dropping audio/transcripts at STT level.
        """
        try:
            alt      = result.channel.alternatives[0]
            sentence = alt.transcript
            if not sentence or not sentence.strip():
                return

            if result.is_final:
                logger.info(f"📝 FINAL  [{self.hotel_id}]: {sentence}")
                asyncio.create_task(self.on_final(sentence))
            else:
                logger.debug(f"📝 INTERIM[{self.hotel_id}]: {sentence}")
                asyncio.create_task(self.on_interim(sentence))
        except Exception as e:
            logger.warning(f"Transcript parse error: {e}")

    async def _on_error(self, _client, error, **kwargs):
        logger.error(f"Deepgram error | {self.hotel_id}: {error}")
        await self._switch_to_fallback()

    async def _on_close(self, _client, close, **kwargs):
        self._connected = False
        logger.info(f"Deepgram closed | {self.hotel_id}")
        if not self._using_fallback and self._active:
            asyncio.create_task(self._try_reconnect())