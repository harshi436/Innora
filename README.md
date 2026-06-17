🏨 Hotel AI Voice Assistant

Multi-tenant AI concierge that answers hotel guests over phone calls (Twilio) and WhatsApp. Handles food orders, housekeeping, spa bookings, essentials requests, and FAQ answers pulled from a per-hotel PDF knowledge base — in English, Hindi, or Hinglish.

📖 For a full step-by-step breakdown of how every module works, see ARCHITECTURE.md.


✨ Features


Real-time inbound/outbound voice calls via Twilio Media Streams, with mid-sentence barge-in
Streaming STT (Deepgram, Groq Whisper fallback) → LLM (Groq/Gemini/Mistral) → TTS (Cartesia/Deepgram/Edge)
RAG knowledge base per hotel (PDFs → Qdrant) — agent only answers from uploaded hotel data
WhatsApp channel using the same intent + RAG + LLM pipeline, with auto order summaries
7-day guest memory in Redis (room number, past conversations)
Fully multi-tenant: every record/cache key/vector is tagged with hotel_id
Admin API + Streamlit dashboard for hotel management and analytics


🧰 Tech Stack

FastAPI · Twilio (Voice + WhatsApp) · Deepgram · Groq/Gemini/Mistral · Cartesia · LangGraph · Qdrant · MongoDB · Redis · Streamlit

📁 Structure

main.py              FastAPI entry point + startup/shutdown
config.py             Settings from .env
routes/                incoming_call.py, whatsapp.py, admin.py
websocket/             websocket_server.py — full voice pipeline
speech/                deepgram_service.py — STT
tts/                   tts_service.py, Greeting_cache.py
agents/                qwen_service.py — LLM provider layer
ai_graph/              graph_builder.py — LangGraph routing
rag/                   retrieval_service.py — Qdrant + PDF ingestion
database/              mongodb.py, redis_client.py
whatsapp/              whatsapp_service.py
utils/                 email_sender.py, sentence_tokenizer.py
scripts/               register_hotel.py, seed_hotel.py, configure_twilio.py
app.py                 Streamlit admin dashboard
test.py                CLI: trigger an outbound AI call

🗄️ MongoDB Collections (Hotel db)

hotels · Food_Orders · Room_cleaning · Spa_Services · Essential_Needs · hotel_pdf · call_logs · Inquiry

All keyed by hotel_id, with guest-level data nested under a guests map.

🚀 Setup

bashgit clone <this-repo> && cd hotel_ai_voice
python -m venv venv && source venv/bin/activate
pip install -r requirements.txt
cp .env.example .env   # fill in Twilio, Deepgram, Cartesia, Groq/Gemini/Mistral, Mongo, Redis, Qdrant keys

mongod --dbpath ./data/db        # start MongoDB
redis-server                      # start Redis

python scripts/register_hotel.py  # register a hotel + upload 1-5 PDFs
python main.py                    # start the server

ngrok http 8000
python scripts/configure_twilio.py   # auto-set the voice webhook

Call the hotel's Twilio number directly, or trigger an outbound call:

bashpython test.py +91XXXXXXXXXX

Optional dashboard:

bashstreamlit run app.py

📞 Call Flow (high level)

Twilio webhook → resolve hotel_id → Redis session
   → WS /media-stream → STT → intent classify → RAG search (if needed)
   → LLM streamed reply → TTS streamed back → guest hears it
   → barge-in cancels playback instantly if guest interrupts
   → farewell → WhatsApp order summary → hang up

📊 Admin API

MethodEndpointDescriptionPOST/admin/hotelsRegister hotelGET/admin/hotelsList hotelsPOST/admin/hotels/{id}/upload-pdfUpload 1–5 PDFsGET/admin/hotels/{id}/{food-orders|cleaning|spa|essentials|inquiries|calls|pdfs}View dataPOST/whatsapp/incomingTwilio WhatsApp webhookGET/healthHealth check

⚠️ Known Gaps


Greeting_cache.py calls mongo_client.get_all_hotels(), which doesn't exist (mongodb.py only has list_hotels()) — bulk greeting pre-warm fails silently at startup.
scripts/seed_hotel.py uses field names (name, manager_phone) that don't match the schema used elsewhere (hotel_name, manager_contact).
whatsapp_service.py has a hardcoded sandbox fallback hotel — remove before production.
