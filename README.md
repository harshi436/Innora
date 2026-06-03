# 🏨 Hotel AI Voice Assistant

Multi-tenant AI phone concierge. The AI agent **calls guests** (outbound only — free Twilio international credits). Guests cannot call in.

**Stack:** FastAPI · Twilio · Deepgram STT · Qwen 2.5-7B · Coqui TTS · LangGraph · Qdrant · MongoDB · Redis

---

## 📁 Project Structure

```
hotel_ai_voice/
├── main.py                        # FastAPI entry point
├── config.py                      # Centralised settings from .env
├── requirements.txt
├── .env.example                   # Fill this and rename to .env
├── test.py                        # Trigger outbound AI call
│
├── routes/
│   ├── incoming_call.py           # POST /incoming-call (Twilio webhook)
│   └── admin.py                   # Hotel CRUD + PDF upload
│
├── websocket/
│   └── websocket_server.py        # WS /media-stream — full AI pipeline
│
├── speech/
│   └── deepgram_service.py        # Real-time STT (Deepgram Nova-2)
│
├── tts/
│   └── coqui_service.py           # Text → WAV → 8kHz μ-law (Coqui)
│
├── agents/
│   └── qwen_service.py            # Qwen 2.5-7B (streaming + intent classify)
│
├── ai_graph/
│   └── graph_builder.py           # LangGraph routing state machine
│
├── rag/
│   └── retrieval_service.py       # Qdrant vector search + PDF ingestion
│
├── database/
│   ├── mongodb.py                 # Motor async client (all 8 collections)
│   └── redis_client.py            # Async Redis session cache
│
├── utils/
│   └── sentence_tokenizer.py      # Streaming sentence boundary detector
│
└── scripts/
    ├── register_hotel.py          # Terminal hotel registration + PDF upload
    └── configure_twilio.py        # Auto-set Twilio webhook URL
```

---

## 🗄️ MongoDB Schema (Database: `Hotel`)

### Collection 1 — `hotels`
```json
{
  "hotel_name": "Grand Royal Hotel",
  "hotel_id": "grand_royal_001",
  "hotel_number": "+911234567890",
  "manager_contact": "+919876543210",
  "hotel_address": "123 Main St, Indore",
  "hotel_email": "info@grandroyal.com",
  "password": "<sha256_hash>",
  "dialed_number": "+17479665797",
  "system_prompt": "...",
  "created_at": "...",
  "updated_at": "..."
}
```

### Collection 2 — `Food_Orders`
```json
{
  "hotel_name": "Grand Royal Hotel",
  "hotel_id": "grand_royal_001",
  "guests": {
    "guest1": {
      "guest_number": "+91XXXXXXXXXX",
      "guest_room_number": "302",
      "food_order": ["Cappuccino", "Club Sandwich"]
    },
    "guest2": { ... }
  }
}
```

### Collection 3 — `Room_cleaning`
```json
{
  "hotel_id": "grand_royal_001",
  "guests": {
    "guest1": {
      "guest_number": "+91XXXXXXXXXX",
      "guest_room_number": "302",
      "room_cleaning": ["Clean room", "Change bedsheets"]
    }
  }
}
```

### Collection 4 — `Spa_Services`
```json
{
  "hotel_id": "grand_royal_001",
  "guests": {
    "guest1": {
      "spa_services": ["Full body massage", "Facial"]
    }
  }
}
```

### Collection 5 — `Essential_Needs`
```json
{
  "hotel_id": "grand_royal_001",
  "guests": {
    "guest1": {
      "essential_needs": ["Toothpaste", "Extra pillow"]
    }
  }
}
```

### Collection 6 — `hotel_pdf`
```json
{
  "hotel_name": "Grand Royal Hotel",
  "hotel_id": "grand_royal_001",
  "pdfs": [
    {
      "filename": "grand_royal_001_abc123_menu.pdf",
      "filepath": "uploads/hotel_pdfs/...",
      "chunk_count": 42,
      "uploaded_at": "..."
    }
  ]
}
```

### Collection 7 — `call_logs`
```json
{
  "hotel_name": "Grand Royal Hotel",
  "hotel_id": "grand_royal_001",
  "guests": {
    "guest1": {
      "guest_phone_number": "+91XXXXXXXXXX",
      "guest_room_number": "302",
      "call_sid": "CAxxxx",
      "conversation": [
        { "agent": "Hi sir, hope you're having a wonderful day. How may I assist you?" },
        { "guest": "I'd like a cappuccino please." },
        { "agent": "Of course! I've placed your order. It will be with you shortly." }
      ]
    }
  }
}
```

