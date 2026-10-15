"""Poll a dedicated inbox (e.g. bills@yourcompany.com) and ingest every PDF/image attachment."""

import email
import imaplib
import logging
import mimetypes
import threading
from email.message import Message
from email.utils import parseaddr

from .config import get_settings
from .db import SessionLocal
from .extraction import SUPPORTED_TYPES
from .workflow import WorkflowError, ingest

log = logging.getLogger("bookkeeper.mailbox")


def bill_attachments(msg: Message) -> list[tuple[str, bytes, str]]:
    """(file_name, content, mime_type) for each supported attachment in an email."""
    found = []
    for part in msg.walk():
        if part.is_multipart():
            continue
        name = part.get_filename()
        mime = part.get_content_type()
        if mime == "application/octet-stream" and name:
            mime = mimetypes.guess_type(name)[0] or mime
        if mime not in SUPPORTED_TYPES:
            continue
        # Skip inline images such as email signatures/logos.
        if mime.startswith("image/") and part.get_content_disposition() != "attachment":
            continue
        payload = part.get_payload(decode=True)
        if payload:
            found.append((name or f"bill{mimetypes.guess_extension(mime) or ''}", payload, mime))
    return found


def process_message(raw: bytes) -> int:
    msg = email.message_from_bytes(raw)
    sender = parseaddr(msg.get("From", ""))[1] or "unknown"
    ref = f"{sender} {msg.get('Message-ID', '')}".strip()
    count = 0
    with SessionLocal() as db:
        for name, content, mime in bill_attachments(msg):
            try:
                ingest(db, content=content, file_name=name, mime_type=mime, source="email", source_ref=ref)
                count += 1
            except WorkflowError as exc:
                db.rollback()
                log.warning("Skipped attachment %s from %s: %s", name, sender, exc)
    if count == 0:
        log.info("Email from %s had no bill attachments", sender)
    return count


def poll_once() -> int:
    cfg = get_settings().imap
    if not cfg.get("host"):
        return 0
    total = 0
    with imaplib.IMAP4_SSL(cfg["host"]) as imap:
        imap.login(cfg["user"], cfg["password"])
        imap.select(cfg["folder"])
        _, data = imap.search(None, "UNSEEN")
        for num in data[0].split():
            _, msg_data = imap.fetch(num, "(RFC822)")  # marks the message \Seen
            try:
                total += process_message(msg_data[0][1])
            except Exception:
                log.exception("Failed to process email %s; leaving it unread for retry", num)
                imap.store(num, "-FLAGS", "\\Seen")
    return total


def start_poller(stop: threading.Event) -> threading.Thread | None:
    cfg = get_settings().imap
    if not cfg.get("host"):
        log.info("IMAP_HOST not set; email ingestion disabled")
        return None

    def loop():
        while not stop.is_set():
            try:
                n = poll_once()
                if n:
                    log.info("Ingested %d bill(s) from email", n)
            except Exception:
                log.exception("Mailbox poll failed")
            stop.wait(cfg["poll_seconds"])

    thread = threading.Thread(target=loop, name="mailbox-poller", daemon=True)
    thread.start()
    return thread
