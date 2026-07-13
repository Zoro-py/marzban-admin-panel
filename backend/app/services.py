from typing import Optional

from sqlmodel import Session, select

from app.models import LedgerEntry, LedgerType


def compute_balance(session: Session, *, customer_id: Optional[int] = None, group_id: Optional[int] = None) -> tuple[float, float]:
    """Returns (total_charge, total_credit) for a customer or a group."""
    if (customer_id is None) == (group_id is None):
        raise ValueError("compute_balance needs exactly one of customer_id/group_id")

    stmt = select(LedgerEntry)
    stmt = stmt.where(LedgerEntry.customer_id == customer_id) if customer_id is not None else stmt.where(
        LedgerEntry.group_id == group_id
    )
    entries = session.exec(stmt).all()

    total_charge = sum(e.amount for e in entries if e.type == LedgerType.charge)
    total_credit = sum(e.amount for e in entries if e.type == LedgerType.credit)
    return total_charge, total_credit


GB = 1024**3


def bytes_from_gb(gb: float) -> int:
    return round(gb * GB)
