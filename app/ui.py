"""Presentation helpers for the web UI: number formatting, queue names, approval-route steps.

Kept free of FastAPI/SQLAlchemy imports so templates can be rendered on their own (previews, emails).
"""

from datetime import date

# Queues shown in the left rail, in order. Keys are the ?status= values the list route understands.
QUEUES = [
    ("mine", "Waiting on you"),
    ("needs_review", "Needs review"),
    ("pending_approval", "In approval"),
    ("post_failed", "Post failed"),
    ("posted", "Posted"),
    ("rejected", "Rejected"),
    (None, "All bills"),
]

QUEUE_TITLES = {k: v for k, v in QUEUES} | {"received": "Received", "approved": "Approved"}

EMPTY_STATES = {
    "mine": "Nothing is waiting on you. Bills that need your approval will show up here.",
    "needs_review": "No bills need review. Anything the reader wasn't sure about lands here.",
    "pending_approval": "No bills are in approval right now.",
    "post_failed": "Every approved bill reached the books.",
    "posted": "No bills have been booked yet.",
    "rejected": "No rejected bills.",
    None: "No bills yet. Drop a PDF or photo of a bill above, or email it to the bills inbox.",
}

ACTION_LABELS = {
    "received": "Received",
    "extracted": "Read the bill",
    "extraction_failed": "Couldn't read the bill",
    "needs_review": "Flagged for review",
    "edited": "Edited",
    "submitted": "Submitted for approval",
    "approved": "Approved",
    "approved_final": "Fully approved",
    "rejected": "Rejected",
    "posted": "Booked",
    "post_failed": "Booking failed",
    "retry_post": "Retried booking",
}

ACTIVE_STATUSES = {"received", "needs_review", "pending_approval", "approved", "post_failed"}


def _group_indian(whole: str) -> str:
    """12345678 -> 1,23,45,678"""
    if len(whole) <= 3:
        return whole
    head, tail = whole[:-3], whole[-3:]
    parts = []
    while len(head) > 2:
        parts.insert(0, head[-2:])
        head = head[:-2]
    if head:
        parts.insert(0, head)
    return ",".join(parts) + "," + tail


def money(value, currency: str | None = None) -> str:
    """Format an amount with 2 decimals; INR uses lakh/crore grouping."""
    if value is None:
        return "—"
    value = float(value)
    sign = "−" if value < 0 else ""
    if (currency or "").upper() == "INR":
        whole, frac = f"{abs(value):.2f}".split(".")
        return f"{sign}{_group_indian(whole)}.{frac}"
    return f"{sign}{abs(value):,.2f}"


def plain(value, places: int = 4) -> str:
    """Number for an input box: no grouping, no trailing zeros (3068.0 -> 3068, 0.125 -> 0.125)."""
    if value is None or value == "":
        return ""
    text = f"{float(value):.{places}f}".rstrip("0").rstrip(".")
    return "0" if text in ("-0", "") else text


def status_label(status) -> str:
    value = getattr(status, "value", status) or ""
    return value.replace("_", " ")


def short_person(email_addr: str | None) -> str:
    """manager@acme.test -> manager"""
    if not email_addr:
        return ""
    return email_addr.split("@", 1)[0]


def is_overdue(bill, today: date | None = None) -> bool:
    status = getattr(bill.status, "value", bill.status)
    return bool(bill.due_date and status in ACTIVE_STATUSES and bill.due_date < (today or date.today()))


def route_steps(bill) -> list[dict]:
    """The approval route as a list of slots for the routing-slip display.

    Each slot: {who, role, state} where state is one of
    done | rejected | waiting | upcoming | auto | posted | failed | idle.
    """
    status = getattr(bill.status, "value", bill.status)
    approvers = list(bill.approvers or [])
    submitted = status not in ("received", "needs_review")
    steps = []

    if submitted and not approvers and status != "rejected":
        steps.append({"who": "Under the approval limit", "role": "Approval", "state": "auto"})
    for i, who in enumerate(approvers):
        if status == "rejected" and i == bill.current_step:
            state = "rejected"
        elif i < bill.current_step or status in ("approved", "posted", "post_failed"):
            state = "done"
        elif status == "pending_approval" and i == bill.current_step:
            state = "waiting"
        else:
            state = "upcoming"
        steps.append({"who": who, "role": f"Approver {i + 1}", "state": state})

    erp_state = {"posted": "posted", "post_failed": "failed"}.get(status, "idle")
    steps.append({"who": bill.erp_bill_id or "", "role": "Books", "state": erp_state})
    return steps


def install(env) -> None:
    """Register filters and globals on a Jinja environment."""
    env.filters["money"] = money
    env.filters["plain"] = plain
    env.filters["status_label"] = status_label
    env.filters["person"] = short_person
    env.globals.update(
        QUEUES=QUEUES, QUEUE_TITLES=QUEUE_TITLES, EMPTY_STATES=EMPTY_STATES,
        ACTION_LABELS=ACTION_LABELS, is_overdue=is_overdue, route_steps=route_steps, today=date.today,
    )
