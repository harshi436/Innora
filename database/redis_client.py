"""
database/redis_client.py — Async Redis session cache.

UPDATES:
  ✅ 7-day guest memory (cross-session — survives call end)
  ✅ Guest room remembered for 7 days
  ✅ append_guest_memory / get_guest_memory / save_guest_room / get_guest_room
  ✅ Per-call history unchanged (SESSION_TTL = 1hr)
"""

import json
from typing import Optional, Dict, Any, List

import redis.asyncio as aioredis
from loguru import logger

from config import settings

SESSION_TTL      = 3600           # 1 hour  — per-call session
GUEST_MEMORY_TTL = 7 * 24 * 3600 # 7 days  — cross-session guest memory


class RedisClient:

    def __init__(self):
        self._client: Optional[aioredis.Redis] = None

    # ─────────────────────────────────────────────────────────────
    # CONNECT / DISCONNECT
    # ─────────────────────────────────────────────────────────────

    async def connect(self):
        try:
            self._client = aioredis.from_url(
                settings.redis_uri,
                encoding="utf-8",
                decode_responses=True,
                socket_keepalive=True,
                health_check_interval=30,
                retry_on_timeout=True,
                socket_connect_timeout=10,
                socket_timeout=10,
            )
            await self._client.ping()
            logger.info("✅ Redis connected")
        except Exception as e:
            logger.error(f"❌ Redis connection failed: {e}")
            self._client = None

    async def disconnect(self):
        try:
            if self._client:
                await self._client.aclose()
                logger.info("Redis disconnected")
        except Exception as e:
            logger.error(f"Redis disconnect error: {e}")

    # ─────────────────────────────────────────────────────────────
    # SESSION METHODS  (per-call, 1hr TTL)
    # ─────────────────────────────────────────────────────────────

    async def set_session(self, key: str, data: Dict[str, Any]) -> None:
        try:
            if not self._client:
                return
            await self._client.set(key, json.dumps(data), ex=SESSION_TTL)
        except Exception as e:
            logger.error(f"Redis set_session error: {e}")

    async def get_session(self, key: str) -> Optional[Dict[str, Any]]:
        try:
            if not self._client:
                return None
            raw = await self._client.get(key)
            return json.loads(raw) if raw else None
        except Exception as e:
            logger.error(f"Redis get_session error: {e}")
            return None

    async def update_session(self, key: str, updates: Dict[str, Any]) -> None:
        try:
            session = await self.get_session(key) or {}
            session.update(updates)
            await self.set_session(key, session)
        except Exception as e:
            logger.error(f"Redis update_session error: {e}")

    async def delete_session(self, key: str) -> None:
        try:
            if not self._client:
                return
            await self._client.delete(key)
        except Exception as e:
            logger.error(f"Redis delete_session error: {e}")

    # ─────────────────────────────────────────────────────────────
    # PER-CALL CHAT HISTORY  (1hr TTL — active call only)
    # ─────────────────────────────────────────────────────────────

    async def append_message(self, key: str, role: str, content: str) -> None:
        try:
            if not self._client:
                return
            history_key = f"history:{key}"
            msg = json.dumps({"role": role, "content": content})
            await self._client.rpush(history_key, msg)
            await self._client.expire(history_key, SESSION_TTL)
        except Exception as e:
            logger.error(f"Redis append_message error: {e}")

    async def get_history(self, key: str) -> List[Dict]:
        try:
            if not self._client:
                return []
            history_key = f"history:{key}"
            raw_list = await self._client.lrange(history_key, 0, -1)
            return [json.loads(m) for m in raw_list]
        except Exception as e:
            logger.error(f"Redis get_history error: {e}")
            return []

    async def clear_history(self, key: str) -> None:
        try:
            if not self._client:
                return
            await self._client.delete(f"history:{key}")
        except Exception as e:
            logger.error(f"Redis clear_history error: {e}")

    # ─────────────────────────────────────────────────────────────
    # 7-DAY GUEST MEMORY  (cross-session — survives call end)
    # ─────────────────────────────────────────────────────────────

    async def append_guest_memory(
        self,
        guest_number: str,
        hotel_id: str,
        role: str,
        content: str,
    ) -> None:
        """
        Save a conversation turn to 7-day persistent guest memory.
        Keyed by guest phone number + hotel — survives across calls.
        Capped at 40 messages (oldest dropped automatically).
        """
        try:
            if not self._client:
                return
            key = f"guest_memory:{hotel_id}:{guest_number}"
            msg = json.dumps({"role": role, "content": content})
            await self._client.rpush(key, msg)
            await self._client.ltrim(key, -40, -1)          # Keep last 40 turns
            await self._client.expire(key, GUEST_MEMORY_TTL)
        except Exception as e:
            logger.error(f"Redis append_guest_memory error: {e}")

    async def get_guest_memory(
        self,
        guest_number: str,
        hotel_id: str,
        last_n: int = 20,
    ) -> List[Dict]:
        """
        Load last N conversation turns for this guest across ALL past calls.
        Returns list of {role, content} dicts — ready to inject into LLM messages.
        """
        try:
            if not self._client:
                return []
            key = f"guest_memory:{hotel_id}:{guest_number}"
            raw_list = await self._client.lrange(key, -last_n, -1)
            return [json.loads(m) for m in raw_list]
        except Exception as e:
            logger.error(f"Redis get_guest_memory error: {e}")
            return []

    async def save_guest_room(
        self,
        guest_number: str,
        hotel_id: str,
        room: str,
    ) -> None:
        """
        Remember a guest's room number for 7 days.
        Next call pe automatically load hoga — guest ko dobara room nahi poochna padega.
        """
        try:
            if not self._client or not room:
                return
            key = f"guest_room:{hotel_id}:{guest_number}"
            await self._client.set(key, room, ex=GUEST_MEMORY_TTL)
            logger.info(f"🧠 Room saved to memory | guest={guest_number[-4:]}*** | room={room}")
        except Exception as e:
            logger.error(f"Redis save_guest_room error: {e}")

    async def get_guest_room(
        self,
        guest_number: str,
        hotel_id: str,
    ) -> Optional[str]:
        """
        Retrieve guest's remembered room number (7-day persistent).
        Returns None if not found or expired.
        """
        try:
            if not self._client:
                return None
            key = f"guest_room:{hotel_id}:{guest_number}"
            return await self._client.get(key)
        except Exception as e:
            logger.error(f"Redis get_guest_room error: {e}")
            return None

    async def get_guest_order_summary(
        self,
        guest_number: str,
        hotel_id: str,
    ) -> Optional[str]:
        """
        Return a short summary of past orders for this guest (if any).
        Used to inject context: 'aapne pehle Jeera Rice order kiya tha'.
        Reads from guest_memory and filters food_order turns.
        """
        try:
            memory = await self.get_guest_memory(guest_number, hotel_id, last_n=40)
            order_mentions = []
            for msg in memory:
                content = msg.get("content", "")
                # Look for assistant messages confirming orders
                if msg.get("role") == "assistant" and any(
                    kw in content.lower()
                    for kw in ["order confirm", "order hai", "deliver", "₹", "rice", "pizza",
                               "burger", "roti", "biryani", "naan", "sandwich"]
                ):
                    order_mentions.append(content[:120])
            if order_mentions:
                return " | ".join(order_mentions[-3:])  # Last 3 order confirmations
            return None
        except Exception as e:
            logger.error(f"Redis get_guest_order_summary error: {e}")
            return None

    # ─────────────────────────────────────────────────────────────
    # GENERIC METHODS
    # ─────────────────────────────────────────────────────────────

    async def set(self, key: str, value: str, ttl: int = SESSION_TTL) -> None:
        try:
            if not self._client:
                return
            await self._client.set(key, value, ex=ttl)
        except Exception as e:
            logger.error(f"Redis SET error: {e}")

    async def get(self, key: str) -> Optional[str]:
        try:
            if not self._client:
                return None
            return await self._client.get(key)
        except Exception as e:
            logger.error(f"Redis GET error: {e}")
            return None

    async def delete(self, key: str) -> None:
        try:
            if not self._client:
                return
            await self._client.delete(key)
        except Exception as e:
            logger.error(f"Redis DELETE error: {e}")


redis_client = RedisClient()