"""Zoho Books v3 connector.

Auth: OAuth2 refresh-token flow (create a "Self Client" at https://api-console.zoho.com,
generate a grant code with the scopes listed in .env.example, exchange it once for a
refresh token). Access tokens are refreshed automatically.
"""

import time

import httpx2

from .base import Attachment, ERPBill, ERPConnector, ERPError, ERPPostResult

# Zoho Books accepts these account types on bill line items.
_EXPENSE_TYPES = {"expense", "cost_of_goods_sold", "other_expense", "fixed_asset", "other_current_asset"}


class ZohoBooksConnector(ERPConnector):
    name = "zoho_books"

    def __init__(
        self,
        *,
        client_id: str,
        client_secret: str,
        refresh_token: str,
        organization_id: str,
        dc: str = "com",
        default_expense_account: str | None = None,
        transport: httpx2.BaseTransport | None = None,
    ):
        if not all([client_id, client_secret, refresh_token, organization_id]):
            raise ERPError("Zoho Books is not configured: set ZOHO_CLIENT_ID/SECRET/REFRESH_TOKEN/ORGANIZATION_ID")
        self.client_id = client_id
        self.client_secret = client_secret
        self.refresh_token = refresh_token
        self.organization_id = organization_id
        self.default_expense_account = default_expense_account
        self.accounts_url = f"https://accounts.zoho.{dc}/oauth/v2/token"
        self.api_url = f"https://www.zohoapis.{dc}/books/v3"
        self.http = httpx2.Client(timeout=30, transport=transport)
        self._token: str | None = None
        self._token_expiry = 0.0
        self._accounts: dict[str, dict] | None = None
        self._taxes: list[dict] | None = None

    # ---------------------------------------------------------------- auth / http

    def _access_token(self) -> str:
        if self._token and time.time() < self._token_expiry - 60:
            return self._token
        resp = self.http.post(
            self.accounts_url,
            params={
                "refresh_token": self.refresh_token,
                "client_id": self.client_id,
                "client_secret": self.client_secret,
                "grant_type": "refresh_token",
            },
        )
        data = resp.json()
        if resp.status_code != 200 or "access_token" not in data:
            raise ERPError(f"Zoho token refresh failed: {data}")
        self._token = data["access_token"]
        self._token_expiry = time.time() + int(data.get("expires_in", 3600))
        return self._token

    def _request(self, method: str, path: str, *, params=None, json=None, files=None, _retry=True) -> dict:
        resp = self.http.request(
            method,
            f"{self.api_url}{path}",
            params={"organization_id": self.organization_id, **(params or {})},
            json=json,
            files=files,
            headers={"Authorization": f"Zoho-oauthtoken {self._access_token()}"},
        )
        if resp.status_code == 401 and _retry:
            self._token = None
            return self._request(method, path, params=params, json=json, files=files, _retry=False)
        if resp.status_code == 429 and _retry:
            time.sleep(int(resp.headers.get("retry-after", "5")))
            return self._request(method, path, params=params, json=json, files=files, _retry=False)
        try:
            data = resp.json()
        except ValueError:
            raise ERPError(f"Zoho {method} {path}: HTTP {resp.status_code} {resp.text[:300]}")
        if resp.status_code >= 400 or data.get("code", 0) != 0:
            raise ERPError(f"Zoho {method} {path}: {data.get('message', resp.text[:300])} (code {data.get('code')})")
        return data

    # ---------------------------------------------------------------- lookups

    def _load_accounts(self) -> dict[str, dict]:
        if self._accounts is None:
            data = self._request("GET", "/chartofaccounts", params={"filter_by": "AccountType.Active"})
            self._accounts = {
                a["account_name"].strip().lower(): a
                for a in data.get("chartofaccounts", [])
                if a.get("account_type") in _EXPENSE_TYPES
            }
        return self._accounts

    def list_expense_accounts(self) -> list[str]:
        return sorted(a["account_name"] for a in self._load_accounts().values())

    def _account_id(self, name: str | None, warnings: list[str]) -> str:
        accounts = self._load_accounts()
        if name and name.strip().lower() in accounts:
            return accounts[name.strip().lower()]["account_id"]
        fallback = self.default_expense_account
        if fallback and fallback.strip().lower() in accounts:
            if name:
                warnings.append(f"Account '{name}' not found in Zoho; used '{fallback}'")
            return accounts[fallback.strip().lower()]["account_id"]
        raise ERPError(f"No Zoho account matches '{name}' and ZOHO_DEFAULT_EXPENSE_ACCOUNT is not set/valid")

    def _tax_id(self, rate: float | None, warnings: list[str]) -> str | None:
        if not rate:
            return None
        if self._taxes is None:
            self._taxes = self._request("GET", "/settings/taxes").get("taxes", [])
        for tax in self._taxes:
            if abs(float(tax.get("tax_percentage", -1)) - rate) < 0.01:
                return tax["tax_id"]
        warnings.append(f"No Zoho tax with rate {rate}% — line booked without tax")
        return None

    def _vendor_id(self, bill: ERPBill) -> str:
        data = self._request(
            "GET", "/contacts", params={"contact_type": "vendor", "contact_name": bill.vendor_name}
        )
        for contact in data.get("contacts", []):
            if contact.get("contact_name", "").strip().lower() == bill.vendor_name.strip().lower():
                return contact["contact_id"]
        payload = {"contact_name": bill.vendor_name, "contact_type": "vendor"}
        if bill.vendor_tax_id:
            payload["notes"] = f"Tax ID: {bill.vendor_tax_id}"
        return self._request("POST", "/contacts", json=payload)["contact"]["contact_id"]

    def _existing_bill(self, vendor_id: str, bill_number: str) -> str | None:
        data = self._request("GET", "/bills", params={"vendor_id": vendor_id, "bill_number": bill_number})
        for b in data.get("bills", []):
            if b.get("bill_number", "").strip().lower() == bill_number.strip().lower():
                return b["bill_id"]
        return None

    # ---------------------------------------------------------------- posting

    def post_bill(self, bill: ERPBill, attachment: Attachment | None = None) -> ERPPostResult:
        warnings: list[str] = []
        vendor_id = self._vendor_id(bill)

        if existing := self._existing_bill(vendor_id, bill.bill_number):
            return ERPPostResult(erp_bill_id=existing, already_existed=True)

        payload = {
            "vendor_id": vendor_id,
            "bill_number": bill.bill_number,
            "date": bill.bill_date.isoformat(),
            "line_items": [],
        }
        if bill.due_date:
            payload["due_date"] = bill.due_date.isoformat()
        if bill.reference:
            payload["reference_number"] = bill.reference
        if bill.notes:
            payload["notes"] = bill.notes[:1000]
        for line in bill.lines:
            item = {
                "account_id": self._account_id(line.account_name, warnings),
                "description": line.description[:2000],
                "rate": round(line.unit_price, 2),
                "quantity": line.quantity,
            }
            if tax_id := self._tax_id(line.tax_rate, warnings):
                item["tax_id"] = tax_id
            payload["line_items"].append(item)

        bill_id = self._request("POST", "/bills", json=payload)["bill"]["bill_id"]

        if attachment:
            try:
                self._request(
                    "POST",
                    f"/bills/{bill_id}/attachment",
                    files={"attachment": (attachment.file_name, attachment.content, attachment.mime_type)},
                )
            except ERPError as exc:
                warnings.append(f"Bill created but attachment upload failed: {exc}")

        return ERPPostResult(erp_bill_id=bill_id, warnings=warnings)
