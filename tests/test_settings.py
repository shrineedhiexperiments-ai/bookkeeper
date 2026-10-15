import pytest
from fastapi.testclient import TestClient

from app import integrations, main
from app.db import SessionLocal
from app.erp import ERPError

ZOHO_FORM = {
    "dc": "eu", "organization_id": "600123", "client_id": "1000.abc",
    "client_secret": "s3cret-value", "refresh_token": "1000.refresh-value",
    "default_expense_account": "Other Expenses",
}


@pytest.fixture
def client():
    with TestClient(main.app) as c:
        c.post("/login", data={"email": "admin@acme.test"}, follow_redirects=False)
        yield c


def test_settings_page_needs_login():
    with TestClient(main.app) as c:
        r = c.get("/settings", follow_redirects=False)
    assert r.status_code == 303 and r.headers["location"] == "/login"


def test_save_overrides_env_and_never_echoes_secrets(client):
    r = client.post("/settings/erp", data=ZOHO_FORM)
    assert r.status_code == 200 and "settings saved" in r.text
    cfg = integrations.load()
    assert cfg.configured and cfg.zoho["dc"] == "eu" and cfg.zoho["client_secret"] == "s3cret-value"
    assert cfg.fields["organization_id"].source == "settings"
    assert "s3cret-value" not in r.text and "refresh-value" not in r.text
    assert "600123" in r.text


def test_blank_secret_keeps_saved_value(client):
    client.post("/settings/erp", data=ZOHO_FORM)
    client.post("/settings/erp", data={**ZOHO_FORM, "client_secret": "", "refresh_token": "", "organization_id": "600999"})
    cfg = integrations.load()
    assert cfg.zoho["client_secret"] == "s3cret-value" and cfg.zoho["organization_id"] == "600999"


def test_unknown_data_centre_rejected(client):
    r = client.post("/settings/erp", data={**ZOHO_FORM, "dc": "evil.example"})
    assert "Unknown Zoho data centre" in r.text
    assert not integrations.load().configured


def test_connection_test_records_result(client, monkeypatch):
    client.post("/settings/erp", data=ZOHO_FORM)

    class OK:
        def list_expense_accounts(self):
            return ["Rent", "Travel"]

    monkeypatch.setattr(main, "build_connector", lambda: OK())
    r = client.post("/settings/erp/test")
    assert "2 expense accounts" in r.text and integrations.load().last_test["ok"]

    def broken():
        raise ERPError("Zoho token refresh failed: invalid_code")

    monkeypatch.setattr(main, "build_connector", broken)
    r = client.post("/settings/erp/test")
    assert "Connection failed" in r.text and not integrations.load().last_test["ok"]


def test_disconnect_clears_saved_settings(client):
    client.post("/settings/erp", data=ZOHO_FORM)
    client.post("/settings/erp/disconnect")
    with SessionLocal() as db:
        assert not integrations.load(db).configured


def test_settings_pages_render(client):
    integ = client.get("/settings/integrations").text
    assert "Browse all integrations" in integ and "Zoho Books" in integ
    assert "approve automatically" in client.get("/settings/approvals").text
    assert "Bill forwarding email" in client.get("/settings/email").text
    assert client.get("/settings/nope").status_code == 404


def test_bill_pay_settings_modal(client):
    page = client.get("/bills?modal=settings").text
    assert "Bookit settings" in page and "Accounting sync" in page
    assert "require approval from" in page and "manager@acme.test" in page
    assert 'id="modal-bg"' not in client.get("/bills").text
