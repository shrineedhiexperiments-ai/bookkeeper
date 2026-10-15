"""Bill lifecycle: ingest -> extract -> validate -> approve -> post to ERP.

    received ──extract──> needs_review ──(fix & submit)──┐
        │                                               v
        └──────────(clean)────────────────────> pending_approval ──reject──> rejected
                                                        │ (all approvers)
                                                        v
                                   approved ──post──> posted | post_failed ──retry──┘
"""

import hashlib
import logging
import os
import re
import uuid
from datetime import date
from typing import Callable

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from . import notify
from .config import get_settings
from .erp import Attachment, ERPBill, ERPConnector, ERPError, ERPLine, get_connector
from .extraction import SUPPORTED_TYPES, ExtractedBill, ExtractionError, extract_bill
from .models import AuditEvent, Bill, BillLine, BillStatus, VendorRule

log = logging.getLogger("bookkeeper.workflow")

SYSTEM_ACTOR = "system"
AMOUNT_TOLERANCE = 0.05  # rounding slack when reconciling line totals

Extractor = Callable[..., ExtractedBill]


class WorkflowError(Exception):
    pass


def _event(bill: Bill, actor: str, action: str, detail: str | None = None) -> None:
    bill.events.append(AuditEvent(actor=actor, action=action, detail=detail))


def _vendor_key(name: str) -> str:
    return re.sub(r"[^a-z0-9]", "", name.lower())


def _parse_date(value: str | None) -> date | None:
    if not value:
        return None
    try:
        return date.fromisoformat(value)
    except ValueError:
        return None


# --------------------------------------------------------------------------- ingest


def ingest(
    db: Session,
    *,
    content: bytes,
    file_name: str,
    mime_type: str,
    source: str,
    source_ref: str | None = None,
    extractor: Extractor = extract_bill,
    connector: ERPConnector | None = None,
) -> Bill:
    """Store a new bill document, read it, and push it into the workflow."""
    if mime_type not in SUPPORTED_TYPES:
        raise WorkflowError(f"Unsupported file type {mime_type}; send a PDF or image")

    upload_dir = get_settings().upload_dir
    os.makedirs(upload_dir, exist_ok=True)
    safe_name = re.sub(r"[^\w.\-]", "_", file_name)[-100:] or "bill"
    path = os.path.join(upload_dir, f"{uuid.uuid4().hex}_{safe_name}")
    with open(path, "wb") as fh:
        fh.write(content)

    bill = Bill(
        source=source,
        source_ref=source_ref,
        file_path=path,
        file_name=file_name,
        mime_type=mime_type,
        file_sha256=hashlib.sha256(content).hexdigest(),
        review_reasons=[],
    )
    _event(bill, source_ref or SYSTEM_ACTOR, "received", f"via {source}: {file_name}")
    db.add(bill)
    db.flush()

    connector = connector or _safe_connector()
    run_extraction(db, bill, content, extractor=extractor, connector=connector)
    db.commit()
    return bill


def _safe_connector() -> ERPConnector | None:
    try:
        return get_connector()
    except ERPError as exc:
        log.warning("ERP not available for account lookup: %s", exc)
        return None


def run_extraction(
    db: Session,
    bill: Bill,
    content: bytes,
    *,
    extractor: Extractor = extract_bill,
    connector: ERPConnector | None = None,
) -> None:
    accounts: list[str] = []
    if connector:
        try:
            accounts = connector.list_expense_accounts()
        except ERPError as exc:
            log.warning("Could not load chart of accounts: %s", exc)

    try:
        extracted = extractor(content, bill.mime_type, accounts)
    except ExtractionError as exc:
        bill.status = BillStatus.NEEDS_REVIEW
        bill.review_reasons = [f"Automatic extraction failed: {exc}"]
        _event(bill, SYSTEM_ACTOR, "extraction_failed", str(exc))
        return

    _apply_extraction(db, bill, extracted)
    _event(bill, SYSTEM_ACTOR, "extracted",
           f"{bill.vendor_name or '?'} #{bill.bill_number or '?'} {bill.currency or ''} {bill.total if bill.total is not None else '?'}")

    reasons = validate(db, bill)
    if not extracted.is_bill:
        reasons.insert(0, "Document does not look like a vendor bill")
    reasons += [f"Low confidence: {f}" for f in extracted.uncertain_fields]
    if reasons:
        bill.status = BillStatus.NEEDS_REVIEW
        bill.review_reasons = reasons
        _event(bill, SYSTEM_ACTOR, "needs_review", "; ".join(reasons))
    else:
        submit_for_approval(db, bill, SYSTEM_ACTOR)


