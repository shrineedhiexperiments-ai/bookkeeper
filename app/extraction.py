"""Read a bill (PDF or image) with Claude and return structured data."""

import base64

import anthropic
from pydantic import BaseModel, Field

from .config import get_settings

SUPPORTED_IMAGE_TYPES = {"image/png", "image/jpeg", "image/gif", "image/webp"}
SUPPORTED_TYPES = SUPPORTED_IMAGE_TYPES | {"application/pdf"}


class ExtractedLine(BaseModel):
    description: str
    quantity: float
    unit_price: float = Field(description="Price per unit before tax")
    amount: float = Field(description="Line total before tax")
    tax_rate: float | None = Field(description="Tax percent applied to this line, e.g. 18 for 18%; null if none")
    account_name: str | None = Field(
        description="Best matching expense account, copied exactly from the provided chart of accounts; null if unsure"
    )


class ExtractedBill(BaseModel):
    is_bill: bool = Field(description="False if the document is not a vendor bill/invoice (e.g. a newsletter or receipt for a refund)")
    vendor_name: str | None
    vendor_tax_id: str | None = Field(description="GSTIN / VAT / ABN / EIN printed for the vendor")
    bill_number: str | None = Field(description="The vendor's invoice number")
    bill_date: str | None = Field(description="Invoice date as YYYY-MM-DD")
    due_date: str | None = Field(description="Due date as YYYY-MM-DD; null if not stated")
    currency: str | None = Field(description="ISO 4217 code, e.g. USD, INR, EUR")
    subtotal: float | None
    tax_total: float | None
    total: float | None = Field(description="Grand total payable")
    lines: list[ExtractedLine]
    uncertain_fields: list[str] = Field(description="Names of fields you could not read confidently")


SYSTEM = """You are an accounts-payable clerk. Extract the vendor bill exactly as printed.
- Do not invent values: use null when a field is absent or illegible, and list it in uncertain_fields.
- Amounts are plain numbers without currency symbols or thousands separators.
- If the bill has no itemisation, return one line covering the whole subtotal.
- Pick account_name only from the chart of accounts given."""


class ExtractionError(Exception):
    pass


def _document_block(content: bytes, mime_type: str) -> dict:
    data = base64.standard_b64encode(content).decode()
    if mime_type == "application/pdf":
        return {"type": "document", "source": {"type": "base64", "media_type": mime_type, "data": data}}
    if mime_type in SUPPORTED_IMAGE_TYPES:
        return {"type": "image", "source": {"type": "base64", "media_type": mime_type, "data": data}}
    raise ExtractionError(f"Unsupported file type {mime_type}")


def extract_bill(
    content: bytes,
    mime_type: str,
    accounts: list[str],
    client: anthropic.Anthropic | None = None,
) -> ExtractedBill:
    client = client or anthropic.Anthropic()
    prompt = "Chart of accounts:\n" + ("\n".join(f"- {a}" for a in accounts) or "(none available)")
    prompt += "\n\nExtract this bill."

    try:
        response = client.beta.messages.parse(
            model=get_settings().anthropic_model,
            max_tokens=16000,
            system=SYSTEM,
            output_config={"effort": "medium"},
            output_format=ExtractedBill,
            # If a safety classifier declines, let the API retry on a suitable fallback model.
            betas=["server-side-fallback-2026-07-01"],
            fallbacks="default",
            messages=[{"role": "user", "content": [_document_block(content, mime_type), {"type": "text", "text": prompt}]}],
        )
    except anthropic.APIConnectionError as exc:
        raise ExtractionError(f"Could not reach the Claude API: {exc}") from exc
    except anthropic.RateLimitError as exc:
        raise ExtractionError("Claude API rate limit hit; retry shortly") from exc
    except anthropic.APIStatusError as exc:
        raise ExtractionError(f"Claude API error {exc.status_code}: {exc.message}") from exc

    if response.stop_reason == "refusal":
        raise ExtractionError("Claude declined to process this document")
    if response.stop_reason == "max_tokens" or response.parsed_output is None:
        raise ExtractionError(f"Extraction incomplete (stop_reason={response.stop_reason})")
    return response.parsed_output
