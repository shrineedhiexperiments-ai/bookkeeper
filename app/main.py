import hashlib
import hmac
import logging
import os
import re
import threading
from contextlib import asynccontextmanager
from datetime import date, timedelta

from fastapi import Depends, FastAPI, File, Form, HTTPException, Request, UploadFile
from fastapi.responses import FileResponse, HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from . import insights, integrations, mailbox, notify, sample, search, workflow
from .config import get_settings
from .db import SessionLocal, get_db, init_db
from .erp import ERPError, build_connector, get_connector, set_connector
from .models import Bill, BillLine, BillStatus, VendorRule
from .workflow import WorkflowError

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")

TEMPLATES = Jinja2Templates(directory=os.path.join(os.path.dirname(__file__), "templates"))
EDITABLE = {BillStatus.RECEIVED, BillStatus.NEEDS_REVIEW}


def _nav_counts(user: str | None) -> dict:
    """Badge counts for the sidebar, available in every template."""
    if not user:
        return {}
    with SessionLocal() as db:
        pending = db.scalars(select(Bill).where(Bill.status == BillStatus.PENDING_APPROVAL)).all()
        return {
            "mine": sum(1 for b in pending if (b.pending_approver or "").lower() == user),
            "needs_review": db.scalar(select(func.count()).where(Bill.status == BillStatus.NEEDS_REVIEW)) or 0,
            "erp_ok": integrations.load(db).configured,
            "sample": sample.has_sample(db),
        }


TEMPLATES.env.globals["nav_counts"] = _nav_counts


def _friendly(reason: str) -> str:
    """Review reasons in plain language (raw API errors stay in the bill's activity log)."""
    r = reason or ""
    if r.startswith("Automatic extraction failed"):
        low = r.lower()
        if "credit balance" in low:
            return "Couldn't read this bill: the Claude API account is out of credits"
        if "401" in r or "x-api-key" in low:
            return "Couldn't read this bill: the Claude API key isn't valid"
        if "workspace" in low:
            return "Couldn't read this bill: the API key's workspace setting is wrong"
        if "rate limit" in low:
            return "Couldn't read this bill yet: Claude was busy. Try again shortly"
        return "Couldn't read this bill automatically. Enter the details by hand"
    if r.startswith("Low confidence: "):
        return "Double-check the " + r.removeprefix("Low confidence: ").replace("_", " ")
    return r


TEMPLATES.env.filters["friendly"] = _friendly
TEMPLATES.env.filters["erp_error"] = lambda e: re.sub(r"^Zoho (GET|POST|PUT|DELETE) \S+: ", "", e or "Posting failed")
EVENT_WORDS = {"received": "Received", "needs_review": "Flagged for review", "submitted": "Submitted for approval",
               "approved": "Approved", "rejected": "Rejected", "posted": "Posted to your books", "post_failed": "Posting failed",
               "extraction_failed": "Couldn't be read automatically", "auto_approved": "Auto-approved"}
TEMPLATES.env.filters["event"] = lambda a: EVENT_WORDS.get(a, a.replace("_", " ").capitalize())
TEMPLATES.env.filters["avatar_hue"] = lambda name: int(hashlib.md5((name or "").lower().encode()).hexdigest(), 16) % 360


@asynccontextmanager
async def lifespan(app: FastAPI):
    init_db()
    stop = threading.Event()
    mailbox.start_poller(stop)
    yield
    stop.set()


app = FastAPI(title="Bookkeeper", lifespan=lifespan)


# --------------------------------------------------------------------------- identity
# Lightweight sign-in: the user states their email and we keep it in a signed cookie.
# Swap for your SSO (Google / Microsoft / Zoho) before exposing this beyond a trusted network.


def _cookie_sig(email_addr: str) -> str:
    return hmac.new(get_settings().secret_key.encode(), email_addr.encode(), hashlib.sha256).hexdigest()


def current_user(request: Request) -> str | None:
    raw = request.cookies.get("bk_user", "")
    email_addr, _, sig = raw.rpartition("|")
    if email_addr and hmac.compare_digest(sig, _cookie_sig(email_addr)):
        return email_addr
    return None