### Collection 8 — `Inquiry`
```json
{
  "hotel_id": "grand_royal_001",
  "guests": {
    "guest1": {
      "inquiry": [
        { "question": "What time does breakfast start?", "timestamp": "..." }
      ]
    }
  }
}
```

---

## 🚀 Setup

### 1. Install dependencies
```bash
cd hotel_ai_voice
python -m venv venv
source venv/bin/activate        # Windows: venv\Scripts\activate
pip install -r requirements.txt
```

### 2. Configure environment
```bash
cp .env.example .env
# Edit .env and fill in all values
```

### 3. Start MongoDB locally
```bash
mongod --dbpath ./data/db
```

### 4. Register your first hotel (terminal UI)
```bash
python scripts/register_hotel.py
```
This will prompt for all hotel details and then ask you to provide 1–5 PDF paths for the knowledge base.

### 5. Start the server
```bash
python main.py
```

### 6. Configure Twilio webhook
```bash
python scripts/configure_twilio.py
```

### 7. Make the AI call a guest
```bash
python test.py +91XXXXXXXXXX
```

---

## 📞 Call Flow

```
python test.py +91XXXXXXXXXX
    ↓ Twilio calls guest
    ↓ Guest answers
POST /incoming-call
    → Lookup hotel by Twilio number → resolve hotel_id
    → Store session in Redis
    → Return TwiML <Connect><Stream>
    ↓
WS /media-stream
    → Load hotel context (hotel_name, system_prompt, manager_contact)
    → Create call_log entry in MongoDB
    → Connect Deepgram STT
    → Generate dynamic greeting via Qwen LLM
    → Greet guest (Coqui TTS → μ-law → Twilio → guest ear)
    ↓
Guest speaks
    → Deepgram STT → final transcript
    → Qwen classifies intent (food_order / room_cleaning / spa / essential / inquiry / event / escalation / farewell)
    → LangGraph routes to correct node:
        food_order      → RAG check → save Food_Orders → Qwen response
        room_cleaning   → RAG check → save Room_cleaning → Qwen response
        spa_service     → RAG check → save Spa_Services → Qwen response
        essential_needs → RAG check → save Essential_Needs → Qwen response
        inquiry         → RAG search → Qwen answer → save Inquiry
        event_inquiry   → "Please contact manager at <number>"
        escalation      → Dial manager via Twilio REST API
        farewell        → Warm goodbye → end call
    → Response → sentence tokenizer → Coqui TTS → Twilio → guest
    → All turns saved to call_logs.conversation
    → AI asks follow-up: "Anything else I can help you with?"
    ↓
Barge-in: guest speaks during AI playback
    → Deepgram interim → flush TTS queue → clear Twilio buffer
    → AI stops speaking immediately
```

---

## 📊 Admin API Endpoints

| Method | Endpoint | Description |
|--------|----------|-------------|
| POST | `/admin/hotels` | Register hotel |
| GET | `/admin/hotels` | List all hotels |
| GET | `/admin/hotels/{hotel_id}` | Hotel details |
| PUT | `/admin/hotels/{hotel_id}` | Update hotel |
| POST | `/admin/hotels/{hotel_id}/upload-pdf` | Upload 1–5 PDFs |
| GET | `/admin/hotels/{hotel_id}/pdfs` | PDF list |
| GET | `/admin/hotels/{hotel_id}/food-orders` | Food orders |
| GET | `/admin/hotels/{hotel_id}/cleaning` | Room cleaning |
| GET | `/admin/hotels/{hotel_id}/spa` | Spa services |
| GET | `/admin/hotels/{hotel_id}/essentials` | Essential needs |
| GET | `/admin/hotels/{hotel_id}/inquiries` | Guest inquiries |
| GET | `/admin/hotels/{hotel_id}/calls` | Call logs |
| GET | `/health` | Health check |

---

## 🔑 Key Design Decisions

- **hotel_id everywhere**: Every MongoDB document, Redis key, Qdrant payload includes `hotel_id`. Zero cross-tenant data leakage.
- **Dialed number → hotel_id**: The Twilio `To` field is the only lookup key. One number = one hotel.
- **Dynamic responses only**: Every greeting, confirmation, follow-up, and farewell is generated by Qwen LLM. Nothing is hardcoded.
- **Sentence-level streaming TTS**: LLM tokens buffer until sentence boundary → immediate TTS synthesis. First audio latency ~800ms.
- **Barge-in**: Deepgram interim results → TTS queue flushed + Twilio buffer cleared within ~100ms.
- **Intent classification**: Separate Qwen call classifies each utterance into one of 9 intents before routing to LangGraph.
- **LangGraph**: 8-node state machine with intent-based routing. Each node writes to the correct MongoDB collection.
#   h o t e l - a i - a g e n t  
 