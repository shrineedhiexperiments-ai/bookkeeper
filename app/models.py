import enum
from datetime import date, datetime, timezone

from sqlalchemy import JSON, Date, DateTime, Enum, Float, ForeignKey, Integer, String, Text
from sqlalchemy.orm import Mapped, mapped_column, relationship

from .db import Base


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


class BillStatus(str, enum.Enum):
    RECEIVED = "received"                # file stored, not yet read
    NEEDS_REVIEW = "needs_review"        # extraction failed / incomplete / possible duplicate
    PENDING_APPROVAL = "pending_approval"
    APPROVED = "approved"
    REJECTED = "rejected"
    POSTED = "posted"                    # booked in the ERP
    POST_FAILED = "post_failed"


class Bill(Base):
    __tablename__ = "bills"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    status: Mapped[BillStatus] = mapped_column(Enum(BillStatus), default=BillStatus.RECEIVED, index=True)
    source: Mapped[str] = mapped_column(String(20))  # upload | email
    source_ref: Mapped[str | None] = mapped_column(String(255))  # sender / message-id
    file_path: Mapped[str] = mapped_column(String(500))
    file_name: Mapped[str] = mapped_column(String(255))
    mime_type: Mapped[str] = mapped_column(String(100))
    file_sha256: Mapped[str] = mapped_column(String(64), index=True)

    vendor_name: Mapped[str | None] = mapped_column(String(255))
    vendor_tax_id: Mapped[str | None] = mapped_column(String(64))
    bill_number: Mapped[str | None] = mapped_column(String(100), index=True)
    bill_date: Mapped[date | None] = mapped_column(Date)
    due_date: Mapped[date | None] = mapped_column(Date)
    currency: Mapped[str | None] = mapped_column(String(3))
    subtotal: Mapped[float | None] = mapped_column(Float)
    tax_total: Mapped[float | None] = mapped_column(Float)
    total: Mapped[float | None] = mapped_column(Float)
    notes: Mapped[str | None] = mapped_column(Text)
    review_reasons: Mapped[list] = mapped_column(JSON, default=list)
    extraction_raw: Mapped[dict | None] = mapped_column(JSON)

    # Approval chain resolved when the bill is submitted (copied so later rule changes don't affect it).
    approvers: Mapped[list] = mapped_column(JSON, default=list)
    current_step: Mapped[int] = mapped_column(Integer, default=0)

    erp: Mapped[str | None] = mapped_column(String(40))
    erp_bill_id: Mapped[str | None] = mapped_column(String(100))
    erp_error: Mapped[str | None] = mapped_column(Text)

    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, onupdate=utcnow)

    lines: Mapped[list["BillLine"]] = relationship(
        back_populates="bill", cascade="all, delete-orphan", order_by="BillLine.position"
    )
    events: Mapped[list["AuditEvent"]] = relationship(
        back_populates="bill", cascade="all, delete-orphan", order_by="AuditEvent.id"
    )

    @property
    def pending_approver(self) -> str | None:
        if self.status != BillStatus.PENDING_APPROVAL or self.current_step >= len(self.approvers):
            return None
        return self.approvers[self.current_step]


class BillLine(Base):
    __tablename__ = "bill_lines"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    bill_id: Mapped[int] = mapped_column(ForeignKey("bills.id", ondelete="CASCADE"), index=True)
    position: Mapped[int] = mapped_column(Integer, default=0)
    description: Mapped[str] = mapped_column(Text)
    quantity: Mapped[float] = mapped_column(Float, default=1)
    unit_price: Mapped[float] = mapped_column(Float)
    amount: Mapped[float] = mapped_column(Float)
    tax_rate: Mapped[float | None] = mapped_column(Float)  # percent, e.g. 18.0
    account_name: Mapped[str | None] = mapped_column(String(255))  # GL account in the ERP

    bill: Mapped[Bill] = relationship(back_populates="lines")


class AuditEvent(Base):
    __tablename__ = "audit_events"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    bill_id: Mapped[int] = mapped_column(ForeignKey("bills.id", ondelete="CASCADE"), index=True)
    actor: Mapped[str] = mapped_column(String(255))
    action: Mapped[str] = mapped_column(String(50))
    detail: Mapped[str | None] = mapped_column(Text)
    at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)

    bill: Mapped[Bill] = relationship(back_populates="events")


class VendorRule(Base):
    """Learned defaults per vendor: the GL account last used when a bill was posted."""

    __tablename__ = "vendor_rules"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    vendor_key: Mapped[str] = mapped_column(String(255), unique=True)
    default_account: Mapped[str | None] = mapped_column(String(255))
