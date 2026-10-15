"""Read-only aggregates for the Home dashboard and the Vendors pages."""

import os
import statistics
from collections import Counter, defaultdict
from datetime import date, datetime, timedelta, timezone

from sqlalchemy import select
from sqlalchemy.orm import Session

from . import integrations
from .config import get_settings
from .models import AuditEvent, Bill, BillStatus, VendorRule

OUTSTANDING = {BillStatus.RECEIVED, BillStatus.NEEDS_REVIEW, BillStatus.PENDING_APPROVAL,
               BillStatus.APPROVED, BillStatus.POST_FAILED}
BOOKED = {BillStatus.PENDING_APPROVAL, BillStatus.APPROVED, BillStatus.POSTED, BillStatus.POST_FAILED}


def vendor_key(name: str | None) -> str:
    return " ".join((name or "").lower().split())


def main_currency(bills) -> str:
    c = Counter(b.currency or "USD" for b in bills if b.total is not None)
    return c.most_common(1)[0][0] if c else "USD"


def _total(bills, currency: str) -> float:
    return round(sum(b.total for b in bills if b.total is not None and (b.currency or "USD") == currency), 2)


def _aware(dt: datetime) -> datetime:
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


def home(db: Session, user: str) -> dict:
    today = date.today()
    bills = list(db.scalars(select(Bill)))
    cur = main_currency(bills)
    out = [b for b in bills if b.status in OUTSTANDING]

    # Tasks for this user
    mine = [b for b in out if b.status == BillStatus.PENDING_APPROVAL and (b.pending_approver or "").lower() == user]
    drafts = sorted([b for b in out if b.status in (BillStatus.RECEIVED, BillStatus.NEEDS_REVIEW)], key=lambda b: b.created_at, reverse=True)
    failed = [b for b in out if b.status == BillStatus.POST_FAILED]

    # Cash-out forecast: outstanding bills by due week
    monday = today - timedelta(days=today.weekday())
    buckets = [{"label": "Overdue", "amount": 0.0, "n": 0, "tone": "warn"}]
    for w in range(6):
        start = monday + timedelta(weeks=w)
        buckets.append({"label": "This week" if w == 0 else start.strftime("%b %d"), "amount": 0.0, "n": 0, "tone": "",
                        "start": start, "end": start + timedelta(days=6)})
    later = {"label": "Later", "amount": 0.0, "n": 0, "tone": "muted"}
    undated = {"label": "No due date", "amount": 0.0, "n": 0, "tone": "muted"}
    for b in out:
        if b.total is None or (b.currency or "USD") != cur:
            continue
        if not b.due_date:
            slot = undated
        elif b.due_date < today:
            slot = buckets[0]
        else:
            slot = next((x for x in buckets[1:] if x["start"] <= b.due_date <= x["end"]), later)
        slot["amount"] += b.total
        slot["n"] += 1
    forecast = buckets + [later, undated]
    peak = max((x["amount"] for x in forecast), default=0) or 1

    # Spend by vendor (bills in or past approval, last 90 days)
    since = today - timedelta(days=90)
    spend = defaultdict(lambda: {"amount": 0.0, "n": 0, "name": ""})
    for b in bills:
        if b.status in BOOKED and b.total is not None and (b.currency or "USD") == cur and b.vendor_name \
                and (b.bill_date or b.created_at.date()) >= since:
            v = spend[vendor_key(b.vendor_name)]
            v["amount"] += b.total
            v["n"] += 1
            v["name"] = b.vendor_name
    top = sorted(spend.values(), key=lambda v: -v["amount"])[:6]

    # Approval turnaround: submitted -> final approval
    hours = []
    by_bill = defaultdict(list)
    for e in db.scalars(select(AuditEvent).where(AuditEvent.action.in_(["submitted", "approved"])).order_by(AuditEvent.id)):
        by_bill[e.bill_id].append(e)
    for evs in by_bill.values():
        sub = next((e for e in evs if e.action == "submitted"), None)
        appr = [e for e in evs if e.action == "approved"]
        if sub and appr:
            hours.append((_aware(appr[-1].at) - _aware(sub.at)).total_seconds() / 3600)

    first_of_month = today.replace(day=1)
    posted_month = [b for b in bills if b.status == BillStatus.POSTED and _aware(b.updated_at).date() >= first_of_month]
    due_week = [b for b in out if b.due_date and today <= b.due_date <= today + timedelta(days=7)]

    events = list(db.scalars(select(AuditEvent).where(AuditEvent.action.not_in(["extracted", "edited", "retry_post"]))
                             .order_by(AuditEvent.at.desc(), AuditEvent.id.desc()).limit(10)))

    return {
        "currency": cur, "mine": mine, "drafts": drafts, "failed": failed,
        "kpis": [
            {"label": "Outstanding", "value": _total(out, cur), "money": True, "sub": f"{len(out)} bill{'s' if len(out) != 1 else ''}", "href": "/bills?status=outstanding"},
            {"label": "Due in 7 days", "value": _total(due_week, cur), "money": True, "sub": f"{len(due_week)} bill{'s' if len(due_week) != 1 else ''}", "href": "/bills?due=soon"},
            {"label": "Posted this month", "value": _total(posted_month, cur), "money": True, "sub": f"{len(posted_month)} bill{'s' if len(posted_month) != 1 else ''}", "href": "/bills?status=posted"},
            {"label": "Approval time", "value": (f"{statistics.median(hours):.1f}h" if hours and statistics.median(hours) < 48 else f"{statistics.median(hours) / 24:.1f}d") if hours else "—",
             "money": False, "sub": "median, submitted to approved" if hours else "no approvals yet", "href": None},
        ],
        "forecast": forecast, "peak": peak, "top_vendors": top, "top_peak": (top[0]["amount"] if top else 0) or 1,
        "events": events, "setup": setup(db, bills), "has_real": any(b.source != "sample" for b in bills),
        "has_sample": any(b.source == "sample" for b in bills),
    }


