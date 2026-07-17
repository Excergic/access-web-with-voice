"""
Gmail API rail — real execution of draft_reply and send_email.
Uses OAuth2 via credentials.json (Desktop app flow).
On first run, opens browser for auth and saves token.json for subsequent runs.
"""

import asyncio
import base64
import logging
import os
from email.mime.text import MIMEText
from functools import lru_cache
from pathlib import Path

from google.auth.transport.requests import Request
from google.oauth2.credentials import Credentials
from google_auth_oauthlib.flow import InstalledAppFlow
from googleapiclient.discovery import build
from googleapiclient.errors import HttpError

log = logging.getLogger(__name__)

SCOPES = [
    "https://www.googleapis.com/auth/gmail.compose",   # create drafts + send
    "https://www.googleapis.com/auth/gmail.readonly",  # read threads for context
]

_SERVER_DIR = Path(__file__).parent.parent.parent
CREDENTIALS_PATH = _SERVER_DIR / "credentials.json"
TOKEN_PATH       = _SERVER_DIR / "token.json"


def _get_credentials() -> Credentials:
    """Load or refresh OAuth2 credentials. Opens browser on first run."""
    creds: Credentials | None = None

    if TOKEN_PATH.exists():
        creds = Credentials.from_authorized_user_file(str(TOKEN_PATH), SCOPES)

    if not creds or not creds.valid:
        if creds and creds.expired and creds.refresh_token:
            creds.refresh(Request())
        else:
            if not CREDENTIALS_PATH.exists():
                raise FileNotFoundError(
                    f"credentials.json not found at {CREDENTIALS_PATH}. "
                    "Download it from Google Cloud Console → APIs & Services → Credentials."
                )
            flow = InstalledAppFlow.from_client_secrets_file(
                str(CREDENTIALS_PATH), SCOPES
            )
            creds = flow.run_local_server(port=0)

        TOKEN_PATH.write_text(creds.to_json())
        log.info("Gmail token saved to %s", TOKEN_PATH)

    return creds


@lru_cache(maxsize=1)
def _get_service():
    """Build and cache the Gmail API service client."""
    creds = _get_credentials()
    return build("gmail", "v1", credentials=creds)


def _get_user_email() -> str:
    """Return the authenticated user's email address."""
    profile = _get_service().users().getProfile(userId="me").execute()
    return profile["emailAddress"]


def _resolve_thread_id(subject: str) -> str:
    """
    The Gmail URL hash (e.g. FMfcgz...) uses a different encoding than the
    Gmail API thread ID (hex string). Reliably resolve the API thread ID by
    searching for the most recent message matching the subject.
    """
    service = _get_service()
    # Escape quotes in subject for the query
    q = f'subject:"{subject.strip()}"'
    results = service.users().messages().list(
        userId="me", q=q, maxResults=1
    ).execute()
    messages = results.get("messages", [])
    if not messages:
        raise ValueError(f"Could not find Gmail thread for subject: {subject!r}")
    # threadId on the message is the canonical API thread ID
    msg = service.users().messages().get(
        userId="me", id=messages[0]["id"], format="minimal"
    ).execute()
    thread_id = msg["threadId"]
    log.info("Resolved thread_id=%s for subject=%r", thread_id, subject)
    return thread_id


def _get_thread_latest_message(api_thread_id: str) -> dict:
    """Return the most recent message in a thread using the API thread ID."""
    thread = _get_service().users().threads().get(
        userId="me", id=api_thread_id, format="metadata",
        metadataHeaders=["From", "To", "Subject", "Message-ID", "References"],
    ).execute()
    messages = thread.get("messages", [])
    if not messages:
        raise ValueError(f"Thread {api_thread_id} has no messages")
    return messages[-1]  # most recent


def _extract_header(message: dict, name: str) -> str:
    headers = message.get("payload", {}).get("headers", [])
    for h in headers:
        if h["name"].lower() == name.lower():
            return h["value"]
    return ""


def _build_raw_reply(to: str, subject: str, body: str,
                     in_reply_to: str, references: str) -> str:
    """Build a base64url-encoded RFC 2822 reply message."""
    msg = MIMEText(body, "plain")
    msg["To"]          = to
    msg["Subject"]     = subject if subject.lower().startswith("re:") else f"Re: {subject}"
    msg["In-Reply-To"] = in_reply_to
    msg["References"]  = f"{references} {in_reply_to}".strip()
    return base64.urlsafe_b64encode(msg.as_bytes()).decode()


# ── Public API ────────────────────────────────────────────────────────────────

async def draft_reply(subject: str, body: str) -> dict:
    """
    Create a Gmail draft reply for the thread matching `subject`.
    Resolves the real API thread ID via Gmail search — avoids URL hash format issues.
    Returns {"draft_id": str, "message_id": str, "api_thread_id": str}.
    """
    def _run() -> dict:
        service   = _get_service()
        api_thread_id = _resolve_thread_id(subject)
        latest    = _get_thread_latest_message(api_thread_id)

        sender     = _extract_header(latest, "From")
        subj       = _extract_header(latest, "Subject")
        message_id = _extract_header(latest, "Message-ID")
        references = _extract_header(latest, "References")

        raw = _build_raw_reply(
            to=sender,
            subject=subj,
            body=body,
            in_reply_to=message_id,
            references=references,
        )

        draft = service.users().drafts().create(
            userId="me",
            body={"message": {"raw": raw, "threadId": api_thread_id}},
        ).execute()

        draft_id   = draft["id"]
        new_msg_id = draft["message"]["id"]
        log.info("Draft created | draft_id=%s msg_id=%s thread=%s",
                 draft_id, new_msg_id, api_thread_id)
        return {"draft_id": draft_id, "message_id": new_msg_id, "api_thread_id": api_thread_id}

    return await asyncio.get_event_loop().run_in_executor(None, _run)


async def send_draft(draft_id: str) -> dict:
    """
    Send an existing Gmail draft.
    Returns {"message_id": str}.
    """
    def _run() -> dict:
        service = _get_service()
        sent = service.users().drafts().send(
            userId="me",
            body={"id": draft_id},
        ).execute()
        msg_id = sent["id"]
        log.info("Email sent | message_id=%s", msg_id)
        return {"message_id": msg_id}

    return await asyncio.get_event_loop().run_in_executor(None, _run)
