"""
routes/whatsapp.py — Inbound WhatsApp webhook from Twilio.

Twilio sends a POST to /whatsapp/incoming when a guest messages
the hotel's WhatsApp number.

Configure in Twilio Console:
  Messaging → WhatsApp Sandbox (or approved number)
  → When a message comes in → POST https://<ngrok>/whatsapp/incoming
"""

from fastapi import APIRouter, Request, Response
from loguru import logger

router = APIRouter(prefix="/whatsapp", tags=["WhatsApp"])


@router.post("/incoming")
async def whatsapp_incoming(request: Request):
    """
    Handle inbound WhatsApp message from Twilio.
    Returns TwiML MessagingResponse with the AI reply.
    """
    try:
        form = await request.form()

        from_number = form.get("From", "")   # e.g. whatsapp:+919876543210
        to_number   = form.get("To",   "")   # e.g. whatsapp:+17479665797
        body        = form.get("Body",  "").strip()

        logger.info(
            f"📲 WA webhook | From={from_number} | To={to_number} | "
            f"Body={body[:60]}"
        )

        if not body:
            reply_text = "Namaste! Main aapki kaise madad kar sakta hoon? 🙏"
        else:
            from whatsapp.whatsapp_service import handle_inbound_whatsapp
            reply_text = await handle_inbound_whatsapp(
                from_number=from_number,
                body=body,
                to_number=to_number,
            )

        twiml = (
            '<?xml version="1.0" encoding="UTF-8"?>'
            "<Response>"
            f"<Message>{_escape_xml(reply_text)}</Message>"
            "</Response>"
        )
        return Response(content=twiml, media_type="application/xml")

    except Exception as e:
        logger.error(f"WhatsApp webhook error: {e}", exc_info=True)
        twiml = (
            '<?xml version="1.0" encoding="UTF-8"?>'
            "<Response>"
            "<Message>Maafi chahte hain, abhi thodi technical dikkat hai. "
            "Kripya thodi der baad dobara try karein.</Message>"
            "</Response>"
        )
        return Response(content=twiml, media_type="application/xml")


def _escape_xml(text: str) -> str:
    """Escape special XML characters in message body."""
    return (
        text
        .replace("&", "&amp;")
        .replace("<", "&lt;")
        .replace(">", "&gt;")
        .replace('"', "&quot;")
        .replace("'", "&apos;")
    )