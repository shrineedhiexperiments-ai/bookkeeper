"""ERP-agnostic interface. Each accounting system (Zoho Books, Xero, QuickBooks, ...)
implements ERPConnector; the rest of the app only talks to this interface."""

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from datetime import date


class ERPError(Exception):
    """Raised when the ERP rejects a request or is unreachable."""


@dataclass
class ERPLine:
    description: str
    quantity: float
    unit_price: float
    account_name: str | None = None
    tax_rate: float | None = None  # percent


@dataclass
class ERPBill:
    vendor_name: str
    bill_number: str
    bill_date: date
    lines: list[ERPLine]
    due_date: date | None = None
    currency: str | None = None
    vendor_tax_id: str | None = None
    notes: str | None = None
    reference: str | None = None  # our internal id, stored on the ERP bill


@dataclass
class Attachment:
    file_name: str
    content: bytes
    mime_type: str


@dataclass
class ERPPostResult:
    erp_bill_id: str
    already_existed: bool = False
    warnings: list[str] = field(default_factory=list)


class ERPConnector(ABC):
    name: str

    @abstractmethod
    def list_expense_accounts(self) -> list[str]:
        """Names of GL accounts a bill line can be booked against (fed to the extractor)."""

    @abstractmethod
    def post_bill(self, bill: ERPBill, attachment: Attachment | None = None) -> ERPPostResult:
        """Create the vendor bill (and vendor if missing). Must be idempotent on
        (vendor, bill_number): if the bill already exists, return it with already_existed=True."""