def _apply_extraction(db: Session, bill: Bill, ex: ExtractedBill) -> None:
    bill.extraction_raw = ex.model_dump()
    bill.vendor_name = ex.vendor_name
    bill.vendor_tax_id = ex.vendor_tax_id
    bill.bill_number = ex.bill_number
    bill.bill_date = _parse_date(ex.bill_date)
    bill.due_date = _parse_date(ex.due_date)
    bill.currency = (ex.currency or "").upper()[:3] or None
    bill.subtotal = ex.subtotal
    bill.tax_total = ex.tax_total
    bill.total = ex.total

    # A vendor's learned account beats the model's guess.
    learned = None
    if ex.vendor_name:
        rule = db.scalar(select(VendorRule).where(VendorRule.vendor_key == _vendor_key(ex.vendor_name)))
        learned = rule.default_account if rule else None

    bill.lines = [
        BillLine(
            position=i,
            description=l.description,
            quantity=l.quantity,
            unit_price=l.unit_price,
            amount=l.amount,
            tax_rate=l.tax_rate,
            account_name=learned or l.account_name,
        )
        for i, l in enumerate(ex.lines)
    ]


def validate(db: Session, bill: Bill) -> list[str]:
    """Business checks that must pass before a bill can enter approval."""
    reasons = []
    for field, label in [("vendor_name", "vendor"), ("bill_number", "bill number"),
                         ("bill_date", "bill date"), ("total", "total")]:
        if not getattr(bill, field):
            reasons.append(f"Missing {label}")
    if not bill.lines:
        reasons.append("No line items")

    if bill.lines and bill.total is not None:
        lines_total = sum(l.amount for l in bill.lines)
        tax = bill.tax_total or 0
        if abs(lines_total + tax - bill.total) > max(AMOUNT_TOLERANCE, 0.005 * bill.total):
            reasons.append(f"Lines ({lines_total:.2f}) + tax ({tax:.2f}) do not add up to total ({bill.total:.2f})")
    for l in bill.lines:
        if abs(l.quantity * l.unit_price - l.amount) > AMOUNT_TOLERANCE:
            reasons.append(f"Line '{l.description[:40]}': qty x price != amount")

    dupe_q = select(Bill.id).where(Bill.id != bill.id, Bill.status != BillStatus.REJECTED)
    if db.scalar(dupe_q.where(Bill.file_sha256 == bill.file_sha256)):
        reasons.append("Same file was already received")
    elif bill.vendor_name and bill.bill_number and db.scalar(
        dupe_q.where(
            func.lower(Bill.vendor_name) == bill.vendor_name.lower(),
            func.lower(Bill.bill_number) == bill.bill_number.lower(),
        )
    ):
        reasons.append("Possible duplicate: same vendor and bill number already exists")
    return reasons


# --------------------------------------------------------------------------- approval


def approvers_for(total: float) -> list[str]:
    for tier in get_settings().approval_tiers:
        if tier.max_amount is None or total <= tier.max_amount:
            return tier.approvers
    raise WorkflowError(f"No approval tier covers amount {total}")


def submit_for_approval(db: Session, bill: Bill, actor: str, *, connector: ERPConnector | None = None) -> None:
    if bill.status not in (BillStatus.RECEIVED, BillStatus.NEEDS_REVIEW):
        raise WorkflowError(f"Cannot submit a bill in status {bill.status.value}")
    bill.approvers = list(approvers_for(bill.total or 0))
    bill.current_step = 0
    bill.review_reasons = []
    bill.status = BillStatus.PENDING_APPROVAL
    _event(bill, actor, "submitted", f"approvers: {', '.join(bill.approvers) or 'auto-approve'}")
    if not bill.approvers:
        _mark_approved(db, bill, SYSTEM_ACTOR, "auto-approved under threshold", connector)
    else:
        db.flush()
        notify.request_approval(bill)