def require_user(request: Request) -> str:
    user = current_user(request)
    if not user:
        raise HTTPException(status_code=303, headers={"Location": "/login"})
    return user


@app.get("/login", response_class=HTMLResponse)
def login_form(request: Request):
    return TEMPLATES.TemplateResponse(request, "login.html", {"user": None})


@app.post("/login")
def login(email_addr: str = Form(..., alias="email")):
    resp = RedirectResponse("/home", status_code=303)
    email_addr = email_addr.strip().lower()
    resp.set_cookie("bk_user", f"{email_addr}|{_cookie_sig(email_addr)}", httponly=True, samesite="lax")
    return resp


@app.get("/logout")
def logout():
    resp = RedirectResponse("/login", status_code=303)
    resp.delete_cookie("bk_user")
    return resp


# --------------------------------------------------------------------------- pages


def _get_bill(db: Session, bill_id: int) -> Bill:
    bill = db.get(Bill, bill_id)
    if not bill:
        raise HTTPException(404, "Bill not found")
    return bill


def toast(resp: RedirectResponse, message: str, kind: str = "ok") -> RedirectResponse:
    """Queue a one-time toast for the next page (read and cleared by the page's script)."""
    from urllib.parse import quote

    resp.set_cookie("bk_toast", quote(f"{kind}|{message}"), max_age=60, samesite="lax")
    return resp


def _redirect(bill_id: int, error: str | None = None) -> RedirectResponse:
    url = f"/bills/{bill_id}" + (f"?error={error}" if error else "")
    return RedirectResponse(url, status_code=303)


@app.get("/")
def root():
    return RedirectResponse("/home", status_code=303)


OUTSTANDING = {BillStatus.RECEIVED, BillStatus.NEEDS_REVIEW, BillStatus.PENDING_APPROVAL,
               BillStatus.APPROVED, BillStatus.POST_FAILED}
VIEWS = {  # tab / card name -> statuses it shows
    "inbox": {BillStatus.RECEIVED, BillStatus.NEEDS_REVIEW},
    "outstanding": OUTSTANDING,
}


def _sum(bills: list[Bill]) -> dict:
    """Total of bills in their most common currency (mixed currencies are flagged, not converted)."""
    by_cur: dict[str, float] = {}
    for b in bills:
        if b.total is not None:
            by_cur[b.currency or "USD"] = by_cur.get(b.currency or "USD", 0) + b.total
    if not by_cur:
        return {"amount": 0.0, "currency": "USD", "mixed": False}
    cur = max(by_cur, key=by_cur.get)
    return {"amount": by_cur[cur], "currency": cur, "mixed": len(by_cur) > 1}


@app.get("/home", response_class=HTMLResponse)
def home(request: Request, db: Session = Depends(get_db), user: str = Depends(require_user)):
    from datetime import datetime

    hour = datetime.now().hour
    greeting = "Good morning" if 5 <= hour < 12 else "Good afternoon" if hour < 17 else "Good evening"
    return TEMPLATES.TemplateResponse(request, "home.html", {"user": user, "greeting": greeting, "today": date.today(),
                                                            **insights.home(db, user)})


@app.post("/sample/load")
def sample_load(db: Session = Depends(get_db), user: str = Depends(require_user)):
    n = sample.load(db, user)
    db.commit()
    return toast(RedirectResponse("/home", status_code=303), f"Loaded {n} sample bills" if n else "Sample data is already loaded")


@app.post("/sample/remove")
def sample_remove(db: Session = Depends(get_db), user: str = Depends(require_user)):
    n = sample.remove(db)
    db.commit()
    return toast(RedirectResponse("/home", status_code=303), f"Removed {n} sample bills")


@app.get("/vendors", response_class=HTMLResponse)
def vendors_page(request: Request, q: str | None = None, db: Session = Depends(get_db), user: str = Depends(require_user)):
    rows = insights.vendors(db)
    if q:
        rows = [r for r in rows if q.lower() in r["name"].lower()]
    return TEMPLATES.TemplateResponse(request, "vendors.html", {"user": user, "rows": rows, "vq": q or ""})


