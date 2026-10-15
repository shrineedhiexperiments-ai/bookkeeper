import json

import anthropic
import httpx2

from app.extraction import extract_bill

BILL_JSON = {
    "is_bill": True, "vendor_name": "Acme", "vendor_tax_id": None, "bill_number": "7",
    "bill_date": "2026-10-01", "due_date": None, "currency": "USD", "subtotal": 100, "tax_total": 0,
    "total": 100, "uncertain_fields": [],
    "lines": [{"description": "x", "quantity": 1, "unit_price": 100, "amount": 100, "tax_rate": None,
               "account_name": "Office Supplies"}],
}


def test_request_shape_and_parsing():
    seen = {}

    def handler(request: httpx2.Request):
        seen["body"] = json.loads(request.content)
        seen["beta"] = request.headers.get("anthropic-beta")
        return httpx2.Response(200, json={
            "id": "msg_1", "type": "message", "role": "assistant", "model": "claude-opus-5-5",
            "content": [{"type": "text", "text": json.dumps(BILL_JSON)}],
            "stop_reason": "end_turn", "stop_sequence": None,
            "usage": {"input_tokens": 10, "output_tokens": 10},
        })

    client = anthropic.Anthropic(api_key="test", http_client=httpx2.Client(transport=httpx2.MockTransport(handler)))
    result = extract_bill(b"%PDF", "application/pdf", ["Office Supplies"], client=client)

    assert result.vendor_name == "Acme" and result.lines[0].account_name == "Office Supplies"
    body = seen["body"]
    assert body["model"] == "claude-opus-5-5"
    assert body["fallbacks"] == "default" and "server-side-fallback-2026-07-01" in seen["beta"]
    assert body["output_config"]["format"]["type"] == "json_schema"
    assert body["messages"][0]["content"][0]["type"] == "document"
