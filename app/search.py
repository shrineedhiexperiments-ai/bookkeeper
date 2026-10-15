"""Bill search: turns a free-text query into structured filters.

Examples of what a query can contain (terms are combined with AND):
  acme                     text in vendor, invoice #, file name, tax ID, notes or line descriptions
  vendor:"acme paper"      vendor name only          invoice:INV-1   invoice number only
  1200  $1,200.50          exact amount (a bare number also matches invoice numbers)
  >500  <=1000  500-1000   amount comparisons and ranges
  2026-10-01  10/01/2026   bill or due date on that day
  oct  october 2026  oct 5 2026-10   month / day / month-of-year
  from:2026-10-01  to:2026-10-31   bill date range
  USD  inr  €  currency:EUR       currency
  posted  rejected  review  status:failed   status
"""

import re
import shlex
from dataclasses import dataclass, field
from datetime import date, timedelta

from sqlalchemy import and_, exists, extract, func, or_, select

from .models import Bill, BillLine, BillStatus

CURRENCIES = {"USD", "EUR", "GBP", "INR", "JPY", "AUD", "CAD", "SGD", "CHF", "CNY", "AED", "SAR", "NZD", "HKD", "ZAR", "SEK", "NOK", "DKK", "MXN", "BRL"}
SYMBOLS = {"$": "USD", "€": "EUR", "£": "GBP", "₹": "INR", "¥": "JPY"}
MONTHS = {m: i + 1 for i, m in enumerate(["jan", "feb", "mar", "apr", "may", "jun", "jul", "aug", "sep", "oct", "nov", "dec"])}
STATUS_WORDS = {
    "received": BillStatus.RECEIVED, "review": BillStatus.NEEDS_REVIEW, "needs_review": BillStatus.NEEDS_REVIEW,
    "pending": BillStatus.PENDING_APPROVAL, "approval": BillStatus.PENDING_APPROVAL, "pending_approval": BillStatus.PENDING_APPROVAL,
    "approved": BillStatus.APPROVED, "rejected": BillStatus.REJECTED, "posted": BillStatus.POSTED,
    "failed": BillStatus.POST_FAILED, "post_failed": BillStatus.POST_FAILED,
}

_NUM = r"[$€£₹¥]?\s*\d[\d,]*(?:\.\d+)?"
_CMP = re.compile(rf"^(>=|<=|>|<)\s*({_NUM})$")
_RANGE = re.compile(rf"^({_NUM})\s*(?:-|\.\.|–)\s*({_NUM})$")
_AMOUNT = re.compile(rf"^{_NUM}$")


def _num(raw: str) -> float:
    return float(re.sub(r"[^\d.]", "", raw))


def _month(tok: str) -> int | None:
    t = tok.lower().rstrip(".,")
    return MONTHS.get(t[:3]) if len(t) >= 3 and t[:3] in MONTHS and t.isalpha() else None


def _parse_date(tok: str) -> date | None:
    for pat, order in ((r"^(\d{4})-(\d{1,2})-(\d{1,2})$", "ymd"), (r"^(\d{1,2})/(\d{1,2})/(\d{4})$", "mdy")):
        if m := re.match(pat, tok):
            a, b, c = map(int, m.groups())
            try:
                return date(a, b, c) if order == "ymd" else date(c, a, b)
            except ValueError:
                return None
    return None


@dataclass
class Term:
    raw: str          # the query text this term came from (removed when its chip is cleared)
    label: str        # what the chip shows
    kind: str         # text | vendor | invoice | amount | currency | date | status
    clause: object = field(repr=False, default=None)