@app.get("/vendors/{key:path}", response_class=HTMLResponse)
def vendor_page(request: Request, key: str, db: Session = Depends(get_db), user: str = Depends(require_user)):
    v = insights.vendor(db, insights.vendor_key(key))
    if not v:
        raise HTTPException(404, "Vendor not found")
    return TEMPLATES.TemplateResponse(request, "vendor.html", {"user": user, "v": v, "today": date.today(),
                                                              "outstanding": OUTSTANDING})


@app.get("/api/palette")
def palette(q: str = "", db: Session = Depends(get_db), user: str = Depends(require_user)):
    """Results for the command palette: bills (smart search) and vendors."""
    q = q.strip()
    bills = list(db.scalars(search.apply(select(Bill).order_by(Bill.updated_at.desc()), search.parse(q)).limit(6))) if q else \
        list(db.scalars(select(Bill).order_by(Bill.updated_at.desc()).limit(5)))
    vendors = [r for r in insights.vendors(db) if q.lower() in r["name"].lower()][:4] if q else []
    from urllib.parse import quote
    return {
        "bills": [{"title": b.vendor_name or b.file_name, "sub": " · ".join(x for x in [
                       b.bill_number, f"{b.currency or ''} {b.total:,.2f}".strip() if b.total is not None else None,
                       b.status.value.replace("_", " ")] if x), "url": f"/bills/{b.id}"} for b in bills],
        "vendors": [{"title": v["name"], "sub": f"{v['n']} bill{'s' if v['n'] != 1 else ''}", "url": "/vendors/" + quote(v["key"])} for v in vendors],
    }


@app.get("/bills", response_class=HTMLResponse)
def list_bills(request: Request, status: str | None = None, due: str | None = None, q: str | None = None,
               modal: str | None = None, db: Session = Depends(get_db), user: str = Depends(require_user)):
    today = date.today()
    soon = today + timedelta(days=7)

    query = select(Bill).order_by(Bill.updated_at.desc(), Bill.id.desc()).limit(500)
    terms = search.parse(q or "")
    query = search.apply(query, terms)
    if status in VIEWS:
        query = query.where(Bill.status.in_(VIEWS[status]))
    elif status and status != "mine":
        try:
            query = query.where(Bill.status == BillStatus(status))
        except ValueError:
            raise HTTPException(404, "Unknown status")
    if due == "overdue":
        query = query.where(Bill.status.in_(OUTSTANDING), Bill.due_date < today)
    elif due == "soon":
        query = query.where(Bill.status.in_(OUTSTANDING), Bill.due_date >= today, Bill.due_date <= soon)
    bills = list(db.scalars(query))
    if status == "mine":
        bills = [b for b in bills if (b.pending_approver or "").lower() == user]

    outstanding = list(db.scalars(select(Bill).where(Bill.status.in_(OUTSTANDING))))
    overdue = [b for b in outstanding if b.due_date and b.due_date < today]
    due_soon = [b for b in outstanding if b.due_date and today <= b.due_date <= soon]
    counts = dict(db.execute(select(Bill.status, func.count()).group_by(Bill.status)).all())
    counts = {s.value: counts.get(s, 0) for s in BillStatus}
    counts["inbox"] = counts["received"] + counts["needs_review"]
    return TEMPLATES.TemplateResponse(request, "list.html", {
        "user": user, "bills": bills, "status": status, "due": due, "today": today,
        "outstanding": OUTSTANDING, "counts": counts, "total_count": sum(counts[s.value] for s in BillStatus),
        "cards": [
            {"label": "Total outstanding", "n": len(outstanding), **_sum(outstanding), "href": "/bills?status=outstanding",
             "on": status == "outstanding" and not due, "tone": ""},
            {"label": "Overdue", "n": len(overdue), **_sum(overdue), "href": "/bills?due=overdue",
             "on": due == "overdue", "tone": "warn"},
            {"label": "Due in next 7 days", "n": len(due_soon), **_sum(due_soon), "href": "/bills?due=soon",
             "on": due == "soon", "tone": ""},
        ],
        "q": (q or "").strip(), "terms": terms,
        "chips": [(t.label, search.without(terms, i)) for i, t in enumerate(terms)],
        "match_total": _sum(bills) if terms else None, "mine_count": _nav_counts(user).get("mine", 0),
        "modal": modal, **_settings_ctx(db),
    })


