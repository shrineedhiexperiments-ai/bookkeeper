"""Stand-in ERP for trying the app without accounting-system credentials.
Bills are "booked" into a local JSON file (data/demo_erp.json)."""

import json
import os
from dataclasses import asdict

from .base import Attachment, ERPBill, ERPConnector, ERPPostResult

ACCOUNTS = [
    "Advertising & Marketing", "Cost of Goods Sold", "Office Supplies", "Other Expenses",
    "Professional Fees", "Rent", "Software Subscriptions", "Telephone & Internet",
    "Travel", "Utilities",
]


class DemoConnector(ERPConnector):
    name = "demo"

    def __init__(self, path: str = "./data/demo_erp.json"):
        self.path = path

    def _load(self) -> list[dict]:
        if not os.path.exists(self.path):
            return []
        with open(self.path) as fh:
            return json.load(fh)

    def list_expense_accounts(self) -> list[str]:
        return ACCOUNTS

    def post_bill(self, bill: ERPBill, attachment: Attachment | None = None) -> ERPPostResult:
        bills = self._load()
        for b in bills:
            if (b["vendor_name"].lower(), b["bill_number"].lower()) == (bill.vendor_name.lower(), bill.bill_number.lower()):
                return ERPPostResult(erp_bill_id=b["id"], already_existed=True)
        record = {"id": f"DEMO-{len(bills) + 1}", **asdict(bill), "attachment": attachment.file_name if attachment else None}
        bills.append(record)
        os.makedirs(os.path.dirname(self.path) or ".", exist_ok=True)
        with open(self.path, "w") as fh:
            json.dump(bills, fh, indent=2, default=str)
        return ERPPostResult(erp_bill_id=record["id"])