def setup(db: Session, bills=None) -> list[dict]:
    s = get_settings()
    erp = integrations.load(db)
    key = os.environ.get("ANTHROPIC_API_KEY", "")
    rules_custom = not any(a.endswith("@example.com") for t in s.approval_tiers for a in t.approvers)
    bills = bills if bills is not None else list(db.scalars(select(Bill).limit(1)))
    return [
        {"done": len(key) > 20 and key.startswith("sk-ant-"), "label": "Add your Claude API key", "hint": "Set ANTHROPIC_API_KEY in .env so bills are read automatically.", "href": None},
        {"done": bool(erp.configured and erp.last_test and erp.last_test.get("ok")), "label": "Connect Zoho Books", "hint": "Approved bills post to your books with the document attached.", "href": "/settings/integrations/zoho_books"},
        {"done": rules_custom, "label": "Set your approval rules", "hint": "Replace the example approvers with your team.", "href": "/settings/approvals"},
        {"done": bool(s.imap.get("host")), "label": "Set up bill forwarding", "hint": "Let vendors email bills straight into Bookit.", "href": "/settings/email"},
        {"done": bool(bills), "label": "Upload your first bill", "hint": "Drag a PDF or photo anywhere in the app.", "href": None, "upload": True},
    ]


def vendors(db: Session) -> list[dict]:
    rules = {r.vendor_key: r.default_account for r in db.scalars(select(VendorRule))}
    groups: dict[str, list[Bill]] = defaultdict(list)
    for b in db.scalars(select(Bill).where(Bill.vendor_name.is_not(None), Bill.status != BillStatus.REJECTED)):
        groups[vendor_key(b.vendor_name)].append(b)
    rows = []
    for key, bs in groups.items():
        bs.sort(key=lambda b: b.created_at, reverse=True)
        cur = main_currency(bs)
        rows.append({
            "key": key, "name": bs[0].vendor_name, "n": len(bs), "currency": cur,
            "total": _total(bs, cur), "outstanding": _total([b for b in bs if b.status in OUTSTANDING], cur),
            "last": bs[0].bill_date or bs[0].created_at.date(), "tax_id": next((b.vendor_tax_id for b in bs if b.vendor_tax_id), None),
            "account": rules.get(workflow_key(bs[0].vendor_name)),
        })
    return sorted(rows, key=lambda r: -r["total"])


def workflow_key(name: str) -> str:
    from .workflow import _vendor_key

    return _vendor_key(name)


def vendor(db: Session, key: str) -> dict | None:
    bs = [b for b in db.scalars(select(Bill).where(Bill.vendor_name.is_not(None)).order_by(Bill.created_at.desc()))
          if vendor_key(b.vendor_name) == key]
    if not bs:
        return None
    cur = main_currency(bs)
    live = [b for b in bs if b.status != BillStatus.REJECTED]
    rule = db.scalar(select(VendorRule).where(VendorRule.vendor_key == workflow_key(bs[0].vendor_name)))
    # Monthly totals for the last 6 months
    today = date.today()
    months = []
    for i in range(5, -1, -1):
        y, m = today.year, today.month - i
        while m <= 0:
            y, m = y - 1, m + 12
        amt = sum(b.total for b in live if b.total is not None and (b.currency or "USD") == cur
                  and (b.bill_date or b.created_at.date()).year == y and (b.bill_date or b.created_at.date()).month == m)
        months.append({"label": date(y, m, 1).strftime("%b"), "amount": round(amt, 2)})
    return {
        "key": key, "name": bs[0].vendor_name, "bills": bs, "currency": cur, "n": len(live),
        "total": _total(live, cur), "outstanding": _total([b for b in live if b.status in OUTSTANDING], cur),
        "avg": round(_total(live, cur) / len(live), 2) if live else 0,
        "tax_id": next((b.vendor_tax_id for b in bs if b.vendor_tax_id), None),
        "account": rule.default_account if rule else None, "first": bs[-1].bill_date or bs[-1].created_at.date(),
        "months": months, "month_peak": max((m["amount"] for m in months), default=0) or 1,
    }
