import json
from datetime import date

import httpx2
import pytest

from app.erp import Attachment, ERPBill, ERPError, ERPLine
from app.erp.zoho_books import ZohoBooksConnector


class FakeZoho:
    def __init__(self, existing_bill=False, vendor_exists=False):
        self.calls = []
        self.existing_bill = existing_bill
        self.vendor_exists = vendor_exists
        self.created_bill = None

    def __call__(self, request: httpx2.Request) -> httpx2.Response:
        path, method = request.url.path, request.method
        self.calls.append((method, path))
        if "oauth" in path:
            return httpx2.Response(200, json={"access_token": "tok", "expires_in": 3600})
        assert request.headers["Authorization"] == "Zoho-oauthtoken tok"
        assert request.url.params["organization_id"] == "org1"
        if path.endswith("/chartofaccounts"):
            return httpx2.Response(200, json={"code": 0, "chartofaccounts": [
                {"account_id": "A1", "account_name": "Office Supplies", "account_type": "expense"},
                {"account_id": "A2", "account_name": "Other Expenses", "account_type": "other_expense"},
                {"account_id": "B1", "account_name": "Bank", "account_type": "bank"},
            ]})
        if path.endswith("/settings/taxes"):
            return httpx2.Response(200, json={"code": 0, "taxes": [{"tax_id": "T18", "tax_percentage": 18}]})
        if path.endswith("/contacts") and method == "GET":
            contacts = [{"contact_id": "V1", "contact_name": "Acme Paper Co"}] if self.vendor_exists else []
            return httpx2.Response(200, json={"code": 0, "contacts": contacts})
        if path.endswith("/contacts") and method == "POST":
            return httpx2.Response(201, json={"code": 0, "contact": {"contact_id": "V9"}})
        if path.endswith("/bills") and method == "GET":
            bills = [{"bill_id": "OLD", "bill_number": "INV-1"}] if self.existing_bill else []
            return httpx2.Response(200, json={"code": 0, "bills": bills})
        if path.endswith("/bills") and method == "POST":
            self.created_bill = json.loads(request.content)
            return httpx2.Response(201, json={"code": 0, "bill": {"bill_id": "NEW"}})
        if path.endswith("/attachment"):
            return httpx2.Response(200, json={"code": 0, "message": "ok"})
        return httpx2.Response(404, json={"code": 404, "message": "nope"})


def connector(fake, default="Other Expenses"):
    return ZohoBooksConnector(client_id="c", client_secret="s", refresh_token="r", organization_id="org1",
                              default_expense_account=default, transport=httpx2.MockTransport(fake))


def bill(**kw):
    data = dict(vendor_name="Acme Paper Co", bill_number="INV-1", bill_date=date(2026, 10, 1),
                due_date=date(2026, 10, 31), reference="BK-7",
                lines=[ERPLine("Paper", 2, 50, "office supplies", 18), ERPLine("Misc", 1, 10, "Travel", None)])
    data.update(kw)
    return ERPBill(**data)


def test_creates_vendor_and_bill_with_mapped_accounts_and_tax():
    fake = FakeZoho()
    result = connector(fake).post_bill(bill(), Attachment("b.pdf", b"%PDF", "application/pdf"))
    assert result.erp_bill_id == "NEW" and not result.already_existed
    assert ("POST", "/books/v3/contacts") in fake.calls
    assert ("POST", "/books/v3/bills/NEW/attachment") in fake.calls
    created = fake.created_bill
    assert created["vendor_id"] == "V9" and created["reference_number"] == "BK-7"
    assert created["line_items"][0] == {"account_id": "A1", "description": "Paper", "rate": 50, "quantity": 2, "tax_id": "T18"}
    assert created["line_items"][1]["account_id"] == "A2"  # unknown 'Travel' -> default account
    assert any("Travel" in w for w in result.warnings)


def test_existing_bill_is_not_recreated():
    fake = FakeZoho(existing_bill=True, vendor_exists=True)
    result = connector(fake).post_bill(bill())
    assert result.already_existed and result.erp_bill_id == "OLD"
    assert fake.created_bill is None


def test_only_expense_accounts_listed():
    assert connector(FakeZoho()).list_expense_accounts() == ["Office Supplies", "Other Expenses"]


def test_unknown_account_without_default_errors():
    with pytest.raises(ERPError):
        connector(FakeZoho(), default=None).post_bill(bill())


def test_api_error_surfaces_message():
    def failing(request):
        if "oauth" in request.url.path:
            return httpx2.Response(200, json={"access_token": "tok", "expires_in": 3600})
        return httpx2.Response(400, json={"code": 1001, "message": "Invalid vendor"})
    with pytest.raises(ERPError, match="Invalid vendor"):
        connector(failing).post_bill(bill())
