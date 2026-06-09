"""
config.py — Centralized application settings.

UPDATED v7
────────────────────────────────────────────────────────────
✅ Pydantic v2 compatible
✅ Multi-provider TTS support
✅ Better env validation
✅ Safe defaults
✅ Realtime voice optimized
✅ Cleaner configuration structure
"""

from pydantic_settings import BaseSettings
from pydantic import Field, ConfigDict


class Settings(BaseSettings):

    model_config = ConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
        protected_namespaces=(),
        case_sensitive=False,
    )

    # ─────────────────────────────────────────────────────
    # Twilio
    # ─────────────────────────────────────────────────────

    twilio_account_sid: str = Field(..., alias="TWILIO_ACCOUNT_SID")
    twilio_auth_token: str = Field(..., alias="TWILIO_AUTH_TOKEN")
    twilio_phone_number: str = Field(..., alias="TWILIO_PHONE_NUMBER")

    # ─────────────────────────────────────────────────────
    # Ngrok
    # ─────────────────────────────────────────────────────

    ngrok_url: str = Field(..., alias="NGROK_URL")

    # ─────────────────────────────────────────────────────
    # Deepgram
    # ─────────────────────────────────────────────────────
    assemblyai_api_key: str = Field(..., alias="ASSEMBLYAI_API_KEY")

    deepgram_api_key: str = Field(..., alias="DEEPGRAM_API_KEY")

    deepgram_aura_model: str = Field(
        default="aura-asteria-en",
        alias="DEEPGRAM_AURA_MODEL",
    )

    # ─────────────────────────────────────────────────────
    # Cartesia
    # ─────────────────────────────────────────────────────

    cartesia_api_key_1: str = Field(default="", alias="CARTESIA_API_KEY_1")
    cartesia_api_key_2: str = Field(default="", alias="CARTESIA_API_KEY_2")
    cartesia_api_key_3: str = Field(default="", alias="CARTESIA_API_KEY_3")
    cartesia_api_key_4: str = Field(default="", alias="CARTESIA_API_KEY_4")
    cartesia_api_key_5: str = Field(default="", alias="CARTESIA_API_KEY_5")

    cartesia_voice_id_en: str = Field(
        default="95d51f79-c397-46f9-b49a-23763d3eaa2d",
        alias="CARTESIA_VOICE_ID_EN",
    )

    cartesia_voice_id_hi: str = Field(
        default="95d51f79-c397-46f9-b49a-23763d3eaa2d",
        alias="CARTESIA_VOICE_ID_HI",
    )

    cartesia_model_id: str = Field(
        default="sonic-3.5",
        alias="CARTESIA_MODEL_ID",
    )

    # ── ElevenLabs TTS ─────────────────────────────

    elevenlabs_api_key_1: str = Field(default="", env="ELEVENLABS_API_KEY_1")
    elevenlabs_api_key_2: str = Field(default="", env="ELEVENLABS_API_KEY_2")
    elevenlabs_api_key_3: str = Field(default="", env="ELEVENLABS_API_KEY_3")
    elevenlabs_api_key_4: str = Field(default="", env="ELEVENLABS_API_KEY_4")
    elevenlabs_api_key_5: str = Field(default="", env="ELEVENLABS_API_KEY_5")
    
    elevenlabs_voice_id: str = Field(
        default="cgSgspJ2msm6clMCkdW9",
        env="ELEVENLABS_VOICE_ID",
    )
    
    elevenlabs_model_id: str = Field(
        default="eleven_multilingual_v2",
        env="ELEVENLABS_MODEL_ID",
    )
        # ── Groq LLM ────────────────────────────────────────────────────────────
    groq_base_url: str = Field(
        default="https://api.groq.com/openai/v1",
        env="GROQ_BASE_URL",
    )
    groq_api_keys: str = Field(..., env="GROQ_API_KEYS")
    model_name:    str = Field(default="llama-3.3-70b-versatile", env="MODEL_NAME")

    # ── Gemini ──────────────────────────────────────────────────────────────
    gemini_api_key: str = Field(default="", env="GEMINI_API_KEY")

    # ── Mistral ─────────────────────────────────────────────────────────────
    mistral_api_key: str = Field(default="", env="MISTRAL_API_KEY")

    # ── MongoDB ─────────────────────────────────────────────────────────────
    mongo_uri: str = Field(default="mongodb://localhost:27017", env="MONGO_URI")
    db_name:   str = Field(default="Hotel", env="DB_NAME")

    # ── Redis ───────────────────────────────────────────────────────────────
    redis_uri: str = Field(..., env="REDIS_URI")

    # ── Qdrant (Vector DB) ───────────────────────────────────────────────────
    qdrant_url:        str = Field(..., env="QDRANT_URL")
    qdrant_api_key:    str = Field(..., env="QDRANT_API_KEY")
    qdrant_collection: str = Field(default="hotel_knowledge", env="QDRANT_COLLECTION")

    # ── LangSmith (optional) ─────────────────────────────────────────────────
    langchain_tracing_v2: str = Field(default="false", env="LANGCHAIN_TRACING_V2")
    langchain_api_key:    str = Field(default="", env="LANGCHAIN_API_KEY")
    langchain_project:    str = Field(default="hotel-ai-voice", env="LANGCHAIN_PROJECT")

    # ── App ─────────────────────────────────────────────────────────────────
    app_host:  str = Field(default="0.0.0.0", env="APP_HOST")
    app_port:  int = Field(default=8000, env="APP_PORT")
    log_level: str = Field(default="INFO", env="LOG_LEVEL")
    conversation_log_only: bool = Field(default=True, env="CONVERSATION_LOG_ONLY")
settings = Settings()
