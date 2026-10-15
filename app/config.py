import json
import os
from dataclasses import dataclass, field
from functools import lru_cache


def _load_dotenv(path: str = ".env") -> None:
    """Minimal .env loader so the app runs without extra dependencies."""
    if not os.path.exists(path):
        return
    with open(path) as fh:
        for raw in fh:
            line = raw.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, value = line.split("=", 1)
            value = value.split(" #", 1)[0].strip().strip('"').strip("'")
            os.environ.setdefault(key.strip(), value)


@dataclass(frozen=True)
class ApprovalTier:
    max_amount: float | None  # None = no upper bound
    approvers: list[str]


@dataclass(frozen=True)
class Settings:
    database_url: str
    upload_dir: str
    app_base_url: str
    secret_key: str
    anthropic_model: str
    approval_tiers: list[ApprovalTier]
    erp: str
    imap: dict = field(default_factory=dict)
    smtp: dict = field(default_factory=dict)
    zoho: dict = field(default_factory=dict)


def _tiers(raw: str) -> list[ApprovalTier]:
    tiers = [ApprovalTier(t.get("max_amount"), list(t.get("approvers", []))) for t in json.loads(raw)]
    # Bounded tiers ascending, unbounded tier last.
    return sorted(tiers, key=lambda t: float("inf") if t.max_amount is None else t.max_amount)


@lru_cache
def get_settings() -> Settings:
    _load_dotenv()
    env = os.environ.get
    return Settings(
        database_url=env("DATABASE_URL", "sqlite:///./data/bookkeeper.db"),
        upload_dir=env("UPLOAD_DIR", "./data/uploads"),
        app_base_url=env("APP_BASE_URL", "http://localhost:8000").rstrip("/"),
        secret_key=env("SECRET_KEY", "dev-secret-change-me"),
        anthropic_model=env("ANTHROPIC_MODEL", "claude-opus-5-5"),
        approval_tiers=_tiers(env("APPROVAL_RULES", '[{"max_amount": null, "approvers": []}]')),
        erp=env("ERP", "zoho_books"),
        imap={
            "host": env("IMAP_HOST"),
            "user": env("IMAP_USER"),
            "password": env("IMAP_PASSWORD"),
            "folder": env("IMAP_FOLDER", "INBOX"),
            "poll_seconds": int(env("IMAP_POLL_SECONDS", "120")),
        },
        smtp={
            "host": env("SMTP_HOST"),
            "port": int(env("SMTP_PORT", "587")),
            "user": env("SMTP_USER"),
            "password": env("SMTP_PASSWORD"),
            "from": env("SMTP_FROM"),
        },
        zoho={
            "dc": env("ZOHO_DC", "com"),
            "client_id": env("ZOHO_CLIENT_ID"),
            "client_secret": env("ZOHO_CLIENT_SECRET"),
            "refresh_token": env("ZOHO_REFRESH_TOKEN"),
            "organization_id": env("ZOHO_ORGANIZATION_ID"),
            "default_expense_account": env("ZOHO_DEFAULT_EXPENSE_ACCOUNT"),
        },
    )