@app.post("/bills/upload")
async def upload(files: list[UploadFile] = File(...), db: Session = Depends(get_db),
                 user: str = Depends(require_user)):
    last = None
    for f in files:
        try:
            last = workflow.ingest(db, content=await f.read(), file_name=f.filename or "bill",
                                   mime_type=f.content_type or "", source="upload", source_ref=user)
        except WorkflowError as exc:
            db.rollback()
            raise HTTPException(400, str(exc))
    if len(files) == 1 and last:
        msg = ("Bill read and sent for approval" if last.status == BillStatus.PENDING_APPROVAL
               else "Bill uploaded. Review the details" if last.status in EDITABLE else "Bill uploaded")
        return toast(_redirect(last.id), msg)
    return toast(RedirectResponse("/bills?status=inbox", status_code=303), f"{len(files)} bills uploaded")


def _accounts() -> list[str]:
    try:
        return get_connector().list_expense_accounts()
    except ERPError:
        return []


@app.get("/bills/{bill_id}", response_class=HTMLResponse)
def bill_detail(request: Request, bill_id: int, error: str | None = None, db: Session = Depends(get_db),
                user: str = Depends(require_user)):
    bill = _get_bill(db, bill_id)
    editable = bill.status in EDITABLE
    if editable and not error:
        return RedirectResponse(f"/bills/{bill_id}/review/vendor", status_code=303)
    return TEMPLATES.TemplateResponse(request, "detail.html", {
        "user": user, "chromeless": True, "bill": bill, "error": error, "editable": editable,
        "accounts": _accounts() if editable else [],
        "can_act": bill.status == BillStatus.PENDING_APPROVAL and (bill.pending_approver or "").lower() == user,
    })


@app.get("/bills/{bill_id}/file")
def bill_file(bill_id: int, db: Session = Depends(get_db), user: str = Depends(require_user)):
    bill = _get_bill(db, bill_id)
    return FileResponse(bill.file_path, media_type=bill.mime_type, filename=bill.file_name,
                        content_disposition_type="inline")


# --------------------------------------------------------------------------- review wizard (draft bills)

STEPS = [("vendor", "Vendor"), ("amount", "Amount"), ("categorization", "Categorization"), ("details", "Details"), ("review", "Review")]
STEP_KEYS = [k for k, _ in STEPS]
HARD = ("Missing", "No line items")


def _vendor_match(db: Session, bill: Bill) -> dict | None:
    """Earlier bills from the same vendor, like a 'recipient match'."""
    if not bill.vendor_name:
        return None
    prior = list(db.scalars(select(Bill).where(
        Bill.id != bill.id, func.lower(Bill.vendor_name) == bill.vendor_name.lower(), Bill.status != BillStatus.REJECTED,
    ).order_by(Bill.created_at.desc())))
    rule = db.scalar(select(VendorRule).where(VendorRule.vendor_key == workflow._vendor_key(bill.vendor_name)))
    posted = [b for b in prior if b.status == BillStatus.POSTED]
    return {"count": len(prior), "last": prior[0] if prior else None, "posted": len(posted),
            "account": rule.default_account if rule else None, "tax_id": next((b.vendor_tax_id for b in prior if b.vendor_tax_id), None)}


@app.get("/bills/{bill_id}/review")
def review_start(bill_id: int, user: str = Depends(require_user)):
    return RedirectResponse(f"/bills/{bill_id}/review/vendor", status_code=303)


@app.get("/bills/{bill_id}/review/{step}", response_class=HTMLResponse)
def review_step(request: Request, bill_id: int, step: str, error: str | None = None,
                db: Session = Depends(get_db), user: str = Depends(require_user)):
    bill = _get_bill(db, bill_id)
    if step not in STEP_KEYS:
        raise HTTPException(404)
    if bill.status not in EDITABLE:
        return RedirectResponse(f"/bills/{bill_id}", status_code=303)
    reasons = workflow.validate(db, bill)
    approvers = None
    if bill.total is not None:
        try:
            approvers = workflow.approvers_for(bill.total)
        except WorkflowError:
            approvers = None
    return TEMPLATES.TemplateResponse(request, "review.html", {
        "user": user, "chromeless": True, "bill": bill, "step": step, "steps": STEPS, "idx": STEP_KEYS.index(step), "error": error,
        "accounts": _accounts(), "match": _vendor_match(db, bill), "reasons": reasons,
        "hard": [r for r in reasons if r.startswith(HARD)], "approvers": approvers,
        "erp": integrations.load(db),
    })


