from email.message import EmailMessage

import pytest
from fastapi.testclient import TestClient

from app import main, mailbox, notify, workflow
from app.db import SessionLocal
from app.models import Bill, BillStatus

from .conftest import make_extracted


@pytest.fixture
def client(erp, monkeypatch):
    extracted = {"value": make_extracted(total=1180)}
    real_ingest = workflow.ingest

    def fake_ingest(db, **kw):
        return real_ingest(db, extractor=lambda *a, **k: extracted["value"], **kw)

    monkeypatch.setattr(workflow, "ingest", fake_ingest)
    monkeypatch.setattr(mailbox, "ingest", fake_ingest)
    with TestClient(main.app) as c:
        c.extracted = extracted
        yield c


def login(client, email):
    client.post("/login", data={"email": email}, follow_redirects=False)


def test_requires_login(client):
    assert client.get("/bills", follow_redirects=False).status_code == 303


def test_upload_approve_post_flow(client, erp):
    login(client, "clerk@acme.test")
    r = client.post("/bills/upload", files={"files": ("b.pdf", b"%PDF-1", "application/pdf")})
    assert r.status_code == 200 and "pending approval" in r.text.lower()

    # clerk is not the approver
    client.post("/bills/1/approve")
    with SessionLocal() as db:
        assert db.get(Bill, 1).status == BillStatus.PENDING_APPROVAL

    login(client, "manager@acme.test")
    assert "Your approval is needed" in client.get("/bills/1").text
    assert "Acme Paper Co" in client.get("/bills?status=mine").text
    client.post("/bills/1/approve")
    with SessionLocal() as db:
        assert db.get(Bill, 1).status == BillStatus.POSTED
    assert len(erp.posted) == 1


def test_review_edit_and_submit(client):
    client.extracted["value"] = make_extracted(total=1180, bill_number=None)
    login(client, "clerk@acme.test")
    client.post("/bills/upload", files={"files": ("b.pdf", b"%PDF-2", "application/pdf")})
    with SessionLocal() as db:
        assert db.get(Bill, 1).status == BillStatus.NEEDS_REVIEW
    form = {
        "vendor_name": "Acme Paper Co", "bill_number": "INV-77", "bill_date": "2026-10-01", "currency": "usd",
        "tax_total": "180", "total": "1180", "submit": "1",
        "line_description": ["Paper", ""], "line_quantity": ["2", ""], "line_unit_price": ["500", ""],
        "line_tax_rate": ["18", ""], "line_account": ["Office Supplies", ""],
    }
    client.post("/bills/1/edit", data=form)
    with SessionLocal() as db:
        bill = db.get(Bill, 1)
        assert bill.status == BillStatus.PENDING_APPROVAL, bill.review_reasons
        assert bill.bill_number == "INV-77" and bill.currency == "USD" and bill.lines[0].amount == 1000


def test_email_link_needs_confirmation(client, erp):
    login(client, "clerk@acme.test")
    client.post("/bills/upload", files={"files": ("b.pdf", b"%PDF-3", "application/pdf")})
    client.cookies.clear()
    token = notify.sign(1, 0, "manager@acme.test", "approve")
    qs = f"action=approve&step=0&approver=manager@acme.test&token={token}"
    assert "Confirm approve" in client.get(f"/bills/1/email-action?{qs}").text
    with SessionLocal() as db:
        assert db.get(Bill, 1).status == BillStatus.PENDING_APPROVAL  # GET alone does nothing

    bad = client.post("/bills/1/email-action", data={"action": "approve", "step": 0,
                                                     "approver": "manager@acme.test", "token": "x"})
    assert bad.status_code == 403
    ok = client.post("/bills/1/email-action", data={"action": "approve", "step": 0,
                                                    "approver": "manager@acme.test", "token": token})
    assert "posted" in ok.text


def test_inbound_email_webhook(client, erp):
    msg = EmailMessage()
    msg["From"] = "Acme Billing <billing@acmepaper.test>"
    msg["Subject"] = "Invoice INV-1001"
    msg.set_content("Please find attached.")
    msg.add_attachment(b"%PDF-email", maintype="application", subtype="pdf", filename="INV-1001.pdf")
    msg.add_attachment(b"GIF89a", maintype="image", subtype="gif", filename="logo.gif", disposition="inline")
    r = client.post("/api/inbound-email?key=test-secret", content=msg.as_bytes())
    assert r.json() == {"ingested": 1}
    assert client.post("/api/inbound-email?key=wrong", content=b"").status_code == 403
    with SessionLocal() as db:
        bill = db.get(Bill, 1)
        assert bill.source == "email" and bill.source_ref.startswith("billing@acmepaper.test")


