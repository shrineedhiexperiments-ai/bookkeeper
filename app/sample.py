"""Sample data so a new workspace can be explored before real bills arrive.

Sample bills are marked source="sample" and can be removed in one go without touching real bills.
Vendors are fictional; each bill gets a small generated PDF invoice.
"""

import hashlib
import os
from datetime import date, datetime, timedelta, timezone

from sqlalchemy import select
from sqlalchemy.orm import Session

from .config import get_settings
from .models import AuditEvent, Bill, BillLine, BillStatus, VendorRule
from .workflow import _vendor_key

SOURCE = "sample"

# vendor, tax id, account, [(description, qty, unit price)], tax %, days ago billed, days until due, status
BILLS = [
    ("Northwind Cloud", "US-84-2210931", "Software Subscriptions", [("Compute and storage, September", 1, 2840.00), ("Managed database", 1, 610.00)], 0, 18, 12, "pending"),
    ("Brightline Design Studio", "US-47-1180042", "Professional Fees", [("Brand refresh, phase 2", 1, 6400.00)], 0, 9, 21, "pending2"),
    ("Harbor Office Space", "US-91-5527310", "Rent", [("Coworking membership, 12 seats", 12, 450.00)], 0, 31, -3, "pending"),
    ("Pinecrest Legal LLP", "US-30-7781205", "Legal Fees", [("Contract review", 6, 325.00), ("Incorporation filings", 1, 480.00)], 0, 44, 2, "posted"),
    ("Atlas Courier", "US-22-6640018", "Postage & Delivery", [("Same-day deliveries", 14, 18.50)], 8.25, 26, 4, "posted"),
    ("Lumen Electric Co", "US-56-3302917", "Utilities", [("Electricity, September", 1, 742.18)], 0, 22, 9, "posted"),
    ("Northwind Cloud", "US-84-2210931", "Software Subscriptions", [("Compute and storage, August", 1, 2655.40), ("Managed database", 1, 610.00)], 0, 49, -19, "posted"),
    ("Sparrow Coffee Co", None, "Meals & Entertainment", [("Office coffee subscription", 1, 189.00)], 8.25, 5, 25, "auto"),
    ("Fieldstone Insurance", "US-61-9902144", "Insurance", [("General liability, Q4", 1, 1875.00)], 0, 12, 18, "failed"),
    ("Quill & Ledger Supplies", "US-19-4471906", "Office Supplies", [("Printer paper, 10 cases", 10, 38.90), ("Toner cartridges", 4, 64.00)], 8.25, 2, 28, "draft"),
    ("Brightline Design Studio", "US-47-1180042", "Professional Fees", [("Brand refresh, phase 1", 1, 5200.00)], 0, 63, -33, "posted"),
    ("Metro Transit Fleet", None, None, [("Vehicle lease, October", 2, 690.00)], 0, 1, -1, "draft_issue"),
]