def _num(v) -> float | None:
    v = str(v or "").replace(",", "").strip()
    return float(v) if v else None


@app.post("/bills/{bill_id}/review/{step}")
async def review_save(request: Request, bill_id: int, step: str, db: Session = Depends(get_db),
                      user: str = Depends(require_user)):
    bill = _get_bill(db, bill_id)
    if step not in STEP_KEYS:
        raise HTTPException(404)
    if bill.status not in EDITABLE:
        return RedirectResponse(f"/bills/{bill_id}", status_code=303)
    form = await request.form()
    go = form.get("go", "next")
    try:
        if step == "vendor":
            bill.vendor_name = (form.get("vendor_name") or "").strip() or None
            bill.vendor_tax_id = (form.get("vendor_tax_id") or "").strip() or None
        elif step == "amount":
            bill.currency = (form.get("currency") or "").strip().upper()[:3] or None
            bill.total = _num(form.get("total"))
            bill.tax_total = _num(form.get("tax_total"))
        elif step == "categorization":
            mode = form.get("mode", "single")
            if mode == "single":
                account = (form.get("account") or "").strip() or None
                if not bill.lines:
                    base = round((bill.total or 0) - (bill.tax_total or 0), 2)
                    bill.lines = [BillLine(position=0, description=f"{bill.vendor_name or 'Vendor'} bill {bill.bill_number or ''}".strip(),
                                           quantity=1, unit_price=base, amount=base)]
                for line in bill.lines:
                    line.account_name = account
            else:
                lines = []
                descs = form.getlist("line_description")
                for i, desc in enumerate(descs):
                    if not desc.strip():
                        continue
                    qty = _num(form.getlist("line_quantity")[i]) or 1
                    price = _num(form.getlist("line_unit_price")[i]) or 0
                    lines.append(BillLine(position=i, description=desc.strip(), quantity=qty, unit_price=price,
                                          amount=round(qty * price, 2), tax_rate=_num(form.getlist("line_tax_rate")[i]),
                                          account_name=form.getlist("line_account")[i] or None))
                bill.lines = lines
            bill.subtotal = round(sum(l.amount for l in bill.lines), 2) if bill.lines else None
        elif step == "details":
            bill.bill_number = (form.get("bill_number") or "").strip() or None
            bill.bill_date = date.fromisoformat(form["bill_date"]) if form.get("bill_date") else None
            bill.due_date = date.fromisoformat(form["due_date"]) if form.get("due_date") else None
            bill.notes = (form.get("notes") or "").strip() or None
    except (ValueError, IndexError) as exc:
        db.rollback()
        return RedirectResponse(f"/bills/{bill_id}/review/{step}?error=Invalid input: {exc}", status_code=303)

    if step != "review":
        workflow._event(bill, user, "edited", f"{step} step")
    bill.review_reasons = workflow.validate(db, bill)
    bill.status = BillStatus.NEEDS_REVIEW if bill.review_reasons else bill.status

    if step == "review" and go == "submit":
        hard = [r for r in bill.review_reasons if r.startswith(HARD)]
        if hard:
            db.commit()
            return RedirectResponse(f"/bills/{bill_id}/review/review?error=" + "; ".join(hard), status_code=303)
        try:
            workflow.submit_for_approval(db, bill, user)
        except WorkflowError as exc:
            db.rollback()
            return RedirectResponse(f"/bills/{bill_id}/review/review?error={exc}", status_code=303)
        db.commit()
        msg = {BillStatus.POSTED: "Auto-approved and posted", BillStatus.APPROVED: "Auto-approved",
               BillStatus.POST_FAILED: "Auto-approved, but posting failed"}.get(
            bill.status, f"Sent to {bill.pending_approver} for approval")
        return toast(_redirect(bill_id), msg, "err" if bill.status == BillStatus.POST_FAILED else "ok")
    db.commit()
    i = STEP_KEYS.index(step)
    if go == "exit":
        return toast(RedirectResponse("/bills?status=inbox", status_code=303), "Draft saved")
    if go in STEP_KEYS:
        return RedirectResponse(f"/bills/{bill_id}/review/{go}", status_code=303)
    nxt = STEP_KEYS[max(0, i - 1)] if go == "back" else STEP_KEYS[min(len(STEP_KEYS) - 1, i + 1)]
    return RedirectResponse(f"/bills/{bill_id}/review/{nxt}", status_code=303)


