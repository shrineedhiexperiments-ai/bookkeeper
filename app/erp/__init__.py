from .base import Attachment, ERPBill, ERPConnector, ERPError, ERPLine, ERPPostResult

__all__ = ["Attachment", "ERPBill", "ERPConnector", "ERPError", "ERPLine", "ERPPostResult", "build_connector", "get_connector", "set_connector"]

_connector: ERPConnector | None = None


def get_connector() -> ERPConnector:
    """Return the configured ERP connector. Add Xero / QuickBooks here as they land."""
    global _connector
    if _connector is None:
        _connector = build_connector()
    return _connector


def build_connector() -> ERPConnector:
    """Build a fresh connector from the Settings page values, falling back to .env."""
    from ..integrations import load

    cfg = load()
    if cfg.erp == "zoho_books":
        from .zoho_books import ZohoBooksConnector

        return ZohoBooksConnector(**cfg.zoho)
    raise ERPError(f"Unsupported ERP '{cfg.erp}'")


def set_connector(connector: ERPConnector | None) -> None:
    """Override the connector (tests, or multi-tenant setups)."""
    global _connector
    _connector = connector