def approve(db: Session, bill: Bill, actor: str, *, connector: ERPConnector | None = None) -> None:
    if bill.status != BillStatus.PENDING_APPROVAL:
        raise WorkflowError(f"Bill is {bill.status.value}, not awaiting approval")
    if actor.lower() != (bill.pending_approver or "").lower():
        raise WorkflowError(f"Waiting on {bill.pending_approver}, not {actor}")
    _event(bill, actor, "approved", f"step {bill.current_step + 1}/{len(bill.approvers)}")
    bill.current_step += 1
    if bill.current_step >= len(bill.approvers):
        _mark_approved(db, bill, actor, "all approvals received", connector)
    else:
        db.flush()
        notify.request_approval(bill)


def reject(db: Session, bill: Bill, actor: str, reason: str) -> None:
    if bill.status not in (BillStatus.PENDING_APPROVAL, BillStatus.NEEDS_REVIEW):
        raise WorkflowError(f"Cannot reject a bill in status {bill.status.value}")
    if bill.status == BillStatus.PENDING_APPROVAL and actor.lower() != (bill.pending_approver or "").lower():
        raise WorkflowError(f"Waiting on {bill.pending_approver}, not {actor}")
    bill.status = BillStatus.REJECTED
    _event(bill, actor, "rejected", reason or None)


def _mark_approved(db: Session, bill: Bill, actor: str, detail: str, connector: ERPConnector | None) -> None:
    bill.status = BillStatus.APPROVED
    _event(bill, actor, "approved_final", detail)
    db.flush()
    post_to_erp(db, bill, connector=connector)


# --------------------------------------------------------------------------- posting


def post_to_erp(db: Session, bill: Bill, *, connector: ERPConnector | None = None) -> None:
    if bill.status not in (BillStatus.APPROVED, BillStatus.POST_FAILED):
        raise WorkflowError(f"Only approved bills can be posted (status {bill.status.value})")
    try:
        connector = connector or get_connector()
        with open(bill.file_path, "rb") as fh:
            attachment = Attachment(bill.file_name, fh.read(), bill.mime_type)
        result = connector.post_bill(
            ERPBill(
                vendor_name=bill.vendor_name,
                vendor_tax_id=bill.vendor_tax_id,
                bill_number=bill.bill_number,
                bill_date=bill.bill_date,
                due_date=bill.due_date,
                currency=bill.currency,
                notes=bill.notes,
                reference=f"BK-{bill.id}",
                lines=[ERPLine(l.description, l.quantity, l.unit_price, l.account_name, l.tax_rate) for l in bill.lines],
            ),
            attachment,
        )
    except (ERPError, OSError) as exc:
        bill.status = BillStatus.POST_FAILED
        bill.erp_error = str(exc)
        _event(bill, SYSTEM_ACTOR, "post_failed", str(exc))
        return

    bill.status = BillStatus.POSTED
    bill.erp = connector.name
    bill.erp_bill_id = result.erp_bill_id
    bill.erp_error = "; ".join(result.warnings) or None
    detail = f"{connector.name} bill {result.erp_bill_id}"
    if result.already_existed:
        detail += " (already existed in ERP — linked, not re-created)"
    _event(bill, SYSTEM_ACTOR, "posted", detail)
    _learn_vendor_account(db, bill)


def _learn_vendor_account(db: Session, bill: Bill) -> None:
    accounts = {l.account_name for l in bill.lines if l.account_name}
    if not bill.vendor_name or len(accounts) != 1:
        return  # only learn when a vendor's bill maps cleanly to one account
    key = _vendor_key(bill.vendor_name)
    rule = db.scalar(select(VendorRule).where(VendorRule.vendor_key == key))
    if rule is None:
        rule = VendorRule(vendor_key=key)
        db.add(rule)
    rule.default_account = accounts.pop()
