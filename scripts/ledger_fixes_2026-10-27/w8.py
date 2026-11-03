import sqlite3, time
from sqlmodel import Session
from app.db import engine
from app.models import *
from app.services import MoneyBook
from app.shop_service import linked_debt_lines
import datetime
OP="claude:seyed-payments-2026-10-27"
s0=sqlite3.connect("file:/app/data/vpn.db?mode=ro",uri=True); d=sqlite3.connect("/app/data/manual_backups/pre-seyedpay-%s.db"%time.strftime("%Y%m%d-%H%M%S")); s0.backup(d); d.close(); print("backup ok")
now=datetime.datetime.utcnow()
with Session(engine) as s:
    s.add(LedgerEntry(type=LedgerType.credit,amount=200000.0,customer_id=53,account_id=57,date=now,note="payment 200,000 T for Seyed_brother (owner statement 2026-10-27; date not given)",source=LedgerSource.web,created_by=OP))
    s.add(LedgerEntry(type=LedgerType.credit,amount=200000.0,customer_id=5,account_id=3,date=now,note="payment 200,000 T applied to Seyed's debt - remainder of the Seyed_brother payment (owner statement 2026-10-27; date not given)",source=LedgerSource.web,created_by=OP))
    s.commit(); b=MoneyBook(s)
    for c in (5,53): print(c,round(b.customer_net(s.get(Customer,c))))
    for r in linked_debt_lines(s,4): print(r)
