from ..config import get_settings
from .base import Attachment, ERPBill, ERPConnector, ERPError, ERPLine, ERPPostResult

__all__ = ["Attachment", "ERPBill", "ERPConnector", "ERPError", "ERPLine", "ERPPostResult", "get_connector"]

_connector: ERPConnector | None = None


def get_connector() -> ERPConnector:
    """Return the configured ERP connector. Add Xero / QuickBooks here as they land."""
    global _connector
    if _connector is None:
        s = get_settings()
        if s.erp == "zoho_books":
            from .zoho_books import ZohoBooksConnector

            _connector = ZohoBooksConnector(**s.zoho)
        elif s.erp == "demo":
            from .demo import DemoConnector

            _connector = DemoConnector()
        else:
            raise ERPError(f"Unsupported ERP '{s.erp}'")
    return _connector


def set_connector(connector: ERPConnector | None) -> None:
    """Override the connector (tests, or multi-tenant setups)."""
    global _connector
    _connector = connector
