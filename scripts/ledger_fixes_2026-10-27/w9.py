import sqlite3, time
import datetime as dt
from sqlmodel import Session, select
from app.db import engine
from app.models import *
from app.services import MoneyBook
from app.shop_service import linked_debt_lines
OP="claude:seyed-payments-2026-10-27"
s0=sqlite3.connect("file:/app/data/vpn.db?mode=ro",uri=True); d=sqlite3.connect("/app/data/manual_backups/pre-seyedpay2-%s.db"%time.strftime("%Y%m%d-%H%M%S")); s0.backup(d); d.close(); print("backup ok")
now=dt.datetime.utcnow()
with Session(engine) as s:
    mine=s.exec(select(LedgerEntry).where(LedgerEntry.created_by==OP,LedgerEntry.voided_at==None)).all()
    assert len(mine)==2 and {e.amount for e in mine}=={200000.0}
    for e in mine:
        e.voided_at=now; e.voided_by=OP; e.void_reason="re-posted correctly: one 200,000 receipt = 25,000 for Seyed_brother (5 GB) + 175,000 to Seyed"; s.add(e)
    def cr(amt,cid,aid,note): s.add(LedgerEntry(type=LedgerType.credit,amount=amt,customer_id=cid,account_id=aid,date=now,note=note,source=LedgerSource.web,created_by=OP))
    cr(25000.0,53,57,"payment: part of the 200,000 received, covers Seyed_brother's actual use (5 GB x 5,000)")
    cr(225000.0,53,57,"plan charge corrected: only ~5 GB used of the old plan (250,000 -> 25,000), owner 2026-10-27")
    cr(175000.0,5,3,"payment: remainder (175,000) of the 200,000 receipt for Seyed_brother, applied to Seyed's debt, owner 2026-10-27")
    s.commit(); b=MoneyBook(s)
    for c in (5,53): print(c,round(b.customer_net(s.get(Customer,c))))
    for r in linked_debt_lines(s,4): print(r)
