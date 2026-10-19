import hashlib
import hmac
import logging
import os
import threading
from contextlib import asynccontextmanager
from datetime import date

from fastapi import Depends, FastAPI, File, Form, HTTPException, Request, UploadFile
from fastapi.responses import FileResponse, HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from . import mailbox, notify, ui, workflow
from .config import get_settings
from .db import get_db, init_db
from .erp import ERPError, get_connector
from .models import Bill, BillLine, BillStatus
from .workflow import WorkflowError

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")

TEMPLATES = Jinja2Templates(directory=os.path.join(os.path.dirname(__file__), "templates"))
ui.install(TEMPLATES.env)
TEMPLATES.env.globals["erp_name"] = lambda: get_settings().erp.replace("_", " ").title()
EDITABLE = {BillStatus.RECEIVED, BillStatus.NEEDS_REVIEW}


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
    resp = RedirectResponse("/bills", status_code=303)
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


def _redirect(bill_id: int, error: str | None = None) -> RedirectResponse:
    url = f"/bills/{bill_id}" + (f"?error={error}" if error else "")
    return RedirectResponse(url, status_code=303)


def _queue_counts(db: Session, user: str) -> dict:
    """Bill counts per status for the queue rail, plus 'mine' (waiting on this user) and 'all'."""
    counts = {status.value: n for status, n in db.execute(select(Bill.status, func.count()).group_by(Bill.status))}
    counts["all"] = sum(counts.values())
    pending = db.execute(select(Bill.approvers, Bill.current_step).where(Bill.status == BillStatus.PENDING_APPROVAL))
    counts["mine"] = sum(1 for approvers, step in pending
                         if step < len(approvers or []) and approvers[step].lower() == user)
    return counts


@app.get("/")
def root():
    return RedirectResponse("/bills", status_code=303)


@app.get("/bills", response_class=HTMLResponse)
def list_bills(request: Request, status: str | None = None, db: Session = Depends(get_db),
               user: str = Depends(require_user)):
    q = select(Bill).order_by(Bill.id.desc()).limit(500)
    if status == "mine":
        bills = [b for b in db.scalars(q.where(Bill.status == BillStatus.PENDING_APPROVAL))
                 if (b.pending_approver or "").lower() == user]
    else:
        if status:
            q = q.where(Bill.status == BillStatus(status))
        bills = list(db.scalars(q))
    return TEMPLATES.TemplateResponse(request, "list.html", {
        "user": user, "bills": bills, "status": status, "statuses": list(BillStatus),
        "counts": _queue_counts(db, user), "inbox": get_settings().imap.get("user"),
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
        return _redirect(last.id)
    return RedirectResponse("/bills", status_code=303)


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
    return TEMPLATES.TemplateResponse(request, "detail.html", {
        "user": user, "bill": bill, "error": error, "editable": editable,
        "accounts": _accounts() if editable else [],
        "can_act": bill.status == BillStatus.PENDING_APPROVAL and (bill.pending_approver or "").lower() == user,
        "counts": _queue_counts(db, user),
    })


@app.get("/bills/{bill_id}/file")
def bill_file(bill_id: int, db: Session = Depends(get_db), user: str = Depends(require_user)):
    bill = _get_bill(db, bill_id)
    return FileResponse(bill.file_path, media_type=bill.mime_type, filename=bill.file_name,
                        content_disposition_type="inline")


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
    return _redirect(bill_id)


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
    return _redirect(bill_id)


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
    return _redirect(bill_id)


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
def email_action(request: Request, bill_id: int, action: str = Form(...), step: int = Form(...), approver: str = Form(...),
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
    return TEMPLATES.TemplateResponse(request, "done.html", {"user": None, "bill": bill, "action": action})


# --------------------------------------------------------------------------- inbound email webhook
# For providers that forward mail over HTTP (Postmark, SendGrid, Mailgun "store & notify" raw MIME,
# Cloudflare Email Workers ...). POST the raw RFC 822 message as the request body.


@app.post("/api/inbound-email")
async def inbound_email(request: Request, key: str):
    if not hmac.compare_digest(key, get_settings().secret_key):
        raise HTTPException(403)
    count = mailbox.process_message(await request.body())
    return {"ingested": count}
