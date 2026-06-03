"""
rag/retrieval_service.py — Qdrant vector DB for hotel knowledge base.

FIXES v2:
  ✅ preload_embedder_sync — fixed indentation (was nested inside _ensure_embedder)
  ✅ Module-level preload_embedder_sync() now works correctly at startup
"""
import asyncio
import uuid
from typing import List, Optional
from loguru import logger

from qdrant_client import AsyncQdrantClient
from qdrant_client.models import (
    Distance,
    VectorParams,
    PointStruct,
    Filter,
    FieldCondition,
    MatchValue,
    FilterSelector,
)
from sentence_transformers import SentenceTransformer
import pdfplumber

from config import settings

EMBEDDING_MODEL = "all-MiniLM-L6-v2"   # 384-dim, fast
VECTOR_SIZE = 384
CHUNK_SIZE = 500
CHUNK_OVERLAP = 100


class RetrievalService:
    def __init__(self):
        self._client: Optional[AsyncQdrantClient] = None
        self._embedder: Optional[SentenceTransformer] = None
        self._client_lock = asyncio.Lock()
        self._embedder_lock = asyncio.Lock()

    async def _ensure_client(self) -> AsyncQdrantClient:
        if self._client is not None:
            return self._client

        async with self._client_lock:
            if self._client is not None:
                return self._client
            try:
                self._client = AsyncQdrantClient(
                    url=settings.qdrant_url,
                    api_key=settings.qdrant_api_key,
                )
                logger.info(f"✅ Qdrant client initialized | url={settings.qdrant_url}")
                return self._client
            except Exception as e:
                logger.error(f"Failed to initialize Qdrant client: {e}")
                raise

    async def _ensure_embedder(self) -> SentenceTransformer:
        """Lazy initialization for embedder (thread-safe)."""
        if self._embedder is not None:
            return self._embedder

        async with self._embedder_lock:
            if self._embedder is not None:
                return self._embedder
            try:
                logger.info(f"Loading embedding model: {EMBEDDING_MODEL}")
                self._embedder = SentenceTransformer(EMBEDDING_MODEL)
                logger.info(f"✅ Embedding model loaded: {EMBEDDING_MODEL}")
                return self._embedder
            except Exception as e:
                logger.error(f"Failed to load embedder: {e}")
                raise

    # ── FIX: preload_embedder_sync is now a PROPER class method ──────────────
    # Previously it was nested INSIDE _ensure_embedder — so it was unreachable.
    # That caused: "cannot import name 'preload_embedder_sync'" warning at startup
    # and the embedding model loading 3-4 seconds into the FIRST live call.

    async def preload_embedder_sync(self):
        """
        Preload embedding model during startup so first call has zero delay.
        Call from main.py lifespan before yield.
        Returns immediately if already loaded.
        """
        if self._embedder is not None:
            logger.info("⚡ Embedder already loaded — skip preload")
            return

        try:
            logger.info(f"⚡ Preloading embedding model: {EMBEDDING_MODEL}")
            # Load synchronously — safe at startup, avoids event loop binding issues
            self._embedder = SentenceTransformer(EMBEDDING_MODEL)
            logger.info(f"✅ Embedding model preloaded: {EMBEDDING_MODEL}")
        except Exception as e:
            logger.error(f"Embedder preload failed: {e}")
            self._embedder = None
            raise

    async def ensure_collection(self):
        """Create Qdrant collection if it doesn't exist."""
        try:
            client = await self._ensure_client()
            collections = await client.get_collections()
            names = [c.name for c in collections.collections]

            if settings.qdrant_collection not in names:
                await client.create_collection(
                    collection_name=settings.qdrant_collection,
                    vectors_config=VectorParams(size=VECTOR_SIZE, distance=Distance.COSINE),
                )
                logger.info(f"✅ Qdrant collection created: {settings.qdrant_collection}")
            else:
                logger.info(f"✅ Qdrant collection exists: {settings.qdrant_collection}")
        except Exception as e:
            logger.error(f"ensure_collection error: {e}")
            raise

    async def delete_hotel_data(self, hotel_id: str) -> None:
        """Delete ALL existing vectors for hotel_id from Qdrant."""
        try:
            client = await self._ensure_client()
            await client.delete(
                collection_name=settings.qdrant_collection,
                points_selector=FilterSelector(
                    filter=Filter(
                        must=[
                            FieldCondition(
                                key="hotel_id",
                                match=MatchValue(value=hotel_id),
                            )
                        ]
                    )
                ),
            )
            logger.info(f"🗑️  Deleted stale vectors | hotel_id={hotel_id}")
        except Exception as e:
            logger.error(f"delete_hotel_data error | hotel_id={hotel_id} | {e}")
            raise

    async def reingest_hotel_pdfs(self, filepaths: List[str], hotel_id: str) -> int:
        """Safe re-index: delete old vectors, then ingest fresh."""
        logger.info(f"🔄 Re-ingesting {len(filepaths)} PDFs | hotel_id={hotel_id}")
        await self.delete_hotel_data(hotel_id)
        total = 0
        for fp in filepaths:
            count = await self.ingest_pdf(fp, hotel_id)
            total += count
        logger.info(f"✅ Re-ingestion complete | {total} chunks | hotel_id={hotel_id}")
        return total

    async def ingest_pdf(self, filepath: str, hotel_id: str) -> int:
        """Extract PDF → chunk → embed → upsert tagged with hotel_id."""
        text = self._extract_pdf_text(filepath)
        if not text.strip():
            logger.warning(f"PDF empty or unreadable: {filepath}")
            return 0

        chunks = self._chunk_text(text)
        logger.info(f"📄 {len(chunks)} chunks extracted from {filepath}")

        embedder = await self._ensure_embedder()
        loop = asyncio.get_event_loop()
        vectors = await loop.run_in_executor(
            None, lambda: embedder.encode(chunks, show_progress_bar=False).tolist()
        )

        points = [
            PointStruct(
                id=str(uuid.uuid4()),
                vector=vec,
                payload={
                    "hotel_id": hotel_id,
                    "text": chunk,
                    "source": filepath,
                },
            )
            for chunk, vec in zip(chunks, vectors)
        ]

        client = await self._ensure_client()
        await client.upsert(collection_name=settings.qdrant_collection, points=points)
        logger.info(f"✅ Upserted {len(points)} vectors | hotel_id={hotel_id}")
        return len(points)
    
    
    # async def warmup(self):
    #     """Dummy search to establish Qdrant connection pool."""
    #     try:
    #         await self._ensure_client()
    #         # Just ping — no real search needed
    #         await asyncio.get_event_loop().run_in_executor(
    #             None,
    #             lambda: self._client.get_collections()
    #         )
    #         logger.info("✅ Qdrant warmed up")
    #     except Exception as e:
    #         logger.warning(f"Qdrant warmup: {e}")

    async def warmup(self):
        """Real dummy search — warms embedding model + Qdrant connection both."""
        try:
            await self.search("hotel room service", "warmup_dummy", top_k=1)
            logger.info("✅ Qdrant warmed up")
        except Exception as e:
            logger.warning(f"Qdrant warmup: {e}")


    async def search(self, query: str, hotel_id: str, top_k: int = 5) -> str:
        """Semantic search strictly filtered by hotel_id."""
        if not query.strip():
            return ""
        try:
            embedder = await self._ensure_embedder()
            loop = asyncio.get_event_loop()
            query_vec = await loop.run_in_executor(
                None, lambda: embedder.encode(query).tolist()
            )

            client = await self._ensure_client()
            results = await client.search(
                collection_name=settings.qdrant_collection,
                query_vector=query_vec,
                query_filter=Filter(
                    must=[FieldCondition(key="hotel_id", match=MatchValue(value=hotel_id))]
                ),
                limit=top_k,
                with_payload=True,
            )

            if not results:
                return ""

            context_parts = [r.payload.get("text", "") for r in results if r.score > 0.15]
            context = "\n\n".join(context_parts)
            logger.debug(f"RAG: {len(context_parts)} chunks | hotel_id={hotel_id} | q={query[:50]}")
            return context

        except Exception as e:
            logger.error(f"Qdrant search error: {e}")
            return ""

    async def close(self):
        if self._client is not None:
            try:
                await self._client.close()
                logger.info("✅ Qdrant client closed")
            except Exception as e:
                logger.error(f"Error closing Qdrant client: {e}")

    def _extract_pdf_text(self, filepath: str) -> str:
        text_parts = []
        try:
            with pdfplumber.open(filepath) as pdf:
                for page in pdf.pages:
                    page_text = page.extract_text()
                    if page_text:
                        text_parts.append(page_text)
        except Exception as e:
            logger.error(f"PDF extraction error: {e}")
        return "\n".join(text_parts)

    def _chunk_text(self, text: str) -> List[str]:
        chunks = []
        start = 0
        text_len = len(text)
        while start < text_len:
            end = min(start + CHUNK_SIZE, text_len)
            chunk = text[start:end].strip()
            if chunk:
                chunks.append(chunk)
            start += CHUNK_SIZE - CHUNK_OVERLAP
        return chunks


# ── Singleton instance ────────────────────────────────────────────────────────

retrieval_service = RetrievalService()


# ── Convenience functions for imports ─────────────────────────────────────────

async def ensure_collection():
    """Call from main.py lifespan startup."""
    await retrieval_service.ensure_collection()


# ── FIX: This module-level function now correctly delegates to the class method
async def preload_embedder_sync():
    """Call from main.py lifespan to warm up embedder before first call."""
    await retrieval_service.preload_embedder_sync()


async def ingest_pdf(filepath: str, hotel_id: str) -> int:
    return await retrieval_service.ingest_pdf(filepath, hotel_id)


async def reingest_hotel_pdfs(filepaths: List[str], hotel_id: str) -> int:
    return await retrieval_service.reingest_hotel_pdfs(filepaths, hotel_id)


async def search(query: str, hotel_id: str, top_k: int = 5) -> str:
    return await retrieval_service.search(query, hotel_id, top_k)





