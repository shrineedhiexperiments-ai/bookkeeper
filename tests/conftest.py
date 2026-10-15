import os
import tempfile

_tmp = tempfile.mkdtemp(prefix="bk-test-")
os.environ.update({
    "DATABASE_URL": f"sqlite:///{_tmp}/test.db",
    "UPLOAD_DIR": f"{_tmp}/uploads",
    "SECRET_KEY": "test-secret",
    "APPROVAL_RULES": '[{"max_amount": 500, "approvers": []},'
                      ' {"max_amount": 5000, "approvers": ["manager@acme.test"]},'
                      ' {"max_amount": null, "approvers": ["manager@acme.test", "cfo@acme.test"]}]',
    "IMAP_HOST": "",
    "SMTP_HOST": "",
})

import pytest  # noqa: E402

from app.db import Base, engine, SessionLocal, init_db  # noqa: E402
from app.erp import ERPConnector, ERPError, ERPPostResult, set_connector  # noqa: E402
from app.extraction import ExtractedBill, ExtractedLine  # noqa: E402


class FakeConnector(ERPConnector):
    name = "fake_erp"

    def __init__(self):
        self.posted = []
        self.fail_with: str | None = None

    def list_expense_accounts(self):
        return ["Office Supplies", "Software Subscriptions", "Other Expenses"]

    def post_bill(self, bill, attachment=None):
        if self.fail_with:
            raise ERPError(self.fail_with)
        self.posted.append((bill, attachment))
        return ERPPostResult(erp_bill_id=f"ERP-{len(self.posted)}")


def make_extracted(total=118.0, vendor="Acme Paper Co", number="INV-1001", **overrides) -> ExtractedBill:
    subtotal = round(total / 1.18, 2)
    data = dict(
        is_bill=True, vendor_name=vendor, vendor_tax_id=None, bill_number=number,
        bill_date="2026-10-01", due_date="2026-10-31", currency="USD",
        subtotal=subtotal, tax_total=round(total - subtotal, 2), total=total,
        lines=[ExtractedLine(description="Printer paper", quantity=1, unit_price=subtotal, amount=subtotal,
                             tax_rate=18, account_name="Office Supplies")],
        uncertain_fields=[],
    )
    data.update(overrides)
    return ExtractedBill(**data)


@pytest.fixture(autouse=True)
def fresh_db():
    Base.metadata.drop_all(engine)
    init_db()
    yield


@pytest.fixture
def db():
    with SessionLocal() as session:
        yield session


@pytest.fixture
def erp():
    connector = FakeConnector()
    set_connector(connector)
    yield connector
    set_connector(None)
