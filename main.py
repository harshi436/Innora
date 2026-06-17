"""main.py — FastAPI entry point.

FIXES v2:
  ✅ preload_embedder_sync import works now (retrieval_service.py fixed)
  ✅ Embedder loads at startup → zero delay on first call
"""

import os
import sys
from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from loguru import logger

from config import settings
from database.mongodb import mongo_client
from database.redis_client import redis_client
from routes.incoming_call import router as call_router
from websocket.websocket_server import router as ws_router
from routes.admin import router as admin_router
from routes.whatsapp import router as whatsapp_router # this is for whatsapp changes are applied 
from rag.retrieval_service import ensure_collection, retrieval_service


def _configure_terminal_logging() -> None:
    logger.remove()

    if not settings.conversation_log_only:
        logger.add(sys.stderr, level=settings.log_level)
        return

    conversation_markers = (
        "Guest:",
        "Agent:",
        "WhatsApp sent",
        "WhatsApp send failed",
        "Price",
        "Total:",
        "BARGE-IN",
        "Barge-in complete",
        "MongoDB connected",
        "Redis connected",
        "Qdrant collection ready",
        "Embedding model preloaded",
        "Ready | ngrok",
    )

    def conversation_or_problem(record):
        return (
            record["level"].no >= 30
            or any(marker in record["message"] for marker in conversation_markers)
        )

    logger.add(
        sys.stderr,
        level="INFO",
        filter=conversation_or_problem,
        format="{time:HH:mm:ss} | {message}",
    )


_configure_terminal_logging()


# ─────────────────────────────────────────────────────────────
# LangChain Environment
# ─────────────────────────────────────────────────────────────

os.environ["LANGCHAIN_TRACING_V2"] = settings.langchain_tracing_v2

if settings.langchain_api_key:
    os.environ["LANGCHAIN_API_KEY"] = settings.langchain_api_key

os.environ["LANGCHAIN_PROJECT"] = settings.langchain_project


# ─────────────────────────────────────────────────────────────
# Lifespan
# ─────────────────────────────────────────────────────────────

@asynccontextmanager
async def lifespan(app: FastAPI):

    logger.info("🚀 Hotel AI Voice Assistant starting...")

    # MongoDB
    try:
        await mongo_client.connect()
        logger.info("✅ MongoDB connected")
    except Exception as e:
        logger.error(f"❌ MongoDB connection failed: {e}")
        raise

    # Redis
    try:
        await redis_client.connect()
        logger.info("✅ Redis connected")
    except Exception as e:
        logger.error(f"Redis connection failed: {e}")
        raise

    # Qdrant
    try:
        await ensure_collection()
        #await retrieval_service.warmup()
        logger.info("✅ Qdrant collection ready")
    except Exception as e:
        logger.error(f"Qdrant init failed: {e}")
        raise

    # ── FIX: Embedder preload — now works because retrieval_service.py is fixed ──
    # Previously: preload_embedder_sync was nested INSIDE _ensure_embedder()
    # so it was never accessible → warning at startup → 3-4s delay on first call.
    # Now: it's a proper class method → loads at startup → first call instant.
    try:
        await retrieval_service.preload_embedder_sync()
        logger.info("⚡ Embedding model preloaded — first call will be fast")
    except Exception as e:
        logger.error(f"Embedder preload failed: {e}")
        raise

    logger.info(f"✅ Ready | ngrok: {settings.ngrok_url}")

    yield

    # ─────────────────────────────
    # Shutdown
    # ─────────────────────────────

    logger.info("🛑 Hotel AI Voice Assistant shutting down...")

    try:
        await retrieval_service.close()
        logger.info("✅ Qdrant client closed")
    except Exception as e:
        logger.warning(f"⚠️ Qdrant shutdown issue: {e}")

    try:
        await mongo_client.disconnect()
        logger.info("✅ MongoDB disconnected")
    except Exception as e:
        logger.warning(f"⚠️ MongoDB shutdown issue: {e}")

    try:
        await redis_client.disconnect()
        logger.info("✅ Redis disconnected")
    except Exception as e:
        logger.warning(f"⚠️ Redis shutdown issue: {e}")

    logger.info("👋 Shutdown complete")


# ─────────────────────────────────────────────────────────────
# FastAPI App
# ─────────────────────────────────────────────────────────────

app = FastAPI(
    title="Hotel AI Voice Assistant",
    version="2.3.0",
    lifespan=lifespan,
)

# CORS
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)

# Routers
app.include_router(call_router)
app.include_router(ws_router)
app.include_router(admin_router)
app.include_router(whatsapp_router) 


# Health Check
@app.get("/health")
async def health():
    return {
        "status": "ok",
        "version": "2.3.0"
    }


# Run directly
if __name__ == "__main__":
    import uvicorn

    uvicorn.run(
        "main:app",
        host=settings.app_host,
        port=settings.app_port,
        reload=False,
    )