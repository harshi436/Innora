"""
database/redis_client.py â€” Async Redis session cache.

UPDATES:
  âœ… 7-day guest memory (cross-session â€” survives call end)
  âœ… Guest room remembered for 7 days
  âœ… append_guest_memory / get_guest_memory / save_guest_room / get_guest_room
  âœ… Per-call history unchanged (SESSION_TTL = 1hr)
"""

import json
from typing import Optional, Dict, Any, List

import redis.asyncio as aioredis
from loguru import logger

from config import settings

SESSION_TTL      = 3600           # 1 hour  â€” per-call session
GUEST_MEMORY_TTL = 180 * 24 * 3600
GUEST_MEMORY_MAX_MESSAGES = 200
GUEST_PROFILE_MAX_ITEMS = 100


class RedisClient:

    def __init__(self):
        self._client: Optional[aioredis.Redis] = None

    # â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€
    # CONNECT / DISCONNECT
    # â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€

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
            logger.info("âœ… Redis connected")
        except Exception as e:
            logger.error(f"âŒ Redis connection failed: {e}")
            self._client = None
            raise

    async def disconnect(self):
        try:
            if self._client:
                await self._client.aclose()
                logger.info("Redis disconnected")
        except Exception as e:
            logger.error(f"Redis disconnect error: {e}")

    # â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€
    # SESSION METHODS  (per-call, 1hr TTL)
    # â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€

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

    # â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€
    # PER-CALL CHAT HISTORY  (1hr TTL â€” active call only)
    # â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€

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

    # â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€
    # 7-DAY GUEST MEMORY  (cross-session â€” survives call end)
    # â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€

    async def append_guest_memory(
        self,
        guest_number: str,
        hotel_id: str,
        role: str,
        content: str,
    ) -> None:
        """
        Save a conversation turn to 7-day persistent guest memory.
        Keyed by guest phone number + hotel â€” survives across calls.
        Capped at 40 messages (oldest dropped automatically).
        """
        try:
            if not self._client:
                return
            key = f"guest_memory:{hotel_id}:{guest_number}"
            msg = json.dumps({"role": role, "content": content})
            await self._client.rpush(key, msg)
            await self._client.ltrim(key, -GUEST_MEMORY_MAX_MESSAGES, -1)
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
        Returns list of {role, content} dicts â€” ready to inject into LLM messages.
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

    async def update_guest_profile(
        self,
        guest_number: str,
        hotel_id: str,
        updates: Dict[str, Any],
    ) -> None:
        """
        Merge structured guest facts into a 7-day profile.
        Keeps stable scalar facts and capped rolling lists for preferences/requests/etc.
        """
        try:
            if not self._client or not updates:
                return
            key = f"guest_profile:{hotel_id}:{guest_number}"
            raw = await self._client.get(key)
            profile = json.loads(raw) if raw else {}

            for field in ("name", "room_number", "last_ai_response"):
                value = updates.get(field)
                if value:
                    profile[field] = value

            for field in ("preferences", "requests", "complaints", "orders", "questions"):
                values = updates.get(field) or []
                if isinstance(values, str):
                    values = [values]
                existing = profile.get(field) or []
                for value in values:
                    if value and value not in existing:
                        existing.append(value)
                profile[field] = existing[-GUEST_PROFILE_MAX_ITEMS:]

            await self._client.set(key, json.dumps(profile), ex=GUEST_MEMORY_TTL)
            logger.info(
                f"ðŸ§  Guest profile updated | guest={guest_number[-4:]}*** | fields={list(updates.keys())}"
            )
        except Exception as e:
            logger.error(f"Redis update_guest_profile error: {e}")

    async def get_guest_profile(
        self,
        guest_number: str,
        hotel_id: str,
    ) -> Dict[str, Any]:
        """Return structured guest facts for prompt injection."""
        try:
            if not self._client:
                return {}
            key = f"guest_profile:{hotel_id}:{guest_number}"
            raw = await self._client.get(key)
            return json.loads(raw) if raw else {}
        except Exception as e:
            logger.error(f"Redis get_guest_profile error: {e}")
            return {}

    async def save_guest_room(
        self,
        guest_number: str,
        hotel_id: str,
        room: str,
    ) -> None:
        """
        Remember a guest's room number for 7 days.
        Next call pe automatically load hoga â€” guest ko dobara room nahi poochna padega.
        """
        try:
            if not self._client or not room:
                return
            key = f"guest_room:{hotel_id}:{guest_number}"
            await self._client.set(key, room, ex=GUEST_MEMORY_TTL)
            logger.info(f"ðŸ§  Room saved to memory | guest={guest_number[-4:]}*** | room={room}")
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
                    for kw in ["order confirm", "order hai", "deliver", "â‚¹", "rice", "pizza",
                               "burger", "roti", "biryani", "naan", "sandwich"]
                ):
                    order_mentions.append(content[:120])
            if order_mentions:
                return " | ".join(order_mentions[-3:])  # Last 3 order confirmations
            return None
        except Exception as e:
            logger.error(f"Redis get_guest_order_summary error: {e}")
            return None

    # â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€
    # GENERIC METHODS
    # â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€

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

    async def scan_keys(self, pattern: str, count: int = 100) -> List[str]:
        """Return keys matching a Redis pattern without blocking the server."""
        try:
            if not self._client:
                return []
            keys = []
            async for key in self._client.scan_iter(match=pattern, count=count):
                keys.append(key)
            return keys
        except Exception as e:
            logger.error(f"Redis SCAN error: {e}")
            return []


redis_client = RedisClient()