def _pdf(lines: list[str]) -> bytes:
    """A minimal one-page PDF with plain text (no dependencies)."""
    def esc(t: str) -> str:
        return t.replace("\\", "\\\\").replace("(", "\\(").replace(")", "\\)")
    ops, y = [], 770
    for i, t in enumerate(lines):
        size = 22 if i == 0 else 11
        ops.append(f"BT /F1 {size} Tf 56 {y} Td ({esc(t)}) Tj ET")
        y -= 34 if i == 0 else 18
    stream = "\n".join(ops).encode("latin-1", "replace")
    objs = [b"<< /Type /Catalog /Pages 2 0 R >>", b"<< /Type /Pages /Kids [3 0 R] /Count 1 >>",
            b"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 612 792] /Contents 4 0 R /Resources << /Font << /F1 5 0 R >> >> >>",
            b"<< /Length %d >>\nstream\n" % len(stream) + stream + b"\nendstream",
            b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>"]
    out, offs = bytearray(b"%PDF-1.4\n"), []
    for i, o in enumerate(objs, 1):
        offs.append(len(out))
        out += b"%d 0 obj\n" % i + o + b"\nendobj\n"
    xref = len(out)
    out += b"xref\n0 %d\n0000000000 65535 f \n" % (len(objs) + 1) + b"".join(b"%010d 00000 n \n" % o for o in offs)
    out += b"trailer\n<< /Size %d /Root 1 0 R >>\nstartxref\n%d\n%%%%EOF\n" % (len(objs) + 1, xref)
    return bytes(out)


def has_sample(db: Session) -> bool:
    return db.scalar(select(Bill.id).where(Bill.source == SOURCE).limit(1)) is not None


def load(db: Session, user: str) -> int:
    if has_sample(db):
        return 0
    s = get_settings()
    os.makedirs(s.upload_dir, exist_ok=True)
    tiers = s.approval_tiers
    now = datetime.now(timezone.utc)
    today = date.today()
    count = 0
    for n, (vendor, tax_id, account, items, tax, ago, due_in, kind) in enumerate(BILLS, 1):
        number = f"{''.join(w[0] for w in vendor.split()[:2]).upper()}-{4100 + n * 7}"
        bill_date = today - timedelta(days=ago)
        due = bill_date + timedelta(days=ago + due_in) if due_in is not None else None
        subtotal = round(sum(q * p for _, q, p in items), 2)
        tax_total = round(subtotal * tax / 100, 2)
        total = round(subtotal + tax_total, 2)
        text = [f"INVOICE  {number}", vendor, f"Tax ID: {tax_id or 'n/a'}", "", f"Bill to: Your Company Inc.",
                f"Invoice date: {bill_date:%b %d, %Y}", f"Due date: {due:%b %d, %Y}" if due else "Due: on receipt", "",
                "Description                                   Qty      Unit price      Amount"]
        text += [f"{d[:40]:<44}{q:>5}{p:>14,.2f}{q * p:>14,.2f}" for d, q, p in items]
        text += ["", f"Subtotal: {subtotal:,.2f}", f"Tax ({tax}%): {tax_total:,.2f}", f"TOTAL DUE (USD): {total:,.2f}",
                 "", "Sample invoice generated by Bookkeeper. Not a real bill."]
        content = _pdf(text)
        path = os.path.join(s.upload_dir, f"sample_{n:02d}_{number}.pdf")
        with open(path, "wb") as fh:
            fh.write(content)

        approvers = list(next((t.approvers for t in tiers if t.max_amount is None or total <= t.max_amount), []))
        if kind in ("pending", "pending2"):
            # Put the signed-in user in the chain so "waiting on your approval" has something in it.
            approvers = (approvers or ["controller@example.com"])
            approvers = ([user] + [a for a in approvers if a != user]) if kind == "pending" else (approvers[:1] + [user])
        created = now - timedelta(days=ago, hours=n)
        b = Bill(source=SOURCE, source_ref="sample data", file_path=path, file_name=f"{number}.pdf", mime_type="application/pdf",
                 file_sha256=hashlib.sha256(content).hexdigest(), vendor_name=vendor, vendor_tax_id=tax_id, bill_number=number,
                 bill_date=bill_date, due_date=due, currency="USD", subtotal=subtotal, tax_total=tax_total, total=total,
                 review_reasons=[], approvers=approvers, current_step=0, created_at=created, updated_at=created)
        b.lines = [BillLine(position=i, description=d, quantity=q, unit_price=p, amount=round(q * p, 2),
                            tax_rate=tax or None, account_name=account) for i, (d, q, p) in enumerate(items)]
        ev = lambda actor, action, detail=None, h=0: b.events.append(  # noqa: E731
            AuditEvent(actor=actor, action=action, detail=detail, at=created + timedelta(hours=h)))
        ev("system", "received", f"via upload: {number}.pdf")
        ev("system", "extracted", f"{vendor} #{number} USD {total:,.2f}", 0.01)

        if kind == "draft":
            b.status = BillStatus.NEEDS_REVIEW
            b.review_reasons = ["Low confidence: due_date"]
            b.approvers = []
            ev("system", "needs_review", "Low confidence: due_date", 0.02)
        elif kind == "draft_issue":
            b.status = BillStatus.NEEDS_REVIEW
            b.bill_number = None
            b.review_reasons = ["Missing bill number"]
            b.approvers = []
            ev("system", "needs_review", "Missing bill number", 0.02)
        else:
            ev(user, "submitted", f"approvers: {', '.join(approvers) or 'auto-approve'}", 1)
            if kind == "pending":
                b.status = BillStatus.PENDING_APPROVAL
            elif kind == "pending2":
                b.status, b.current_step = BillStatus.PENDING_APPROVAL, 1
                ev(approvers[0], "approved", "step 1", 5)
            else:
                for i, a in enumerate(approvers):
                    ev(a, "approved", f"step {i + 1}", 3 + i * 6)
                if not approvers:
                    ev("system", "approved", "auto-approved under threshold", 1.01)
                b.current_step = len(approvers)
                if kind == "failed":
                    b.status, b.erp, b.erp_error = BillStatus.POST_FAILED, "zoho_books", "Zoho POST /bills: Account 'Insurance' not found (code 1002)"
                    ev("system", "post_failed", b.erp_error, 14)
                else:
                    b.status, b.erp, b.erp_bill_id = BillStatus.POSTED, "zoho_books", f"ZB-{460000 + n * 13}"
                    ev("system", "posted", f"zoho_books bill {b.erp_bill_id}", 14)
                    b.updated_at = created + timedelta(hours=14)
        db.add(b)
        if b.status == BillStatus.POSTED and account:
            # What the app learns after a real post: the vendor's usual account.
            key = _vendor_key(vendor)
            if not db.scalar(select(VendorRule).where(VendorRule.vendor_key == key)):
                db.add(VendorRule(vendor_key=key, default_account=account))
                db.flush()
        count += 1
    return count


def remove(db: Session) -> int:
    bills = list(db.scalars(select(Bill).where(Bill.source == SOURCE)))
    sample_vendors = {_vendor_key(v) for v, *_ in BILLS}
    real_vendors = {_vendor_key(b.vendor_name) for b in db.scalars(select(Bill).where(Bill.source != SOURCE, Bill.vendor_name.is_not(None)))}
    for rule in db.scalars(select(VendorRule).where(VendorRule.vendor_key.in_(sample_vendors - real_vendors))):
        db.delete(rule)
    for b in bills:
        try:
            os.remove(b.file_path)
        except OSError:
            pass
        db.delete(b)
    return len(bills)
