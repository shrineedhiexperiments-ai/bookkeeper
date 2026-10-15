"""Approval emails with signed one-click approve/reject links."""

import hashlib
import hmac
import logging
import smtplib
from email.message import EmailMessage

from .config import get_settings

log = logging.getLogger("bookkeeper.notify")


def sign(bill_id: int, step: int, approver: str, action: str) -> str:
    msg = f"{bill_id}:{step}:{approver.lower()}:{action}".encode()
    return hmac.new(get_settings().secret_key.encode(), msg, hashlib.sha256).hexdigest()


def verify(bill_id: int, step: int, approver: str, action: str, token: str) -> bool:
    return hmac.compare_digest(sign(bill_id, step, approver, action), token)


def action_link(bill_id: int, step: int, approver: str, action: str) -> str:
    s = get_settings()
    token = sign(bill_id, step, approver, action)
    return f"{s.app_base_url}/bills/{bill_id}/email-action?action={action}&step={step}&approver={approver}&token={token}"


def send_email(to: str, subject: str, body: str) -> None:
    smtp = get_settings().smtp
    if not smtp.get("host"):
        log.info("EMAIL (SMTP not configured) to=%s subject=%s\n%s", to, subject, body)
        return
    msg = EmailMessage()
    msg["From"] = smtp["from"] or smtp["user"]
    msg["To"] = to
    msg["Subject"] = subject
    msg.set_content(body)
    with smtplib.SMTP(smtp["host"], smtp["port"]) as server:
        server.starttls()
        if smtp.get("user"):
            server.login(smtp["user"], smtp["password"])
        server.send_message(msg)


def request_approval(bill) -> None:
    approver = bill.pending_approver
    if not approver:
        return
    s = get_settings()
    body = (
        f"A bill needs your approval (step {bill.current_step + 1} of {len(bill.approvers)}).\n\n"
        f"Vendor:   {bill.vendor_name}\n"
        f"Bill no.: {bill.bill_number}\n"
        f"Date:     {bill.bill_date}\n"
        f"Total:    {bill.currency or ''} {bill.total:,.2f}\n\n"
        f"Review:   {s.app_base_url}/bills/{bill.id}\n"
        f"Approve:  {action_link(bill.id, bill.current_step, approver, 'approve')}\n"
        f"Reject:   {action_link(bill.id, bill.current_step, approver, 'reject')}\n"
    )
    try:
        send_email(approver, f"Approve bill: {bill.vendor_name} {bill.currency or ''} {bill.total:,.2f}", body)
    except Exception:  # never let a mail outage block the workflow
        log.exception("Failed to send approval email for bill %s", bill.id)
