"""ERP integration settings managed from the Settings page.

Values saved in the UI are stored in the integration_settings table and take precedence
over .env; anything not saved falls back to .env. Secrets are never sent back to the browser.
"""

import json
from dataclasses import dataclass
from datetime import datetime, timezone

from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import Session

from .config import get_settings
from .db import SessionLocal
from .models import IntegrationSetting

ZOHO_FIELDS = ["dc", "organization_id", "client_id", "client_secret", "refresh_token", "default_expense_account"]
SECRET_FIELDS = {"client_secret", "refresh_token"}
REQUIRED_FIELDS = {"organization_id", "client_id", "client_secret", "refresh_token"}
ZOHO_DCS = {
    "com": "United States (zoho.com)",
    "eu": "Europe (zoho.eu)",
    "in": "India (zoho.in)",
    "com.au": "Australia (zoho.com.au)",
    "jp": "Japan (zoho.jp)",
    "ca": "Canada (zoho.ca)",
    "sa": "Saudi Arabia (zoho.sa)",
}

# Connectors shown on the Settings page; only those with available=True can be selected.
ERP_CATALOG = [
    {"id": "zoho_books", "name": "Zoho Books", "available": True},
    {"id": "xero", "name": "Xero", "available": False},
    {"id": "quickbooks", "name": "QuickBooks Online", "available": False},
]

_TEST_KEY = "erp.last_test"


def _key(field: str) -> str:
    return f"zoho.{field}"


def _saved(db: Session) -> dict[str, str]:
    return {s.key: s.value for s in db.query(IntegrationSetting).all() if s.value}


@dataclass
class FieldState:
    name: str
    value: str | None   # None for secrets: never rendered
    is_set: bool
    source: str         # "settings" | "env" | ""


@dataclass
class ERPConfig:
    erp: str
    zoho: dict
    fields: dict[str, FieldState]
    last_test: dict | None

    @property
    def configured(self) -> bool:
        return all(self.zoho.get(f) for f in REQUIRED_FIELDS)


def load(db: Session | None = None) -> ERPConfig:
    """Effective ERP config: saved settings first, then .env."""
    env = get_settings()
    try:
        if db is None:
            with SessionLocal() as own:
                saved = _saved(own)
        else:
            saved = _saved(db)
    except SQLAlchemyError:  # table not created yet
        saved = {}

    zoho, fields = {}, {}
    for f in ZOHO_FIELDS:
        stored = saved.get(_key(f))
        from_env = env.zoho.get(f)
        value = stored or from_env
        zoho[f] = value
        fields[f] = FieldState(
            name=f,
            value=None if f in SECRET_FIELDS else value,
            is_set=bool(value),
            source="settings" if stored else ("env" if from_env else ""),
        )
    zoho["dc"] = zoho.get("dc") or "com"
    last_test = json.loads(saved[_TEST_KEY]) if saved.get(_TEST_KEY) else None
    return ERPConfig(erp=saved.get("erp") or env.erp, zoho=zoho, fields=fields, last_test=last_test)


def _put(db: Session, key: str, value: str | None, user: str) -> None:
    row = db.get(IntegrationSetting, key)
    if row is None:
        db.add(IntegrationSetting(key=key, value=value, updated_by=user))
    else:
        row.value, row.updated_by = value, user


def save_zoho(db: Session, form: dict[str, str], user: str) -> None:
    """Save the Zoho form. Blank secret fields keep their current value."""
    dc = (form.get("dc") or "com").strip()
    if dc not in ZOHO_DCS:
        raise ValueError(f"Unknown Zoho data centre '{dc}'")
    _put(db, "erp", "zoho_books", user)
    for f in ZOHO_FIELDS:
        value = dc if f == "dc" else (form.get(f) or "").strip()
        if f in SECRET_FIELDS and not value:
            continue
        _put(db, _key(f), value or None, user)
    _put(db, _TEST_KEY, None, user)  # settings changed; previous test result no longer applies


def record_test(db: Session, ok: bool, message: str, user: str) -> None:
    result = {"ok": ok, "message": message, "at": datetime.now(timezone.utc).isoformat(timespec="seconds"), "by": user}
    _put(db, _TEST_KEY, json.dumps(result), user)


def disconnect(db: Session) -> None:
    """Forget everything saved from the UI (.env values, if any, apply again)."""
    db.query(IntegrationSetting).filter(
        (IntegrationSetting.key.like("zoho.%")) | (IntegrationSetting.key.in_(["erp", _TEST_KEY]))
    ).delete(synchronize_session=False)
