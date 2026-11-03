import sqlite3, time
import datetime as dt
from sqlmodel import Session
from app.db import engine
from app.models import *
from app.services import MoneyBook
OP="claude:aentezari-537-2026-10-30"
s0=sqlite3.connect("file:/app/data/vpn.db?mode=ro",uri=True); d=sqlite3.connect("/app/data/manual_backups/pre-aentezari-%s.db"%time.strftime("%Y%m%d-%H%M%S")); s0.backup(d); d.close(); print("backup ok")
with Session(engine) as s:
    e=s.get(LedgerEntry,537); assert e.account_id is None and e.customer_id==154 and e.amount==500000.0 and e.voided_at is None
    e.voided_at=dt.datetime.utcnow(); e.voided_by=OP; e.void_reason="re-posted with account_id=162 (same amount/date) so the panel account row agrees with the customer"; s.add(e)
    s.add(LedgerEntry(type=LedgerType.credit,amount=500000.0,customer_id=154,account_id=162,date=e.date,note=e.note,source=e.source,created_by=OP))
    s.commit(); b=MoneyBook(s); c=s.get(Customer,154); a=s.get(Account,162)
    print("cust net",b.customer_net(c),"acc net",b.account_net(a),"posted",b.account_posted(a),"pending",b.account_pending(a))
