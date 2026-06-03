import asyncio
import base64
import os
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from typing import Optional

from loguru import logger
from google.auth.transport.requests import Request
from google.oauth2.credentials import Credentials
from googleapiclient.discovery import build

SCOPES = [
    'https://www.googleapis.com/auth/gmail.send',
    'https://www.googleapis.com/auth/gmail.readonly',
]
CREDENTIALS_FILE = os.getenv('GOOGLE_OAUTH_CLIENT_SECRETS', 'hotel-ai-voice-email.json')
TOKEN_FILE = os.getenv('GOOGLE_OAUTH_TOKEN_FILE', 'token.json')


def _load_credentials() -> Optional[Credentials]:
    creds = None
    if os.path.exists(TOKEN_FILE):
        try:
            creds = Credentials.from_authorized_user_file(TOKEN_FILE, SCOPES)
        except Exception as e:
            logger.warning(f"Unable to load Gmail token file: {e}")
            creds = None

    if creds and creds.valid:
        return creds

    if creds and creds.expired and creds.refresh_token:
        try:
            creds.refresh(Request())
            with open(TOKEN_FILE, 'w', encoding='utf-8') as token:
                token.write(creds.to_json())
            return creds
        except Exception as e:
            logger.warning(f"Failed to refresh Gmail credentials: {e}")
            return None

    logger.error(
        "Gmail credentials are not valid. Run reindex.py to authorize and create token.json."
    )
    return None


def send_email(to_email: str, subject: str, body: str) -> bool:
    creds = _load_credentials()
    if not creds:
        raise RuntimeError(
            "Google credentials unavailable. Run reindex.py and authorize Gmail access."
        )

    if not os.path.exists(CREDENTIALS_FILE):
        raise FileNotFoundError(
            f"Missing Gmail client secret file: {CREDENTIALS_FILE}"
        )

    service = build('gmail', 'v1', credentials=creds, cache_discovery=False)
    message = MIMEMultipart('alternative')
    message['to'] = to_email
    message['subject'] = subject

    plain = MIMEText(body, 'plain', 'utf-8')
    html = MIMEText(
        '<html><body>'
        + '<p>' + body.replace('\n', '</p><p>') + '</p>'
        + '</body></html>',
        'html',
        'utf-8'
    )
    message.attach(plain)
    message.attach(html)

    raw = base64.urlsafe_b64encode(message.as_bytes()).decode()

    service.users().messages().send(userId='me', body={'raw': raw}).execute()
    logger.info(f"📧 Email sent to {to_email} with subject: {subject}")
    return True


async def send_email_async(to_email: str, subject: str, body: str) -> bool:
    try:
        return await asyncio.to_thread(send_email, to_email, subject, body)
    except Exception as e:
        logger.error(f"send_email_async failed: {e}")
        return False
