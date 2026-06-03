"""
routes/incoming_call.py — Twilio POST webhook.

BUG FIXED:
  ❌ OLD (BROKEN): caller_number = form.get("To")  ← WRONG for outgoing calls
                   dialed_number = form.get("From") ← WRONG for outgoing calls

  ✅ NEW (FIXED):  For OUTGOING calls (agent calls guest via test.py):
                     From = Twilio number (+17479665797)  → hotel lookup
                     To   = Guest number  (+91XXXXXXXXXX) → caller_number

                   For INCOMING calls (guest calls Twilio number):
                     From = Guest number  (+91XXXXXXXXXX) → caller_number
                     To   = Twilio number (+17479665797)  → hotel lookup

  Both cases: hotel is always on the Twilio number side.
  So hotel lookup is always by the TWILIO number, not the guest number.

  FIX: Read both From and To, then figure out which one is the Twilio number

  by checking against settings.twilio_phone_number, OR simply always look up
  hotel by both and use whichever matches. The simplest correct approach:
  hotel lookup tries "To" first (incoming), then "From" (outgoing).
"""

from fastapi import APIRouter, Request, Response
from twilio.twiml.voice_response import Stream, VoiceResponse, Connect
from loguru import logger

from config import settings
from database.mongodb import mongo_client
from database.redis_client import redis_client

router = APIRouter()


@router.post("/incoming-call")
async def incoming_call(request: Request):

    try:
        form = await request.form()

        from_number: str = form.get("From", "")   # For incoming: guest. For outgoing: Twilio.
        to_number: str = form.get("To", "")        # For incoming: Twilio. For outgoing: guest.
        call_sid: str = form.get("CallSid", "")
        call_direction: str = form.get("Direction", "inbound")  # "inbound" or "outbound-api"

        logger.info(
            f"📞 Incoming Call | "
            f"From={from_number} | "
            f"To={to_number} | "
            f"Direction={call_direction} | "
            f"sid={call_sid}"
        )


        # Normalize both numbers
        from_clean = from_number.replace(" ", "").replace("-", "").strip()
        to_clean = to_number.replace(" ", "").replace("-", "").strip()

        twilio_number = settings.twilio_phone_number.replace(" ", "").replace("-", "").strip()

        if call_direction == "outbound-api" or from_clean == twilio_number:
            # Outgoing call: From=Twilio, To=Guest
            dialed_number = from_clean   # Twilio number → hotel lookup
            caller_number = to_clean     # Guest number
        else:
            # Incoming call: From=Guest, To=Twilio
            dialed_number = to_clean     # Twilio number → hotel lookup
            caller_number = from_clean   # Guest number

        logger.info(
            f"🔍 Hotel lookup by: {dialed_number} | Guest: {caller_number}"
        )

        # ─────────────────────────────────────────────────────────────
        # HOTEL LOOKUP
        # ─────────────────────────────────────────────────────────────

        hotel = await mongo_client.get_hotel_by_dialed_number(dialed_number)

        if not hotel:
            logger.warning(f"⚠️ No hotel mapped for number: {dialed_number}")
            vr = VoiceResponse()
            vr.say(
                "Sorry, this hotel service is not configured correctly. "
                "Please contact hotel management directly.",
                voice="Polly.Joanna",
            )
            return Response(content=str(vr), media_type="application/xml")

        hotel_id = hotel.get("hotel_id", "")
        hotel_name = hotel.get("hotel_name") or hotel.get("name", "Hotel")
        manager_contact = (
            hotel.get("manager_contact")
            or hotel.get("manager_phone", "")
        )
        hotel_email = hotel.get("hotel_email", "")
        system_prompt = hotel.get("system_prompt", "")

        logger.info(
            f"🏨 Hotel Loaded | "
            f"name={hotel_name} | "
            f"hotel_id={hotel_id}"
        )

        # ─────────────────────────────────────────────────────────────
        # REDIS SESSION
        # ─────────────────────────────────────────────────────────────

        await redis_client.set_session(
            f"call:{call_sid}",
            {
                "hotel_id": hotel_id,
                "hotel_name": hotel_name,
                "caller_number": caller_number,
                "dialed_number": dialed_number,
                "call_sid": call_sid,
                "manager_contact": manager_contact,
                "hotel_email": hotel_email,
                "system_prompt": system_prompt,
            },
        )

        # ─────────────────────────────────────────────────────────────
        # TWIML — Connect to WebSocket Media Stream
        # ─────────────────────────────────────────────────────────────

        ngrok_base = settings.ngrok_url.rstrip("/")
        ws_url = (
            ngrok_base
            .replace("https://", "wss://")
            .replace("http://", "ws://")
        )
        ws_url = f"{ws_url}/media-stream"

        logger.info(f"🔌 WebSocket URL: {ws_url}")

        response = VoiceResponse()
        connect = Connect()
        stream = Stream(url=ws_url)

        stream.parameter(name="hotel_id", value=str(hotel_id))
        stream.parameter(name="call_sid", value=str(call_sid))
        stream.parameter(name="caller_number", value=str(caller_number))

        connect.append(stream)
        response.append(connect)

        twiml_response = str(response)
        logger.info(f"📡 TwiML Generated:\n{twiml_response}")

        return Response(
            content=twiml_response,
            media_type="application/xml",
        )

    except Exception as e:

        logger.error(f"❌ Incoming call error: {e}", exc_info=True)

        vr = VoiceResponse()
        vr.say(
            "Sorry, an internal server error occurred.",
            voice="Polly.Joanna",
        )
        return Response(content=str(vr), media_type="application/xml")