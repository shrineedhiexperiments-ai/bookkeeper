import pytest

from app import workflow
from app.models import BillStatus
from app.workflow import WorkflowError

from .conftest import make_extracted

PDF = b"%PDF-1.4 fake"


def ingest(db, erp, extracted, content=PDF):
    return workflow.ingest(db, content=content, file_name="bill.pdf", mime_type="application/pdf",
                           source="upload", source_ref="clerk@acme.test",
                           extractor=lambda *a, **k: extracted, connector=erp)


def test_small_bill_is_auto_approved_and_posted(db, erp):
    bill = ingest(db, erp, make_extracted(total=118))
    assert bill.status == BillStatus.POSTED
    assert bill.erp_bill_id == "ERP-1"
    posted, attachment = erp.posted[0]
    assert posted.vendor_name == "Acme Paper Co"
    assert posted.lines[0].account_name == "Office Supplies"
    assert attachment.content == PDF


def test_two_step_approval_in_order(db, erp):
    bill = ingest(db, erp, make_extracted(total=11800))
    assert bill.status == BillStatus.PENDING_APPROVAL
    assert bill.approvers == ["manager@acme.test", "cfo@acme.test"]

    with pytest.raises(WorkflowError):
        workflow.approve(db, bill, "cfo@acme.test", connector=erp)  # out of order

    workflow.approve(db, bill, "manager@acme.test", connector=erp)
    assert bill.status == BillStatus.PENDING_APPROVAL and bill.pending_approver == "cfo@acme.test"
    workflow.approve(db, bill, "CFO@acme.test", connector=erp)
    assert bill.status == BillStatus.POSTED
    assert [e.action for e in bill.events][-3:] == ["approved", "approved_final", "posted"]


def test_reject_stops_workflow(db, erp):
    bill = ingest(db, erp, make_extracted(total=1180))
    workflow.reject(db, bill, "manager@acme.test", "not ours")
    assert bill.status == BillStatus.REJECTED
    assert erp.posted == []


def test_totals_mismatch_needs_review(db, erp):
    bill = ingest(db, erp, make_extracted(total=118, tax_total=50))
    assert bill.status == BillStatus.NEEDS_REVIEW
    assert any("do not add up" in r for r in bill.review_reasons)


def test_missing_fields_and_uncertainty_need_review(db, erp):
    bill = ingest(db, erp, make_extracted(bill_number=None, uncertain_fields=["due_date"]))
    assert bill.status == BillStatus.NEEDS_REVIEW
    assert "Missing bill number" in bill.review_reasons
    assert "Low confidence: due_date" in bill.review_reasons


def test_duplicates_are_flagged(db, erp):
    ingest(db, erp, make_extracted())
    same_file = ingest(db, erp, make_extracted())
    assert "Same file was already received" in same_file.review_reasons
    same_number = ingest(db, erp, make_extracted(), content=b"%PDF other scan")
    assert any("Possible duplicate" in r for r in same_number.review_reasons)


def test_post_failure_can_be_retried(db, erp):
    erp.fail_with = "Zoho down"
    bill = ingest(db, erp, make_extracted())
    assert bill.status == BillStatus.POST_FAILED and "Zoho down" in bill.erp_error
    erp.fail_with = None
    workflow.post_to_erp(db, bill, connector=erp)
    assert bill.status == BillStatus.POSTED


def test_vendor_account_is_learned(db, erp):
    first = ingest(db, erp, make_extracted())
    first.lines[0].account_name  # posted with Office Supplies
    second = ingest(db, erp, make_extracted(
        number="INV-2",
        lines=[make_extracted().lines[0].model_copy(update={"account_name": "Other Expenses"})],
    ), content=b"%PDF second")
    assert second.lines[0].account_name == "Office Supplies"


def test_extraction_failure_goes_to_review(db, erp):
    from app.extraction import ExtractionError

    def boom(*a, **k):
        raise ExtractionError("unreadable")

    bill = workflow.ingest(db, content=PDF, file_name="x.pdf", mime_type="application/pdf",
                           source="email", extractor=boom, connector=erp)
    assert bill.status == BillStatus.NEEDS_REVIEW


def test_unsupported_file_type_rejected(db, erp):
    with pytest.raises(WorkflowError):
        workflow.ingest(db, content=b"x", file_name="a.docx", mime_type="application/msword",
                        source="upload", connector=erp)