def _delete_bill(db: Session, bill: Bill) -> None:
    path = bill.file_path
    db.delete(bill)
    db.flush()
    # Remove the stored file unless another bill points at the same upload.
    if path and not db.scalar(select(Bill.id).where(Bill.file_path == path)):
        try:
            os.remove(path)
        except OSError:
            pass


@app.post("/bills/{bill_id}/delete")
def delete_draft(bill_id: int, db: Session = Depends(get_db), user: str = Depends(require_user)):
    bill = _get_bill(db, bill_id)
    if bill.status not in EDITABLE:
        return _redirect(bill_id, "Only draft bills can be deleted")
    name = bill.vendor_name or bill.file_name
    _delete_bill(db, bill)
    db.commit()
    return toast(RedirectResponse("/bills?status=inbox", status_code=303), f"Deleted draft from {name}")


@app.post("/bills/bulk")
async def bulk_action(request: Request, db: Session = Depends(get_db), user: str = Depends(require_user)):
    form = await request.form()
    ids = [int(i) for i in form.getlist("ids") if str(i).isdigit()]
    resp = RedirectResponse(request.headers.get("referer") or "/bills", status_code=303)
    if form.get("action") == "delete":
        drafts = list(db.scalars(select(Bill).where(Bill.id.in_(ids), Bill.status.in_(EDITABLE))))
        for b in drafts:
            _delete_bill(db, b)
        db.commit()
        skipped = len(ids) - len(drafts)
        toast(resp, f"Deleted {len(drafts)} draft{'s' if len(drafts) != 1 else ''}"
                    + (f"; skipped {skipped} already in approval" if skipped else ""))
    return resp


@app.post("/bills/{bill_id}/edit")
async def edit_bill(request: Request, bill_id: int, db: Session = Depends(get_db),
                    user: str = Depends(require_user)):
    bill = _get_bill(db, bill_id)
    if bill.status not in EDITABLE:
        return _redirect(bill_id, "Bill can no longer be edited")
    form = await request.form()

    def num(v):
        return float(v) if str(v).strip() else None

    try:
        bill.vendor_name = form.get("vendor_name") or None
        bill.vendor_tax_id = form.get("vendor_tax_id") or None
        bill.bill_number = form.get("bill_number") or None
        bill.bill_date = date.fromisoformat(form["bill_date"]) if form.get("bill_date") else None
        bill.due_date = date.fromisoformat(form["due_date"]) if form.get("due_date") else None
        bill.currency = (form.get("currency") or "").upper()[:3] or None
        bill.tax_total = num(form.get("tax_total", ""))
        bill.total = num(form.get("total", ""))
        bill.notes = form.get("notes") or None

        lines = []
        for i, desc in enumerate(form.getlist("line_description")):
            if not desc.strip():
                continue
            qty = num(form.getlist("line_quantity")[i]) or 1
            price = num(form.getlist("line_unit_price")[i]) or 0
            lines.append(BillLine(
                position=i, description=desc.strip(), quantity=qty, unit_price=price,
                amount=round(qty * price, 2), tax_rate=num(form.getlist("line_tax_rate")[i]),
                account_name=form.getlist("line_account")[i] or None,
            ))
        bill.lines = lines
        bill.subtotal = round(sum(l.amount for l in lines), 2)
    except (ValueError, IndexError) as exc:
        db.rollback()
        return _redirect(bill_id, f"Invalid input: {exc}")

    workflow._event(bill, user, "edited")
    bill.review_reasons = workflow.validate(db, bill)
    bill.status = BillStatus.NEEDS_REVIEW if bill.review_reasons else bill.status
    if form.get("submit") and not bill.review_reasons:
        workflow.submit_for_approval(db, bill, user)
    db.commit()
    return _redirect(bill_id)