def parse(q: str) -> list[Term]:
    try:
        toks = shlex.split(q or "")
    except ValueError:  # unbalanced quotes
        toks = (q or "").replace('"', " ").split()
    terms: list[Term] = []
    i = 0
    while i < len(toks):
        tok, nxt = toks[i], toks[i + 1] if i + 1 < len(toks) else None
        raw = shlex.quote(tok) if " " in tok else tok
        key, _, val = tok.partition(":") if re.match(r"^[a-z]+:.+", tok, re.I) else ("", "", tok)
        key = key.lower()
        i += 1

        if key == "vendor":
            terms.append(Term(raw, f"Vendor: {val}", "vendor", Bill.vendor_name.ilike(f"%{val}%")))
        elif key in ("invoice", "bill", "number"):
            terms.append(Term(raw, f"Invoice: {val}", "invoice", Bill.bill_number.ilike(f"%{val}%")))
        elif key in ("from", "to", "after", "before") and (d := _parse_date(val)):
            if key in ("from", "after"):
                terms.append(Term(raw, f"From {d:%b %d, %Y}", "date", Bill.bill_date >= d))
            else:
                terms.append(Term(raw, f"To {d:%b %d, %Y}", "date", Bill.bill_date <= d))
        elif key in ("currency", "cur") and val.upper() in CURRENCIES | set(SYMBOLS.values()):
            terms.append(Term(raw, f"Currency: {val.upper()}", "currency", Bill.currency == val.upper()))
        elif key == "status" and val.lower() in STATUS_WORDS:
            st = STATUS_WORDS[val.lower()]
            terms.append(Term(raw, f"Status: {st.value.replace('_', ' ')}", "status", Bill.status == st))
        elif key == "amount" and (_AMOUNT.match(val) or _CMP.match(val) or _RANGE.match(val)):
            toks.insert(i, val)  # re-parse the value as an amount term
            continue
        elif tok.upper() in CURRENCIES:
            terms.append(Term(raw, f"Currency: {tok.upper()}", "currency", Bill.currency == tok.upper()))
        elif tok in SYMBOLS:
            terms.append(Term(raw, f"Currency: {SYMBOLS[tok]}", "currency", Bill.currency == SYMBOLS[tok]))
        elif tok.lower() in STATUS_WORDS:
            st = STATUS_WORDS[tok.lower()]
            terms.append(Term(raw, f"Status: {st.value.replace('_', ' ')}", "status", Bill.status == st))
        elif d := _parse_date(tok):
            terms.append(Term(raw, f"Date: {d:%b %d, %Y}", "date", or_(Bill.bill_date == d, Bill.due_date == d)))
        elif m := re.match(r"^(\d{4})-(\d{1,2})$", tok):
            y, mo = int(m.group(1)), int(m.group(2))
            if 1 <= mo <= 12:
                start = date(y, mo, 1)
                end = (start + timedelta(days=32)).replace(day=1)
                terms.append(Term(raw, f"Date: {start:%b %Y}", "date", and_(Bill.bill_date >= start, Bill.bill_date < end)))
        elif (mo := _month(tok)) is not None:
            # "oct", "oct 2026", "oct 5", "oct 5 2026"
            day = year = None
            if nxt and re.fullmatch(r"\d{1,2},?", nxt) and 1 <= int(nxt.rstrip(",")) <= 31:
                day, raw, i = int(nxt.rstrip(",")), f"{raw} {nxt}", i + 1
                nxt = toks[i] if i < len(toks) else None
            if nxt and re.fullmatch(r"(19|20)\d{2}", nxt):
                year, raw, i = int(nxt), f"{raw} {nxt}", i + 1
            conds = [extract("month", Bill.bill_date) == mo]
            if day:
                conds.append(extract("day", Bill.bill_date) == day)
            if year:
                conds.append(extract("year", Bill.bill_date) == year)
            label = date(2000, mo, 1).strftime("%b") + (f" {day}" if day else "") + (f", {year}" if year else "")
            terms.append(Term(raw, f"Date: {label}", "date", and_(*conds)))
        elif m := _CMP.match(tok):
            op, n = m.group(1), _num(m.group(2))
            col = Bill.total
            clause = {">": col > n, ">=": col >= n, "<": col < n, "<=": col <= n}[op]
            words = {">": "over", ">=": "at least", "<": "under", "<=": "at most"}[op]
            terms.append(Term(raw, f"Amount {words} {n:,.2f}", "amount", clause))
        elif m := _RANGE.match(tok):
            lo, hi = sorted((_num(m.group(1)), _num(m.group(2))))
            terms.append(Term(raw, f"Amount {lo:,.2f}–{hi:,.2f}", "amount", Bill.total.between(lo, hi)))
        elif _AMOUNT.match(tok):
            n = _num(tok)
            clause = func.abs(Bill.total - n) < 0.005
            if not re.match(r"^[$€£₹¥]", tok):  # bare digits may also be part of an invoice number
                clause = or_(clause, Bill.bill_number.ilike(f"%{tok}%"))
            terms.append(Term(raw, f"Amount {n:,.2f}", "amount", clause))
        else:
            like = f"%{val}%"
            line_match = exists(select(BillLine.id).where(BillLine.bill_id == Bill.id, BillLine.description.ilike(like)))
            terms.append(Term(raw, f"“{val}”", "text", or_(
                Bill.vendor_name.ilike(like), Bill.bill_number.ilike(like), Bill.file_name.ilike(like),
                Bill.vendor_tax_id.ilike(like), Bill.notes.ilike(like), line_match,
            )))
    return terms


def apply(query, terms: list[Term]):
    for t in terms:
        if t.clause is not None:
            query = query.where(t.clause)
    return query


def without(terms: list[Term], index: int) -> str:
    """The query string with one term removed (for a chip's ✕)."""
    return " ".join(t.raw for j, t in enumerate(terms) if j != index)
