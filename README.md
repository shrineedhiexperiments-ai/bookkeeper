# Bookkeeper — automated bill processing

Drop a bill in (upload or email), it is read automatically, routed for approval, and
booked into your accounting system. Zoho Books is supported first; Xero, QuickBooks
and others plug in behind the same connector interface.

```
 upload / email ─▶ store file ─▶ Claude reads the bill ─▶ validate ─┬─▶ needs review (human fixes) ─┐
                                                                     └──────────────┬────────────────┘
                                                                                    ▼
                              approval chain picked by amount (0, 1 or N approvers, in order)
                                                                                    ▼
                                     post to ERP (vendor + bill + PDF attachment) ─▶ posted
                                                                                    └▶ post failed ─▶ retry
```

## What it does

| Stage | Details |
|---|---|
| **Intake** | Web upload (multi-file), an IMAP inbox poller (`bills@yourco.com`), or `POST /api/inbound-email?key=…` with raw MIME for SendGrid/Postmark/Mailgun/Cloudflare forwarding. PDF, PNG, JPEG, GIF, WebP. Inline signature images are ignored. |
| **Extraction** | Claude reads the document and returns vendor, tax ID, bill number, dates, currency, totals, and line items, each mapped to a GL account from your live Zoho chart of accounts. Fields it isn't sure about are flagged, not guessed. |
| **Validation** | Required fields, lines + tax = total, qty × price = amount, duplicate file (SHA-256), duplicate vendor + bill number. Any issue → **Needs review**, with the reasons shown next to the document. |
| **Approval** | Amount-based tiers in `APPROVAL_RULES` (e.g. ≤500 auto, ≤5,000 manager, above that manager then CFO). Approvers get an email with one-click approve/reject links (HMAC-signed, with a confirm page so mail scanners can't trigger them) or use the web UI. Each bill keeps a full audit trail. |
| **Posting** | Finds or creates the vendor, maps accounts and tax rates, creates the bill with `reference_number = BK-<id>`, and attaches the original document. Posting is idempotent: if that vendor + bill number already exists in Zoho, it is linked instead of duplicated. Failures stay visible with a retry button. |
| **Learning** | When a vendor's bill posts to a single account, that account becomes the vendor's default for future bills. |

## Run it

```bash
pip install -r requirements.txt
cp .env.example .env        # fill in ANTHROPIC_API_KEY, Zoho credentials, approval rules
uvicorn app.main:app --reload
# open http://localhost:8000, sign in with your email
```

Run the tests: `python -m pytest -q`. They use a fake ERP, a mocked Zoho API and a mocked Claude endpoint, so they need no credentials.

### Zoho Books setup

1. Go to https://api-console.zoho.com and create a **Self Client**.
2. Generate a grant code with these scopes:
   `ZohoBooks.bills.CREATE,ZohoBooks.bills.READ,ZohoBooks.contacts.CREATE,ZohoBooks.contacts.READ,ZohoBooks.accountants.READ,ZohoBooks.settings.READ`
3. Exchange it for a refresh token:
   ```bash
   curl -X POST "https://accounts.zoho.com/oauth/v2/token?code=GRANT_CODE&client_id=ID&client_secret=SECRET&grant_type=authorization_code"
   ```
4. Put the client ID, secret, refresh token, and organization ID (Zoho Books → Settings → Organization profile) in `.env`. Use the right data centre in `ZOHO_DC` (`com`, `eu`, `in`, `com.au`, …).
5. Set `ZOHO_DEFAULT_EXPENSE_ACCOUNT` to the account to fall back on when the suggested account doesn't exist.

## Code map

```
app/
  main.py          FastAPI routes + web UI
  workflow.py      state machine: ingest, validate, approve/reject, post, vendor learning
  extraction.py    Claude bill extraction (structured output, typed schema)
  mailbox.py       IMAP poller + MIME attachment parsing
  notify.py        approval emails, signed action links
  models.py        Bill, BillLine, AuditEvent, VendorRule
  erp/base.py      ERPConnector interface  ← implement this for Xero / QuickBooks
  erp/zoho_books.py
```

## Adding another ERP

Subclass `ERPConnector` in `app/erp/<name>.py`, implementing:

- `list_expense_accounts()`: account names the extractor can choose from
- `post_bill(bill, attachment)`: find or create the vendor, create the bill, attach the file. It must be idempotent on vendor + bill number.

Then register it in `app/erp/__init__.py:get_connector` and set `ERP=<name>`. The workflow, UI and tests don't change. For **Xero** use `POST /api.xro/2.0/Invoices` with `Type=ACCPAY` and the Attachments endpoint. For **QuickBooks Online** use the `Bill` entity plus `Attachable` upload.

## Not production-ready yet

- **Sign-in is a placeholder**: you type an email and get a signed cookie. Put SSO (Google, Microsoft or Zoho) in front of it before exposing it beyond a trusted network.
- Single organisation, single ERP per deployment. Multi-tenant needs an `org_id` on every table and connector per org.
- Extraction and posting run inside the request. For high volume, move them to a worker queue (RQ, Celery or Arq).
- SQLite by default. Set `DATABASE_URL` to Postgres for real use.
- Multi-currency: the bill currency is recorded, but Zoho takes the currency from the vendor record. Foreign-currency vendors need their currency set in Zoho.