@app.post("/bills/{bill_id}/submit")
def submit(bill_id: int, db: Session = Depends(get_db), user: str = Depends(require_user)):
    """Submit despite open review notes (a human has looked at it)."""
    bill = _get_bill(db, bill_id)
    try:
        hard = [r for r in workflow.validate(db, bill) if r.startswith("Missing") or r == "No line items"]
        if hard:
            raise WorkflowError("; ".join(hard))
        workflow.submit_for_approval(db, bill, user)
        db.commit()
    except WorkflowError as exc:
        db.rollback()
        return _redirect(bill_id, str(exc))
    return _redirect(bill_id)


@app.post("/bills/{bill_id}/approve")
def approve(bill_id: int, db: Session = Depends(get_db), user: str = Depends(require_user)):
    bill = _get_bill(db, bill_id)
    try:
        workflow.approve(db, bill, user)
        db.commit()
    except WorkflowError as exc:
        db.rollback()
        return _redirect(bill_id, str(exc))
    msg = {BillStatus.POSTED: "Approved and posted", BillStatus.POST_FAILED: "Approved, but posting failed",
           BillStatus.PENDING_APPROVAL: f"Approved. Now with {bill.pending_approver}"}.get(bill.status, "Approved")
    return toast(_redirect(bill_id), msg, "err" if bill.status == BillStatus.POST_FAILED else "ok")


@app.post("/bills/{bill_id}/reject")
def reject(bill_id: int, reason: str = Form(""), db: Session = Depends(get_db),
           user: str = Depends(require_user)):
    bill = _get_bill(db, bill_id)
    try:
        workflow.reject(db, bill, user, reason)
        db.commit()
    except WorkflowError as exc:
        db.rollback()
        return _redirect(bill_id, str(exc))
    return toast(_redirect(bill_id), "Bill rejected")


@app.post("/bills/{bill_id}/retry-post")
def retry_post(bill_id: int, db: Session = Depends(get_db), user: str = Depends(require_user)):
    bill = _get_bill(db, bill_id)
    try:
        workflow._event(bill, user, "retry_post")
        workflow.post_to_erp(db, bill)
        db.commit()
    except WorkflowError as exc:
        db.rollback()
        return _redirect(bill_id, str(exc))
    ok = bill.status == BillStatus.POSTED
    return toast(_redirect(bill_id), "Posted" if ok else "Posting failed again", "ok" if ok else "err")


# --------------------------------------------------------------------------- settings


def _settings_redirect(msg: str | None = None, error: str | None = None) -> RedirectResponse:
    from urllib.parse import urlencode

    qs = urlencode({k: v for k, v in {"msg": msg, "error": error}.items() if v})
    return RedirectResponse("/settings/integrations/zoho_books" + (f"?{qs}" if qs else ""), status_code=303)


def _settings_ctx(db: Session) -> dict:
    """Everything the settings pages and the Bookit settings modal show."""
    s = get_settings()
    erp = integrations.load(db)
    t = erp.last_test
    if not erp.configured:
        zoho_state = "available"
    elif t and t.get("ok"):
        zoho_state = "connected"
    elif t:
        zoho_state = "failed"
    else:
        zoho_state = "untested"
    return {
        "erp": erp, "zoho_state": zoho_state, "catalog": integrations.ERP_CATALOG, "dcs": integrations.ZOHO_DCS,
        "tiers": s.approval_tiers, "imap_on": bool(s.imap.get("host")), "smtp_on": bool(s.smtp.get("host")),
        "imap_user": s.imap.get("user"), "smtp_from": s.smtp.get("from"),
        "inbound_url": f"{s.app_base_url}/api/inbound-email?key=…",
    }


@app.get("/settings")
def settings_root(user: str = Depends(require_user)):
    return RedirectResponse("/settings/integrations", status_code=303)


@app.get("/settings/{section}", response_class=HTMLResponse)
def settings_section(request: Request, section: str, db: Session = Depends(get_db), user: str = Depends(require_user)):
    pages = {"integrations": "settings_integrations.html", "approvals": "settings_approvals.html", "email": "settings_email.html"}
    if section not in pages:
        raise HTTPException(404)
    return TEMPLATES.TemplateResponse(request, pages[section], {"user": user, "section": section, **_settings_ctx(db)})