def test_search_filters_bills(client):
    login(client, "ap@acme.test")
    client.post("/bills/upload", files={"files": ("a.pdf", b"%PDF-1.4 one", "application/pdf")})
    assert "Acme Paper Co" in client.get("/bills?q=acme").text
    assert "No bills match" in client.get("/bills?q=zzz-nothing").text


def test_review_wizard_walks_draft_to_approval(client):
    login(client, "clerk@acme.test")
    client.extracted["value"] = make_extracted(total=1180, number=None)
    r = client.post("/bills/upload", files={"files": ("w.pdf", b"%PDF-1.4 wizard", "application/pdf")})
    assert r.url.path == "/bills/1/review/vendor" and "Vendor" in r.text  # drafts open in the wizard
    client.post("/bills/1/review/vendor", data={"vendor_name": "Acme Paper Co", "vendor_tax_id": "", "go": "next"})
    client.post("/bills/1/review/amount", data={"total": "1,180.00", "tax_total": "180", "currency": "usd", "go": "next"})
    client.post("/bills/1/review/categorization", data={"mode": "single", "account": "Office Supplies", "go": "next"})
    r = client.post("/bills/1/review/details", data={"bill_number": "INV-77", "bill_date": "2026-10-01", "due_date": "", "notes": "", "go": "next"})
    assert r.url.path == "/bills/1/review/review" and "manager@acme.test" in r.text
    r = client.post("/bills/1/review/review", data={"go": "submit"})
    assert "Waiting" in r.text
    with SessionLocal() as db:
        b = db.get(Bill, 1)
        assert b.status == BillStatus.PENDING_APPROVAL and b.bill_number == "INV-77" and b.currency == "USD"
        assert all(l.account_name == "Office Supplies" for l in b.lines)


def test_submit_blocked_until_required_fields(client):
    login(client, "clerk@acme.test")
    client.extracted["value"] = make_extracted(number=None)
    client.post("/bills/upload", files={"files": ("x.pdf", b"%PDF-1.4 x", "application/pdf")})
    r = client.post("/bills/1/review/review", data={"go": "submit"})
    assert "Missing bill number" in r.text
    with SessionLocal() as db:
        assert db.get(Bill, 1).status == BillStatus.NEEDS_REVIEW


def test_delete_draft_and_bulk_delete(client):
    login(client, "clerk@acme.test")
    client.extracted["value"] = make_extracted(number=None)
    for i in range(3):
        client.post("/bills/upload", files={"files": (f"d{i}.pdf", f"%PDF-1.4 {i}".encode(), "application/pdf")})
    client.post("/bills/1/delete")
    client.post("/bills/bulk", data={"action": "delete", "ids": ["2", "3"]})
    with SessionLocal() as db:
        assert db.query(Bill).count() == 0


def test_home_vendors_and_palette(client):
    login(client, "manager@acme.test")
    r = client.post("/bills/upload", files={"files": ("h.pdf", b"%PDF-1.4 home", "application/pdf")})
    assert "bk_toast" in r.history[0].headers.get("set-cookie", "")  # toast queued for the next page
    home = client.get("/home").text
    assert "Waiting on your approval" in home and "Acme Paper Co" in home and "Cash out" in home
    assert "Acme Paper Co" in client.get("/vendors").text
    v = client.get("/vendors/acme%20paper%20co")
    assert v.status_code == 200 and "Total billed" in v.text
    assert client.get("/vendors/nobody").status_code == 404
    data = client.get("/api/palette?q=acme").json()
    assert data["bills"][0]["url"] == "/bills/1" and data["vendors"][0]["title"] == "Acme Paper Co"
    assert client.get("/", follow_redirects=False).headers["location"] == "/home"


def test_sample_data_load_and_remove(client):
    login(client, "manager@acme.test")
    client.post("/bills/upload", files={"files": ("real.pdf", b"%PDF-1.4 real", "application/pdf")})
    r = client.post("/sample/load")
    assert "exploring sample data" in r.text and "Waiting on your approval" in r.text
    with SessionLocal() as db:
        assert db.query(Bill).filter(Bill.source == "sample").count() == 12
        assert any(b.status == BillStatus.POSTED for b in db.query(Bill).filter(Bill.source == "sample"))
    assert client.get("/bills/2").status_code == 200  # sample bills open normally
    assert "Northwind Cloud" in client.get("/vendors").text
    client.post("/sample/remove")
    with SessionLocal() as db:
        assert db.query(Bill).count() == 1  # the real bill stays