@app.get("/settings/integrations/zoho_books", response_class=HTMLResponse)
def settings_zoho(request: Request, msg: str | None = None, error: str | None = None,
                  db: Session = Depends(get_db), user: str = Depends(require_user)):
    return TEMPLATES.TemplateResponse(request, "settings_zoho.html", {
        "user": user, "section": "integrations", "msg": msg, "error": error, **_settings_ctx(db),
    })


@app.post("/settings/erp")
async def settings_save_erp(request: Request, db: Session = Depends(get_db), user: str = Depends(require_user)):
    form = await request.form()
    try:
        integrations.save_zoho(db, {k: str(v) for k, v in form.items()}, user)
        db.commit()
    except ValueError as exc:
        db.rollback()
        return _settings_redirect(error=str(exc))
    set_connector(None)  # rebuild with the new settings on next use
    return _settings_redirect(msg="Zoho Books settings saved. Test the connection to confirm they work.")


@app.post("/settings/erp/test")
def settings_test_erp(db: Session = Depends(get_db), user: str = Depends(require_user)):
    try:
        accounts = build_connector().list_expense_accounts()
        ok, message = True, f"Connected. {len(accounts)} expense accounts available for coding bills."
    except ERPError as exc:
        ok, message = False, str(exc)
    except Exception as exc:  # network errors etc.
        ok, message = False, f"Could not reach Zoho: {exc}"
    integrations.record_test(db, ok, message, user)
    db.commit()
    set_connector(None)
    return _settings_redirect(**({"msg": message} if ok else {"error": message}))


@app.post("/settings/erp/disconnect")
def settings_disconnect_erp(db: Session = Depends(get_db), user: str = Depends(require_user)):
    integrations.disconnect(db)
    db.commit()
    set_connector(None)
    return _settings_redirect(msg="Saved Zoho Books settings removed.")


# --------------------------------------------------------------------------- one-click email actions
# GET only shows a confirmation page, so mail-scanner link prefetching can't approve anything.


@app.get("/bills/{bill_id}/email-action", response_class=HTMLResponse)
def email_action_confirm(request: Request, bill_id: int, action: str, step: int, approver: str, token: str,
                         db: Session = Depends(get_db)):
    if not notify.verify(bill_id, step, approver, action, token):
        raise HTTPException(403, "Invalid link")
    bill = _get_bill(db, bill_id)
    stale = bill.status != BillStatus.PENDING_APPROVAL or bill.current_step != step
    return TEMPLATES.TemplateResponse(request, "confirm.html", {
        "user": None, "bill": bill, "action": action, "step": step, "approver": approver,
        "token": token, "stale": stale,
    })


@app.post("/bills/{bill_id}/email-action")
def email_action(bill_id: int, action: str = Form(...), step: int = Form(...), approver: str = Form(...),
                 token: str = Form(...), reason: str = Form(""), db: Session = Depends(get_db)):
    if not notify.verify(bill_id, step, approver, action, token):
        raise HTTPException(403, "Invalid link")
    bill = _get_bill(db, bill_id)
    if bill.current_step != step:
        raise HTTPException(409, "This approval step has already been handled")
    try:
        if action == "approve":
            workflow.approve(db, bill, approver)
        elif action == "reject":
            workflow.reject(db, bill, approver, reason or "rejected via email")
        else:
            raise HTTPException(400, "Unknown action")
        db.commit()
    except WorkflowError as exc:
        db.rollback()
        raise HTTPException(409, str(exc))
    return HTMLResponse(f"<p style='font-family:sans-serif'>Done — bill {bill.id} is now <b>{bill.status.value}</b>.</p>")


# --------------------------------------------------------------------------- inbound email webhook
# For providers that forward mail over HTTP (Postmark, SendGrid, Mailgun "store & notify" raw MIME,
# Cloudflare Email Workers ...). POST the raw RFC 822 message as the request body.


@app.post("/api/inbound-email")
async def inbound_email(request: Request, key: str):
    if not hmac.compare_digest(key, get_settings().secret_key):
        raise HTTPException(403)
    count = mailbox.process_message(await request.body())
    return {"ingested": count}
